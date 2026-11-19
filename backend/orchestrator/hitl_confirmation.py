"""Checks owner settings confirmations and project file-read authority and records their audit events.
Protected tool dispatch uses this module to audit each Kernel-authorized effect.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)
SURFACE_CONFIRMATION_TTL_S = 120
SURFACE_CONFIRMATIONS_PER_OWNER = 16
_SURFACE_CONFIRMATIONS_TOTAL = 128

_SERVER_ARGUMENTS = frozenset({"_credentials", "_credentials_encrypted", "_session_llm_credentials",
                               "_delegation_token", "_cap_job_id"})


@dataclass(frozen=True)
class AuditCall:
    request_id: str
    owner: str
    chat: str | None
    agent: str
    tool: str
    arguments: str
    risks: tuple[str, ...]
    approval_source: str | None = None


@dataclass(frozen=True)
class SurfaceConfirmation:
    confirmation_id: str
    owner: str
    websocket: object
    action: str
    target: str
    expires: float


def _arguments(args) -> str:
    if not isinstance(args, dict):
        raise ValueError("confirmation arguments must be an object")
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("confirmation arguments exceed limit")
    return encoded


def attended(orch, websocket, owner: str) -> bool:
    from orchestrator.async_tasks import VirtualWebSocket

    if not isinstance(owner, str) or not owner or websocket is None or isinstance(websocket, VirtualWebSocket):
        return False
    claims = getattr(orch, "ui_sessions", {}).get(websocket)
    return (isinstance(claims, dict) and claims.get("sub") == owner
            and not claims.get("machine_class") and claims.get("_invocation_channel") not in {
                "rest", "mcp", "a2a", "background", "scheduled", "chained", "stream"})


async def _audit(orch, call: AuditCall, transition: str) -> None:
    from audit.recorder import now_utc
    from audit.schemas import AuditEventCreate

    try:
        correlation = str(uuid.UUID(call.request_id))
    except (ValueError, TypeError, AttributeError):
        correlation = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hitl:{call.request_id}"))
    try:
        receipt = await orch.audit_recorder.record(AuditEventCreate(
            actor_user_id=call.owner, auth_principal=call.owner,
            event_class="agent_tool_call", action_type=f"hitl.{transition}",
            description=f"{transition}: {call.tool}", correlation_id=correlation,
            conversation_id=call.chat,
            outcome=("interrupted" if transition == "effect_uncertain" else
                     "failure" if transition == "effect_failed_no_effect" else "success"),
            inputs_meta={"agent_id": call.agent, "tool": call.tool,
                         "risks": list(call.risks),
                         "arguments_sha256": hashlib.sha256(
                             call.arguments.encode("utf-8")).hexdigest(),
                         **({"approval_source": call.approval_source} if call.approval_source else {})},
            started_at=now_utc(), completed_at=now_utc(),
        ))
        if "source_bound" in call.risks and receipt is None:
            raise RuntimeError("audit receipt unavailable")
    except Exception:
        if "source_bound" in call.risks:
            raise
        logger.debug("HITL audit unavailable", exc_info=True)


async def audit_de_escalation(orch, owner, chat, agent, tool, args) -> None:
    try:
        encoded = _arguments(args)
    except (TypeError, ValueError):
        encoded = "{}"
    call = AuditCall(str(uuid.uuid4()), owner, chat, agent, tool, encoded, ("de_escalation",))
    await _audit(orch, call, "de_escalation")


async def audit_effect_outcome(orch, call: AuditCall, *, uncertain: bool, no_effect: bool = False) -> None:
    if "source_bound" not in call.risks:
        raise ValueError("source-bound approval required")
    await _audit(orch, call, "effect_failed_no_effect" if no_effect else
                 "effect_uncertain" if uncertain else "effect_response_success")


async def audit_kernel_effect(orch, capability, transition: str, *, uncertain=False, no_effect=False) -> None:
    from audit.recorder import now_utc
    from audit.schemas import AuditEventCreate

    if transition not in {"effect_started", "effect_response_success", "effect_failed_no_effect", "effect_uncertain"}:
        raise ValueError("invalid Kernel effect audit transition")
    correlation = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"astral-kernel-effect:{capability.request_id}:{capability.ticket_digest}:{transition}"))
    receipt = await orch.audit_recorder.record(AuditEventCreate(
        actor_user_id=capability.owner, auth_principal="machine:mesh_request",
        event_class="agent_tool_call", action_type=f"mesh.kernel_{transition}",
        description=f"Kernel-authorized {transition}: {capability.tool}",
        correlation_id=correlation, conversation_id=capability.chat,
        outcome=("interrupted" if uncertain else "failure" if no_effect else "success"),
        inputs_meta={"approval_source": "kernel_ticket", "request_id": capability.request_id,
                     "mesh_id": capability.mesh_id, "device_id": capability.device_id,
                     "request_device": capability.request_device, "agent_id": capability.agent,
                     "tool": capability.tool, "ticket_sha256": capability.ticket_digest,
                     "action_digest": capability.action_digest,
                     "arguments_sha256": capability.arguments_digest,
                     "kernel_consented": capability.consented},
        started_at=now_utc(), completed_at=now_utc(),
    ))
    if receipt is None:
        raise RuntimeError("Kernel effect audit receipt unavailable")


def _surface_confirmations(orch) -> dict[str, SurfaceConfirmation]:
    now = time.monotonic()
    current = {key: item for key, item in getattr(orch, "_hitl_surface_confirmations", {}).items()
               if item.expires > now}
    orch._hitl_surface_confirmations = current
    return current


def _surface_call(orch, entry: SurfaceConfirmation) -> AuditCall:
    from orchestrator.hitl import IRREVERSIBLE

    chat = getattr(orch, "_ws_active_chat", {}).get(id(entry.websocket))
    return AuditCall(entry.confirmation_id, entry.owner, chat, "owner-surface", entry.action, entry.target,
                     (IRREVERSIBLE,))


async def request_surface_confirmation(orch, websocket, owner, action, target) -> str | None:
    try:
        encoded = _arguments(target)
    except (TypeError, ValueError):
        return None
    if not attended(orch, websocket, owner):
        return None
    confirmations = _surface_confirmations(orch)
    for item in confirmations.values():
        if (item.websocket is websocket and (item.owner, item.action, item.target) == (owner, action, encoded)):
            return item.confirmation_id
    if (len(confirmations) >= _SURFACE_CONFIRMATIONS_TOTAL
            or sum(item.owner == owner for item in confirmations.values()) >= SURFACE_CONFIRMATIONS_PER_OWNER):
        return None
    entry = SurfaceConfirmation(uuid.uuid4().hex, owner, websocket, action, encoded,
                                time.monotonic() + SURFACE_CONFIRMATION_TTL_S)
    confirmations[entry.confirmation_id] = entry
    await _audit(orch, _surface_call(orch, entry), "proposed")
    return entry.confirmation_id


async def consume_surface_confirmation(orch, websocket, owner, action, target, confirmation_id) -> bool:
    try:
        encoded = _arguments(target)
    except (TypeError, ValueError):
        return False
    confirmations = _surface_confirmations(orch)
    entry = confirmations.get(confirmation_id) if isinstance(confirmation_id, str) else None
    if (entry is None or entry.websocket is not websocket or (entry.owner, entry.action, entry.target)
            != (owner, action, encoded) or not attended(orch, websocket, owner)):
        return False
    confirmations.pop(confirmation_id)
    await _audit(orch, _surface_call(orch, entry), "consumed")
    return True


async def project_read_approval(orch, websocket, owner, chat, agent, tool, args):
    from orchestrator.project_authority import FILE_VERBS, ProjectSessionWitness, current_origin, origin_current
    from orchestrator.remote_confirmation import computer_host

    if agent != "computer-use-1" or tool not in FILE_VERBS:
        return None
    host = computer_host(orch, owner, chat, args)
    if not getattr(host, "project_mode", False):
        return None
    origin = current_origin(orch, owner, chat, host)
    if origin is None:
        raise RuntimeError("project authority unavailable")
    origin.require_grant()
    session = orch.computer_sessions.live_for_host(owner, host.host_id)
    connection = getattr(orch, "_connection_contexts", {}).get(id(websocket))
    session_origin = getattr(session, "project_origin", None)
    if (not attended(orch, websocket, owner) or not origin_current(orch, session_origin)
            or session_origin.host is not host or session_origin.context.project_id != origin.context.project_id
            or session.state != "active" or session.chat_id != chat or session.owner_sub != owner
            or session.controller_ws_id != id(websocket) or orch.computer_sessions.get(session.session_id) is not session
            or connection is None or connection.websocket is not websocket or not connection.registered
            or connection.closing or getattr(orch, "cancelled_sessions", {}).get(id(websocket), False)):
        raise RuntimeError("project session unavailable")
    witness = ProjectSessionWitness.capture(orch, session)
    generation = connection.connection_generation
    call = AuditCall(str(uuid.uuid4()), owner, chat, agent, tool, _arguments(args), ("source_bound",), "project_grant")
    for transition in ("project_grant", "effect_started"):
        await _audit(orch, call, transition)
        if (not witness.current(orch) or not origin_current(orch, session_origin)
                or not attended(orch, websocket, owner) or orch._connection_contexts.get(id(websocket)) is not connection
                or connection.closing or not connection.registered or connection.connection_generation != generation
                or getattr(orch, "cancelled_sessions", {}).get(id(websocket), False)):
            raise RuntimeError("project session changed during audit")
    return call
