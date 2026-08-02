"""Bounded exact-model batch ASR tests for Feature 065."""

from __future__ import annotations

import re

import pytest

from voice_agent.speech_adapters import (
    ASR_MODEL,
    HttpRequest,
    HttpResponse,
    SpeechAdapterError,
    SpeachesBatchSTT,
    SpeachesTTS,
)
from voice_agent.tests.fake_speech_service import (
    FakeResponse,
    StrictFakeSpeechService,
)
try:
    # The isolated image renames the reviewed shared source into this package.
    from voice_agent.streaming_egress import FixedOriginHttpTransport
except ModuleNotFoundError:  # Host-tree test layout.
    from shared.streaming_egress import FixedOriginHttpTransport


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.requests: list[HttpRequest] = []

    async def post(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.response


@pytest.mark.asyncio
async def test_batch_transcription_uses_exact_model_bearer_and_memory_wav() -> None:
    transport = FakeTransport(
        HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b'{"text":"Do the thing","language":"en"}',
        )
    )
    adapter = SpeachesBatchSTT(transport=transport, api_key="speech-secret")

    transcript = await adapter.transcribe_pcm16(b"\0\0" * 16_000)

    assert transcript.text == "Do the thing"
    assert transcript.language == "en"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.path == "/audio/transcriptions"
    assert request.headers["accept"] == "application/json"
    assert request.headers["authorization"] == "Bearer speech-secret"
    content_type = request.headers["content-type"]
    match = re.fullmatch(r"multipart/form-data; boundary=([a-z0-9-]+)", content_type)
    assert match
    boundary = match.group(1).encode("ascii")
    assert request.body.endswith(b"--" + boundary + b"--\r\n")
    assert b'name="model"\r\n\r\n' + ASR_MODEL.encode() + b"\r\n" in request.body
    assert b'name="response_format"\r\n\r\nverbose_json\r\n' in request.body
    assert b'name="file"; filename="utterance.wav"' in request.body
    assert b"Content-Type: audio/wav" in request.body
    assert b"RIFF" in request.body and b"WAVE" in request.body
    assert request.max_response_bytes == 65_536
    assert request.timeout_seconds == 15.0


@pytest.mark.asyncio
async def test_batch_transcription_retries_one_transient_5xx() -> None:
    class FlakyTransport:
        def __init__(self) -> None:
            self.requests: list[HttpRequest] = []

        async def post(self, request: HttpRequest) -> HttpResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                return HttpResponse(status=503, headers={}, body=b"private")
            return HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=b'{"text":"Done","language":"en"}',
            )

    transport = FlakyTransport()
    transcript = await SpeachesBatchSTT(
        transport=transport,
        api_key="speech-secret",
    ).transcribe_pcm16(b"\0\0" * 16_000)

    assert transcript.text == "Done"
    assert len(transport.requests) == 2
    assert sum(request.timeout_seconds for request in transport.requests) == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pcm", "reason"),
    (
        (b"", "empty_audio"),
        (b"\0", "invalid_pcm"),
        (b"\0\0" * (16_000 * 60 + 1), "audio_too_long"),
    ),
)
async def test_invalid_audio_never_reaches_upstream(pcm: bytes, reason: str) -> None:
    transport = FakeTransport(HttpResponse(status=200, headers={}, body=b'{"text":"x"}'))
    adapter = SpeachesBatchSTT(transport=transport, api_key="secret")

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.transcribe_pcm16(pcm)

    assert caught.value.reason == reason
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "reason"),
    (
        (b"not-json", "invalid_transcript_response"),
        (b"[]", "invalid_transcript_response"),
        (b'{"text":42}', "invalid_transcript_response"),
        (b'{"text":"   "}', "empty_transcript"),
        (b'{"text":"x"}', "invalid_language"),
        (b'{"text":"x","language":null}', "invalid_language"),
        (b'{"text":"x","language":"not_a_language"}', "invalid_language"),
        (b'{"text":"' + b"x" * 8_001 + b'"}', "transcript_too_large"),
    ),
)
async def test_malformed_or_unbounded_transcript_fails_closed(
    body: bytes,
    reason: str,
) -> None:
    adapter = SpeachesBatchSTT(
        transport=FakeTransport(HttpResponse(status=200, headers={}, body=body)),
        api_key="secret",
    )

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.transcribe_pcm16(b"\0\0" * 512)

    assert caught.value.reason == reason
    assert body[:32].decode("utf-8", errors="ignore") not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (401, "credential_rejected"),
        (404, "upstream_unavailable"),
        (429, "upstream_overloaded"),
        (503, "upstream_unavailable"),
    ),
)
async def test_asr_http_failures_are_typed_and_redacted(
    status: int,
    reason: str,
) -> None:
    adapter = SpeachesBatchSTT(
        transport=FakeTransport(
            HttpResponse(
                status=status,
                headers={"x-provider-debug": "secret"},
                body=b'{"error":"provider internals"}',
            )
        ),
        api_key="speech-secret",
    )

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.transcribe_pcm16(b"\0\0" * 512)

    assert caught.value.reason == reason
    rendered = str(caught.value)
    assert "provider internals" not in rendered
    assert "speech-secret" not in rendered


@pytest.mark.asyncio
async def test_asr_rejects_wrong_success_content_type() -> None:
    adapter = SpeachesBatchSTT(
        transport=FakeTransport(
            HttpResponse(status=200, headers={"content-type": "text/html"}, body=b"oops")
        ),
        api_key="secret",
    )

    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.transcribe_pcm16(b"\0\0" * 512)

    assert caught.value.reason == "unexpected_content_type"


def test_asr_missing_credential_fails_closed() -> None:
    with pytest.raises(SpeechAdapterError) as caught:
        SpeachesBatchSTT(
            transport=FakeTransport(HttpResponse(status=200, headers={}, body=b"{}")),
            api_key=" ",
        )

    assert caught.value.reason == "missing_credential"


@pytest.mark.asyncio
async def test_strict_fake_service_exercises_real_fixed_origin_asr_and_tts() -> None:
    async with StrictFakeSpeechService() as service:
        transport = FixedOriginHttpTransport(
            service.origin,
            allow_insecure_loopback_development=True,
        )
        transcript = await SpeachesBatchSTT(
            transport=transport,
            api_key=service.api_key,
        ).transcribe_pcm16(b"\0\0" * 320)
        audio = await SpeachesTTS(
            transport=transport,
            api_key=service.api_key,
        ).synthesize("On it!", max_duration_samples=48_000)

        assert transcript.language == "en"
        assert audio.sample_rate == 24_000
        assert audio.samples == 2_400
        asr, tts = service.requests
        assert asr.target == "/v1/audio/transcriptions"
        assert asr.headers["authorization"] == "Bearer speech-test-key"
        assert b'Systran/faster-whisper-large-v3' in asr.body
        assert b'name="file"; filename="utterance.wav"' in asr.body
        assert b"RIFF" in asr.body and b"WAVE" in asr.body
        assert tts.target == "/v1/audio/speech"
        assert tts.headers["authorization"] == "Bearer speech-test-key"
        assert b'speaches-ai/Kokoro-82M-v1.0-ONNX' in tts.body
        assert b'"voice":"af_heart"' in tts.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (401, "credential_rejected"),
        (404, "upstream_unavailable"),
        (429, "upstream_overloaded"),
        (503, "upstream_unavailable"),
    ),
)
async def test_strict_fake_service_statuses_are_redacted(
    status: int,
    reason: str,
) -> None:
    async with StrictFakeSpeechService() as service:
        service.enqueue(
            "/v1/audio/transcriptions",
            FakeResponse(
                status,
                b'{"error":"provider-private-body"}',
                "application/json",
            ),
        )
        # The adapter retries one transient 5xx, so both attempts receive the
        # same content-bearing failure and still expose only the typed reason.
        if status == 503:
            service.enqueue(
                "/v1/audio/transcriptions",
                FakeResponse(
                    status,
                    b'{"error":"provider-private-body"}',
                    "application/json",
                ),
            )
        transport = FixedOriginHttpTransport(
            service.origin,
            allow_insecure_loopback_development=True,
        )
        adapter = SpeachesBatchSTT(transport=transport, api_key=service.api_key)

        with pytest.raises(SpeechAdapterError) as caught:
            await adapter.transcribe_pcm16(b"\0\0" * 320)

        assert caught.value.reason == reason
        assert "provider-private-body" not in str(caught.value)
        assert service.api_key not in str(caught.value)
