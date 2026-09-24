"""Tests that every user-agent action and denial in orchestrator.py and user_agents.py
leaves an audited row under agent_lifecycle, attributed to the owning human, via
audit/hooks.py and audit/recorder.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import audit.hooks as audit_hooks  # noqa: E402
import audit.recorder as audit_recorder  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from shared.protocol import MCPResponse  # noqa: E402

OWNER = "byo058audit_owner"
AID = "byo058audit-agent"


async def _t(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)


class FakeUI:
    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(t)

    async def send(self, t):
        self.sent.append(t)

    async def close(self, *a, **k):
        return None


@pytest.fixture
def captured(monkeypatch):
    rec = MagicMock()
    rec.record = AsyncMock()
    monkeypatch.setattr(audit_recorder, "get_recorder", lambda: rec)
    monkeypatch.setattr(audit_hooks, "get_recorder", lambda: rec)
    return rec


@pytest.fixture
def agent_id():
    return f"{AID}-{uuid.uuid4().hex}"


@pytest.fixture
async def orch(monkeypatch, captured, agent_id):
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    from orchestrator.orchestrator import Orchestrator
    o = await asyncio.to_thread(Orchestrator)
    registry = o.user_agent_registry
    try:
        o.audit_recorder = captured
        o.send_ui_render = AsyncMock()
        o._safe_send = AsyncMock()
        await _t(
            ua.create_user_agent,
            registry,
            agent_id=agent_id,
            owner_user_id=OWNER,
            display_name="Greeter",
        )
        await _t(ua.mark_validated, registry, agent_id, "0.1.0")
        yield o
    finally:
        try:
            row = await _t(ua.get_user_agent, registry, agent_id)
            if row is not None and row.get("deleted_at") is None:
                await _t(ua.soft_delete, registry, agent_id)
        finally:
            await asyncio.wait_for(o._close_started_services(), timeout=15.0)


def _rows(captured):
    return [c.args[0] for c in captured.record.await_args_list]


async def test_user_agent_tool_dispatch_audits_attributed_to_owner(
    orch, captured, agent_id
):
    from orchestrator.orchestrator import PreparedDispatch

    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": OWNER}
    orch._authorize_and_prepare = AsyncMock(return_value=PreparedDispatch(
        args={"name": "world"}, stream_params={}, cap_job_id=None,
        delegation_token=None))
    orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result="hi"))

    tool_call = SimpleNamespace(function=SimpleNamespace(
        name="greet", arguments='{"name": "world"}'))
    resp = await orch.execute_single_tool(
        ws, tool_call, {"greet": agent_id}, chat_id="c1", user_id=OWNER)
    assert resp is not None and resp.error is None

    rows = _rows(captured)
    tool_rows = [r for r in rows if getattr(r, "event_class", None) == "agent_tool_call"]
    assert [r.action_type for r in tool_rows] == ["tool.greet.start", "tool.greet.end"]
    for r in tool_rows:
        assert r.actor_user_id == OWNER
        assert r.agent_id == agent_id
    assert tool_rows[-1].outcome == "success"


async def test_bundle_delivery_should_audit(orch, captured, agent_id):
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": OWNER}
    orch.ui_clients.append(ws)
    orch._agent_host_sockets[id(ws)] = "hs-1"
    await orch.deliver_agent_bundle(
        OWNER, agent_id, {"agent_main.py": "code"}, "0.1.0"
    )
    rows = _rows(captured)
    assert any(getattr(r, "agent_id", None) == agent_id
               or "deliver" in (getattr(r, "action_type", "") or "") for r in rows), \
        "expected an audit row for BYO bundle delivery"


async def test_soft_delete_should_audit(orch, captured, agent_id):
    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": OWNER}
    orch.ui_clients.append(ws)
    ok = await orch.delete_user_agent(OWNER, agent_id)
    assert ok is True
    rows = _rows(captured)
    assert any("delete" in (getattr(r, "action_type", "") or "")
               or getattr(r, "agent_id", None) == agent_id for r in rows), \
        "expected an audit row for BYO soft-delete"


async def test_refused_registration_should_audit(orch, captured, agent_id):
    from shared.protocol import AgentCard, AgentSkill, RegisterAgent
    ws = SimpleNamespace(is_user_agent_tunnel=True, owner_sub="someone-else",
                         host_session_id="hs-x", close=AsyncMock())
    card = AgentCard(name="G", description="g", agent_id=agent_id,
                     skills=[AgentSkill(name="greet", description="g", id="greet",
                                        scope="tools:read", input_schema={})])
    await orch.register_agent(ws, RegisterAgent(agent_card=card))
    assert agent_id not in orch.agents
    rows = _rows(captured)
    assert rows, "expected an audit row for a refused user-agent registration"


async def test_go_live_should_audit(orch, captured, agent_id):
    from shared.protocol import AgentCard, AgentSkill, RegisterAgent
    ws = SimpleNamespace(is_user_agent_tunnel=True, owner_sub=OWNER,
                         host_session_id="hs-1", close=AsyncMock())
    card = AgentCard(name="G", description="g", agent_id=agent_id,
                     skills=[AgentSkill(name="greet", description="g", id="greet",
                                        scope="tools:read", input_schema={})])
    await orch.register_agent(ws, RegisterAgent(agent_card=card))
    row = await _t(ua.get_user_agent, orch.user_agent_registry, agent_id)
    assert row["status"] == "live"
    rows = _rows(captured)
    assert any(getattr(r, "agent_id", None) == agent_id for r in rows), \
        "expected an audit row for a user-agent going live"


async def test_denied_tool_dispatch_should_audit(orch, captured, agent_id):
    from orchestrator.orchestrator import GateRefusal

    ws = FakeUI()
    orch.ui_sessions[ws] = {"sub": OWNER}
    orch._authorize_and_prepare = AsyncMock(return_value=GateRefusal(
        response=MCPResponse(error={"message": "This tool is disabled in your permissions."}),
        render_components=None, render_target=None))
    tool_call = SimpleNamespace(function=SimpleNamespace(name="greet", arguments="{}"))
    resp = await orch.execute_single_tool(
        ws, tool_call, {"greet": agent_id}, chat_id="c1", user_id=OWNER)
    assert resp is not None and resp.error is not None
    rows = _rows(captured)
    assert rows, "expected an audit row for a denied user-agent tool dispatch"
