"""Tests that orchestrator.py's turn cancel interrupts an in-flight tool dispatch: the turn stops,
the dispatch is cancelled, a result that lands after the cancel is audited as late and never
rendered or returned to the model, and a cancel with no tool in flight behaves as before.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.protocol import MCPResponse
from tests.helpers.registered_human import registered_chat
from tests.test_analysis_render_targets import (
    USER, _chat, _cleanup, _components_json, _msg, _register, _tc, _usage, _ws,
    generated_agent_bundles as generated_agent_bundles, orch as orch, plane_runtime as plane_runtime,
)

pytestmark = pytest.mark.asyncio


def _llm(orch):
    calls = []

    async def fake(websocket, messages, tools_desc=None, temperature=None, feature="tool_dispatch"):
        calls.append(messages)
        if len(calls) == 1:
            return _msg(tool_calls=[_tc()]), _usage()
        return _msg(content="Done."), _usage()

    orch._call_llm = fake
    return calls


def _late_audits(orch):
    return [call.args[0] for call in orch.audit_recorder.record.await_args_list
            if getattr(call.args[0], "action_type", "") == "tool.forecast_tool.late_after_cancel"]


def _cancel_alerts(orch):
    return [c for c in orch.send_ui_render.await_args_list if "cancelled" in _components_json(c)]


async def test_cancel_during_a_slow_tool_cancels_the_dispatch_and_stops_the_turn(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    calls = _llm(orch)
    started, cancelled = asyncio.Event(), []

    async def slow(websocket, tc, mapping, chat, user_id=None, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(tc.function.name)
            raise

    orch.execute_single_tool = slow
    turn = asyncio.create_task(registered_chat(orch, ws, "forecast please", chat_id, user_id=USER))
    await asyncio.wait_for(started.wait(), 5)
    await orch._cancel_turn(ws, USER, chat_id)
    await asyncio.wait_for(turn, 5)
    assert cancelled == ["forecast_tool"] and len(calls) == 1
    assert _cancel_alerts(orch) and not _late_audits(orch)
    orch._deliver_round_components.assert_not_awaited()
    assert not getattr(orch, "_turn_tool_tasks", {}) and not getattr(orch, "_tool_cancel_requests", set())
    assert turn.cancelling() == 0
    await _cleanup(orch, chat_id)


async def test_a_result_landing_after_the_cancel_is_audited_and_never_rendered_or_returned(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    calls = _llm(orch)

    async def finishes_late(websocket, tc, mapping, chat, user_id=None, **_kwargs):
        orch.cancelled_sessions[id(websocket)] = True
        return SimpleNamespace(result={"_data": {"done": "word document done"}}, error=None,
                               ui_components=[{"type": "text", "content": "word document done"}],
                               correlation_id=None)

    orch.execute_single_tool = finishes_late
    await registered_chat(orch, ws, "forecast please", chat_id, user_id=USER)
    assert len(calls) == 1
    late, = _late_audits(orch)
    assert late.outcome == "interrupted" and late.conversation_id == chat_id
    orch._deliver_round_components.assert_not_awaited()
    assert not any("word document done" in _components_json(c) for c in orch.send_ui_render.await_args_list)
    assert _cancel_alerts(orch)
    await _cleanup(orch, chat_id)


async def test_a_cancel_with_no_tool_in_flight_behaves_as_before(orch):
    ws = _ws(orch)
    chat_id = await _chat(orch)
    await orch._cancel_turn(ws, USER, chat_id)
    assert orch.cancelled_sessions[id(ws)] is True
    sent = [json.loads(call.args[1]) for call in orch._safe_send.await_args_list]
    assert {"type": "chat_status", "status": "done", "message": "Cancelled"} in sent
    assert not _late_audits(orch)
    await _cleanup(orch, chat_id)


async def test_an_unrelated_cancellation_of_the_turn_still_propagates(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    _llm(orch)
    started = asyncio.Event()

    async def slow(websocket, tc, mapping, chat, user_id=None, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    orch.execute_single_tool = slow
    turn = asyncio.create_task(registered_chat(orch, ws, "forecast please", chat_id, user_id=USER))
    await asyncio.wait_for(started.wait(), 5)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert not getattr(orch, "_turn_tool_tasks", {})
    await _cleanup(orch, chat_id)


async def test_a_response_for_a_cancelled_dispatch_is_audited_as_late(orch):
    ws = _ws(orch)
    orch._ws_active_chat[id(ws)] = "chat-late"
    orch._note_cancelled_request("req_forecast_tool_late", "a-1", "forecast_tool", ws)
    response = MCPResponse(request_id="req_forecast_tool_late", result={"_data": {"ok": True}})
    await orch.handle_agent_message(object(), response.to_json())
    late, = _late_audits(orch)
    assert late.actor_user_id == USER and late.conversation_id == "chat-late" and late.agent_id == "a-1"
    assert "req_forecast_tool_late" not in orch._late_tool_requests
    await orch.handle_agent_message(object(), response.to_json())
    assert len(_late_audits(orch)) == 1


async def test_late_request_records_are_bounded_and_expire(orch, monkeypatch):
    from orchestrator import orchestrator as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    for index in range(300):
        orch._note_cancelled_request(f"req_{index}", "a-1", "forecast_tool", None)
    assert len(orch._late_tool_requests) == 256
    clock[0] += 601
    orch._note_cancelled_request("req_fresh", "a-1", "forecast_tool", None)
    assert list(orch._late_tool_requests) == ["req_fresh"]


async def test_late_audit_without_an_owner_or_recorder_only_logs(orch):
    orch.audit_recorder.record = AsyncMock(side_effect=RuntimeError("audit store down"))
    await orch._audit_late_tool_result(USER, "chat", "a-1", "forecast_tool")
    await orch._audit_late_tool_result(None, "chat", "a-1", "forecast_tool")
    assert orch.audit_recorder.record.await_count == 1
