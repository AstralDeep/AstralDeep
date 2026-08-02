"""Locked-image, real-LiveKit integration proof for Feature 065.

This test is intentionally dormant in the default networkless worker suite.
The dedicated runner supplies an isolated Docker network and ephemeral test
credentials. Audio and transcript bytes remain in memory and are cleared on
every exit path.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import wave
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from voice_agent.session import (
    AUDIO_FRAME_SAMPLES,
    AUDIO_STREAM_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
    DirectRtcSession,
    LiveKitRtcFactory,
    SessionBinding,
    SessionNotice,
    SileroVad,
    WorkerRtcGrant,
)
from voice_agent.speech_adapters import (
    ASR_MODEL,
    KOKORO_MODEL,
    KOKORO_SAMPLE_RATE,
    KOKORO_VOICE,
    SpeechPreflight,
    SpeachesBatchSTT,
    SpeachesTTS,
)
from voice_agent.tests.fake_speech_service import FakeResponse, StrictFakeSpeechService


INTEGRATION_ENABLED = os.getenv("ASTRAL_VOICE_LIVEKIT_INTEGRATION") == "1"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not INTEGRATION_ENABLED,
        reason="dedicated isolated LiveKit integration environment is required",
    ),
]


def _uuid(value: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | value))


def _grant_claims(token: str) -> dict[str, Any]:
    """Decode only for claim assertions; the real server verifies the HMAC."""

    parts = token.split(".")
    assert len(parts) == 3
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    assert isinstance(claims, dict)
    return claims


class _DeterministicVad:
    """Exercise session endpointing deterministically after real RTC decoding."""

    def __init__(self) -> None:
        self._probabilities = deque([0.9] * 4 + [0.0] * 64)
        self.frames = 0
        self.nonzero_frames = 0

    def probability(self, pcm_s16le: bytes) -> float:
        assert len(pcm_s16le) == AUDIO_FRAME_SAMPLES * 2
        self.frames += 1
        self.nonzero_frames += int(any(pcm_s16le))
        return self._probabilities.popleft() if self._probabilities else 0.0

    def reset(self) -> None:
        return None


async def _wait_for(predicate: Any, *, timeout: float = 12.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout)


async def _wait_for_livekit(host: str, port: int = 7880) -> None:
    deadline = asyncio.get_running_loop().time() + 10.0
    while True:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=0.5
            )
        except (OSError, TimeoutError):
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("livekit_not_ready") from None
            await asyncio.sleep(0.1)
        else:
            writer.close()
            await writer.wait_closed()
            return


def _pcm_frame(rtc: Any, *, sample: int = 1_000) -> Any:
    frame = rtc.AudioFrame.create(
        sample_rate=AUDIO_STREAM_SAMPLE_RATE,
        num_channels=1,
        samples_per_channel=AUDIO_FRAME_SAMPLES,
    )
    sample_bytes = sample.to_bytes(2, "little", signed=True)
    frame.data.cast("B")[:] = sample_bytes * AUDIO_FRAME_SAMPLES
    return frame


def _wav(*, samples: int, sample: int = 600) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(KOKORO_SAMPLE_RATE)
        stream.writeframes(sample.to_bytes(2, "little", signed=True) * samples)
    return output.getvalue()


async def _packet_for_topic(queue: asyncio.Queue[Any], topic: str) -> Any:
    async def receive() -> Any:
        while True:
            packet = await queue.get()
            if packet.topic == topic:
                return packet

    return await asyncio.wait_for(receive(), timeout=12.0)


def _turn_bound(session_id: str, client_turn_id: str) -> dict[str, Any]:
    return {
        "type": "turn_bound",
        "session_id": session_id,
        "generation": 1,
        "client_turn_id": client_turn_id,
        "turn_id": _uuid(31),
        "chat_id": _uuid(3),
        "chat_context_revision": 1,
        "media_grant_revision": 1,
        "submission_id": _uuid(32),
        "request_generation": _uuid(33),
    }


def _transcript_accepted(session_id: str, client_turn_id: str) -> dict[str, Any]:
    return {
        **_turn_bound(session_id, client_turn_id),
        "type": "transcript_accepted",
        "accepted_message_id": 17,
    }


def _result_speak(session_id: str, announcement_id: str) -> dict[str, Any]:
    return {
        "type": "speak",
        "session_id": session_id,
        "generation": 1,
        "media_grant_revision": 1,
        "transport": "livekit",
        "announcement_id": announcement_id,
        "announcement_sequence": 1,
        "turn_id": _uuid(31),
        "kind": "result",
        "quantum_role": "result_opening",
        "quantum_index": 0,
        "max_duration_samples": 36_000,
        "result_reserved_samples_after": 36_000,
        "text": "The request is complete.",
        "sensitive_authorized": False,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }


def _audio_artifacts() -> set[Path]:
    suffixes = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".pcm", ".wav"}
    found: set[Path] = set()
    for root in (Path("/app"), Path("/tmp")):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in suffixes:
                found.add(path)
    return found


@pytest.mark.asyncio
async def test_locked_worker_real_livekit_speech_round_trip_is_ephemeral() -> None:
    from livekit import rtc
    from voice_agent.streaming_egress import FixedOriginHttpTransport

    livekit_url = os.environ["VOICE_INTEGRATION_LIVEKIT_URL"]
    livekit_host = os.environ["VOICE_INTEGRATION_LIVEKIT_HOST"]
    room_name = os.environ["VOICE_INTEGRATION_ROOM_NAME"]
    worker_token = os.environ.pop("VOICE_INTEGRATION_WORKER_TOKEN")
    client_token = os.environ.pop("VOICE_INTEGRATION_CLIENT_TOKEN")
    for forbidden_name in (
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "VOICE_INTEGRATION_LIVEKIT_API_KEY",
        "VOICE_INTEGRATION_LIVEKIT_API_SECRET",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        assert not os.environ.get(forbidden_name)
    assert KOKORO_MODEL == "speaches-ai/Kokoro-82M-v1.0-ONNX"
    assert ASR_MODEL == "Systran/faster-whisper-large-v3"
    assert KOKORO_VOICE == "af_heart"
    assert OUTPUT_SAMPLE_RATE == KOKORO_SAMPLE_RATE == 24_000

    await _wait_for_livekit(livekit_host)
    audio_before = _audio_artifacts()
    session_id = _uuid(1)
    worker_identity = "voice-worker-integration"
    client_identity = "client-integration"
    worker_claims = _grant_claims(worker_token)
    client_claims = _grant_claims(client_token)
    issued_at = datetime.fromtimestamp(worker_claims["nbf"], UTC)
    for claims in (worker_claims, client_claims):
        assert claims["exp"] - claims["nbf"] == 90
        assert claims["video"]["room"] == room_name
        assert claims["video"]["roomCreate"] is False
        assert claims["video"]["roomJoin"] is True
        assert claims["video"]["canPublishSources"] == ["microphone"]
    assert worker_claims["iss"] == client_claims["iss"]
    assert worker_claims["sub"] == worker_identity
    assert client_claims["sub"] == client_identity
    assert worker_claims["video"]["canPublishData"] is True
    assert client_claims["video"]["canPublishData"] is False

    data_packets: asyncio.Queue[Any] = asyncio.Queue()
    output_audio: asyncio.Queue[dict[str, int | str]] = asyncio.Queue()
    collector_tasks: set[asyncio.Task[None]] = set()
    client_room = rtc.Room()

    @client_room.on("data_received")
    def receive_data(packet: Any) -> None:
        data_packets.put_nowait(packet)

    async def collect_audio(track: Any, track_sid: str) -> None:
        stream = rtc.AudioStream.from_track(
            track=track,
            capacity=16,
            sample_rate=KOKORO_SAMPLE_RATE,
            num_channels=1,
            frame_size_ms=20,
        )
        samples = 0
        nonzero = False
        try:
            async for event in stream:
                frame = event.frame
                assert frame.sample_rate == KOKORO_SAMPLE_RATE
                assert frame.num_channels == 1
                samples += frame.samples_per_channel
                nonzero = nonzero or any(frame.data.cast("B"))
                if samples >= KOKORO_SAMPLE_RATE // 10:
                    output_audio.put_nowait(
                        {
                            "track_sid": track_sid,
                            "samples": samples,
                            "sample_rate": frame.sample_rate,
                            "nonzero": int(nonzero),
                        }
                    )
                    return
        finally:
            await stream.aclose()

    @client_room.on("track_subscribed")
    def receive_track(track: Any, publication: Any, participant: Any) -> None:
        del participant
        task = asyncio.create_task(collect_audio(track, str(publication.sid)))
        collector_tasks.add(task)
        task.add_done_callback(collector_tasks.discard)

    input_source: Any | None = None
    session: DirectRtcSession | None = None
    session_task: asyncio.Task[None] | None = None
    service: StrictFakeSpeechService | None = None
    notices: list[SessionNotice] = []
    vad = _DeterministicVad()
    try:
        await client_room.connect(
            livekit_url,
            client_token,
            rtc.RoomOptions(auto_subscribe=True, connect_timeout=8.0),
        )
        client_token = ""
        input_source = rtc.AudioSource(
            AUDIO_STREAM_SAMPLE_RATE,
            1,
            queue_size_ms=120,
        )
        input_track = rtc.LocalAudioTrack.create_audio_track(
            "astraldeep.integration.microphone",
            input_source,
        )
        input_options = rtc.TrackPublishOptions()
        input_options.source = rtc.TrackSource.SOURCE_MICROPHONE
        input_publication = await client_room.local_participant.publish_track(
            input_track,
            input_options,
        )

        async with StrictFakeSpeechService() as service:
            transport = FixedOriginHttpTransport(
                service.origin,
                allow_insecure_loopback_development=True,
            )
            profile = await SpeechPreflight(
                transport=transport,
                api_key=service.api_key,
            ).run()
            assert (
                profile.asr_model,
                profile.tts_model,
                profile.voice,
                profile.sample_rate_hz,
            ) == (ASR_MODEL, KOKORO_MODEL, KOKORO_VOICE, KOKORO_SAMPLE_RATE)
            service.enqueue(
                "/v1/audio/speech",
                FakeResponse(200, _wav(samples=24_000), "audio/wav"),
            )
            now = datetime.now(UTC)
            binding = SessionBinding(
                session_id=session_id,
                generation=1,
                assignment_id=_uuid(2),
                room_name=room_name,
                worker_identity=worker_identity,
                transport="livekit",
                media_grant_revision=1,
                worker_rtc_grant_revision=1,
                client_participant_identity=client_identity,
                grant_expires_at=now + timedelta(seconds=89),
                worker_rtc_grant=WorkerRtcGrant(
                    revision=1,
                    livekit_url=livekit_url,
                    join_token=worker_token,
                    issued_at=issued_at,
                    expires_at=issued_at + timedelta(seconds=90),
                    room_name=room_name,
                    worker_identity=worker_identity,
                ),
                visible_chat_id=_uuid(3),
                chat_context_revision=1,
            )
            session = DirectRtcSession(
                binding,
                rtc_factory=LiveKitRtcFactory(),
                vad=vad,
                asr=SpeachesBatchSTT(transport=transport, api_key=service.api_key),
                tts=SpeachesTTS(transport=transport, api_key=service.api_key),
                notice_sink=notices.append,
                worker_control_secret=b"c" * 32,
            )
            session_task = asyncio.create_task(session.run())
            await session.wait_started(timeout=8.0)
            worker_token = ""
            assert binding.worker_rtc_grant.join_token == ""
            await _wait_for(lambda: bool(session._input_handles))
            session.deliver(
                {
                    "type": "set_capture",
                    "session_id": session_id,
                    "generation": 1,
                    "media_grant_revision": 1,
                    "enabled": True,
                }
            )
            await _wait_for(lambda: session.capture_open)

            input_frame = _pcm_frame(rtc)
            for _ in range(48):
                await input_source.capture_frame(input_frame)
            del input_frame
            await _wait_for(lambda: session.retained_final_count == 1)
            recognition = next(
                notice for notice in notices if notice.kind == "recognition_started"
            )
            client_turn_id = str(recognition.metadata["client_turn_id"])
            session.deliver(_turn_bound(session_id, client_turn_id))
            await _wait_for(
                lambda: session_task.done()
                or any(notice.kind == "transcript_emitted" for notice in notices)
            )
            assert not session_task.done(), "session_failed_before_transcript_delivery"
            transcript_packet = await _packet_for_topic(
                data_packets, "astraldeep.voice.transcript.v1"
            )
            assert transcript_packet.kind == rtc.DataPacketKind.KIND_RELIABLE
            transcript = json.loads(transcript_packet.data)
            assert transcript["final"] is True
            assert transcript["text"] == "synthetic request"
            assert transcript["detected_language"] == "en"
            assert transcript["client_turn_id"] == client_turn_id
            assert transcript["turn_id"] == _uuid(31)
            assert transcript["source_participant_identity"] == worker_identity
            session.deliver(_transcript_accepted(session_id, client_turn_id))
            await _wait_for(lambda: session.retained_final_count == 0)

            announcement_id = _uuid(40)
            session.deliver(_result_speak(session_id, announcement_id))
            announcement_packet = await _packet_for_topic(
                data_packets, "astraldeep.voice.announcement.v1"
            )
            assert announcement_packet.kind == rtc.DataPacketKind.KIND_RELIABLE
            announcement = json.loads(announcement_packet.data)
            assert announcement["announcement_id"] == announcement_id
            assert announcement["turn_id"] == transcript["turn_id"]
            assert announcement["kind"] == "result"
            assert announcement["sample_rate_hz"] == KOKORO_SAMPLE_RATE
            assert announcement["duration_samples"] == 24_000
            rendered = await asyncio.wait_for(output_audio.get(), timeout=12.0)
            assert rendered["track_sid"] == announcement["track_sid"]
            assert rendered["sample_rate"] == KOKORO_SAMPLE_RATE
            assert 0 < int(rendered["samples"]) <= 24_000
            assert rendered["nonzero"] == 1

            paths = [request.target for request in service.requests]
            assert paths == [
                "/v1/models",
                "/v1/audio/transcriptions",
                "/v1/audio/speech",
                "/v1/audio/transcriptions",
                "/v1/audio/speech",
            ]
            runtime_asr = service.requests[-2]
            runtime_tts = service.requests[-1]
            assert ASR_MODEL.encode("utf-8") in runtime_asr.body
            assert len(runtime_asr.body) < 256 * 1024
            wav_offset = runtime_asr.body.index(b"RIFF")
            with wave.open(io.BytesIO(runtime_asr.body[wav_offset:]), "rb") as stream:
                assert stream.getframerate() == AUDIO_STREAM_SAMPLE_RATE
                assert stream.getnchannels() == 1
                assert any(stream.readframes(stream.getnframes()))
            assert json.loads(runtime_tts.body) == {
                "input": "The request is complete.",
                "model": KOKORO_MODEL,
                "response_format": "wav",
                "voice": KOKORO_VOICE,
            }
            assert all(
                request.headers.get("authorization")
                == "Bearer " + service.api_key
                for request in service.requests
            )
            transcript.clear()
            announcement.clear()
            transcript_packet = None
            announcement_packet = None
            del runtime_asr, runtime_tts

            session.deliver(
                {
                    "type": "set_capture",
                    "session_id": session_id,
                    "generation": 1,
                    "media_grant_revision": 1,
                    "enabled": False,
                }
            )
            await session.close("integration_complete")
            await asyncio.wait_for(session_task, timeout=5.0)
            assert session.media_state == "ended"
            assert session.retained_audio_bytes == 0
            assert session.retained_final_count == 0
            assert session.retained_final_bytes == 0
            assert not session._input_handles
            assert session._output_source is None
            assert vad.frames >= 44
            assert vad.nonzero_frames >= 1
            assert SileroVad().recurrent_state_shape == (2, 1, 128)

        assert service is not None and service.requests == []
        service.api_key = ""
        assert input_publication.sid
    finally:
        if session is not None:
            await session.close("integration_cleanup")
        if session_task is not None:
            await asyncio.gather(session_task, return_exceptions=True)
        if input_source is not None:
            input_source.clear_queue()
            await input_source.aclose()
        for task in tuple(collector_tasks):
            task.cancel()
        if collector_tasks:
            await asyncio.gather(*collector_tasks, return_exceptions=True)
        await client_room.disconnect()
        worker_token = ""
        client_token = ""
        worker_claims.clear()
        client_claims.clear()

    assert _audio_artifacts() == audio_before
