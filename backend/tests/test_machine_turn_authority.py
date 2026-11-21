"""Tests for orchestrator/chain_authority.py: machine turns derive authority narrowed to
consented-and-current scopes, and unbound or over-broad delegation chains are refused.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.chain_authority import MachineAuthority  # noqa: E402
from shared.protocol import MCPResponse  # noqa: E402


@pytest.fixture
def real_orch(orchestrator_factory):
    o = orchestrator_factory()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
    o.tool_permissions.get_tool_scope_map = MagicMock(return_value={"t1": "tools:read", "peer_tool": "tools:read"})
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    o.local_agents["a1"] = MagicMock()
    return o


def _authority():
    return MachineAuthority(
        access_token="consent-token", allowed_scopes=["tools:read"],
        principal="machine:scheduled_job", user_id="u1",
        consent_ref="grant-1", turn_class="scheduled_job")


@pytest.mark.asyncio
async def test_unbound_machine_turn_still_refused_in_production(real_orch, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

    vws = VirtualWebSocket(BackgroundTask(task_id="t", chat_id="c1", user_id="u1"))
    real_orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result="ok"))
    import json as _json
    tc = SimpleNamespace(function=SimpleNamespace(name="t1", arguments=_json.dumps({})))
    resp = await real_orch.execute_single_tool(
        vws, tc, {"t1": "a1"}, "c1", user_id="u1")
    assert "delegated authorization" in (resp.error or {}).get("message", "")


@pytest.mark.asyncio
async def test_machine_turn_chains_attenuate(real_orch):
    import asyncio

    from orchestrator import agent_authority as dg
    from shared.feature_flags import flags
    from shared.protocol import AgentHopRequest

    prior = flags._flags.get("recursive_delegation")
    flags._flags["recursive_delegation"] = True
    try:
        from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
        vws = VirtualWebSocket(BackgroundTask(task_id="t", chat_id="c1", user_id="u1"))
        real_orch._bind_machine_turn(vws, _authority())

        now = int(time.time())
        root = {"sub": "u1", "act": {"sub": "agent:a1"},
                "scope": "tools:read tool:peer_tool",
                "iss": "astral-delegation", "aud": "svc",
                "iat": now, "exp": now + 300, "delegation": True}
        real_orch.local_agents["b1"] = MagicMock()
        real_orch.agent_cards["b1"] = SimpleNamespace(
            skills=[SimpleNamespace(id="peer_tool")])
        real_orch.tool_permissions.get_enabled_scope_names = MagicMock(
            return_value=["tools:read"])
        real_orch.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
        real_orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result="ok"))
        real_orch._register_dispatch_context(
            "req-a", "a1",
            {"user_id": "u1", "session_id": "c1",
             "_delegation_token": dg.encode_delegation_payload(root)}, vws)

        ws = SimpleNamespace(_hop_futures={})
        fut = asyncio.get_running_loop().create_future()
        ws._hop_futures["hop-1"] = fut
        await real_orch._handle_agent_hop_request(ws, AgentHopRequest(
            request_id="hop-1", parent_request_id="req-a",
            initiator_agent_id="a1", callee_agent_id="b1",
            tool_name="peer_tool", arguments={}))
        resp = await asyncio.wait_for(fut, timeout=2)
        assert resp.result == "ok"
        child = dg.decode_token_payload(
            real_orch._execute_with_retry.await_args.args[3]["_delegation_token"])
        assert child["delegation_depth"] == 1
        assert set(child["scope"].split()) <= set(root["scope"].split())
        assert dg.actor_chain(child) == ["agent:b1", "agent:a1"]
        assert child["sub"] == "u1"
    finally:
        flags._flags["recursive_delegation"] = prior
