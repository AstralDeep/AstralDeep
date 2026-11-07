"""Tests for side questions (orchestrator/side_question.py): answered from the conversation with no
tools, nothing written to history, and only to the asking socket."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import side_question
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


async def test_a_side_question_without_an_id_is_ignored(orch):
    ws = await _negotiated(orch)
    orch._call_llm = AsyncMock()
    await side_question.answer(orch, ws, USER, None, {"question": "hi"})
    assert not _results(orch)
