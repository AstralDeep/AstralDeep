"""Authenticated, bounded foreground PCM bridge for watchOS.

The bridge is a last-mile adapter around the same serialized worker session,
Silero VAD, ASR, TTS, transcript proof, and coordinator control channel used by
direct RTC clients.  It retains at most bounded in-memory PCM and never writes
audio, tickets, transcripts, or provider payloads to disk or diagnostics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import struct
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

from websockets.exceptions import ConnectionClosedOK

from .session import (
    AUDIO_FRAME_SAMPLES,
    AUDIO_STREAM_SAMPLE_RATE,
    MAX_ANNOUNCEMENT_ENVELOPE_BYTES,
    OUTPUT_FRAME_SAMPLES,
    OUTPUT_SAMPLE_RATE,
    DirectRtcSession,
    RtcSessionError,
    SessionBinding,
    SessionNotice,
    SessionSupervisor,
    _OwnedEvent,
    _SpeechMeta,
)
from .watch_ticket import WatchTicketClaims, WatchTicketError, verify_watch_ticket


WATCH_BRIDGE_PATH = "/api/voice/watch-bridge"
WATCH_PCM_MAGIC = b"ADVC"
WATCH_PCM_VERSION = 1
WATCH_MICROPHONE_KIND = 1
WATCH_ASSISTANT_KIND = 2
WATCH_HEADER = struct.Struct(">4sBBHQQH")
WATCH_CAPTURE_BYTES = 640
WATCH_PLAYBACK_BYTES = 960
WATCH_MAX_CONTROL_BYTES = 12 * 1024
WATCH_MAX_RATE_FRAMES = 60
WATCH_RATE_WINDOW_SECONDS = 1.0
WATCH_MAX_SESSION_SECONDS = 300
WATCH_BRIDGE_QUEUE_SIZE = 8
_NONCE_IDENTITY = re.compile(r"^watch-([0-9a-f]{64})$")


class WatchBridgeError(RuntimeError):
    """Content-free bridge refusal safe for a close reason."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BridgeSocket(Protocol):
    request: Any

    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class WatchPcmFrame:
    """Strict fixed-header ADVC frame parser/encoder."""

    __slots__ = ("kind", "sequence", "timestamp_us", "payload")

    def __init__(
        self,
        *,
        kind: int,
        sequence: int,
        timestamp_us: int,
        payload: bytes,
    ) -> None:
        if kind not in {WATCH_MICROPHONE_KIND, WATCH_ASSISTANT_KIND}:
            raise WatchBridgeError("invalid_audio_kind")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 <= sequence < 2**64
            or isinstance(timestamp_us, bool)
            or not isinstance(timestamp_us, int)
            or not 0 <= timestamp_us < 2**64
        ):
            raise WatchBridgeError("invalid_audio_sequence")
        expected = (
            WATCH_CAPTURE_BYTES
            if kind == WATCH_MICROPHONE_KIND
            else WATCH_PLAYBACK_BYTES
        )
        if not isinstance(payload, bytes) or len(payload) != expected:
            raise WatchBridgeError("invalid_audio_length")
        self.kind = kind
        self.sequence = sequence
        self.timestamp_us = timestamp_us
        self.payload = payload

    @classmethod
    def parse_microphone(cls, value: bytes) -> WatchPcmFrame:
        if not isinstance(value, bytes) or len(value) != WATCH_HEADER.size + (
            WATCH_CAPTURE_BYTES
        ):
            raise WatchBridgeError("invalid_audio_length")
        magic, version, kind, flags, sequence, timestamp_us, length = (
            WATCH_HEADER.unpack_from(value)
        )
        if magic != WATCH_PCM_MAGIC or version != WATCH_PCM_VERSION or flags != 0:
            raise WatchBridgeError("invalid_audio_header")
        if kind != WATCH_MICROPHONE_KIND or length != WATCH_CAPTURE_BYTES:
            raise WatchBridgeError("invalid_audio_kind")
        return cls(
            kind=kind,
            sequence=sequence,
            timestamp_us=timestamp_us,
            payload=bytes(value[WATCH_HEADER.size :]),
        )

    def encode(self) -> bytes:
        return WATCH_HEADER.pack(
            WATCH_PCM_MAGIC,
            WATCH_PCM_VERSION,
            self.kind,
            0,
            self.sequence,
            self.timestamp_us,
            len(self.payload),
        ) + self.payload


class WatchTicketReplayStore:
    """Bounded in-memory one-time nonce consumption with eager expiry."""

    def __init__(self, *, capacity: int = 1_024) -> None:
        if not 1 <= capacity <= 16_384:
            raise ValueError("invalid_ticket_capacity")
        self._capacity = capacity
        self._consumed: dict[bytes, datetime] = {}
        self._lock = asyncio.Lock()

    @property
    def retained_count(self) -> int:
        return len(self._consumed)

    async def consume(self, claims: WatchTicketClaims, *, now: datetime) -> None:
        digest = claims.nonce_hash
        async with self._lock:
            self._prune(now)
            if claims.expires_at <= now:
                raise WatchBridgeError("ticket_expired")
            if digest in self._consumed:
                raise WatchBridgeError("ticket_replayed")
            if len(self._consumed) >= self._capacity:
                raise WatchBridgeError("bridge_capacity_exhausted")
            self._consumed[digest] = claims.expires_at

    def _prune(self, now: datetime) -> None:
        for digest, expires_at in tuple(self._consumed.items()):
            if expires_at <= now:
                self._consumed.pop(digest, None)


class _IngressFence:
    """Per-socket sequence, timestamp, rate, and duration authority."""

    def __init__(self, *, monotonic: Callable[[], float] | None = None) -> None:
        self._monotonic = monotonic or time.monotonic
        self._last_sequence: int | None = None
        self._last_timestamp_us: int | None = None
        self._started = self._now()
        self._arrivals: deque[float] = deque()

    def accept(self, frame: WatchPcmFrame) -> None:
        now = self._now()
        if now - self._started > WATCH_MAX_SESSION_SECONDS:
            raise WatchBridgeError("bridge_duration_exceeded")
        expected = 0 if self._last_sequence is None else self._last_sequence + 1
        if frame.sequence != expected:
            raise WatchBridgeError("audio_sequence_gap")
        if self._last_timestamp_us is not None:
            delta = frame.timestamp_us - self._last_timestamp_us
            if not 1 <= delta <= 100_000:
                raise WatchBridgeError("invalid_audio_timestamp")
        elif frame.timestamp_us > 1_000_000:
            raise WatchBridgeError("invalid_audio_timestamp")
        cutoff = now - WATCH_RATE_WINDOW_SECONDS
        while self._arrivals and self._arrivals[0] <= cutoff:
            self._arrivals.popleft()
        if len(self._arrivals) >= WATCH_MAX_RATE_FRAMES:
            raise WatchBridgeError("audio_rate_exceeded")
        self._arrivals.append(now)
        self._last_sequence = frame.sequence
        self._last_timestamp_us = frame.timestamp_us

    def _now(self) -> float:
        value = self._monotonic()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise WatchBridgeError("invalid_bridge_clock")
        return float(value)


class WatchPcmSession(DirectRtcSession):
    """Direct-RTC owner with an authenticated watch last-mile PCM adapter."""

    _SOURCE_ID = "watch-pcm"

    def __init__(self, binding: SessionBinding, **kwargs: Any) -> None:
        if binding.transport != "watch_pcm_websocket":
            raise ValueError("invalid_watch_transport")
        super().__init__(binding, **kwargs)
        self._bridge: BridgeSocket | None = None
        self._bridge_lock = asyncio.Lock()
        self._watch_pcm = bytearray()
        self._assistant_sequence = 0
        self._assistant_timestamp_us = 0
        self._expected_assistant_last: int | None = None

    async def attach_bridge(
        self,
        socket: BridgeSocket,
        claims: WatchTicketClaims,
    ) -> None:
        self._validate_ticket_claims(claims)
        async with self._bridge_lock:
            if self._bridge is not None:
                raise WatchBridgeError("bridge_already_connected")
            self._bridge = socket
        ready = {
            "type": "bridge_ready",
            "schema_version": "1",
            "session_id": self.binding.session_id,
            "generation": self.binding.generation,
            "media_grant_revision": self.binding.media_grant_revision,
            "worker_identity": self.binding.worker_identity,
            "capture": {
                "encoding": "pcm_s16le",
                "channels": 1,
                "sample_rate_hz": 16_000,
                "frame_duration_ms": 20,
            },
            "playback": {
                "encoding": "pcm_s16le",
                "channels": 1,
                "sample_rate_hz": 24_000,
                "frame_duration_ms": 20,
            },
        }
        await self._send_bridge_json(ready)
        self._enqueue_rtc(_OwnedEvent("watch_attached"))

    async def detach_bridge(self, socket: BridgeSocket, reason: str) -> None:
        del reason
        async with self._bridge_lock:
            if self._bridge is not socket:
                return
            self._bridge = None
            for index in range(len(self._watch_pcm)):
                self._watch_pcm[index] = 0
            self._watch_pcm.clear()
        self._enqueue_rtc(_OwnedEvent("watch_detached"))

    async def feed_microphone_frame(
        self,
        socket: BridgeSocket,
        frame: WatchPcmFrame,
    ) -> None:
        if frame.kind != WATCH_MICROPHONE_KIND:
            raise WatchBridgeError("invalid_audio_kind")
        async with self._bridge_lock:
            if self._bridge is not socket:
                raise WatchBridgeError("stale_watch_bridge")
            self._watch_pcm.extend(frame.payload)
            frame_bytes = AUDIO_FRAME_SAMPLES * 2
            while len(self._watch_pcm) >= frame_bytes:
                chunk = bytes(self._watch_pcm[:frame_bytes])
                del self._watch_pcm[:frame_bytes]
                self._enqueue_rtc(
                    _OwnedEvent(
                        "audio_frame",
                        (
                            self._SOURCE_ID,
                            chunk,
                            AUDIO_STREAM_SAMPLE_RATE,
                            1,
                            AUDIO_FRAME_SAMPLES,
                            self._rtc_events.qsize(),
                        ),
                    ),
                )

    async def interrupt_from_bridge(self) -> None:
        self._enqueue_rtc(_OwnedEvent("watch_interrupt"))

    async def _media_grant_rotated(self, frame: Mapping[str, Any]) -> None:
        if frame.get("media_grant_revision") == self.binding.media_grant_revision:
            await super()._media_grant_rotated(frame)
            return
        async with self._bridge_lock:
            bridge = self._bridge
            self._bridge = None
            for index in range(len(self._watch_pcm)):
                self._watch_pcm[index] = 0
            self._watch_pcm.clear()
        if bridge is not None:
            with suppress(Exception):
                await bridge.close(code=1000, reason="grant_rotated")
        await super()._media_grant_rotated(frame)

    def _validate_ticket_claims(self, claims: WatchTicketClaims) -> None:
        match = _NONCE_IDENTITY.fullmatch(self.binding.client_participant_identity)
        if (
            claims.session_id != self.binding.session_id
            or claims.generation != self.binding.generation
            or claims.media_grant_revision != self.binding.media_grant_revision
            or claims.worker_identity != self.binding.worker_identity
            or match is None
            or not hashlib.sha256(claims.nonce).hexdigest() == match.group(1)
        ):
            raise WatchBridgeError("ticket_scope_mismatch")

    async def _handle_rtc_event(self, event: _OwnedEvent) -> None:
        if event.kind == "watch_attached":
            became_listening = self._update_capture_open()
            if became_listening:
                await self._emit(
                    SessionNotice("media_state", metadata={"state": "listening"})
                )
            return
        if event.kind == "watch_detached":
            self._capture_open = False
            await self._abort_utterance("bridge_disconnected", emit=False)
            self._media_state = "reconnecting"
            await self._emit(
                SessionNotice(
                    "media_state",
                    reason="reconnecting",
                    metadata={"state": "reconnecting"},
                )
            )
            return
        if event.kind == "watch_interrupt":
            await self._stop_speech("user_stop", emit=True)
            return
        await super()._handle_rtc_event(event)

    def _input_available(self) -> bool:
        return self._bridge is not None

    def _input_source_authorized(self, source_id: str) -> bool:
        return source_id == self._SOURCE_ID and self._bridge is not None

    async def _publish_transcript_payload(self, payload: bytes) -> None:
        await self._send_bridge_text(payload)

    async def _synthesis_complete(
        self,
        epoch: int,
        audio: Any | None,
        reason: str | None,
    ) -> None:
        meta = self._speech_meta
        if reason is None and audio is not None and meta is not None:
            samples = getattr(audio, "samples", 0)
            pcm = getattr(audio, "pcm_s16le", b"")
            if isinstance(samples, int) and isinstance(pcm, bytes) and samples > 0:
                rounded = ((samples + OUTPUT_FRAME_SAMPLES - 1) // (
                    OUTPUT_FRAME_SAMPLES
                )) * OUTPUT_FRAME_SAMPLES
                if rounded <= meta.max_duration_samples:
                    pcm = pcm + b"\0" * ((rounded - samples) * 2)
                    samples = rounded
                else:
                    samples = (samples // OUTPUT_FRAME_SAMPLES) * OUTPUT_FRAME_SAMPLES
                    pcm = pcm[: samples * 2]
                if samples < OUTPUT_FRAME_SAMPLES:
                    await super()._synthesis_complete(
                        epoch,
                        None,
                        "invalid_synthesized_audio",
                    )
                    return
                audio = replace(audio, pcm_s16le=pcm, samples=samples)
        await super()._synthesis_complete(epoch, audio, reason)

    async def _publish_announcement_manifest(
        self,
        meta: _SpeechMeta,
        *,
        track_name: str,
        duration_samples: int,
    ) -> None:
        del track_name
        if duration_samples % OUTPUT_FRAME_SAMPLES:
            raise RtcSessionError("watch_sample_range_mismatch")
        frame_count = duration_samples // OUTPUT_FRAME_SAMPLES
        first = self._assistant_sequence
        last = first + frame_count - 1
        manifest: dict[str, Any] = {
            "type": "voice_announcement_media",
            "schema_version": "1",
            "session_id": self.binding.session_id,
            "generation": self.binding.generation,
            "media_grant_revision": self.binding.media_grant_revision,
            "announcement_id": meta.announcement_id,
            "announcement_sequence": meta.announcement_sequence,
            "turn_id": meta.turn_id,
            "kind": meta.kind,
            "quantum_role": meta.quantum_role,
            "quantum_index": meta.quantum_index,
            "transport": "watch_pcm_websocket",
            "worker_identity": self.binding.worker_identity,
            "sample_rate_hz": OUTPUT_SAMPLE_RATE,
            "duration_samples": duration_samples,
            "first_media_sequence": first,
            "last_media_sequence": last,
        }
        if meta.result_reserved_samples_after is not None:
            manifest["result_reserved_samples_after"] = (
                meta.result_reserved_samples_after
            )
        encoded = _json_bytes(manifest, MAX_ANNOUNCEMENT_ENVELOPE_BYTES)
        await self._send_bridge_text(encoded)
        self._expected_assistant_last = last

    async def _capture_output_chunk(
        self,
        epoch: int,
        chunk: bytes,
        source: Any,
    ) -> None:
        if len(chunk) != WATCH_PLAYBACK_BYTES:
            raise RtcSessionError("watch_sample_range_mismatch")
        expected_last = self._expected_assistant_last
        if expected_last is None or self._assistant_sequence > expected_last:
            raise RtcSessionError("watch_manifest_missing")
        frame = WatchPcmFrame(
            kind=WATCH_ASSISTANT_KIND,
            sequence=self._assistant_sequence,
            timestamp_us=self._assistant_timestamp_us,
            payload=chunk,
        )
        await self._send_bridge_binary(frame.encode())
        self._assistant_sequence += 1
        self._assistant_timestamp_us += 20_000
        if self._assistant_sequence - 1 == expected_last:
            self._expected_assistant_last = None
        await super()._capture_output_chunk(epoch, chunk, source)

    async def _send_bridge_json(self, value: Mapping[str, Any]) -> None:
        await self._send_bridge_text(_json_bytes(value, WATCH_MAX_CONTROL_BYTES))

    async def _send_bridge_text(self, payload: bytes) -> None:
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise RtcSessionError("bridge_control_invalid") from None
        async with self._bridge_lock:
            bridge = self._bridge
            if bridge is None:
                raise RtcSessionError("watch_bridge_unavailable")
            try:
                await bridge.send(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RtcSessionError("watch_bridge_send_failed") from None

    async def _send_bridge_binary(self, payload: bytes) -> None:
        async with self._bridge_lock:
            bridge = self._bridge
            if bridge is None:
                raise RtcSessionError("watch_bridge_unavailable")
            try:
                await bridge.send(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RtcSessionError("watch_bridge_send_failed") from None

    async def _teardown(self, *, final_state: str, reason: str) -> None:
        async with self._bridge_lock:
            bridge = self._bridge
            self._bridge = None
            for index in range(len(self._watch_pcm)):
                self._watch_pcm[index] = 0
            self._watch_pcm.clear()
        if bridge is not None:
            with suppress(Exception):
                await bridge.send(
                    json.dumps(
                        {
                            "type": "bridge_ended",
                            "schema_version": "1",
                            "reason": "session_ended",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
            with suppress(Exception):
                await bridge.close(code=1000, reason="session_ended")
        await super()._teardown(final_state=final_state, reason=reason)


class WatchBridgeServer:
    """One bounded WebSocket listener attached to current worker assignments."""

    def __init__(
        self,
        *,
        supervisor: SessionSupervisor,
        secret: bytes,
        worker_identity: str,
        host: str,
        port: int,
        path: str = WATCH_BRIDGE_PATH,
        utcnow: Callable[[], datetime] | None = None,
        replay_store: WatchTicketReplayStore | None = None,
        serve_factory: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        if not isinstance(host, str) or not host or len(host) > 255:
            raise ValueError("invalid_watch_bridge_host")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid_watch_bridge_port")
        if path != WATCH_BRIDGE_PATH:
            raise ValueError("invalid_watch_bridge_path")
        self._supervisor = supervisor
        self._secret = secret
        self._worker_identity = worker_identity
        self._host = host
        self._port = port
        self._path = path
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._replays = replay_store or WatchTicketReplayStore()
        self._serve_factory = serve_factory
        self._server: Any | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        serve_factory = self._serve_factory
        if serve_factory is None:
            from websockets.asyncio.server import serve

            serve_factory = serve
        self._server = await serve_factory(
            self.handle,
            self._host,
            self._port,
            max_size=WATCH_MAX_CONTROL_BYTES,
            max_queue=WATCH_BRIDGE_QUEUE_SIZE,
            compression=None,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=3,
        )

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def handle(self, socket: BridgeSocket) -> None:
        runtime: WatchPcmSession | None = None
        try:
            claims = await self._authenticate(socket)
            runtime = self._supervisor.watch_session(
                session_id=claims.session_id,
                generation=claims.generation,
                media_grant_revision=claims.media_grant_revision,
            )
            await runtime.attach_bridge(socket, claims)
            fence = _IngressFence()
            while True:
                message = await socket.recv()
                if isinstance(message, bytes):
                    frame = WatchPcmFrame.parse_microphone(message)
                    fence.accept(frame)
                    await runtime.feed_microphone_frame(socket, frame)
                    continue
                if not isinstance(message, str) or len(message.encode("utf-8")) > 2_048:
                    raise WatchBridgeError("invalid_control")
                await self._control_message(socket, runtime, message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosedOK:
            pass
        except Exception as exc:
            reason = getattr(exc, "code", "bridge_error")
            if not isinstance(reason, str) or re.fullmatch(r"[a-z0-9_]{1,64}", reason) is None:
                reason = "bridge_error"
            with suppress(Exception):
                await socket.close(code=1008, reason=reason)
        finally:
            if runtime is not None:
                await runtime.detach_bridge(socket, "connection_closed")

    async def _authenticate(self, socket: BridgeSocket) -> WatchTicketClaims:
        request = getattr(socket, "request", None)
        path = getattr(request, "path", "")
        headers = getattr(request, "headers", None)
        if path != self._path or "?" in path:
            raise WatchBridgeError("invalid_bridge_path")
        header = getattr(headers, "get", None)
        if not callable(header):
            raise WatchBridgeError("invalid_bridge_headers")
        try:
            origin = header("Origin")
            authorization = header("Authorization", "")
        except Exception:
            raise WatchBridgeError("invalid_bridge_headers") from None
        if origin not in {None, ""}:
            raise WatchBridgeError("origin_rejected")
        if (
            not isinstance(authorization, str)
            or not authorization.startswith("Bearer ")
            or authorization.count(" ") != 1
        ):
            raise WatchBridgeError("ticket_required")
        try:
            claims = verify_watch_ticket(
                authorization[7:],
                self._secret,
                now=self._now(),
                expected_worker_identity=self._worker_identity,
            )
        except WatchTicketError as exc:
            raise WatchBridgeError(exc.code) from None
        await self._replays.consume(claims, now=self._now())
        return claims

    async def _control_message(
        self,
        socket: BridgeSocket,
        runtime: WatchPcmSession,
        message: str,
    ) -> None:
        try:
            value = json.loads(message)
        except json.JSONDecodeError:
            raise WatchBridgeError("invalid_control") from None
        if not isinstance(value, dict) or value.get("schema_version") != "1":
            raise WatchBridgeError("invalid_control")
        kind = value.get("type")
        if kind == "ping" and set(value) == {"type", "schema_version"}:
            await socket.send('{"schema_version":"1","type":"pong"}')
            return
        if kind == "interrupt" and set(value) == {
            "type",
            "schema_version",
            "reason",
        } and value.get("reason") in {"user_stop", "barge_in"}:
            await runtime.interrupt_from_bridge()
            return
        raise WatchBridgeError("invalid_control")

    def _now(self) -> datetime:
        value = self._utcnow()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise WatchBridgeError("invalid_bridge_clock")
        return value.astimezone(UTC)


def _json_bytes(value: Mapping[str, Any], maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise RtcSessionError("bridge_control_invalid") from None
    if len(encoded) > maximum:
        raise RtcSessionError("bridge_control_too_large")
    return encoded


__all__ = [
    "WATCH_BRIDGE_PATH",
    "WatchBridgeError",
    "WatchBridgeServer",
    "WatchPcmFrame",
    "WatchPcmSession",
    "WatchTicketReplayStore",
]
