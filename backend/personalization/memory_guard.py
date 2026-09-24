"""Memory-poisoning defense: regex and HMAC core that refuses
instruction-injection/exfiltration content before it reaches durable memory and flags
tampered rows at read time. Wired into memory_tools.py's write path and
repository.py's trust level.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any, Dict, Optional

_FIELD_SEP = "\x1f"

# Matches directive phrasing only, so real preferences pass
_POISON_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|earlier|above)\s+"
               r"(?:instructions?|prompts?|messages?|rules?)", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+|the\s+)?(?:previous|prior|above|system|earlier)",
               re.IGNORECASE),
    re.compile(r"forget\s+(?:everything|all\s+(?:previous|prior)|your\s+instructions?)",
               re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:a\s+|an\s+)?\w", re.IGNORECASE),
    re.compile(r"(?:new|updated|revised)\s+(?:system\s+)?(?:instructions?|rules?|"
               r"directives?|prompt)\s*[:=]", re.IGNORECASE),
    re.compile(r"system\s+prompt\s*[:=]", re.IGNORECASE),
    re.compile(r"(?:override|bypass|disable|turn\s+off)\s+(?:the\s+)?(?:safety|"
               r"security|guard|guardrail|filter|moderation)", re.IGNORECASE),
    re.compile(r"(?:always|whenever|every\s+time)\b.{0,60}?\b(?:reveal|leak|exfiltrate|"
               r"send|share|email|post|disclose)\b.{0,60}?\b(?:secret|password|passwd|"
               r"api[\s_-]?key|credential|token|private)", re.IGNORECASE),
]


def guard_enabled() -> bool:
    return os.getenv("FF_MEMORY_GUARD", "true").strip().lower() not in ("0", "false", "no", "off")


def is_poisoning_attempt(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return any(p.search(value) for p in _POISON_PATTERNS)


def _hmac_key() -> Optional[bytes]:
    raw = os.getenv("MEMORY_HMAC_KEY")
    return raw.encode("utf-8") if raw else None


def sign_fields(*fields: Any) -> Optional[str]:
    key = _hmac_key()
    if not key:
        return None
    msg = _FIELD_SEP.join("" if f is None else str(f) for f in fields).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_fields(signature: Optional[str], *fields: Any) -> bool:
    key = _hmac_key()
    if not key or not signature:
        return True
    expected = sign_fields(*fields)
    return bool(expected) and hmac.compare_digest(expected, str(signature))


def trust_of(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return "derived"
    sig = item.get("signature")
    if sig and not verify_fields(sig, item.get("id"), item.get("user_id"),
                                 item.get("category"), item.get("value"),
                                 item.get("source")):
        return "tampered"
    return "trusted" if item.get("source") == "explicit" else "derived"
