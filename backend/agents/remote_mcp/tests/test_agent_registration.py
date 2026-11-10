"""Checks remote MCP agent registration and owner-bound tool dispatch.
The connector service is replaced so these tests exercise the local trust boundary.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agents.remote_mcp import mcp_tools
from agents.remote_mcp.remote_mcp_agent import RemoteMCPAgent
from orchestrator import local_agents
from shared import attachment_materializer, attachment_resolver
from shared.feature_flags import flags


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_remote_mcp_registration_requires_feature_flag(monkeypatch, enabled):
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setattr(flags, "is_enabled", lambda name: enabled and name == "remote_mcp")
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *_: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *_: None)
    monkeypatch.setattr(local_agents, "_load_agent_class", lambda _: _Agent)
    registered = []

    async def register(_websocket, message):
        registered.append(message.agent_card.agent_id)

    orch = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(
            runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object())),
        register_agent=register, local_agents={},
    )
    result = await local_agents.register_built_ins(orch)

    assert result == (["remote-mcp-1"] if enabled else [])
    assert registered == result
    assert list(orch.local_agents) == result
    assert "remote-mcp-1" in local_agents.FIRST_PARTY_PUBLIC_AGENT_IDS


class _Agent:
    def __init__(self, *, orchestrator):
        assert orchestrator is not None
        self.card = SimpleNamespace(agent_id="remote-mcp-1", skills=[])


def test_tool_scopes_keep_remote_calls_reviewed():
    assert mcp_tools.TOOL_REGISTRY["list_remote_tools"]["scope"] == "tools:read"
    assert mcp_tools.TOOL_REGISTRY["call_remote_tool"]["scope"] == "tools:execute"


def test_agent_requires_orchestrator():
    with pytest.raises(RuntimeError, match="requires the orchestrator"):
        RemoteMCPAgent()


def test_list_remote_tools_uses_verified_owner(monkeypatch):
    from orchestrator import remote_mcp_connectors

    seen = []

    def list_tools(_orch, owner):
        seen.append(owner)
        return [{"name": "echo", "connector_id": "a" * 32}]

    monkeypatch.setattr(remote_mcp_connectors, "list_enabled_tools", list_tools)
    mcp_tools.register_deps(object())
    try:
        result = mcp_tools.list_remote_tools(user_id="owner")
    finally:
        mcp_tools.register_deps(None)

    assert seen == ["owner"]
    assert result["_data"]["tools"][0]["name"] == "echo"


def test_tool_rejects_missing_verified_owner(monkeypatch):
    from orchestrator import remote_mcp_connectors

    monkeypatch.setattr(remote_mcp_connectors, "call_tool", lambda *_: pytest.fail("outbound call"))
    result = mcp_tools.call_remote_tool("a" * 32, "echo", {}, user_id="")
    assert result["_data"]["error"] == "remote_mcp_call_refused"
    assert "Owner identity" in result["_data"]["message"]


def test_tool_uses_verified_owner_and_selected_connector(monkeypatch):
    from orchestrator import remote_mcp_connectors

    calls = []

    def call(_orch, owner, connector_id, tool_name, arguments):
        calls.append((owner, connector_id, tool_name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(remote_mcp_connectors, "call_tool", call)
    mcp_tools.register_deps(object())
    result = mcp_tools.call_remote_tool("a" * 32, "echo", {"message": "hello"}, user_id="owner")
    mcp_tools.register_deps(None)

    assert calls == [("owner", "a" * 32, "echo", {"message": "hello"})]
    assert result["_data"]["content"][0]["text"] == "ok"
