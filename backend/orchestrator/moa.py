"""Pure, deterministic Mixture-of-Agents and debate panel for hard turns: reduces
several candidate proposals to one winner by aggregation or pairwise judged debate,
gated by request/draft difficulty. Model calls are injected by turn_hooks.py.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import reduce
from typing import Callable, Dict, List, Optional

__all__ = [
    "Proposal",
    "moa_enabled",
    "should_invoke",
    "difficulty_threshold",
    "request_difficulty",
    "draft_uncertainty",
    "turn_difficulty",
    "aggregate",
    "majority_answer",
    "debate_judge",
    "ranking_judge",
    "panel",
]

DEFAULT_DIFFICULTY_THRESHOLD = 0.6


def difficulty_threshold() -> float:
    raw = os.getenv("MOA_DIFFICULTY_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_DIFFICULTY_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DIFFICULTY_THRESHOLD
    if value != value:
        return DEFAULT_DIFFICULTY_THRESHOLD
    return min(1.0, max(0.0, value))


_REASONING_MARKERS = (
    "why", "how does", "how do", "how would", "how should", "explain",
    "compare", "contrast", "analyze", "analyse", "analysis", "evaluate",
    "assess", "trade-off", "tradeoff", "trade off", "pros and cons",
    "advantages", "disadvantages", "versus", " vs ", " vs.", "justify",
    "argue", "critique", "implications", "in depth", "in-depth", "prove",
    "derive", "step by step", "step-by-step", "which is better",
    "should i", "should we", "recommend", "reason", "design",
)
_MULTIPART_RE = re.compile(
    r"(?:(?:^|\n)\s*(?:\d+[.)]|[-*•])\s)|\b(?:and also|also|then|additionally|"
    r"as well as|secondly|finally|furthermore)\b",
    re.IGNORECASE,
)

try:  # pragma: no cover
    from orchestrator.model_router import _HEDGE_MARKERS as HEDGE_MARKERS
except Exception:  # pragma: no cover
    HEDGE_MARKERS = (
        "i'm not sure", "i am not sure", "not certain", "cannot determine",
        "i cannot", "i can't", "unable to", "as an ai", "i don't have enough",
        "insufficient information", "it is unclear", "it's unclear",
    )


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def request_difficulty(request: str) -> float:
    text = (request or "").strip()
    if not text:
        return 0.0
    low = " " + text.lower() + " "
    score = 0.0
    words = len(text.split())
    if words >= 40:
        score += 0.20
    elif words >= 15:
        score += 0.10
    qmarks = text.count("?")
    if qmarks >= 2:
        score += 0.15
    elif qmarks == 1:
        score += 0.05
    hits = sum(1 for marker in _REASONING_MARKERS if marker in low)
    score += min(0.45, 0.15 * hits)
    if _MULTIPART_RE.search(text):
        score += 0.10
    return _clamp01(score)


def draft_uncertainty(draft: str) -> float:
    text = (draft or "")
    if not text.strip():
        return 0.0
    low = text.lower()
    score = 0.0
    if any(marker in low for marker in HEDGE_MARKERS):
        score += 0.25
    if len(text) > 1500:
        score += 0.10
    return _clamp01(score)


def turn_difficulty(request: str, draft: str = "") -> float:
    return _clamp01(request_difficulty(request) + draft_uncertainty(draft))


def moa_enabled() -> bool:
    return os.getenv("FF_MOA_DEBATE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def should_invoke(
    *,
    difficulty: float,
    confidence: float,
    difficulty_threshold: float = 0.6,
    confidence_threshold: float = 0.5,
) -> bool:
    return difficulty >= difficulty_threshold or confidence <= confidence_threshold


@dataclass(frozen=True)
class Proposal:
    agent: str
    text: str
    score: float = 0.0


def aggregate(proposals: List[Proposal]) -> Proposal:
    if not proposals:
        raise ValueError("aggregate() requires at least one proposal")
    return max(proposals, key=lambda p: p.score)


def majority_answer(
    proposals: List[Proposal],
    *,
    key: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    if not proposals:
        return None

    normalize = key if key is not None else (lambda t: t.strip().lower())

    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for index, proposal in enumerate(proposals):
        normalized = normalize(proposal.text)
        counts[normalized] = counts.get(normalized, 0) + 1
        if normalized not in first_seen:
            first_seen[normalized] = index

    return min(
        counts,
        key=lambda value: (-counts[value], first_seen[value]),
    )


def debate_judge(
    a: Proposal,
    b: Proposal,
    judge: Callable[[Proposal, Proposal], int],
) -> Proposal:
    try:
        verdict = judge(a, b)
    except Exception:
        return a if a.score >= b.score else b

    if verdict > 0:
        return b
    return a


def ranking_judge(ranking: Dict[str, int]) -> Callable[[Proposal, Proposal], int]:
    def judge(a: Proposal, b: Proposal) -> int:
        ra, rb = ranking[a.agent], ranking[b.agent]
        if ra < rb:
            return -1
        if rb < ra:
            return 1
        return 0
    return judge


def panel(
    proposals: List[Proposal],
    *,
    judge: Optional[Callable[[Proposal, Proposal], int]] = None,
) -> Proposal:
    if not proposals:
        raise ValueError("panel() requires at least one proposal")

    if judge is None:
        return aggregate(proposals)

    return reduce(lambda winner, nxt: debate_judge(winner, nxt, judge), proposals)
