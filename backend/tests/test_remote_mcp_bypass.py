"""Checks a computer host's bypass switch approves remote MCP connector calls only from that host's own socket, with the review, taint and privacy floors kept."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator import hitl_confirmation as confirmation
from orchestrator.hitl import CROSS_PRINCIPAL, UNTRUSTED_TAINTED
from shared.protocol import MCPResponse


CONNECTOR_ID = "1" * 32
RISKS = ("source_bound", "privileged", "tools:execute", "origin:signed_human")


class ConnectorStore:
    def __init__(self):
        self.records = {
            ("owner-a", "__remote_mcp_connections__", "remote_mcp_" + CONNECTOR_ID): json.dumps({
                "id": CONNECTOR_ID, "name": "Synthetic connection", "url": "https://mcp.example/mcp",
                "transport": "streamable_http", "auth_type": "none", "enabled": True,
                "tools": {"echo": True}, "tools_catalog": [{"name": "echo"}],
            }),
        }

    def get_credential(self, owner, agent, key):
        return self.records.get((owner, agent, key))


class Hosts:
    def __init__(self):
        self.by_socket = {}

    def host_for_socket(self, websocket):
        return self.by_socket.get(id(websocket))


class Sessions:
    def __init__(self):
        self.bypass = set()

    def bypass_on(self, owner, host_id):
        return (owner, host_id) in self.bypass


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr("personalization.phi_policy.protection_enabled", lambda *_args: False)
    mac = object()
    events = []
    executed = []

    async def record(event):
        events.append(event)
        return SimpleNamespace(event_id="receipt")

    orch = SimpleNamespace(
        credential_manager=ConnectorStore(), ui_sessions={mac: {"sub": "owner-a"}},
        _ws_active_chat={id(mac): "chat-a"},
        _map_file_paths=lambda _chat, args, **_kwargs: args,
        audit_recorder=SimpleNamespace(record=record),
        computer_hosts=Hosts(), computer_sessions=Sessions(),
    )
    orch.computer_hosts.by_socket[id(mac)] = SimpleNamespace(owner_sub="owner-a", host_id="mac-1", websocket=mac)
    orch.computer_sessions.bypass.add(("owner-a", "mac-1"))

    async def execute_single_tool(websocket, tool_call, agents, chat, *, user_id):
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)
        hook = getattr(orch, "before_effect", None)
        if hook is not None:
            await hook()
        pending = await confirmation.evaluate(orch, websocket, user_id, chat, agents[name], name, args,
                                              orch.reentry_risks)
        if pending is not None:
            return pending
        executed.append(args)
        return MCPResponse(result={"_ui_components": [], "_data": {"stats": {"rows": 3}}})

    orch.execute_single_tool = execute_single_tool
    orch.reentry_risks = RISKS
    confirmation.begin_task(orch, mac, "owner-a", "chat-a")
    return SimpleNamespace(orch=orch, mac=mac, events=events, executed=executed)


def call_arguments(**arguments):
    return {"connector_id": CONNECTOR_ID, "tool_name": "echo", "arguments": arguments or {"text": "synthetic"}}


async def evaluate(world, args, *, socket=None, owner="owner-a", agent="remote-mcp-1", tool="call_remote_tool",
                   risks=RISKS):
    return await confirmation.evaluate(world.orch, socket or world.mac, owner, "chat-a", agent, tool, args, risks)


def actions(world):
    return [event.action_type for event in world.events]


def assert_card(world, response):
    assert response.error is None
    assert response.result["_data"]["status"] == "confirmation_required"
    assert world.executed == []
    assert actions(world) == ["hitl.proposed"]


@pytest.mark.asyncio
async def test_bypass_on_the_requesting_mac_runs_the_connector_call_without_a_card(world):
    args = call_arguments()

    response = await evaluate(world, args)

    assert response.error is None
    assert response.result["_data"] == {"stats": {"rows": 3}, "approval_source": "bypass"}
    assert world.executed == [args]
    assert actions(world) == ["hitl.bypass", "hitl.consumed"]
    assert world.events[0].inputs_meta["approval_source"] == "bypass"
    assert not getattr(world.orch, "_hitl_pending_calls", {})


@pytest.mark.asyncio
async def test_bypass_off_keeps_the_review_card(world):
    world.orch.computer_sessions.bypass.clear()
    assert_card(world, await evaluate(world, call_arguments()))


@pytest.mark.asyncio
async def test_bypass_on_another_computer_does_not_cover_a_chat_from_this_socket(world):
    web = object()
    world.orch.ui_sessions[web] = {"sub": "owner-a"}
    world.orch._ws_active_chat[id(web)] = "chat-a"
    confirmation.begin_task(world.orch, web, "owner-a", "chat-a")

    response = await evaluate(world, call_arguments(), socket=web)

    assert_card(world, response)


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [
    SimpleNamespace(owner_sub="owner-b", host_id="mac-1"),
    SimpleNamespace(owner_sub="owner-a", host_id="mac-1", websocket=object()),
])
async def test_host_of_another_owner_or_socket_never_carries_bypass(world, host):
    if not hasattr(host, "websocket"):
        host.websocket = world.mac
    world.orch.computer_hosts.by_socket[id(world.mac)] = host
    world.orch.computer_sessions.bypass.add(("owner-b", "mac-1"))
    assert_card(world, await evaluate(world, call_arguments()))


@pytest.mark.asyncio
@pytest.mark.parametrize("risk", [UNTRUSTED_TAINTED, CROSS_PRINCIPAL])
async def test_untrusted_or_cross_principal_calls_still_ask(world, risk):
    assert_card(world, await evaluate(world, call_arguments(), risks=(*RISKS, risk)))


@pytest.mark.asyncio
async def test_tainted_sink_still_asks(world, monkeypatch):
    monkeypatch.setattr(confirmation, "_tainted_sink", lambda *_args: True)
    assert_card(world, await evaluate(world, call_arguments()))


@pytest.mark.asyncio
async def test_private_arguments_stay_refused_under_bypass(world):
    response = await evaluate(world, call_arguments(text="password: private-value"))
    assert response.error["code"] == "ACTION_REVIEW_UNAVAILABLE"
    assert world.executed == []
    assert world.events == []


@pytest.mark.asyncio
async def test_oversized_arguments_run_under_bypass_but_not_without_it(world):
    args = call_arguments(text="word " * 1200)

    response = await evaluate(world, args)
    assert response.error is None
    assert world.executed == [args]

    world.orch.computer_sessions.bypass.clear()
    world.executed.clear()
    refused = await evaluate(world, args)
    assert refused.error["code"] == "ACTION_REVIEW_UNAVAILABLE"
    assert world.executed == []


@pytest.mark.asyncio
async def test_disabled_connector_tool_is_refused_even_with_bypass(world):
    key = ("owner-a", "__remote_mcp_connections__", "remote_mcp_" + CONNECTOR_ID)
    connector = json.loads(world.orch.credential_manager.records[key])
    world.orch.credential_manager.records[key] = json.dumps({**connector, "tools": {"echo": False}})

    response = await evaluate(world, call_arguments())

    assert response.error["code"] == "REMOTE_MCP_REVIEW_UNAVAILABLE"
    assert world.executed == []
    assert world.events == []


@pytest.mark.asyncio
async def test_bypass_turned_off_before_the_effect_runs_nothing(world):
    async def revoke():
        world.orch.computer_sessions.bypass.clear()

    world.orch.before_effect = revoke

    response = await evaluate(world, call_arguments())

    assert response.error is not None
    assert world.executed == []
    assert actions(world) == ["hitl.bypass"]


@pytest.mark.asyncio
async def test_chat_that_is_no_longer_active_on_the_mac_runs_nothing(world):
    world.orch._ws_active_chat[id(world.mac)] = "chat-b"

    response = await evaluate(world, call_arguments())

    assert response.error["message"] == "Bypass no longer covers this connector call. Nothing was sent."
    assert world.executed == []
    assert "hitl.bypass" not in actions(world)


@pytest.mark.asyncio
async def test_unavailable_audit_runs_nothing(world):
    async def unavailable(_event):
        raise RuntimeError("synthetic audit failure")

    world.orch.audit_recorder = SimpleNamespace(record=unavailable)

    response = await evaluate(world, call_arguments())

    assert response.error["message"] == "Action audit is unavailable. The action was not run."
    assert world.executed == []


@pytest.mark.asyncio
async def test_unattended_socket_is_refused_before_bypass(world):
    world.orch.ui_sessions[world.mac] = {"sub": "owner-a", "machine_class": "agent"}

    response = await evaluate(world, call_arguments())

    assert response.error is not None
    assert world.executed == []
    assert world.events == []


@pytest.mark.asyncio
async def test_other_remote_mcp_tools_never_inherit_bypass(world):
    response = await evaluate(world, call_arguments(), tool="list_remote_tools")
    assert world.executed == []
    assert response.result is None or response.result.get("_data", {}).get("approval_source") != "bypass"


def test_consent_decision_is_standing_only_for_the_bypassed_host_socket(world):
    decide = confirmation.consent_decision
    args = call_arguments()
    assert decide(world.orch, world.mac, "owner-a", "chat-a", "remote-mcp-1", "call_remote_tool", args, RISKS) == (
        "standing", None)
    assert decide(world.orch, object(), "owner-a", "chat-a", "remote-mcp-1", "call_remote_tool", args, RISKS) == (
        "review", None)
    assert decide(world.orch, world.mac, "owner-a", "chat-a", "remote-mcp-1", "call_remote_tool", "[]", RISKS) == (
        "review", None)
    world.orch.computer_hosts = None
    assert decide(world.orch, world.mac, "owner-a", "chat-a", "remote-mcp-1", "call_remote_tool", args, RISKS) == (
        "review", None)
