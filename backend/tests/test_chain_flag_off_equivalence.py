"""T020 (056-delegated-agent-chaining): flag-off byte-equivalence (SC-009).

With ``FF_RECURSIVE_DELEGATION`` off (the default), the chaining seam is
inert: hop requests are refused before any context/authority work, no audit
rows are emitted, no dispatch happens, and the direct path's token behavior
is exactly the flat single-hop exchange. (The 048 property suite,
``test_delegation.py``, and ``test_tool_permissions.py`` run alongside this
file in the same suite — their unchanged green run is the other half of the
equivalence evidence.)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import audit.recorder as audit_recorder  # noqa: E402
from orchestrator import delegation as dg  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.protocol import AgentHopRequest, MCPResponse  # noqa: E402


@pytest.fixture(autouse=True)
def flag_off(monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", False)


@pytest.fixture
def orch():
    from orchestrator.concurrency_cap import ConcurrencyCap
    from orchestrator.hooks import HookManager
    from orchestrator.orchestrator import Orchestrator

    # Exercise the real dispatch methods without publishing an
    # application-scoped Plane graph from this collaborator-injected unit
    # fixture.
    o = Orchestrator.__new__(Orchestrator)
    o.agents = {}
    o.a2a_clients = {}
    o.local_agents = {}
    o.agent_cards = {}
    o.ui_sessions = {}
    o.security_flags = {}
    o._dispatch_context = {}
    o._chain_budgets = {}
    o._pending_cap_entries = {}
    o._hop_cap_entries = {}
    o._job_context = {}
    o._active_request = {}
    o._taint_trackers = {}
    o.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
    o.hooks = HookManager()
    o.stream_manager = None
    o._llm_store = SimpleNamespace(
        get=AsyncMock(return_value=None),
        get_system=AsyncMock(return_value=None),
    )
    o.send_ui_render = AsyncMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager = MagicMock()
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    o.local_agents["callee-1"] = MagicMock()
    o._execute_with_retry = AsyncMock(return_value=MCPResponse(result="ok"))
    return o


def test_flag_defaults_off(monkeypatch):
    # Fresh instance, not a module reload (a reload would rebind the global
    # singleton out from under modules that imported it).
    from shared.feature_flags import FeatureFlags
    monkeypatch.delenv("FF_RECURSIVE_DELEGATION", raising=False)
    assert FeatureFlags().is_enabled("recursive_delegation") is False


@pytest.mark.asyncio
async def test_hop_request_inert_with_flag_off(orch, monkeypatch):
    rec = MagicMock()
    rec.record = AsyncMock()
    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: rec)

    now = int(time.time())
    parent = {"sub": "u1", "act": {"sub": "agent:initiator-1"},
              "scope": "tools:read", "exp": now + 300}
    orch._register_dispatch_context(
        "req-parent", "initiator-1",
        {"user_id": "u1", "session_id": "c1",
         "_delegation_token": dg.encode_delegation_payload(parent)}, MagicMock())

    ws = SimpleNamespace(_hop_futures={})
    fut = asyncio.get_running_loop().create_future()
    ws._hop_futures["hop-1"] = fut
    await orch._handle_agent_hop_request(ws, AgentHopRequest(
        request_id="hop-1", parent_request_id="req-parent",
        initiator_agent_id="initiator-1", callee_agent_id="callee-1",
        tool_name="t", arguments={}))
    resp = await asyncio.wait_for(fut, timeout=2)
    assert "chaining is disabled" in resp.error["message"]
    orch._execute_with_retry.assert_not_awaited()
    rec.record.assert_not_awaited()  # inert — zero audit emission
    assert not orch._chain_budgets  # no budget was even created


@pytest.mark.asyncio
async def test_direct_dispatch_token_path_unchanged(orch, monkeypatch):
    """The flat single-hop exchange is byte-for-byte today's path: the token
    that _get_delegation_token returns is injected verbatim, no child mint."""
    monkeypatch.setattr(
        orch, "_get_delegation_token", AsyncMock(return_value="flat-token-xyz"))
    seen = {}

    async def _cap(ws, agent_id, tool_name, args, max_retries=None, **_kwargs):
        seen.update(args=dict(args))
        return MCPResponse(result="ok")

    orch._execute_with_retry = _cap
    import json as _json
    tc = SimpleNamespace(function=SimpleNamespace(name="t1", arguments=_json.dumps({})))
    await orch.execute_single_tool(MagicMock(), tc, {"t1": "callee-1"}, "c1",
                                   user_id="u1")
    assert seen["args"]["_delegation_token"] == "flat-token-xyz"


def test_call_agent_tool_exists_but_holds_no_authority():
    """The runtime surface ships regardless of the flag (it returns honest
    errors when mediation refuses); it must expose no token/mint surface."""
    from shared.agent_runtime import AgentRuntime
    surface = [a for a in vars(AgentRuntime) if not a.startswith("__")]
    assert "call_agent_tool" in surface
    assert not any("mint" in a or "token" in a for a in surface)
