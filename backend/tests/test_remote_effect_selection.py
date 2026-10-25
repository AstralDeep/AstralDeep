"""Checks the selected web-session authority bridge and retry lifecycle for qualified SSH calls.
Real Plane sessions are refreshed while only the external IAM exchange is substituted.
"""

from datetime import datetime, timezone
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocket
from orchestrator import remote_effect_execution as effects, web_auth
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.work_admission import OperationOwner, OwnerScope
from orchestrator.work_reconciliation import ReconciliationRefused
from tests.test_remote_effect_execution_postgres import env as env, run

_REAL_REFRESH = effects.RemoteEffectSelection._refresh


def bridge(env, monkeypatch):
    monkeypatch.setattr(effects.RemoteEffectSelection, "_refresh", _REAL_REFRESH)
    socket = WebSocket({"type": "websocket", "headers": [(b"cookie", ("astral_session=" + web_auth._sign("session")).encode())]},
                       receive=AsyncMock(), send=AsyncMock())
    connection = SimpleNamespace(websocket=socket, closing=False, registered=True)
    env.socket, env.connection = socket, connection
    env.orch._connection_contexts = {id(socket): connection}
    env.orch.local_agents = {"remote-compute-1": object()}
    claims = {"sub": "owner", "exp": datetime.now(timezone.utc).timestamp() + 600,
              "realm_access": {"roles": ["user"]}}
    env.orch.ui_sessions = {socket: {**claims, "_raw_token": "fixture-access"}}
    monkeypatch.setattr(effects.auth, "verify_production_token", AsyncMock(return_value=claims))
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", AsyncMock(return_value={
        "access_token": "refreshed-access", "refresh_token": "refreshed-refresh"}))
    token = _CONNECTION_OPERATION_CONTEXT.set({"owner": OperationOwner(OwnerScope.USER, "owner", None),
                                             "execution_fence": env.claim.fence})
    return token


@pytest.mark.asyncio
async def test_selected_session_refreshes_and_binds_to_physical_send(env, monkeypatch):
    token = bridge(env, monkeypatch)
    try:
        selected = await effects.select_effect(env.orch, env.socket, "remote-compute-1", "make_directory",
            {"machine_id": env.machine, "path": "/fixture", "_delegation_token": "private"}, "owner")
        result, client = await run(env, monkeypatch, selected)
        assert result.ok and len(client.commands) == 1
        assert selected.closed
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["no_connection", "closing", "wrong_owner", "no_owner", "no_fence", "no_session", "wrong_claims", "offline"])
async def test_unsupported_or_missing_current_authority_refuses_selection(env, monkeypatch, bad):
    token = bridge(env, monkeypatch)
    try:
        if bad == "no_connection":
            env.orch._connection_contexts.clear()
        elif bad == "closing":
            env.connection.closing = True
        elif bad == "wrong_owner":
            _CONNECTION_OPERATION_CONTEXT.get()["owner"] = OperationOwner(OwnerScope.USER, "foreign", None)
        elif bad == "no_owner":
            _CONNECTION_OPERATION_CONTEXT.get()["owner"] = None
        elif bad == "no_fence":
            _CONNECTION_OPERATION_CONTEXT.get()["execution_fence"] = None
        elif bad == "no_session":
            env.socket.scope["headers"] = []
        elif bad == "wrong_claims":
            env.orch.ui_sessions[env.socket]["sub"] = "foreign"
        else:
            env.orch.local_agents.clear()
        with pytest.raises(ReconciliationRefused):
            await effects.select_effect(env.orch, env.socket, "remote-compute-1", "make_directory", {}, "owner")
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_replaced_session_cannot_refresh_original_selection(env, monkeypatch):
    token = bridge(env, monkeypatch)
    try:
        selected = await effects.select_effect(env.orch, env.socket, "remote-compute-1", "make_directory", {}, "owner")
        env.sessions.delete("session")
        env.sessions.create("session", user_id="owner", access_token="replacement", refresh_token="replacement", hard_max_seconds=3600)
        with pytest.raises(ReconciliationRefused, match="effect_session_replaced"):
            await selected._refresh()
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_qualified_retry_wrapper_authorizes_only_one_attempt(env, monkeypatch):
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPResponse
    token = bridge(env, monkeypatch)
    calls = []
    env.orch.MAX_RETRIES = 3
    env.orch._protected_dispatch_channel = lambda socket, explicit=None: "web"
    env.orch._execute_with_retry_loop = MethodType(Orchestrator._execute_with_retry_loop, env.orch)

    async def execute(*args, **kwargs):
        calls.append(1)
        return MCPResponse(error={"message": "reply lost", "retryable": True})

    env.orch.execute_tool_and_wait = execute
    try:
        result = await Orchestrator._execute_with_retry(env.orch, env.socket,
            "remote-compute-1", "make_directory", {}, user_id="owner")
        assert len(calls) == 1 and result.error["retryable"] is False
        env.orch._connection_contexts.clear()
        refused = await Orchestrator._execute_with_retry(env.orch, env.socket,
            "remote-compute-1", "make_directory", {}, user_id="owner")
        assert refused.error["code"] == "effect_placement_unsupported" and len(calls) == 1
        assert await effects.select_effect(env.orch, env.socket, "remote-compute-1", "list_directory", {}, "owner") is None
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_external_qualified_dispatch_has_no_transport_fallback(env):
    from orchestrator.orchestrator import Orchestrator
    env.orch.local_agents = {}
    env.orch._terminal_dispatch_refusal = lambda agent: None
    result = await Orchestrator._dispatch_tool_call(env.orch, "remote-compute-1", "remove_path",
        {"path": "/fixture"}, 1, env.socket, protected_owner_id="owner")
    assert result.error["code"] == "effect_placement_unsupported"
    assert result.error["retryable"] is False
