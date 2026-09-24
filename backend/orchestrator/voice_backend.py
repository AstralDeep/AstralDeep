"""Parses and holds the deployment's speech-backend choice once at process start,
without retaining raw invalid input so status and logs can't echo it; voice_api.py
and voice_bootstrap.py treat this as the sole authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class VoiceSpeechBackend(StrEnum):
    LLM_FACTORY = "llm_factory"
    CLIENT_LOCAL = "client_local"


@dataclass(frozen=True, slots=True)
class SpeechBackendSelection:
    value: VoiceSpeechBackend | None
    valid: bool
    source: str

    def __post_init__(self) -> None:
        if self.source not in {"legacy_default", "explicit"}:
            raise ValueError("invalid_speech_backend_source")
        if self.valid != (self.value is not None):
            raise ValueError("invalid_speech_backend_selection")
        if self.source == "legacy_default" and (
            self.value is not VoiceSpeechBackend.LLM_FACTORY
        ):
            raise ValueError("invalid_speech_backend_default")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
    ) -> "SpeechBackendSelection":
        if "VOICE_SPEECH_BACKEND" not in environ:
            return cls(
                value=VoiceSpeechBackend.LLM_FACTORY,
                valid=True,
                source="legacy_default",
            )
        raw = environ.get("VOICE_SPEECH_BACKEND")
        try:
            backend = VoiceSpeechBackend(raw)
        except (TypeError, ValueError):
            return cls(value=None, valid=False, source="explicit")
        return cls(value=backend, valid=True, source="explicit")


def backend_value(value: object) -> VoiceSpeechBackend | None:
    if isinstance(value, VoiceSpeechBackend):
        return value
    try:
        return VoiceSpeechBackend(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "SpeechBackendSelection",
    "VoiceSpeechBackend",
    "backend_value",
]
