"""Mixture-of-Agents / debate for hard turns — 033 Wave-2 (C-N9).

This module provides difficulty-gated panel logic: for high-stakes or
low-confidence turns, several candidate answers ("proposals") are combined
either by Mixture-of-Agents (MoA) aggregation or by pairwise
debate-then-judge into a single winning proposal.

The module is intentionally PURE and deterministic: there is no database,
no network, and no LLM access here. Any model interaction (e.g. scoring a
proposal or judging a debate) is supplied by the caller as an injected
callable, which keeps this logic trivially testable and provider-agnostic.
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
    """Panel trigger threshold, read PER CALL from ``MOA_DIFFICULTY_THRESHOLD``.

    Clamped to ``[0, 1]``; an unset or unparsable value falls back to
    :data:`DEFAULT_DIFFICULTY_THRESHOLD` so a typo can never open the gate
    for every turn.
    """
    raw = os.getenv("MOA_DIFFICULTY_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_DIFFICULTY_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DIFFICULTY_THRESHOLD
    if value != value:  # NaN
        return DEFAULT_DIFFICULTY_THRESHOLD
    return min(1.0, max(0.0, value))


# Request-side markers of a reasoning-heavy ask. Each distinct marker that
# appears adds a fixed step, capped — three markers already mean "hard".
_REASONING_MARKERS = (
    "why", "how does", "how do", "how would", "how should", "explain",
    "compare", "contrast", "analyze", "analyse", "analysis", "evaluate",
    "assess", "trade-off", "tradeoff", "trade off", "pros and cons",
    "advantages", "disadvantages", "versus", " vs ", " vs.", "justify",
    "argue", "critique", "implications", "in depth", "in-depth", "prove",
    "derive", "step by step", "step-by-step", "which is better",
    "should i", "should we", "recommend", "reason", "design",
)
# Multi-part asks: enumerations, explicit "also/then/additionally" chaining.
_MULTIPART_RE = re.compile(
    r"(?:(?:^|\n)\s*(?:\d+[.)]|[-*•])\s)|\b(?:and also|also|then|additionally|"
    r"as well as|secondly|finally|furthermore)\b",
    re.IGNORECASE,
)

# Draft-side hedges. Reused from the model router when importable so the two
# capabilities agree on what "the model is unsure" looks like.
try:  # pragma: no cover — exercised whichever branch the import takes
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
    """Cheap, deterministic difficulty estimate of the USER'S REQUEST in [0, 1].

    Signals (additive, then clamped):

    * length — ≥15 words ``+0.10``, ≥40 words ``+0.20``;
    * question marks — one ``+0.05``, two or more ``+0.15``;
    * reasoning markers (why / compare / analyze / trade-off / …) —
      ``+0.15`` each distinct marker, capped at ``+0.45``;
    * multi-part asks (enumerations, "also/then/additionally") ``+0.10``.

    A short factual question ("What's the capital of France?") scores well
    under the default threshold; a multi-part compare-and-justify ask scores
    over it.
    """
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
    """Uncertainty signal from the DRAFT ANSWER in [0, 1].

    ``+0.25`` when the draft hedges (any :data:`HEDGE_MARKERS` phrase) and
    ``+0.10`` for a very long (>1500 char) draft, which tends to mean the
    model was working through something. Deterministic, no model calls.
    """
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
    """Combined request + draft difficulty in [0, 1] — the panel's gate signal.

    Hedges alone never open the gate: a short hedged reply to a simple ask
    stays well under the default threshold, because the request term is what
    carries most of the weight.
    """
    return _clamp01(request_difficulty(request) + draft_uncertainty(draft))


def moa_enabled() -> bool:
    """Return whether the MoA/debate feature flag is enabled.

    Controlled by the ``FF_MOA_DEBATE`` environment variable. Truthy values
    are ``1``, ``true``, ``yes`` and ``on`` (case-insensitive, surrounding
    whitespace ignored). Anything else — including an unset variable — is
    treated as disabled (fail-closed).
    """
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
    """Decide whether the expensive panel should run for this turn.

    The panel is reserved for hard turns: it runs only when the turn is at
    least as hard as ``difficulty_threshold`` OR the current answer's
    confidence is at most ``confidence_threshold``. Easy, confident turns
    skip the panel and answer directly.

    Both ``difficulty`` and ``confidence`` are expected in ``[0, 1]``.

    Returns:
        ``True`` when the panel should be invoked, ``False`` otherwise.
    """
    return difficulty >= difficulty_threshold or confidence <= confidence_threshold


@dataclass(frozen=True)
class Proposal:
    """A single candidate answer produced by one agent.

    Attributes:
        agent: Identifier of the agent (or persona) that produced the text.
        text: The candidate answer.
        score: Quality/confidence score used by aggregation and as a
            debate fallback. Higher is better. Defaults to ``0.0``.
    """

    agent: str
    text: str
    score: float = 0.0


def aggregate(proposals: List[Proposal]) -> Proposal:
    """MoA aggregation: return the highest-scoring proposal.

    Ties on ``score`` are broken by order — the earliest proposal in the
    list wins.

    Args:
        proposals: Non-empty list of candidate proposals.

    Returns:
        The single winning proposal.

    Raises:
        ValueError: If ``proposals`` is empty.
    """
    if not proposals:
        raise ValueError("aggregate() requires at least one proposal")
    # max() is stable: on equal scores it keeps the first-seen item, which
    # gives the earliest-wins tie-break we want.
    return max(proposals, key=lambda p: p.score)


def majority_answer(
    proposals: List[Proposal],
    *,
    key: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    """Return the most common normalized answer text (majority vote).

    Each proposal's ``text`` is normalized via ``key`` before counting. The
    default normalizer strips surrounding whitespace and lower-cases the
    text. The returned value is the *normalized* form of the most frequent
    answer. Ties in frequency are broken by earliest appearance.

    Args:
        proposals: List of candidate proposals (may be empty).
        key: Optional normalizer applied to each ``text``. Defaults to
            ``lambda t: t.strip().lower()``.

    Returns:
        The winning normalized text, or ``None`` if ``proposals`` is empty.
    """
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

    # Highest count wins; tie-break on the earliest first-seen index.
    return min(
        counts,
        key=lambda value: (-counts[value], first_seen[value]),
    )


def debate_judge(
    a: Proposal,
    b: Proposal,
    judge: Callable[[Proposal, Proposal], int],
) -> Proposal:
    """Run a single pairwise debate between two proposals.

    The ``judge`` callable receives ``(a, b)`` and returns an integer:

    * ``-1`` — ``a`` wins,
    * ``1``  — ``b`` wins,
    * ``0``  — tie, resolved in favour of ``a``.

    If the judge raises any exception, the debate falls back to the
    higher-scoring proposal (ties resolved in favour of ``a``).

    Args:
        a: First proposal.
        b: Second proposal.
        judge: Callable deciding the winner.

    Returns:
        The winning proposal.
    """
    try:
        verdict = judge(a, b)
    except Exception:
        # Fallback: defer to score, with `a` winning ties.
        return a if a.score >= b.score else b

    if verdict > 0:
        return b
    # verdict < 0 (a wins) and verdict == 0 (tie -> a) both pick `a`.
    return a


def ranking_judge(ranking: Dict[str, int]) -> Callable[[Proposal, Proposal], int]:
    """Build a pairwise judge from ONE precomputed ranking (agent → rank, lower
    is better).

    This lets the caller pay for a single model judgement over the whole
    candidate set and still run it through :func:`panel`'s tournament. An
    agent missing from ``ranking`` raises ``KeyError`` inside the judge, which
    :func:`debate_judge` turns into its score-based fallback — so a partial
    or malformed ranking degrades to the scores the caller assigned rather
    than to an arbitrary winner.
    """
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
    """Reduce many proposals to a single winner.

    When a ``judge`` is supplied, run a single-elimination tournament: fold
    the proposals left-to-right, pitting the running winner against each
    next proposal via :func:`debate_judge`. Otherwise, fall back to MoA
    :func:`aggregate` (highest score wins).

    Args:
        proposals: Non-empty list of candidate proposals.
        judge: Optional pairwise judge callable (see :func:`debate_judge`).

    Returns:
        The single winning proposal.

    Raises:
        ValueError: If ``proposals`` is empty.
    """
    if not proposals:
        raise ValueError("panel() requires at least one proposal")

    if judge is None:
        return aggregate(proposals)

    return reduce(lambda winner, nxt: debate_judge(winner, nxt, judge), proposals)
