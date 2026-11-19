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
