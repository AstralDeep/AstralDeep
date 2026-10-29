"""Verifies bounded model-review repair without replaying tools or weakening completion checks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.goal_progress import GoalProgress
from orchestrator.goal_verification import evaluate_progress


def _host(replies):
    return SimpleNamespace(
        _call_llm=AsyncMock(side_effect=replies),
        _accumulate_usage=AsyncMock(),
        cancelled_sessions={},
    )


def _reply(content, finish="stop"):
    return SimpleNamespace(content=content, _astral_finish_reason=finish), {"total_tokens": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [
    _reply("malformed"),
    _reply('{"status":"complete","remaining":[],"reason":"done"}'),
    _reply('{"status":"continue","remaining":["verify"],"reason":""}', "length"),
    RuntimeError("provider unavailable"),
])
async def test_progress_review_repairs_one_invalid_response_with_original_evidence(first):
    host = _host([first, _reply('{"status":"continue","remaining":["Run tests"],"reason":"Missing proof"}')])
    host._accumulate_usage = lambda *_: None
    socket = object()
    decision = await evaluate_progress(host, socket, GoalProgress("Original project goal"), None, None, chat_id="chat")

    assert decision.status == "continue"
    assert decision.remaining == ("Run tests",)
    assert host._call_llm.await_count == 2
    for call in host._call_llm.await_args_list:
        assert call.kwargs["tools_desc"] is None
        assert call.kwargs["allow_stream"] is False
        assert "Original project goal" in str(call.args[1])


@pytest.mark.asyncio
async def test_repeated_invalid_reviews_stay_blocked_after_one_repair():
    host = _host([_reply("invalid"), _reply("still invalid")])
    host._accumulate_usage = lambda *_: None
    decision = await evaluate_progress(host, object(), GoalProgress("Original goal"), None, None, chat_id="chat")
    assert decision.status == "blocked"
    assert host._call_llm.await_count == 2


@pytest.mark.asyncio
async def test_real_blocked_review_is_not_retried():
    host = _host([_reply('{"status":"blocked","remaining":[],"reason":"The user declined permission."}')])
    host._accumulate_usage = lambda *_: None
    decision = await evaluate_progress(host, object(), GoalProgress("Original goal"), None, None, chat_id="chat")
    assert decision.status == "blocked"
    assert decision.reason == "The user declined permission."
    assert host._call_llm.await_count == 1


@pytest.mark.asyncio
async def test_stop_during_invalid_review_prevents_repair_call():
    socket = object()
    host = _host([])
    host._accumulate_usage = lambda *_: None

    async def stop(*_args, **_kwargs):
        host.cancelled_sessions[id(socket)] = True
        return _reply("invalid")

    host._call_llm.side_effect = stop
    assert await evaluate_progress(host, socket, GoalProgress("Original goal"), None, None, chat_id="chat") is None
    assert host._call_llm.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_review_propagates_without_retry():
    host = _host([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await evaluate_progress(host, object(), GoalProgress("Original goal"), None, None, chat_id="chat")
    assert host._call_llm.await_count == 1
