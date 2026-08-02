"""Strict, bounded Watch PCM bridge tests for Feature 065."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from voice_agent.session import (
    AUDIO_FRAME_SAMPLES,
    DirectRtcSession,
    OUTPUT_FRAME_SAMPLES,
    RtcSessionError,
    _OwnedEvent,
    _SpeechMeta,
)
from voice_agent.speech_adapters import SynthesizedAudio
from voice_agent.tests.test_session_start_065 import (
    FakeAsr,
    FakeRoom,
    FakeRtcFactory,
    FakeTts,
    FakeVad,
    _binding,
)
from voice_agent.watch_bridge import (
    WATCH_ASSISTANT_KIND,
    WATCH_BRIDGE_PATH,
    WATCH_CAPTURE_BYTES,
    WATCH_HEADER,
    WATCH_MAX_RATE_FRAMES,
    WATCH_MICROPHONE_KIND,
    WATCH_PLAYBACK_BYTES,
    WatchBridgeError,
    WatchBridgeServer,
    WatchPcmFrame,
    WatchPcmSession,
    WatchTicketReplayStore,
    _IngressFence,
    _json_bytes,
)
from voice_agent.watch_ticket import (
    WatchTicketError,
    derive_watch_nonce,
    issue_watch_ticket,
    verify_watch_ticket,
    watch_participant_identity,
)
from websockets.exceptions import ConnectionClosedOK


NOW = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
TICKET_SIGNATURE_DOMAIN = b"astraldeep.voice.watch-bridge.ticket.v1\0"
SECRET = b"watch-ticket-test-secret-that-is-long-enough"
SESSION_ID = "00000000-0000-4000-8000-000000000201"
DEVICE_ID = "00000000-0000-4000-8000-000000000202"
CONNECTION_ID = "00000000-0000-4000-8000-000000000203"
ACTIVATION_ID = "00000000-0000-4000-8000-000000000204"
WORKER_ID = "voice-worker-a"


def _claims():
    nonce = derive_watch_nonce(
        SECRET,
        user_id="owner",
        session_key=ACTIVATION_ID,
        generation=1,
        media_grant_revision=1,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
    )
    ticket = issue_watch_ticket(
        SECRET,
        user_id="owner",
        session_id=SESSION_ID,
        generation=1,
        media_grant_revision=1,
        worker_identity=WORKER_ID,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        nonce=nonce,
    )
    return ticket, verify_watch_ticket(
        ticket,
        SECRET,
        now=NOW,
        expected_worker_identity=WORKER_ID,
    )


def _frame(sequence: int, timestamp_us: int) -> WatchPcmFrame:
    return WatchPcmFrame(
        kind=WATCH_MICROPHONE_KIND,
        sequence=sequence,
        timestamp_us=timestamp_us,
        payload=b"\0" * WATCH_CAPTURE_BYTES,
    )


def _watch_session() -> tuple[WatchPcmSession, object]:
    _, claims = _claims()
    binding = _binding()
    binding.session_id = SESSION_ID
    binding.worker_identity = WORKER_ID
    binding.worker_rtc_grant.worker_identity = WORKER_ID
    binding.transport = "watch_pcm_websocket"
    binding.client_participant_identity = watch_participant_identity(claims.nonce)
    session = WatchPcmSession(
        binding,
        rtc_factory=FakeRtcFactory(FakeRoom()),
        vad=FakeVad(),
        asr=FakeAsr(),
        tts=FakeTts(),
        notice_sink=lambda _notice: None,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
    )
    return session, claims


def _speech_meta(*, reserved: int | None = None) -> _SpeechMeta:
    return _SpeechMeta(
        epoch=1,
        announcement_id="00000000-0000-4000-8000-000000000211",
        kind="result",
        text="Completed the request.",
        max_duration_samples=1_920,
        announcement_sequence=2,
        turn_id="00000000-0000-4000-8000-000000000212",
        quantum_role="single",
        quantum_index=0,
        result_reserved_samples_after=reserved,
    )


def test_advc_microphone_frame_round_trips_with_exact_size() -> None:
    frame = _frame(0, 0)
    encoded = frame.encode()
    assert len(encoded) == WATCH_HEADER.size + WATCH_CAPTURE_BYTES
    parsed = WatchPcmFrame.parse_microphone(encoded)
    assert parsed.sequence == 0
    assert parsed.timestamp_us == 0
    assert parsed.payload == b"\0" * WATCH_CAPTURE_BYTES


def test_advc_assistant_frame_encodes_exact_playback_contract() -> None:
    frame = WatchPcmFrame(
        kind=WATCH_ASSISTANT_KIND,
        sequence=2**64 - 1,
        timestamp_us=2**64 - 1,
        payload=b"\1" * WATCH_PLAYBACK_BYTES,
    )
    encoded = frame.encode()
    magic, version, kind, flags, sequence, timestamp, length = WATCH_HEADER.unpack_from(
        encoded
    )
    assert (magic, version, kind, flags) == (b"ADVC", 1, WATCH_ASSISTANT_KIND, 0)
    assert (sequence, timestamp, length) == (
        2**64 - 1,
        2**64 - 1,
        WATCH_PLAYBACK_BYTES,
    )
    assert encoded[WATCH_HEADER.size :] == b"\1" * WATCH_PLAYBACK_BYTES


@pytest.mark.parametrize(
    ("kind", "sequence", "timestamp_us", "payload", "code"),
    (
        (0, 0, 0, b"\0" * WATCH_CAPTURE_BYTES, "invalid_audio_kind"),
        (
            WATCH_MICROPHONE_KIND,
            True,
            0,
            b"\0" * WATCH_CAPTURE_BYTES,
            "invalid_audio_sequence",
        ),
        (
            WATCH_MICROPHONE_KIND,
            -1,
            0,
            b"\0" * WATCH_CAPTURE_BYTES,
            "invalid_audio_sequence",
        ),
        (
            WATCH_MICROPHONE_KIND,
            0,
            True,
            b"\0" * WATCH_CAPTURE_BYTES,
            "invalid_audio_sequence",
        ),
        (
            WATCH_MICROPHONE_KIND,
            0,
            -1,
            b"\0" * WATCH_CAPTURE_BYTES,
            "invalid_audio_sequence",
        ),
        (
            WATCH_MICROPHONE_KIND,
            0,
            0,
            bytearray(WATCH_CAPTURE_BYTES),
            "invalid_audio_length",
        ),
        (
            WATCH_ASSISTANT_KIND,
            0,
            0,
            b"\0" * WATCH_CAPTURE_BYTES,
            "invalid_audio_length",
        ),
    ),
)
def test_advc_frame_constructor_rejects_invalid_fields(
    kind, sequence, timestamp_us, payload, code: str
) -> None:
    with pytest.raises(WatchBridgeError, match=code):
        WatchPcmFrame(
            kind=kind,
            sequence=sequence,
            timestamp_us=timestamp_us,
            payload=payload,
        )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: b"FAIL" + value[4:],
        lambda value: value[:-1],
        lambda value: value[:6] + b"\x00\x01" + value[8:],
        lambda value: value[:5] + b"\x02" + value[6:],
    ),
)
def test_advc_parser_rejects_header_kind_flags_and_size(mutate) -> None:
    with pytest.raises(WatchBridgeError):
        WatchPcmFrame.parse_microphone(mutate(_frame(0, 0).encode()))


def test_ingress_fence_rejects_sequence_timestamp_and_rate_abuse() -> None:
    clock = [0.0]
    fence = _IngressFence(monotonic=lambda: clock[0])
    fence.accept(_frame(0, 0))
    with pytest.raises(WatchBridgeError, match="audio_sequence_gap"):
        fence.accept(_frame(2, 20_000))

    fence = _IngressFence(monotonic=lambda: clock[0])
    fence.accept(_frame(0, 0))
    with pytest.raises(WatchBridgeError, match="invalid_audio_timestamp"):
        fence.accept(_frame(1, 0))

    fence = _IngressFence(monotonic=lambda: clock[0])
    for sequence in range(WATCH_MAX_RATE_FRAMES):
        fence.accept(_frame(sequence, sequence * 20_000))
    with pytest.raises(WatchBridgeError, match="audio_rate_exceeded"):
        fence.accept(_frame(WATCH_MAX_RATE_FRAMES, WATCH_MAX_RATE_FRAMES * 20_000))


def test_ingress_fence_prunes_rate_window_and_enforces_duration_and_clock() -> None:
    clock = [0.0]
    fence = _IngressFence(monotonic=lambda: clock[0])
    fence.accept(_frame(0, 0))
    clock[0] = 1.0
    fence.accept(_frame(1, 20_000))

    clock[0] = 301.0
    with pytest.raises(WatchBridgeError, match="bridge_duration_exceeded"):
        fence.accept(_frame(2, 40_000))

    with pytest.raises(WatchBridgeError, match="invalid_audio_timestamp"):
        _IngressFence(monotonic=lambda: 0.0).accept(_frame(0, 1_000_001))

    for value in (True, float("nan"), float("inf"), "0"):
        with pytest.raises(WatchBridgeError, match="invalid_bridge_clock"):
            _IngressFence(monotonic=lambda value=value: value)


@pytest.mark.parametrize("capacity", (0, 16_385))
def test_ticket_replay_store_rejects_unbounded_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="invalid_ticket_capacity"):
        WatchTicketReplayStore(capacity=capacity)


@pytest.mark.asyncio
async def test_ticket_nonce_is_consumed_once_and_pruned_after_expiry() -> None:
    _, claims = _claims()
    store = WatchTicketReplayStore(capacity=1)
    await store.consume(claims, now=NOW)
    assert store.retained_count == 1
    with pytest.raises(WatchBridgeError, match="ticket_replayed"):
        await store.consume(claims, now=NOW)
    with pytest.raises(WatchBridgeError, match="ticket_expired"):
        await store.consume(claims, now=claims.expires_at)
    assert store.retained_count == 0


@pytest.mark.asyncio
async def test_ticket_replay_store_fails_closed_at_capacity() -> None:
    _, claims = _claims()
    store = WatchTicketReplayStore(capacity=1)
    await store.consume(claims, now=NOW)
    second = replace(claims, nonce=b"x" * 32)
    with pytest.raises(WatchBridgeError, match="bridge_capacity_exhausted"):
        await store.consume(second, now=NOW)


class _Socket:
    def __init__(self, authorization: str, messages: list[str | bytes]) -> None:
        self.request = SimpleNamespace(
            path="/api/voice/watch-bridge",
            headers={"Authorization": authorization},
        )
        self.messages = iter(messages)
        self.sent: list[str | bytes] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        return next(self.messages)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class _Runtime:
    def __init__(self) -> None:
        self.attached = 0
        self.detached = 0
        self.interrupts = 0
        self.frames: list[WatchPcmFrame] = []

    async def attach_bridge(self, _socket, _claims) -> None:
        self.attached += 1

    async def detach_bridge(self, _socket, _reason: str) -> None:
        self.detached += 1

    async def feed_microphone_frame(self, _socket, frame: WatchPcmFrame) -> None:
        self.frames.append(frame)

    async def interrupt_from_bridge(self) -> None:
        self.interrupts += 1


class _ZeroizationBuffer(bytearray):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.before_clear: bytes | None = None

    def clear(self) -> None:
        self.before_clear = bytes(self)
        super().clear()


@pytest.mark.asyncio
async def test_watch_session_attaches_feeds_interrupts_and_detaches() -> None:
    session, claims = _watch_session()
    socket = _Socket("", [])

    await session.attach_bridge(socket, claims)
    ready = json.loads(socket.sent[0])
    assert ready == {
        "capture": {
            "channels": 1,
            "encoding": "pcm_s16le",
            "frame_duration_ms": 20,
            "sample_rate_hz": 16_000,
        },
        "generation": 1,
        "media_grant_revision": 1,
        "playback": {
            "channels": 1,
            "encoding": "pcm_s16le",
            "frame_duration_ms": 20,
            "sample_rate_hz": 24_000,
        },
        "schema_version": "1",
        "session_id": SESSION_ID,
        "type": "bridge_ready",
        "worker_identity": WORKER_ID,
    }
    assert session._rtc_events.get_nowait().kind == "watch_attached"
    assert session._input_available() is True
    assert session._input_source_authorized("watch-pcm") is True
    assert session._input_source_authorized("microphone") is False
    await session._publish_transcript_payload(b'{"type":"voice_transcript"}')
    assert json.loads(socket.sent[-1]) == {"type": "voice_transcript"}

    with pytest.raises(WatchBridgeError, match="bridge_already_connected"):
        await session.attach_bridge(_Socket("", []), claims)
    with pytest.raises(WatchBridgeError, match="invalid_audio_kind"):
        await session.feed_microphone_frame(
            socket,
            WatchPcmFrame(
                kind=WATCH_ASSISTANT_KIND,
                sequence=0,
                timestamp_us=0,
                payload=b"\0" * WATCH_PLAYBACK_BYTES,
            ),
        )
    with pytest.raises(WatchBridgeError, match="stale_watch_bridge"):
        await session.feed_microphone_frame(_Socket("", []), _frame(0, 0))

    await session.feed_microphone_frame(socket, _frame(0, 0))
    assert session._rtc_events.empty()
    await session.feed_microphone_frame(socket, _frame(1, 20_000))
    audio = session._rtc_events.get_nowait()
    assert audio.kind == "audio_frame"
    source, pcm, sample_rate, channels, samples, buffered = audio.args
    assert (source, sample_rate, channels, samples, buffered) == (
        "watch-pcm",
        16_000,
        1,
        AUDIO_FRAME_SAMPLES,
        0,
    )
    assert pcm == b"\0" * (AUDIO_FRAME_SAMPLES * 2)
    assert session._watch_pcm == bytearray(WATCH_CAPTURE_BYTES * 2 - len(pcm))

    await session.interrupt_from_bridge()
    assert session._rtc_events.get_nowait().kind == "watch_interrupt"
    await session.detach_bridge(_Socket("", []), "stale")
    assert session._bridge is socket
    await session.detach_bridge(socket, "finished")
    assert session._bridge is None
    assert session._watch_pcm == bytearray()
    assert session._rtc_events.get_nowait().kind == "watch_detached"
    assert session._input_available() is False


def test_watch_session_rejects_wrong_transport_and_ticket_scope() -> None:
    binding = _binding()
    with pytest.raises(ValueError, match="invalid_watch_transport"):
        WatchPcmSession(
            binding,
            rtc_factory=FakeRtcFactory(FakeRoom()),
            vad=FakeVad(),
            asr=FakeAsr(),
            tts=FakeTts(),
            notice_sink=lambda _notice: None,
            worker_control_secret=b"c" * 32,
        )

    session, claims = _watch_session()
    mismatches = (
        replace(claims, session_id=DEVICE_ID),
        replace(claims, generation=2),
        replace(claims, media_grant_revision=2),
        replace(claims, worker_identity="voice-worker-b"),
        replace(claims, nonce=b"z" * 32),
    )
    for mismatch in mismatches:
        with pytest.raises(WatchBridgeError, match="ticket_scope_mismatch"):
            session._validate_ticket_claims(mismatch)
    session.binding.client_participant_identity = "client-a"
    with pytest.raises(WatchBridgeError, match="ticket_scope_mismatch"):
        session._validate_ticket_claims(claims)


@pytest.mark.asyncio
async def test_watch_session_rotates_grant_and_handles_watch_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _claims_value = _watch_session()
    socket = _Socket("", [])
    session._bridge = socket
    retained_pcm = _ZeroizationBuffer(b"secret-pcm")
    session._watch_pcm = retained_pcm
    rotations: list[dict[str, object]] = []

    async def rotated(_self, frame):
        rotations.append(frame)

    monkeypatch.setattr(DirectRtcSession, "_media_grant_rotated", rotated)
    current = {"media_grant_revision": 1}
    await session._media_grant_rotated(current)
    assert session._bridge is socket
    assert rotations == [current]

    replacement = {"media_grant_revision": 2}
    await session._media_grant_rotated(replacement)
    assert session._bridge is None
    assert retained_pcm.before_clear == b"\0" * len(b"secret-pcm")
    assert session._watch_pcm == bytearray()
    assert socket.closed == [(1000, "grant_rotated")]
    assert rotations == [current, replacement]

    notices = []
    session._notice_sink = notices.append
    session._bridge = socket
    session._capture_requested = True
    session._context_synced = True
    await session._handle_rtc_event(_OwnedEvent("watch_attached"))
    assert notices[-1].metadata == {"state": "listening"}

    calls: list[tuple[str, object]] = []

    async def abort(reason: str, *, emit: bool) -> None:
        calls.append((reason, emit))

    async def stop(reason: str, *, emit: bool) -> None:
        calls.append((reason, emit))

    async def parent_event(_self, event: _OwnedEvent) -> None:
        calls.append(("parent", event.kind))

    monkeypatch.setattr(session, "_abort_utterance", abort)
    monkeypatch.setattr(session, "_stop_speech", stop)
    monkeypatch.setattr(DirectRtcSession, "_handle_rtc_event", parent_event)
    await session._handle_rtc_event(_OwnedEvent("watch_detached"))
    assert session._media_state == "reconnecting"
    assert notices[-1].reason == "reconnecting"
    await session._handle_rtc_event(_OwnedEvent("watch_interrupt"))
    await session._handle_rtc_event(_OwnedEvent("control"))
    assert calls == [
        ("bridge_disconnected", False),
        ("user_stop", True),
        ("parent", "control"),
    ]


@pytest.mark.asyncio
async def test_watch_session_synthesis_aligns_pcm_to_transport_quantum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _claims_value = _watch_session()
    calls: list[tuple[int, object, str | None]] = []

    async def complete(_self, epoch, audio, reason):
        calls.append((epoch, audio, reason))

    monkeypatch.setattr(DirectRtcSession, "_synthesis_complete", complete)
    session._speech_meta = _speech_meta()

    rounded = SynthesizedAudio(b"\1\0" * 961, 24_000, 1, 2, 961)
    await session._synthesis_complete(1, rounded, None)
    assert calls[-1][1].samples == 1_440
    assert len(calls[-1][1].pcm_s16le) == 2_880

    session._speech_meta.max_duration_samples = 960
    truncated = SynthesizedAudio(b"\2\0" * 1_001, 24_000, 1, 2, 1_001)
    await session._synthesis_complete(2, truncated, None)
    assert calls[-1][1].samples == 960
    assert len(calls[-1][1].pcm_s16le) == 1_920

    session._speech_meta.max_duration_samples = 1
    too_short = SynthesizedAudio(b"\3\0" * 100, 24_000, 1, 2, 100)
    await session._synthesis_complete(3, too_short, None)
    assert calls[-1] == (3, None, "invalid_synthesized_audio")

    invalid_shape = SimpleNamespace(samples="960", pcm_s16le=bytearray(1_920))
    await session._synthesis_complete(4, invalid_shape, None)
    assert calls[-1] == (4, invalid_shape, None)
    await session._synthesis_complete(5, None, "provider_failed")
    assert calls[-1] == (5, None, "provider_failed")


@pytest.mark.asyncio
async def test_watch_session_publishes_manifest_and_ordered_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _claims_value = _watch_session()
    socket = _Socket("", [])
    session._bridge = socket
    meta = _speech_meta(reserved=4_800)

    with pytest.raises(RtcSessionError, match="watch_sample_range_mismatch"):
        await session._publish_announcement_manifest(
            meta,
            track_name="ignored",
            duration_samples=OUTPUT_FRAME_SAMPLES + 1,
        )

    await session._publish_announcement_manifest(
        meta,
        track_name="ignored",
        duration_samples=OUTPUT_FRAME_SAMPLES * 2,
    )
    manifest = json.loads(socket.sent[-1])
    assert manifest["transport"] == "watch_pcm_websocket"
    assert manifest["first_media_sequence"] == 0
    assert manifest["last_media_sequence"] == 1
    assert manifest["result_reserved_samples_after"] == 4_800

    captured: list[tuple[int, bytes, object]] = []

    async def capture(_self, epoch, chunk, source):
        captured.append((epoch, chunk, source))

    monkeypatch.setattr(DirectRtcSession, "_capture_output_chunk", capture)
    with pytest.raises(RtcSessionError, match="watch_sample_range_mismatch"):
        await session._capture_output_chunk(1, b"short", object())

    chunk = b"\4" * WATCH_PLAYBACK_BYTES
    source = object()
    await session._capture_output_chunk(1, chunk, source)
    await session._capture_output_chunk(1, chunk, source)
    assert captured == [(1, chunk, source), (1, chunk, source)]
    binaries = [value for value in socket.sent if isinstance(value, bytes)]
    assert len(binaries) == 2
    first = WATCH_HEADER.unpack_from(binaries[0])
    second = WATCH_HEADER.unpack_from(binaries[1])
    assert (first[2], first[4], first[5]) == (WATCH_ASSISTANT_KIND, 0, 0)
    assert (second[2], second[4], second[5]) == (
        WATCH_ASSISTANT_KIND,
        1,
        20_000,
    )
    assert session._expected_assistant_last is None
    with pytest.raises(RtcSessionError, match="watch_manifest_missing"):
        await session._capture_output_chunk(1, chunk, source)


class _SendFailureSocket(_Socket):
    def __init__(self, error: BaseException) -> None:
        super().__init__("", [])
        self.error = error

    async def send(self, message: str | bytes) -> None:
        del message
        raise self.error


@pytest.mark.asyncio
async def test_watch_session_bridge_send_failures_are_content_free() -> None:
    session, _claims_value = _watch_session()
    with pytest.raises(RtcSessionError, match="bridge_control_invalid"):
        await session._send_bridge_text(b"\xff")
    with pytest.raises(RtcSessionError, match="watch_bridge_unavailable"):
        await session._send_bridge_text(b"{}")
    with pytest.raises(RtcSessionError, match="watch_bridge_unavailable"):
        await session._send_bridge_binary(b"pcm")

    session._bridge = _SendFailureSocket(RuntimeError("private provider detail"))
    with pytest.raises(RtcSessionError) as text_failure:
        await session._send_bridge_text(b"{}")
    with pytest.raises(RtcSessionError) as binary_failure:
        await session._send_bridge_binary(b"pcm")
    for failure in (text_failure.value, binary_failure.value):
        assert failure.reason == "watch_bridge_send_failed"
        assert str(failure) == "watch_bridge_send_failed"
        assert failure.__suppress_context__ is True

    session._bridge = _SendFailureSocket(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await session._send_bridge_text(b"{}")
    with pytest.raises(asyncio.CancelledError):
        await session._send_bridge_binary(b"pcm")


@pytest.mark.asyncio
async def test_watch_session_teardown_clears_pcm_and_closes_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _claims_value = _watch_session()
    socket = _Socket("", [])
    session._bridge = socket
    retained_pcm = _ZeroizationBuffer(b"private pcm")
    session._watch_pcm = retained_pcm
    calls: list[tuple[str, str]] = []

    async def teardown(_self, *, final_state: str, reason: str) -> None:
        calls.append((final_state, reason))

    monkeypatch.setattr(DirectRtcSession, "_teardown", teardown)
    await session._teardown(final_state="closed", reason="test")
    assert json.loads(socket.sent[0]) == {
        "reason": "session_ended",
        "schema_version": "1",
        "type": "bridge_ended",
    }
    assert socket.closed == [(1000, "session_ended")]
    assert retained_pcm.before_clear == b"\0" * len(b"private pcm")
    assert session._watch_pcm == bytearray()
    assert calls == [("closed", "test")]

    session._bridge = _SendFailureSocket(RuntimeError("closed"))
    await session._teardown(final_state="failed", reason="send_failed")
    assert calls[-1] == ("failed", "send_failed")


@pytest.mark.asyncio
async def test_server_authenticates_exact_assignment_and_relays_only_pcm() -> None:
    ticket, _ = _claims()
    runtime = _Runtime()

    class Supervisor:
        def watch_session(self, **fences):
            assert fences == {
                "session_id": SESSION_ID,
                "generation": 1,
                "media_grant_revision": 1,
            }
            return runtime

    socket = _Socket("Bearer " + ticket, [_frame(0, 0).encode()])
    server = WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7890,
        utcnow=lambda: NOW,
    )
    await server.handle(socket)
    assert runtime.attached == 1
    assert runtime.detached == 1
    assert [frame.sequence for frame in runtime.frames] == [0]
    assert socket.closed == [(1008, "bridge_error")]


@pytest.mark.asyncio
async def test_server_rejects_origin_and_replayed_ticket_before_assignment() -> None:
    ticket, _ = _claims()

    class Supervisor:
        def watch_session(self, **_fences):
            raise AssertionError("unauthorized ticket reached assignment lookup")

    server = WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7890,
        utcnow=lambda: NOW,
    )
    first = _Socket("Bearer " + ticket, [])
    first.request.headers["Origin"] = "https://example.invalid"
    await server.handle(first)
    assert first.closed == [(1008, "origin_rejected")]

    await server._authenticate(_Socket("Bearer " + ticket, []))
    with pytest.raises(WatchBridgeError, match="ticket_replayed"):
        await server._authenticate(_Socket("Bearer " + ticket, []))


class _ServerHandle:
    def __init__(self) -> None:
        self.close_calls = 0
        self.wait_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    async def wait_closed(self) -> None:
        self.wait_calls += 1


@pytest.mark.parametrize(
    ("fields", "code"),
    (
        ({"host": ""}, "invalid_watch_bridge_host"),
        ({"host": "x" * 256}, "invalid_watch_bridge_host"),
        ({"port": True}, "invalid_watch_bridge_port"),
        ({"port": 0}, "invalid_watch_bridge_port"),
        ({"port": 65_536}, "invalid_watch_bridge_port"),
        ({"path": "/wrong"}, "invalid_watch_bridge_path"),
    ),
)
def test_server_rejects_invalid_listener_configuration(fields, code: str) -> None:
    kwargs = {
        "supervisor": object(),
        "secret": SECRET,
        "worker_identity": WORKER_ID,
        "host": "127.0.0.1",
        "port": 7_890,
    }
    kwargs.update(fields)
    with pytest.raises(ValueError, match=code):
        WatchBridgeServer(**kwargs)


@pytest.mark.asyncio
async def test_server_start_and_close_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _ServerHandle()
    calls: list[tuple[object, str, int, dict[str, object]]] = []

    async def serve(handler, host, port, **kwargs):
        calls.append((handler, host, port, kwargs))
        return created

    monkeypatch.setattr("websockets.asyncio.server.serve", serve)
    server = WatchBridgeServer(
        supervisor=object(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7_890,
        utcnow=lambda: NOW,
    )
    await server.start()
    await server.start()
    assert len(calls) == 1
    assert calls[0][1:3] == ("127.0.0.1", 7_890)
    assert calls[0][3] == {
        "close_timeout": 3,
        "compression": None,
        "max_queue": 8,
        "max_size": 12 * 1_024,
        "ping_interval": 20,
        "ping_timeout": 10,
    }
    await server.close()
    await server.close()
    assert (created.close_calls, created.wait_calls) == (1, 1)


class _ReceiveFailureSocket(_Socket):
    def __init__(self, authorization: str, error: BaseException) -> None:
        super().__init__(authorization, [])
        self.error = error

    async def recv(self) -> str | bytes:
        raise self.error


@pytest.mark.asyncio
async def test_server_relays_control_and_treats_clean_disconnect_as_success() -> None:
    ticket, _claims_value = _claims()
    runtime = _Runtime()

    class Supervisor:
        def watch_session(self, **_fences):
            return runtime

    server = WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7_890,
        utcnow=lambda: NOW,
    )
    socket = _Socket(
        "Bearer " + ticket,
        [
            '{"schema_version":"1","type":"ping"}',
            '{"reason":"barge_in","schema_version":"1","type":"interrupt"}',
            "x" * 2_049,
        ],
    )
    await server.handle(socket)
    assert socket.sent == ['{"schema_version":"1","type":"pong"}']
    assert runtime.interrupts == 1
    assert runtime.detached == 1
    assert socket.closed == [(1008, "invalid_control")]

    ticket, _claims_value = _claims()
    clean = _ReceiveFailureSocket(
        "Bearer " + ticket,
        ConnectionClosedOK(None, None),
    )
    await WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7_890,
        utcnow=lambda: NOW,
    ).handle(clean)
    assert clean.closed == []


@pytest.mark.asyncio
async def test_server_propagates_cancellation_but_always_detaches() -> None:
    ticket, _claims_value = _claims()
    runtime = _Runtime()

    class Supervisor:
        def watch_session(self, **_fences):
            return runtime

    socket = _ReceiveFailureSocket("Bearer " + ticket, asyncio.CancelledError())
    server = WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7_890,
        utcnow=lambda: NOW,
    )
    with pytest.raises(asyncio.CancelledError):
        await server.handle(socket)
    assert runtime.detached == 1
    assert socket.closed == []


@pytest.mark.asyncio
async def test_server_sanitizes_untrusted_close_reasons() -> None:
    ticket, _claims_value = _claims()

    class PrivateFailure(RuntimeError):
        code = "private provider detail!"

    class Supervisor:
        def watch_session(self, **_fences):
            raise PrivateFailure("must never reach the close frame")

    socket = _Socket("Bearer " + ticket, [])
    await WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7_890,
        utcnow=lambda: NOW,
    ).handle(socket)
    assert socket.closed == [(1008, "bridge_error")]


@pytest.mark.asyncio
async def test_server_authentication_and_control_fail_closed() -> None:
    ticket, _claims_value = _claims()
    server = WatchBridgeServer(
        supervisor=object(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7_890,
        utcnow=lambda: NOW,
    )
    bad_path = _Socket("Bearer " + ticket, [])
    bad_path.request.path = WATCH_BRIDGE_PATH + "?ticket=forbidden"
    with pytest.raises(WatchBridgeError, match="invalid_bridge_path"):
        await server._authenticate(bad_path)

    no_headers = _Socket("Bearer " + ticket, [])
    no_headers.request.headers = None
    with pytest.raises(WatchBridgeError, match="invalid_bridge_headers"):
        await server._authenticate(no_headers)

    class ExplodingHeaders:
        def get(self, *_args):
            raise RuntimeError("must stay private")

    exploding = _Socket("Bearer " + ticket, [])
    exploding.request.headers = ExplodingHeaders()
    with pytest.raises(WatchBridgeError, match="invalid_bridge_headers"):
        await server._authenticate(exploding)

    for authorization in (None, "", "Basic token", "Bearer a b"):
        socket = _Socket("", [])
        socket.request.headers["Authorization"] = authorization
        with pytest.raises(WatchBridgeError, match="ticket_required"):
            await server._authenticate(socket)

    malformed = _Socket("Bearer not-a-ticket", [])
    with pytest.raises(WatchBridgeError, match="invalid_ticket"):
        await server._authenticate(malformed)

    runtime = _Runtime()
    socket = _Socket("", [])
    await server._control_message(
        socket,
        runtime,
        '{"schema_version":"1","type":"ping"}',
    )
    assert socket.sent == ['{"schema_version":"1","type":"pong"}']
    await server._control_message(
        socket,
        runtime,
        '{"reason":"user_stop","schema_version":"1","type":"interrupt"}',
    )
    assert runtime.interrupts == 1
    for message in (
        "not-json",
        "[]",
        "{}",
        '{"extra":true,"schema_version":"1","type":"ping"}',
        '{"reason":"other","schema_version":"1","type":"interrupt"}',
    ):
        with pytest.raises(WatchBridgeError, match="invalid_control"):
            await server._control_message(socket, runtime, message)


def test_server_clock_and_control_json_validation_are_bounded() -> None:
    for value in (None, NOW.replace(tzinfo=None)):
        server = WatchBridgeServer(
            supervisor=object(),
            secret=SECRET,
            worker_identity=WORKER_ID,
            host="127.0.0.1",
            port=7_890,
            utcnow=lambda value=value: value,
        )
        with pytest.raises(WatchBridgeError, match="invalid_bridge_clock"):
            server._now()

    assert _json_bytes({"type": "ping"}, 32) == b'{"type":"ping"}'
    with pytest.raises(RtcSessionError, match="bridge_control_invalid"):
        _json_bytes({"value": float("nan")}, 128)
    with pytest.raises(RtcSessionError, match="bridge_control_invalid"):
        _json_bytes({"value": object()}, 128)
    with pytest.raises(RtcSessionError, match="bridge_control_too_large"):
        _json_bytes({"value": "x" * 64}, 16)


def _signed_ticket_payload(value: object) -> str:
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        SECRET,
        TICKET_SIGNATURE_DOMAIN + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"v1.{encoded}.{encoded_signature}"


def _ticket_payload() -> dict[str, object]:
    ticket, _claims_value = _claims()
    encoded = ticket.split(".")[1]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    return json.loads(raw)


def test_watch_ticket_public_contract_rejects_invalid_inputs() -> None:
    ticket, claims = _claims()
    assert claims.nonce_hash == hashlib.sha256(claims.nonce).digest()
    claims_repr = repr(claims)
    assert "nonce=<redacted>" in claims_repr
    assert claims.nonce.hex() not in claims_repr
    assert base64.b64encode(claims.nonce).decode("ascii") not in claims_repr
    assert base64.urlsafe_b64encode(claims.nonce).decode("ascii") not in claims_repr
    assert watch_participant_identity(claims.nonce).startswith("watch-")

    with pytest.raises(WatchTicketError, match="invalid_ticket_secret"):
        verify_watch_ticket(
            ticket, b"short", now=NOW, expected_worker_identity=WORKER_ID
        )
    with pytest.raises(WatchTicketError, match="invalid_ticket_lifetime"):
        issue_watch_ticket(
            SECRET,
            user_id="owner",
            session_id=SESSION_ID,
            generation=1,
            media_grant_revision=1,
            worker_identity=WORKER_ID,
            device_id=DEVICE_ID,
            connection_generation=CONNECTION_ID,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=6),
            nonce=claims.nonce,
        )
    with pytest.raises(WatchTicketError, match="invalid_issued_at"):
        issue_watch_ticket(
            SECRET,
            user_id="owner",
            session_id=SESSION_ID,
            generation=1,
            media_grant_revision=1,
            worker_identity=WORKER_ID,
            device_id=DEVICE_ID,
            connection_generation=CONNECTION_ID,
            issued_at=NOW.replace(tzinfo=None),
            expires_at=NOW + timedelta(minutes=1),
            nonce=claims.nonce,
        )

    for invalid in (None, "tiny", "v2.a.b", "v1.a.***"):
        with pytest.raises(WatchTicketError, match="invalid_ticket"):
            verify_watch_ticket(
                invalid,
                SECRET,
                now=NOW,
                expected_worker_identity=WORKER_ID,
            )

    version, payload, signature = ticket.split(".")
    with pytest.raises(WatchTicketError, match="invalid_ticket"):
        verify_watch_ticket(
            ".".join(("v2", payload, signature)),
            SECRET,
            now=NOW,
            expected_worker_identity=WORKER_ID,
        )
    for invalid_signature in ("", "A"):
        with pytest.raises(WatchTicketError, match="invalid_ticket"):
            verify_watch_ticket(
                ".".join((version, payload, invalid_signature)),
                SECRET,
                now=NOW,
                expected_worker_identity=WORKER_ID,
            )
    replacement = "A" if signature[0] != "A" else "B"
    tampered = ".".join((version, payload, replacement + signature[1:]))
    with pytest.raises(WatchTicketError, match="invalid_ticket"):
        verify_watch_ticket(
            tampered,
            SECRET,
            now=NOW,
            expected_worker_identity=WORKER_ID,
        )
    with pytest.raises(WatchTicketError, match="wrong_worker"):
        verify_watch_ticket(
            ticket,
            SECRET,
            now=NOW,
            expected_worker_identity="voice-worker-b",
        )
    with pytest.raises(WatchTicketError, match="ticket_expired"):
        verify_watch_ticket(
            ticket,
            SECRET,
            now=NOW + timedelta(minutes=1),
            expected_worker_identity=WORKER_ID,
        )


@pytest.mark.parametrize(
    ("mutate", "code"),
    (
        (lambda value: value.update(v=2), "invalid_ticket"),
        (lambda value: value.pop("sub"), "invalid_ticket"),
        (lambda value: value.update(sub="not-a-digest"), "invalid_ticket"),
        (lambda value: value.update(nonce="***"), "invalid_ticket"),
        (lambda value: value.update(iat=True), "invalid_ticket"),
        (lambda value: value.update(exp=value["iat"]), "ticket_expired"),
        (
            lambda value: value.update(
                exp=value["iat"] + int(timedelta(minutes=6).total_seconds())
            ),
            "invalid_ticket",
        ),
        (lambda value: value.update(iat=10**20), "invalid_ticket"),
        (lambda value: value.update(gen=False), "invalid_ticket"),
        (lambda value: value.update(worker="bad worker"), "invalid_ticket"),
    ),
)
def test_watch_ticket_rejects_signed_malformed_claims(mutate, code: str) -> None:
    payload = _ticket_payload()
    mutate(payload)
    with pytest.raises(WatchTicketError, match=code):
        verify_watch_ticket(
            _signed_ticket_payload(payload),
            SECRET,
            now=NOW,
            expected_worker_identity=WORKER_ID,
        )


def test_watch_ticket_rejects_signed_invalid_and_oversized_json() -> None:
    for value in (b"not-json", b"{" + b"x" * 2_048 + b"}"):
        with pytest.raises(WatchTicketError, match="invalid_ticket"):
            verify_watch_ticket(
                _signed_ticket_payload(value),
                SECRET,
                now=NOW,
                expected_worker_identity=WORKER_ID,
            )


@pytest.mark.parametrize(
    ("kwargs", "code"),
    (
        ({"user_id": ""}, "invalid_user_id"),
        ({"session_key": "not-a-uuid"}, "invalid_session_key"),
        ({"generation": True}, "invalid_generation"),
        ({"media_grant_revision": 0}, "invalid_media_grant_revision"),
        ({"device_id": "not-a-uuid"}, "invalid_device_id"),
        ({"connection_generation": "not-a-uuid"}, "invalid_connection_generation"),
    ),
)
def test_watch_nonce_rejects_invalid_scope(kwargs, code: str) -> None:
    values = {
        "user_id": "owner",
        "session_key": ACTIVATION_ID,
        "generation": 1,
        "media_grant_revision": 1,
        "device_id": DEVICE_ID,
        "connection_generation": CONNECTION_ID,
    }
    values.update(kwargs)
    with pytest.raises(WatchTicketError, match=code):
        derive_watch_nonce(SECRET, **values)

    with pytest.raises(WatchTicketError, match="invalid_ticket_nonce"):
        watch_participant_identity(b"short")
