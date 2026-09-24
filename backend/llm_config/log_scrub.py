"""Defense-in-depth log scrubber for API keys: redact_llm_config cleans dicts/lists/JSON
strings/free text, and LLMKeyRedactionFilter attaches to loggers so every emitted
record is scrubbed regardless of source.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any


_REDACTED = "<redacted>"

TYPESAFE_KEY_PATTERN = re.compile(
    r"\b[a-z]{2,10}_(?=[a-z0-9_]{0,240}[0-9])[a-z0-9_]{40,}\b"
    r"|\b(?:ts|tsk|tsai)[-_](?:live[-_])?[A-Za-z0-9_\-]{20,}\b"
)

_KEY_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bor-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bsk_live_[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),
    TYPESAFE_KEY_PATTERN,
)


def _is_api_key_field(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    normalized = name.replace("-", "").replace("_", "").replace(".", "").lower()
    return normalized.endswith("apikey")


def _redact_text(text: str) -> str:
    for pat in _KEY_TOKEN_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


def redact_llm_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_api_key_field(k) else redact_llm_config(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_llm_config(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_llm_config(item) for item in value)
    if isinstance(value, str):
        if value and value[0] in "{[":
            try:
                parsed = json.loads(value)
                return json.dumps(redact_llm_config(parsed))
            except (ValueError, TypeError):
                pass
        return _redact_text(value)
    return value


class LLMKeyRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_llm_config(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_llm_config(a) for a in record.args)
        return True


def install_redaction_filter(logger_name: str = "") -> None:
    target = logging.getLogger(logger_name)
    for f in target.filters:
        if isinstance(f, LLMKeyRedactionFilter):
            return
    target.addFilter(LLMKeyRedactionFilter())
