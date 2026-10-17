"""Checks owner REST requests wait for single-use human terminal approval.
The existing durable proposal gate is exercised with its owning repository test adapter.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import remote_confirmation as rc, terminal_approvals as approvals
from orchestrator.terminal_security import TerminalError
from shared.protocol import MCPResponse
from shared.feature_flags import flags
from tests.test_remote_confirmation_063 import _FakeDB, _orch


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setitem(flags._flags, "terminal_shell", True)
    db = _FakeDB()
    orch = _orch(db)
    socket = object()
    claims = {"sub": "owner", "exp": time.time() + 600, "realm_access": {"roles": ["user"]}}
    orch.ui_sessions[socket] = claims
    orch.terminal_sessions = SimpleNamespace(runtime_binding=lambda *a, **k: "a" * 64,
        runtime_preview=lambda *a, **k: "isolated.test:2222 as runner",
        runtime_machine_id=lambda *a, **k: "b" * 32)
    orch.security_flags = {}
    orch.rote = SimpleNamespace(get_profile=lambda socket: SimpleNamespace(device_type="browser"))
    orch._conversation_scopes = {}
    orch._ws_active_chat = {}
    orch._terminal_review_scoped = set()
    orch.tool_permissions = SimpleNamespace(is_tool_allowed=lambda *a: True)
    calls = []

    def current():
        calls.append("current")

    return SimpleNamespace(db=db, orch=orch, socket=socket, claims=claims, check=current, calls=calls)


def propose(fixture, *, check=None):
    request = approvals.PendingTerminalRequest(fixture.orch, "owner", "start_terminal", check or fixture.check)
    with approvals.request_scope(request):
        result = rc.evaluate(fixture.orch, None, "terminal-shell-1", "start_terminal", {}, None, "owner")
    assert rc.is_approval_card_result(result[1])
    return request, result[1][0]


@pytest.mark.asyncio
async def test_rest_proposal_never_executes_until_real_owner_approves(fixture):
    request, card = propose(fixture)
    assert fixture.orch.execute_single_tool.calls == []
    assert request.public("owner")["status"] == "pending"
    dispatched = []

    async def execute(socket, call, mapping, chat_id, *, user_id):
        dispatched.append((socket, user_id, chat_id))
        assert callable(approvals.current_effect_check(fixture.orch, "owner", "start_terminal"))
        return MCPResponse(result={"session": {"session_id": "new-session", "cols": 80}, "private": "not-public"})

    fixture.orch.execute_single_tool = execute
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", card["content"][-2]["payload"])
    assert dispatched == [(fixture.socket, "owner", None)]
    assert request.public("owner") == {"proposal_id": request.proposal_id, "status": "succeeded",
                                      "session": {"session_id": "new-session", "cols": 80}}
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", card["content"][-2]["payload"])
    assert len(dispatched) == 1
    assert fixture.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["request", "foreign", "machine", "rest", "expiry", "restart"])
async def test_changed_request_or_nonhuman_cannot_execute_or_recover_result(fixture, loss):
    current = {"valid": True}

    def check():
        if not current["valid"]:
            raise TerminalError("terminal_authority_changed", 401)

    request, card = propose(fixture, check=check)
    fixture.orch.execute_single_tool = AsyncMock()
    actor = "owner"
    if loss == "request":
        current["valid"] = False
    elif loss == "foreign":
        actor = "foreign"
    elif loss == "machine":
        fixture.claims["machine_class"] = "scheduler"
    elif loss == "rest":
        fixture.claims["_invocation_channel"] = "rest"
    elif loss == "expiry":
        fixture.claims["exp"] = 0
    else:
        fixture.orch._terminal_approval_requests.clear()
    await rc.handle_decision(fixture.orch, fixture.socket, actor, card["content"][-2]["payload"])
    fixture.orch.execute_single_tool.assert_not_awaited()
    with pytest.raises(TerminalError):
        request.public("foreign")


@pytest.mark.asyncio
async def test_decline_is_observable_and_cannot_be_replayed_as_approve(fixture):
    request, card = propose(fixture)
    fixture.orch.execute_single_tool = AsyncMock()
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", card["content"][-1]["payload"])
    assert request.public("owner")["status"] == "declined"
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", card["content"][-2]["payload"])
    fixture.orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_failure_has_safe_code_and_never_exposes_private_text(fixture):
    request, card = propose(fixture)
    fixture.orch.execute_single_tool = AsyncMock(side_effect=RuntimeError("private endpoint credential"))
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", card["content"][-2]["payload"])
    assert request.public("owner")["error"] == "terminal_dispatch_unavailable"
    assert "private" not in str(request.public("owner"))


def test_context_cannot_be_reused_for_another_owner_tool_or_host(fixture):
    request = approvals.PendingTerminalRequest(fixture.orch, "owner", "start_terminal", fixture.check)
    with approvals.request_scope(request):
        assert approvals.current_request(fixture.orch, "owner", "start_terminal") is request
        assert approvals.current_request(fixture.orch, "other", "start_terminal") is None
        assert approvals.current_request(fixture.orch, "owner", "terminal_write") is None
        assert approvals.current_request(object(), "owner", "start_terminal") is None
    assert approvals.current_request(fixture.orch, "owner", "start_terminal") is None


def test_expired_handoff_is_not_retained_or_delivered(fixture, monkeypatch):
    clock = {"now": 10.0}
    monkeypatch.setattr(approvals, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    request, _ = propose(fixture)
    clock["now"] = 1000.0
    with pytest.raises(TerminalError, match="terminal_approval_expired"):
        request.public("owner")
    with pytest.raises(TerminalError, match="terminal_approval_not_found"):
        approvals.get_request(fixture.orch, "owner", request.proposal_id)
    assert fixture.orch._terminal_approval_requests == {}


@pytest.mark.parametrize("encoded", ["aWQK", "AP8=", "invalid@"])
def test_approval_preview_identifies_target_and_exact_text_or_binary_bytes(fixture, encoded):
    args = {"runtime_binding": "a" * 64, "data_b64": encoded}
    if encoded == "invalid@":
        with pytest.raises(TerminalError, match="terminal_input_invalid"):
            rc._terminal_summary(fixture.orch, "owner", "terminal_write", args)
    else:
        summary = rc._terminal_summary(fixture.orch, "owner", "terminal_write", args)
        assert "isolated.test:2222 as runner" in summary
        assert ("input_text" in summary and "id" in summary) if encoded == "aWQK" else "input_base64" in summary
        assert "input_sha256" in summary and "a" * 64 not in summary


@pytest.mark.parametrize("args", [{"data": ""}, {"data": "x" * 16385}, {"data": 3},
    {"data_b64": ""}, {"data_b64": "a" * 21849}, {"data_b64": 3}, {"data_b64": "eHh4" * 5462}])
def test_empty_malformed_or_oversize_input_never_becomes_a_durable_proposal(fixture, args):
    with pytest.raises(TerminalError, match="terminal_input_invalid"):
        rc.evaluate(fixture.orch, fixture.socket, "terminal-shell-1", "terminal_write", args, None, "owner")
    assert fixture.db.rows == {}


def test_pending_capacity_refuses_without_replacing_an_existing_request(fixture, monkeypatch):
    monkeypatch.setattr(approvals, "MAX_PENDING_PER_OWNER", 1)
    request, _ = propose(fixture)
    with pytest.raises(TerminalError, match="terminal_approval_capacity"):
        propose(fixture)
    assert approvals.get_request(fixture.orch, "owner", request.proposal_id) is request
    assert len(fixture.db.rows) == 1


@pytest.mark.asyncio
async def test_undelivered_or_failed_request_cannot_be_approved(fixture):
    request, card = propose(fixture)
    request.status = "failed"
    fixture.orch.execute_single_tool = AsyncMock()
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", card["content"][-2]["payload"])
    fixture.orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status", [("connected", 202), ("disconnected", 409),
    ("capacity", 429), ("send_failure", 409), ("invalid_card", 503)])
async def test_http_request_publishes_approval_and_owner_polling_never_executes(fixture, monkeypatch, mode, status):
    import httpx
    from fastapi import FastAPI
    from orchestrator import terminal_api

    app = FastAPI()
    app.state.orchestrator = fixture.orch
    app.dependency_overrides[terminal_api._owner_claims] = lambda: fixture.claims
    app.include_router(terminal_api.terminal_router)
    monkeypatch.setattr(terminal_api, "require_terminal_execution", lambda *a: None)
    monkeypatch.setattr(terminal_api, "get_web_or_bearer_user_payload", AsyncMock(return_value=fixture.claims))
    fixture.orch.send_ui_render = AsyncMock(return_value=True)
    fixture.orch.execute_single_tool = AsyncMock()
    if mode == "disconnected":
        fixture.orch.ui_sessions.clear()
    elif mode == "send_failure":
        fixture.orch.send_ui_render.side_effect = RuntimeError("private socket failure")
    elif mode == "capacity":
        monkeypatch.setattr(approvals, "MAX_PENDING_PER_OWNER", 0)
    if mode == "connected":
        fixture.orch.ui_sessions[object()] = {**fixture.claims, "sub": "foreign"}
        fixture.orch.ui_sessions[object()] = {**fixture.claims, "_invocation_channel": "rest"}
        fixture.orch.ui_sessions[object()] = {**fixture.claims, "exp": 0}

    async def dispatch(**kwargs):
        result = rc.evaluate(fixture.orch, None, kwargs["agent_id"], kwargs["tool_name"],
                             kwargs["arguments"], None, kwargs["user_id"])
        return MCPResponse(result={"_data": {"status": "confirmation_required"}},
                            ui_components=[] if mode == "invalid_card" else result[1])

    fixture.orch.execute_authorized_tool = dispatch
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={"cols": 80, "rows": 24})
        assert response.status_code == status
        if mode == "connected":
            body = response.json()
            assert body["status"] == "pending"
            poll = await client.get("/api/terminals/approvals/" + body["proposal_id"])
            assert poll.json() == body and poll.headers["cache-control"] == "no-store"
            assert fixture.orch.send_ui_render.await_count == 1
            assert fixture.orch.send_ui_render.await_args.args[0] is fixture.socket
            fixture.claims["sub"] = "foreign"
            assert (await client.get("/api/terminals/approvals/" + body["proposal_id"])).status_code == 404
        else:
            assert response.json()["error"].startswith("terminal_")
            assert "private" not in response.text
    fixture.orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_human_decisions_execute_once(fixture):
    request, card = propose(fixture)
    fixture.orch.execute_single_tool = AsyncMock(return_value=MCPResponse(result={"session": {"session_id": "once"}}))
    payload = card["content"][-2]["payload"]
    await asyncio.gather(*(rc.handle_decision(fixture.orch, fixture.socket, "owner", payload) for _ in range(2)))
    assert fixture.orch.execute_single_tool.await_count == 1
    assert request.public("owner")["session"]["session_id"] == "once"
