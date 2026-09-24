"""Flag-gated, fail-open glue between orchestrator.py's ReAct loop and the
flow_patterns/ledger/asi_coverage/skill_memory/supervisor/moa/fanout/mas_defense
modules; a disabled flag or any error returns the no-op value.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def flow_pattern(request: str, tool_count: int = 0, has_attachment: bool = False):
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
    if not pattern:
        return False
    try:
        from orchestrator import flow_patterns
        return not flow_patterns.within_tool_budget(pattern, tools_used)
    except Exception:
        return False


def new_ledger(request: str, recalled=None):
    try:
        from orchestrator import ledger
        if not ledger.ledger_enabled():
            return None
        return ledger.TaskLedger.from_request(request, recalled=recalled)
    except Exception:
        logger.debug("new_ledger hook failed", exc_info=True)
        return None


def ledger_audit(led) -> Optional[dict]:
    try:
        return led.to_audit_dict() if led is not None else None
    except Exception:
        return None


def plan_deviation(planned_tools, actual_tools):
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


def match_skill(store, request: str):
    try:
        from orchestrator import skill_memory
        if not skill_memory.skill_memory_enabled() or not store:
            return None
        return skill_memory.match_recipe(list(store), request)
    except Exception:
        return None


def induce_skill(store, request: str, trace):
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


def review_answer(content: str, phi_check=None):
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


def should_debate(difficulty: float, confidence: float) -> bool:
    try:
        from orchestrator import moa
        return moa.moa_enabled() and moa.should_invoke(
            difficulty=difficulty, confidence=confidence,
            difficulty_threshold=moa.difficulty_threshold())
    except Exception:
        return False


def turn_difficulty(request: str, draft: str = "") -> float:
    try:
        from orchestrator import moa
        return moa.turn_difficulty(request, draft)
    except Exception:
        return 0.0


def should_debate_turn(request: str, draft: str = "") -> bool:
    try:
        from orchestrator import moa
        if not moa.moa_enabled():
            return False
        return moa.should_invoke(
            difficulty=moa.turn_difficulty(request, draft), confidence=1.0,
            difficulty_threshold=moa.difficulty_threshold())
    except Exception:
        return False


def aggregate_candidates(candidates, ranking=None) -> Optional[str]:
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


def fanout_batches(items: List[Any]):
    try:
        from orchestrator import fanout
        n = len(items or [])
        if not fanout.fanout_enabled() or not fanout.should_fan_out(n):
            return None
        return fanout.decompose(list(items))
    except Exception:
        return None


def scan_payload(payload) -> list:
    try:
        from orchestrator import mas_defense
        if not mas_defense.mas_defense_enabled():
            return []
        return mas_defense.scan_message(payload)
    except Exception:
        return []
