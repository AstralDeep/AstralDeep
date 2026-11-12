"""Tests that a model stream which stops making progress is timed out and retried, never returned partial."""

import asyncio

import pytest

from tests.helpers.llm_faults import (
    FaultyCompletions, bare_orch, heartbeat_only, no_retry_sleep, reply, stall, stream, tiny_deadlines, tool_chunk)

USER = [{"role": "user", "content": "hi"}]


@pytest.fixture
async def faults(monkeypatch):
    made = []

    def build(script):
        comp = FaultyCompletions(script)
        made.append(comp)
        return comp

    tiny_deadlines(monkeypatch)
    no_retry_sleep(monkeypatch)
    yield build
    for comp in made:
        comp.release.set()


async def call(orch, **kwargs):
    return await asyncio.wait_for(
        orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1", **kwargs), 5)


async def test_stall_before_first_token_is_retried_as_a_stream(faults):
    comp = faults([stall(), reply("ok")])
    msg, _usage = await call(bare_orch(comp))
    assert msg.content == "ok"
    assert [c.get("stream") for c in comp.calls] == [True, True]


async def test_stall_mid_tool_arguments_retries_and_never_returns_a_partial_call(faults):
    comp = faults([
        stall(1, tool_chunk(0, "call_a", "get_weather", '{"city": "Ro')),
        stream(tool_chunk(0, "call_a", "get_weather", '{"city": "Rome"}'), finish="tool_calls"),
    ])
    msg, _usage = await call(bare_orch(comp))
    assert msg.tool_calls[0].function.arguments == '{"city": "Rome"}'
    assert len(comp.calls) == 2


async def test_heartbeat_chunks_without_progress_still_stall(faults):
    comp = faults([heartbeat_only(), reply("ok")])
    msg, _usage = await call(bare_orch(comp))
    assert msg.content == "ok"
    assert len(comp.calls) == 2


async def test_a_stall_on_every_attempt_fails_as_a_transport_error(faults):
    comp = faults([stall()])
    orch = bare_orch(comp)
    assert await call(orch) == (None, None)
    assert len(comp.calls) == orch.MAX_RETRIES == 3
    assert all(c.get("stream") is True for c in comp.calls)
    assert orch._audits[-1]["upstream_error_class"] == "transport_error"


async def test_cancel_during_stall_returns_at_once(faults):
    comp = faults([stall()])
    orch = bare_orch(comp)
    turn = asyncio.create_task(orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1"))
    await asyncio.sleep(0.01)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, 1)
