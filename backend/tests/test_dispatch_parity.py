"""Tests that every dispatch gate in execute_single_tool refuses with a reason and that
the call leaves an audit row (orchestrator/policy.py, taint.py).
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import audit.hooks as audit_hooks  # noqa: E402
from shared.protocol import MCPResponse  # noqa: E402


class _AnyToolScopes(dict):
    def get(self, key, default=None):
        return "tools:read"


@pytest.fixture
def orch(orchestrator_factory, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    o = orchestrator_factory()
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
    o.tool_permissions.get_tool_scope_map = MagicMock(side_effect=lambda agent_id: {
        "t1": "tools:read", "delete_records": "tools:write", "send_email": "tools:write"
    } if agent_id == "a1" else {})
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    o.local_agents["a1"] = MagicMock()
    return o


def _tc(tool, args=None):
    return SimpleNamespace(
        function=SimpleNamespace(name=tool, arguments=json.dumps(args or {})))


async def _single(orch, tool="t1", agent="a1", args=None):
    resp = await orch.execute_single_tool(
        MagicMock(), _tc(tool, args), {tool: agent}, "c1", user_id="u1")
    return (resp.error or {}).get("message", "") if resp else ""


VIOLATIONS = [
    "security_flag", "permission", "policy", "taint",
    "supervisor", "hitl", "delegation_required", "cap",
]


def _arm(orch, violation, monkeypatch):
    if violation == "security_flag":
        orch.security_flags["a1"] = {"t1": {"blocked": True, "reason": "threat"}}
    elif violation == "permission":
        orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    elif violation == "policy":
        from orchestrator import policy
        monkeypatch.setattr(policy, "policy_enabled", lambda: True)
        monkeypatch.setattr(
            policy, "evaluate_policy",
            lambda rules, ctx: policy.PolicyDecision(
                effect=policy.DENY, reason="policy says no", rule_id="r1"))
    elif violation == "taint":
        from orchestrator import taint
        monkeypatch.setattr(taint, "taint_enabled", lambda: True)
        monkeypatch.setattr(taint, "is_sink", lambda a, t: True)
        monkeypatch.setattr(taint, "check_flow", lambda trust: "deny")
    elif violation == "supervisor":
        monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "true")
        orch._active_request = {"c1": "show my dashboard"}
    elif violation == "hitl":
        monkeypatch.setenv("FF_HITL_HIGHRISK", "true")
        orch._active_request = {"c1": "whatever"}
    elif violation == "delegation_required":
        monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    elif violation == "cap":
        monkeypatch.setattr(orch, "_is_long_running_tool", lambda a, t: True)
        return "cap"
    return None


def _tool_for(violation):
    return {"supervisor": "delete_records", "hitl": "send_email"}.get(violation, "t1")


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", VIOLATIONS)
async def test_each_gate_refuses_with_a_reason(orch, monkeypatch, violation):
    pre = _arm(orch, violation, monkeypatch)
    tool = _tool_for(violation)
    if pre == "cap":
        for i in range(orch.concurrency_cap.max_per_user_agent):
            assert await orch.concurrency_cap.acquire("u1", "a1", f"pre{i}")
    assert await _single(orch, tool=tool), f"{violation}: single path did not refuse"


@pytest.mark.asyncio
async def test_no_agent_refusal(orch):
    assert "effect declaration is unavailable" in await _single(orch, agent="ghost")


@pytest.mark.asyncio
async def test_single_path_records_an_agent_tool_call_audit_row(orch, monkeypatch):
    rec = MagicMock()
    rec.record = AsyncMock()
    monkeypatch.setattr(audit_hooks, "get_recorder", lambda: rec)
    monkeypatch.setattr(
        orch, "_execute_with_retry",
        AsyncMock(return_value=MCPResponse(result="ok")))
    ws = MagicMock()
    orch.ui_sessions[ws] = {"sub": "u1"}

    await orch.execute_single_tool(ws, _tc("t1"), {"t1": "a1"}, "c1", user_id="u1")
    rows = [c.args[0] for c in rec.record.await_args_list]
    shaped = [(r.event_class, r.actor_user_id, r.outcome) for r in rows
              if r.event_class == "agent_tool_call"]
    assert shaped


@pytest.mark.asyncio
async def test_real_agent_hop_cannot_reach_meta_tools(orch):
    msg = await _single(orch, tool="create_capability", agent="evil-agent-1")
    assert "effect declaration is unavailable" in msg
