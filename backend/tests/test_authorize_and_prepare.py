"""T004 (056-delegated-agent-chaining): the shared gate authorizer.

``Orchestrator._authorize_and_prepare`` is the single-path gate stack factored
into one reusable sequence (security flag, permission, policy, taint,
supervisor, HITL, path mapping, credentials, disabled-tool, no-agent,
delegation, PRE_TOOL_USE hook, concurrency cap). These tests drive each gate's
allow AND deny through the authorizer directly and assert the refusal surfaces
identically to the historical single path (``execute_single_tool`` consumes
the same object), so dispatch-path parity (US3/FR-017) has one enforcement
point to test against.
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


@pytest.fixture
def orch():
    from orchestrator.orchestrator import Orchestrator

    o = Orchestrator()
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    # Register a live agent so the allow path clears the no-agent check.
    o.local_agents["a1"] = MagicMock()
    return o


async def _auth(orch, tool="t1", agent="a1", *, user="u1", chat="c1", args=None, ws=None):
    return await orch._authorize_and_prepare(
        ws or MagicMock(), agent, tool, dict(args or {}), chat, user)


def _msg(outcome) -> str:
    assert isinstance(outcome, GateRefusal)
    return (outcome.response.error or {}).get("message", "")


# --------------------------------------------------------------------------- #
# Allow path
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_allow_returns_prepared_dispatch(orch):
    out = await _auth(orch, args={"q": "hi"})
    assert isinstance(out, PreparedDispatch)
    # Path-mapping step injected the chat/user identifiers.
    assert out.args["session_id"] == "c1"
    assert out.args["user_id"] == "u1"
    assert out.args["q"] == "hi"
    assert out.cap_job_id is None


@pytest.mark.asyncio
async def test_allow_injects_encrypted_credentials(orch):
    orch.credential_manager.get_agent_credentials_encrypted = MagicMock(
        return_value="enc-blob")
    out = await _auth(orch)
    assert isinstance(out, PreparedDispatch)
    assert out.args["_credentials"] == "enc-blob"
    assert out.args["_credentials_encrypted"] is True


# --------------------------------------------------------------------------- #
# Gate: system security-flag block
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_security_flag_block_refuses(orch):
    orch.security_flags["a1"] = {"t1": {"blocked": True, "reason": "unsafe"}}
    out = await _auth(orch)
    assert "system-blocked" in _msg(out)
    assert out.render_target == "chat"
    assert out.response.error["retryable"] is False


# --------------------------------------------------------------------------- #
# Gate: per-user tool permission
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_permission_denied_refuses(orch):
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    out = await _auth(orch)
    assert "restricted for this agent" in _msg(out)
    assert out.render_target is None  # default-canvas render, as today


# --------------------------------------------------------------------------- #
# Gate: policy engine (deny + rewrite)
# --------------------------------------------------------------------------- #

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
    # 055: the stream twin must fingerprint the REDACTED params.
    assert out.stream_params == {"q": "[redacted]"}


# --------------------------------------------------------------------------- #
# Gate: taint sink
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_taint_deny_refuses(orch, monkeypatch):
    from orchestrator import taint
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    monkeypatch.setattr(taint, "is_sink", lambda a, t: True)
    monkeypatch.setattr(taint, "check_flow", lambda trust: "deny")
    out = await _auth(orch)
    assert "untrusted" in _msg(out)


# --------------------------------------------------------------------------- #
# Gates: supervisor + HITL (env-driven, mirroring test_security_gates_wiring)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Gate: no-agent
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_unregistered_agent_refuses(orch):
    out = await _auth(orch, agent="ghost-agent")
    assert "No agent available" in _msg(out)
    assert out.render_target == "chat"
    # Historical shape: the response itself carries no ui_components; the
    # alert is rendered separately by the dispatch path.
    assert out.response.ui_components is None
    assert out.render_components


# --------------------------------------------------------------------------- #
# Gate: delegation required (production posture)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_delegation_required_refuses_without_token(orch, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    out = await _auth(orch)
    assert "delegated authorization" in _msg(out)
    assert out.render_target == "chat"
    assert out.response.ui_components is None


@pytest.mark.asyncio
async def test_delegation_refusal_names_permissions_not_the_realm(orch, monkeypatch):
    """An empty effective scope set on an authenticated turn is the user's
    permissions, not a realm misconfiguration — pointing them at the IdP is a
    dead end."""
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
    """No bound user token (an unconsented machine turn) is an authority
    problem, not a permissions one — even though its scope set is also empty."""
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


# --------------------------------------------------------------------------- #
# Gate: PRE_TOOL_USE hook
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_hook_block_refuses_without_render(orch, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "hook_system", True)
    orch.hooks.emit = AsyncMock(
        return_value=SimpleNamespace(action="block", reason="nope",
                                     modified_args=None))
    out = await _auth(orch)
    assert "blocked by hook" in _msg(out)
    # Hook blocks never rendered an alert on the single path.
    assert out.render_components is None


# --------------------------------------------------------------------------- #
# Gate: concurrency cap
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Parity: execute_single_tool surfaces the authorizer's refusal unchanged
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_single_path_surfaces_authorizer_refusal(orch):
    orch.security_flags["a1"] = {"t1": {"blocked": True, "reason": "unsafe"}}
    tc = SimpleNamespace(function=SimpleNamespace(name="t1", arguments=json.dumps({})))
    resp = await orch.execute_single_tool(
        MagicMock(), tc, {"t1": "a1"}, "c1", user_id="u1")
    direct = await _auth(orch)
    assert (resp.error or {}).get("message") == _msg(direct)
    # The single path rendered the refusal alert to the chat target.
    orch.send_ui_render.assert_awaited()
    assert orch.send_ui_render.await_args.kwargs.get("target") == "chat"


# --------------------------------------------------------------------------- #
# Gate: feature 063 destructive-operation confirmation (remote-compute-1 only)
# --------------------------------------------------------------------------- #

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
    assert out.response.ui_components == [card] and out.render_components == [card]
    ev.assert_called_once()


@pytest.mark.asyncio
async def test_remote_control_confirmation_none_proceeds(orch, monkeypatch):
    from orchestrator import remote_confirmation
    ev = MagicMock(return_value=None)  # non-destructive / already-approved → proceed
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
