"""Checks terminal handoffs retire after failed authentication or card delivery.
The ordinary ASGI route and UI sender retain current owner registration and delivery outcomes.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from orchestrator import remote_confirmation as rc, terminal_api, terminal_approvals as approvals
from orchestrator.orchestrator import Orchestrator
from shared.protocol import MCPResponse
from tests.test_terminal_approvals import fixture as fixture


@pytest.fixture
def handoff(fixture, monkeypatch):
    app = FastAPI()
    app.state.orchestrator = fixture.orch
    app.dependency_overrides[terminal_api._owner_claims] = lambda: fixture.claims
    app.include_router(terminal_api.terminal_router)
    monkeypatch.setattr(terminal_api, "require_terminal_execution", lambda *a: None)
    monkeypatch.setattr(terminal_api, "get_web_or_bearer_user_payload", AsyncMock(return_value=fixture.claims))
    fixture.orch.send_ui_render = AsyncMock(return_value=True)
    fixture.orch.execute_single_tool = AsyncMock(
        return_value=MCPResponse(result={"session": {"session_id": "must-not-execute"}}))
    state = {}

    async def dispatch(**kwargs):
        state["request"] = approvals.current_request(fixture.orch, "owner", "start_terminal")
        result = rc.evaluate(fixture.orch, None, kwargs["agent_id"], kwargs["tool_name"],
                             kwargs["arguments"], None, kwargs["user_id"])
        state["card"] = result[1][0]
        return MCPResponse(result={"_data": {"status": "confirmation_required"}}, ui_components=result[1])

    fixture.orch.execute_authorized_tool = dispatch
    return SimpleNamespace(app=app, fixture=fixture, state=state)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["authentication", "verification", "cancel_authentication", "cancel_send"])
async def test_interrupted_http_request_retires_proposal_before_later_decision(handoff, monkeypatch, failure):
    cancelled = failure.startswith("cancel_")
    error = asyncio.CancelledError() if cancelled else HTTPException(401, "Not authenticated")
    if failure in {"authentication", "cancel_authentication"}:
        monkeypatch.setattr(terminal_api, "get_web_or_bearer_user_payload", AsyncMock(side_effect=error))
    elif failure == "verification":
        monkeypatch.setattr(terminal_api, "verify_user", AsyncMock(side_effect=error))
    else:
        handoff.fixture.orch.send_ui_render.side_effect = error
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await client.post("/api/terminals", json={})
        else:
            assert (await client.post("/api/terminals", json={})).status_code == 401
    request = handoff.state["request"]
    assert request.status == "failed"
    assert request.error == "terminal_dispatch_unavailable"
    fixture = handoff.fixture
    fixture.orch.send_ui_render = AsyncMock(return_value=True)
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", handoff.state["card"]["content"][-2]["payload"])
    fixture.orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["false", "none", "replaced", "removed", "expired"])
async def test_card_requires_positive_delivery_and_same_current_owner_registration(handoff, outcome):
    fixture = handoff.fixture

    async def send(*args, **kwargs):
        if outcome == "replaced":
            fixture.orch.ui_sessions[fixture.socket] = dict(fixture.claims)
        elif outcome == "removed":
            fixture.orch.ui_sessions.pop(fixture.socket)
        elif outcome == "expired":
            fixture.claims["exp"] = 0
        return False if outcome == "false" else None if outcome == "none" else True

    fixture.orch.send_ui_render.side_effect = send
    async with httpx.AsyncClient(transport=httpx.ASGITransport(handoff.app), base_url="https://terminal.invalid") as client:
        response = await client.post("/api/terminals", json={})
    assert response.status_code == 409
    expected = ("terminal_review_context_required" if outcome in {"replaced", "removed", "expired"}
                else "terminal_human_session_required")
    assert response.json() == {"error": expected}
    assert handoff.state["request"].status == "failed"
    fixture.orch.send_ui_render = AsyncMock(return_value=True)
    await rc.handle_decision(fixture.orch, fixture.socket, "owner", handoff.state["card"]["content"][-2]["payload"])
    fixture.orch.execute_single_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_actual_ui_sender_reports_safe_send_outcome_without_changing_frame(monkeypatch, failure):
    import webrender

    monkeypatch.setattr(webrender, "target_for_profile", lambda profile: "web")
    monkeypatch.setattr(webrender, "render_for_target", lambda *a: "<p>bounded</p>")
    components = [{"type": "alert", "message": "bounded"}]
    socket = SimpleNamespace(send_text=AsyncMock(side_effect=RuntimeError("socket closed") if failure else None))
    orch = SimpleNamespace(rote=SimpleNamespace(adapt=lambda *a: components, get_profile=lambda *a: {}),
                           _scope_conversation_transient=lambda ws, data: data, _trace_frame=lambda *a, **k: None)
    orch._safe_send = lambda ws, data: Orchestrator._safe_send(orch, ws, data)
    assert await Orchestrator.send_ui_render(orch, socket, components, target="chat", speak=False) is (not failure)
    frame = json.loads(socket.send_text.await_args.args[0])
    assert frame["type"] == "ui_render" and frame["target"] == "chat" and frame["components"] == components
    assert socket.send_text.await_count == 1


@pytest.mark.asyncio
async def test_actual_ui_sender_reports_dispatch_suppression_without_sending(monkeypatch):
    from persistent_agents import dispatch_context

    monkeypatch.setattr(dispatch_context, "current_dispatch", lambda: object())
    orch = SimpleNamespace(_safe_send=AsyncMock())
    assert await Orchestrator.send_ui_render(orch, object(), []) is False
    orch._safe_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_ui_sender_preserves_chat_fanout_and_original_delivery_outcome(monkeypatch):
    import webrender
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

    monkeypatch.setattr(webrender, "target_for_profile", lambda profile: "web")
    monkeypatch.setattr(webrender, "render_for_target", lambda *a: "<p>bounded</p>")
    socket = VirtualWebSocket(BackgroundTask(task_id="owned", chat_id="", user_id="owner"))
    target = object()
    components = [{"type": "alert", "message": "bounded"}]
    orch = SimpleNamespace(rote=SimpleNamespace(adapt=lambda *a: components, get_profile=lambda *a: {}),
                           _safe_send=AsyncMock(return_value=False), _vws_fan_targets=lambda ws: [target],
                           send_ui_render=AsyncMock(return_value=True))
    assert await Orchestrator.send_ui_render(orch, socket, components, target="chat", speak=False) is False
    orch.send_ui_render.assert_awaited_once_with(target, components, target="chat", speak=False)
