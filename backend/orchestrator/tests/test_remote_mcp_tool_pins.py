"""Covers hash pinning of remote MCP tool definitions: approval, change, removal, dispatch refusal and audit."""

from __future__ import annotations

import json

import pytest

from orchestrator import remote_mcp_connectors as service
from orchestrator import remote_mcp_pins as pins
from orchestrator.tests.test_remote_mcp_connectors import CredentialManager, FakeTransport
from types import SimpleNamespace


@pytest.fixture
def world(monkeypatch):
    events = []

    async def record(**event):
        events.append(event)

    monkeypatch.setattr("audit.hooks.record_generic", record)
    orch = SimpleNamespace(credential_manager=CredentialManager())
    saved = service.save_connector(orch, "owner-a", {
        "name": "Fixture", "url": "https://mcp.example/mcp", "transport": "streamable_http", "auth_type": "none"})
    return orch, saved["id"], events


def _serve(monkeypatch, tools):
    async def initialize(_connector, **_kwargs):
        return list(tools), None

    async def request(_connector, payload, _session, **_kwargs):
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}, ""

    monkeypatch.setattr(service, "_initialize", initialize)
    monkeypatch.setattr(service, "_mcp_request", request)


def _tool(description="safe", schema=None, annotations=None):
    tool = {"name": "echo", "description": description, "inputSchema": schema or {"type": "object"}}
    if annotations is not None:
        tool["annotations"] = annotations
    return tool


def _approve(orch, connector_id, monkeypatch, tool):
    _serve(monkeypatch, [tool])
    service.refresh_tools(orch, "owner-a", connector_id)
    service.set_tool_selection(orch, "owner-a", connector_id, "echo", True)


def test_hash_is_canonical_and_covers_every_field():
    base = _tool(annotations={"readOnlyHint": True})
    same = {"annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}, "description": "safe", "name": "echo"}
    assert pins.definition_hash(base) == pins.definition_hash(same)
    assert pins.is_pin(pins.definition_hash(base))
    assert pins.definition_hash(base).startswith("sha256:")
    for changed in (_tool("other", annotations={"readOnlyHint": True}),
                    _tool(schema={"type": "object", "properties": {}}, annotations={"readOnlyHint": True}),
                    _tool(annotations={"readOnlyHint": False}), {**base, "name": "echo2"}):
        assert pins.definition_hash(changed) != pins.definition_hash(base)
    assert pins.definition_hash("nope") == ""
    assert not pins.is_pin("sha256:xyz") and not pins.is_pin(None)


def test_annotations_are_reduced_to_known_safe_fields():
    clean = pins.sanitize_annotations({"readOnlyHint": True, "destructiveHint": "yes", "title": " A  B ",
                                       "extra": {"x": 1}})
    assert clean == {"readOnlyHint": True, "title": "A B"}
    assert pins.sanitize_annotations(None) == {}


def test_unapproved_tool_is_not_callable_or_listed(world, monkeypatch):
    orch, connector_id, _ = world
    _serve(monkeypatch, [_tool()])
    service.refresh_tools(orch, "owner-a", connector_id)
    assert service.list_enabled_tools(orch, "owner-a") == []
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector["tools"] = {"echo": True}
    service.save_connector(orch, "owner-a", connector)
    with pytest.raises(service.RemoteMCPError, match="not been approved"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})


def test_approval_pins_current_definition_and_is_audited(world, monkeypatch):
    orch, connector_id, events = world
    _approve(orch, connector_id, monkeypatch, _tool())
    connector = service.get_connector(orch, "owner-a", connector_id)
    assert connector["tool_pins"]["echo"] == pins.definition_hash(connector["tools_catalog"][0])
    assert [e for e in events if e["action_type"] == "remote_mcp.tool.definition_approved"]
    assert service.call_tool(orch, "owner-a", connector_id, "echo", {})["result"]["content"][0]["text"] == "ok"
    assert service.list_enabled_tools(orch, "owner-a")[0]["name"] == "echo"


@pytest.mark.parametrize("changed", [
    _tool("poisoned instructions"),
    _tool(schema={"type": "object", "properties": {"x": {"type": "string"}}}),
    _tool(annotations={"destructiveHint": False}),
])
def test_refresh_disables_a_changed_tool_and_audits_it(world, monkeypatch, changed):
    orch, connector_id, events = world
    _approve(orch, connector_id, monkeypatch, _tool())
    _serve(monkeypatch, [changed])
    service.refresh_tools(orch, "owner-a", connector_id)
    connector = service.get_connector(orch, "owner-a", connector_id)
    assert connector["tools"]["echo"] is False
    assert connector["tool_changes"]["echo"]["status"] == "changed"
    assert connector["tool_changes"]["echo"]["current"] == pins.definition_hash(changed)
    assert service.list_enabled_tools(orch, "owner-a") == []
    audited = [e for e in events if e["action_type"] == "remote_mcp.tool.definition_changed"]
    assert len(audited) == 1 and audited[0]["outcome"] == "failure"
    service.refresh_tools(orch, "owner-a", connector_id)
    assert len([e for e in events if e["action_type"] == "remote_mcp.tool.definition_changed"]) == 1
    with pytest.raises(service.RemoteMCPError, match="not enabled"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})


def test_reapproval_accepts_the_new_definition_and_clears_the_flag(world, monkeypatch):
    orch, connector_id, _ = world
    _approve(orch, connector_id, monkeypatch, _tool())
    _serve(monkeypatch, [_tool("new text")])
    service.refresh_tools(orch, "owner-a", connector_id)
    service.set_tool_selection(orch, "owner-a", connector_id, "echo", True)
    connector = service.get_connector(orch, "owner-a", connector_id)
    assert connector["tool_changes"] == {}
    assert service.call_tool(orch, "owner-a", connector_id, "echo", {})["tool_name"] == "echo"


def test_a_change_reverted_upstream_clears_the_flag_but_stays_disabled(world, monkeypatch):
    orch, connector_id, _ = world
    _approve(orch, connector_id, monkeypatch, _tool())
    _serve(monkeypatch, [_tool("other")])
    service.refresh_tools(orch, "owner-a", connector_id)
    _serve(monkeypatch, [_tool()])
    service.refresh_tools(orch, "owner-a", connector_id)
    connector = service.get_connector(orch, "owner-a", connector_id)
    assert connector["tool_changes"] == {} and connector["tools"]["echo"] is False


def test_dispatch_refuses_a_definition_that_changed_between_refreshes(world, monkeypatch):
    orch, connector_id, events = world
    _approve(orch, connector_id, monkeypatch, _tool())
    _serve(monkeypatch, [_tool("swapped after approval")])
    with pytest.raises(service.RemoteMCPError, match="changed since it was approved"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})
    connector = service.get_connector(orch, "owner-a", connector_id)
    assert connector["tools"]["echo"] is False
    assert connector["tool_changes"]["echo"]["status"] == "changed"
    assert connector["tools_catalog"][0]["description"].endswith("swapped after approval")
    assert any(e["action_type"] == "remote_mcp.tool.definition_changed"
               and e["inputs_meta"]["source"] == "dispatch" for e in events)


def test_removed_tool_is_detected_on_refresh_and_dispatch(world, monkeypatch):
    orch, connector_id, events = world
    _approve(orch, connector_id, monkeypatch, _tool())
    _serve(monkeypatch, [])
    service.refresh_tools(orch, "owner-a", connector_id)
    connector = service.get_connector(orch, "owner-a", connector_id)
    assert connector["tool_changes"]["echo"]["status"] == "removed"
    assert any(e["action_type"] == "remote_mcp.tool.definition_removed" for e in events)

    _approve(orch, connector_id, monkeypatch, _tool())
    _serve(monkeypatch, [])
    with pytest.raises(service.RemoteMCPError, match="no longer offers"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})
    assert service.get_connector(orch, "owner-a", connector_id)["tool_changes"]["echo"]["status"] == "removed"


def test_other_tools_keep_their_approval_when_one_changes(world, monkeypatch):
    orch, connector_id, _ = world
    other = {"name": "other", "description": "x", "inputSchema": {"type": "object"}}
    _serve(monkeypatch, [_tool(), other])
    service.refresh_tools(orch, "owner-a", connector_id)
    service.set_tool_selection(orch, "owner-a", connector_id, "echo", True)
    service.set_tool_selection(orch, "owner-a", connector_id, "other", True)
    _serve(monkeypatch, [_tool("changed"), other])
    service.refresh_tools(orch, "owner-a", connector_id)
    tools = service.get_connector(orch, "owner-a", connector_id)["tools"]
    assert tools == {"echo": False, "other": True}


def test_changing_server_authority_resets_approvals(world, monkeypatch):
    orch, connector_id, _ = world
    _approve(orch, connector_id, monkeypatch, _tool())
    connector = service.get_connector(orch, "owner-a", connector_id)
    connector["url"] = "https://elsewhere.example/mcp"
    service.save_connector(orch, "owner-a", connector)
    reset = service.get_connector(orch, "owner-a", connector_id)
    assert reset["tool_pins"] == {} and reset["tool_changes"] == {} and reset["tools_catalog"] == []


def test_invalid_stored_pins_are_rejected():
    base = {"name": "n", "url": "https://mcp.example/mcp", "transport": "sse", "auth_type": "none"}
    for bad in ({"tool_pins": {"echo": "md5:abc"}}, {"tool_pins": []}, {"tool_pins": {"bad name": "sha256:" + "0" * 64}},
                {"tool_changes": {"echo": {"status": "new"}}}, {"tool_changes": []}):
        with pytest.raises(service.RemoteMCPError):
            service._validate_config({**base, **bad})


def test_initialize_carries_sanitized_annotations_into_the_catalog(monkeypatch):
    fake = FakeTransport()
    original = fake.post_response_stream

    async def stream(request):
        async for item in original(request):
            if item.body and b'"tools"' in item.body:
                body = json.loads(item.body)
                body["result"]["tools"][0]["annotations"] = {"readOnlyHint": True, "junk": 1}
                item = SimpleNamespace(status=item.status, headers=item.headers, body=json.dumps(body).encode())
            yield item

    fake.post_response_stream = stream
    connector = {"name": "n", "url": "https://mcp.example/mcp", "transport": "streamable_http", "auth_type": "none"}
    import asyncio
    tools = asyncio.run(service.discover_tools(connector, transport_factory=lambda *_a, **_k: fake))
    assert tools[0]["annotations"] == {"readOnlyHint": True}


def test_dispatch_refuses_an_approval_replaced_while_listing_tools(world, monkeypatch):
    orch, connector_id, _ = world
    original = _tool("original approval")
    replacement = _tool("replacement approval")
    _approve(orch, connector_id, monkeypatch, original)
    calls = []

    async def initialize(_connector, **_kwargs):
        current = service.get_connector(orch, "owner-a", connector_id)
        current["tool_pins"]["echo"] = pins.definition_hash(replacement)
        current["tools_catalog"] = [replacement]
        key = service._KEY_PREFIX + connector_id
        raw = orch.credential_manager.get_credential("owner-a", service._AGENT_ID, key)
        assert orch.credential_manager.compare_and_set_credential(
            "owner-a", service._AGENT_ID, key, raw, json.dumps(current))
        return [original], None

    async def request(_connector, payload, _session, **_kwargs):
        calls.append(payload)
        return {"result": {"content": []}}, ""

    monkeypatch.setattr(service, "_initialize", initialize)
    monkeypatch.setattr(service, "_mcp_request", request)
    with pytest.raises(service.RemoteMCPError, match="approval changed"):
        service.call_tool(orch, "owner-a", connector_id, "echo", {})
    assert calls == []
