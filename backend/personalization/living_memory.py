"""Deterministic, dependency-free cores for memory temporal validity, Ebbinghaus-style
forgetting, evolving persona, and provenance/unlearning classification; used by
memory_tools.py to gate recall, decay, and persona updates.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

def temporal_enabled() -> bool:
    return os.getenv("FF_MEMORY_TEMPORAL", "false").strip().lower() in ("1", "true", "yes", "on")


def forgetting_enabled() -> bool:
    return os.getenv("FF_MEMORY_FORGETTING", "false").strip().lower() in ("1", "true", "yes", "on")


def persona_enabled() -> bool:
    return os.getenv("FF_MEMORY_PERSONA", "false").strip().lower() in ("1", "true", "yes", "on")


_FAR_FUTURE = 1 << 62


def is_valid_at(memory: Dict[str, Any], ts: int) -> bool:
    vf = memory.get("valid_from")
    vt = memory.get("valid_to")
    lo = int(vf) if vf is not None else 0
    hi = int(vt) if vt is not None else _FAR_FUTURE
    return lo <= int(ts) < hi


def as_of(memories: List[Dict[str, Any]], ts: int) -> List[Dict[str, Any]]:
    return [m for m in (memories or []) if is_valid_at(m, ts)]


def detect_contradiction(memories: List[Dict[str, Any]],
                         ts: Optional[int] = None) -> List[Tuple[str, List[Dict[str, Any]]]]:
    live = as_of(memories, ts) if ts is not None else list(memories or [])
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for m in live:
        by_cat.setdefault(str(m.get("category", "")), []).append(m)
    out: List[Tuple[str, List[Dict[str, Any]]]] = []
    for cat, items in by_cat.items():
        values = {str(i.get("value", "")).strip().lower() for i in items}
        if len(values) > 1:
            out.append((cat, items))
    return out


def should_abstain(candidates: List[Dict[str, Any]], *,
                   min_salience: float = 0.0) -> bool:
    if not candidates:
        return False
    if detect_contradiction(candidates):
        return True
    return max((float(c.get("salience", 0.0) or 0.0) for c in candidates),
               default=0.0) < min_salience


_BASE_STABILITY_MS = 7 * 24 * 3600 * 1000


def retention_strength(memory: Dict[str, Any], now: int) -> float:
    last = memory.get("last_recalled_at")
    if last is None:
        last = memory.get("created_at")
    if last is None:
        last = now
    dt = max(0, int(now) - int(last))
    recalls = int(memory.get("recall_count", 0) or 0)
    salience = float(memory.get("salience", 0.0) or 0.0)
    stability = _BASE_STABILITY_MS * (1 + recalls) * (1.0 + max(0.0, salience))
    return math.exp(-dt / stability) if stability > 0 else 0.0


def reinforce(memory: Dict[str, Any], now: int) -> Dict[str, Any]:
    return {"recall_count": int(memory.get("recall_count", 0) or 0) + 1,
            "last_recalled_at": int(now)}


def should_forget(memory: Dict[str, Any], now: int, *, floor: float = 0.05) -> bool:
    if memory.get("source") == "explicit" or memory.get("pinned"):
        return False
    return retention_strength(memory, now) < floor


def safety_forget(memory: Dict[str, Any], *, phi_check=None) -> bool:
    if phi_check is None:
        return False
    try:
        return bool(phi_check(str(memory.get("value", ""))))
    except Exception:
        return True


@dataclass(frozen=True)
class PersonaCandidate:
    text: str
    score: float


def persona_score(persona: str, signals: List[str]) -> float:
    text = (persona or "").strip().lower()
    if not text:
        return 0.0
    sig = [s.strip().lower() for s in (signals or []) if s and s.strip()]
    if not sig:
        covered = 1.0
    else:
        covered = sum(1 for s in sig if s in text) / len(sig)
    length_penalty = min(0.3, len(text) / 4000.0)
    return round(covered - length_penalty, 6)


def evolve_persona(current: str, signals: List[str], *,
                   proposal: Optional[str] = None) -> PersonaCandidate:
    cur = PersonaCandidate(current or "", persona_score(current or "", signals))
    cand_text = proposal if proposal is not None else _append_uncovered(current or "", signals)
    cand = PersonaCandidate(cand_text, persona_score(cand_text, signals))
    return cand if cand.score > cur.score else cur


def _append_uncovered(persona: str, signals: List[str]) -> str:
    text = persona or ""
    low = text.lower()
    add = [s.strip() for s in (signals or []) if s and s.strip() and s.strip().lower() not in low]
    if not add:
        return text
    prefix = (text + " ") if text and not text.endswith((" ", "\n")) else text
    return (prefix + "Prefers: " + "; ".join(add) + ".").strip()


def apply_feedback(persona: str, signal: str, sentiment: str) -> str:
    s = (signal or "").strip()
    if not s:
        return persona or ""
    note = f"Likes: {s}." if str(sentiment).lower() in ("up", "positive", "1") else f"Avoid: {s}."
    base = (persona or "").strip()
    return (base + (" " if base else "") + note).strip()


def provenance_of(memory: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": memory.get("source", "explicit"),
        "category": memory.get("category"),
        "learned_at": memory.get("created_at"),
        "ingested_at": memory.get("ingested_at") or memory.get("created_at"),
        "last_recalled_at": memory.get("last_recalled_at"),
        "signed": bool(memory.get("signature")),
    }


def unlearn_kind(request: str) -> str:
    r = (request or "").strip().lower()
    erase_markers = ("forget", "delete", "erase", "remove", "wipe", "scrub",
                     "right to be forgotten", "gdpr")
    correct_markers = ("actually", "correction", "instead", "should be", "it's",
                       "no longer", "change it to", "update")
    if any(m in r for m in erase_markers) and not any(m in r for m in correct_markers):
        return "hard"
    return "supersede"
