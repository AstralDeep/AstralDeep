"""Tests for Orchestrator._authorize_and_prepare, the single-path gate stack (security
flag, permission, policy, taint, supervisor, HITL, delegation, hooks, concurrency
cap) that execute_single_tool also consumes.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.orchestrator import GateRefusal, PreparedDispatch  # noqa: E402
from shared.protocol import AgentCard, AgentSkill  # noqa: E402


@pytest.fixture
def orch():
    from orchestrator.concurrency_cap import ConcurrencyCap
    from orchestrator.hooks import HookManager
    from orchestrator.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.agents = {}
    o.a2a_clients = {}
    o.local_agents = {"a1": MagicMock()}
    o.agent_cards = {}
    o.ui_sessions = {}
    o.security_flags = {}
    o._pending_cap_entries = {}
    o._hop_cap_entries = {}
    o._job_context = {}
    o.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
    o.hooks = HookManager()
    o.stream_manager = None

    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager = MagicMock()
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(
        return_value=None
    )
    o._llm_store = SimpleNamespace(
        get=AsyncMock(return_value=None),
        get_system=AsyncMock(return_value=None),
    )
    return o


async def _auth(orch, tool="t1", agent="a1", *, user="u1", chat="c1", args=None, ws=None):
    return await orch._authorize_and_prepare(
        ws or MagicMock(), agent, tool, dict(args or {}), chat, user)


def _msg(outcome) -> str:
    assert isinstance(outcome, GateRefusal)
    return (outcome.response.error or {}).get("message", "")


@pytest.mark.asyncio
async def test_allow_returns_prepared_dispatch(orch):
    out = await _auth(orch, args={"q": "hi"})
    assert isinstance(out, PreparedDispatch)
    assert out.args["session_id"] == "c1"
    assert out.args["user_id"] == "u1"
    assert out.args["q"] == "hi"
    assert out.cap_job_id is None


@pytest.mark.asyncio
async def test_strict_tool_schema_does_not_receive_undeclared_context(orch):
    orch.agent_cards["a1"] = AgentCard(
        name="Strict external agent",
        description="Rejects undeclared arguments",
        agent_id="a1",
        skills=[AgentSkill(
            id="t1",
            name="t1",
            description="strict tool",
            input_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "additionalProperties": False,
            },
        )],
    )
    out = await _auth(orch, args={"q": "hi"})
    assert isinstance(out, PreparedDispatch)
    assert out.args == {"q": "hi"}


@pytest.mark.asyncio
async def test_strict_tool_schema_receives_declared_context_only(orch):
    orch.agent_cards["a1"] = AgentCard(
        name="Context-aware agent",
        description="Declares user context but not session context",
        agent_id="a1",
        skills=[AgentSkill(
            id="t1",
            name="t1",
            description="context-aware tool",
            input_schema={
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "user_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        )],
    )
    out = await _auth(orch, args={"q": "hi"})
    assert isinstance(out, PreparedDispatch)
    assert out.args == {"q": "hi", "user_id": "u1"}


@pytest.mark.asyncio
async def test_partial_legacy_card_still_honours_strict_schema(orch):
    orch.agent_cards["a1"] = SimpleNamespace(skills=[SimpleNamespace(
        id="t1",
        input_schema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "additionalProperties": False,
        },
    )])
    out = await _auth(orch, args={"q": "hi"})
    assert isinstance(out, PreparedDispatch)
    assert out.args == {"q": "hi"}


@pytest.mark.asyncio
async def test_allow_injects_encrypted_credentials(orch):
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(
        return_value="enc-blob")
    out = await _auth(orch)
    assert isinstance(out, PreparedDispatch)
    assert out.args["_credentials"] == "enc-blob"
    assert out.args["_credentials_encrypted"] is True


@pytest.mark.asyncio
async def test_security_flag_block_refuses(orch):
    orch.security_flags["a1"] = {"t1": {"blocked": True, "reason": "unsafe"}}
    out = await _auth(orch)
    assert "system-blocked" in _msg(out)
    assert out.render_target == "chat"
    assert out.response.error["retryable"] is False


@pytest.mark.asyncio
async def test_permission_denied_refuses(orch):
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    out = await _auth(orch)
    assert "restricted for this agent" in _msg(out)
    assert out.render_target is None


@pytest.mark.asyncio
async def test_policy_deny_refuses(orch, monkeypatch):
    from orchestrator import policy
    monkeypatch.setattr(policy, "policy_enabled", lambda: True)
    monkeypatch.setattr(
        policy, "evaluate_policy",
        lambda rules, ctx: policy.PolicyDecision(
            effect=policy.DENY, reason="blocked by test rule", rule_id="r1"))
    out = await _auth(orch)
    assert "blocked by test rule" in _msg(out)


@pytest.mark.asyncio
async def test_policy_rewrite_updates_args_and_stream_params(orch, monkeypatch):
    from orchestrator import policy
    monkeypatch.setattr(policy, "policy_enabled", lambda: True)
    monkeypatch.setattr(
        policy, "evaluate_policy",
        lambda rules, ctx: policy.PolicyDecision(args={"q": "[redacted]"}))
    out = await _auth(orch, args={"q": "secret"})
    assert isinstance(out, PreparedDispatch)
    assert out.args["q"] == "[redacted]"
    assert out.stream_params == {"q": "[redacted]"}


@pytest.mark.asyncio
async def test_taint_deny_refuses(orch, monkeypatch):
    from orchestrator import taint
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    monkeypatch.setattr(taint, "is_sink", lambda a, t: True)
    monkeypatch.setattr(taint, "check_flow", lambda trust: "deny")
    out = await _auth(orch)
    assert "untrusted" in _msg(out)


@pytest.mark.asyncio
async def test_supervisor_blocks_unrequested_destructive(orch, monkeypatch):
    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "true")
    orch._active_request = {"c1": "show me my dashboard"}
    out = await _auth(orch, tool="delete_records")
    assert "didn't ask for" in _msg(out)


@pytest.mark.asyncio
async def test_hitl_blocks_egress(orch, monkeypatch):
    monkeypatch.setenv("FF_HITL_HIGHRISK", "true")
    orch._active_request = {"c1": "email bob"}
    out = await _auth(orch, tool="send_email")
    assert "confirm" in _msg(out).lower()


@pytest.mark.asyncio
async def test_unregistered_agent_refuses(orch):
    out = await _auth(orch, agent="ghost-agent")
    assert "No agent available" in _msg(out)
    assert out.render_target == "chat"
    assert out.response.ui_components is None
    assert out.render_components


@pytest.mark.asyncio
async def test_delegation_required_refuses_without_token(orch, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    out = await _auth(orch)
    assert "delegated authorization" in _msg(out)
    assert out.render_target == "chat"
    assert out.response.ui_components is None


@pytest.mark.asyncio
async def test_delegation_refusal_names_permissions_not_the_realm(orch, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    ws = MagicMock()
    orch.ui_sessions[ws] = {"_raw_token": "user-token"}
    orch.tool_permissions.get_enabled_scope_names = MagicMock(return_value=[])
    out = await _auth(orch, ws=ws)
    msg = _msg(out)
    assert "tool permissions" in msg
    assert "Agents & permissions" in msg
    assert "identity provider" not in msg


@pytest.mark.asyncio
async def test_unbound_turn_refusal_names_the_exchange(orch, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    orch.tool_permissions.get_enabled_scope_names = MagicMock(return_value=[])
    out = await _auth(orch)
    assert "delegated authorization" in _msg(out)


@pytest.mark.asyncio
async def test_delegation_optional_passes_without_token(orch, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    out = await _auth(orch)
    assert isinstance(out, PreparedDispatch)
    assert out.delegation_token is None


@pytest.mark.asyncio
async def test_hook_block_refuses_without_render(orch, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "hook_system", True)
    orch.hooks.emit = AsyncMock(
        return_value=SimpleNamespace(action="block", reason="nope",
                                     modified_args=None))
    out = await _auth(orch)
    assert "blocked by hook" in _msg(out)
    assert out.render_components is None


@pytest.mark.asyncio
async def test_cap_acquired_on_long_running(orch, monkeypatch):
    monkeypatch.setattr(orch, "_is_long_running_tool", lambda a, t: True)
    out = await _auth(orch)
    assert isinstance(out, PreparedDispatch)
    assert out.cap_job_id is not None
    assert out.args["_cap_job_id"] == out.cap_job_id
    assert orch._pending_cap_entries[out.cap_job_id] == ("u1", "a1")
    assert orch._job_context[out.cap_job_id]["chat_id"] == "c1"


@pytest.mark.asyncio
async def test_cap_exceeded_refuses(orch, monkeypatch):
    monkeypatch.setattr(orch, "_is_long_running_tool", lambda a, t: True)
    for i in range(orch.concurrency_cap.max_per_user_agent):
        assert await orch.concurrency_cap.acquire("u1", "a1", f"job{i}")
    out = await _auth(orch)
    assert "jobs running" in _msg(out)
    assert out.render_target == "chat"


@pytest.mark.asyncio
async def test_single_path_surfaces_authorizer_refusal(orch):
    orch.security_flags["a1"] = {"t1": {"blocked": True, "reason": "unsafe"}}
    tc = SimpleNamespace(function=SimpleNamespace(name="t1", arguments=json.dumps({})))
    resp = await orch.execute_single_tool(
        MagicMock(), tc, {"t1": "a1"}, "c1", user_id="u1")
    direct = await _auth(orch)
    assert (resp.error or {}).get("message") == _msg(direct)
    orch.send_ui_render.assert_awaited()
    assert orch.send_ui_render.await_args.kwargs.get("target") == "chat"


@pytest.mark.asyncio
async def test_remote_control_confirmation_refusal_becomes_gate_refusal(orch, monkeypatch):
    from orchestrator import remote_confirmation
    card = {"type": "card", "title": "Confirm"}
    ev = MagicMock(return_value=("confirmation_required: approve", [card]))
    monkeypatch.setattr(remote_confirmation, "evaluate", ev)
    out = await _auth(orch, tool="cancel_job", agent="remote-compute-1", args={"job_id": "1"})
    assert isinstance(out, GateRefusal)
    assert "confirmation_required" in _msg(out)
    assert out.render_target == "chat"
    assert out.response.ui_components is None
    assert out.render_components == [card]
    ev.assert_called_once()


@pytest.mark.asyncio
async def test_remote_control_confirmation_none_proceeds(orch, monkeypatch):
    from orchestrator import remote_confirmation
    ev = MagicMock(return_value=None)
    monkeypatch.setattr(remote_confirmation, "evaluate", ev)
    orch.local_agents["remote-compute-1"] = MagicMock()
    out = await _auth(orch, tool="make_directory", agent="remote-compute-1", args={"path": "/x"})
    assert isinstance(out, PreparedDispatch)
    ev.assert_called_once()


@pytest.mark.asyncio
async def test_confirmation_gate_skipped_for_other_agents(orch, monkeypatch):
    from orchestrator import remote_confirmation
    ev = MagicMock(side_effect=AssertionError("confirmation gate must not run for other agents"))
    monkeypatch.setattr(remote_confirmation, "evaluate", ev)
    out = await _auth(orch, tool="t1", agent="a1")
    assert isinstance(out, PreparedDispatch)
    ev.assert_not_called()
