"""T010 (056-delegated-agent-chaining): dispatch-path gate parity (FR-017).

For each gate, the SAME violating call is driven down the single path
(``execute_single_tool``) and the parallel batch (``execute_parallel_tools``)
and must refuse with an identical error, with equivalent audit evidence on
allowed dispatches. The chained-hop leg re-enters ``execute_single_tool``
via the same shared authorizer, so a hop leg (added with the US1 seam in
``test_chain_hop.py``) inherits this parity for free — SC-006.
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


@pytest.fixture
def orch(orchestrator_factory):
    o = orchestrator_factory()
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
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


async def _parallel(orch, tool="t1", agent="a1", args=None):
    results = await orch.execute_parallel_tools(
        MagicMock(), [_tc(tool, args)], {tool: agent}, "c1", user_id="u1")
    resp = results[0]
    return (resp.error or {}).get("message", "") if resp else ""


VIOLATIONS = [
    "security_flag", "permission", "policy", "taint",
    "supervisor", "hitl", "delegation_required", "cap",
]


def _arm(orch, violation, monkeypatch):
    """Configure one gate to refuse, mirroring quickstart §US3's matrix."""
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
    # Supervisor triggers on destructive verbs; HITL on egress verbs.
    return {"supervisor": "delete_records", "hitl": "send_email"}.get(violation, "t1")


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", VIOLATIONS)
async def test_same_refusal_on_single_and_parallel(orch, monkeypatch, violation):
    pre = _arm(orch, violation, monkeypatch)
    tool = _tool_for(violation)
    if pre == "cap":
        for i in range(orch.concurrency_cap.max_per_user_agent):
            assert await orch.concurrency_cap.acquire("u1", "a1", f"pre{i}")
    single_msg = await _single(orch, tool=tool)
    parallel_msg = await _parallel(orch, tool=tool)
    assert single_msg, f"{violation}: single path did not refuse"
    assert single_msg == parallel_msg, (
        f"{violation}: refusal diverged\n single:   {single_msg}\n parallel: {parallel_msg}")


@pytest.mark.asyncio
async def test_no_agent_refusal_matches(orch):
    single_msg = await _single(orch, agent="ghost")
    parallel_msg = await _parallel(orch, agent="ghost")
    assert "No agent available" in single_msg
    assert single_msg == parallel_msg


@pytest.mark.asyncio
async def test_parallel_now_mints_delegation_token(orch, monkeypatch):
    """The parallel path previously dispatched UNSCOPED; it must now carry
    the same delegation token a single call would (quickstart §US3 step 2)."""
    seen_args = {}

    async def _capture(ws, agent_id, tool_name, args, **_dispatch_context):
        seen_args[tool_name] = dict(args)
        return MCPResponse(result="ok")

    monkeypatch.setattr(orch, "_execute_with_retry", _capture)
    monkeypatch.setattr(
        orch, "_get_delegation_token", AsyncMock(return_value="tok-123"))
    await _parallel(orch, tool="t1")
    assert seen_args["t1"].get("_delegation_token") == "tok-123"


@pytest.mark.asyncio
async def test_audit_rows_equivalent_on_both_paths(orch, monkeypatch):
    """An allowed dispatch emits the same paired agent_tool_call rows on
    either path (equivalent audit evidence, SC-006)."""
    rec = MagicMock()
    rec.record = AsyncMock()
    monkeypatch.setattr(audit_hooks, "get_recorder", lambda: rec)
    monkeypatch.setattr(
        orch, "_execute_with_retry",
        AsyncMock(return_value=MCPResponse(result="ok")))

    ws1, ws2 = MagicMock(), MagicMock()
    orch.ui_sessions[ws1] = {"sub": "u1"}
    orch.ui_sessions[ws2] = {"sub": "u1"}

    await orch.execute_single_tool(ws1, _tc("t1"), {"t1": "a1"}, "c1", user_id="u1")
    single_rows = [c.args[0] for c in rec.record.await_args_list]
    rec.record.reset_mock()
    await orch.execute_parallel_tools(ws2, [_tc("t1")], {"t1": "a1"}, "c1", user_id="u1")
    parallel_rows = [c.args[0] for c in rec.record.await_args_list]

    def _shape(rows):
        return [(r.event_class, r.action_type, r.actor_user_id,
                 r.auth_principal, r.outcome) for r in rows
                if r.event_class == "agent_tool_call"]

    assert _shape(single_rows) == _shape(parallel_rows)
    assert _shape(single_rows)  # both emitted the paired rows


@pytest.mark.asyncio
async def test_meta_tool_parity_in_parallel_batch(orch, monkeypatch):
    """__scheduler__/__memory__/__desktop_codegen__ now dispatch from a
    parallel batch exactly like __orchestrator__ (T008/FR-018)."""
    from orchestrator import desktop_codegen, memory_chat, scheduling_chat
    for mod in (scheduling_chat, memory_chat, desktop_codegen):
        monkeypatch.setattr(
            mod, "handle_meta_tool",
            AsyncMock(return_value=MCPResponse(result="meta-ok")))
    results = await orch.execute_parallel_tools(
        MagicMock(),
        [_tc("schedule_recurring_task"), _tc("remember"), _tc("offer_desktop_codegen")],
        {"schedule_recurring_task": "__scheduler__",
         "remember": "__memory__",
         "offer_desktop_codegen": "__desktop_codegen__"},
        "c1", user_id="u1")
    assert [r.result for r in results] == ["meta-ok", "meta-ok", "meta-ok"]


@pytest.mark.asyncio
async def test_real_agent_hop_cannot_reach_meta_tools(orch):
    """The meta-tool exemption is structurally closed to real agent ids: a
    call resolved to a NON-reserved agent id named like a meta-tool falls
    through the normal gates (here: no such registered agent)."""
    msg = await _single(orch, tool="create_capability", agent="evil-agent-1")
    assert "No agent available" in msg
