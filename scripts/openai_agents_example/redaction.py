"""Log and receipt redaction for the example: only bounded, allow-listed fields may
reach logs or qualification receipts, credential-shaped substrings are scrubbed, and
payload text never passes through. Used by client.py logging and qualify.py receipts
so external telemetry and sanitized logs stay free of generation, tool, audio, and
credential material.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

_CREDENTIAL_PATTERNS = (
    re.compile(r"afk_[A-Za-z0-9._=\-]{4,}"),
    re.compile(r"(?i)bearer[ \t]+[A-Za-z0-9._=\-]{4,}"),
    re.compile(r"(?i)authorization[\"':= ]+[A-Za-z0-9._=\-]{4,}"),
)
_REDACTED = "[redacted]"

ALLOWED_LOG_KEYS = frozenset({
    "event", "method", "phase", "outcome", "code", "status", "tool", "disposition",
    "revision", "operation_id", "count", "detail_code", "elapsed_ms",
})
_OPERATION_ID = re.compile(r"^[0-9a-fA-F-]{1,64}$")


def scrub_text(text: str) -> str:
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def _safe_value(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > 128:
            return _REDACTED
        return scrub_text(value)
    return _REDACTED


def sanitize_mapping(values: Mapping[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, value in values.items():
        if key not in ALLOWED_LOG_KEYS:
            continue
        if key == "operation_id" and isinstance(value, str) and _OPERATION_ID.fullmatch(value):
            sanitized[key] = value
            continue
        sanitized[key] = _safe_value(value)
    return sanitized


def sanitize_line(line: str) -> str:
    return scrub_text(line)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub_text(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(
                scrub_text(value) if isinstance(value, str) else value
                for value in record.args
            )
        return True


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    payload = sanitize_mapping({"event": event, **fields})
    logger.info("%s", payload)


__all__ = ["ALLOWED_LOG_KEYS", "RedactingFilter", "log_event", "sanitize_line", "sanitize_mapping", "scrub_text"]
