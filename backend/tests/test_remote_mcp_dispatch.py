"""Checks that remote MCP calls enter the ordinary authenticated tool gate.
The registered in-process agent never sends traffic in these refusal tests.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.remote_mcp.remote_mcp_agent import RemoteMCPAgent
from orchestrator.orchestrator import GateRefusal
from shared.protocol import RegisterAgent
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
