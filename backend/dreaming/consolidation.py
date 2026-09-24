"""Scoring and sweep logic for dreaming: run_sweep promotes recurring, PHI-gated signals
into durable memory, then, if idle, delegates to dreaming/sleeptime.py to precompute
anticipated answers. Called by dreaming/api.py and the scheduler.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Dreaming.Consolidation")

_RECENCY_HALF_LIFE_DAYS = 7.0


def score_signal(recall_count: int, last_seen_ms: int, now_ms: int) -> float:
    age_days = max(0.0, (now_ms - (last_seen_ms or now_ms)) / 86_400_000.0)
    recency = math.pow(0.5, age_days / _RECENCY_HALF_LIFE_DAYS)
    return float(recall_count) + recency


def select_promotions(
    signals: List[Dict[str, Any]],
    now_ms: int,
    *,
    min_recalls: int = 2,
    max_promote: int = 25,
) -> List[Dict[str, Any]]:
    eligible = [s for s in signals if int(s.get("recall_count", 0)) >= min_recalls]
    eligible.sort(
        key=lambda s: score_signal(int(s.get("recall_count", 0)), int(s.get("last_seen_at") or now_ms), now_ms),
        reverse=True,
    )
    return eligible[:max_promote]


# Leading underscore hides this from user-facing traits
_SLEEPTIME_PLAN_KEY = "_sleeptime_precompute"
_SLEEPTIME_MAX_SIGNALS = 25


def _run_sleeptime_precompute(
    repo,
    user_id: str,
    *,
    now_ms: int,
    last_activity_ms: Optional[int],
    idle_after_ms: int,
    budget: int,
) -> Optional[Dict[str, Any]]:
    from dreaming.sleeptime import (
        anticipate_questions,
        is_idle,
        precompute_plan,
        sleeptime_enabled,
    )

    if not sleeptime_enabled():
        return None
    if last_activity_ms is not None and not is_idle(
        last_activity_ms, now_ms, idle_after_ms=idle_after_ms
    ):
        return None

    if not (hasattr(repo, "upsert_profile") and hasattr(repo, "get_profile")):
        return None

    try:
        signals = repo.list_signals(user_id) or []
    except Exception:  # pragma: no cover
        signals = []
    recent_messages = [str(s.get("value") or "") for s in signals[:_SLEEPTIME_MAX_SIGNALS]]
    try:
        memories = repo.list_memory(user_id) if hasattr(repo, "list_memory") else []
    except Exception:  # pragma: no cover
        memories = []

    anticipated = anticipate_questions(recent_messages, list(memories or []))
    plan = precompute_plan(anticipated, budget=budget)
    if not plan:
        return None

    record = {
        "generated_at": now_ms,
        "trigger": "idle",
        "questions": [
            {"question": a.question, "rationale": a.rationale, "priority": a.priority}
            for a in plan
        ],
    }
    try:
        profile = repo.get_profile(user_id) or {}
        personality = dict(profile.get("personality") or {})
        personality[_SLEEPTIME_PLAN_KEY] = record
        repo.upsert_profile(user_id, personality=personality)
    except Exception:  # pragma: no cover
        logger.debug("dreaming.sleeptime persist failed (non-fatal)", exc_info=True)
        return None

    logger.info("dreaming.sleeptime_precomputed",
                extra={"user_id": user_id, "precomputed": len(plan)})
    return record


def run_sweep(
    repo,
    phi_gate,
    user_id: str,
    *,
    trigger: str = "scheduled",
    min_recalls: int = 2,
    now_ms: Optional[int] = None,
    last_activity_ms: Optional[int] = None,
    idle_after_ms: int = 300_000,
    precompute_budget: int = 3,
) -> Dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    signals = repo.list_signals(user_id)
    candidates = select_promotions(signals, now_ms, min_recalls=min_recalls)

    promoted = 0
    for sig in candidates:
        value = sig.get("value", "")
        if phi_gate.contains_phi(value):
            repo.delete_signal(user_id, sig["id"])
            continue
        repo.create_memory(
            user_id, sig["category"], value,
            source="promoted",
            salience=score_signal(int(sig.get("recall_count", 0)), int(sig.get("last_seen_at") or now_ms), now_ms),
        )
        repo.delete_signal(user_id, sig["id"])
        promoted += 1

    summary = (
        f"Reviewed {len(signals)} recent signal(s); promoted {promoted} recurring, "
        f"non-PHI item(s) into long-term memory."
    )
    sweep = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "ran_at": now_ms,
        "candidates_considered": len(signals),
        "promoted_count": promoted,
        "summary": summary,
        "trigger": trigger,
    }
    if hasattr(repo, "record_sweep"):
        repo.record_sweep(sweep)

    precompute = _run_sleeptime_precompute(
        repo, user_id, now_ms=now_ms, last_activity_ms=last_activity_ms,
        idle_after_ms=idle_after_ms, budget=precompute_budget,
    )
    sweep["precompute"] = (precompute or {}).get("questions", [])

    logger.info("dreaming.sweep_ran",
                extra={"user_id": user_id, "trigger": trigger,
                       "considered": len(signals), "promoted": promoted,
                       "precomputed": len(sweep["precompute"])})
    return sweep
