"""Checks the admission of each tool call of a turn the node runs itself: the catalog and granted-tool checks before any mint, the exact-call ticket held from gate entry to the effect, and refusal whenever no such ticket exists.
The real kernel, orchestrator and dispatch path are exercised in test_node_mesh_turn_tools.py."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from orchestrator import node_mesh_admission as admission, vessel_executor
from orchestrator.node_mesh import NodeMeshError
from orchestrator.node_mesh_calls import CatalogTool, ResolvedTool, skill_definition

OWNER = "owner"
CHAT = "local-1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ticket_for(arguments, tool="lookup"):
    text = canonical(arguments)
    action = canonical({"tool": tool, "args_digest": "sha256:" + hashlib.sha256(text.encode()).hexdigest()})
    return vessel_executor.CallTicket('{"ticket":1}', action, text)


@pytest.fixture
def host():
    skill = SimpleNamespace(id="lookup", description="read", input_schema={}, metadata={})
    sibling = SimpleNamespace(id="peek", description="read too", input_schema={}, metadata={})
    definition = skill_definition(skill)
    resolved = ResolvedTool("agent", "lookup", "tools:read", definition)
    events = []
    state = SimpleNamespace(
        permission=True, minted=[], verified=0, verify_error=None, on_verify=None, mint_error=None,
        mint_arguments=None, resolved=resolved, owner=OWNER,
        catalog={
            ("agent", "lookup"): CatalogTool(definition, "outward_read"),
            ("agent", "peek"): CatalogTool(skill_definition(sibling), "outward_read"),
        },
    )

    class Mesh:
        kernel = object()

        async def verify_ticket(self, *_args):
            state.verified += 1
            if state.on_verify:
                state.on_verify(state.verified)
            if state.verify_error:
                raise state.verify_error
            return SimpleNamespace()

    async def audit(*args, **kwargs):
        events.append((args[2], kwargs["detail"]))

    orch = SimpleNamespace(
        security_flags={},
        agent_cards={"agent": SimpleNamespace(skills=[skill, sibling])},
        tool_permissions=SimpleNamespace(
            get_tool_scope=lambda *_args: "tools:read", is_tool_allowed=lambda *_args: state.permission,
        ),
        _audit_gate_denial=audit,
    )

    async def catalog():
        return dict(state.catalog)

    async def mint(agent, tool, arguments, owner):
        if state.mint_error:
            raise state.mint_error
        state.minted.append((agent, tool, arguments, owner))
        return ticket_for(state.mint_arguments if state.mint_arguments is not None else arguments, tool), state.resolved

    mesh = Mesh()
    tools = {("agent", "lookup", definition): "tools:read"}

    def bound():
        return admission.turn(mesh, lambda: state.owner, OWNER, CHAT, tools, catalog, mint)

    ran = []

    async def invoke():
        ran.append(vessel_executor.current_ticket())
        return "ran"

    async def call(arguments=None, *, agent="agent", tool="lookup", owner=OWNER, chat=CHAT, websocket=None):
        arguments = {"q": "x"} if arguments is None else arguments
        refusal = await admission.check(orch, websocket, owner, chat, agent, tool, arguments)
        if refusal is not None:
            return refusal
        return await admission.dispatch(orch, websocket, owner, chat, agent, tool, arguments, invoke)

    return SimpleNamespace(**locals())


def code(response):
    return response.error["code"]


async def test_a_granted_catalog_call_is_minted_once_and_runs_carrying_its_own_ticket(host):
    with host.bound():
        assert await host.call({"q": "x", "_credentials": "server"}) == "ran"
    assert host.state.minted == [("agent", "lookup", {"q": "x"}, OWNER)]
    assert host.ran == [ticket_for({"q": "x"})] and vessel_executor.current_ticket() is None
    assert host.state.verified == 3 and host.events == []


@pytest.mark.parametrize("agent,tool,expected", [
    ("__memory__", "memory_get", "node.meta_tool_refused"),
    (None, "lookup", "node.meta_tool_refused"),
    ("agent", "peek", "gate.permission_denied"),
    ("agent", "absent", "mint.unknown_tool"),
    ("other", "lookup", "mint.unknown_tool"),
])
async def test_calls_outside_the_granted_catalog_tools_are_refused_before_any_mint(host, agent, tool, expected):
    with host.bound():
        response = await host.call(agent=agent, tool=tool)
    assert code(response) == expected and host.state.minted == [] and host.ran == []
    assert host.events == [(agent, expected)]


async def test_a_changed_definition_is_refused_before_any_mint(host):
    host.skill.description = "changed after the grant"
    with host.bound():
        response = await host.call()
    assert code(response) == "gate.tool_def_changed" and host.state.minted == [] and host.ran == []


async def test_a_tool_dropped_from_the_catalog_is_refused(host):
    host.state.catalog.pop(("agent", "lookup"))
    with host.bound():
        assert code(await host.call()) == "mint.unknown_tool"
    assert host.state.minted == []


@pytest.mark.parametrize("change,expected", [
    ("owner", "node.owner_mismatch"),
    ("bridge_owner", "node.owner_mismatch"),
    ("chat", "request.call_mismatch"),
])
async def test_a_call_for_another_owner_or_chat_is_refused(host, change, expected):
    if change == "bridge_owner":
        host.state.owner = "someone-else"
    with host.bound():
        response = await host.call(owner="intruder" if change == "owner" else OWNER, chat="other" if change == "chat" else CHAT)
    assert code(response) == expected and host.state.minted == [] and host.ran == []


@pytest.mark.parametrize("failure,expected", [
    ("refused", "consent.required"),
    ("resolved", "request.call_mismatch"),
    ("arguments", "request.call_mismatch"),
    ("verify", "gate.mesh_stopped"),
    ("unavailable", "node.mesh_admission_unavailable"),
])
async def test_a_mint_or_ticket_failure_at_gate_entry_holds_nothing(host, failure, expected):
    if failure == "refused":
        host.state.mint_error = vessel_executor.TicketRefused("consent.required")
    if failure == "resolved":
        host.state.resolved = ResolvedTool("other", "lookup", "tools:read", host.definition)
    if failure == "arguments":
        host.state.mint_arguments = {"q": "something else"}
    if failure == "verify":
        host.state.verify_error = NodeMeshError("gate.mesh_stopped")
    if failure == "unavailable":
        host.state.mint_error = RuntimeError("binding failed")
    with host.bound() as bound:
        response = await host.call()
        assert bound.held == []
    assert code(response) == expected and host.ran == []


async def test_a_definition_changed_while_minting_is_refused_at_gate_entry(host):
    def change(count):
        host.skill.description = "changed while minting"

    host.state.on_verify = change
    with host.bound() as bound:
        assert code(await host.call()) == "gate.tool_def_changed"
        assert bound.held == []
    assert host.ran == []


async def test_physical_dispatch_without_a_held_ticket_never_runs(host):
    with host.bound():
        response = await admission.dispatch(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}, host.invoke)
        assert code(await admission.check(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}, physical=True)) == "node.ticket_required"
    assert code(response) == "node.ticket_required" and host.ran == []


async def test_a_ticket_held_for_other_arguments_or_another_tool_does_not_cover_the_call(host):
    with host.bound() as bound:
        assert await admission.check(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}) is None
        other = await admission.dispatch(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "y"}, host.invoke)
        sibling = await admission.dispatch(host.orch, None, OWNER, CHAT, "agent", "peek", {"q": "x"}, host.invoke)
        assert len(bound.held) == 1
    assert code(other) == code(sibling) == "node.ticket_required" and host.ran == []


async def test_each_held_ticket_is_used_once(host):
    with host.bound():
        assert await admission.check(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}) is None
        assert await admission.dispatch(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}, host.invoke) == "ran"
        again = await admission.dispatch(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}, host.invoke)
    assert code(again) == "node.ticket_required" and len(host.ran) == 1


@pytest.mark.parametrize("change,expected", [
    ("stop", "gate.mesh_stopped"),
    ("definition", "gate.tool_def_changed"),
    ("permission", "gate.permission_denied"),
    ("flag", "gate.hard_flag"),
    ("owner", "node.owner_mismatch"),
    ("catalog", "mint.unknown_tool"),
    ("late_stop", "gate.mesh_stopped"),
])
async def test_authority_lost_between_gate_entry_and_the_effect_refuses_the_effect(host, change, expected):
    with host.bound():
        assert await admission.check(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}) is None
        if change == "stop":
            host.state.verify_error = NodeMeshError("gate.mesh_stopped")
        if change == "definition":
            host.skill.description = "changed after entry"
        if change == "permission":
            host.state.permission = False
        if change == "flag":
            host.orch.security_flags = {"agent": {"lookup": {"blocked": True}}}
        if change == "owner":
            host.state.owner = "someone-else"
        if change == "catalog":
            host.state.catalog.clear()
        if change == "late_stop":
            entered = host.state.verified

            def stop_on_second(count):
                if count == entered + 2:
                    host.state.verify_error = NodeMeshError("gate.mesh_stopped")

            host.state.on_verify = stop_on_second
        response = await admission.dispatch(host.orch, None, OWNER, CHAT, "agent", "lookup", {"q": "x"}, host.invoke)
    assert code(response) == expected and host.ran == []
    assert host.events[-1] == ("agent", expected)


async def test_a_marked_turn_socket_without_a_bound_turn_is_refused():
    socket = SimpleNamespace(**{admission.TURN_MARK: True})
    audited = []

    async def audit(*args, **kwargs):
        audited.append(kwargs["detail"])

    orch = SimpleNamespace(_audit_gate_denial=audit)
    ran = []

    async def invoke():
        ran.append(True)

    entry = await admission.check(orch, socket, OWNER, CHAT, "agent", "lookup", {})
    effect = await admission.dispatch(orch, socket, OWNER, CHAT, "agent", "lookup", {}, invoke)
    assert code(entry) == code(effect) == "node.mesh_admission_unavailable" and not ran
    assert audited == ["node.mesh_admission_unavailable"] * 2


async def test_a_turn_that_ended_refuses_a_late_call(host):
    with host.bound() as bound:
        pass
    socket = SimpleNamespace(**{admission.TURN_MARK: True})
    token = admission._TURN.set(bound)
    try:
        response = await host.call(websocket=socket)
    finally:
        admission._TURN.reset(token)
    assert code(response) == "node.mesh_admission_unavailable" and host.state.minted == []


async def test_dispatch_outside_any_mesh_context_runs_unchanged(host):
    assert await admission.dispatch(host.orch, None, OWNER, CHAT, "agent", "lookup", {}, host.invoke) == "ran"
    assert host.ran == [None] and host.events == []


async def test_turn_scopes_and_tools_are_sorted_and_exact(host):
    with host.bound() as bound:
        assert bound.scopes == ["tools:read"]
        assert bound.allowed == (("agent", "lookup", host.definition),)
        assert admission.current_turn() is bound
    assert admission.current_turn() is None
