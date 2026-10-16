"""Pattern-based HIPAA/PHI redactor (field-key heuristics plus SSN/email/phone/date/MRN
regex) the chat-step recorder runs before any tool argument or result is shown to the
user or persisted to chat history.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Tuple

logger = logging.getLogger("PHIRedactor")

TRUNCATION_LIMIT = 512
TRUNCATION_MARKER = "…"

PHI_FIELD_PATTERNS: Tuple[str, ...] = (
    "name",
    "dob",
    "birth",
    "birthdate",
    "ssn",
    "social_security",
    "mrn",
    "medical_record",
    "patient_id",
    "address",
    "street",
    "city",
    "zip",
    "postal",
    "phone",
    "telephone",
    "fax",
    "email",
    "ip_address",
    "url",
    "photo",
    "image_url",
    "biometric",
    "fingerprint",
    "voiceprint",
    "device_id",
    "device_serial",
    "vehicle_id",
    "license_plate",
    "license_number",
    "certificate",
    "account_number",
    "credit_card",
    "ccn",
    "card_number",
    "health_plan_id",
    "beneficiary",
    "insurance_id",
)

PHI_VALUE_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:ssn]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[REDACTED:email]"),
    (
        re.compile(r"\b(?:\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"),
        "[REDACTED:phone]",
    ),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED:ip]"),
    (re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b"), "[REDACTED:date]"),
    (re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/](?:19|20)\d{2}\b"), "[REDACTED:date]"),
    (re.compile(r"\bMRN[\s:#-]*\d{4,}\b", re.IGNORECASE), "[REDACTED:mrn]"),
)

SECRET_FIELD_PATTERNS: Tuple[str, ...] = (
    "api_key",
    "secret",
    "token",
    "password",
    "private_key",
    "access_token",
    "bearer",
)

SECRET_FIELD_REGEX: re.Pattern[str] = re.compile(
    r"(?:^|[_.-])(?:api[_.-]?key|password|passwd|pwd|private[_.-]?key|access[_.-]?token|refresh[_.-]?token|auth[_.-]?token|client[_.-]?secret|app[_.-]?secret|bearer(?:[_.-]?token)?)(?:$|[_.-])"
    r"|^(?:secret|token|api_key|apikey|password|passwd|pwd)$",
    re.IGNORECASE,
)

SECRET_EXCLUDED_KEYS: frozenset[str] = frozenset({
    "token_count",
    "tokens_count",
    "token_counts",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "tokens_used",
    "token_usage",
    "max_tokens",
    "secretion",
    "secretions",
    "secretin",
    "secretase",
    "secreting",
    "secretory",
})

SECRET_VALUE_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[a-zA-Z0-9_\-]{20,}\b"), "[REDACTED:api_key]"),
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{50,90})\b"),
        "[REDACTED:token]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws_key]"),
    (
        re.compile(r"\beyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\b"),
        "[REDACTED:jwt]",
    ),
    (
        re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+)?PRIVATE KEY-----"),
        "[REDACTED:private_key]",
    ),
    (re.compile(r"(?i)\bBearer\s+[a-zA-Z0-9_.\-]{25,}\b"), "[REDACTED:bearer_token]"),
)



def _matches_phi_field_key(key: str) -> bool:
    lowered = key.lower()
    return any(p in lowered for p in PHI_FIELD_PATTERNS)


def _matches_secret_field_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered in SECRET_EXCLUDED_KEYS:
        return False
    if any(suffix in lowered for suffix in ("_count", "tokens_used", "token_usage", "prompt_tokens", "completion_tokens")):
        return False
    if "secretion" in lowered or "secretin" in lowered or "secretase" in lowered:
        return False
    return bool(SECRET_FIELD_REGEX.search(lowered))


def _redact_string(s: str) -> Tuple[str, bool]:
    changed = False
    for pattern, replacement in PHI_VALUE_PATTERNS + SECRET_VALUE_PATTERNS:
        new_s = pattern.sub(replacement, s)
        if new_s != s:
            changed = True
            s = new_s
    return s, changed


def _mask_value(value: Any, marker: str = "[REDACTED:phi]") -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: marker for k in value}
    if isinstance(value, list):
        return [marker for _ in value]
    return marker


def _redact_walk(value: Any, redacted: list) -> Any:
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if _matches_phi_field_key(k):
                result[k] = _mask_value(v, "[REDACTED:phi]")
                redacted[0] = True
            elif _matches_secret_field_key(k):
                result[k] = _mask_value(v, "[REDACTED:secret]")
                redacted[0] = True
            else:
                result[k] = _redact_walk(v, redacted)
        return result
    if isinstance(value, list):
        return [_redact_walk(item, redacted) for item in value]
    if isinstance(value, str):
        new, changed = _redact_string(value)
        if changed:
            redacted[0] = True
        return new
    return value


def _truncate(s: str) -> Tuple[str, bool]:
    if len(s) <= TRUNCATION_LIMIT:
        return s, False
    cutoff = max(0, TRUNCATION_LIMIT - len(TRUNCATION_MARKER))
    return s[:cutoff] + TRUNCATION_MARKER, True


def redact(value: Any, *, kind: str = "args") -> Tuple[Optional[str], bool]:
    if value is None:
        return None, False
    try:
        flag = [False]
        scrubbed = _redact_walk(value, flag)
        if isinstance(scrubbed, str):
            serialised = scrubbed
        else:
            try:
                serialised = json.dumps(scrubbed, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                serialised = repr(scrubbed)
        truncated, was_truncated = _truncate(serialised)
        if flag[0]:
            logger.info(
                "phi_redactor.redaction_applied",
                extra={"kind": kind, "truncated": was_truncated},
            )
        return truncated, was_truncated
    # Never raises: caller must not crash or leak via traceback
    except Exception as exc:  # pragma: no cover
        logger.error(
            "phi_redactor.redaction_failed: %s",
            type(exc).__name__,
            extra={"kind": kind, "error": type(exc).__name__},
        )
        return "[redaction failed]", False
