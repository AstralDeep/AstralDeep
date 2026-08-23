"""Coordinator that wires the 033 flow-control / multi-agent capabilities into
the live chat turn.

Every hook is flag-gated (default OFF) and fail-open: a disabled flag or any
error returns the no-op value, so the turn behaves exactly as before. The
per-capability logic lives in the dedicated modules — this module is only the
glue that the orchestrator's ReAct loop calls at a few seams.

Capabilities glued here:
  flow_patterns (C-S1)   — per-turn tool budget
  ledger (C-N7)          — dual task/progress ledger
  asi_coverage (C-S12)   — plan-deviation detection
  skill_memory (C-N10)   — induce/replay reusable recipes
  supervisor (C-S5)      — drafted-answer leak/PHI review
  moa (C-N9)             — multi-candidate aggregation
  fanout (C-N8)          — batch oversized parallel tool waves
  mas_defense (C-S14)    — scan inter-agent payloads
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# --- flow patterns (C-S1) -------------------------------------------------- #

def flow_pattern(request: str, tool_count: int = 0, has_attachment: bool = False):
    """Classify this turn's flow pattern, or None when the capability is off."""
    try:
        from orchestrator import flow_patterns
        if not flow_patterns.flow_patterns_enabled():
            return None
        return flow_patterns.classify_flow(
            request, tool_count=tool_count, has_attachment=has_attachment)
    except Exception:
        logger.debug("flow_pattern hook failed", exc_info=True)
        return None


def over_tool_budget(pattern: Optional[str], tools_used: int) -> bool:
    """True when this flow pattern's tool budget is exhausted."""
    if not pattern:
        return False
    try:
        from orchestrator import flow_patterns
        return not flow_patterns.within_tool_budget(pattern, tools_used)
    except Exception:
        return False


# --- dual ledger (C-N7) ---------------------------------------------------- #

def new_ledger(request: str, recalled=None):
    """Open a Task-Ledger for the turn, or None when off."""
    try:
        from orchestrator import ledger
        if not ledger.ledger_enabled():
            return None
        return ledger.TaskLedger.from_request(request, recalled=recalled)
    except Exception:
        logger.debug("new_ledger hook failed", exc_info=True)
        return None


def ledger_audit(led) -> Optional[dict]:
    """A JSON-safe snapshot of the ledger for the audit chain, or None."""
    try:
        return led.to_audit_dict() if led is not None else None
    except Exception:
        return None


# --- ASI coverage (C-S12): plan deviation ---------------------------------- #

def plan_deviation(planned_tools, actual_tools):
    """Return a real Deviation when actual tools diverge from plan, else None."""
    try:
        from orchestrator import asi_coverage
        if not asi_coverage.asi_coverage_enabled():
            return None
        dev = asi_coverage.detect_deviation(list(planned_tools or []),
                                            list(actual_tools or []))
        return dev if asi_coverage.has_deviation(dev) else None
    except Exception:
        logger.debug("plan_deviation hook failed", exc_info=True)
        return None


# --- skill memory (C-N10) -------------------------------------------------- #

def match_skill(store, request: str):
    """Best matching learned recipe for this request, or None."""
    try:
        from orchestrator import skill_memory
        if not skill_memory.skill_memory_enabled() or not store:
            return None
        return skill_memory.match_recipe(list(store), request)
    except Exception:
        return None


def induce_skill(store, request: str, trace):
    """Distill a successful tool trace into a recipe and append it to the store."""
    try:
        from orchestrator import skill_memory
        if not skill_memory.skill_memory_enabled() or not trace or store is None:
            return None
        kws = [w for w in (request or "").lower().split() if len(w) > 3][:6]
        recipe = skill_memory.induce_recipe(
            f"recipe_{len(store) + 1}", trace, trigger_keywords=kws)
        store.append(recipe)
        return recipe
    except Exception:
        logger.debug("induce_skill hook failed", exc_info=True)
        return None


# --- supervisor (C-S5): drafted-answer review ------------------------------ #

def review_answer(content: str, phi_check=None):
    """Return (ok, reason). ok=False ⇒ the drafted answer leaks/PHI and must be
    blocked before it reaches the user."""
    try:
        from orchestrator import supervisor
        if not supervisor.supervisor_enabled():
            return True, ""
        verdict, reasons = supervisor.review_output(content or "", phi_check=phi_check)
        if verdict == supervisor.BLOCK:
            return False, "; ".join(reasons)
        return True, ""
    except Exception:
        return True, ""


# --- MoA debate (C-N9) ----------------------------------------------------- #

def should_debate(difficulty: float, confidence: float) -> bool:
    """Whether to run the multi-candidate panel for this (hard) turn.

    The difficulty threshold is ``MOA_DIFFICULTY_THRESHOLD`` (read per call,
    default 0.6)."""
    try:
        from orchestrator import moa
        return moa.moa_enabled() and moa.should_invoke(
            difficulty=difficulty, confidence=confidence,
            difficulty_threshold=moa.difficulty_threshold())
    except Exception:
        return False


def turn_difficulty(request: str, draft: str = "") -> float:
    """Deterministic request+draft difficulty in [0, 1] (0.0 on any error)."""
    try:
        from orchestrator import moa
        return moa.turn_difficulty(request, draft)
    except Exception:
        return 0.0


def should_debate_turn(request: str, draft: str = "") -> bool:
    """Gate the panel on a REAL signal computed from the turn: the user's
    request (length, question marks, reasoning / compare / why markers,
    multi-part asks) plus hedge markers in the drafted answer. Simple short
    answers never trigger the panel; only the combined score crossing
    ``MOA_DIFFICULTY_THRESHOLD`` does. Off-flag / any error ⇒ False."""
    try:
        from orchestrator import moa
        if not moa.moa_enabled():
            return False
        # confidence=1.0 so only the difficulty term can open the gate: the
        # draft's hedges are already folded into the difficulty score.
        return moa.should_invoke(
            difficulty=moa.turn_difficulty(request, draft), confidence=1.0,
            difficulty_threshold=moa.difficulty_threshold())
    except Exception:
        return False


def aggregate_candidates(candidates, ranking=None) -> Optional[str]:
    """candidates: iterable of (agent, text, score). Return the winning text.

    With ``ranking`` (agent → rank, lower is better — one judge verdict over
    the whole set) the winner is picked by the debate tournament
    (:func:`moa.panel` + :func:`moa.ranking_judge`); a candidate the ranking
    does not cover falls back to score. Without a ranking the highest score
    wins."""
    try:
        from orchestrator import moa
        props = [moa.Proposal(agent=a, text=t, score=s) for (a, t, s) in candidates]
        if not props:
            return None
        if ranking:
            return moa.panel(props, judge=moa.ranking_judge(dict(ranking))).text
        return moa.aggregate(props).text
    except Exception:
        return None


# --- fan-out (C-N8) -------------------------------------------------------- #

def fanout_batches(items: List[Any]):
    """Decompose an oversized parallel wave into batches, or None when off/small."""
    try:
        from orchestrator import fanout
        n = len(items or [])
        if not fanout.fanout_enabled() or not fanout.should_fan_out(n):
            return None
        return fanout.decompose(list(items))
    except Exception:
        return None


# --- MAS message defense (C-S14) ------------------------------------------- #

def scan_payload(payload) -> list:
    """Injection/attack findings in an inter-agent payload (empty when off)."""
    try:
        from orchestrator import mas_defense
        if not mas_defense.mas_defense_enabled():
            return []
        return mas_defense.scan_message(payload)
    except Exception:
        return []
