"""Deterministic, committed-visible completion recaps for Feature 065.

This module never calls a model and never scrapes a client DOM.  Its fallback
accepts only the sanitized semantic component payload already committed for the
result, extracts an allowlisted visible subset, and keeps the spoken candidate
bounded.  Confidentiality remains fail-closed before synthesis.
"""

from __future__ import annotations

import asyncio
import html
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID


MAX_RECAP_WORDS = 80
SENSITIVE_NOTICE = "A sensitive result is ready. Open it on screen or ask me to read it."
RESULT_READY_NOTICE = "Your result is ready on screen."

_MAX_DEPTH = 12
_MAX_NODES = 512
_MAX_CANDIDATES = 256
_VISIBLE_FIELDS = (
    "status_text",
    "title",
    "heading",
    "summary",
    "conclusion",
    "result",
    "content",
    "text",
    "message",
    "description",
    "value",
    "label",
    "caption",
)
_CAVEAT_FIELDS = ("caveat", "caveats", "warning", "warnings", "limitation", "limitations")
_NEXT_FIELDS = (
    "next_action",
    "next_actions",
    "next_step",
    "next_steps",
    "recommendation",
    "recommendations",
)
_STRUCTURAL_FIELDS = (
    "children",
    "components",
    "items",
    "sections",
    "rows",
    "columns",
    "header",
    "body",
    "footer",
)
_BLOCKED_FIELDS = frozenset(
    {
        "raw_html",
        "html",
        "script",
        "scripts",
        "style",
        "styles",
        "tool",
        "tools",
        "tool_call",
        "tool_calls",
        "trace",
        "traces",
        "debug",
        "reasoning",
        "hidden_reasoning",
        "credential",
        "credentials",
        "secret",
        "secrets",
        "token",
        "tokens",
        "api_key",
        "authorization",
        "metadata",
        "telemetry",
        "progress",
        "intermediate",
    }
)
_NON_SPEAKABLE_COMPONENT_TYPES = frozenset(
    {"audio", "video", "image", "icon", "code", "html", "script", "style"}
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:\bbearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:sk-|gsk_|xai-)[A-Za-z0-9_-]{12,}|"
    r"-----BEGIN [A-Z ]+PRIVATE KEY-----|"
    r"\b(?:api[_ -]?key|secret|password|token)\s*[:=]\s*\S{8,})"
)
_SCRIPT_BLOCK = re.compile(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>")
_HTML_TAG = re.compile(r"(?s)<[^>]+>")
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\([^)]*\)")
_URL = re.compile(r"(?i)\b(?:https?|wss?)://\S+")
_WHITESPACE = re.compile(r"\s+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_SIGNIFICANT_CLAUSE = re.compile(
    r"(?i)\s+(?=(?:however|but|caveat|warning|limitation|next|recommendation|"
    r"recommended|action)\b)"
)
_CAVEAT_WORDS = re.compile(r"(?i)\b(?:but|however|caveat|warning|limitation|except|unless)\b")
_NEXT_WORDS = re.compile(r"(?i)\b(?:next|should|recommend|follow up|action)\b")

RecapSource = Literal[
    "authoritative_summary",
    "committed_visible_fallback",
    "sensitive_notice",
    "terminal_status",
]
OutputPolicy = Literal["full_recap", "english_lifecycle_only"]
Sensitivity = Literal["sensitive", "non_sensitive"]


class VoiceRecapError(RuntimeError):
    """A content-free recap/consent refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SpokenRecap:
    text: str
    source: RecapSource
    output_policy: OutputPolicy
    output_reason: str
    sensitivity: Sensitivity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("recap text is required")
        if len(self.text.split()) > MAX_RECAP_WORDS:
            raise ValueError("recap exceeds the word limit")


class CommittedVisibleTextExtractor:
    """Extract only allowlisted text from committed semantic UI payloads."""

    def extract(self, components: Sequence[Mapping[str, Any]] | None) -> str:
        if components is None:
            return ""
        if isinstance(components, (str, bytes, bytearray)) or not isinstance(
            components, Sequence
        ):
            raise ValueError("committed components must be a sequence")
        buckets: dict[str, list[str]] = {
            "primary": [],
            "caveat": [],
            "next": [],
        }
        state = {"nodes": 0}
        for component in components:
            if not isinstance(component, Mapping):
                raise ValueError("committed component must be an object")
            self._walk(component, buckets, state, depth=0, table_row=False)
        return _compose_bounded(buckets)

    def _walk(
        self,
        value: Any,
        buckets: dict[str, list[str]],
        state: dict[str, int],
        *,
        depth: int,
        table_row: bool,
    ) -> None:
        if depth > _MAX_DEPTH or state["nodes"] >= _MAX_NODES:
            return
        state["nodes"] += 1
        if isinstance(value, Mapping):
            if value.get("hidden") is True or value.get("visible") is False:
                return
            component_type = value.get("type")
            if (
                isinstance(component_type, str)
                and component_type.strip().lower() in _NON_SPEAKABLE_COMPONENT_TYPES
            ):
                return
            if table_row:
                for key, item in value.items():
                    normalized_key = str(key).strip().lower()
                    if normalized_key in _BLOCKED_FIELDS or normalized_key.startswith("_"):
                        continue
                    if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                        self._add(buckets["primary"], f"{key}: {item}")
            for field_name in _VISIBLE_FIELDS:
                if field_name in value:
                    self._collect_visible(value[field_name], buckets["primary"])
            for field_name in _CAVEAT_FIELDS:
                if field_name in value:
                    self._collect_visible(value[field_name], buckets["caveat"])
            for field_name in _NEXT_FIELDS:
                if field_name in value:
                    self._collect_visible(value[field_name], buckets["next"])
            for field_name in _STRUCTURAL_FIELDS:
                child = value.get(field_name)
                if child is None:
                    continue
                self._walk(
                    child,
                    buckets,
                    state,
                    depth=depth + 1,
                    table_row=field_name == "rows",
                )
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for item in value:
                self._walk(
                    item,
                    buckets,
                    state,
                    depth=depth + 1,
                    table_row=table_row,
                )

    @staticmethod
    def _collect_visible(value: Any, output: list[str]) -> None:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            CommittedVisibleTextExtractor._add(output, str(value))
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for item in value:
                if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                    CommittedVisibleTextExtractor._add(output, str(item))

    @staticmethod
    def _add(output: list[str], value: str) -> None:
        if sum(len(items) for items in (output,)) >= _MAX_CANDIDATES:
            return
        cleaned = sanitize_speakable_text(value)
        if cleaned and cleaned not in output:
            output.append(cleaned)


def build_spoken_recap(
    *,
    authoritative_summary: str | None,
    committed_components: Sequence[Mapping[str, Any]] | None,
    detected_language: str,
    extractor: CommittedVisibleTextExtractor | None = None,
) -> SpokenRecap:
    """Apply source precedence and the launch English-output posture."""

    language = detected_language.strip().lower() if isinstance(detected_language, str) else ""
    if language != "en" and not language.startswith("en-"):
        return SpokenRecap(
            text=RESULT_READY_NOTICE,
            source="terminal_status",
            output_policy="english_lifecycle_only",
            output_reason="output_language_unsupported",
        )
    summary = sanitize_speakable_text(authoritative_summary or "")
    if summary:
        return SpokenRecap(
            text=_cap_authoritative_summary(summary),
            source="authoritative_summary",
            output_policy="full_recap",
            output_reason="ready",
        )
    fallback = (extractor or CommittedVisibleTextExtractor()).extract(
        committed_components
    )
    if fallback:
        return SpokenRecap(
            text=fallback,
            source="committed_visible_fallback",
            output_policy="full_recap",
            output_reason="ready",
        )
    return SpokenRecap(
        text=RESULT_READY_NOTICE,
        source="terminal_status",
        output_policy="full_recap",
        output_reason="visible_text_unavailable",
    )


def apply_sensitivity_policy(
    recap: SpokenRecap,
    *,
    confidentiality: str,
    contains_phi: Callable[[str], bool],
    consent_granted: bool = False,
) -> SpokenRecap:
    """Fail closed on unknown/error and gate details behind fresh consent."""

    sensitive = confidentiality != "non_sensitive"
    if not sensitive:
        try:
            sensitive = bool(contains_phi(recap.text))
        except Exception:
            sensitive = True
    if sensitive and not consent_granted:
        return SpokenRecap(
            text=SENSITIVE_NOTICE,
            source="sensitive_notice",
            output_policy=recap.output_policy,
            output_reason="sensitive_consent_required",
            sensitivity="sensitive",
        )
    return replace(
        recap,
        sensitivity="sensitive" if sensitive else "non_sensitive",
    )


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
    """Bounded, one-use, memory-only sensitive recap staging."""

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
    """Resolve only exact state-bound English controls; ambiguity dispatches normally."""

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


def sanitize_speakable_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    if _SECRET_VALUE.search(normalized):
        return ""
    normalized = _SCRIPT_BLOCK.sub(" ", normalized)
    normalized = html.unescape(normalized)
    normalized = _MARKDOWN_IMAGE.sub(" ", normalized)
    normalized = _MARKDOWN_LINK.sub(r"\1", normalized)
    normalized = _URL.sub(" ", normalized)
    normalized = _HTML_TAG.sub(" ", normalized)
    normalized = normalized.replace("```", " ").replace("`", "")
    normalized = "".join(
        character
        for character in normalized
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    )
    return _WHITESPACE.sub(" ", normalized).strip()


def _compose_bounded(buckets: dict[str, list[str]]) -> str:
    primary = _dedupe(buckets["primary"])
    caveats = _dedupe(buckets["caveat"])
    next_actions = _dedupe(buckets["next"])
    if not primary and not caveats and not next_actions:
        return ""
    reserve_caveat = 14 if caveats else 0
    reserve_next = 14 if next_actions else 0
    primary_budget = max(20, MAX_RECAP_WORDS - reserve_caveat - reserve_next)
    parts = [
        _cap_words(". ".join(primary), primary_budget),
        _cap_words(". ".join(caveats), reserve_caveat),
        _cap_words(". ".join(next_actions), reserve_next),
    ]
    return _cap_words(". ".join(part for part in parts if part), MAX_RECAP_WORDS)


def _cap_authoritative_summary(text: str) -> str:
    sentences = [
        clause.strip()
        for sentence in _SENTENCE.split(text)
        for clause in _SIGNIFICANT_CLAUSE.split(sentence)
        if clause.strip()
    ]
    caveats = [item for item in sentences if _CAVEAT_WORDS.search(item)]
    next_actions = [item for item in sentences if _NEXT_WORDS.search(item)]
    primary = [item for item in sentences if item not in caveats and item not in next_actions]
    return _compose_bounded(
        {"primary": primary, "caveat": caveats, "next": next_actions}
    )


def _cap_words(text: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    words = text.split()
    if len(words) <= maximum:
        return text.strip(" .")
    return " ".join(words[:maximum]).rstrip(" ,;:") + "…"


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
    # Spoken controls are deliberately narrower than ordinary transcript text.
    # Reject non-ASCII input before compatibility normalization so confusable
    # full-width characters cannot become an exact privileged command.
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
        # Existing committed result identifiers are allowed to be bounded
        # opaque text; reject only whitespace/control-bearing values.
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
