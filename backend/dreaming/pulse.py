"""Deterministic helpers riding on the dreaming sweep: build_digest turns sweep signals
into the Pulse card-grid chrome surface, and propose_schedule parses conversational
scheduling asks into a proposal the user must confirm.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def pulse_enabled() -> bool:
    return os.getenv("FF_PULSE_DIGEST", "false").strip().lower() in ("1", "true", "yes", "on")


def build_digest(items: List[Dict[str, Any]], *, max_cards: int = 6) -> List[Dict[str, Any]]:
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        cat = str(it.get("category", "general")).strip() or "general"
        by_cat.setdefault(cat, []).append(it)

    def cat_salience(entries: List[Dict[str, Any]]) -> float:
        return max((float(e.get("salience", 0.0) or 0.0) for e in entries), default=0.0)

    cards: List[Dict[str, Any]] = []
    for cat in sorted(by_cat, key=lambda c: (-cat_salience(by_cat[c]), c)):
        entries = sorted(by_cat[cat],
                         key=lambda e: -float(e.get("salience", 0.0) or 0.0))
        lines: List[str] = []
        seen = set()
        for e in entries:
            text = str(e.get("title") or e.get("value") or "").strip()
            norm = text.lower()
            if text and norm not in seen:
                seen.add(norm)
                lines.append(text)
        if not lines:
            continue
        cards.append({
            "type": "card",
            "title": cat.replace("_", " ").title(),
            "content": [{"type": "text", "content": "• " + line} for line in lines[:5]],
        })
        if len(cards) >= max_cards:
            break
    return cards


@dataclass(frozen=True)
class ScheduleProposal:
    cadence: str
    at: Optional[str]
    description: str
    confirm_needed: bool = True
    fields: Dict[str, Any] = field(default_factory=dict)


_CADENCE_PATTERNS = [
    ("weekday", re.compile(r"\b(every\s+weekday|weekdays|each\s+workday)\b", re.I)),
    ("weekly", re.compile(r"\b(every\s+week|weekly|every\s+(mon|tue|wed|thu|fri|sat|sun))", re.I)),
    ("daily", re.compile(r"\b(every\s+day|daily|each\s+(morning|evening|night)|every\s+morning)\b", re.I)),
    ("hourly", re.compile(r"\b(every\s+hour|hourly)\b", re.I)),
]
_TIME_OF_DAY = re.compile(r"\b(morning|noon|afternoon|evening|night|midnight)\b", re.I)
_CLOCK = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_WEEKDAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b", re.I)
_RELATIVE = re.compile(r"\bin\s+(\d+)\s*(min(?:ute)?s?|hours?|hrs?|days?)\b", re.I)


def propose_schedule(request: str) -> ScheduleProposal:
    r = request or ""
    cadence = "unknown"
    for name, pat in _CADENCE_PATTERNS:
        if pat.search(r):
            cadence = name
            break

    at: Optional[str] = None
    rel = _RELATIVE.search(r)
    if cadence == "unknown" and rel:
        cadence = "once"
        n, unit = rel.group(1), rel.group(2).lower()
        u = "m" if unit.startswith("min") else ("h" if unit.startswith(("hour", "hr")) else "d")
        at = f"+{n}{u}"
    else:
        clock = _CLOCK.search(r)
        wd = _WEEKDAY.search(r)
        tod = _TIME_OF_DAY.search(r)
        if clock:
            at = clock.group(0)
        elif cadence == "weekly" and wd:
            at = wd.group(0).lower()
        elif tod:
            at = tod.group(0).lower()

    return ScheduleProposal(
        cadence=cadence, at=at,
        description=r.strip()[:200],
        confirm_needed=True,
        fields={"cadence": cadence, "at": at},
    )


def is_schedulable(proposal: ScheduleProposal) -> bool:
    return proposal.cadence != "unknown"
