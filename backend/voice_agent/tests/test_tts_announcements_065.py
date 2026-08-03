"""Exact-profile and hard-budget tests for Feature 065 speech synthesis."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from voice_agent.control import PoolClient, ProtocolViolation
from voice_agent.session import SessionNotice
from voice_agent.speech_adapters import (
    KOKORO_MODEL,
    KOKORO_SAMPLE_RATE,
    KOKORO_VOICE,
    HttpRequest,
    HttpResponse,
    SpeechAdapterError,
    SpeachesTTS,
    SynthesizedAudio,
)
from voice_agent.tests.test_session_start_065 import (
    NOW,
    FakeLocalParticipant,
    FakeRoom,
    FakeRtcFactory,
    FakeTts,
    _session,
    _speak,
    _uuid,
    _wait_for,
)
from voice_agent.tests.test_worker_runtime_integration_065 import (
    _config,
    _speak_frame,
)


def _wav(
    samples: int,
    *,
    sample_rate: int = KOKORO_SAMPLE_RATE,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\0" * samples * channels * sample_width)
    return output.getvalue()


class FakeTransport:
    def __init__(
        self,
        response: HttpResponse | Callable[[HttpRequest], Awaitable[HttpResponse]],
    ) -> None:
        self.response = response
        self.requests: list[HttpRequest] = []

    async def post(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if callable(self.response):
            return await self.response(request)
        return self.response


class StaticAudioTts:
    def __init__(self, audio: SynthesizedAudio) -> None:
        self.audio = audio
        self.calls: list[tuple[str, int]] = []

    async def synthesize(
        self,
        text: str,
        *,
        max_duration_samples: int,
    ) -> SynthesizedAudio:
        self.calls.append((text, max_duration_samples))
        return self.audio


class BlockingTts:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[tuple[str, int]] = []

    async def synthesize(
        self,
        text: str,
        *,
        max_duration_samples: int,
    ) -> SynthesizedAudio:
        self.calls.append((text, max_duration_samples))
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class ManifestFailingLocalParticipant(FakeLocalParticipant):
    def __init__(self) -> None:
        super().__init__()
        self.manifest_attempts = 0

    async def publish_data(
        self,
        payload: bytes,
        *,
        reliable: bool,
        destination_identities: list[str],
        topic: str,
    ) -> None:
        del payload, reliable, destination_identities, topic
        self.manifest_attempts += 1
        raise RuntimeError("manifest provider detail must stay redacted")


@pytest.mark.asyncio
async def test_kokoro_request_and_valid_wav_are_exact() -> None:
    transport = FakeTransport(
        HttpResponse(
            status=200, headers={"content-type": "audio/wav"}, body=_wav(24_000)
        )
    )
    adapter = SpeachesTTS(transport=transport, api_key="session-secret")

    audio = await adapter.synthesize("On it!", max_duration_samples=96_000)

    assert audio.sample_rate == 24_000
    assert audio.channels == 1
    assert audio.sample_width_bytes == 2
    assert audio.samples == 24_000
    assert len(audio.pcm_s16le) == 48_000
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.path == "/audio/speech"
    assert request.headers == {
        "accept": "audio/wav",
        "authorization": "Bearer session-secret",
        "content-type": "application/json",
    }
    assert request.max_response_bytes == 96_000 * 2 + 65_536
    assert request.timeout_seconds == 8.0
    assert KOKORO_MODEL == "speaches-ai/Kokoro-82M-v1.0-ONNX"
    assert KOKORO_VOICE == "af_heart"
    assert KOKORO_SAMPLE_RATE == 24_000
    assert json.loads(request.body) == {
        "input": "On it!",
        "model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
        "response_format": "wav",
        "voice": "af_heart",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (b"not a wave", "invalid_wav"),
        (_wav(0), "invalid_wav"),
        (_wav(1_000)[:-2], "invalid_wav"),
        (_wav(1_000, sample_rate=16_000), "unexpected_sample_rate"),
        (_wav(1_000, channels=2), "unexpected_channel_count"),
        (_wav(1_000, sample_width=1), "unexpected_sample_width"),
    ),
)
async def test_invalid_or_substituted_audio_fails_closed(
    payload: bytes,
    reason: str,
) -> None:
    adapter = SpeachesTTS(
        transport=FakeTransport(HttpResponse(status=200, headers={}, body=payload)),
        api_key="secret",
    )

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.synthesize("Hello", max_duration_samples=96_000)

    assert caught.value.reason == reason
    assert "secret" not in str(caught.value)
    assert payload[:16].hex() not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", (36_000, 96_000))
async def test_exact_quantum_boundaries_are_accepted(ceiling: int) -> None:
    transport = FakeTransport(
        HttpResponse(
            status=200, headers={"content-type": "audio/wav"}, body=_wav(ceiling)
        )
    )

    audio = await SpeachesTTS(
        transport=transport,
        api_key="secret",
    ).synthesize("A bounded phrase", max_duration_samples=ceiling)

    assert audio.samples == ceiling
    assert len(audio.pcm_s16le) == ceiling * 2
    assert transport.requests[0].max_response_bytes == ceiling * 2 + 65_536


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", (0, 96_001))
async def test_command_ceiling_is_bounded_before_network(ceiling: int) -> None:
    transport = FakeTransport(HttpResponse(status=200, headers={}, body=_wav(1)))
    adapter = SpeachesTTS(transport=transport, api_key="secret")

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.synthesize("Hello", max_duration_samples=ceiling)

    assert caught.value.reason == "invalid_sample_ceiling"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_result_opening_and_general_quantum_overruns_publish_nothing() -> None:
    for ceiling in (36_000, 96_000):
        transport = FakeTransport(
            HttpResponse(status=200, headers={}, body=_wav(ceiling + 1))
        )
        adapter = SpeachesTTS(transport=transport, api_key="secret")

        with pytest.raises(SpeechAdapterError) as caught:
            await adapter.synthesize("A bounded phrase", max_duration_samples=ceiling)

        assert caught.value.reason == "audio_budget_exceeded"
        assert len(transport.requests) == 1


def test_worker_control_accepts_exact_opening_and_aggregate_reservation_caps() -> None:
    client = PoolClient(_config(), utcnow=lambda: NOW)
    opening = _speak_frame(
        turn_id=_uuid(22),
        kind="result",
        quantum_role="result_opening",
        quantum_index=0,
        max_duration_samples=36_000,
        result_reserved_samples_after=36_000,
    )
    continuation = _speak_frame(
        turn_id=_uuid(22),
        kind="result",
        quantum_role="result_continuation",
        quantum_index=31,
        max_duration_samples=96_000,
        result_reserved_samples_after=720_000,
    )

    assert client._validate_speak(opening) is None
    assert client._validate_speak(continuation) is None


@pytest.mark.parametrize(
    ("frame", "reason"),
    (
        (_speak_frame(max_duration_samples=96_001), "invalid_speak_sample_ceiling"),
        (
            _speak_frame(
                turn_id=_uuid(22),
                kind="result",
                quantum_role="result_opening",
                quantum_index=0,
                max_duration_samples=36_001,
                result_reserved_samples_after=36_001,
            ),
            "invalid_speak_quantum",
        ),
        (
            _speak_frame(
                turn_id=_uuid(22),
                kind="result",
                quantum_role="result_continuation",
                quantum_index=1,
                max_duration_samples=96_000,
                result_reserved_samples_after=720_001,
            ),
            "invalid_speak_quantum",
        ),
    ),
)
def test_worker_control_rejects_quantum_or_aggregate_over_budget(
    frame: dict[str, Any],
    reason: str,
) -> None:
    with pytest.raises(ProtocolViolation, match=reason):
        PoolClient(_config(), utcnow=lambda: NOW)._validate_speak(frame)


@pytest.mark.asyncio
async def test_aggregate_reservation_cap_is_echoed_in_manifest_and_lifecycle() -> None:
    room = FakeRoom()
    factory = FakeRtcFactory(room)
    notices: list[SessionNotice] = []
    session = _session(factory, tts=FakeTts(samples=960), notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    command = _speak(
        kind="result",
        turn_id=_uuid(22),
        announcement_id=_uuid(23),
    )
    command.update(
        quantum_role="result_continuation",
        quantum_index=31,
        max_duration_samples=96_000,
        result_reserved_samples_after=720_000,
    )

    session.deliver(command)
    await _wait_for(
        lambda: any(
            notice.kind == "speech_finished" and notice.announcement_id == _uuid(23)
            for notice in notices
        )
    )

    manifest = json.loads(room.local_participant.published_data[0]["payload"])
    assert manifest["result_reserved_samples_after"] == 720_000
    assert manifest["quantum_role"] == "result_continuation"
    assert manifest["quantum_index"] == 31
    lifecycle = [
        notice
        for notice in notices
        if notice.kind in {"speech_started", "speech_finished"}
    ]
    assert [
        notice.metadata["result_reserved_samples_after"] for notice in lifecycle
    ] == [
        720_000,
        720_000,
    ]

    await session.close("test")
    await task


def _failure_scenario(
    stage: str,
) -> tuple[FakeRoom, FakeRtcFactory, Any, str, bool]:
    room = FakeRoom()
    if stage == "synthesis":
        tts: Any = SpeachesTTS(
            transport=FakeTransport(
                HttpResponse(
                    status=200,
                    headers={"content-type": "audio/wav"},
                    body=b"not a wave",
                )
            ),
            api_key="secret",
        )
        return room, FakeRtcFactory(room), tts, "tts_failed", False
    if stage == "profile":
        tts = StaticAudioTts(SynthesizedAudio(b"\0\0" * 10, 16_000, 1, 2, 10))
        return room, FakeRtcFactory(room), tts, "invalid_synthesized_audio", False
    if stage == "pcm_shape":
        tts = StaticAudioTts(SynthesizedAudio(b"\0\0" * 9, 24_000, 1, 2, 10))
        return room, FakeRtcFactory(room), tts, "invalid_synthesized_audio", False
    if stage == "budget":
        return (
            room,
            FakeRtcFactory(room),
            FakeTts(samples=96_001),
            "invalid_synthesized_audio",
            False,
        )
    if stage == "publish":
        room.local_participant.publish_error = RuntimeError(
            "track provider detail must stay redacted"
        )
        return room, FakeRtcFactory(room), FakeTts(), "output_publish_failed", True
    if stage == "manifest":
        room.local_participant = ManifestFailingLocalParticipant()
        return (
            room,
            FakeRtcFactory(room),
            FakeTts(),
            "announcement_manifest_publish_failed",
            True,
        )
    raise AssertionError("unknown test stage")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ("synthesis", "profile", "pcm_shape", "budget", "publish", "manifest"),
)
async def test_synthesis_profile_budget_and_publication_errors_emit_no_audio(
    stage: str,
) -> None:
    room, factory, tts, expected_reason, track_attempted = _failure_scenario(stage)
    notices: list[SessionNotice] = []
    session = _session(factory, tts=tts, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()

    session.deliver(_speak(announcement_id=_uuid(40)))
    await _wait_for(
        lambda: any(
            notice.kind == "speech_failed" and notice.reason == expected_reason
            for notice in notices
        )
    )

    failure = next(notice for notice in notices if notice.kind == "speech_failed")
    assert failure.text is None
    assert "provider detail" not in repr(notices)
    assert not any(notice.kind == "speech_started" for notice in notices)
    assert room.local_participant.published_data == []
    assert bool(room.local_participant.published) is track_attempted
    assert all(source.frames == [] for source in factory.sources)
    if stage == "manifest":
        assert room.local_participant.manifest_attempts == 1
        assert room.local_participant.unpublished == ["TR_output_1"]
        assert factory.sources[0].close_calls == 1
    elif stage == "publish":
        assert room.local_participant.unpublished == []
        assert factory.sources[0].close_calls == 1
    else:
        assert factory.sources == []

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_interruption_during_result_opening_suppresses_all_publication() -> None:
    room = FakeRoom()
    factory = FakeRtcFactory(room)
    tts = BlockingTts()
    notices: list[SessionNotice] = []
    session = _session(factory, tts=tts, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    command = _speak(
        kind="result",
        turn_id=_uuid(22),
        announcement_id=_uuid(24),
    )
    command.update(
        quantum_role="result_opening",
        quantum_index=0,
        max_duration_samples=36_000,
        result_reserved_samples_after=36_000,
    )
    session.deliver(command)
    await tts.entered.wait()

    session.deliver(
        {
            "type": "stop_speech",
            "session_id": _uuid(1),
            "generation": 1,
            "media_grant_revision": 1,
            "announcement_id": _uuid(24),
            "reason": "user_stop",
        }
    )
    await tts.cancelled.wait()
    await _wait_for(
        lambda: any(
            notice.kind == "speech_interrupted" and notice.announcement_id == _uuid(24)
            for notice in notices
        )
    )

    assert tts.calls == [("Hello. What can I help with?", 36_000)]
    assert room.local_participant.published == []
    assert room.local_participant.published_data == []
    assert factory.sources == []

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_upstream_error_is_typed_and_body_is_never_disclosed() -> None:
    upstream_body = b'{"error":"provider internals and secret-token"}'
    adapter = SpeachesTTS(
        transport=FakeTransport(
            HttpResponse(status=503, headers={"x-debug": "private"}, body=upstream_body)
        ),
        api_key="secret-token",
    )

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.synthesize("Hello", max_duration_samples=96_000)

    assert caught.value.reason == "upstream_unavailable"
    rendered = str(caught.value)
    assert "provider internals" not in rendered
    assert "secret-token" not in rendered
    assert "private" not in rendered


@pytest.mark.asyncio
async def test_one_retryable_transport_stall_is_retried_within_cadence_bound() -> None:
    calls = 0

    class RetryableTimeout(RuntimeError):
        reason = "total_timeout"
        retryable = True

    async def flaky(_request: HttpRequest) -> HttpResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RetryableTimeout("provider detail must not escape")
        return HttpResponse(status=200, headers={}, body=_wav(1_000))

    transport = FakeTransport(flaky)
    audio = await SpeachesTTS(
        transport=transport,
        api_key="secret",
    ).synthesize("On it!", max_duration_samples=96_000)

    assert audio.samples == 1_000
    assert len(transport.requests) == 2
    assert sum(request.timeout_seconds for request in transport.requests) == 16.0


@pytest.mark.asyncio
async def test_non_retryable_transport_failure_is_redacted_and_not_retried() -> None:
    class UnsafeFailure(RuntimeError):
        reason = "secret_bearing_provider_reason"
        retryable = False

    async def fail(_request: HttpRequest) -> HttpResponse:
        raise UnsafeFailure("private provider body")

    transport = FakeTransport(fail)
    with pytest.raises(SpeechAdapterError) as caught:
        await SpeachesTTS(
            transport=transport,
            api_key="secret",
        ).synthesize("On it!", max_duration_samples=96_000)

    assert caught.value.reason == "transport_failed"
    assert len(transport.requests) == 1
    assert "provider" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ("", "   ", "x" * 4_097))
async def test_invalid_text_never_reaches_speech_service(text: str) -> None:
    transport = FakeTransport(HttpResponse(status=200, headers={}, body=_wav(1)))
    adapter = SpeachesTTS(transport=transport, api_key="secret")

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.synthesize(text, max_duration_samples=96_000)

    assert caught.value.reason == "invalid_text"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_missing_credential_fails_before_network() -> None:
    transport = FakeTransport(HttpResponse(status=200, headers={}, body=_wav(1)))

    with pytest.raises(SpeechAdapterError) as caught:
        SpeachesTTS(transport=transport, api_key="")

    assert caught.value.reason == "missing_credential"
    assert transport.requests == []
