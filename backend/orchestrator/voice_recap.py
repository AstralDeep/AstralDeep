"""Builds deterministic spoken recaps from committed, sanitized UI text and gates
sensitive results behind fresh consent; never calls a model. Also resolves spoken
voice controls. Used by orchestrator.py and voice_bootstrap.py.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID


_MAX_DEPTH = 12
_MAX_NODES = 512


_WHITESPACE = re.compile(r"\s+")


class VoiceRecapError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SensitiveResultConsent:
    user_id: str
    result_id: str
    method: Literal["tap", "strict_spoken_control"]
    granted_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @classmethod
    def issue(
        cls,
        *,
        user_id: str,
        result_id: str,
        method: Literal["tap", "strict_spoken_control"],
        now: datetime,
        lifetime: timedelta = timedelta(minutes=2),
    ) -> "SensitiveResultConsent":
        checked_now = _aware(now)
        if not timedelta(seconds=1) <= lifetime <= timedelta(minutes=2):
            raise VoiceRecapError("invalid_consent_lifetime")
        return cls(
            user_id=_user(user_id),
            result_id=_result_id(result_id),
            method=method,
            granted_at=checked_now,
            expires_at=checked_now + lifetime,
        )

    def consume(
        self,
        *,
        user_id: str,
        result_id: str,
        now: datetime,
    ) -> "SensitiveResultConsent":
        checked_now = _aware(now)
        if self.consumed_at is not None:
            raise VoiceRecapError("consent_already_consumed")
        if self.user_id != _user(user_id) or self.result_id != _result_id(result_id):
            raise VoiceRecapError("consent_scope_mismatch")
        if checked_now < self.granted_at or checked_now >= self.expires_at:
            raise VoiceRecapError("consent_expired")
        return replace(self, consumed_at=checked_now)


@dataclass(slots=True, repr=False)
class _PendingSensitiveRecap:
    user_id: str
    session_id: str
    generation: int
    media_grant_revision: int
    turn_id: str
    result_id: str
    text: str = field(repr=False)
    expires_at: datetime

    def clear(self) -> None:
        self.text = ""

    def __repr__(self) -> str:
        return (
            "_PendingSensitiveRecap("
            f"session_id={self.session_id!r}, turn_id={self.turn_id!r}, "
            f"result_id={self.result_id!r}, text=<redacted>)"
        )


class SensitiveRecapRegistry:
    def __init__(self, *, capacity: int = 128) -> None:
        if not 1 <= capacity <= 1_024:
            raise ValueError("invalid_sensitive_recap_capacity")
        self._capacity = capacity
        self._entries: dict[tuple[str, str], _PendingSensitiveRecap] = {}
        self._lock = asyncio.Lock()

    @property
    def retained_count(self) -> int:
        return len(self._entries)

    async def remember(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        turn_id: str,
        result_id: str,
        text: str,
        now: datetime,
        lifetime: timedelta = timedelta(minutes=2),
    ) -> None:
        checked_now = _aware(now)
        if not timedelta(seconds=1) <= lifetime <= timedelta(minutes=2):
            raise VoiceRecapError("invalid_consent_lifetime")
        if not isinstance(text, str) or not text.strip() or len(text) > 4_096:
            raise VoiceRecapError("invalid_sensitive_recap")
        entry = _PendingSensitiveRecap(
            user_id=_user(user_id),
            session_id=_uuid_text(session_id, "invalid_session"),
            generation=_positive_int(generation, "invalid_generation"),
            media_grant_revision=_positive_int(
                media_grant_revision,
                "invalid_media_grant_revision",
            ),
            turn_id=_uuid_text(turn_id, "invalid_turn"),
            result_id=_result_id(result_id),
            text=text.strip(),
            expires_at=checked_now + lifetime,
        )
        key = (entry.user_id, entry.result_id)
        async with self._lock:
            self._prune(checked_now)
            existing = self._entries.get(key)
            if existing is None and len(self._entries) >= self._capacity:
                raise VoiceRecapError("sensitive_recap_capacity_exhausted")
            if existing is not None:
                existing.clear()
            self._entries[key] = entry

    async def consume(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        turn_id: str,
        result_id: str,
        now: datetime,
    ) -> str:
        checked_now = _aware(now)
        checked_user = _user(user_id)
        checked_result = _result_id(result_id)
        key = (checked_user, checked_result)
        async with self._lock:
            self._prune(checked_now)
            entry = self._entries.pop(key, None)
            if entry is None:
                raise VoiceRecapError("sensitive_consent_unavailable")
            expected = (
                _uuid_text(session_id, "invalid_session"),
                _positive_int(generation, "invalid_generation"),
                _positive_int(
                    media_grant_revision,
                    "invalid_media_grant_revision",
                ),
                _uuid_text(turn_id, "invalid_turn"),
            )
            actual = (
                entry.session_id,
                entry.generation,
                entry.media_grant_revision,
                entry.turn_id,
            )
            if actual != expected or checked_now >= entry.expires_at:
                entry.clear()
                raise VoiceRecapError("sensitive_consent_unavailable")
            text = entry.text
            entry.clear()
            return text

    async def clear(self) -> None:
        async with self._lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.clear()

    def _prune(self, now: datetime) -> None:
        for key, entry in tuple(self._entries.items()):
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                entry.clear()


@dataclass(frozen=True, slots=True)
class SpokenControlContext:
    pending_sensitive_result_id: str | None = None
    speech_active: bool = False
    voice_active: bool = False
    foreground_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSpokenControl:
    action: Literal["read_sensitive_result", "stop_speech", "mute_voice", "cancel_task"]
    target_id: str | None


_READ_PHRASES = frozenset({"read it", "read the result", "read that result", "read my result"})
_STOP_PHRASES = frozenset({"stop speaking", "stop talking", "stop voice"})
_MUTE_PHRASES = frozenset({"mute voice", "mute speech"})
_CANCEL_PHRASES = frozenset({"cancel request", "cancel the request", "cancel my request"})


def resolve_spoken_control(
    transcript: str,
    context: SpokenControlContext,
) -> ResolvedSpokenControl | None:
    phrase = _normalize_control(transcript)
    if phrase in _READ_PHRASES and context.pending_sensitive_result_id is not None:
        return ResolvedSpokenControl(
            "read_sensitive_result",
            _result_id(context.pending_sensitive_result_id),
        )
    if phrase in _STOP_PHRASES and context.speech_active:
        return ResolvedSpokenControl("stop_speech", None)
    if phrase in _MUTE_PHRASES and context.voice_active:
        return ResolvedSpokenControl("mute_voice", None)
    if phrase in _CANCEL_PHRASES and context.foreground_task_id:
        return ResolvedSpokenControl("cancel_task", context.foreground_task_id)
    return None


def _dedupe(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _normalize_control(value: str) -> str:
    if not isinstance(value, str):
        return ""
    # Reject before normalizing, or confusable chars slip through
    if not value.isascii():
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = _WHITESPACE.sub(" ", normalized)
    if normalized.endswith((".", "?", "!")):
        normalized = normalized[:-1].rstrip()
    return normalized


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise VoiceRecapError("invalid_time")
    return value.astimezone(UTC)


def _user(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise VoiceRecapError("invalid_user")
    return value.strip()


def _result_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise VoiceRecapError("invalid_result")
    try:
        parsed = UUID(value)
    except ValueError:
        if any(character.isspace() or ord(character) < 33 for character in value):
            raise VoiceRecapError("invalid_result") from None
        return value
    return str(parsed)


def _uuid_text(value: str, code: str) -> str:
    try:
        parsed = UUID(value, version=4)
    except (TypeError, ValueError, AttributeError):
        raise VoiceRecapError(code) from None
    if str(parsed) != value:
        raise VoiceRecapError(code)
    return value


def _positive_int(value: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VoiceRecapError(code)
    return value
