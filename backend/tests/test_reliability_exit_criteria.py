"""One test per reliability exit criterion: stalls, partial answers, malformed arguments, repeated failures and model outages."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from orchestrator import llm_recovery, repeat_guard
from orchestrator.orchestrator import Orchestrator
from tests.helpers.llm_faults import (
    FaultyCompletions, bare_orch, content_chunk, fail, no_retry_sleep, reply, stall, status, tiny_deadlines)

USER = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
          "required": ["city"]}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)
    tiny_deadlines(monkeypatch)
    no_retry_sleep(monkeypatch)
    llm_recovery.reset_turn()
    yield
    llm_recovery.reset_turn()


async def stream_call(orch):
    return await asyncio.wait_for(
        orch._call_llm(object(), USER, allow_stream=True, stream_chat_id="chat-1"), 5)


async def test_a_provider_that_never_sends_a_first_token_surfaces_within_the_deadline():
    comp = FaultyCompletions([stall()])
    try:
        assert await stream_call(bare_orch(comp)) == (None, None)
    finally:
        comp.release.set()
    assert [c.get("stream") for c in comp.calls] == [True, True, True]


async def test_a_stream_that_stalls_mid_answer_keeps_the_partial_text():
    comp = FaultyCompletions([stall(1, content_chunk("First line.\n")), fail(ConnectionError("down"))])
    try:
        msg, _usage = await stream_call(bare_orch(comp))
    finally:
        comp.release.set()
    assert msg.content == "First line.\n" and msg._astral_incomplete is True


@pytest.mark.parametrize("raw, expected, model_calls", [
    ('Sure: {"city": "Rome"}', {"city": "Rome"}, 0),
    ('{"city": "Ro\tme"}', {"city": "Ro\tme"}, 0),
    ('{"city": "Rome", "days": "3"}', {"city": "Rome", "days": 3}, 0),
    ("city=Rome", {"city": "Rome"}, 1),
])
async def test_each_malformed_argument_case_costs_at_most_one_extra_model_call(raw, expected, model_calls):
    orch = Orchestrator.__new__(Orchestrator)
    skill = SimpleNamespace(id="get_weather", name="get_weather", input_schema=SCHEMA)
    orch.agent_cards = {"weather-1": SimpleNamespace(skills=[skill])}
    orch._call_llm_json = AsyncMock(return_value={"city": "Rome"})
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_weather", arguments=raw))
    await orch._prepare_tool_calls(None, [tc], {"get_weather": "weather-1"}, None)
    assert json.loads(tc.function.arguments, strict=False) == expected
    assert orch._call_llm_json.await_count == model_calls


def test_no_identical_failing_call_is_dispatched_more_than_twice():
    guard = repeat_guard.start_turn()
    dispatched = 0
    for _ in range(6):
        if not guard.blocked("read_file", '{"path": "/x"}'):
            dispatched += 1
        guard.observe("read_file", '{"path": "/x"}', True)
    assert dispatched == 2


async def test_a_primary_model_outage_completes_on_the_fallback_with_a_notice(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "m2")
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("answer")]})
    orch = bare_orch(comp)
    msg, _usage = await orch._call_llm(object(), USER)
    assert msg.content == "answer"
    assert orch._rendered[0][0]["message"] == "m1 did not respond. This turn is using m2."
