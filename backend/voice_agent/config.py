"""Fail-closed configuration for the isolated Feature 065 voice worker."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


ASR_MODEL = "Systran/faster-whisper-large-v3"
TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX"
TTS_VOICE = "af_heart"
OUTPUT_LOCALE = "en-US"
AUDIO_FORMAT = "wav"
SAMPLE_RATE_HZ = 24_000

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_WORKER_ENVIRONMENT = frozenset(
    {
        "ASTRAL_VOICE_CONTROL_URL",
        "VOICE_CONTROL_SECRET",
        "VOICE_SPEECH_API_KEY",
        "VOICE_SPEECH_BASE_URL",
        "VOICE_WORKER_CLOSURE_SHA256",
        "VOICE_WORKER_IDENTITY",
        "VOICE_WORKER_MAX_SESSIONS",
        "VOICE_WATCH_BRIDGE_LISTEN_HOST",
        "VOICE_WATCH_BRIDGE_LISTEN_PORT",
    }
)
_PROXY_ENVIRONMENT = frozenset(
    {
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    }
)


class ConfigError(ValueError):
    """A content-free startup configuration failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """The one server-owned launch profile supported by Feature 065."""

    asr_model: str = ASR_MODEL
    tts_model: str = TTS_MODEL
    voice: str = TTS_VOICE
    output_locale: str = OUTPUT_LOCALE
    format: str = AUDIO_FORMAT
    sample_rate_hz: int = SAMPLE_RATE_HZ

    def to_dict(self) -> dict[str, str | int]:
        """Return the contract representation without mutable shared state."""

        return {
            "asr_model": self.asr_model,
            "tts_model": self.tts_model,
            "voice": self.voice,
            "output_locale": self.output_locale,
            "format": self.format,
            "sample_rate_hz": self.sample_rate_hz,
        }


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Validated worker settings with credentials excluded from representations."""

    environment: str
    control_url: str = field(repr=False)
    control_secret: bytes = field(repr=False)
    worker_identity: str
    max_sessions: int
    runtime_closure_sha256: str
    watch_bridge_listen_host: str
    watch_bridge_listen_port: int
    speech_base_url: str = field(repr=False)
    speech_api_key: str = field(repr=False)
    profile: VoiceProfile = field(default_factory=VoiceProfile)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> WorkerConfig:
        """Read only the worker's explicit authority-bearing environment.

        Process-wide variables such as ``PATH`` remain usable, while unknown
        worker-prefixed variables and ambient provider, LiveKit API, or proxy
        authority fail startup. Empty compatibility variables are harmless.
        """

        values = os.environ if environ is None else environ
        _validate_environment_names(values)

        environment = values.get("ASTRAL_ENV", "production").strip().lower()
        if environment not in {"production", "staging", "development", "test"}:
            raise ConfigError("invalid_astral_environment")

        control_url = _required(values, "ASTRAL_VOICE_CONTROL_URL")
        speech_base_url = _required(values, "VOICE_SPEECH_BASE_URL")
        allow_insecure = environment in {"development", "test"}
        _validate_url(
            control_url,
            secure_scheme="wss",
            insecure_scheme="ws",
            allow_insecure=allow_insecure,
            error_prefix="control",
        )
        _validate_url(
            speech_base_url,
            secure_scheme="https",
            insecure_scheme="http",
            allow_insecure=allow_insecure,
            error_prefix="speech",
        )

        secret_text = _required(values, "VOICE_CONTROL_SECRET")
        secret = secret_text.encode("utf-8")
        if not 32 <= len(secret) <= 512:
            raise ConfigError("invalid_control_secret")

        speech_key = _required(values, "VOICE_SPEECH_API_KEY")
        if len(speech_key.encode("utf-8")) > 8_192:
            raise ConfigError("invalid_speech_credential")

        worker_identity = _required(values, "VOICE_WORKER_IDENTITY")
        if _OPAQUE_ID.fullmatch(worker_identity) is None:
            raise ConfigError("invalid_worker_identity")

        max_sessions_text = _required(values, "VOICE_WORKER_MAX_SESSIONS")
        try:
            max_sessions = int(max_sessions_text, 10)
        except ValueError as exc:
            raise ConfigError("invalid_max_sessions") from exc
        if str(max_sessions) != max_sessions_text or not 1 <= max_sessions <= 100:
            raise ConfigError("invalid_max_sessions")

        closure_digest = _required(values, "VOICE_WORKER_CLOSURE_SHA256")
        if _SHA256.fullmatch(closure_digest) is None or (
            closure_digest == "0" * 64 and environment not in {"development", "test"}
        ):
            raise ConfigError("invalid_closure_digest")

        watch_host = values.get("VOICE_WATCH_BRIDGE_LISTEN_HOST", "0.0.0.0")
        if (
            not isinstance(watch_host, str)
            or not watch_host
            or watch_host != watch_host.strip()
            or len(watch_host) > 255
            or "\x00" in watch_host
        ):
            raise ConfigError("invalid_watch_bridge_listen_host")
        watch_port_text = values.get("VOICE_WATCH_BRIDGE_LISTEN_PORT", "7890")
        try:
            watch_port = int(watch_port_text, 10)
        except (TypeError, ValueError) as exc:
            raise ConfigError("invalid_watch_bridge_listen_port") from exc
        if str(watch_port) != watch_port_text or not 1 <= watch_port <= 65535:
            raise ConfigError("invalid_watch_bridge_listen_port")

        return cls(
            environment=environment,
            control_url=control_url,
            control_secret=secret,
            worker_identity=worker_identity,
            max_sessions=max_sessions,
            runtime_closure_sha256=closure_digest,
            watch_bridge_listen_host=watch_host,
            watch_bridge_listen_port=watch_port,
            speech_base_url=speech_base_url.rstrip("/"),
            speech_api_key=speech_key,
        )


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("missing_" + name.lower())
    if value != value.strip() or "\x00" in value:
        raise ConfigError("invalid_" + name.lower())
    return value


def _validate_environment_names(values: Mapping[str, str]) -> None:
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ConfigError("invalid_environment")
        if not value:
            continue
        if name.startswith("OPENAI_"):
            raise ConfigError("legacy_provider_environment")
        if name.startswith("LIVEKIT_"):
            raise ConfigError("livekit_api_environment")
        if name in _PROXY_ENVIRONMENT:
            raise ConfigError("ambient_proxy_environment")
        if (
            name.startswith("VOICE_") or name.startswith("ASTRAL_VOICE_")
        ) and name not in _ALLOWED_WORKER_ENVIRONMENT:
            raise ConfigError("unknown_worker_environment")


def _validate_url(
    value: str,
    *,
    secure_scheme: str,
    insecure_scheme: str,
    allow_insecure: bool,
    error_prefix: str,
) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"invalid_{error_prefix}_url") from exc
    allowed_schemes = {secure_scheme}
    if allow_insecure:
        allowed_schemes.add(insecure_scheme)
    if parsed.scheme not in allowed_schemes:
        if parsed.scheme == insecure_scheme:
            raise ConfigError(f"insecure_{error_prefix}_url")
        raise ConfigError(f"invalid_{error_prefix}_url")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port == 0
    ):
        raise ConfigError(f"invalid_{error_prefix}_url")
