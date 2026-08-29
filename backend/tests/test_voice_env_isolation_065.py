"""Feature 065 speech credentials stay inside the isolated media worker."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.voice_backend import SpeechBackendSelection, VoiceSpeechBackend
from voice_agent.config import ConfigError, WorkerConfig


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _example_environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", maxsplit=1)
        values[name] = value
    return values


def _worker_environment() -> dict[str, str]:
    return {
        "ASTRAL_ENV": "test",
        "ASTRAL_VOICE_CONTROL_URL": "wss://control.example.test/api/voice/worker-control",
        "VOICE_CONTROL_SECRET": "c" * 32,
        "VOICE_WORKER_IDENTITY": "voice-worker-test",
        "VOICE_WORKER_MAX_SESSIONS": "2",
        "VOICE_WORKER_CLOSURE_SHA256": "0" * 64,
        "VOICE_SPEECH_BASE_URL": "https://speech.example.test/v1",
        "VOICE_SPEECH_API_KEY": "speech-only-sentinel",
        # Compose deliberately leaves the legacy names present but empty.
        "OPENAI_BASE_URL": "",
        "OPENAI_API_KEY": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
    }


def test_example_selects_exact_remote_default_without_adding_local_authority() -> None:
    values = _example_environment()

    selection = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": values["VOICE_SPEECH_BACKEND"]}
    )

    assert selection.value is VoiceSpeechBackend.LLM_FACTORY
    assert selection.valid is True
    assert selection.source == "explicit"
    assert values["FF_CONVERSATIONAL_VOICE"] == "true"
    assert not any(
        name.startswith("CLIENT_LOCAL_")
        and (name.endswith("_URL") or name.endswith("_KEY"))
        for name in values
    )


def test_worker_accepts_only_explicit_worker_local_speech_authority() -> None:
    config = WorkerConfig.from_environ(_worker_environment())

    assert config.speech_base_url == "https://speech.example.test/v1"
    assert config.speech_api_key == "speech-only-sentinel"
    assert config.profile.asr_model == "Systran/faster-whisper-large-v3"
    assert config.profile.tts_model == "speaches-ai/Kokoro-82M-v1.0-ONNX"
    assert config.profile.voice == "af_heart"


@pytest.mark.parametrize("name", ("OPENAI_BASE_URL", "OPENAI_API_KEY"))
def test_worker_rejects_nonempty_ambient_openai_authority(name: str) -> None:
    environment = _worker_environment()
    environment[name] = "ambient-provider-sentinel"

    with pytest.raises(ConfigError, match="legacy_provider_environment"):
        WorkerConfig.from_environ(environment)


def test_worker_does_not_fall_back_when_worker_speech_authority_is_missing() -> None:
    environment = _worker_environment()
    environment["OPENAI_BASE_URL"] = "https://ambient.example.test/v1"
    environment["OPENAI_API_KEY"] = "ambient-provider-sentinel"
    del environment["VOICE_SPEECH_BASE_URL"]
    del environment["VOICE_SPEECH_API_KEY"]

    with pytest.raises(ConfigError, match="legacy_provider_environment"):
        WorkerConfig.from_environ(environment)
