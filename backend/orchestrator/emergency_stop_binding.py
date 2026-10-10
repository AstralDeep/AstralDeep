"""Host wiring for emergency_stop.py: builds the durable audit persistence and re-arm
loader over the orchestrator's hash-chained audit repository, the local interruption
hook over background work and computer sessions, the work-admission submission gate,
and mounts one EmergencyStopCoordinator on the orchestrator instance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("Orchestrator.EmergencyStopBinding")

EVENT_CLASS = "emergency_stop"
ENGAGE_ACTION = "emergency_stop.engage"
RESUME_ACTION = "emergency_stop.resume"
_STOP_CODE = "emergency_stop_active"
_SCAN_LIMIT = 50
_INTERRUPT_TASK_LIMIT = 25


def _audit_repo(orch) -> Any:
    repo = getattr(orch, "audit_repo", None)
    if repo is None:
        raise RuntimeError("audit repository unavailable")
    return repo


def build_persistence(orch):
    def persist(owner_id: str, action: str, detail: dict[str, Any]) -> None:
        from audit.hooks import make_correlation_id, now_utc
        from audit.schemas import AuditEventCreate

        _audit_repo(orch).insert(AuditEventCreate(
            actor_user_id=owner_id,
            auth_principal=owner_id,
            event_class=EVENT_CLASS,
            action_type=action,
            description=action.split(".", 1)[-1].replace("_", " "),
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta=dict(detail or {}),
            started_at=now_utc(),
        ))
    return persist


def _meta_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _event_time(event: Any) -> float | None:
    recorded = getattr(event, "recorded_at", None)
    if isinstance(recorded, datetime):
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        return recorded.timestamp()
    meta = getattr(event, "inputs_meta", None) or {}
    try:
        parsed = datetime.fromisoformat(str(meta.get("engaged_at")))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def build_loader(orch):
    def load(owner_id: str) -> dict[str, Any] | None:
        items, _cursor = _audit_repo(orch).list_for_user(
            owner_id, event_classes=[EVENT_CLASS], limit=_SCAN_LIMIT)
        for event in items:
            if getattr(event, "outcome", "") != "success":
                continue
            action = getattr(event, "action_type", "")
            meta = getattr(event, "inputs_meta", None) or {}
            if action == ENGAGE_ACTION:
                return {
                    "engaged": True,
                    "revision": _meta_int(meta.get("revision"), 1),
                    "engaged_at": _event_time(event),
                    "engaged_by": str(getattr(event, "actor_user_id", "") or owner_id),
                    "reason": str(meta.get("reason") or ""),
                }
            if action == RESUME_ACTION:
                return {"engaged": False,
                        "revision": _meta_int(meta.get("revision"), 0)}
        return {"engaged": False, "revision": 0}
    return load


def build_denial_audit(orch):
    async def record(action: str, outcome: str, *, owner_id: str,
                     detail: dict[str, Any] | None = None) -> None:
        from audit.hooks import record_generic

        await record_generic(
            claims={"sub": owner_id},
            event_class=EVENT_CLASS,
            action_type=action,
            description=action.split(".", 1)[-1].replace("_", " "),
            inputs_meta=dict(detail or {}),
            outcome=outcome,
            outcome_detail=str((detail or {}).get("denial") or outcome),
        )
    return record


def build_interrupt(orch):
    async def interrupt(owner_id: str) -> dict[str, Any]:
        counts = {"tasks": 0, "sessions": 0}
        manager = getattr(orch, "async_task_manager", None)
        if manager is not None:
            for task in await manager.list_for_user(owner_id, limit=_INTERRUPT_TASK_LIMIT):
                try:
                    if await manager.cancel(task.task_id):
                        counts["tasks"] += 1
                except Exception:
                    logger.debug("emergency stop task cancel failed", exc_info=True)
        sessions = getattr(orch, "computer_sessions", None)
        if sessions is not None:
            for session in sessions.live_for_owner(owner_id):
                try:
                    if session.active():
                        await sessions.pause(session, "emergency_stop", push=False)
                        counts["sessions"] += 1
                except Exception:
                    logger.debug("emergency stop session pause failed", exc_info=True)
        return counts
    return interrupt


def mount(orch):
    from orchestrator.emergency_stop import EmergencyStopCoordinator

    coordinator = EmergencyStopCoordinator(
        persist=build_persistence(orch),
        load_durable=build_loader(orch),
        interrupt=build_interrupt(orch),
        audit_denial=build_denial_audit(orch),
    )
    orch.emergency_stop = coordinator
    return coordinator


def background_admission_guard(orch):
    async def guard(user_id: str) -> str | None:
        return stop_denial(orch, user_id)
    return guard


def submission_gate(orch):
    from orchestrator.work_admission import OwnerScope

    stop = getattr(orch, "emergency_stop", None)
    if stop is None:
        return None

    def gate(request) -> str | None:
        owner = getattr(request, "owner", None)
        if owner is None:
            return None
        if owner.owner_scope not in (OwnerScope.USER, OwnerScope.SCHEDULE):
            return None
        user_id = owner.owner_user_id
        if not isinstance(user_id, str) or not user_id:
            return None
        try:
            allowed = stop.admission_allowed(user_id)
        except Exception:
            logger.warning(
                "emergency stop admission check failed for %s; failing closed",
                user_id, exc_info=True)
            return "emergency_stop_unavailable"
        return None if allowed else _STOP_CODE
    return gate


def stop_denial(orch, owner_id) -> str | None:
    stop = getattr(orch, "emergency_stop", None)
    if stop is None or not isinstance(owner_id, str) or not owner_id:
        return None
    if owner_id == "legacy":
        return None
    try:
        allowed = stop.admission_allowed(owner_id)
    except Exception:
        logger.warning(
            "emergency stop admission check failed for %s; failing closed",
            owner_id, exc_info=True)
        return "emergency_stop_unavailable"
    return None if allowed else _STOP_CODE
