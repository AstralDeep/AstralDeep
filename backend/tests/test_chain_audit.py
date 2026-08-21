"""T018 (056-delegated-agent-chaining): paired hop provenance records.

Every hop emits a ``delegation.hop.mint`` / ``delegation.hop.enforce`` pair
under the ``delegation`` event class, sharing one correlation id with the
hop's own ``tool.<name>.start/end`` pair — and NEVER carrying token bytes
(FR-028).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import audit.hooks as audit_hooks  # noqa: E402
import audit.recorder as audit_recorder  # noqa: E402
from orchestrator import delegation as dg  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.protocol import AgentHopRequest, MCPResponse  # noqa: E402


@pytest.fixture(autouse=True)
def chaining_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)


@pytest.fixture
def captured(monkeypatch):
    rec = MagicMock()
    rec.record = AsyncMock()
    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: rec)
    monkeypatch.setattr(audit_hooks, "get_recorder", lambda: rec)
    return rec


@pytest.fixture
def orch():
    from orchestrator.concurrency_cap import ConcurrencyCap
    from orchestrator.hooks import HookManager
    from orchestrator.orchestrator import Orchestrator

    # These tests exercise the real delegation and dispatch methods while
    # replacing every external collaborator.  Avoid composing the
    # application-scoped Plane graph for this pure unit boundary.
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
    o.tool_permissions.get_enabled_scope_names = MagicMock(return_value=["tools:read"])
    o.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager = MagicMock()
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    o.local_agents["initiator-1"] = MagicMock()
    o.local_agents["callee-1"] = MagicMock()
    o.agent_cards["callee-1"] = SimpleNamespace(
        skills=[SimpleNamespace(id="peer_tool")])
    o._execute_with_retry = AsyncMock(return_value=MCPResponse(result="ok"))
    return o


def _parent(scope="tools:read tool:peer_tool"):
    now = int(time.time())
    return {"sub": "u1", "act": {"sub": "agent:initiator-1"}, "scope": scope,
            "iss": "mock-astral-delegation", "aud": "agent-svc",
            "iat": now, "exp": now + 300, "delegation": True}


async def _hop(orch, parent):
    ui_ws = MagicMock()
    ui_ws.machine_claims = None
    orch.ui_sessions[ui_ws] = {"sub": "u1"}
    token = dg.encode_delegation_payload(parent)
    orch._register_dispatch_context(
        "req-parent", "initiator-1",
        {"user_id": "u1", "session_id": "c1", "_delegation_token": token}, ui_ws)
    ws = SimpleNamespace(_hop_futures={})
    fut = asyncio.get_running_loop().create_future()
    ws._hop_futures["hop-1"] = fut
    await orch._handle_agent_hop_request(ws, AgentHopRequest(
        request_id="hop-1", parent_request_id="req-parent",
        initiator_agent_id="initiator-1", callee_agent_id="callee-1",
        tool_name="peer_tool", arguments={"q": "hi"}))
    return await asyncio.wait_for(fut, timeout=2), token


@pytest.mark.asyncio
async def test_paired_hop_records_share_one_correlation(orch, captured):
    resp, _ = await _hop(orch, _parent())
    assert resp.result == "ok"
    rows = [c.args[0] for c in captured.record.await_args_list]
    hop_rows = [r for r in rows if r.event_class == "delegation"]
    assert [r.action_type for r in hop_rows] == [
        "delegation.hop.mint", "delegation.hop.enforce"]
    assert hop_rows[0].outcome == "in_progress"
    assert hop_rows[1].outcome == "success"
    corr = {r.correlation_id for r in hop_rows}
    assert len(corr) == 1
    # The hop's tool.start/end pair shares the SAME correlation id (SC-003).
    tool_rows = [r for r in rows if r.event_class == "agent_tool_call"]
    assert {r.correlation_id for r in tool_rows} == corr
    assert [r.action_type for r in tool_rows] == [
        "tool.peer_tool.start", "tool.peer_tool.end"]
    # Hop rows attribute the human authorizer + the acting agent; tool rows
    # name the initiating agent as the RFC 8693 actor.
    for r in hop_rows:
        assert r.actor_user_id == "u1"
        assert r.auth_principal == "agent:callee-1"
        assert r.agent_id == "callee-1"
    for r in tool_rows:
        assert r.actor_user_id == "u1"
        assert r.auth_principal == "agent:initiator-1"


@pytest.mark.asyncio
async def test_hop_records_carry_metadata_never_token_bytes(orch, captured):
    _, token = await _hop(orch, _parent())
    rows = [c.args[0] for c in captured.record.await_args_list]
    hop_rows = [r for r in rows if r.event_class == "delegation"]
    meta = hop_rows[0].inputs_meta
    assert meta["parent_actor"] == "agent:initiator-1"
    assert meta["acting_agent"] == "agent:callee-1"
    assert meta["delegation_depth"] == 1
    assert meta["actor_chain"] == ["agent:callee-1", "agent:initiator-1"]
    assert "granted_scopes" in meta and "requested_scopes" in meta
    serialized = json.dumps([r.model_dump(mode="json") for r in rows], default=str)
    # FR-028: no token material anywhere in any record.
    assert token not in serialized
    assert token.split(".")[2] not in serialized  # nor its signature segment


@pytest.mark.asyncio
async def test_gate_refused_hop_is_audited(orch, captured):
    """SC-002: a hop refused by a gate that fires BEFORE the delegation step
    (here: the explicit per-user opt-out) still carries audit evidence."""
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    resp, _ = await _hop(orch, _parent())
    assert "restricted for this agent" in (resp.error or {}).get("message", "")
    rows = [c.args[0] for c in captured.record.await_args_list]
    hop_rows = [r for r in rows if r.event_class == "delegation"]
    assert len(hop_rows) == 1
    assert hop_rows[0].action_type == "delegation.hop.mint"
    assert hop_rows[0].outcome == "failure"
    assert "restricted" in (hop_rows[0].outcome_detail or "")
    assert hop_rows[0].agent_id == "callee-1"


@pytest.mark.asyncio
async def test_refused_hop_audits_failure_with_scope_evidence(orch, captured):
    resp, _ = await _hop(orch, _parent(scope="tools:write tool:unrelated"))
    assert resp.error is not None
    rows = [c.args[0] for c in captured.record.await_args_list]
    hop_rows = [r for r in rows if r.event_class == "delegation"]
    assert len(hop_rows) == 1
    r = hop_rows[0]
    assert r.action_type == "delegation.hop.mint"
    assert r.outcome == "failure"
    assert r.outcome_detail == "empty_intersection"
    # FR-005: requested-vs-granted recorded.
    assert r.inputs_meta["requested_scopes"]
    assert r.inputs_meta["granted_scopes"] == []
    # No tool dispatch happened, so no agent_tool_call rows.
    assert not [x for x in rows if x.event_class == "agent_tool_call"]
