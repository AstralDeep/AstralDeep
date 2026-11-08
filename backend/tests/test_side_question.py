"""Tests for side questions (orchestrator/side_question.py): answered from the conversation with no
tools, nothing written to history, and only to the asking socket."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import side_question, turn_steer
from orchestrator.orchestrator import Orchestrator
from tests.test_analysis_render_targets import (
    USER, _chat, _cleanup, _ws,
    generated_agent_bundles as generated_agent_bundles, orch as orch, plane_runtime as plane_runtime,
)

pytestmark = pytest.mark.asyncio


def _ask(question="What did I ask first?"):
    return {"question": question, "client_message_id": str(uuid.uuid4()),
            "submission_id": str(uuid.uuid4()), "request_generation": str(uuid.uuid4())}


def _results(orch):
    return [json.loads(c.args[1]) for c in orch._safe_send.await_args_list
            if json.loads(c.args[1]).get("type") == "side_question_result"]


async def _negotiated(orch):
    ws = _ws(orch)
    orch.ui_sessions[ws] = {"sub": USER, "_turn_steer_v1": True}
    return ws


async def test_a_side_question_uses_the_conversation_with_no_tools_and_writes_nothing(orch):
    ws = await _negotiated(orch)
    chat_id = await _chat(orch)
    await asyncio.to_thread(orch.history.add_message, chat_id, "user", "Plan a trip to Lisbon", user_id=USER)
    before = (await asyncio.to_thread(orch.history.get_chat, chat_id, USER))["messages"]
    orch._call_llm = AsyncMock(return_value=(SimpleNamespace(content="You asked about Lisbon."), None))
    ask = _ask()
    await side_question.answer(orch, ws, USER, chat_id, ask)
    args = orch._call_llm.await_args
    assert args.args[2] is None and args.kwargs["feature"] == "side_question"
    sent = args.args[1]
    assert {"role": "user", "content": "Plan a trip to Lisbon"} in sent
    assert sent[-1] == {"role": "user", "content": "What did I ask first?"}
    assert _results(orch) == [{"type": "side_question_result",
                               "client_message_id": ask["client_message_id"],
                               "answer": "You asked about Lisbon."}]
    after = (await asyncio.to_thread(orch.history.get_chat, chat_id, USER))["messages"]
    assert after == before
    await _cleanup(orch, chat_id)


@pytest.mark.parametrize("payload", [
    {"question": " ", "client_message_id": str(uuid.uuid4())},
    {"question": "ok", "client_message_id": str(uuid.uuid4()), "tools": ["run_command"]},
])
async def test_an_unreadable_side_question_gets_a_notice_without_a_model_call(orch, payload):
    ws = await _negotiated(orch)
    orch._call_llm = AsyncMock()
    await side_question.answer(orch, ws, USER, None, payload)
    orch._call_llm.assert_not_awaited()
    assert _results(orch)[-1]["answer"] == "This side question could not be read."


async def test_without_negotiation_no_model_is_called(orch):
    ws = _ws(orch)
    orch.ui_sessions[ws] = {"sub": USER}
    orch._call_llm = AsyncMock()
    await side_question.answer(orch, ws, USER, None, _ask())
    orch._call_llm.assert_not_awaited()


async def test_a_failed_model_call_still_answers_the_sheet(orch):
    ws = await _negotiated(orch)
    orch._call_llm = AsyncMock(side_effect=RuntimeError("provider down"))
    await side_question.answer(orch, ws, USER, None, _ask())
    assert "could not be answered" in _results(orch)[-1]["answer"]


@pytest.mark.parametrize("client_message_id", [
    None, 7, "not-a-uuid", str(uuid.uuid4()).upper(), str(uuid.uuid1()),
])
async def test_a_side_question_without_a_canonical_v4_id_is_ignored(orch, client_message_id):
    ws = await _negotiated(orch)
    orch._call_llm = AsyncMock()
    payload = {"question": "hi"} if client_message_id is None else {
        "question": "hi", "client_message_id": client_message_id}
    await side_question.answer(orch, ws, USER, None, payload)
    await side_question.answer(orch, ws, USER, None, "not a payload")
    orch._call_llm.assert_not_awaited()
    assert not _results(orch)


async def test_component_history_is_flattened_to_text_with_titles(orch):
    ws = await _negotiated(orch)
    chat_id = await _chat(orch)
    card = {"type": "card", "title": "Forecast", "content": [
        {"type": "text", "content": "Sunny"}, {"type": "alert", "title": "Heat", "message": "Drink water"},
        {"type": "divider"}, 42]}
    await asyncio.to_thread(orch.history.add_message, chat_id, "user", "Weather?", user_id=USER)
    await asyncio.to_thread(orch.history.add_message, chat_id, "assistant", [card], user_id=USER)
    await asyncio.to_thread(orch.history.add_message, chat_id, "assistant", [{"type": "divider"}], user_id=USER)
    await asyncio.to_thread(orch.history.add_message, chat_id, "system", "hidden", user_id=USER)
    orch._call_llm = AsyncMock(return_value=(SimpleNamespace(content="Sunny."), None))
    await side_question.answer(orch, ws, USER, chat_id, _ask("Was it hot?"))
    sent = orch._call_llm.await_args.args[1]
    assert sent[1:] == [{"role": "user", "content": "Weather?"},
                        {"role": "assistant", "content": "Forecast\nSunny\nHeat\nDrink water"},
                        {"role": "user", "content": "Was it hot?"}]
    await _cleanup(orch, chat_id)


async def test_the_transcript_is_bounded_in_messages_and_size(orch):
    ws = await _negotiated(orch)
    chat_id = await _chat(orch)
    for index in range(side_question.TRANSCRIPT_MESSAGES + 2):
        await asyncio.to_thread(orch.history.add_message, chat_id, "user", f"{index}" + "x" * 5000, user_id=USER)
    orch._call_llm = AsyncMock(return_value=(SimpleNamespace(content="ok"), None))
    await side_question.answer(orch, ws, USER, chat_id, _ask())
    transcript = orch._call_llm.await_args.args[1][1:-1]
    assert len(transcript) == side_question.TRANSCRIPT_MESSAGES
    assert transcript[0]["content"].startswith("2x")
    assert all(len(turn["content"]) == side_question.TRANSCRIPT_MESSAGE_SCALARS for turn in transcript)
    await _cleanup(orch, chat_id)


async def test_the_running_request_and_its_steers_are_visible_once(orch):
    ws = await _negotiated(orch)
    chat_id = await _chat(orch)
    await asyncio.to_thread(orch.history.add_message, chat_id, "user", "Earlier question", user_id=USER)
    turn_steer.open_turn(orch, ws, chat_id, USER, "Earlier question")
    turn_steer._mailboxes(orch)[(id(ws), chat_id, USER)].applied += ["Use metric units", " "]
    orch._call_llm = AsyncMock(return_value=(SimpleNamespace(content="ok"), None))
    await side_question.answer(orch, ws, USER, chat_id, _ask())
    assert orch._call_llm.await_args.args[1][1:-1] == [
        {"role": "user", "content": "Earlier question"}, {"role": "user", "content": "Use metric units"}]
    other = _ws(orch, "other-user")
    orch.ui_sessions[other] = {"sub": "other-user", "_turn_steer_v1": True}
    await side_question.answer(orch, other, "other-user", chat_id, _ask())
    assert orch._call_llm.await_args.args[1][1:-1] == []
    turn_steer.close_turn(orch, ws, chat_id, USER)
    await _cleanup(orch, chat_id)


async def test_only_one_side_question_runs_per_socket(orch):
    ws = await _negotiated(orch)
    other = _ws(orch, USER)
    orch.ui_sessions[other] = {"sub": USER, "_turn_steer_v1": True}
    started, release = asyncio.Event(), asyncio.Event()

    async def model(websocket, messages, tools_desc=None, **_kwargs):
        started.set()
        if websocket is ws:
            await release.wait()
        return SimpleNamespace(content="answer"), None

    orch._call_llm = AsyncMock(side_effect=model)
    first, second, elsewhere = _ask(), _ask(), _ask()
    running = asyncio.create_task(side_question.answer(orch, ws, USER, None, first))
    await asyncio.wait_for(started.wait(), 5)
    await side_question.answer(orch, ws, USER, None, second)
    await side_question.answer(orch, other, USER, None, elsewhere)
    release.set()
    await asyncio.wait_for(running, 5)
    answers = {r["client_message_id"]: r["answer"] for r in _results(orch)}
    assert answers == {second["client_message_id"]: side_question._BUSY,
                       elsewhere["client_message_id"]: "answer", first["client_message_id"]: "answer"}
    assert orch._call_llm.await_count == 2
    assert id(ws) not in orch._side_questions_in_flight
    await side_question.answer(orch, ws, USER, None, _ask())
    assert _results(orch)[-1]["answer"] == "answer"


async def test_a_cancelled_side_question_frees_the_socket(orch):
    ws = await _negotiated(orch)
    orch._call_llm = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await side_question.answer(orch, ws, USER, None, _ask())
    assert id(ws) not in orch._side_questions_in_flight


async def test_side_question_frames_take_the_control_lane_and_never_enter_the_operation_queue(orch):
    raw = json.dumps({"type": "ui_event", "action": "side_question", "payload": _ask()})
    handled = []

    async def run_ui_control(_context, value):
        handled.append(value)

    async def forbidden_enqueue(*_args, **_kwargs):
        raise AssertionError("a side question entered the operation queue")

    orch._run_ui_control = run_ui_control
    orch._enqueue_connection_frame = forbidden_enqueue
    context = SimpleNamespace(registered=True, websocket=object(), connection_scope_id=uuid.uuid4(),
                              closing=False, preregistration=[], tracked_tasks=set(), operation_tasks=set())
    assert Orchestrator._ui_control_kind(json.loads(raw)) == "side_question"
    assert await orch._route_ui_frame(context, raw) is True
    await asyncio.gather(*set(context.tracked_tasks))
    assert handled == [raw]
    assert not context.operation_tasks


async def test_a_side_question_event_is_answered_for_the_sockets_active_chat(monkeypatch):
    websocket = object()
    answered = []

    async def answer(orch, ws, user_id, chat_id, payload):
        answered.append((orch, ws, user_id, chat_id, payload))

    monkeypatch.setattr(side_question, "answer", answer)
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.ui_sessions = {websocket: {"sub": USER}}
    orchestrator._ws_active_chat = {id(websocket): "c1"}
    orchestrator._chat_recorders = {}
    orchestrator._safe_send = AsyncMock()
    payload = _ask()
    await orchestrator.handle_ui_message(
        websocket, json.dumps({"type": "ui_event", "action": "side_question", "payload": payload}))
    assert answered == [(orchestrator, websocket, USER, "c1", payload)]
