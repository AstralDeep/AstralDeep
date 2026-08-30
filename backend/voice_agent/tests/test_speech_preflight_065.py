"""Exact live model/audio startup preflight tests for Feature 065."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from copy import deepcopy

import pytest

import voice_agent.speech_adapters as speech_adapters
from voice_agent.speech_adapters import (
    ASR_MODEL,
    KOKORO_MODEL,
    KOKORO_SAMPLE_RATE,
    KOKORO_VOICE,
    HttpRequest,
    HttpResponse,
    SpeechPreflight,
    SpeechPreflightError,
)


def _wav(
    samples: int = 1_000,
    *,
    sample_rate: int = KOKORO_SAMPLE_RATE,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\0\0" * samples)
    return output.getvalue()


def _inventory() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {
                "id": KOKORO_MODEL,
                "task": "text-to-speech",
                "sample_rate": KOKORO_SAMPLE_RATE,
                "voices": [
                    {
                        "id": KOKORO_VOICE,
                        "name": KOKORO_VOICE,
                        "language": "en-us",
                        "gender": "female",
                    }
                ],
            },
            {
                "id": ASR_MODEL,
                "task": "automatic-speech-recognition",
                "language": ["en"],
            },
        ],
    }


class FakeTransport:
    def __init__(
        self,
        *,
        inventory: HttpResponse | BaseException | None = None,
        asr: HttpResponse | None = None,
        tts: HttpResponse | None = None,
    ) -> None:
        self.inventory = inventory or HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(_inventory()).encode(),
        )
        self.asr = asr or HttpResponse(
            status=200,
            headers={"content-type": "application/json"},
            body=b'{"text":"","language":"en"}',
        )
        self.tts = tts or HttpResponse(
            status=200,
            headers={"content-type": "audio/wav"},
            body=_wav(),
        )
        self.requests: list[tuple[str, HttpRequest]] = []

    async def get(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(("GET", request))
        if isinstance(self.inventory, BaseException):
            raise self.inventory
        return self.inventory

    async def post(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(("POST", request))
        return self.asr if request.path == "/audio/transcriptions" else self.tts


class BlockingTerminalTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.tts_requests = 0

    async def post(self, request: HttpRequest) -> HttpResponse:
        if request.path == "/audio/speech":
            self.tts_requests += 1
            if self.tts_requests > 1:
                await asyncio.Event().wait()
        return await super().post(request)


@pytest.mark.asyncio
async def test_preflight_proves_inventory_real_asr_and_af_heart_24khz_wav() -> None:
    transport = FakeTransport()

    result = await SpeechPreflight(
        transport=transport,
        api_key="speech-secret",
    ).run()

    assert result.asr_model == ASR_MODEL
    assert result.tts_model == KOKORO_MODEL
    assert result.voice == "af_heart"
    assert result.sample_rate_hz == 24_000
    assert [method for method, _request in transport.requests] == ["GET"] + [
        "POST"
    ] * 10
    inventory_request = transport.requests[0][1]
    assert inventory_request.path == "/models"
    assert inventory_request.body == b""
    assert inventory_request.max_response_bytes == 512 * 1024
    assert inventory_request.timeout_seconds == 5.0
    assert inventory_request.headers == {
        "accept": "application/json",
        "authorization": "Bearer speech-secret",
    }
    asr_request = transport.requests[1][1]
    assert asr_request.path == "/audio/transcriptions"
    assert ASR_MODEL.encode() in asr_request.body
    assert b"RIFF" in asr_request.body
    tts_requests = [
        request for _method, request in transport.requests if request.path == "/audio/speech"
    ]
    assert json.loads(tts_requests[0].body) == {
        "input": "On it!",
        "model": KOKORO_MODEL,
        "response_format": "wav",
        "voice": "af_heart",
    }
    assert tts_requests[0].max_response_bytes == 48_000 * 2 + 65_536
    assert {
        json.loads(request.body)["input"] for request in tts_requests[1:]
    } == {
        "Private result ready.",
        "Request failed.",
        "I can't help with that.",
        "Please try again later.",
        "Choose another chat.",
        "Please try that again.",
        "Please say that again.",
        "Request cancelled.",
    }
    assert all(
        request.max_response_bytes == 36_000 * 2 + 65_536
        for request in tts_requests[1:]
    )


@pytest.mark.asyncio
async def test_preflight_rejects_a_terminal_phrase_over_the_1_5_second_ceiling() -> None:
    transport = FakeTransport(
        tts=HttpResponse(
            status=200,
            headers={"content-type": "audio/wav"},
            body=_wav(36_001),
        )
    )

    with pytest.raises(SpeechPreflightError, match="tts_unavailable"):
        await SpeechPreflight(transport=transport, api_key="secret").run()

    tts_requests = [
        request for _method, request in transport.requests if request.path == "/audio/speech"
    ]
    assert len(tts_requests) == 2
    assert tts_requests[-1].max_response_bytes == 36_000 * 2 + 65_536


@pytest.mark.asyncio
async def test_terminal_phrase_preflight_has_one_aggregate_deadline(monkeypatch) -> None:
    monkeypatch.setattr(
        speech_adapters,
        "_SHORT_TERMINAL_PREFLIGHT_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    transport = BlockingTerminalTransport()

    with pytest.raises(SpeechPreflightError, match="tts_unavailable"):
        await asyncio.wait_for(
            SpeechPreflight(transport=transport, api_key="secret").run(),
            timeout=0.25,
        )

    assert transport.tts_requests == 2


def _inventory_response(value: object) -> HttpResponse:
    return HttpResponse(
        status=200,
        headers={"content-type": "application/json"},
        body=json.dumps(value).encode(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "reason"),
    (
        (lambda value: value["data"].pop(), "asr_unavailable"),
        (lambda value: value["data"].pop(0), "tts_unavailable"),
        (
            lambda value: value["data"][0].update(sample_rate=16_000),
            "tts_unavailable",
        ),
        (
            lambda value: value["data"][0].update(voices=[]),
            "voice_unavailable",
        ),
        (
            lambda value: value["data"].append(deepcopy(value["data"][1])),
            "asr_unavailable",
        ),
        (lambda value: value.update(object="other"), "model_inventory_invalid"),
    ),
)
async def test_inventory_drift_fails_before_audio_calls(mutate, reason: str) -> None:
    inventory = _inventory()
    mutate(inventory)
    transport = FakeTransport(inventory=_inventory_response(inventory))

    with pytest.raises(SpeechPreflightError) as caught:
        await SpeechPreflight(transport=transport, api_key="secret").run()

    assert caught.value.reason == reason
    assert [method for method, _request in transport.requests] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "reason"),
    (
        (
            FakeTransport(
                inventory=HttpResponse(
                    status=401,
                    headers={},
                    body=b'{"secret":"provider-body"}',
                )
            ),
            "credential_rejected",
        ),
        (
            FakeTransport(inventory=RuntimeError("secret endpoint detail")),
            "model_inventory_unavailable",
        ),
        (
            FakeTransport(
                inventory=HttpResponse(
                    status=200,
                    headers={"content-type": "text/html"},
                    body=b"not-json",
                )
            ),
            "model_inventory_invalid",
        ),
        (
            FakeTransport(
                asr=HttpResponse(status=503, headers={}, body=b"provider secret")
            ),
            "asr_unavailable",
        ),
        (
            FakeTransport(
                tts=HttpResponse(status=503, headers={}, body=b"provider secret")
            ),
            "tts_unavailable",
        ),
        (
            FakeTransport(
                tts=HttpResponse(
                    status=200,
                    headers={"content-type": "audio/wav"},
                    body=_wav(sample_rate=16_000),
                )
            ),
            "tts_unavailable",
        ),
    ),
)
async def test_preflight_failures_are_typed_and_content_free(
    transport: FakeTransport,
    reason: str,
) -> None:
    with pytest.raises(SpeechPreflightError) as caught:
        await SpeechPreflight(
            transport=transport,
            api_key="speech-secret",
        ).run()

    assert caught.value.reason == reason
    rendered = str(caught.value)
    assert "speech-secret" not in rendered
    assert "provider secret" not in rendered
    assert "endpoint detail" not in rendered


@pytest.mark.asyncio
async def test_duplicate_or_oversized_inventory_is_rejected() -> None:
    duplicate = (
        b'{"object":"list","data":[],"data":['
        + b'{"id":"'
        + ASR_MODEL.encode()
        + b'"}]}'
    )
    for body in (duplicate, b"{" + b"x" * (512 * 1024) + b"}"):
        transport = FakeTransport(
            inventory=HttpResponse(
                status=200,
                headers={"content-type": "application/json"},
                body=body,
            )
        )
        with pytest.raises(SpeechPreflightError) as caught:
            await SpeechPreflight(transport=transport, api_key="secret").run()
        assert caught.value.reason == "model_inventory_invalid"
        assert len(transport.requests) == 1


def test_preflight_requires_credential_without_rendering_it() -> None:
    with pytest.raises(SpeechPreflightError, match="missing_credential"):
        SpeechPreflight(transport=FakeTransport(), api_key=" ")
