"""Keeps one incoming peer call bound to its verified mesh and current physical authority.
The chat bridge owns the context lifetime; protected dispatch rechecks it before gates and effects.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Iterator, Optional

from orchestrator import vessel_executor
from orchestrator.hitl_confirmation import _arguments, _SERVER_ARGUMENTS
from orchestrator.node_mesh import NodeMeshError
from shared.protocol import MCPResponse


@dataclass(frozen=True)
class Admission:
    mesh: Any
    kernel: Any
    owner_of: Callable[[], Optional[str]]
    owner: str
    chat: str
    resolved: Any
    ticket: vessel_executor.CallTicket
    lease: object


_CURRENT: contextvars.ContextVar[Optional[Admission]] = contextvars.ContextVar("node_mesh_admission", default=None)
_ACTIVE: set[object] = set()


@contextlib.contextmanager
def binding(mesh, owner_of, owner, chat, resolved, ticket) -> Iterator[None]:
    lease = object()
    admission = Admission(mesh, mesh.kernel, owner_of, owner, chat, resolved, ticket, lease)
    _ACTIVE.add(lease)
    token = _CURRENT.set(admission)
    try:
        yield
    finally:
        _ACTIVE.discard(lease)
        _CURRENT.reset(token)


def _matches(admission, owner, chat, agent, tool, args):
    if (owner, chat, agent, tool) != (
        admission.owner, admission.chat, admission.resolved.agent_id, admission.resolved.tool,
    ):
        return False
    action = json.loads(admission.ticket.action_json)
    original = json.loads(admission.ticket.arguments_json)
    compared = {key: value for key, value in args.items() if key in original or key not in _SERVER_ARGUMENTS}
    for key, expected in (("user_id", owner), ("session_id", chat)):
        if key not in original and compared.get(key) == expected:
            compared.pop(key)
    digest = "sha256:" + hashlib.sha256(admission.ticket.arguments_json.encode()).hexdigest()
    return action.get("args_digest") == digest and _arguments(compared) == _arguments(original)


def _current_tool(orch, admission):
    from orchestrator.node_mesh_calls import skill_definition

    resolved = admission.resolved
    flags = orch.security_flags.get(resolved.agent_id, {})
    if flags.get(resolved.tool, {}).get("blocked"):
        return "gate.hard_flag"
    card = orch.agent_cards.get(resolved.agent_id)
    skills = [skill for skill in getattr(card, "skills", ()) if skill.id == resolved.tool]
    if len(skills) != 1:
        return "gate.tool_def_changed"
    definition = vessel_executor.pinned_definition(orch, resolved.agent_id, resolved.tool) or skill_definition(skills[0])
    if definition != resolved.definition:
        return "gate.tool_def_changed"
    permissions = orch.tool_permissions
    if (permissions.get_tool_scope(resolved.agent_id, resolved.tool) != resolved.scope
            or not permissions.is_tool_allowed(admission.owner, resolved.agent_id, resolved.tool)):
        return "gate.permission_denied"
    return None


def _binding_code(admission, ticket, owner):
    if (admission is None or admission.lease not in _ACTIVE or admission.ticket is not ticket
            or admission.mesh.kernel is not admission.kernel):
        return "node.mesh_admission_unavailable"
    if admission.owner_of() != admission.owner or owner != admission.owner:
        return "node.owner_mismatch"
    return None


async def check(orch, websocket, owner, chat, agent, tool, args, *, physical=False):
    admission = _CURRENT.get()
    ticket = vessel_executor.current_ticket()
    if admission is None and ticket is None and getattr(websocket, "_node_mesh_peer_call", False) is not True:
        return None
    code = "node.mesh_admission_unavailable"
    try:
        code = _binding_code(admission, ticket, owner)
        if code is None:
            if not _matches(admission, owner, chat, agent, tool, args):
                code = "request.call_mismatch"
            else:
                await admission.mesh.verify_ticket(ticket.ticket_json, ticket.action_json, admission.resolved.effect_kind)
                code = _binding_code(admission, ticket, owner)
                if code is None and physical:
                    code = _current_tool(orch, admission)
                    if code is None:
                        await admission.mesh.verify_ticket(ticket.ticket_json, ticket.action_json, admission.resolved.effect_kind)
                        code = _binding_code(admission, ticket, owner)
                        if code is None:
                            code = (None if _matches(admission, owner, chat, agent, tool, args)
                                    else "request.call_mismatch")
                        if code is None:
                            code = _current_tool(orch, admission)
                if code is None:
                    return None
    except NodeMeshError as exc:
        code = exc.code
    except Exception:
        code = "node.mesh_admission_unavailable"
    await orch._audit_gate_denial(websocket, owner, agent, tool, chat, args, gate="node_mesh", detail=code)
    return MCPResponse(error={"code": code, "message": "Current mesh authority refused this call.",
                              "retryable": False, "effect_state": "not_started"})
