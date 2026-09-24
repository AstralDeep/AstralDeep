"""Tests for scheduler/runner.py and orchestrator/chain_authority.py: a scheduled run
derives authority narrowed to consented-and-current scopes so tools dispatch
delegated, pausing with one notification when authority can't be derived.
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
from scheduler.runner import JobRunner  # noqa: E402
from shared.protocol import MCPResponse  # noqa: E402


def _job(**over):
    j = {
        "id": "job-1", "user_id": "u1", "name": "arXiv sweep",
        "instruction": "check arXiv for new SDUI papers",
        "agent_id": "web-research-1", "consented_scopes": ["tools:read", "tools:search"],
        "offline_grant_id": "grant-1", "schedule_kind": "cron",
        "schedule_expr": "0 8 * * *", "timezone": "UTC",
        "target_chat_id": "c1", "status": "active",
    }
    j.update(over)
    return j


def _store():
    st = MagicMock()
    st.start_run = MagicMock(return_value="run-1")
    st.finish_run = MagicMock()
    st.set_status = MagicMock()
    st.update_after_run = MagicMock()
    return st


def _grants(valid=True, token="consent-token"):
    g = MagicMock()
    g.is_valid = MagicMock(return_value=valid)
    g.latest_valid_for = MagicMock(return_value=None)
    g.mint_access_token = AsyncMock(return_value=token)
    return g


def _orch(current_scopes=None):
    scopes = ({"tools:read": True, "tools:search": True}
              if current_scopes is None else current_scopes)
    o = MagicMock()
    o.tool_permissions.get_agent_scopes = MagicMock(return_value=dict(scopes))
    o.tool_permissions.get_enabled_scope_names = MagicMock(
        return_value=[s for s, on in scopes.items() if on])
    o.run_scheduled_turn = AsyncMock(return_value="did the thing")
    o.notify_user = AsyncMock()
    return o


@pytest.mark.asyncio
async def test_scheduled_run_threads_consent_derived_authority():
    orch, store, grants = _orch(), _store(), _grants()
    outcome = await JobRunner(orch, store, grants).run_job(_job())
    assert outcome == "success"
    grants.mint_access_token.assert_awaited_once_with(
        "grant-1",
        user_id="u1",
    )
    kwargs = orch.run_scheduled_turn.await_args.kwargs
    authority = kwargs["authority"]
    assert isinstance(authority, MachineAuthority)
    assert authority.access_token == "consent-token"
    assert authority.principal == "machine:scheduled_job"
    assert authority.consent_ref == "grant-1"
    assert kwargs["allowed_scopes"] == ["tools:read", "tools:search"]


@pytest.mark.asyncio
async def test_authority_never_wider_than_current_grants():
    orch = _orch(current_scopes={"tools:read": True, "tools:search": False})
    store, grants = _store(), _grants()
    await JobRunner(orch, store, grants).run_job(_job())
    kwargs = orch.run_scheduled_turn.await_args.kwargs
    assert kwargs["allowed_scopes"] == ["tools:read"]
    assert kwargs["authority"].allowed_scopes == ["tools:read"]


@pytest.mark.asyncio
async def test_safe_baseline_user_is_not_mistaken_for_having_no_grants():
    orch = _orch()
    orch.tool_permissions.get_agent_scopes = MagicMock(return_value={})
    orch.tool_permissions.get_enabled_scope_names = MagicMock(
        return_value=["tools:read", "tools:search"])
    store, grants = _store(), _grants()

    outcome = await JobRunner(orch, store, grants).run_job(_job())

    assert outcome == "success"
    kwargs = orch.run_scheduled_turn.await_args.kwargs
    assert kwargs["allowed_scopes"] == ["tools:read", "tools:search"]
    orch.tool_permissions.get_agent_scopes.assert_not_called()


@pytest.mark.asyncio
async def test_effective_revocation_still_skips():
    orch = _orch()
    orch.tool_permissions.get_enabled_scope_names = MagicMock(return_value=[])
    store, grants = _store(), _grants()

    outcome = await JobRunner(orch, store, grants).run_job(_job())

    assert outcome == "skipped_auth"
    orch.run_scheduled_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("setup,reason", [
    (lambda g: setattr(g, "is_valid", MagicMock(return_value=False)), "revoked"),
    (lambda g: setattr(g, "mint_access_token",
                       AsyncMock(side_effect=RuntimeError("kc revoked"))), "mint"),
])
async def test_skip_dispatches_nothing_and_pauses(setup, reason):
    orch, store, grants = _orch(), _store(), _grants()
    setup(grants)
    outcome = await JobRunner(orch, store, grants).run_job(_job())
    assert outcome == "skipped_auth"
    orch.run_scheduled_turn.assert_not_awaited()
    assert store.finish_run.call_args.kwargs["outcome"] == "skipped_auth"
    store.set_status.assert_called_once_with("u1", "job-1", "paused")
    orch.notify_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_consent_skips():
    orch, store, grants = _orch(), _store(), _grants()
    outcome = await JobRunner(orch, store, grants).run_job(
        _job(offline_grant_id=None))
    assert outcome == "skipped_auth"
    orch.run_scheduled_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_scope_intersection_skips():
    orch = _orch(current_scopes={"tools:read": False, "tools:search": False})
    store, grants = _store(), _grants()
    outcome = await JobRunner(orch, store, grants).run_job(_job())
    assert outcome == "skipped_auth"
    orch.run_scheduled_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_pauses_with_one_notification():
    orch, store, grants = _orch(), _store(), _grants(valid=False)
    runner = JobRunner(orch, store, grants)
    for _ in range(4):
        assert await runner.run_job(_job()) == "skipped_auth"
    assert orch.notify_user.await_count == 1
    grants.is_valid = MagicMock(return_value=True)
    assert await runner.run_job(_job()) == "success"
    grants.is_valid = MagicMock(return_value=False)
    assert await runner.run_job(_job()) == "skipped_auth"
    assert orch.notify_user.await_count == 3


@pytest.fixture
def real_orch(orchestrator_factory):
    o = orchestrator_factory()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.get_tool_scope = MagicMock(return_value="tools:read")
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
async def test_bound_machine_turn_dispatches_delegated(real_orch, monkeypatch):
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

    vws = VirtualWebSocket(BackgroundTask(task_id="t", chat_id="c1", user_id="u1"))
    real_orch._bind_machine_turn(vws, _authority())

    seen = {}

    async def _exchange(raw, agent, tools, uid, scopes):
        seen.update(raw=raw, agent=agent, user=uid)
        return {"access_token": "delegated-from-consent"}

    real_orch.delegation.exchange_token_for_agent = _exchange
    real_orch.agent_cards["a1"] = SimpleNamespace(skills=[SimpleNamespace(id="t1")])
    real_orch.tool_permissions.get_enabled_scope_names = MagicMock(
        return_value=["tools:read"])

    dispatched = {}

    async def _cap(ws, agent_id, tool_name, args, **_dispatch_context):
        dispatched.update(args=dict(args))
        return MCPResponse(result="ok")

    real_orch._execute_with_retry = _cap
    import json as _json
    tc = SimpleNamespace(function=SimpleNamespace(name="t1", arguments=_json.dumps({})))
    resp = await real_orch.execute_single_tool(
        vws, tc, {"t1": "a1"}, "c1", user_id="u1")

    assert resp.result == "ok", "machine turn must dispatch, not be refused"
    assert seen["raw"] == "consent-token"
    assert dispatched["args"]["_delegation_token"] == "delegated-from-consent"


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

    from orchestrator import delegation as dg
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
                "iss": "mock-astral-delegation", "aud": "svc",
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
