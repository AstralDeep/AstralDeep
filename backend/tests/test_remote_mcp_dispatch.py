"""Checks that remote MCP calls enter the ordinary authenticated tool gate.
The registered in-process agent never sends traffic in these refusal tests.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.remote_mcp.remote_mcp_agent import RemoteMCPAgent
from orchestrator.orchestrator import GateRefusal
from shared.protocol import RegisterAgent
from tests.test_remote_confirmation_063 import real_orch as real_orch


@pytest.mark.asyncio
async def test_gate_replaces_forged_owner_before_remote_mcp_read(orchestrator_factory, monkeypatch):
    from orchestrator import auth

    base_orch = orchestrator_factory()
    base_orch.audit_recorder = MagicMock()
    base_orch.audit_recorder.record = AsyncMock()
    base_orch.send_ui_render = AsyncMock()
    base_orch._map_file_paths = lambda _cid, args, **_kwargs: args
    monkeypatch.setattr(auth, "validate_agent_api_key", lambda _key: True)
    monkeypatch.setattr(RemoteMCPAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    agent = RemoteMCPAgent(orchestrator=base_orch)
    await base_orch.register_agent(None, RegisterAgent(agent_card=agent.card))
    assert base_orch.tool_permissions.get_tool_scope_map(agent.agent_id) == {
        "list_remote_tools": "tools:read", "call_remote_tool": "tools:execute",
    }
    base_orch.local_agents[agent.agent_id] = agent
    socket = object()
    base_orch.ui_sessions[socket] = {"sub": "owner", "exp": time.time() + 600,
                                     "realm_access": {"roles": ["user"]}}
    base_orch.tool_permissions.is_tool_allowed = lambda *_args: True

    prepared = await base_orch._authorize_and_prepare(
        socket, "remote-mcp-1", "list_remote_tools", {"user_id": "foreign"},
        user_id="owner", auto_subscribe_stream=False,
    )

    assert not isinstance(prepared, GateRefusal)
    assert prepared.args["user_id"] == "owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", [False, True])
async def test_connector_effect_requires_kernel_approval_even_on_the_owner_mac(real_orch, bypass):
    from agents.remote_mcp.mcp_tools import TOOL_REGISTRY

    orch = real_orch
    socket = object()
    orch.ui_sessions[socket] = {"sub": "owner"}
    orch.tool_permissions.get_tool_scope_map.return_value = {name: tool["scope"] for name, tool in TOOL_REGISTRY.items()}
    host = SimpleNamespace(owner_sub="owner", host_id="mac", platform="macos", websocket=socket)
    orch.computer_hosts = SimpleNamespace(host_for_socket=lambda candidate: host if candidate is socket else None)
    orch.computer_sessions = SimpleNamespace(bypass_on=lambda owner, machine: (owner, machine) == ("owner", "mac") and bypass)
    orch._execute_with_retry_audited = AsyncMock()
    args = {"connector_id": "test-connector", "tool_name": "echo", "arguments": {"text": "sample"}}
    name = "call_remote_tool"
    call = SimpleNamespace(id="connector-call", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    result = await orch.execute_single_tool(socket, call, {name: "remote-mcp-1"}, "chat", user_id="owner")
    assert result.error["code"] == "source_bound_approval_required"
    assert result.error["effect_state"] == "not_started" and not result.ui_components
    orch._execute_with_retry_audited.assert_not_awaited()
