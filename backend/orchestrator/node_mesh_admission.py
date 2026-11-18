"""Keeps one incoming peer call, or every tool call of a turn the node runs itself, bound to its verified mesh and current physical authority.
A call of the node's own turn must be in the published catalog and the turn's granted tools, and holds a kernel ticket minted for exactly that call; without one it never dispatches.
The chat bridge owns both context lifetimes, and protected dispatch in orchestrator.py rechecks them at gate entry and before the effect."""

from __future__ import annotations

import contextlib
import contextvars
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

from orchestrator import vessel_executor
from orchestrator.hitl_confirmation import _arguments, _SERVER_ARGUMENTS
from orchestrator.node_mesh import NodeMeshError, RequestReference
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
    request: Optional[RequestReference] = None
    check_current: Optional[Callable[[], Awaitable[None]]] = None


@dataclass(frozen=True)
class KernelEffectCapability:
    owner: str
    chat: str
    agent: str
    tool: str
    request_id: str
    mesh_id: str
    device_id: str
    request_device: str
    action_digest: str
    ticket_digest: str
    arguments_digest: str
    consented: bool


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
    request: Optional[RequestReference] = None
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
_USED_EFFECTS: set[object] = set()
_RESERVED_EFFECTS: Dict[object, asyncio.Task] = {}


@contextlib.contextmanager
def binding(mesh, owner_of, owner, chat, resolved, ticket, *, request=None, check_current=None) -> Iterator[None]:
    lease = object()
    reference = None if request is None else RequestReference.capture(request)
    admission = Admission(mesh, mesh.kernel, owner_of, owner, chat, resolved, ticket, lease, reference, check_current)
    _ACTIVE.add(lease)
    token = _CURRENT.set(admission)
    try:
        yield
    finally:
        _ACTIVE.discard(lease)
        _USED_EFFECTS.discard(lease)
        _RESERVED_EFFECTS.pop(lease, None)
        _CURRENT.reset(token)


@contextlib.contextmanager
def turn(mesh, owner_of, owner, chat, tools, catalog, mint, *, request=None) -> Iterator[Turn]:
    lease = object()
    reference = None if request is None else RequestReference.capture(request)
    bound = Turn(mesh, mesh.kernel, owner_of, owner, chat, dict(tools), catalog, mint, lease, reference)
    _ACTIVE.add(lease)
    token = _TURN.set(bound)
    try:
        yield bound
    finally:
        _ACTIVE.discard(lease)
        _TURN.reset(token)


def current_turn() -> Optional[Turn]:
    return _TURN.get()


def current_admission() -> Optional[Admission]:
    return _CURRENT.get()


def _bound_mesh_owner() -> Optional[str]:
    from shared import node_identity

    try:
        return node_identity.bound_mesh_owner()
    except node_identity.NodeIdentityError:
        return None


async def personal_authority(orch, owner: str, *, agent_id: Optional[str] = None):
    from orchestrator.chain_authority import MachineAuthority
    from shared import node_identity

    if not node_identity.is_personal():
        raise NodeMeshError("node.personal_mesh_required")
    bound = _CURRENT.get() if agent_id is not None else _TURN.get()

    def current():
        if _bound_mesh_owner() != owner:
            return "node.owner_mismatch"
        if agent_id is None:
            return _turn_binding_code(bound, owner, bound.chat if bound is not None else None)
        code = _binding_code(bound, vessel_executor.current_ticket(), owner)
        if code is None and bound.resolved.agent_id != agent_id:
            code = "request.call_mismatch"
        return code

    code = current()
    if code is not None:
        raise NodeMeshError(code)
    if bound.request is None or (bound.request.call is None) != (agent_id is None):
        raise NodeMeshError("request.call_mismatch")
    context = await bound.mesh.personal_authority_context(bound.request, owner, bound.kernel)
    code = current()
    if agent_id is None:
        tools, scopes = bound.allowed, bound.scopes
    else:
        ticket = bound.ticket
        if bound.request.call[2:] != (ticket.arguments_json, ticket.action_json, ticket.ticket_json):
            raise NodeMeshError("request.call_mismatch")
        await bound.mesh.verify_ticket(ticket.ticket_json, ticket.action_json, bound.resolved.effect_kind)
        code = current() or _current_resolved(orch, owner, bound.resolved)
        tools = ((agent_id, bound.resolved.tool, bound.resolved.definition),)
        scopes = [bound.resolved.scope]
    if code is not None:
        raise NodeMeshError(code)
    consent = f"mesh:{context.mesh_id}/request:{bound.request.id}"
    try:
        identity = node_identity.load_node_identity(create=False)
        credential = identity.mint_token({
            "sub": owner, "act": {"sub": "machine:mesh_request"}, "scope": " ".join(scopes),
            "consent_ref": consent,
            "mesh_authority": {
                "mesh": context.mesh_id, "member": context.member_id, "request": bound.request.id,
                "chat": bound.request.chat_id, "local_chat": bound.chat, "source": bound.request.from_device,
                "tools": [list(tool) for tool in tools],
            },
        }, lifetime=node_identity.MAX_TOKEN_SECONDS)
    except node_identity.NodeIdentityError:
        raise NodeMeshError("node.signing_key_unavailable") from None
    return MachineAuthority(
        access_token=credential, allowed_scopes=scopes, principal="machine:mesh_request", user_id=owner,
        consent_ref=consent, turn_class="mesh_request", agent_id=agent_id,
        allowed_tools=tools if agent_id is None else None,
    )


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
    if admission.lease in _USED_EFFECTS:
        return "node.ticket_used"
    if admission.owner_of() != admission.owner or owner != admission.owner:
        return "node.owner_mismatch"
    return None


async def source_bound_capability(orch, owner, chat, agent, tool, args):
    from shared import node_identity

    if (not node_identity.is_personal()
            or not isinstance(owner, str) or not owner
            or not isinstance(chat, str) or not chat or not isinstance(agent, str) or not agent
            or not isinstance(tool, str) or not tool or not isinstance(args, dict)):
        return None
    admission = _CURRENT.get()
    task = asyncio.current_task()
    if admission is None or task is None:
        return None
    ticket = vessel_executor.current_ticket()
    if (_binding_code(admission, ticket, owner) is not None
            or admission.request is None or admission.request.call is None
            or admission.request.call[2:] != (ticket.arguments_json, ticket.action_json, ticket.ticket_json)
            or not _matches(admission, owner, chat, agent, tool, args)
            or admission.resolved.effect_kind != "outside_effect"
            or _current_tool(orch, admission) is not None):
        return None
    reserved_task = _RESERVED_EFFECTS.get(admission.lease)
    if reserved_task is not None and reserved_task is not task:
        return None
    _RESERVED_EFFECTS[admission.lease] = task
    mesh, request, resolved = admission.mesh, admission.request, admission.resolved
    capability = None
    try:
        verified = await mesh.verify_ticket(ticket.ticket_json, ticket.action_json, resolved.effect_kind)
        checked = await mesh.check_host_call(request.id, resolved.definition, resolved.effect_kind)
        context = await mesh.personal_authority_context(request, owner, mesh.kernel)
        if admission.check_current is not None:
            await admission.check_current()
        action = json.loads(ticket.action_json)
        if (mesh.kernel is not admission.kernel
                or (node_identity.is_personal() and _bound_mesh_owner() != owner)
                or RequestReference.capture(context.request) != request
                or verified.mesh != context.mesh_id or verified.turn != action.get("turn")
                or action.get("tool") != resolved.tool or action.get("tool_def") != resolved.definition
                or verified.action_digest == "" or checked.action_digest != verified.action_digest
                or checked.mesh != verified.mesh or _binding_code(admission, ticket, owner) is not None
                or _RESERVED_EFFECTS.get(admission.lease) is not task
                or not _matches(admission, owner, chat, agent, tool, args)
                or _current_tool(orch, admission) is not None):
            return None
        capability = KernelEffectCapability(
            owner, chat, agent, tool, request.id, verified.mesh, verified.device,
            request.from_device, verified.action_digest,
            "sha256:" + hashlib.sha256(ticket.ticket_json.encode("utf-8")).hexdigest(),
            "sha256:" + hashlib.sha256(ticket.arguments_json.encode("utf-8")).hexdigest(),
            bool(verified.consented),
        )
        return capability
    except Exception:
        return None
    finally:
        if capability is None and _RESERVED_EFFECTS.get(admission.lease) is task:
            _RESERVED_EFFECTS.pop(admission.lease, None)


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
        code = await _turn_tool_code(orch, bound, agent, tool)
        if code is not None:
            return code, None
        await bound.mesh.verify_ticket(held.ticket.ticket_json, held.ticket.action_json, held.resolved.effect_kind)
        code = (
            _turn_binding_code(bound, owner, chat)
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


async def dispatch(orch, websocket, owner, chat, agent, tool, args, invoke, *, kernel_effect=None):
    if _CURRENT.get() is None and _in_turn(websocket):
        refusal, ticket = await _check_turn(orch, websocket, owner, chat, agent, tool, args, True)
        if refusal is not None:
            return refusal
        with vessel_executor.carrying(ticket):
            return await invoke()
    refusal = await check(orch, websocket, owner, chat, agent, tool, args, physical=True)
    if refusal is not None:
        return refusal
    admission = _CURRENT.get()
    if admission is not None:
        task = asyncio.current_task()
        reserved_task = _RESERVED_EFFECTS.get(admission.lease)
        if reserved_task is not None and reserved_task is not task:
            return await _refused(orch, websocket, owner, chat, agent, tool, args, "node.ticket_in_use")
        if kernel_effect is not None:
            try:
                if (reserved_task is not task or admission.request is None
                        or kernel_effect.request_id != admission.request.id
                        or kernel_effect.owner != owner or kernel_effect.chat != chat
                        or kernel_effect.agent != agent or kernel_effect.tool != tool):
                    raise NodeMeshError("node.mesh_admission_unavailable")
                code = _binding_code(admission, vessel_executor.current_ticket(), owner)
                if code is None:
                    code = _current_tool(orch, admission)
                if code is not None:
                    raise NodeMeshError(code)
                checked = await admission.mesh.check_host_call(
                    admission.request.id, admission.resolved.definition, admission.resolved.effect_kind)
                context = await admission.mesh.personal_authority_context(
                    admission.request, owner, admission.kernel)
                if admission.check_current is not None:
                    await admission.check_current()
                if (checked.action_digest != kernel_effect.action_digest
                        or checked.mesh != kernel_effect.mesh_id
                        or RequestReference.capture(context.request) != admission.request
                        or _binding_code(admission, vessel_executor.current_ticket(), owner) is not None
                        or _current_tool(orch, admission) is not None
                        or _RESERVED_EFFECTS.get(admission.lease) is not task):
                    raise NodeMeshError("request.call_mismatch")
                claimed = await admission.mesh.claim_host_call(
                    admission.request.id, admission.resolved.definition, admission.resolved.effect_kind)
                if claimed.action_digest != kernel_effect.action_digest or claimed.mesh != kernel_effect.mesh_id:
                    raise NodeMeshError("node.ticket_used")
            except NodeMeshError as exc:
                return await _refused(orch, websocket, owner, chat, agent, tool, args, exc.code)
            except Exception:
                return await _refused(orch, websocket, owner, chat, agent, tool, args,
                                      "node.mesh_admission_unavailable")
        _USED_EFFECTS.add(admission.lease)
        _RESERVED_EFFECTS.pop(admission.lease, None)
    return await invoke()


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
