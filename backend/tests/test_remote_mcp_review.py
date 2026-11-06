"""Checks owner-bound MCP routing identifiers reach exact-action approval without exposing private tool arguments."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator import hitl_confirmation as confirmation


CONNECTOR_ID = "1" * 32


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


@pytest.fixture
def review_world(monkeypatch):
    monkeypatch.setattr("personalization.phi_policy.protection_enabled", lambda *_args: False)
    socket = object()
    events = []

    async def record(event):
        events.append(event)
        return SimpleNamespace(event_id="receipt")

    orch = SimpleNamespace(
        credential_manager=ConnectorStore(), ui_sessions={socket: {"sub": "owner-a"}},
        _ws_active_chat={id(socket): "chat-a"},
        _map_file_paths=lambda _chat, args, **_kwargs: args,
        audit_recorder=SimpleNamespace(record=record),
    )
    confirmation.begin_task(orch, socket, "owner-a", "chat-a")
    return orch, socket, events


def call_arguments():
    return {"connector_id": CONNECTOR_ID, "tool_name": "echo", "arguments": {"text": "synthetic-echo"}}


async def evaluate(world, args, *, owner="owner-a", agent="remote-mcp-1", tool="call_remote_tool"):
    orch, socket, _events = world
    return await confirmation.evaluate(
        orch, socket, owner, "chat-a", agent, tool, args, ("source_bound", "tools:execute"))


@pytest.mark.asyncio
async def test_enabled_owner_tool_has_complete_preview_and_still_requires_exact_approval(review_world):
    orch, _socket, events = review_world
    args = call_arguments()

    response = await evaluate(review_world, args)

    assert response.error is None
    assert response.result["_data"]["status"] == "confirmation_required"
    card = response.ui_components[0]
    preview = next(component["code"] for component in card["content"] if component["type"] == "code")
    assert json.loads(preview) == {
        "connector_id": CONNECTOR_ID, "tool_name": "echo", "arguments": {"text": "synthetic-echo"},
    }
    decisions = [component["payload"]["decision"] for component in card["content"]
                 if component.get("action") == "authorize_action"]
    assert decisions == ["approve", "decline"]
    pending = next(iter(orch._hitl_pending_calls.values()))
    assert json.loads(pending.arguments) == args
    assert [event.action_type for event in events] == ["hitl.proposed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"connector_id": "2" * 32}, {"connector_id": "../another-connector"},
    {"tool_name": "unregistered"}, {"tool_name": {"name": "echo"}},
])
async def test_forged_routing_identifiers_never_receive_a_review_card(review_world, mutation):
    orch, _socket, events = review_world
    args = {**call_arguments(), **mutation}
    response = await evaluate(review_world, args)
    assert response.error
    assert not response.ui_components
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert not events


@pytest.mark.asyncio
async def test_connector_lookup_uses_current_owner_instead_of_payload_owner(review_world):
    orch, socket, events = review_world
    orch.ui_sessions[socket] = {"sub": "owner-b"}
    confirmation.begin_task(orch, socket, "owner-b", "chat-a")
    response = await evaluate(review_world, {**call_arguments(), "user_id": "owner-a"}, owner="owner-b")
    assert response.error
    assert not response.ui_components
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert not events


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"name": "Private person"}, {"patient_name": "Private person"},
    {"tool_name": "echo"}, {"text": "password: private-value"},
    {"authorization": "private-value"}, {"nested": {"name": "Private person"}},
])
async def test_validated_route_does_not_exempt_nested_private_arguments(review_world, arguments):
    orch, _socket, events = review_world
    response = await evaluate(review_world, {**call_arguments(), "arguments": arguments})
    assert response.error
    assert not response.ui_components
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert not events


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,tool", [("another-agent", "call_remote_tool"), ("remote-mcp-1", "other_tool")])
async def test_other_agent_or_tool_cannot_claim_mcp_identifier_exemptions(review_world, agent, tool):
    response = await evaluate(review_world, call_arguments(), agent=agent, tool=tool)
    assert response.error
    assert not response.ui_components


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"enabled": False}, {"tools": {"echo": False}},
    {"tools_catalog": []}, {"tools_catalog": [None, {"name": "other"}]},
])
async def test_tool_must_remain_enabled_and_present_in_owner_catalog(review_world, change):
    orch, _socket, events = review_world
    key = ("owner-a", "__remote_mcp_connections__", "remote_mcp_" + CONNECTOR_ID)
    connector = json.loads(orch.credential_manager.records[key])
    orch.credential_manager.records[key] = json.dumps({**connector, **change})
    response = await evaluate(review_world, call_arguments())
    assert response.error["code"] == "REMOTE_MCP_REVIEW_UNAVAILABLE"
    assert not response.ui_components
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert not events


@pytest.mark.asyncio
async def test_unavailable_owner_store_fails_closed(review_world, monkeypatch):
    orch, _socket, events = review_world

    def unavailable(*_args):
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr(orch.credential_manager, "get_credential", unavailable)
    response = await evaluate(review_world, call_arguments())
    assert response.error["code"] == "REMOTE_MCP_REVIEW_UNAVAILABLE"
    assert not response.ui_components
    assert not events


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["123-45-6789", "sk-" + "x" * 24, "ghp_" + "x" * 32])
async def test_cached_tool_name_retains_private_value_and_secret_checks(review_world, tool_name):
    orch, _socket, events = review_world
    key = ("owner-a", "__remote_mcp_connections__", "remote_mcp_" + CONNECTOR_ID)
    connector = json.loads(orch.credential_manager.records[key])
    connector.update(tools={tool_name: True}, tools_catalog=[{"name": tool_name}])
    orch.credential_manager.records[key] = json.dumps(connector)
    response = await evaluate(review_world, {**call_arguments(), "tool_name": tool_name})
    assert response.error["code"] == "ACTION_REVIEW_UNAVAILABLE"
    assert not response.ui_components
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert not events


def test_trusted_opaque_identifier_never_exempts_secret_key_or_value():
    for args in ({"session_id": "ghp_" + "x" * 32}, {"authorization": "opaque-id"}):
        preview, reviewable = confirmation._review_arguments(args, trusted_identifiers=args, phi=False)
        assert not reviewable
        assert next(iter(args.values())) not in preview


@pytest.mark.asyncio
async def test_cached_tool_name_still_reaches_owner_phi_gate(review_world, monkeypatch):
    orch, _socket, events = review_world
    key = ("owner-a", "__remote_mcp_connections__", "remote_mcp_" + CONNECTOR_ID)
    connector = json.loads(orch.credential_manager.records[key])
    connector.update(tools={"private_patient": True}, tools_catalog=[{"name": "private_patient"}])
    orch.credential_manager.records[key] = json.dumps(connector)
    inspected = []

    def contains_phi(value):
        inspected.append(value)
        return "private_patient" in value

    monkeypatch.setattr("personalization.phi_policy.protection_enabled", lambda *_args: True)
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate", lambda: SimpleNamespace(contains_phi=contains_phi))
    response = await evaluate(review_world, {**call_arguments(), "tool_name": "private_patient"})
    assert response.error["code"] == "ACTION_REVIEW_UNAVAILABLE"
    assert inspected and "private_patient" in inspected[-1]
    assert not response.ui_components
    assert not events
