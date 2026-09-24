"""Fixed-profile Kokoro TTS and Whisper ASR adapters for the voice worker, plus the
bounded FixedPhraseTTSCache; network access goes through an injected SpeechTransport,
and voice_agent/main.py wires them into session.py's DirectRtcSession.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import secrets
import wave
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


KOKORO_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX"
KOKORO_VOICE = "af_heart"
KOKORO_SAMPLE_RATE = 24_000
ASR_MODEL = "Systran/faster-whisper-large-v3"
ASR_SAMPLE_RATE = 16_000
MAX_SPEECH_TEXT_CHARS = 4_096
MAX_QUANTUM_SAMPLES = 96_000
MAX_ASR_SAMPLES = ASR_SAMPLE_RATE * 60
MAX_TRANSCRIPT_CHARS = 8_000
_WAV_CONTAINER_ALLOWANCE = 65_536
_TTS_TIMEOUT_SECONDS = 8.0
_TTS_ATTEMPTS = 2
_SHORT_TERMINAL_PREFLIGHT_TIMEOUT_SECONDS = 20.0
_ASR_TIMEOUT_SECONDS = 15.0
_ASR_ATTEMPTS = 2
_ASR_RESPONSE_BYTES = 65_536
_MODEL_INVENTORY_BYTES = 512 * 1024
_MODEL_INVENTORY_TIMEOUT_SECONDS = 5.0
_MAX_INVENTORY_MODELS = 256
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")


class SpeechAdapterError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"speech adapter failed: {reason}")


class SpeechPreflightError(SpeechAdapterError):
    pass


@dataclass(frozen=True, slots=True)
class HttpRequest:
    path: str
    headers: Mapping[str, str]
    body: bytes
    max_response_bytes: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class SpeechTransport(Protocol):
    async def post(self, request: HttpRequest) -> HttpResponse:
        pass

    async def get(self, request: HttpRequest) -> HttpResponse:
        pass


@dataclass(frozen=True, slots=True)
class SynthesizedAudio:
    pcm_s16le: bytes
    sample_rate: int
    channels: int
    sample_width_bytes: int
    samples: int


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str | None


@dataclass(frozen=True, slots=True)
class SpeechPreflightResult:
    asr_model: str = ASR_MODEL
    tts_model: str = KOKORO_MODEL
    voice: str = KOKORO_VOICE
    sample_rate_hz: int = KOKORO_SAMPLE_RATE


class SpeechPreflight:
    def __init__(self, *, transport: SpeechTransport, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise SpeechPreflightError("missing_credential")
        self._transport = transport
        self._api_key = api_key
        self._asr = SpeachesBatchSTT(transport=transport, api_key=api_key)
        self._tts = SpeachesTTS(transport=transport, api_key=api_key)

    async def run(self) -> SpeechPreflightResult:
        inventory = await self._model_inventory()
        _validate_exact_inventory(inventory)
        try:
            transcript = await self._asr.transcribe_pcm16(b"\0\0" * ASR_SAMPLE_RATE)
        except SpeechAdapterError as exc:
            if exc.reason != "empty_transcript":
                raise SpeechPreflightError(
                    _component_failure(exc.reason, "asr_unavailable")
                ) from None
        else:
            del transcript
        try:
            audio = await self._tts.synthesize(
                "On it!",
                max_duration_samples=KOKORO_SAMPLE_RATE * 2,
            )
        except SpeechAdapterError as exc:
            raise SpeechPreflightError(
                _component_failure(exc.reason, "tts_unavailable")
            ) from None
        else:
            del audio
        try:
            async with asyncio.timeout(_SHORT_TERMINAL_PREFLIGHT_TIMEOUT_SECONDS):
                for text in SHORT_TERMINAL_PHRASE_TEXTS:
                    try:
                        audio = await self._tts.synthesize(
                            text,
                            max_duration_samples=SHORT_TERMINAL_SAMPLES,
                        )
                    except SpeechAdapterError as exc:
                        raise SpeechPreflightError(
                            _component_failure(exc.reason, "tts_unavailable")
                        ) from None
                    else:
                        # Zero-retention: drop startup-probe audio immediately
                        del audio
        except TimeoutError:
            raise SpeechPreflightError("tts_unavailable") from None
        return SpeechPreflightResult()

    async def _model_inventory(self) -> Any:
        request = HttpRequest(
            path="/models",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {self._api_key}",
            },
            body=b"",
            max_response_bytes=_MODEL_INVENTORY_BYTES,
            timeout_seconds=_MODEL_INVENTORY_TIMEOUT_SECONDS,
        )
        try:
            response = await self._transport.get(request)
        except Exception as exc:
            if isinstance(exc, SpeechPreflightError):
                raise
            raise SpeechPreflightError("model_inventory_unavailable") from None
        if response.status != 200:
            raise SpeechPreflightError(
                _component_failure(
                    _status_reason(response.status),
                    "model_inventory_unavailable",
                )
            )
        if len(response.body) > _MODEL_INVENTORY_BYTES:
            raise SpeechPreflightError("model_inventory_invalid")
        content_type = _content_type(response.headers)
        if content_type and content_type != "application/json":
            raise SpeechPreflightError("model_inventory_invalid")
        try:
            return json.loads(
                response.body,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=lambda _value: _invalid_json_number(),
            )
        except SpeechPreflightError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise SpeechPreflightError("model_inventory_invalid") from None


class SpeachesTTS:
    def __init__(self, *, transport: SpeechTransport, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise SpeechAdapterError("missing_credential")
        self._transport = transport
        self._api_key = api_key

    async def synthesize(
        self,
        text: str,
        *,
        max_duration_samples: int,
    ) -> SynthesizedAudio:
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_SPEECH_TEXT_CHARS
        ):
            raise SpeechAdapterError("invalid_text")
        if (
            isinstance(max_duration_samples, bool)
            or not isinstance(max_duration_samples, int)
            or not 1 <= max_duration_samples <= MAX_QUANTUM_SAMPLES
        ):
            raise SpeechAdapterError("invalid_sample_ceiling")

        body = json.dumps(
            {
                "input": text,
                "model": KOKORO_MODEL,
                "response_format": "wav",
                "voice": KOKORO_VOICE,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        request = HttpRequest(
            path="/audio/speech",
            headers={
                "accept": "audio/wav",
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            body=body,
            max_response_bytes=(max_duration_samples * 2 + _WAV_CONTAINER_ALLOWANCE),
            timeout_seconds=_TTS_TIMEOUT_SECONDS,
        )
        response = await _post_speech_with_retry(
            self._transport,
            request,
            attempts=_TTS_ATTEMPTS,
        )
        if response.status != 200:
            raise SpeechAdapterError(_status_reason(response.status))
        if len(response.body) > max_duration_samples * 2 + _WAV_CONTAINER_ALLOWANCE:
            raise SpeechAdapterError("response_too_large")

        content_type = _content_type(response.headers)
        if content_type and content_type not in {
            "audio/wav",
            "audio/wave",
            "audio/x-wav",
            "application/octet-stream",
        }:
            raise SpeechAdapterError("unexpected_content_type")
        return _parse_kokoro_wav(response.body, max_duration_samples)


SERVER_OWNED_PHRASE_TEXTS = frozenset(
    {
        "Hi! I'm ready when you are.",
        "On it!",
        "I'm on it.",
        "Let me take care of that.",
        "I'm still working on that.",
        "I'm continuing with your request.",
        "I'm keeping at it.",
        "I need something from you before I can continue.",
        "Please sign in so I can continue.",
        "Please grant the requested permission so I can continue.",
        "Please review the approval request so I can continue.",
        "Please set up your AI provider in Settings so I can continue.",
        "Private result ready.",
        "Request failed.",
        "I can't help with that.",
        "Please try again later.",
        "Choose another chat.",
        "Please try that again.",
        "Please say that again.",
        "Request cancelled.",
    }
)
SHORT_TERMINAL_SAMPLES = 36_000
SHORT_TERMINAL_PHRASE_TEXTS = (
    "Private result ready.",
    "Request failed.",
    "I can't help with that.",
    "Please try again later.",
    "Choose another chat.",
    "Please try that again.",
    "Please say that again.",
    "Request cancelled.",
)
TTS_CACHE_MAX_ENTRIES = 32
TTS_CACHE_MAX_TEXT_CHARS = 200


class FixedPhraseTTSCache:
    def __init__(
        self,
        inner: Any,
        *,
        cacheable_texts: frozenset[str] = SERVER_OWNED_PHRASE_TEXTS,
        max_entries: int = TTS_CACHE_MAX_ENTRIES,
    ) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or not 1 <= max_entries <= TTS_CACHE_MAX_ENTRIES
        ):
            raise ValueError("invalid_tts_cache_size")
        self._inner = inner
        self._cacheable_texts = frozenset(cacheable_texts)
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str, str, int], SynthesizedAudio] = (
            OrderedDict()
        )

    def _cache_key(self, text: Any) -> tuple[str, str, str, int] | None:
        if (
            not isinstance(text, str)
            or len(text) > TTS_CACHE_MAX_TEXT_CHARS
            or text not in self._cacheable_texts
        ):
            return None
        return (text, KOKORO_MODEL, KOKORO_VOICE, KOKORO_SAMPLE_RATE)

    async def synthesize(
        self,
        text: str,
        *,
        max_duration_samples: int,
    ) -> SynthesizedAudio:
        key = self._cache_key(text)
        if key is not None:
            cached = self._entries.get(key)
            if cached is not None:
                # Cache hit must replicate the miss path's validation
                if (
                    isinstance(max_duration_samples, bool)
                    or not isinstance(max_duration_samples, int)
                    or not 1 <= max_duration_samples <= MAX_QUANTUM_SAMPLES
                ):
                    raise SpeechAdapterError("invalid_sample_ceiling")
                if cached.samples > max_duration_samples:
                    raise SpeechAdapterError("audio_budget_exceeded")
                self._entries.move_to_end(key)
                return cached
        audio = await self._inner.synthesize(
            text, max_duration_samples=max_duration_samples
        )
        if (
            key is not None
            and isinstance(audio, SynthesizedAudio)
            and audio.samples <= MAX_QUANTUM_SAMPLES
        ):
            self._entries[key] = audio
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return audio


class SpeachesBatchSTT:
    def __init__(self, *, transport: SpeechTransport, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise SpeechAdapterError("missing_credential")
        self._transport = transport
        self._api_key = api_key

    async def transcribe_pcm16(self, pcm_s16le: bytes) -> Transcript:
        if not isinstance(pcm_s16le, bytes) or len(pcm_s16le) % 2:
            raise SpeechAdapterError("invalid_pcm")
        if not pcm_s16le:
            raise SpeechAdapterError("empty_audio")
        if len(pcm_s16le) > MAX_ASR_SAMPLES * 2:
            raise SpeechAdapterError("audio_too_long")

        wav_payload = _pcm16_wav(pcm_s16le)
        boundary = _multipart_boundary(wav_payload)
        body = _asr_multipart(boundary, wav_payload)
        response = await _post_speech_with_retry(
            self._transport,
            HttpRequest(
                path="/audio/transcriptions",
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {self._api_key}",
                    "content-type": f"multipart/form-data; boundary={boundary}",
                },
                body=body,
                max_response_bytes=_ASR_RESPONSE_BYTES,
                timeout_seconds=_ASR_TIMEOUT_SECONDS,
            ),
            attempts=_ASR_ATTEMPTS,
        )
        if response.status != 200:
            raise SpeechAdapterError(_status_reason(response.status))
        if len(response.body) > _ASR_RESPONSE_BYTES:
            raise SpeechAdapterError("response_too_large")
        content_type = _content_type(response.headers)
        if content_type and content_type != "application/json":
            raise SpeechAdapterError("unexpected_content_type")

        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeechAdapterError("invalid_transcript_response") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise SpeechAdapterError("invalid_transcript_response")
        text = payload["text"]
        if not text.strip():
            raise SpeechAdapterError("empty_transcript")
        if len(text) > MAX_TRANSCRIPT_CHARS:
            raise SpeechAdapterError("transcript_too_large")

        language = payload.get("language")
        if not isinstance(language, str) or _LANGUAGE_TAG.fullmatch(language) is None:
            raise SpeechAdapterError("invalid_language")
        return Transcript(text=text, language=language)


def _validate_exact_inventory(value: Any) -> None:
    if not isinstance(value, dict) or value.get("object") != "list":
        raise SpeechPreflightError("model_inventory_invalid")
    models = value.get("data")
    if (
        not isinstance(models, list)
        or not 1 <= len(models) <= _MAX_INVENTORY_MODELS
        or any(not isinstance(model, dict) for model in models)
    ):
        raise SpeechPreflightError("model_inventory_invalid")
    asr = [model for model in models if model.get("id") == ASR_MODEL]
    if len(asr) != 1 or asr[0].get("task") != "automatic-speech-recognition":
        raise SpeechPreflightError("asr_unavailable")
    tts = [model for model in models if model.get("id") == KOKORO_MODEL]
    if (
        len(tts) != 1
        or tts[0].get("task") != "text-to-speech"
        or tts[0].get("sample_rate") != KOKORO_SAMPLE_RATE
    ):
        raise SpeechPreflightError("tts_unavailable")
    voices = tts[0].get("voices")
    if not isinstance(voices, list) or not any(
        isinstance(voice, dict)
        and voice.get("id") == KOKORO_VOICE
        and voice.get("name") == KOKORO_VOICE
        for voice in voices
    ):
        raise SpeechPreflightError("voice_unavailable")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SpeechPreflightError("model_inventory_invalid")
        result[key] = value
    return result


def _invalid_json_number() -> None:
    raise SpeechPreflightError("model_inventory_invalid")


def _component_failure(reason: str, fallback: str) -> str:
    if reason in {"credential_rejected", "upstream_overloaded"}:
        return reason
    return fallback


def _status_reason(status: int) -> str:
    if status in {401, 403}:
        return "credential_rejected"
    if status == 429:
        return "upstream_overloaded"
    return "upstream_unavailable"


async def _post_speech_with_retry(
    transport: SpeechTransport,
    request: HttpRequest,
    *,
    attempts: int,
) -> HttpResponse:
    for attempt in range(attempts):
        try:
            response = await transport.post(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retryable = getattr(exc, "retryable", False) is True
            if retryable and attempt + 1 < attempts:
                continue
            reason = getattr(exc, "reason", None)
            if reason not in {
                "connect_timeout",
                "dns_timeout",
                "read_timeout",
                "tls_handshake_timeout",
                "total_timeout",
                "write_timeout",
            }:
                reason = "transport_failed"
            raise SpeechAdapterError(reason) from None
        if response.status in {500, 502, 503, 504} and attempt + 1 < attempts:
            continue
        return response
    raise SpeechAdapterError("transport_failed")


def _content_type(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return ""


def _pcm16_wav(pcm_s16le: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(ASR_SAMPLE_RATE)
        writer.writeframes(pcm_s16le)
    return output.getvalue()


def _multipart_boundary(wav_payload: bytes) -> str:
    for _ in range(4):
        boundary = f"astraldeep-{secrets.token_hex(16)}"
        if boundary.encode("ascii") not in wav_payload:
            return boundary
    raise SpeechAdapterError("multipart_boundary_failure")


def _asr_multipart(boundary: str, wav_payload: bytes) -> bytes:
    marker = boundary.encode("ascii")

    def field(name: str, value: str) -> bytes:
        return (
            b"--"
            + marker
            + b'\r\nContent-Disposition: form-data; name="'
            + name.encode("ascii")
            + b'"\r\n\r\n'
            + value.encode("utf-8")
            + b"\r\n"
        )

    return b"".join(
        (
            field("model", ASR_MODEL),
            # Needed for language field; plain json omits it
            field("response_format", "verbose_json"),
            b"--" + marker + b'\r\nContent-Disposition: form-data; name="file"; '
            b'filename="utterance.wav"\r\nContent-Type: audio/wav\r\n\r\n'
            + wav_payload
            + b"\r\n--"
            + marker
            + b"--\r\n",
        )
    )


def _parse_kokoro_wav(payload: bytes, max_duration_samples: int) -> SynthesizedAudio:
    try:
        with wave.open(io.BytesIO(payload), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            sample_count = reader.getnframes()
            compression = reader.getcomptype()
            if sample_count < 1:
                raise SpeechAdapterError("invalid_wav")
            if channels != 1:
                raise SpeechAdapterError("unexpected_channel_count")
            if sample_width != 2:
                raise SpeechAdapterError("unexpected_sample_width")
            if sample_rate != KOKORO_SAMPLE_RATE:
                raise SpeechAdapterError("unexpected_sample_rate")
            if compression != "NONE":
                raise SpeechAdapterError("unexpected_compression")
            if sample_count > max_duration_samples:
                raise SpeechAdapterError("audio_budget_exceeded")
            pcm = reader.readframes(sample_count + 1)
    except SpeechAdapterError:
        raise
    except (EOFError, OSError, wave.Error) as exc:
        raise SpeechAdapterError("invalid_wav") from exc

    expected_bytes = sample_count * channels * sample_width
    if len(pcm) != expected_bytes:
        raise SpeechAdapterError("invalid_wav")
    return SynthesizedAudio(
        pcm_s16le=bytes(pcm),
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        samples=sample_count,
    )
