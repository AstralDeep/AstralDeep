"""Keeps one incoming peer call, or every tool call of a turn the node runs itself, bound to its verified mesh and current physical authority.
A call of the node's own turn must be in the published catalog and the turn's granted tools, and holds a kernel ticket minted for exactly that call; without one it never dispatches.
The chat bridge owns both context lifetimes, and protected dispatch in orchestrator.py rechecks them at gate entry and before the effect."""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

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


TurnMint = Callable[[str, str, Dict[str, Any], Optional[str]], Awaitable[Tuple[vessel_executor.CallTicket, Any]]]
TurnCatalog = Callable[[], Awaitable[Dict[Tuple[str, str], Any]]]
TURN_MARK = "_node_mesh_turn"


@dataclass(frozen=True)
class _Held:
    agent: str
    tool: str
    ticket: vessel_executor.CallTicket
    resolved: Any


@dataclass
class Turn:
    mesh: Any
    kernel: Any
    owner_of: Callable[[], Optional[str]]
    owner: str
    chat: str
    tools: Dict[Tuple[str, str, str], str]
    catalog: TurnCatalog
    mint: TurnMint
    lease: object
    held: List[_Held] = field(default_factory=list)

    @property
    def scopes(self) -> List[str]:
        return sorted(set(self.tools.values()))

    @property
    def allowed(self) -> Tuple[Tuple[str, str, str], ...]:
        return tuple(sorted(self.tools))


_CURRENT: contextvars.ContextVar[Optional[Admission]] = contextvars.ContextVar("node_mesh_admission", default=None)
_TURN: contextvars.ContextVar[Optional[Turn]] = contextvars.ContextVar("node_mesh_turn", default=None)
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


@contextlib.contextmanager
def turn(mesh, owner_of, owner, chat, tools, catalog, mint) -> Iterator[Turn]:
    lease = object()
    bound = Turn(mesh, mesh.kernel, owner_of, owner, chat, dict(tools), catalog, mint, lease)
    _ACTIVE.add(lease)
    token = _TURN.set(bound)
    try:
        yield bound
    finally:
        _ACTIVE.discard(lease)
        _TURN.reset(token)


def current_turn() -> Optional[Turn]:
    return _TURN.get()


def _matches(admission, owner, chat, agent, tool, args):
    if (owner, chat, agent, tool) != (
        admission.owner, admission.chat, admission.resolved.agent_id, admission.resolved.tool,
    ):
        return False
    return _covers(admission.ticket, args, owner, chat)


def _covers(ticket, args, owner, chat):
    action = json.loads(ticket.action_json)
    original = json.loads(ticket.arguments_json)
    compared = {key: value for key, value in args.items() if key in original or key not in _SERVER_ARGUMENTS}
    for key, expected in (("user_id", owner), ("session_id", chat)):
        if key not in original and compared.get(key) == expected:
            compared.pop(key)
    digest = "sha256:" + hashlib.sha256(ticket.arguments_json.encode()).hexdigest()
    return action.get("args_digest") == digest and _arguments(compared) == _arguments(original)


def _current_tool(orch, admission):
    return _current_resolved(orch, admission.owner, admission.resolved)


def _current_resolved(orch, owner, resolved):
    from orchestrator.node_mesh_calls import skill_definition

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
            or not permissions.is_tool_allowed(owner, resolved.agent_id, resolved.tool)):
        return "gate.permission_denied"
    return None


def _binding_code(admission, ticket, owner):
    if (admission is None or admission.lease not in _ACTIVE or admission.ticket is not ticket
            or admission.mesh.kernel is not admission.kernel):
        return "node.mesh_admission_unavailable"
    if admission.owner_of() != admission.owner or owner != admission.owner:
        return "node.owner_mismatch"
    return None


def _in_turn(websocket) -> bool:
    return _TURN.get() is not None or getattr(websocket, TURN_MARK, False) is True


def _turn_binding_code(bound, owner, chat):
    if bound is None or bound.lease not in _ACTIVE or bound.mesh.kernel is not bound.kernel:
        return "node.mesh_admission_unavailable"
    if bound.owner_of() != bound.owner or owner != bound.owner:
        return "node.owner_mismatch"
    if chat != bound.chat:
        return "request.call_mismatch"
    return None


async def _turn_tool_code(orch, bound, agent, tool):
    from orchestrator.node_mesh_calls import current_definition

    if not isinstance(agent, str) or not agent or agent.startswith("__"):
        return "node.meta_tool_refused"
    definition = current_definition(orch, agent, tool)
    entry = (await bound.catalog()).get((agent, tool))
    if entry is None or definition is None:
        return "mint.unknown_tool"
    if (agent, tool, definition) in bound.tools and entry.definition == definition:
        return None
    if entry.definition != definition or any(key[:2] == (agent, tool) for key in bound.tools):
        return "gate.tool_def_changed"
    return "gate.permission_denied"


def _minted_arguments(args):
    return {key: value for key, value in args.items() if key not in _SERVER_ARGUMENTS}


async def _turn_entry(orch, bound, owner, chat, agent, tool, args):
    code = _turn_binding_code(bound, owner, chat) or await _turn_tool_code(orch, bound, agent, tool)
    if code is not None:
        return code
    try:
        ticket, resolved = await bound.mint(agent, tool, _minted_arguments(args), owner)
    except vessel_executor.TicketRefused as exc:
        return exc.code
    if (resolved.agent_id, resolved.tool) != (agent, tool) or not _covers(ticket, args, owner, chat):
        return "request.call_mismatch"
    await bound.mesh.verify_ticket(ticket.ticket_json, ticket.action_json, resolved.effect_kind)
    code = _turn_binding_code(bound, owner, chat) or await _turn_tool_code(orch, bound, agent, tool)
    if code is None:
        bound.held.append(_Held(agent, tool, ticket, resolved))
    return code


async def _turn_physical(orch, bound, owner, chat, agent, tool, args):
    code = _turn_binding_code(bound, owner, chat)
    if code is not None:
        return code, None
    held = next(
        (item for item in bound.held if (item.agent, item.tool) == (agent, tool) and _covers(item.ticket, args, owner, chat)),
        None,
    )
    if held is None:
        return "node.ticket_required", None
    bound.held.remove(held)
    for _ in range(2):
        await bound.mesh.verify_ticket(held.ticket.ticket_json, held.ticket.action_json, held.resolved.effect_kind)
        code = (
            _turn_binding_code(bound, owner, chat)
            or await _turn_tool_code(orch, bound, agent, tool)
            or _current_resolved(orch, owner, held.resolved)
        )
        if code is not None:
            return code, None
    return None, held.ticket


async def _refused(orch, websocket, owner, agent, tool, chat, args, code):
    await orch._audit_gate_denial(websocket, owner, agent, tool, chat, args, gate="node_mesh", detail=code)
    return MCPResponse(error={"code": code, "message": "Current mesh authority refused this call.",
                              "retryable": False, "effect_state": "not_started"})


async def _check_turn(orch, websocket, owner, chat, agent, tool, args, physical):
    from orchestrator.tool_permissions import turn_permission_memo

    with turn_permission_memo():
        return await _checked_turn(orch, websocket, owner, chat, agent, tool, args, physical)


async def _checked_turn(orch, websocket, owner, chat, agent, tool, args, physical):
    bound = _TURN.get()
    ticket = None
    try:
        if physical:
            code, ticket = await _turn_physical(orch, bound, owner, chat, agent, tool, args)
        else:
            code = await _turn_entry(orch, bound, owner, chat, agent, tool, args)
    except NodeMeshError as exc:
        code = exc.code
    except Exception:
        code = "node.mesh_admission_unavailable"
    if code is None:
        return None, ticket
    return await _refused(orch, websocket, owner, agent, tool, chat, args, code), None


async def dispatch(orch, websocket, owner, chat, agent, tool, args, invoke):
    if _CURRENT.get() is None and _in_turn(websocket):
        refusal, ticket = await _check_turn(orch, websocket, owner, chat, agent, tool, args, True)
        if refusal is not None:
            return refusal
        with vessel_executor.carrying(ticket):
            return await invoke()
    refusal = await check(orch, websocket, owner, chat, agent, tool, args, physical=True)
    return refusal if refusal is not None else await invoke()


async def check(orch, websocket, owner, chat, agent, tool, args, *, physical=False):
    admission = _CURRENT.get()
    if admission is None and _in_turn(websocket):
        if physical:
            return await _refused(orch, websocket, owner, agent, tool, chat, args, "node.ticket_required")
        refusal, _ = await _check_turn(orch, websocket, owner, chat, agent, tool, args, False)
        return refusal
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
    return await _refused(orch, websocket, owner, agent, tool, chat, args, code)
