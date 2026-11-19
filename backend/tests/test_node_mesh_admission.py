"""Checks the lifetime, exact argument and current-target boundaries of incoming mesh admission and the machine credential minted for a bound call.
Real signed request and physical-dispatch regressions are in test_node_mesh_final_admission.py.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import node_mesh_admission as admission, vessel_executor
from orchestrator.hitl_confirmation import _arguments
from orchestrator.node_mesh import NodeMeshError, RequestReference
from orchestrator.node_mesh_calls import ResolvedTool, skill_definition


@pytest.fixture
def runtime(request):
    events = []
    owners = ["owner"]
    permission = [True]
    skill = SimpleNamespace(id="lookup", description="read", input_schema={}, metadata={})
    resolved = ResolvedTool("agent", "lookup", "tools:read", skill_definition(skill))
    original = {"q": "café", "_owner_value": True, **getattr(request, "param", {})}
    raw = _arguments(original)
    action = _arguments({"args_digest": "sha256:" + hashlib.sha256(raw.encode()).hexdigest()})
    ticket = vessel_executor.CallTicket("{}", action, raw)

    class Mesh:
        kernel = object()
        calls = 0
        mutation = None
        error = None

        async def verify_ticket(self, *_args):
            self.calls += 1
            if self.mutation:
                self.mutation(self.calls)
            if self.error:
                raise self.error
            return SimpleNamespace()

    async def audit(*_args, **kwargs):
        events.append(kwargs)

    orch = SimpleNamespace(
        security_flags={}, agent_cards={"agent": SimpleNamespace(skills=[skill])},
        tool_permissions=SimpleNamespace(
            get_tool_scope=lambda *_args: "tools:read", is_tool_allowed=lambda *_args: permission[0],
        ),
        _audit_gate_denial=audit,
    )
    mesh = Mesh()
    websocket = SimpleNamespace()

    @contextmanager
    def bound():
        with admission.binding(mesh, lambda: owners[0], "owner", "chat", resolved, ticket):
            with vessel_executor.carrying(ticket):
                yield

    async def check(**kwargs):
        return await admission.check(
            orch, websocket, kwargs.get("owner", "owner"), kwargs.get("chat", "chat"),
            kwargs.get("agent", "agent"), kwargs.get("tool", "lookup"), kwargs.get("args", original),
            physical=kwargs.get("physical", True),
        )

    return SimpleNamespace(**locals())


@pytest.mark.parametrize("socket", [SimpleNamespace, MagicMock, AsyncMock])
async def test_ordinary_dispatch_without_mesh_context_is_unchanged(runtime, socket):
    assert await admission.check(runtime.orch, socket(), "owner", "chat", "agent", "lookup", runtime.original,
                                 physical=True) is None
    assert runtime.events == []


@pytest.mark.parametrize("context", ["admission", "ticket"])
async def test_partial_authority_is_not_ignored_when_the_peer_marker_is_false(runtime, context):
    bound = (admission.binding(runtime.mesh, lambda: "owner", "owner", "chat", runtime.resolved, runtime.ticket)
             if context == "admission" else vessel_executor.carrying(runtime.ticket))
    with bound:
        result = await admission.check(runtime.orch, MagicMock(_node_mesh_peer_call=False), "owner", "chat",
                                       "agent", "lookup", runtime.original, physical=True)
    assert result is not None and result.error["code"] == "node.mesh_admission_unavailable"
    assert runtime.events[-1]["detail"] == result.error["code"]
    assert runtime.mesh.calls == 0


async def test_a_carried_ticket_requires_a_live_trusted_binding(runtime):
    with vessel_executor.carrying(runtime.ticket):
        result = await runtime.check()
    assert result.error["code"] == "node.mesh_admission_unavailable"
    assert runtime.events[-1]["detail"] == result.error["code"]


async def test_server_metadata_preserves_the_exact_owner_arguments(runtime):
    with runtime.bound():
        assert await runtime.check(args={**runtime.original, "_delegation_token": "server", "user_id": "owner", "session_id": "chat"}) is None
    assert runtime.mesh.calls == 2


@pytest.mark.parametrize("runtime", [{"_txn_token": "policy-credential"}], indirect=True)
async def test_consuming_a_credential_cannot_change_the_arguments_covered_by_a_ticket(runtime):
    prepared = dict(runtime.original)
    prepared.pop("_txn_token")
    with runtime.bound():
        assert await runtime.check() is None
        refused = await runtime.check(args=prepared)
    assert refused.error["code"] == "request.call_mismatch"


@pytest.mark.parametrize("change,code", [
    ("owner", "node.owner_mismatch"), ("chat", "request.call_mismatch"),
    ("agent", "request.call_mismatch"), ("tool", "request.call_mismatch"),
    ("args", "request.call_mismatch"),
])
async def test_the_binding_cannot_be_reused_for_another_target(runtime, change, code):
    with runtime.bound():
        result = await runtime.check(**{change: {} if change == "args" else "other"})
    assert result.error["code"] == code
    assert runtime.mesh.calls == 0


@pytest.mark.parametrize("args", [
    {"q": "café", "_owner_value": 1},
    {"q": "café", "_owner_value": True, "_unreviewed": "added"},
    {"q": "café", "_owner_value": True, "_txn_token": "policy-credential"},
    {"q": "café", "_owner_value": True, "user_id": "foreign"},
])
async def test_owner_underscore_values_and_unknown_metadata_are_not_ignored(runtime, args):
    with runtime.bound():
        result = await runtime.check(args=args)
    assert result.error["code"] == "request.call_mismatch"


@pytest.mark.parametrize("change,code", [
    ("hard_flag", "gate.hard_flag"), ("missing", "gate.tool_def_changed"),
    ("definition", "gate.tool_def_changed"), ("scope", "gate.permission_denied"),
    ("permission", "gate.permission_denied"),
])
async def test_the_current_physical_tool_remains_required(runtime, change, code):
    if change == "hard_flag":
        runtime.orch.security_flags = {"agent": {"lookup": {"blocked": True}}}
    elif change == "missing":
        runtime.orch.agent_cards.clear()
    elif change == "definition":
        runtime.skill.description = "changed"
    elif change == "scope":
        runtime.orch.tool_permissions.get_tool_scope = lambda *_args: "tools:write"
    else:
        runtime.permission[0] = False
    with runtime.bound():
        result = await runtime.check()
    assert result.error["code"] == code


@pytest.mark.parametrize("error,code", [
    (NodeMeshError("ticket.expired"), "ticket.expired"),
    (RuntimeError("private diagnostic"), "node.mesh_admission_unavailable"),
])
async def test_current_kernel_verification_fails_closed(runtime, error, code):
    runtime.mesh.error = error
    with runtime.bound():
        result = await runtime.check()
    assert result.error["code"] == code
    assert "private diagnostic" not in str(result)


async def test_inherited_tasks_lose_authority_when_the_binding_closes(runtime):
    release = asyncio.Event()
    async def later():
        await release.wait()
        return await runtime.check()
    with runtime.bound():
        task = asyncio.create_task(later())
    release.set()
    result = await task
    assert result.error["code"] == "node.mesh_admission_unavailable"


async def test_binding_expiry_during_kernel_verification_cannot_dispatch(runtime):
    exit_binding = runtime.bound()
    exit_binding.__enter__()
    runtime.mesh.mutation = lambda calls: exit_binding.__exit__(None, None, None) if calls == 1 else None
    result = await runtime.check()
    assert result is not None and result.error["code"] == "node.mesh_admission_unavailable"


@pytest.mark.parametrize("change,code", [
    ("owner", "node.owner_mismatch"), ("kernel", "node.mesh_admission_unavailable"),
    ("permission", "gate.permission_denied"), ("definition", "gate.tool_def_changed"),
])
async def test_mutation_during_final_kernel_verification_is_rechecked(runtime, change, code):
    def mutate(calls):
        if calls != 2:
            return
        if change == "owner":
            runtime.owners[0] = "other"
        elif change == "kernel":
            runtime.mesh.kernel = object()
        elif change == "permission":
            runtime.permission[0] = False
        else:
            runtime.skill.description = "changed"
    runtime.mesh.mutation = mutate
    with runtime.bound():
        result = await runtime.check()
    assert result is not None and result.error["code"] == code


async def test_machine_peer_dispatch_cannot_drop_both_ticket_and_binding(runtime):
    runtime.websocket._node_mesh_peer_call = True
    result = await runtime.check()
    assert result.error["code"] == "node.mesh_admission_unavailable"


async def test_arguments_changed_during_final_verification_are_refused(runtime):
    arguments = dict(runtime.original)
    def mutate(calls):
        if calls == 2:
            arguments["q"] = "changed"
    runtime.mesh.mutation = mutate
    with runtime.bound():
        result = await runtime.check(args=arguments)
    assert result is not None and result.error["code"] == "request.call_mismatch"


@pytest.fixture
def personal(runtime, monkeypatch):
    from shared import node_identity

    minted = []

    class Identity:
        def mint_token(self, claims, lifetime):
            minted.append((claims, lifetime))
            return "signed-token"

    monkeypatch.setattr(node_identity, "is_personal", lambda: True)
    monkeypatch.setattr(node_identity, "bound_mesh_owner", lambda: "owner")
    monkeypatch.setattr(node_identity, "load_node_identity", lambda create=True: Identity())
    ticket = runtime.ticket
    reference = RequestReference(
        "r1", "chat", "dev-phone", "dev-node", "do it", None,
        ("call-1", "lookup", ticket.arguments_json, ticket.action_json, ticket.ticket_json),
    )
    runtime.mesh.personal_authority_context = AsyncMock(return_value=SimpleNamespace(mesh_id="mesh-1", member_id="dev-node"))

    @contextmanager
    def bound(request=reference, ticket=ticket):
        with admission.binding(runtime.mesh, lambda: runtime.owners[0], "owner", "chat", runtime.resolved, ticket, request=request):
            with vessel_executor.carrying(ticket):
                yield

    async def authority(owner="owner", agent="agent"):
        return await admission.personal_authority(runtime.orch, owner, agent_id=agent)

    return SimpleNamespace(minted=minted, bound=bound, reference=reference, authority=authority, node_identity=node_identity)


async def test_personal_authority_mints_a_credential_for_exactly_the_called_tool(runtime, personal):
    with personal.bound():
        result = await personal.authority()
    assert result.allowed_tools is None and result.allowed_scopes == ["tools:read"] and result.agent_id == "agent"
    assert (result.access_token, result.principal, result.user_id, result.turn_class) == (
        "signed-token", "machine:mesh_request", "owner", "mesh_request",
    )
    assert result.consent_ref == "mesh:mesh-1/request:r1"
    claims, lifetime = personal.minted[0]
    assert lifetime == personal.node_identity.MAX_TOKEN_SECONDS
    assert (claims["sub"], claims["act"], claims["scope"], claims["consent_ref"]) == (
        "owner", {"sub": "machine:mesh_request"}, "tools:read", "mesh:mesh-1/request:r1",
    )
    assert claims["mesh_authority"] == {
        "mesh": "mesh-1", "member": "dev-node", "request": "r1", "chat": "chat", "local_chat": "chat",
        "source": "dev-phone", "tools": [["agent", "lookup", runtime.resolved.definition]],
    }
    assert runtime.mesh.calls == 1


async def test_personal_authority_needs_a_personal_node(runtime, personal, monkeypatch):
    monkeypatch.setattr(personal.node_identity, "is_personal", lambda: False)
    with personal.bound(), pytest.raises(NodeMeshError) as refused:
        await personal.authority()
    assert refused.value.code == "node.personal_mesh_required" and not personal.minted


async def test_personal_authority_needs_a_live_binding(runtime, personal):
    with pytest.raises(NodeMeshError) as refused:
        await personal.authority()
    assert refused.value.code == "node.mesh_admission_unavailable" and not personal.minted


@pytest.mark.parametrize("failure", ["foreign_owner", "other_mesh_owner", "unbound_mesh_owner"])
async def test_personal_authority_refuses_an_owner_the_mesh_does_not_bind(runtime, personal, monkeypatch, failure):
    if failure == "other_mesh_owner":
        monkeypatch.setattr(personal.node_identity, "bound_mesh_owner", lambda: "other")
    if failure == "unbound_mesh_owner":
        def unbound():
            raise personal.node_identity.NodeIdentityError("unbound")

        monkeypatch.setattr(personal.node_identity, "bound_mesh_owner", unbound)
    with personal.bound(), pytest.raises(NodeMeshError) as refused:
        await personal.authority(owner="other" if failure == "foreign_owner" else "owner")
    assert refused.value.code == "node.owner_mismatch" and not personal.minted


async def test_personal_authority_refuses_another_agents_tool(runtime, personal):
    with personal.bound(), pytest.raises(NodeMeshError) as refused:
        await personal.authority(agent="other")
    assert refused.value.code == "request.call_mismatch" and not personal.minted


@pytest.mark.parametrize("kind", ["none", "text", "other_ticket"])
async def test_personal_authority_needs_the_exact_call_request_it_was_bound_with(runtime, personal, kind):
    request = personal.reference
    if kind == "none":
        request = None
    if kind == "text":
        request = RequestReference("r1", "chat", "dev-phone", "dev-node", "do it", None, None)
    if kind == "other_ticket":
        request = RequestReference(
            "r1", "chat", "dev-phone", "dev-node", "do it", None,
            ("call-1", "lookup", personal.reference.call[2], personal.reference.call[3], '{"other":1}'),
        )
    with personal.bound(request), pytest.raises(NodeMeshError) as refused:
        await personal.authority()
    assert refused.value.code == "request.call_mismatch" and not personal.minted and runtime.mesh.calls == 0


async def test_personal_authority_passes_on_the_kernels_refusals(runtime, personal):
    runtime.mesh.personal_authority_context.side_effect = NodeMeshError("gate.mesh_stopped")
    with personal.bound(), pytest.raises(NodeMeshError) as stopped:
        await personal.authority()
    assert stopped.value.code == "gate.mesh_stopped" and not personal.minted
    runtime.mesh.personal_authority_context.side_effect = None
    runtime.mesh.error = NodeMeshError("ticket.stopped")
    with personal.bound(), pytest.raises(NodeMeshError) as refused:
        await personal.authority()
    assert refused.value.code == "ticket.stopped" and not personal.minted


@pytest.mark.parametrize("change,code", [("permission", "gate.permission_denied"), ("owner", "node.owner_mismatch")])
async def test_personal_authority_is_refused_when_authority_is_lost_during_verification(runtime, personal, change, code):
    def mutate(calls):
        if change == "permission":
            runtime.permission[0] = False
        else:
            runtime.owners[0] = "someone.else"

    runtime.mesh.mutation = mutate
    with personal.bound(), pytest.raises(NodeMeshError) as refused:
        await personal.authority()
    assert refused.value.code == code and not personal.minted


async def test_personal_authority_reports_an_unavailable_signing_key(runtime, personal, monkeypatch):
    def missing(create=True):
        raise personal.node_identity.NodeIdentityError("no key")

    monkeypatch.setattr(personal.node_identity, "load_node_identity", missing)
    with personal.bound(), pytest.raises(NodeMeshError) as refused:
        await personal.authority()
    assert refused.value.code == "node.signing_key_unavailable"
