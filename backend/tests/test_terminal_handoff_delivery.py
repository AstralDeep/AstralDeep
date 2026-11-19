"""Checks the UI sender reports delivery and dispatch suppression while preserving chat fanout."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.orchestrator import Orchestrator


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
@pytest.mark.parametrize(("continuity", "owner", "chat", "delivered"), [
    (True, "owner", "chat", True),
    (True, "owner", "", False),
    (True, "", "chat", False),
    (False, "owner", "chat", False),
])
async def test_chat_fanout_stays_with_the_task_owner_and_chat(monkeypatch, continuity, owner, chat, delivered):
    import webrender
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    from shared.feature_flags import flags

    monkeypatch.setattr(webrender, "target_for_profile", lambda profile: "web")
    monkeypatch.setattr(webrender, "render_for_target", lambda *a: "<p>bounded</p>")
    monkeypatch.setitem(flags._flags, "bg_continuity", continuity)
    socket = VirtualWebSocket(BackgroundTask(task_id="owned", chat_id=chat, user_id=owner))
    target, other_chat, other_owner = object(), object(), object()
    components = [{"type": "alert", "message": "bounded"}]
    orch = Orchestrator.__new__(Orchestrator)
    orch.ui_clients = [socket, target, other_chat, other_owner]
    orch.ui_sessions = {ws: {"sub": "owner" if ws is not other_owner else "other"} for ws in orch.ui_clients}
    orch._ws_active_chat = {id(ws): "chat" if ws is not other_chat else "other-chat" for ws in orch.ui_clients}
    orch.rote = SimpleNamespace(adapt=lambda *a: components, get_profile=lambda *a: {})
    orch._safe_send = AsyncMock(return_value=False)
    orch.send_ui_render = AsyncMock(return_value=True)

    assert await Orchestrator.send_ui_render(orch, socket, components, target="chat", speak=False) is False
    if delivered:
        orch.send_ui_render.assert_awaited_once_with(target, components, target="chat", speak=False)
    else:
        orch.send_ui_render.assert_not_awaited()
