"""Bind emergency-stop state to strict Plane-backed audit persistence and discovery.
Reachability never substitutes for a remote stop receipt, and bounded audit reconstruction
refuses unavailable or malformed transition history.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Any

logger = logging.getLogger("Orchestrator.EmergencyStopBinding")

ENGAGE_ACTION = "emergency_stop.engage"
RESUME_ACTION = "emergency_stop.resume"
ACK_ACTION = "emergency_stop.acknowledgment"
EVENT_CLASS = "emergency_stop"
_SCAN_LIMIT = 25
_SCAN_PAGES = 16
_REMOTE_PREFIX = "remote:"
_PROBE_TIMEOUT_SECONDS = 6.0
_OPEN_EXECUTION_SCAN = 200


def build_audit_hook(orch):
    async def hook(action: str, outcome: str, *, owner_id: str,
                   claims: dict | None = None, detail: dict[str, Any] | None = None) -> None:
        from audit.hooks import actor_principal_from_claims
        from audit.recorder import make_correlation_id, now_utc
        from audit.schemas import AuditEventCreate, AuditEventDTO

        repo = getattr(orch, "audit_repo", None)
        if repo is None:
            raise RuntimeError("emergency stop audit unavailable")
        user, principal = actor_principal_from_claims(claims or {"sub": owner_id})
        if user != owner_id:
            raise RuntimeError("emergency stop audit owner mismatch")
        event = AuditEventCreate(
            actor_user_id=owner_id, auth_principal=principal,
            event_class=EVENT_CLASS,
            action_type=action,
            description=action.split(".", 1)[-1].replace("_", " "),
            inputs_meta=dict(detail or {}),
            correlation_id=make_correlation_id(), started_at=now_utc(),
            outcome=outcome,
            outcome_detail=None if outcome == "success" else str((detail or {}).get("denial") or outcome),
        )
        recorded = await asyncio.to_thread(repo.insert, event)
        if (not isinstance(recorded, AuditEventDTO) or not recorded.event_id
                or recorded.event_class != event.event_class
                or recorded.action_type != event.action_type
                or recorded.outcome != event.outcome
                or recorded.correlation_id != event.correlation_id
                or recorded.inputs_meta != event.inputs_meta):
            raise RuntimeError("emergency stop durable audit receipt mismatch")
    return hook


def build_remote_responders(orch):
    def discover(owner_id: str) -> list[str]:
        from orchestrator import remote_machines

        rows = remote_machines.list_machines(plane_source(orch), owner_id)
        if (not isinstance(rows, list)
                or any(not isinstance(row, dict) or not isinstance(row.get("machine_id"), str)
                       or not row["machine_id"] for row in rows)):
            raise RuntimeError("emergency stop inventory unavailable")
        return [row["machine_id"] for row in rows]
    return discover


def plane_source(orch):
    from orchestrator.plane_repository_context import plane_source_from_orchestrator

    return plane_source_from_orchestrator(orch)


async def probe_responder(orch, owner_id: str, responder: str) -> str:
    if not responder.startswith(_REMOTE_PREFIX):
        return "acknowledged"
    machine_id = responder[len(_REMOTE_PREFIX):]
    source = plane_source(orch)

    def open_executions():
        from orchestrator import remote_jobs

        try:
            rows = remote_jobs.list_open(source, limit=_OPEN_EXECUTION_SCAN)
        except Exception:
            logger.debug("emergency_stop open-execution scan failed", exc_info=True)
            return None
        return [row for row in rows
                if row.get("owner_id") == owner_id and row.get("machine_id") == machine_id
                and not row.get("terminal")]

    outstanding = await asyncio.to_thread(open_executions)
    if outstanding is None:
        return "unreachable"
    if outstanding:
        return "pending"

    def reach():
        from orchestrator import remote_machines
        from orchestrator.remote_transport import get_transport

        try:
            target = remote_machines.build_target(
                source, getattr(orch, "credential_manager", None), owner_id, machine_id)
        except Exception:
            return "unreachable"
        try:
            result = get_transport().probe(target, timeout=_PROBE_TIMEOUT_SECONDS)
        except Exception:
            return "unreachable"
        return "pending" if bool(getattr(result, "ok", False)) else "unreachable"

    return await asyncio.to_thread(reach)


def build_rearm_loader(orch):
    def load(owner_id: str) -> dict[str, Any] | None:
        repo = getattr(orch, "audit_repo", None)
        if repo is None:
            raise RuntimeError("emergency stop audit unavailable")
        cursor = None
        seen = set()
        for _ in range(_SCAN_PAGES):
            items, next_cursor = repo.list_for_user(
                owner_id, event_classes=[EVENT_CLASS], limit=_SCAN_LIMIT, cursor=cursor)
            if not isinstance(items, list) or len(items) > _SCAN_LIMIT:
                raise RuntimeError("emergency stop audit page invalid")
            for event in items:
                action = getattr(event, "action_type", "")
                if (getattr(event, "event_class", None) != EVENT_CLASS
                        or getattr(event, "outcome", None) != "success"
                        or action not in (ENGAGE_ACTION, RESUME_ACTION)):
                    continue
                meta = getattr(event, "inputs_meta", None)
                if not isinstance(meta, dict):
                    raise RuntimeError("emergency stop audit metadata invalid")
                if action == ENGAGE_ACTION:
                    return {
                        "engaged": True, "revision": _meta_int(meta.get("revision")),
                        "engaged_at": _meta_time(meta.get("engaged_at")),
                        "engaged_by": owner_id, "reason": meta.get("reason"),
                    }
                if meta.get("denial"):
                    raise RuntimeError("emergency stop successful denial invalid")
                revision = _meta_int(meta.get("current_revision"))
                if _meta_int(meta.get("expected_revision")) != revision:
                    raise RuntimeError("emergency stop resume revision invalid")
                return {"engaged": False, "revision": revision}
            if next_cursor is None:
                return None
            if (not isinstance(next_cursor, str) or not next_cursor
                    or next_cursor in seen or not items):
                raise RuntimeError("emergency stop audit cursor invalid")
            seen.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError("emergency stop audit history incomplete")
    return load


def _meta_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise RuntimeError("emergency stop audit revision invalid")
    return value


def _meta_time(value: Any) -> float:
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        if 0 <= value <= 2**63 - 1 and math.isfinite(value):
            return float(value)
        raise RuntimeError("emergency stop audit time invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise RuntimeError("emergency stop audit time invalid") from None
    if parsed.tzinfo is None:
        raise RuntimeError("emergency stop audit timezone missing")
    timestamp = parsed.timestamp()
    if not math.isfinite(timestamp) or timestamp < 0:
        raise RuntimeError("emergency stop audit time invalid")
    return timestamp


def mount(orch):
    from orchestrator.emergency_stop import EmergencyStopCoordinator
    from orchestrator.emergency_stop_store import EmergencyStopStore

    store = EmergencyStopStore(orch)
    orch.emergency_stop_store = store

    coordinator = EmergencyStopCoordinator(
        audit=store.transition,
        remote_responders=build_remote_responders(orch),
        probe_responder=lambda owner_id, responder: probe_responder(orch, owner_id, responder),
        rearm_loader=store.load,
        fresh_rearm=True,
        interrupt=lambda owner_id, task: interrupt_owner(orch, owner_id, task),
    )
    orch.emergency_stop = coordinator
    return coordinator


async def interrupt_owner(orch, owner_id, control_task=None):
    from orchestrator.emergency_stop import await_interruption

    current = asyncio.current_task()
    tasks = set()
    for context in tuple((getattr(orch, "_connection_contexts", None) or {}).values()):
        claims = (getattr(orch, "ui_sessions", None) or {}).get(context.websocket)
        if not isinstance(claims, dict) or claims.get("sub") != owner_id:
            continue
        for task in tuple(context.operation_tasks):
            if task is not current and task is not control_task and not task.done():
                tasks.add(task)
                task.cancel()
    managers = []
    for manager_name in ("async_task_manager", "stream_manager"):
        manager = getattr(orch, manager_name, None)
        if manager is not None:
            managers.append(asyncio.create_task(manager.cancel_for_owner(owner_id)))
    try:
        await await_interruption(tasks | set(managers))
        for task in managers:
            task.result()
    finally:
        for task in managers:
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume_interruption)


def _consume_interruption(task):
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def submission_gate(orch):
    from orchestrator.work_admission import OwnerScope

    stop = getattr(orch, "emergency_stop", None)
    if stop is None:
        return None

    def gate(request) -> str | None:
        owner = getattr(request, "owner", None)
        if owner is None or owner.owner_scope not in {OwnerScope.USER, OwnerScope.SCHEDULE} or not owner.owner_user_id:
            return None
        return None if stop.admission_allowed(owner.owner_user_id) else "emergency_stop_active"
    return gate
