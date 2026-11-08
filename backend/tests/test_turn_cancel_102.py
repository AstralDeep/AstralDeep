"""Tests that orchestrator.py's turn cancel interrupts an in-flight tool dispatch: the turn stops,
the dispatch is cancelled, a result that lands after the cancel is audited as late and never
rendered or returned to the model, and a cancel with no tool in flight behaves as before.
"""

from __future__ import annotations

import asyncio
import json
import uuid
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


async def test_a_cancelled_turn_keeps_the_user_message_in_the_saved_conversation(orch):
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
    await orch._cancel_turn(ws, USER, chat_id)
    await asyncio.wait_for(turn, 5)
    saved = (await asyncio.to_thread(orch.history.get_chat, chat_id, USER))["messages"]
    assert [message["role"] for message in saved] == ["user", "assistant"]
    assert "forecast please" in json.dumps(saved[0]["content"])
    assert "cancelled" in json.dumps(saved[1]["content"]).lower()


async def test_a_scheduled_turn_with_no_model_response_marks_its_stage_failed(orch):
    from orchestrator.scheduled_publication import stage_scheduled_history

    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch._call_llm = AsyncMock(return_value=(None, None))
    with stage_scheduled_history(
        history=orch.history, chat_id=chat_id, user_id=USER,
        create_chat_if_missing=False, agent_id=None,
    ) as stage:
        await registered_chat(orch, ws, "summarize inbox", chat_id, user_id=USER)
    assert stage.failed is True


async def test_a_cancel_during_the_model_call_runs_no_tools(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    tools = AsyncMock()
    orch.execute_single_tool = tools

    async def cancelled_while_thinking(*_args, **_kwargs):
        orch.cancelled_sessions[id(ws)] = True
        return _msg(tool_calls=[_tc()]), _usage()

    orch._call_llm = cancelled_while_thinking
    await registered_chat(orch, ws, "forecast please", chat_id, user_id=USER)
    tools.assert_not_awaited()
    sent = [json.loads(call.args[1]) for call in orch._safe_send.await_args_list]
    assert not [frame for frame in sent if frame.get("status") == "executing"]
    assert _cancel_alerts(orch)
    await _cleanup(orch, chat_id)


async def test_a_cancel_just_before_dispatch_runs_no_tools(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    tools = AsyncMock()
    orch.execute_single_tool = tools
    _llm(orch)
    original_send = orch._safe_send

    async def cancel_on_running(websocket, data):
        if json.loads(data).get("status") == "executing":
            orch.cancelled_sessions[id(ws)] = True
        return await original_send(websocket, data)

    orch._safe_send = cancel_on_running
    await registered_chat(orch, ws, "forecast please", chat_id, user_id=USER)
    tools.assert_not_awaited()


async def _turn_held_in_the_model(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    started, aborted = asyncio.Event(), asyncio.Event()

    async def held(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            aborted.set()
            raise

    orch._call_llm = held
    orch.execute_single_tool = AsyncMock()
    turn = asyncio.create_task(registered_chat(orch, ws, "forecast please", chat_id, user_id=USER))
    await asyncio.wait_for(started.wait(), 5)
    return ws, chat_id, turn, aborted


async def test_cancel_aborts_the_model_call_in_flight(orch):
    ws, chat_id, turn, aborted = await _turn_held_in_the_model(orch)
    await orch._cancel_turn(ws, USER, chat_id)
    await asyncio.wait_for(turn, 5)
    assert aborted.is_set()
    orch.execute_single_tool.assert_not_awaited()
    assert _cancel_alerts(orch)
    assert (id(ws), chat_id) not in orch._turn_tool_tasks
    await _cleanup(orch, chat_id)


async def test_a_cancel_landing_before_the_model_call_skips_it(orch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    calls = _llm(orch)
    orch.execute_single_tool = AsyncMock()

    async def cancelled_while_routing(*_args, **_kwargs):
        orch.cancelled_sessions[id(ws)] = True
        return None

    orch._typesafe_await_decision = cancelled_while_routing
    await registered_chat(orch, ws, "forecast please", chat_id, user_id=USER)
    assert calls == []
    orch.execute_single_tool.assert_not_awaited()
    assert _cancel_alerts(orch)
    assert (id(ws), chat_id) not in orch._turn_tool_tasks
    await _cleanup(orch, chat_id)


async def test_an_aborted_model_call_ends_the_turn_even_if_the_cancel_flag_was_cleared(orch):
    ws, chat_id, turn, aborted = await _turn_held_in_the_model(orch)
    dispatching = orch._turn_tool_tasks[(id(ws), chat_id)]
    orch._tool_cancel_requests = {dispatching}
    orch.cancelled_sessions[id(ws)] = False
    dispatching.cancel()
    await asyncio.wait_for(turn, 5)
    assert aborted.is_set() and _cancel_alerts(orch)
    orch.execute_single_tool.assert_not_awaited()
    await _cleanup(orch, chat_id)


@pytest.mark.parametrize("audit_fails", [False, True])
async def test_a_cancelled_model_call_leaves_one_failure_audit(orch, monkeypatch, audit_fails):
    ws = _ws(orch)
    if audit_fails:
        orch._record_llm_call = AsyncMock(side_effect=RuntimeError("audit store down"))
    started = asyncio.Event()
    resolved = SimpleNamespace(model="m", base_url="https://llm.test/v1")

    async def resolve(_websocket, **_kwargs):
        return SimpleNamespace(), orch._CredentialSource.SYSTEM, resolved

    async def held(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(orch, "_resolve_llm_client_for", resolve)
    monkeypatch.setattr(orch, "_context_model_call", held)
    call = asyncio.create_task(orch._call_llm_guarded(ws, [{"role": "user", "content": "hi"}]))
    await asyncio.wait_for(started.wait(), 5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    recorded = [(c.kwargs["outcome"], c.kwargs.get("upstream_error_class"))
                for c in orch._record_llm_call.await_args_list]
    assert recorded == [("failure", "cancelled")]


async def test_a_cancel_after_a_recorded_model_call_adds_no_failure_audit(orch, monkeypatch):
    ws = _ws(orch)
    started = asyncio.Event()
    resolved = SimpleNamespace(model="m", base_url="https://llm.test/v1")
    reply = SimpleNamespace(content="hi", tool_calls=None)

    async def resolve(_websocket, **_kwargs):
        return SimpleNamespace(), orch._CredentialSource.USER, resolved

    async def answered(*_args, **_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=reply, finish_reason="stop")], usage=None)

    async def held(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(orch, "_resolve_llm_client_for", resolve)
    monkeypatch.setattr(orch, "_context_model_call", answered)
    monkeypatch.setattr(orch, "_emit_llm_usage_report", held)
    call = asyncio.create_task(orch._call_llm_guarded(ws, [{"role": "user", "content": "hi"}]))
    await asyncio.wait_for(started.wait(), 5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert [c.kwargs["outcome"] for c in orch._record_llm_call.await_args_list] == ["success"]


async def test_an_unrelated_cancellation_during_the_model_call_still_propagates(orch):
    _ws_unused, chat_id, turn, aborted = await _turn_held_in_the_model(orch)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, 5)
    assert aborted.is_set()
    await _cleanup(orch, chat_id)


@pytest.mark.parametrize(("role", "published"), [("assistant_result", False), ("atomic", True)])
async def test_a_cancelled_turn_publishes_its_stage_unless_it_is_a_voice_result(
    orch, role, published, user_skills_disabled,
):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    stage = SimpleNamespace(publication_role=role, completion_state="completed")
    orch._append_conversation_message = AsyncMock(return_value=1)
    orch._publish_conversation_snapshot = AsyncMock(return_value={})
    orch.execute_single_tool = AsyncMock()

    async def cancelled_while_thinking(*_args, **_kwargs):
        orch.cancelled_sessions[id(ws)] = True
        return _msg(tool_calls=[_tc()]), _usage()

    orch._call_llm = cancelled_while_thinking
    await orch._handle_chat_message_impl(
        ws, "forecast please", chat_id, user_id=USER, conversation_stage=stage,
        conversation_request_generation=str(uuid.uuid4()), llm_preflight_complete=True,
    )
    orch.execute_single_tool.assert_not_awaited()
    assert _cancel_alerts(orch)
    assert orch._publish_conversation_snapshot.await_count == int(published)
    if published:
        assert orch._publish_conversation_snapshot.await_args.kwargs["stage"] is stage
    await _cleanup(orch, chat_id)


async def _failed_turn_messages(orch, call_llm):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch._call_llm = call_llm
    await registered_chat(orch, ws, "failure probe alpha", chat_id, user_id=USER)
    saved = (await asyncio.to_thread(orch.history.get_chat, chat_id, USER))["messages"]
    await _cleanup(orch, chat_id)
    return saved


async def test_a_turn_with_no_model_response_keeps_the_user_message_and_the_error(orch):
    async def no_response(*_args, **_kwargs):
        return None, None

    saved = await _failed_turn_messages(orch, no_response)
    assert [message["role"] for message in saved] == ["user", "assistant"]
    assert "failure probe alpha" in json.dumps(saved[0]["content"])
    assert "Failed to get a response" in json.dumps(saved[1]["content"])


async def test_a_turn_that_raises_keeps_the_user_message_and_the_error(orch):
    async def raises(*_args, **_kwargs):
        raise RuntimeError("502 Bad Gateway")

    saved = await _failed_turn_messages(orch, raises)
    assert [message["role"] for message in saved] == ["user", "assistant"]
    assert "failure probe alpha" in json.dumps(saved[0]["content"])
    assert "502 Bad Gateway" in json.dumps(saved[1]["content"])


async def _publish_failed(orch, stage):
    await orch._publish_failed_turn(
        _ws(orch), stage=stage, chat_id="c1", user_id=USER, request_generation="r1",
        server_initiated=False, message="Failed.")


async def test_a_failed_turn_with_no_open_stage_publishes_nothing(orch):
    orch._append_conversation_message = AsyncMock()
    await _publish_failed(orch, None)
    await _publish_failed(orch, SimpleNamespace(completion_state="cancelled", sealed=False))
    await _publish_failed(orch, SimpleNamespace(completion_state="completed", sealed=True))
    orch._append_conversation_message.assert_not_awaited()


async def test_a_failed_publication_is_logged_and_does_not_mask_the_turn_error(orch, caplog):
    orch._append_conversation_message = AsyncMock(side_effect=RuntimeError("store unavailable"))
    stage = SimpleNamespace(completion_state="completed", sealed=False)
    await _publish_failed(orch, stage)
    assert stage.completion_state == "failed"
    assert "failed turn publication failed" in caplog.text


async def test_a_draft_agent_without_tools_keeps_the_user_message(orch, monkeypatch):
    _register(orch)
    ws = _ws(orch)
    chat_id = await _chat(orch)
    orch._call_llm = AsyncMock(side_effect=AssertionError("no model call expected"))
    orch.agent_cards["draft-empty"] = SimpleNamespace(skills=[], name="Empty draft", description="")
    orch.agent_capabilities["draft-empty"] = []
    await registered_chat(orch, ws, "test my draft", chat_id, user_id=USER, draft_agent_id="draft-empty")
    saved = (await asyncio.to_thread(orch.history.get_chat, chat_id, USER))["messages"]
    await _cleanup(orch, chat_id)
    assert [message["role"] for message in saved] == ["user", "assistant"]
    assert "Draft agent has no usable tools" in json.dumps(saved[1]["content"])


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
