"""Strict validation for the optional window-capture provenance descriptor accepted by
attachments/router.py's /api/upload and re-checked by orchestrator.py's attachment
preamble builder; refusal reasons feed the audit trail and HTTP 400 denials.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CAPTURE_SOURCE_KIND = "window_capture"

MAX_APP_BUNDLE_ID_CHARS = 256
MAX_WINDOW_TITLE_CHARS = 512
MAX_CAPTURED_AT_CHARS = 64
MAX_ACCESSIBILITY_TEXT_CHARS = 20_000
MAX_REFUSAL_REASON_CHARS = 200

_DESCRIPTOR_FIELDS = frozenset({
    "kind",
    "app_bundle_id",
    "window_title",
    "captured_at",
    "accessibility_text_truncated",
})


class CaptureSourceError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason[:MAX_REFUSAL_REASON_CHARS]


class WindowCaptureSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    app_bundle_id: str = Field(min_length=1, max_length=MAX_APP_BUNDLE_ID_CHARS)
    window_title: str = Field(max_length=MAX_WINDOW_TITLE_CHARS)
    captured_at: str = Field(min_length=1, max_length=MAX_CAPTURED_AT_CHARS)
    accessibility_text_truncated: bool

    @field_validator("kind")
    @classmethod
    def _kind_must_be_window_capture(cls, value: str) -> str:
        if value != CAPTURE_SOURCE_KIND:
            raise ValueError("unknown_kind")
        return value

    @field_validator("app_bundle_id", "window_title")
    @classmethod
    def _text_fields_must_be_clean(cls, value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("control_characters")
        return value

    @field_validator("captured_at")
    @classmethod
    def _captured_at_must_be_iso8601(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid_timestamp") from exc
        return value

    @field_validator("accessibility_text_truncated", mode="before")
    @classmethod
    def _flag_must_be_json_bool(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("invalid_flag")
        return value

    def captured_at_datetime(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except ValueError:
            return None


_VALIDATOR_REASONS = frozenset({
    "unknown_kind",
    "control_characters",
    "invalid_timestamp",
    "invalid_flag",
})


def _reason_from_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc", ()) if item != "__root__")
        kind = str(error.get("type", "invalid"))
        ctx = error.get("ctx")
        inner = ctx.get("error") if isinstance(ctx, dict) else None
        ctx_reason = str(inner) if inner is not None else ""
        if kind == "extra_forbidden":
            parts.append(f"unknown_field:{loc or '?'}")
        elif kind == "missing":
            parts.append(f"missing_field:{loc or '?'}")
        elif kind == "string_too_long":
            parts.append(f"field_too_long:{loc or '?'}")
        elif kind == "value_error" and ctx_reason in _VALIDATOR_REASONS:
            parts.append(ctx_reason)
        else:
            parts.append(f"invalid_field:{loc or '?'}")
    return "descriptor:" + ",".join(parts) if parts else "descriptor:invalid"


def validate_capture_source_payload(payload: Any) -> WindowCaptureSource:
    if not isinstance(payload, dict):
        raise CaptureSourceError("descriptor:not_an_object")
    unknown = sorted(str(key) for key in payload if key not in _DESCRIPTOR_FIELDS)
    if unknown:
        raise CaptureSourceError(f"descriptor:unknown_field:{unknown[0][:64]}")
    try:
        return WindowCaptureSource.model_validate(payload)
    except ValidationError as exc:
        raise CaptureSourceError(_reason_from_validation_error(exc)) from exc


def parse_capture_source(raw: Any) -> Optional[WindowCaptureSource]:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CaptureSourceError("descriptor:not_a_json_string")
    if not raw.strip():
        raise CaptureSourceError("descriptor:empty")
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CaptureSourceError("descriptor:invalid_json") from exc
    return validate_capture_source_payload(payload)


def capture_source_from_turn_entry(value: Any) -> Optional[WindowCaptureSource]:
    if value is None:
        return None
    try:
        return validate_capture_source_payload(value)
    except CaptureSourceError:
        return None


def accessibility_text_from_turn_entry(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if not value or len(value) > MAX_ACCESSIBILITY_TEXT_CHARS:
        return None
    return value


__all__ = [
    "CAPTURE_SOURCE_KIND",
    "CaptureSourceError",
    "MAX_ACCESSIBILITY_TEXT_CHARS",
    "MAX_APP_BUNDLE_ID_CHARS",
    "MAX_CAPTURED_AT_CHARS",
    "MAX_REFUSAL_REASON_CHARS",
    "MAX_WINDOW_TITLE_CHARS",
    "WindowCaptureSource",
    "accessibility_text_from_turn_entry",
    "capture_source_from_turn_entry",
    "parse_capture_source",
    "validate_capture_source_payload",
]
