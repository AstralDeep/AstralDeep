"""Immutable process-owned speech-backend selection for Feature 075.

Clients may report eligibility for the selected backend, but this module is
the sole place where deployment policy is interpreted.  Invalid explicit
values are deliberately represented without retaining the raw value so status
and logs cannot echo operator input.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class VoiceSpeechBackend(StrEnum):
    """The exhaustive server-owned speech backend vocabulary."""

    LLM_FACTORY = "llm_factory"
    CLIENT_LOCAL = "client_local"


@dataclass(frozen=True, slots=True)
class SpeechBackendSelection:
    """One parse-once, non-secret deployment selection."""

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
        """Parse the exact environment value once without aliases or trimming."""

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
    """Return one exact backend enum without accepting aliases."""

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
