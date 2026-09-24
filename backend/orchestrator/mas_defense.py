"""Inter-agent message defenses for multi-agent flows: HMAC-signs and verifies hop
provenance, enforces a per-edge sender-recipient allow-list, and scans payloads for
injection or exfiltration directives. Used by turn_hooks.py and subtasks.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

_ATTACK_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("ignore previous",
     re.compile(r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|earlier|preceding|above)\b", re.I)),
    ("disregard instructions",
     re.compile(r"\bdisregard\s+(?:your|all|the|any|previous|prior|preceding)?\s*"
                r"(?:instructions?|prompts?|rules?|guidelines?|directions?)\b", re.I)),
    ("new instructions",
     re.compile(r"\bnew\s+instructions?\s*[:\-]", re.I)),
    ("you are now",
     re.compile(r"\byou\s+are\s+now\s+(?:a\s+|an\s+|in\s+)?"
                r"(?:dan\b|jailbroken|jailbreak|unrestricted|developer\s+mode|"
                r"admin(?:istrator)?\b|root\b|god\s+mode|do\s+anything\s+now)", re.I)),
    ("act as admin",
     re.compile(r"\bact\s+as\s+(?:an?\s+)?(?:admin(?:istrator)?|root|superuser|system|developer\s+mode)\b", re.I)),
    ("exfiltrate",
     re.compile(r"\bexfiltrat(?:e|es|ing|ion)\b", re.I)),
    ("system prompt",
     re.compile(r"\b(?:reveal|show|print|repeat|display|output|leak|share|expose|dump|"
                r"ignore|forget|reset|override|disclose)\b[^.\n]{0,40}?\bsystem\s+prompt\b", re.I)),
    ("reveal your",
     re.compile(r"\breveal\s+your\s+(?:system|instructions?|prompt|api|secret|password|"
                r"token|key|credentials?|config(?:uration)?)\b", re.I)),
    ("api_key",
     re.compile(r"\b(?:reveal|leak|send|exfiltrate|share|show|print|give|output|return|"
                r"dump|expose|steal|email|post|upload|your)\b[^.\n]{0,25}?\bapi[_\s-]?keys?\b", re.I)),
    ("database_url",
     re.compile(r"\b(?:reveal|leak|send|exfiltrate|share|show|print|give|output|return|"
                r"dump|expose|steal|email|post|upload)\b[^.\n]{0,25}?\bdatabase[_\s-]?url\b", re.I)),
    ("send to",
     re.compile(r"\bsend\b[^.\n]{0,40}?\bto\s+(?:https?://|[\w.+-]+@[\w.-]+|"
                r"the\s+(?:attacker|following\s+(?:url|address|email|endpoint)))", re.I)),
)


def mas_defense_enabled() -> bool:
    return os.getenv("FF_MAS_DEFENSE", "false").strip().lower() in ("1", "true", "yes", "on")


def _key() -> Optional[bytes]:
    raw = os.getenv("MAS_MESSAGE_KEY") or os.getenv("MEMORY_HMAC_KEY")
    return raw.encode("utf-8") if raw else None


def _payload_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def sign_message(sender: str, recipient: str, payload: Any) -> Optional[str]:
    key = _key()
    if not key:
        return None
    body = f"{sender}\x1f{recipient}\x1f{_payload_hash(payload)}"
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_message(sender: str, recipient: str, payload: Any,
                   signature: Optional[str]) -> Tuple[bool, str]:
    key = _key()
    if not key:
        return False, "unsigned"
    if not signature:
        return False, "missing signature"
    expected = sign_message(sender, recipient, payload)
    if expected is None or not hmac.compare_digest(expected, signature):
        return False, "bad signature"
    return True, "ok"


def edge_allowed(sender: str, recipient: str,
                 allowed_edges: Optional[Iterable[Tuple[str, str]]]) -> bool:
    if allowed_edges is None:
        return True
    edges = set((str(s), str(r)) for s, r in allowed_edges)
    return (sender, recipient) in edges or (sender, "*") in edges


@dataclass(frozen=True)
class ScanFinding:
    marker: str


def scan_message(payload: Any) -> List[ScanFinding]:
    text = (payload if isinstance(payload, str)
            else json.dumps(payload, default=str))
    out: List[ScanFinding] = []
    for label, pattern in _ATTACK_PATTERNS:
        if pattern.search(text):
            out.append(ScanFinding(label))
    return out


def is_safe_message(sender: str, recipient: str, payload: Any, signature: Optional[str],
                    *, allowed_edges: Optional[Iterable[Tuple[str, str]]] = None,
                    require_signature: bool = True) -> Tuple[bool, str]:
    if not edge_allowed(sender, recipient, allowed_edges):
        return False, f"edge {sender}->{recipient} not allowed"
    if require_signature:
        ok, reason = verify_message(sender, recipient, payload, signature)
        if not ok:
            return False, f"integrity: {reason}"
    findings = scan_message(payload)
    if findings:
        return False, f"attack markers: {', '.join(f.marker for f in findings[:3])}"
    return True, "ok"
