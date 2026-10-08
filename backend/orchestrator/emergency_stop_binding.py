"""Host wiring for emergency_stop.py: builds the audit recorder, enrolled remote
responder discovery, probe-based acknowledgment with open-execution awareness,
and the audit-backed durable re-arm loader, then mounts one
EmergencyStopCoordinator on the orchestrator instance.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("Orchestrator.EmergencyStopBinding")

ENGAGE_ACTION = "emergency_stop.engage"
RESUME_ACTION = "emergency_stop.resume"
ACK_ACTION = "emergency_stop.acknowledgment"
EVENT_CLASS = "emergency_stop"
_SCAN_LIMIT = 25
_REMOTE_PREFIX = "remote:"
_PROBE_TIMEOUT_SECONDS = 6.0
_OPEN_EXECUTION_SCAN = 200


def build_audit_hook():
    async def hook(action: str, outcome: str, *, owner_id: str,
                   claims: dict | None = None, detail: dict[str, Any] | None = None) -> None:
        from audit.hooks import record_generic

        await record_generic(
            claims=claims or {"sub": owner_id},
            event_class=EVENT_CLASS,
            action_type=action,
            description=action.split(".", 1)[-1].replace("_", " "),
            inputs_meta=dict(detail or {}),
            outcome=outcome,
            outcome_detail=None if outcome == "success" else str((detail or {}).get("denial") or outcome),
        )
    return hook


def build_remote_responders(orch):
    def discover(owner_id: str) -> list[str]:
        from orchestrator import remote_machines

        rows = remote_machines.list_machines(plane_source(orch), owner_id)
        return [row["machine_id"] for row in rows
                if isinstance(row, dict) and row.get("machine_id")]
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
        return "acknowledged" if bool(getattr(result, "ok", False)) else "unreachable"

    return await asyncio.to_thread(reach)


def build_rearm_loader(orch):
    def load(owner_id: str) -> dict[str, Any] | None:
        repo = getattr(orch, "audit_repo", None)
        if repo is None:
            return None
        items, _cursor = repo.list_for_user(
            owner_id, event_classes=[EVENT_CLASS], limit=_SCAN_LIMIT)
        for event in items:
            action = getattr(event, "action_type", "")
            meta = getattr(event, "inputs_meta", None) or {}
            if action == ENGAGE_ACTION:
                engaged_at = _meta_time(meta.get("engaged_at")) or _event_time(event)
                return {
                    "engaged": True,
                    "revision": _meta_int(meta.get("revision"), 1),
                    "engaged_at": engaged_at,
                    "engaged_by": str(getattr(event, "actor_user_id", "") or owner_id),
                    "reason": str(meta.get("reason") or ""),
                }
            if action == RESUME_ACTION and not meta.get("denial"):
                return {"engaged": False,
                        "revision": _meta_int(meta.get("current_revision"), 0)}
        return None
    return load


def _meta_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _meta_time(value: Any) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _event_time(event: Any) -> float | None:
    recorded = getattr(event, "recorded_at", None)
    if isinstance(recorded, datetime):
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        return recorded.timestamp()
    return None


def mount(orch):
    from orchestrator.emergency_stop import EmergencyStopCoordinator

    coordinator = EmergencyStopCoordinator(
        audit=build_audit_hook(),
        remote_responders=build_remote_responders(orch),
        probe_responder=lambda owner_id, responder: probe_responder(orch, owner_id, responder),
        rearm_loader=build_rearm_loader(orch),
    )
    orch.emergency_stop = coordinator
    return coordinator


def submission_gate(orch):
    from orchestrator.work_admission import OwnerScope

    stop = getattr(orch, "emergency_stop", None)
    if stop is None:
        return None

    def gate(request) -> str | None:
        owner = getattr(request, "owner", None)
        if owner is None or owner.owner_scope is not OwnerScope.USER or not owner.owner_user_id:
            return None
        return None if stop.admission_allowed(owner.owner_user_id) else "emergency_stop_active"
    return gate
