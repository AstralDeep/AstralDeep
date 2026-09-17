"""Log scrubber for the user's API key (feature 006-user-llm-config).

Defence-in-depth around FR-002 / SC-002. The application code already
takes pains to never log :class:`SessionCreds` (its ``__repr__`` elides
the key) and to never include ``api_key`` in audit-event payloads
(:func:`backend.llm_config.audit_events._assert_no_api_key`). This
scrubber catches the residual cases:

* A FastAPI/uvicorn access log that captures the request body of
  ``POST /api/llm/test`` (which carries ``api_key`` in plaintext —
  by design, since the probe needs it).
* A debug-level dump of a parsed WebSocket message via something
  like ``logger.debug("got msg: %s", payload)``.
* An exception's stringified arguments that happen to include a key.

The :func:`redact_llm_config` helper takes any dict / JSON-string /
loggable record and replaces ``api_key`` field values (and substrings
matching common API-key-shaped tokens) with the literal ``"<redacted>"``.
The :class:`LLMKeyRedactionFilter` is a :mod:`logging` ``Filter`` that
runs the scrubber over every log record's ``args`` and ``msg`` before
emission.

Feature 089 widened this in two ways. Key-name matching is no longer an
equality test against ``"api_key"``: any key that *ends* in ``api_key``
is redacted, so ``typesafe_api_key``, ``userApiKey`` and ``llm.api_key``
are all covered without a new rule per credential. And the token-shape
list gained a TypeSafe pattern, because the TypeSafe key travels the same
settings and probe paths the LLM key does.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any


_REDACTED = "<redacted>"

# TypeSafe System One key shape (feature 089, FR-035).
#
# Only the *pattern* is committed -- never a key and never a prefix sample. It
# was derived from the observed format of a real key under T003a, after a
# guessed prefix list was checked against one and did **not** match: a scrubber
# that misses the credential it exists for is worse than none, because it
# creates the belief that logs are safe.
#
# Two alternatives:
#
# 1. A short lowercase prefix, an underscore, then a long lowercase-alnum tail.
#    The tail must be at least 40 characters and must contain a digit. Both
#    bounds are there to keep ordinary snake_case out: without the digit gate,
#    identifiers like ``test_the_circuit_opens_after_three_consecutive_fallbacks``
#    redact themselves out of every debug line.
# 2. The shorter, hyphen- or underscore-separated vendor spellings, which the
#    synthetic test canary uses.
TYPESAFE_KEY_PATTERN = re.compile(
    r"\b[a-z]{2,10}_(?=[a-z0-9_]{0,240}[0-9])[a-z0-9_]{40,}\b"
    r"|\b(?:ts|tsk|tsai)[-_](?:live[-_])?[A-Za-z0-9_\-]{20,}\b"
)

# API-key-shaped tokens we redact wherever they appear in free-form text.
_KEY_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),  # OpenAI (also sk-ant-/sk-or-/sk-proj-)
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bor-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bsk_live_[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),  # Google API keys (Gemini)
    # TypeSafe System One (feature 089). The prefix set is deliberately wider
    # than one vendor spelling: a redaction that fires on a non-key is harmless,
    # a redaction that misses a key is not. Confirm the exact prefix against the
    # owner's real key before relying on the T059 canary scan.
    TYPESAFE_KEY_PATTERN,
)


def _is_api_key_field(name: Any) -> bool:
    """True for ``api_key`` and for any field name that ends in it.

    Matching on the suffix rather than the exact string is what makes one rule
    cover ``api_key``, ``typesafe_api_key``, ``userApiKey`` and ``llm.api_key``.
    Comparison is case-insensitive and ignores ``-``/``.``/``_`` separators, so a
    camelCase or dotted spelling cannot slip past a snake_case rule.
    """
    if not isinstance(name, str):
        return False
    normalized = name.replace("-", "").replace("_", "").replace(".", "").lower()
    return normalized.endswith("apikey")


def _redact_text(text: str) -> str:
    for pat in _KEY_TOKEN_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


def redact_llm_config(value: Any) -> Any:
    """Return ``value`` with every API-key field replaced by
    ``"<redacted>"`` and any API-key-shaped token in free text replaced
    similarly. Leaves the input shape otherwise unchanged.

    A field counts as an API key when its name ends in ``api_key`` under
    :func:`_is_api_key_field`, which covers the TypeSafe key alongside the
    LLM one.

    Handles ``dict``, ``list``, ``tuple``, ``str``, and JSON-serialized
    strings; all other types pass through unchanged. Recurses into
    nested structures.
    """
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
        # Attempt JSON-aware redaction first; fall back to text scan.
        if value and value[0] in "{[":
            try:
                parsed = json.loads(value)
                return json.dumps(redact_llm_config(parsed))
            except (ValueError, TypeError):
                pass
        return _redact_text(value)
    return value


class LLMKeyRedactionFilter(logging.Filter):
    """:mod:`logging` filter that scrubs API keys from every record.

    Install on the root logger (or on uvicorn / FastAPI loggers) at
    application startup so every log emission, regardless of source,
    passes through the redactor before reaching a handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Scrub the formatted message.
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        # Scrub each positional arg if it's a stringifiable structure.
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_llm_config(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_llm_config(a) for a in record.args)
        return True


def install_redaction_filter(logger_name: str = "") -> None:
    """Attach :class:`LLMKeyRedactionFilter` to ``logger_name`` (root by default).

    Idempotent: a second call has no effect if the filter is already attached.
    """
    target = logging.getLogger(logger_name)
    for f in target.filters:
        if isinstance(f, LLMKeyRedactionFilter):
            return
    target.addFilter(LLMKeyRedactionFilter())
