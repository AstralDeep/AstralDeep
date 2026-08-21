"""Per-tool quality-signal computation and `tool_flagged` / `tool_recovered`
audit-event emission.

A daily background job calls :func:`compute_for_window` once per 24 h
(scheduled in :mod:`backend.orchestrator.orchestrator`'s startup). The
function aggregates the prior 14-day window per ``(agent, tool)``,
classifies each tool as ``healthy`` / ``insufficient-data`` /
``underperforming`` per the operator-configurable thresholds, persists a
snapshot row, and emits audit events on transitions.

Defaults (FR-010 / FR-011 / FR-012):

* window = 14 days
* min eligibility dispatch count = 25
* flag if ``failure_rate >= 0.20 OR negative_feedback_rate >= 0.30``
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from audit.recorder import get_recorder, make_correlation_id, now_utc
from audit.schemas import AuditEventCreate

from .repository import FeedbackRepository
from .schemas import ToolQualitySignalDTO

logger = logging.getLogger("Feedback.Quality")


WINDOW_DAYS_ENV = "FEEDBACK_QUALITY_WINDOW_DAYS"
MIN_DISPATCH_ENV = "FEEDBACK_QUALITY_MIN_DISPATCH"
FAIL_RATE_ENV = "FEEDBACK_QUALITY_FAILURE_RATE_THRESHOLD"
NEG_FB_RATE_ENV = "FEEDBACK_QUALITY_NEGATIVE_FB_RATE_THRESHOLD"

DEFAULT_WINDOW_DAYS = 14
DEFAULT_MIN_DISPATCH = 25
DEFAULT_FAIL_RATE = 0.20
DEFAULT_NEG_FB_RATE = 0.30


def _read_thresholds():
    return (
        int(os.getenv(WINDOW_DAYS_ENV, str(DEFAULT_WINDOW_DAYS))),
        int(os.getenv(MIN_DISPATCH_ENV, str(DEFAULT_MIN_DISPATCH))),
        float(os.getenv(FAIL_RATE_ENV, str(DEFAULT_FAIL_RATE))),
        float(os.getenv(NEG_FB_RATE_ENV, str(DEFAULT_NEG_FB_RATE))),
    )


# ───────────────────── trajectory evaluation (C-N5 wiring) ────────────────────
# When FF_AGENT_EVAL is on, fold a deterministic trajectory-quality signal into
# the daily job. We reconstruct each turn's ordered tool-call sequence from the
# hash-chained audit trail (agent_tool_call *.end rows, grouped by
# correlation_id, ordered by recorded_at) and score each agent's trajectories
# against its OWN modal trajectory — a consistency/reliability measure (the same
# posture as τ-bench pass^k) that needs no external ground-truth reference.

#: Cap on trajectories pulled per window (bounds the extra query cost).
TRAJ_CAP_ENV = "FEEDBACK_QUALITY_TRAJECTORY_CAP"
DEFAULT_TRAJ_CAP = 2000


def _fetch_agent_trajectories(
    repo: FeedbackRepository, window_start: datetime, window_end: datetime,
    *, cap: int = DEFAULT_TRAJ_CAP,
) -> Dict[str, List[List[str]]]:
    """Reconstruct per-agent ordered tool-call trajectories from the audit log.

    Returns ``{agent_id: [[tool_name, ...], ...]}`` — one inner list per
    distinct ``correlation_id`` (a turn), tools in ``recorded_at`` order. Reads
    through Plane's fixed administration query. Best-effort: any error yields
    an empty mapping so the quality job is never broken by it.
    """
    try:
        rows = repo._audit.call(
            repo._audit.repository.list_tool_trajectory_events_for_administration,
            from_ts=window_start,
            to_ts=window_end,
            limit=cap,
        )
    except Exception:
        logger.debug("agent_eval: trajectory query failed", exc_info=True)
        return {}

    by_agent: Dict[str, Dict[Any, List[str]]] = {}
    for r in rows:
        agent_id = r.agent_id
        corr = r.correlation_id
        tool = r.tool_name
        by_agent.setdefault(agent_id, {}).setdefault(corr, []).append(tool)
    return {a: list(turns.values()) for a, turns in by_agent.items()}


def _score_agent_trajectories(
    trajectories: List[List[str]],
) -> Optional[Dict[str, Any]]:
    """Score one agent's trajectories against its modal (consensus) trajectory.

    Uses ``orchestrator.agent_eval`` (the C-N5 backbone): the most common
    tool-sequence is the reference; every trajectory is scored against it and
    folded into a single quality + a pass^k reliability number. Returns ``None``
    when there is nothing to score or the backbone is unavailable.
    """
    if not trajectories:
        return None
    try:
        from orchestrator import agent_eval
    except Exception:  # pragma: no cover — backbone import shouldn't fail
        logger.debug("agent_eval: backbone import failed", exc_info=True)
        return None

    # Reference = modal trajectory (most frequent ordered tool sequence).
    modal_key, _ = Counter(tuple(t) for t in trajectories).most_common(1)[0]
    reference = list(modal_key)

    pairs = [(t, reference) for t in trajectories]
    batch = agent_eval.score_trajectory_batch(pairs)

    # Reliability: pass^k over "exactly matches the consensus" per turn.
    outcomes = [
        agent_eval.trajectory_exact_match(t, reference) >= 1.0 for t in trajectories
    ]
    k = min(len(outcomes), int(os.getenv("FEEDBACK_QUALITY_PASS_K", "2")) or 2)
    pass_k = agent_eval.pass_k_from_outcomes(outcomes, k) if k >= 1 else 0.0

    return {
        "trajectory_count": batch["trajectory_count"],
        "mean_quality": batch["mean_quality"],
        "consensus_match_rate": batch["exact_match_rate"],
        "pass_k": pass_k,
        "k": k,
        "reference_len": len(reference),
        "metric_means": batch["metric_means"],
    }


def evaluate_trajectories(
    repo: FeedbackRepository, window_start: datetime, window_end: datetime,
    *, cap: int = DEFAULT_TRAJ_CAP,
) -> Dict[str, Dict[str, Any]]:
    """Compute a per-agent trajectory-quality summary for the window.

    Returns ``{agent_id: summary}`` (summary as in :func:`_score_agent_trajectories`).
    The daily job calls this only when ``FF_AGENT_EVAL`` is enabled; it is a
    pure read (no writes) and never raises.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for agent_id, trajectories in _fetch_agent_trajectories(
        repo, window_start, window_end, cap=cap
    ).items():
        summary = _score_agent_trajectories(trajectories)
        if summary is not None:
            out[agent_id] = summary
    return out


def classify_status(
    *, dispatch_count: int, failure_rate: float, negative_feedback_rate: float,
    min_dispatch: int = DEFAULT_MIN_DISPATCH,
    fail_rate_threshold: float = DEFAULT_FAIL_RATE,
    neg_fb_rate_threshold: float = DEFAULT_NEG_FB_RATE,
) -> str:
    if dispatch_count < min_dispatch:
        return "insufficient-data"
    if failure_rate >= fail_rate_threshold or negative_feedback_rate >= neg_fb_rate_threshold:
        return "underperforming"
    return "healthy"


async def compute_for_window(
    repo: FeedbackRepository,
    *,
    now: Optional[datetime] = None,
    window_days: Optional[int] = None,
    min_dispatch: Optional[int] = None,
    fail_rate_threshold: Optional[float] = None,
    neg_fb_rate_threshold: Optional[float] = None,
) -> List[ToolQualitySignalDTO]:
    """Compute snapshots for the prior window and emit transition audit events.

    Returns the list of computed snapshots. Caller is responsible for
    handing them to the proposal generator if it wants to (the synthesizer
    runs on its own cadence).
    """
    cfg_window, cfg_min, cfg_fail, cfg_neg = _read_thresholds()
    window_days = window_days or cfg_window
    min_dispatch = min_dispatch if min_dispatch is not None else cfg_min
    fail_rate_threshold = fail_rate_threshold if fail_rate_threshold is not None else cfg_fail
    neg_fb_rate_threshold = neg_fb_rate_threshold if neg_fb_rate_threshold is not None else cfg_neg

    window_end = now or datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=window_days)

    rows = repo.aggregate_window(window_start, window_end)
    snapshots: List[ToolQualitySignalDTO] = []

    for r in rows:
        agent_id = r["agent_id"]
        tool_name = r["tool_name"]
        dispatch_count = int(r["dispatch_count"])
        failure_count = int(r["failure_count"])
        negative_feedback_count = int(r["negative_feedback_count"])
        failure_rate = (failure_count / dispatch_count) if dispatch_count else 0.0
        negative_feedback_rate = (
            negative_feedback_count / dispatch_count if dispatch_count else 0.0
        )
        status = classify_status(
            dispatch_count=dispatch_count,
            failure_rate=failure_rate,
            negative_feedback_rate=negative_feedback_rate,
            min_dispatch=min_dispatch,
            fail_rate_threshold=fail_rate_threshold,
            neg_fb_rate_threshold=neg_fb_rate_threshold,
        )

        prior = repo.latest_quality_signal(agent_id, tool_name)

        new_dto = ToolQualitySignalDTO(
            id="",
            agent_id=agent_id,
            tool_name=tool_name,
            window_start=window_start,
            window_end=window_end,
            dispatch_count=dispatch_count,
            failure_count=failure_count,
            negative_feedback_count=negative_feedback_count,
            failure_rate=failure_rate,
            negative_feedback_rate=negative_feedback_rate,
            status=status,
            computed_at=window_end,
        )
        persisted = repo.insert_quality_signal(new_dto)
        snapshots.append(persisted)

        # Transition detection (FR-012a)
        prior_status = prior.status if prior else None
        if status == "underperforming" and prior_status != "underperforming":
            await _emit_transition_event(
                event="tool_flagged", dto=persisted,
                fail_rate_threshold=fail_rate_threshold,
                neg_fb_rate_threshold=neg_fb_rate_threshold,
            )
        elif prior_status == "underperforming" and status != "underperforming":
            await _emit_transition_event(
                event="tool_recovered", dto=persisted,
                fail_rate_threshold=fail_rate_threshold,
                neg_fb_rate_threshold=neg_fb_rate_threshold,
            )

    # C-N5: fold a deterministic trajectory-quality signal into the job output
    # (flag-gated; default OFF → byte-identical behaviour to before).
    await _maybe_evaluate_trajectories(repo, window_start, window_end, snapshots)

    return snapshots


async def _maybe_evaluate_trajectories(
    repo: FeedbackRepository, window_start: datetime, window_end: datetime,
    snapshots: List[ToolQualitySignalDTO],
) -> Dict[str, Any]:
    """Score recent agent tool-call trajectories and fold the result into the
    job output: stamp each snapshot DTO with a ``trajectory_quality`` attribute
    and emit one ``agent_eval`` audit event per agent. No-op + ``{}`` unless
    ``FF_AGENT_EVAL`` is on. Never raises (the daily job must not break).
    """
    try:
        from orchestrator.agent_eval import agent_eval_enabled
        if not agent_eval_enabled():
            return {}
        summaries = evaluate_trajectories(repo, window_start, window_end)
        if not summaries:
            logger.info("agent_eval: no trajectories to score in window")
            return {}

        # Fold into the returned snapshots so callers that inspect them see the
        # per-agent trajectory quality without a separate query.
        for dto in snapshots:
            summary = summaries.get(dto.agent_id)
            if summary is not None:
                try:
                    setattr(dto, "trajectory_quality", summary)
                except Exception:
                    pass

        for agent_id, summary in summaries.items():
            logger.info(
                "agent_eval: agent=%s trajectories=%d mean_quality=%.3f "
                "consensus_match=%.3f pass^%d=%.3f",
                agent_id, summary["trajectory_count"], summary["mean_quality"],
                summary["consensus_match_rate"], summary["k"], summary["pass_k"],
            )
            await _emit_trajectory_event(agent_id, summary)
        return summaries
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("agent_eval: trajectory evaluation failed: %s", exc)
        return {}


async def _emit_trajectory_event(agent_id: str, summary: Dict[str, Any]) -> None:
    """Emit an ``agent_eval`` (class ``tool_quality``) audit event carrying a
    per-agent trajectory-quality summary. Best-effort."""
    rec = get_recorder()
    if rec is None:
        return
    try:
        await rec.record(AuditEventCreate(
            actor_user_id="system",
            auth_principal="system:feedback.quality_job",
            agent_id=agent_id,
            event_class="tool_quality",
            action_type="trajectory_evaluated",
            description=(
                f"Trajectory evaluation for {agent_id}: "
                f"{summary['trajectory_count']} turns, "
                f"mean_quality={summary['mean_quality']:.3f}, "
                f"pass^{summary['k']}={summary['pass_k']:.3f}"
            ),
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={
                "agent_id": agent_id,
                "trajectory_count": summary["trajectory_count"],
                "mean_quality": summary["mean_quality"],
                "consensus_match_rate": summary["consensus_match_rate"],
                "pass_k": summary["pass_k"],
                "k": summary["k"],
                "metric_means": summary["metric_means"],
            },
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.warning("agent_eval audit emit failed: %s", exc)


async def _emit_transition_event(
    *,
    event: str,  # "tool_flagged" | "tool_recovered"
    dto: ToolQualitySignalDTO,
    fail_rate_threshold: float,
    neg_fb_rate_threshold: float,
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    try:
        await rec.record(AuditEventCreate(
            actor_user_id="system",
            auth_principal="system:feedback.quality_job",
            agent_id=dto.agent_id,
            event_class="tool_quality",
            action_type=event,
            description=(
                f"Tool {dto.tool_name} on agent {dto.agent_id} "
                f"{'flagged underperforming' if event == 'tool_flagged' else 'recovered'}"
            ),
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={
                "tool_name": dto.tool_name,
                "agent_id": dto.agent_id,
                "window_days": (dto.window_end - dto.window_start).days,
                "dispatch_count": dto.dispatch_count,
                "failure_rate": round(dto.failure_rate, 4),
                "negative_feedback_rate": round(dto.negative_feedback_rate, 4),
                "fail_rate_threshold": fail_rate_threshold,
                "neg_fb_rate_threshold": neg_fb_rate_threshold,
            },
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.warning("tool_quality audit emit failed (%s): %s", event, exc)
