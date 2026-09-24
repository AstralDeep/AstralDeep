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


def _matches_phi_field_key(key: str) -> bool:
    lowered = key.lower()
    return any(p in lowered for p in PHI_FIELD_PATTERNS)


def _redact_string(s: str) -> Tuple[str, bool]:
    changed = False
    for pattern, replacement in PHI_VALUE_PATTERNS:
        new_s = pattern.sub(replacement, s)
        if new_s != s:
            changed = True
            s = new_s
    return s, changed


def _mask_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: "[REDACTED:phi]" for k in value}
    if isinstance(value, list):
        return ["[REDACTED:phi]" for _ in value]
    return "[REDACTED:phi]"


def _redact_walk(value: Any, redacted: list) -> Any:
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if _matches_phi_field_key(k):
                result[k] = _mask_value(v)
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
            "phi_redactor.redaction_failed",
            extra={"kind": kind, "error": str(exc)},
        )
        return "[redaction failed]", False
