"""Tests that an answer cut off after text reached the user is continued, never re-run."""

import asyncio

import pytest

from orchestrator.orchestrator import _GOAL_BUFFERED_RESPONSE, _INCOMPLETE_NOTICE
from tests.helpers.llm_faults import (
    FaultyCompletions, bare_orch, content_chunk, drop, fail, no_retry_sleep, reply, stall, tiny_deadlines)

USER = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    tiny_deadlines(monkeypatch)
    no_retry_sleep(monkeypatch)


async def call(orch, **kwargs):
    return await asyncio.wait_for(
        orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1", **kwargs), 5)


async def test_drop_after_visible_text_continues_without_rerunning():
    comp = FaultyCompletions([drop(1, content_chunk("First line.\n")), reply("Second line.")])
    msg, _usage = await call(bare_orch(comp))
    assert msg.content == "First line.\nSecond line."
    assert len(comp.calls) == 2
    assert "stream" not in comp.calls[1]
    assert comp.calls[1]["messages"][:1] == USER
    assert comp.calls[1]["messages"][-2] == {"role": "assistant", "content": "First line.\n"}
    assert comp.calls[1]["messages"][-1]["role"] == "user"
    assert not getattr(msg, "_astral_incomplete", False)


async def test_stall_after_visible_text_continues():
    comp = FaultyCompletions([stall(1, content_chunk("First line.\n")), reply("Second line.")])
    try:
        msg, _usage = await call(bare_orch(comp))
    finally:
        comp.release.set()
    assert msg.content == "First line.\nSecond line."


async def test_failed_continuation_keeps_the_partial_and_says_so():
    comp = FaultyCompletions([drop(1, content_chunk("First line.\n")), fail(ConnectionError("down"))])
    orch = bare_orch(comp)
    msg, _usage = await call(orch)
    assert msg.content == "First line.\n"
    assert msg._astral_incomplete is True
    assert len(comp.calls) == 2
    assert orch._rendered[-1][0]["message"] == _INCOMPLETE_NOTICE
    assert orch._rendered[-1][0]["variant"] == "warning"


async def test_continuation_keeps_the_tools():
    tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}]
    comp = FaultyCompletions([drop(1, content_chunk("First line.\n")), reply("Second line.")])
    await call(bare_orch(comp), tools_desc=tools)
    assert comp.calls[1]["tools"] == tools


async def test_buffered_goal_stream_is_retried_not_continued():
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        comp = FaultyCompletions([drop(1, content_chunk("Hello")), reply("full answer")])
        msg, _usage = await call(bare_orch(comp))
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)
    assert msg.content == "full answer"
    assert comp.calls[1]["messages"] == USER
