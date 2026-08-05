"""Direct-RTC session guards for Feature 065's first conversational turn."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from voice_agent.voice_transcript import (
    TranscriptProofBinding,
    verify_transcript_proof,
)
from voice_agent.session import (
    ASR_TAIL_SILENCE_FRAMES,
    AUDIO_FRAME_SAMPLES,
    AUDIO_STREAM_CAPACITY,
    AUDIO_STREAM_FRAME_MS,
    AUDIO_STREAM_SAMPLE_RATE,
    OUTPUT_QUEUE_MS,
    VAD_END_SILENCE_FRAMES,
    VOICE_TRANSCRIPT_TOPIC,
    DirectRtcSession,
    LiveKitRtcFactory,
    ProtocolViolation,
    RtcSessionError,
    SessionBinding,
    SessionNotice,
    SileroVad,
    WorkerRtcGrant,
    validate_announcement_binding,
)
from voice_agent.speech_adapters import SynthesizedAudio, Transcript

NOW = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)


def _uuid(value: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | value))


def _binding() -> SessionBinding:
    return SessionBinding(
        session_id=_uuid(1),
        generation=1,
        assignment_id=_uuid(2),
        room_name="voice-room-a",
        worker_identity="voice-worker-a",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-a",
        grant_expires_at=NOW + timedelta(minutes=4),
        worker_rtc_grant=WorkerRtcGrant(
            revision=1,
            livekit_url="wss://livekit.internal",
            join_token="memory-only-worker-token-" + "x" * 32,
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=4),
            room_name="voice-room-a",
            worker_identity="voice-worker-a",
        ),
        visible_chat_id=_uuid(3),
        chat_context_revision=1,
    )


@dataclass(slots=True)
class FakeRoomOptions:
    auto_subscribe: bool
    connect_timeout: float


@dataclass(slots=True)
class FakeTrack:
    name: str = "microphone"


class FakePublication:
    def __init__(
        self,
        sid: str,
        *,
        kind: str = "audio",
        source: str = "microphone",
        track: FakeTrack | None = None,
    ) -> None:
        self.sid = sid
        self.kind = kind
        self.source = source
        self.track = track
        self.subscription_calls: list[bool] = []

    def set_subscribed(self, subscribed: bool) -> None:
        self.subscription_calls.append(subscribed)


class FakeParticipant:
    def __init__(
        self, identity: str, publications: list[FakePublication] | None = None
    ) -> None:
        self.identity = identity
        self.track_publications = {
            publication.sid: publication for publication in publications or []
        }


class FakeLocalPublication:
    def __init__(self, sid: str) -> None:
        self.sid = sid


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[tuple[FakeTrack, Any]] = []
        self.unpublished: list[str] = []
        self.publication = FakeLocalPublication("TR_output_1")
        self.publish_error: BaseException | None = None
        self.published_data: list[dict[str, Any]] = []

    async def publish_track(
        self, track: FakeTrack, options: Any
    ) -> FakeLocalPublication:
        self.published.append((track, options))
        if self.publish_error is not None:
            raise self.publish_error
        return self.publication

    async def unpublish_track(self, sid: str) -> None:
        self.unpublished.append(sid)

    async def publish_data(
        self,
        payload: bytes,
        *,
        reliable: bool,
        destination_identities: list[str],
        topic: str,
    ) -> None:
        self.published_data.append(
            {
                "payload": payload,
                "reliable": reliable,
                "destination_identities": destination_identities,
                "topic": topic,
            }
        )


class FakeRoom:
    def __init__(
        self,
        participants: list[FakeParticipant] | None = None,
        *,
        connect_error: BaseException | None = None,
    ) -> None:
        self.remote_participants = {
            participant.identity: participant for participant in participants or []
        }
        self.local_participant = FakeLocalParticipant()
        self.callbacks: dict[str, list[Any]] = {}
        self.connect_calls: list[tuple[str, str, FakeRoomOptions]] = []
        self.connect_error = connect_error
        self.disconnect_calls = 0

    def on(self, event: str, callback: Any) -> Any:
        self.callbacks.setdefault(event, []).append(callback)
        return callback

    async def connect(self, url: str, token: str, options: FakeRoomOptions) -> None:
        self.connect_calls.append((url, token, options))
        if self.connect_error is not None:
            raise self.connect_error

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    def emit(self, event: str, *args: Any) -> None:
        for callback in self.callbacks.get(event, []):
            result = callback(*args)
            assert not inspect.isawaitable(result), "RTC callback must stay synchronous"


@dataclass(slots=True)
class FakeFrame:
    pcm: bytes
    sample_rate: int = AUDIO_STREAM_SAMPLE_RATE
    channels: int = 1
    samples: int = AUDIO_FRAME_SAMPLES
    buffered_frames: int = 0


class FakeAudioStream:
    def __init__(self) -> None:
        self.events: asyncio.Queue[FakeFrame | None] = asyncio.Queue()
        self.closed = 0

    def __aiter__(self) -> FakeAudioStream:
        return self

    async def __anext__(self) -> FakeFrame:
        item = await self.events.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def feed(self, pcm: bytes | None = None, *, buffered_frames: int = 0) -> None:
        self.events.put_nowait(
            FakeFrame(
                pcm=pcm if pcm is not None else b"\0\0" * AUDIO_FRAME_SAMPLES,
                buffered_frames=buffered_frames,
            )
        )

    async def aclose(self) -> None:
        self.closed += 1
        self.events.put_nowait(None)


class FakeAudioSource:
    def __init__(
        self,
        *,
        block_capture: bool = False,
        capture_error: BaseException | None = None,
    ) -> None:
        self.frames: list[bytes] = []
        self.clear_calls = 0
        self.close_calls = 0
        self.playout_calls = 0
        self.block_capture = block_capture
        self.capture_error = capture_error
        self.capture_entered = asyncio.Event()
        self.capture_release = asyncio.Event()

    async def capture_frame(self, frame: bytes) -> None:
        self.capture_entered.set()
        if self.capture_error is not None:
            raise self.capture_error
        if self.block_capture:
            await self.capture_release.wait()
        self.frames.append(frame)

    def clear_queue(self) -> None:
        self.clear_calls += 1
        self.frames.clear()

    async def wait_for_playout(self) -> None:
        self.playout_calls += 1

    async def aclose(self) -> None:
        self.close_calls += 1
        self.capture_release.set()


class FakeRtcFactory:
    def __init__(
        self,
        room: FakeRoom,
        *,
        block_output: bool = False,
        capture_error: BaseException | None = None,
    ) -> None:
        self.room = room
        self.streams: list[FakeAudioStream] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.sources: list[FakeAudioSource] = []
        self.source_calls: list[dict[str, int]] = []
        self.block_output = block_output
        self.capture_error = capture_error

    def create_room(self) -> FakeRoom:
        return self.room

    def room_options(
        self, *, auto_subscribe: bool, connect_timeout: float
    ) -> FakeRoomOptions:
        return FakeRoomOptions(auto_subscribe, connect_timeout)

    def is_audio_publication(self, publication: FakePublication) -> bool:
        return publication.kind == "audio"

    def is_microphone_publication(self, publication: FakePublication) -> bool:
        return publication.source == "microphone"

    def create_audio_stream(
        self,
        track: FakeTrack,
        *,
        capacity: int,
        sample_rate: int,
        num_channels: int,
        frame_size_ms: int,
    ) -> FakeAudioStream:
        stream = FakeAudioStream()
        self.streams.append(stream)
        self.stream_calls.append(
            {
                "track": track,
                "capacity": capacity,
                "sample_rate": sample_rate,
                "num_channels": num_channels,
                "frame_size_ms": frame_size_ms,
            }
        )
        return stream

    def decode_audio_event(self, event: FakeFrame) -> tuple[bytes, int, int, int]:
        return event.pcm, event.sample_rate, event.channels, event.samples

    def stream_buffer_depth(self, event: FakeFrame, stream: FakeAudioStream) -> int:
        del stream
        return event.buffered_frames

    def create_audio_source(
        self,
        *,
        sample_rate: int,
        num_channels: int,
        queue_size_ms: int,
    ) -> FakeAudioSource:
        self.source_calls.append(
            {
                "sample_rate": sample_rate,
                "num_channels": num_channels,
                "queue_size_ms": queue_size_ms,
            }
        )
        source = FakeAudioSource(
            block_capture=self.block_output,
            capture_error=self.capture_error,
        )
        self.sources.append(source)
        return source

    def create_local_audio_track(self, name: str, source: FakeAudioSource) -> FakeTrack:
        del source
        return FakeTrack(name=name)

    def track_publish_options(self) -> dict[str, str]:
        return {"source": "unknown"}

    def create_output_frame(
        self, pcm_s16le: bytes, *, sample_rate: int, num_channels: int
    ) -> bytes:
        assert sample_rate == 24_000
        assert num_channels == 1
        return pcm_s16le


class FakeVad:
    def __init__(self, probabilities: list[float] | None = None) -> None:
        self.probabilities = deque(probabilities or [])
        self.calls: list[bytes] = []
        self.reset_calls = 0

    def probability(self, pcm_s16le: bytes) -> float:
        self.calls.append(pcm_s16le)
        return self.probabilities.popleft() if self.probabilities else 0.0

    def reset(self) -> None:
        self.reset_calls += 1


class FakeAsr:
    def __init__(self, transcripts: list[Transcript] | None = None) -> None:
        self.transcripts = deque(transcripts or [Transcript("hello", "en")])
        self.calls: list[bytes] = []

    async def transcribe_pcm16(self, pcm_s16le: bytes) -> Transcript:
        self.calls.append(pcm_s16le)
        return self.transcripts.popleft()


class FakeTts:
    def __init__(self, *, samples: int = 960) -> None:
        self.samples = samples
        self.calls: list[tuple[str, int]] = []

    async def synthesize(
        self, text: str, *, max_duration_samples: int
    ) -> SynthesizedAudio:
        self.calls.append((text, max_duration_samples))
        return SynthesizedAudio(
            pcm_s16le=b"\1\0" * self.samples,
            sample_rate=24_000,
            channels=1,
            sample_width_bytes=2,
            samples=self.samples,
        )


def _set_capture(enabled: bool) -> dict[str, Any]:
    return {
        "type": "set_capture",
        "session_id": _uuid(1),
        "generation": 1,
        "media_grant_revision": 1,
        "enabled": enabled,
    }


def _speak(
    *,
    kind: str = "greeting",
    turn_id: str | None = None,
    announcement_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "speak",
        "session_id": _uuid(1),
        "generation": 1,
        "media_grant_revision": 1,
        "transport": "livekit",
        "announcement_id": announcement_id or _uuid(20),
        "announcement_sequence": 1,
        "turn_id": turn_id,
        "kind": kind,
        "quantum_role": "single",
        "quantum_index": 0,
        "max_duration_samples": 96_000,
        "text": "Hello. What can I help with?",
        "sensitive_authorized": False,
        "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
    }


def _transcript_envelope(
    *,
    client_turn_id: str,
    text: str = "Book the follow-up",
    proof_expires_at: datetime | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "type": "voice_transcript",
        "schema_version": "1",
        "session_id": _uuid(1),
        "generation": 1,
        "turn_id": _uuid(31),
        "client_turn_id": client_turn_id,
        "submission_id": _uuid(32),
        "request_generation": _uuid(33),
        "chat_id": _uuid(3),
        "chat_context_revision": 1,
        "media_grant_revision": 1,
        "sequence": 1,
        "final": True,
        "text": text,
        "detected_language": "en",
        "text_digest_sha256": digest,
        "transcript_proof": "a" * 64,
        "proof_expires_at": (proof_expires_at or NOW + timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "source_participant_identity": "voice-worker-a",
    }


def _turn_bound_frame(client_turn_id: str) -> dict[str, Any]:
    return {
        "type": "turn_bound",
        "session_id": _uuid(1),
        "generation": 1,
        "client_turn_id": client_turn_id,
        "turn_id": _uuid(31),
        "chat_id": _uuid(3),
        "chat_context_revision": 1,
        "media_grant_revision": 1,
        "submission_id": _uuid(32),
        "request_generation": _uuid(33),
    }


def _accepted_frame(client_turn_id: str) -> dict[str, Any]:
    return {
        "type": "transcript_accepted",
        "session_id": _uuid(1),
        "generation": 1,
        "turn_id": _uuid(31),
        "client_turn_id": client_turn_id,
        "submission_id": _uuid(32),
        "request_generation": _uuid(33),
        "chat_id": _uuid(3),
        "media_grant_revision": 1,
        "accepted_message_id": 17,
    }


def _rejected_frame(client_turn_id: str) -> dict[str, Any]:
    frame = _accepted_frame(client_turn_id)
    frame.pop("accepted_message_id")
    frame.update(
        type="transcript_rejected",
        reason="permission_denied",
        retry_policy="none",
    )
    return frame


async def _wait_for(predicate: Any, *, timeout: float = 1.0) -> None:
    async def wait_loop() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_loop(), timeout)


def _session(
    factory: FakeRtcFactory,
    *,
    vad: FakeVad | None = None,
    asr: FakeAsr | None = None,
    tts: FakeTts | None = None,
    notices: list[SessionNotice] | None = None,
) -> DirectRtcSession:
    destination = notices if notices is not None else []
    return DirectRtcSession(
        _binding(),
        rtc_factory=factory,
        vad=vad or FakeVad(),
        asr=asr or FakeAsr(),
        tts=tts or FakeTts(),
        notice_sink=destination.append,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
    )


def _set_recognition_binding(session: DirectRtcSession, client_turn_id: str) -> None:
    session._client_turn_id = client_turn_id
    recognition = SimpleNamespace(
        client_turn_id=client_turn_id,
        media_grant_revision=1,
        visible_chat_id=_uuid(3),
        chat_context_revision=1,
        turn_id=None,
        submission_id=None,
        request_generation=None,
    )
    session._recognition_binding = recognition
    session._recognition_bindings[client_turn_id] = recognition


def test_pinned_livekit_1_1_14_signatures_and_drop_oldest_are_guarded() -> None:
    rtc = pytest.importorskip("livekit.rtc")
    import importlib.metadata

    assert importlib.metadata.version("livekit") == "1.1.14"
    options = inspect.signature(rtc.RoomOptions)
    assert options.parameters["auto_subscribe"].default is True
    assert options.parameters["connect_timeout"].default is None
    assert inspect.signature(rtc.Room.connect).return_annotation == "None"
    connect_source = inspect.getsource(rtc.Room.connect)
    assert 'emit("connected"' not in connect_source

    stream = inspect.signature(rtc.AudioStream)
    assert stream.parameters["capacity"].default == 0
    assert stream.parameters["sample_rate"].default == 48_000
    assert stream.parameters["num_channels"].default == 1
    assert stream.parameters["frame_size_ms"].default is None
    queue_source = inspect.getsource(
        __import__("livekit.rtc.audio_stream", fromlist=["RingQueue"]).RingQueue.put
    )
    assert "popleft()" in queue_source
    assert (
        inspect.signature(rtc.AudioSource).parameters["queue_size_ms"].default == 1000
    )
    assert not hasattr(rtc.Room, "reconnect")


@pytest.mark.asyncio
async def test_connect_return_reconciles_only_expected_existing_microphone() -> None:
    expected_mic = FakePublication("TR_mic", track=FakeTrack())
    expected_screen = FakePublication("TR_screen", source="screen_share_audio")
    expected_video = FakePublication("TR_video", kind="video", source="camera")
    intruder_mic = FakePublication("TR_intruder", track=FakeTrack())
    room = FakeRoom(
        [
            FakeParticipant(
                "client-a", [expected_mic, expected_screen, expected_video]
            ),
            FakeParticipant("client-b", [intruder_mic]),
        ]
    )
    factory = FakeRtcFactory(room)
    session = _session(factory)
    task = asyncio.create_task(session.run())
    await session.wait_started()

    assert len(room.connect_calls) == 1
    url, token, options = room.connect_calls[0]
    assert url == "wss://livekit.internal"
    assert token.startswith("memory-only-worker-token-")
    assert options == FakeRoomOptions(auto_subscribe=False, connect_timeout=8.0)
    assert expected_mic.subscription_calls == [True]
    assert expected_screen.subscription_calls == []
    assert expected_video.subscription_calls == []
    assert intruder_mic.subscription_calls == []
    assert factory.stream_calls == [
        {
            "track": expected_mic.track,
            "capacity": AUDIO_STREAM_CAPACITY,
            "sample_rate": AUDIO_STREAM_SAMPLE_RATE,
            "num_channels": 1,
            "frame_size_ms": AUDIO_STREAM_FRAME_MS,
        }
    ]
    assert session.binding.worker_rtc_grant.join_token == ""
    assert session.context_synced is True
    assert session.media_state == "ready"

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_callbacks_are_sync_enqueue_only_before_owner_reconciles() -> None:
    participant = FakeParticipant("client-a")
    room = FakeRoom([participant])
    factory = FakeRtcFactory(room)
    notices: list[SessionNotice] = []
    session = _session(factory, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: any(item.kind == "capture_unavailable" for item in notices))

    publication = FakePublication("TR_later", track=FakeTrack())
    participant.track_publications[publication.sid] = publication
    room.emit("track_published", publication, participant)
    assert publication.subscription_calls == []
    await _wait_for(lambda: publication.subscription_calls == [True])
    await _wait_for(
        lambda: any(
            item.kind == "media_state" and item.metadata.get("state") == "listening"
            for item in notices
        )
    )

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_capture_requires_explicit_gate_and_deterministic_silero_endpoint() -> (
    None
):
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    probabilities = [0.9] * 8 + [0.1] * VAD_END_SILENCE_FRAMES
    vad = FakeVad(probabilities)
    asr = FakeAsr([Transcript("Book the follow-up", "en")])
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    stream = factory.streams[0]

    stream.feed()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert vad.calls == []
    assert asr.calls == []

    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    for _ in probabilities:
        stream.feed()
    await _wait_for(lambda: len(asr.calls) == 1)
    await _wait_for(lambda: session.retained_final_count == 1)
    recognition = next(item for item in notices if item.kind == "recognition_started")
    assert recognition.metadata == {
        "client_turn_id": recognition.metadata["client_turn_id"],
        "media_grant_revision": 1,
        "visible_chat_id": _uuid(3),
        "chat_context_revision": 1,
    }
    assert room.local_participant.published_data == []
    session.deliver(_turn_bound_frame(recognition.metadata["client_turn_id"]))
    await _wait_for(lambda: len(room.local_participant.published_data) == 1)
    envelope = json.loads(room.local_participant.published_data[0]["payload"])
    assert envelope["text"] == "Book the follow-up"
    assert envelope["detected_language"] == "en"
    assert envelope["client_turn_id"] == recognition.metadata["client_turn_id"]
    assert len(envelope["transcript_proof"]) == 64
    assert not any(item.kind == "final_transcript" for item in notices)
    assert any(
        item.kind == "media_state" and item.metadata.get("state") == "speech_detected"
        for item in notices
    )
    assert any(
        item.kind == "media_state" and item.metadata.get("state") == "transcribing"
        for item in notices
    )
    # Feature 066: the trailing endpoint-silence run is trimmed to a
    # 128-ms ASR-context tail before the batch POST.
    assert (
        len(asr.calls[0]) == (8 + ASR_TAIL_SILENCE_FRAMES) * AUDIO_FRAME_SAMPLES * 2
    )
    assert vad.reset_calls >= 2
    assert session.retained_audio_bytes == 0
    session.deliver(_accepted_frame(recognition.metadata["client_turn_id"]))
    await _wait_for(lambda: session.retained_final_count == 0)
    assert session.retained_final_bytes == 0

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_vad_start_accepts_natural_voice_probability_burst() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    # Exact Silero v6 probabilities observed around the sustained portion of a
    # non-sensitive, macOS-generated natural-voice phrase.  The old eight-frame
    # gate never started this utterance even though the model was confidently
    # positive for 128 ms.
    speech = [0.395, 0.562, 0.830, 0.880, 0.841, 0.723]
    probabilities = speech + [0.1] * VAD_END_SILENCE_FRAMES
    vad = FakeVad(probabilities)
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    for _ in probabilities:
        factory.streams[0].feed()
    await _wait_for(lambda: len(asr.calls) == 1)

    assert any(item.kind == "recognition_started" for item in notices)
    # Feature 066: endpoint silence past the 128-ms tail never reaches ASR.
    assert (
        len(asr.calls[0])
        == (len(speech) + ASR_TAIL_SILENCE_FRAMES) * AUDIO_FRAME_SAMPLES * 2
    )

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_vad_start_survives_livekit_opus_posterior_smoothing() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    # Exact Silero v6 probabilities measured after a real LiveKit 1.1.14
    # publisher/subscriber Opus round trip of a non-sensitive natural phrase.
    # Requiring four >=0.5 frames rejected this ordinary speech; the bounded
    # release-threshold pre-roll retains enough evidence without accepting a
    # lone high-confidence noise spike.
    # 0.313 already sits below the release threshold, so the trailing
    # endpoint run starts there and the retained speech evidence is four
    # frames plus the feature-066 ASR-context tail.
    speech = [0.429, 0.571, 0.580, 0.444]
    probabilities = speech + [0.313] + [0.1] * (VAD_END_SILENCE_FRAMES - 1)
    vad = FakeVad(probabilities)
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    for _ in probabilities:
        factory.streams[0].feed()
    await _wait_for(lambda: len(asr.calls) == 1)

    assert any(item.kind == "recognition_started" for item in notices)
    # Feature 066: endpoint silence past the 128-ms tail never reaches ASR.
    assert (
        len(asr.calls[0])
        == (len(speech) + ASR_TAIL_SILENCE_FRAMES) * AUDIO_FRAME_SAMPLES * 2
    )

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_vad_hysteresis_bridges_dips_and_delays_endpoint() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    start = [0.8, 0.4, 0.8, 0.8, 0.8]
    ambiguous_speech = [0.4] * 24
    ending_silence = [0.1] * VAD_END_SILENCE_FRAMES
    vad = FakeVad(start + ambiguous_speech + ending_silence)
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    for _ in start + ambiguous_speech:
        factory.streams[0].feed()
    await _wait_for(lambda: len(vad.calls) == len(start + ambiguous_speech))
    assert any(item.kind == "recognition_started" for item in notices)
    assert asr.calls == []

    for _ in ending_silence:
        factory.streams[0].feed()
    await _wait_for(lambda: len(asr.calls) == 1)
    # Feature 066: the ambiguous bridged frames are speech evidence and stay;
    # only the trailing endpoint run is trimmed to the 128-ms tail.
    assert len(asr.calls[0]) == (
        (len(start + ambiguous_speech) + ASR_TAIL_SILENCE_FRAMES)
        * AUDIO_FRAME_SAMPLES
        * 2
    )

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_vad_endpoint_bridges_natural_clause_pause() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    clause = [0.8] * 4
    natural_pause = [0.1] * (VAD_END_SILENCE_FRAMES - 6)
    continuation = [0.8] * 5
    ending = [0.1] * VAD_END_SILENCE_FRAMES
    probabilities = clause + natural_pause + continuation + ending
    vad = FakeVad(probabilities)
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    for _ in clause + natural_pause:
        factory.streams[0].feed()
    await _wait_for(lambda: len(vad.calls) == len(clause + natural_pause))
    assert asr.calls == []

    for _ in continuation + ending:
        factory.streams[0].feed()
    await _wait_for(lambda: len(asr.calls) == 1)
    # Feature 066: the bridged clause pause is internal and survives whole;
    # only the trailing endpoint run is trimmed to the 128-ms tail.
    assert len(asr.calls[0]) == (
        (len(clause + natural_pause + continuation) + ASR_TAIL_SILENCE_FRAMES)
        * AUDIO_FRAME_SAMPLES
        * 2
    )

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_vad_candidate_is_bounded_and_rejects_brief_noise() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    probabilities = [0.9] + [0.4] * 15 + [0.1]
    vad = FakeVad(probabilities)
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    for _ in probabilities:
        factory.streams[0].feed()
    await _wait_for(lambda: len(vad.calls) == len(probabilities))

    assert asr.calls == []
    assert not any(item.kind == "recognition_started" for item in notices)
    assert session.retained_audio_bytes == 0

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_exact_signed_final_replays_until_matching_rejection_then_scrubs() -> (
    None
):
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    probabilities = [0.9] * 8 + [0.1] * VAD_END_SILENCE_FRAMES
    vad = FakeVad(probabilities)
    asr = FakeAsr([Transcript("  Cafe\u0301\r\nstatus  ", "en")])
    tts = FakeTts()
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, tts=tts, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    for _ in probabilities:
        factory.streams[0].feed()
    await _wait_for(lambda: session.retained_final_count == 1)
    recognition = next(
        notice for notice in notices if notice.kind == "recognition_started"
    )
    client_turn_id = recognition.metadata["client_turn_id"]
    assert room.local_participant.published_data == []

    session.deliver(_turn_bound_frame(client_turn_id))
    await _wait_for(lambda: len(room.local_participant.published_data) == 1)
    first_payload = room.local_participant.published_data[0]["payload"]
    envelope = json.loads(first_payload)
    assert envelope["text"] == "Caf\u00e9\nstatus"
    proof_binding = TranscriptProofBinding(
        session_id=_uuid(1),
        generation=1,
        media_grant_revision=1,
        assignment_id=_uuid(2),
        worker_identity="voice-worker-a",
        turn_id=_uuid(31),
        client_turn_id=client_turn_id,
        submission_id=_uuid(32),
        request_generation=_uuid(33),
        chat_id=_uuid(3),
        chat_context_revision=1,
        detected_language="en",
    )
    assert (
        verify_transcript_proof(
            b"c" * 32,
            proof_binding,
            envelope["text"],
            text_digest_sha256=envelope["text_digest_sha256"],
            transcript_proof=envelope["transcript_proof"],
            proof_expires_at=envelope["proof_expires_at"],
            now=NOW + timedelta(seconds=1),
        )
        == "Caf\u00e9\nstatus"
    )
    assert all(notice.text is None for notice in notices)

    room.emit("reconnected")
    await _wait_for(lambda: len(room.local_participant.published_data) == 2)
    assert room.local_participant.published_data[1]["payload"] == first_payload

    session.deliver(_rejected_frame(client_turn_id))
    await _wait_for(lambda: session.retained_final_count == 0)
    assert session.retained_final_bytes == 0
    guidance = _speak(
        kind="waiting",
        turn_id=_uuid(31),
        announcement_id=_uuid(21),
    )
    guidance.update(
        phrase_key="llm_setup_needed",
        text="Please set up your AI provider in Settings so I can continue.",
    )
    session.deliver(guidance)
    await _wait_for(lambda: len(tts.calls) == 1)
    assert tts.calls == [
        (
            "Please set up your AI provider in Settings so I can continue.",
            96_000,
        )
    ]
    assert "Caf\u00e9" not in tts.calls[0][0]
    assert session.retained_final_count == 0
    room.emit("reconnected")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Count only transcript publications: the guidance announcement manifest
    # shares this record and lands whenever synthesis completes.
    assert (
        sum(
            record["topic"] == VOICE_TRANSCRIPT_TOPIC
            for record in room.local_participant.published_data
        )
        == 2
    )

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_finite_stream_full_depth_aborts_utterance_without_asr() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    vad = FakeVad([0.9] * 100)
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    factory.streams[0].feed(buffered_frames=AUDIO_STREAM_CAPACITY)
    await _wait_for(
        lambda: any(item.reason == "audio_stream_overrun" for item in notices)
    )
    assert asr.calls == []
    assert session.retained_audio_bytes == 0
    assert vad.reset_calls >= 2

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_no_speech_and_empty_final_never_emit_user_text() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    speech = [0.9] * 8 + [0.1] * VAD_END_SILENCE_FRAMES
    vad = FakeVad([0.1] * 10 + speech)
    asr = FakeAsr([Transcript("   ", "en")])
    notices: list[SessionNotice] = []
    session = _session(factory, vad=vad, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    for _ in range(10):
        factory.streams[0].feed()
    await asyncio.sleep(0)
    assert asr.calls == []
    for _ in speech:
        factory.streams[0].feed()
    await _wait_for(lambda: len(asr.calls) == 1)
    await asyncio.sleep(0)
    assert not any(item.kind == "final_transcript" for item in notices)

    await session.close("test")
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        FakeFrame(b"\0\0" * AUDIO_FRAME_SAMPLES, sample_rate=48_000),
        FakeFrame(b"\0\0" * AUDIO_FRAME_SAMPLES, channels=2),
        FakeFrame(b"\0\0" * 10, samples=10),
    ],
)
async def test_invalid_input_profile_aborts_without_asr(frame: FakeFrame) -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    factory = FakeRtcFactory(FakeRoom([FakeParticipant("client-a", [publication])]))
    asr = FakeAsr()
    notices: list[SessionNotice] = []
    session = _session(factory, asr=asr, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    factory.streams[0].events.put_nowait(frame)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert asr.calls == []

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_vad_and_asr_failures_are_content_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="voice_agent.session")

    class BrokenVad(FakeVad):
        def probability(self, pcm_s16le: bytes) -> float:
            del pcm_s16le
            raise RuntimeError("raw input must not escape")

    class BrokenAsr(FakeAsr):
        async def transcribe_pcm16(self, pcm_s16le: bytes) -> Transcript:
            self.calls.append(pcm_s16le)
            raise RuntimeError("provider body must not escape")

    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    first_notices: list[SessionNotice] = []
    first_factory = FakeRtcFactory(room)
    first = _session(first_factory, vad=BrokenVad(), notices=first_notices)
    first_task = asyncio.create_task(first.run())
    await first.wait_started()
    first.deliver(_set_capture(True))
    await _wait_for(lambda: first.capture_open)
    first_factory.streams[0].feed()
    await _wait_for(lambda: any(item.reason == "vad_failed" for item in first_notices))
    await first.close("test")
    await first_task

    publication2 = FakePublication("TR_mic_2", track=FakeTrack())
    factory2 = FakeRtcFactory(FakeRoom([FakeParticipant("client-a", [publication2])]))
    second_notices: list[SessionNotice] = []
    second = _session(
        factory2,
        vad=FakeVad([0.9] * 8 + [0.1] * VAD_END_SILENCE_FRAMES),
        asr=BrokenAsr(),
        notices=second_notices,
    )
    second_task = asyncio.create_task(second.run())
    await second.wait_started()
    second.deliver(_set_capture(True))
    await _wait_for(lambda: second.capture_open)
    for _ in range(8 + VAD_END_SILENCE_FRAMES):
        factory2.streams[0].feed()
    await _wait_for(lambda: any(item.reason == "asr_failed" for item in second_notices))
    started = next(
        item for item in second_notices if item.kind == "recognition_started"
    )
    failed = next(item for item in second_notices if item.kind == "recognition_failed")
    assert failed.metadata == {"client_turn_id": started.metadata["client_turn_id"]}
    assert not any(item.text for item in second_notices)
    assert "voice_asr_failed reason=unexpected_adapter_error" in caplog.text
    assert "provider body must not escape" not in caplog.text
    await second.close("test")
    await second_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transcript", "expected_reason"),
    (
        (Transcript("   ", "en"), "empty_transcript"),
        (Transcript("hello", "invalid language"), "invalid_asr_result"),
        (None, "invalid_asr_result"),
    ),
)
async def test_invalid_asr_finals_emit_only_bounded_correlated_failures(
    transcript: Transcript | None,
    expected_reason: str,
) -> None:
    notices: list[SessionNotice] = []
    session = _session(FakeRtcFactory(FakeRoom()), notices=notices)
    client_turn_id = _uuid(31)
    _set_recognition_binding(session, client_turn_id)

    await session._recognition_complete(transcript, None)

    failed = next(item for item in notices if item.kind == "recognition_failed")
    assert failed.reason == expected_reason
    assert failed.metadata == {"client_turn_id": client_turn_id}
    assert failed.text is None
    assert client_turn_id in session._recognition_bindings
    await session._turn_bound(_turn_bound_frame(client_turn_id))
    session._transcript_disposition(_rejected_frame(client_turn_id))
    assert client_turn_id not in session._recognition_bindings


def test_only_greeting_may_have_a_null_turn() -> None:
    validate_announcement_binding(_speak())
    with pytest.raises(ProtocolViolation, match="announcement_turn_mismatch"):
        validate_announcement_binding(_speak(kind="progress", turn_id=None))
    with pytest.raises(ProtocolViolation, match="announcement_turn_mismatch"):
        validate_announcement_binding(_speak(turn_id=_uuid(25)))


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"kind": "", "turn_id": _uuid(25)}, "invalid_announcement_kind"),
        (
            {"kind": "progress", "turn_id": 7},
            "invalid_announcement_turn",
        ),
        ({"quantum_index": 2}, "invalid_announcement_quantum"),
        (
            {
                "kind": "result",
                "turn_id": _uuid(25),
                "quantum_role": "result_opening",
                "quantum_index": 1,
            },
            "invalid_announcement_quantum",
        ),
        (
            {
                "kind": "result",
                "turn_id": _uuid(25),
                "quantum_role": "result_continuation",
                "quantum_index": 0,
            },
            "invalid_announcement_quantum",
        ),
        ({"quantum_role": "other"}, "invalid_announcement_quantum"),
        ({"max_duration_samples": 96_001}, "invalid_announcement_ceiling"),
    ],
)
def test_announcement_quantum_validation_fails_closed(
    updates: dict[str, Any], reason: str
) -> None:
    frame = _speak(kind="progress", turn_id=_uuid(25))
    frame.update(updates)
    with pytest.raises(ProtocolViolation, match=reason):
        validate_announcement_binding(frame)


def test_result_opening_and_continuation_bindings_are_valid() -> None:
    opening = _speak(kind="result", turn_id=_uuid(25))
    opening.update(
        {
            "quantum_role": "result_opening",
            "quantum_index": 0,
            "max_duration_samples": 36_000,
        }
    )
    continuation = dict(opening)
    continuation.update(
        {
            "quantum_role": "result_continuation",
            "quantum_index": 1,
            "max_duration_samples": 96_000,
        }
    )
    validate_announcement_binding(opening)
    validate_announcement_binding(continuation)


@pytest.mark.asyncio
async def test_greeting_uses_small_output_queue_and_is_idempotent() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    tts = FakeTts(samples=960)
    notices: list[SessionNotice] = []
    session = _session(factory, tts=tts, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    session.deliver(_speak())
    await _wait_for(lambda: any(item.kind == "speech_finished" for item in notices))

    assert tts.calls == [("Hello. What can I help with?", 96_000)]
    assert factory.source_calls == [
        {"sample_rate": 24_000, "num_channels": 1, "queue_size_ms": OUTPUT_QUEUE_MS}
    ]
    assert sum(len(frame) for frame in factory.sources[0].frames) == 960 * 2
    assert room.local_participant.unpublished == ["TR_output_1"]
    assert factory.sources[0].close_calls == 1
    assert session.greeting_count == 1
    assert session.capture_open is False
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    assert len(room.local_participant.published_data) == 1
    manifest_record = room.local_participant.published_data[0]
    assert manifest_record["topic"] == "astraldeep.voice.announcement.v1"
    assert manifest_record["reliable"] is True
    assert manifest_record["destination_identities"] == ["client-a"]
    manifest = json.loads(manifest_record["payload"])
    assert manifest == {
        "type": "voice_announcement_media",
        "schema_version": "1",
        "session_id": _uuid(1),
        "generation": 1,
        "media_grant_revision": 1,
        "announcement_id": _uuid(20),
        "announcement_sequence": 1,
        "transport": "livekit",
        "worker_identity": "voice-worker-a",
        "turn_id": None,
        "kind": "greeting",
        "quantum_role": "single",
        "quantum_index": 0,
        "track_sid": "TR_output_1",
        "track_name": f"astraldeep.voice.{_uuid(20)}",
        "duration_samples": 960,
        "sample_rate_hz": 24_000,
    }
    started = next(item for item in notices if item.kind == "speech_started")
    finished = next(item for item in notices if item.kind == "speech_finished")
    expected_metadata = {
        "announcement_sequence": 1,
        "media_grant_revision": 1,
        "turn_id": None,
        "kind": "greeting",
        "quantum_role": "single",
        "quantum_index": 0,
    }
    assert started.metadata == expected_metadata
    assert finished.metadata == {**expected_metadata, "duration_ms": 40}

    session.deliver(_speak())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(tts.calls) == 1

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_stop_speech_advances_epoch_and_clears_output_twice() -> None:
    room = FakeRoom()
    factory = FakeRtcFactory(room, block_output=True)
    notices: list[SessionNotice] = []
    session = _session(factory, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_speak())
    await _wait_for(lambda: bool(factory.sources))
    source = factory.sources[0]
    await source.capture_entered.wait()
    epoch = session.speech_epoch

    session.deliver(
        {
            "type": "stop_speech",
            "session_id": _uuid(1),
            "generation": 1,
            "media_grant_revision": 1,
            "announcement_id": _uuid(20),
            "reason": "user_stop",
        }
    )
    await _wait_for(lambda: source.close_calls == 1)
    assert session.speech_epoch == epoch + 1
    assert source.clear_calls >= 2
    assert room.local_participant.unpublished == ["TR_output_1"]
    assert any(item.kind == "speech_interrupted" for item in notices)

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_tts_invalid_audio_publish_and_capture_fail_closed() -> None:
    class BrokenTts(FakeTts):
        async def synthesize(
            self, text: str, *, max_duration_samples: int
        ) -> SynthesizedAudio:
            self.calls.append((text, max_duration_samples))
            raise RuntimeError("provider body must not escape")

    class WrongProfileTts(FakeTts):
        async def synthesize(
            self, text: str, *, max_duration_samples: int
        ) -> SynthesizedAudio:
            self.calls.append((text, max_duration_samples))
            return SynthesizedAudio(
                pcm_s16le=b"\0\0" * 10,
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
                samples=10,
            )

    scenarios: list[tuple[FakeRtcFactory, FakeTts, str]] = []
    scenarios.append((FakeRtcFactory(FakeRoom()), BrokenTts(), "tts_failed"))
    scenarios.append(
        (
            FakeRtcFactory(FakeRoom()),
            WrongProfileTts(),
            "invalid_synthesized_audio",
        )
    )
    publish_room = FakeRoom()
    publish_room.local_participant.publish_error = RuntimeError("native detail")
    scenarios.append((FakeRtcFactory(publish_room), FakeTts(), "output_publish_failed"))
    scenarios.append(
        (
            FakeRtcFactory(FakeRoom(), capture_error=RuntimeError("native detail")),
            FakeTts(),
            "output_failed",
        )
    )

    for factory, tts, expected in scenarios:
        notices: list[SessionNotice] = []
        session = _session(factory, tts=tts, notices=notices)
        task = asyncio.create_task(session.run())
        await session.wait_started()
        session.deliver(_speak(announcement_id=_uuid(100 + len(notices))))
        await _wait_for(
            lambda: any(
                item.kind == "speech_failed" and item.reason == expected
                for item in notices
            )
        )
        assert "native detail" not in repr(notices)
        await session.close("test")
        await task


@pytest.mark.asyncio
async def test_expired_or_unsynced_speech_never_reaches_tts() -> None:
    factory = FakeRtcFactory(FakeRoom())
    tts = FakeTts()
    notices: list[SessionNotice] = []
    session = _session(factory, tts=tts, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()

    expired = _speak()
    expired["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    session.deliver(expired)
    with pytest.raises(RtcSessionError, match="session_runtime_failed"):
        await task
    assert tts.calls == []
    assert session.media_state == "failed"

    second_tts = FakeTts()
    second_notices: list[SessionNotice] = []
    second = _session(
        FakeRtcFactory(FakeRoom()), tts=second_tts, notices=second_notices
    )
    second_task = asyncio.create_task(second.run())
    await second.wait_started()
    second._context_synced = False
    second.deliver(_speak(announcement_id=_uuid(201)))
    await _wait_for(
        lambda: any(item.reason == "media_not_ready" for item in second_notices)
    )
    assert second_tts.calls == []
    await second.close("test")
    await second_task


@pytest.mark.asyncio
async def test_missing_expected_microphone_fails_capture_closed() -> None:
    room = FakeRoom([FakeParticipant("other-client", [FakePublication("TR_bad")])])
    notices: list[SessionNotice] = []
    session = _session(FakeRtcFactory(room), notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(
        lambda: any(item.reason == "microphone_unavailable" for item in notices)
    )
    assert session.capture_open is False

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_connect_failure_is_content_free_and_clears_grant() -> None:
    session = _session(
        FakeRtcFactory(FakeRoom(connect_error=RuntimeError("token leaked upstream")))
    )
    with pytest.raises(RtcSessionError, match="rtc_connect_failed"):
        await session.run()
    assert session.media_state == "failed"
    assert session.binding.worker_rtc_grant.join_token == ""
    assert "token leaked" not in repr(session)


@pytest.mark.asyncio
async def test_event_queue_overrun_and_notice_failure_fail_session_closed() -> None:
    room = FakeRoom()
    factory = FakeRtcFactory(room)
    session = DirectRtcSession(
        _binding(),
        rtc_factory=factory,
        vad=FakeVad(),
        asr=FakeAsr(),
        tts=FakeTts(),
        notice_sink=lambda notice: None,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
        rtc_event_queue_size=1,
    )
    task = asyncio.create_task(session.run())
    await session.wait_started()
    room.emit("participant_connected", FakeParticipant("other-a"))
    room.emit("participant_connected", FakeParticipant("other-b"))
    with pytest.raises(RtcSessionError, match="rtc_event_queue_overrun"):
        await task
    assert session.media_state == "failed"

    async_notices: list[SessionNotice] = []

    async def async_sink(notice: SessionNotice) -> None:
        async_notices.append(notice)

    second = DirectRtcSession(
        _binding(),
        rtc_factory=FakeRtcFactory(FakeRoom()),
        vad=FakeVad(),
        asr=FakeAsr(),
        tts=FakeTts(),
        notice_sink=async_sink,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
    )
    second_task = asyncio.create_task(second.run())
    await second.wait_started()
    assert async_notices
    await second.close("test")
    await second_task

    def broken_sink(notice: SessionNotice) -> None:
        del notice
        raise RuntimeError("do not log me")

    third = DirectRtcSession(
        _binding(),
        rtc_factory=FakeRtcFactory(FakeRoom()),
        vad=FakeVad(),
        asr=FakeAsr(),
        tts=FakeTts(),
        notice_sink=broken_sink,
        worker_control_secret=b"c" * 32,
        utcnow=lambda: NOW,
    )
    with pytest.raises(RtcSessionError, match="notice_sink_failed"):
        await third.run()
    assert third.media_state == "failed"


def test_session_constructor_and_notice_repr_guards() -> None:
    kwargs = {
        "rtc_factory": FakeRtcFactory(FakeRoom()),
        "vad": FakeVad(),
        "asr": FakeAsr(),
        "tts": FakeTts(),
        "notice_sink": lambda notice: None,
        "worker_control_secret": b"c" * 32,
    }
    with pytest.raises(ValueError, match="invalid_rtc_event_queue_size"):
        DirectRtcSession(_binding(), rtc_event_queue_size=0, **kwargs)
    notice = SessionNotice("final_transcript", text="sensitive transcript")
    assert "sensitive transcript" not in repr(notice)
    assert "<redacted>" in repr(notice)


@pytest.mark.asyncio
async def test_simultaneously_ready_control_and_rtc_events_are_not_lost() -> None:
    session = _session(FakeRtcFactory(FakeRoom()))
    control = {"type": "opaque-control"}
    rtc_event = session._callback("participant_connected")
    session._queue.put_nowait(control)
    rtc_event(FakeParticipant("other-client"))

    first_source, first_value = await session._next_owned_event()
    second_source, second_value = await session._next_owned_event()
    assert {first_source, second_source} == {"control", "rtc"}
    assert first_value is control or second_value is control
    await session.close("test")


@pytest.mark.asyncio
async def test_reconnect_fences_capture_and_reconciles_republished_input() -> None:
    first = FakePublication("TR_first", track=FakeTrack("first"))
    participant = FakeParticipant("client-a", [first])
    room = FakeRoom([participant])
    factory = FakeRtcFactory(room)
    session = _session(factory)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    room.emit("reconnecting")
    assert session.media_state == "ready"
    await _wait_for(lambda: session.media_state == "reconnecting")
    assert session.capture_open is False
    assert factory.streams[0].closed == 1

    second = FakePublication("TR_second", track=FakeTrack("second"))
    participant.track_publications = {second.sid: second}
    room.emit("reconnected")
    await _wait_for(lambda: len(factory.streams) == 2)
    assert second.subscription_calls == [True]
    assert session.media_state == "ready"
    assert session.capture_open is True

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_context_and_grant_rotation_fence_then_apply() -> None:
    first = FakePublication("TR_first", track=FakeTrack("first"))
    first_participant = FakeParticipant("client-a", [first])
    second = FakePublication("TR_second", track=FakeTrack("second"))
    second_participant = FakeParticipant("client-next", [second])
    room = FakeRoom([first_participant, second_participant])
    factory = FakeRtcFactory(room)
    notices: list[SessionNotice] = []
    session = _session(factory, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    session.deliver(
        {
            "type": "session_context_update",
            "session_id": _uuid(1),
            "generation": 1,
            "media_grant_revision": 1,
            "visible_chat_id": _uuid(40),
            "chat_context_revision": 2,
        }
    )
    await _wait_for(lambda: session.binding.chat_context_revision == 2)
    assert session.binding.visible_chat_id == _uuid(40)
    assert session.context_synced is True
    assert session.capture_open is True

    rotation = {
        "type": "media_grant_rotated",
        "session_id": _uuid(1),
        "generation": 1,
        "refresh_id": _uuid(41),
        "previous_media_grant_revision": 1,
        "media_grant_revision": 2,
        "client_participant_identity": "client-next",
        "transport": "livekit",
        "grant_expires_at": (NOW + timedelta(minutes=3)).isoformat(),
    }
    session.deliver(dict(rotation))
    await _wait_for(lambda: session.binding.media_grant_revision == 2)
    assert session.binding.client_participant_identity == "client-next"
    assert second.subscription_calls == [True]
    assert len(factory.streams) == 2
    assert session.capture_open is True
    grant_applied = next(item for item in notices if item.kind == "media_grant_applied")
    assert grant_applied.metadata == {
        "refresh_id": _uuid(41),
        "media_grant_revision": 2,
        "client_participant_identity": "client-next",
    }
    session.deliver(dict(rotation))
    await _wait_for(
        lambda: (
            len([item for item in notices if item.kind == "media_grant_applied"]) == 2
        )
    )
    assert session.binding.media_grant_revision == 2
    assert session.capture_open is True

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_final_transcript_publishes_only_reliable_targeted_livekit_data() -> None:
    room = FakeRoom()
    notices: list[SessionNotice] = []
    session = _session(FakeRtcFactory(room), notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    client_turn_id = _uuid(30)
    _set_recognition_binding(session, client_turn_id)
    envelope = _transcript_envelope(client_turn_id=client_turn_id)

    await session.publish_transcript_envelope(envelope)

    assert len(room.local_participant.published_data) == 1
    published = room.local_participant.published_data[0]
    assert published["reliable"] is True
    assert published["destination_identities"] == ["client-a"]
    assert published["topic"] == VOICE_TRANSCRIPT_TOPIC
    assert json.loads(published["payload"]) == envelope
    emitted = next(item for item in notices if item.kind == "transcript_emitted")
    assert emitted.text is None
    assert emitted.metadata == {
        "turn_id": _uuid(31),
        "client_turn_id": client_turn_id,
        "submission_id": _uuid(32),
        "request_generation": _uuid(33),
        "chat_id": _uuid(3),
        "chat_context_revision": 1,
        "media_grant_revision": 1,
        "final": True,
        "utf8_bytes": len(envelope["text"].encode("utf-8")),
        "text_digest_sha256": envelope["text_digest_sha256"],
        "proof_expires_at": envelope["proof_expires_at"],
    }
    assert emitted.language == "en"

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_transcript_keeps_recognition_context_across_navigation_and_rotation() -> (
    None
):
    room = FakeRoom()
    session = _session(FakeRtcFactory(room))
    task = asyncio.create_task(session.run())
    await session.wait_started()
    client_turn_id = _uuid(30)
    _set_recognition_binding(session, client_turn_id)
    envelope = _transcript_envelope(client_turn_id=client_turn_id)
    session.binding.visible_chat_id = _uuid(90)
    session.binding.chat_context_revision = 2
    session.binding.media_grant_revision = 2
    session.binding.client_participant_identity = "client-next"

    await session.publish_transcript_envelope(envelope)

    published = room.local_participant.published_data[0]
    assert published["destination_identities"] == ["client-next"]
    assert json.loads(published["payload"])["chat_id"] == _uuid(3)
    assert json.loads(published["payload"])["media_grant_revision"] == 1

    await session.close("test")
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda value: value.update(session_id=_uuid(99)),
            "transcript_binding_mismatch",
        ),
        (
            lambda value: value.update(client_turn_id=_uuid(99)),
            "transcript_binding_mismatch",
        ),
        (lambda value: value.update(text=" padded "), "noncanonical_transcript"),
        (lambda value: value.update(text="bad\rcarriage"), "invalid_transcript_text"),
        (
            lambda value: value.update(text_digest_sha256="b" * 64),
            "transcript_digest_mismatch",
        ),
        (
            lambda value: value.update(
                proof_expires_at=(NOW - timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "transcript_proof_expired",
        ),
        (lambda value: value.update(extra=True), "invalid_transcript_fields"),
    ],
)
async def test_transcript_publisher_rejects_invalid_or_stale_envelopes(
    mutate: Any, reason: str
) -> None:
    room = FakeRoom()
    session = _session(FakeRtcFactory(room))
    task = asyncio.create_task(session.run())
    await session.wait_started()
    _set_recognition_binding(session, _uuid(30))
    envelope = _transcript_envelope(client_turn_id=_uuid(30))
    mutate(envelope)

    with pytest.raises(ProtocolViolation, match=reason):
        await session.publish_transcript_envelope(envelope)
    assert room.local_participant.published_data == []

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_transcript_publisher_rejects_oversize_and_disconnected_output() -> None:
    room = FakeRoom()
    session = _session(FakeRtcFactory(room))
    _set_recognition_binding(session, _uuid(30))
    oversized = _transcript_envelope(client_turn_id=_uuid(30), text="x" * 8_001)
    with pytest.raises(ProtocolViolation, match="transcript_text_too_large"):
        await session.publish_transcript_envelope(oversized)

    valid = _transcript_envelope(client_turn_id=_uuid(30))
    with pytest.raises(RtcSessionError, match="rtc_room_unavailable"):
        await session.publish_transcript_envelope(valid)


@pytest.mark.asyncio
async def test_subscription_and_departure_callbacks_remove_input() -> None:
    publication = FakePublication("TR_mic")
    participant = FakeParticipant("client-a", [publication])
    room = FakeRoom([participant])
    factory = FakeRtcFactory(room)
    session = _session(factory)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    assert publication.subscription_calls == [True]
    assert factory.streams == []

    track = FakeTrack()
    publication.track = track
    room.emit("track_subscribed", track, publication, participant)
    await _wait_for(lambda: len(factory.streams) == 1)
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    room.emit("track_unsubscribed", track, publication, participant)
    await _wait_for(lambda: not session.capture_open)
    publication.track = track
    room.emit("track_subscribed", track, publication, participant)
    await _wait_for(lambda: len(factory.streams) == 2)
    room.emit("participant_disconnected", participant)
    await _wait_for(lambda: not session.capture_open)

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_local_republish_rekeys_active_output_sid() -> None:
    room = FakeRoom()
    factory = FakeRtcFactory(room, block_output=True)
    session = _session(factory)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_speak())
    await _wait_for(lambda: session.output_track_sid == "TR_output_1")

    room.local_participant.publication.sid = "TR_output_2"
    room.emit(
        "local_track_republished",
        room.local_participant.publication,
        "TR_output_1",
    )
    await _wait_for(lambda: session.output_track_sid == "TR_output_2")

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_close_erases_media_control_buffers_and_grant() -> None:
    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    vad = FakeVad([0.9] * 100)
    session = _session(factory, vad=vad)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    for _ in range(4):
        factory.streams[0].feed()
    await _wait_for(lambda: session.retained_audio_bytes > 0)
    speech_epoch = session.speech_epoch
    buffered = {"text": "sensitive", "nested": [{"token": "temporary"}]}
    session.deliver(buffered)

    await session.close("test")
    await session.close("duplicate_callback")
    await task
    assert buffered == {}
    assert session.retained_audio_bytes == 0
    assert session.retained_final_count == 0
    assert session.binding.worker_rtc_grant.join_token == ""
    assert room.disconnect_calls == 1
    assert all(stream.closed >= 1 for stream in factory.streams)
    assert publication.subscription_calls == [True, False]
    assert session.speech_epoch == speech_epoch + 1


@pytest.mark.asyncio
async def test_cancelled_close_finishes_active_track_and_room_cleanup_once() -> None:
    class GatedDisconnectRoom(FakeRoom):
        def __init__(self, participants: list[FakeParticipant]) -> None:
            super().__init__(participants)
            self.disconnect_started = asyncio.Event()
            self.disconnect_release = asyncio.Event()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            self.disconnect_started.set()
            await self.disconnect_release.wait()

    publication = FakePublication("TR_mic", track=FakeTrack())
    room = GatedDisconnectRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room, block_output=True)
    session = _session(factory)
    run_task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_speak())
    await _wait_for(lambda: session.output_track_sid == "TR_output_1")
    speech_epoch = session.speech_epoch

    close_task = asyncio.create_task(session.close("logout"))
    await room.disconnect_started.wait()
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    room.disconnect_release.set()
    await run_task
    await session.close("duplicate_callback")

    assert session.speech_epoch == speech_epoch + 1
    assert publication.subscription_calls == [True, False]
    assert room.local_participant.unpublished == ["TR_output_1"]
    assert factory.sources[0].close_calls == 1
    assert factory.sources[0].clear_calls >= 2
    assert room.disconnect_calls == 1


def test_exact_silero_v6_recurrent_state_resets_in_image() -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    model = Path("/opt/voice-assets/silero_vad.onnx")
    if not model.is_file():
        pytest.skip("exact Silero asset is an in-image guard")
    vad = SileroVad(model_path=model)
    assert vad.recurrent_state_shape == (2, 1, 128)
    first = vad.probability(b"\0\0" * AUDIO_FRAME_SAMPLES)
    assert math.isfinite(first)
    assert 0.0 <= first <= 1.0
    assert np.any(vad.recurrent_state != 0.0)
    vad.reset()
    assert np.all(vad.recurrent_state == 0.0)


def test_silero_rejects_bad_frames_and_inference_outputs_in_image(
    tmp_path: Path,
) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    model = Path("/opt/voice-assets/silero_vad.onnx")
    if not model.is_file():
        pytest.skip("exact Silero asset is an in-image guard")
    with pytest.raises(RtcSessionError, match="vad_model_unavailable"):
        SileroVad(model_path=tmp_path / "missing.onnx")
    invalid = tmp_path / "invalid.onnx"
    invalid.write_bytes(b"not an onnx model")
    with pytest.raises(RtcSessionError, match="vad_model_invalid"):
        SileroVad(model_path=invalid)

    vad = SileroVad(model_path=model)
    with pytest.raises(RtcSessionError, match="invalid_vad_frame"):
        vad.probability(b"\0\0")

    class BrokenSession:
        def run(self, outputs: Any, inputs: Any) -> Any:
            del outputs, inputs
            raise RuntimeError("native detail")

    vad._session = BrokenSession()
    with pytest.raises(RtcSessionError, match="vad_inference_failed"):
        vad.probability(b"\0\0" * AUDIO_FRAME_SAMPLES)

    class WrongShapeSession:
        def run(self, outputs: Any, inputs: Any) -> Any:
            del outputs, inputs
            return np.zeros((2, 1)), np.zeros((2, 1, 128))

    vad._session = WrongShapeSession()
    with pytest.raises(RtcSessionError, match="vad_output_invalid"):
        vad.probability(b"\0\0" * AUDIO_FRAME_SAMPLES)

    class NonfiniteSession:
        def run(self, outputs: Any, inputs: Any) -> Any:
            del outputs, inputs
            return np.array([[np.nan]]), np.zeros((2, 1, 128))

    vad._session = NonfiniteSession()
    with pytest.raises(RtcSessionError, match="vad_output_invalid"):
        vad.probability(b"\0\0" * AUDIO_FRAME_SAMPLES)


def test_livekit_factory_constructs_exact_pinned_runtime_objects_in_image() -> None:
    rtc = pytest.importorskip("livekit.rtc")
    factory = LiveKitRtcFactory()
    options = factory.room_options(auto_subscribe=False, connect_timeout=8.0)
    assert isinstance(options, rtc.RoomOptions)
    assert options.auto_subscribe is False
    assert options.connect_timeout == 8.0


def test_livekit_factory_disables_vendor_rtc_diagnostics() -> None:
    module = SimpleNamespace()
    for logger_name in ("livekit", "livekit.rtc", "livekit.rtc.synchronizer"):
        logging.getLogger(logger_name).disabled = False

    LiveKitRtcFactory(module)

    assert all(
        logging.getLogger(logger_name).disabled
        for logger_name in ("livekit", "livekit.rtc", "livekit.rtc.synchronizer")
    )


def test_livekit_factory_maps_every_audited_surface_with_fake_module() -> None:
    calls: dict[str, Any] = {}

    class NativeFrame:
        def __init__(self, samples: int = 2) -> None:
            self.sample_rate = 16_000
            self.num_channels = 1
            self.samples_per_channel = samples
            self.data = memoryview(bytearray(samples * 2)).cast("h")

    class AudioStreamType:
        @staticmethod
        def from_track(**kwargs: Any) -> str:
            calls["stream"] = kwargs
            return "stream"

    class AudioSourceType:
        def __init__(self, **kwargs: Any) -> None:
            calls["source"] = kwargs

    class LocalAudioTrackType:
        @staticmethod
        def create_audio_track(name: str, source: Any) -> tuple[str, Any]:
            calls["track"] = (name, source)
            return name, source

    class TrackPublishOptionsType:
        source: int = -1

    class AudioFrameType:
        @staticmethod
        def create(
            *, sample_rate: int, num_channels: int, samples_per_channel: int
        ) -> NativeFrame:
            calls["frame"] = (
                sample_rate,
                num_channels,
                samples_per_channel,
            )
            return NativeFrame(samples_per_channel)

    module = SimpleNamespace(
        Room=lambda: "room",
        RoomOptions=lambda **kwargs: kwargs,
        TrackKind=SimpleNamespace(KIND_AUDIO=1),
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE=2, SOURCE_UNKNOWN=0),
        AudioStream=AudioStreamType,
        AudioSource=AudioSourceType,
        LocalAudioTrack=LocalAudioTrackType,
        TrackPublishOptions=TrackPublishOptionsType,
        AudioFrame=AudioFrameType,
    )
    factory = LiveKitRtcFactory(module)
    assert factory.create_room() == "room"
    assert factory.room_options(auto_subscribe=False, connect_timeout=8.0) == {
        "auto_subscribe": False,
        "connect_timeout": 8.0,
    }
    assert factory.is_audio_publication(SimpleNamespace(kind=1)) is True
    assert factory.is_microphone_publication(SimpleNamespace(source=2)) is True
    assert (
        factory.create_audio_stream(
            "track",
            capacity=32,
            sample_rate=16_000,
            num_channels=1,
            frame_size_ms=32,
        )
        == "stream"
    )
    event = SimpleNamespace(frame=NativeFrame())
    assert factory.decode_audio_event(event) == (b"\0\0\0\0", 16_000, 1, 2)
    stream = SimpleNamespace(_queue=SimpleNamespace(_queue=deque([1, 2])))
    assert factory.stream_buffer_depth(event, stream) == 2
    source = factory.create_audio_source(
        sample_rate=24_000, num_channels=1, queue_size_ms=200
    )
    assert calls["source"] == {
        "sample_rate": 24_000,
        "num_channels": 1,
        "queue_size_ms": 200,
    }
    assert factory.create_local_audio_track("assistant", source)[0] == "assistant"
    assert factory.track_publish_options().source == 2
    frame = factory.create_output_frame(b"\1\0\2\0", sample_rate=24_000, num_channels=1)
    assert bytes(frame.data.cast("B")) == b"\1\0\2\0"
    with pytest.raises(RtcSessionError, match="invalid_output_pcm"):
        factory.create_output_frame(b"odd", sample_rate=24_000, num_channels=1)
