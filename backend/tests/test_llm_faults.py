"""Tests for the scripted fake model provider in tests/helpers/llm_faults.py."""

import threading

import pytest

from tests.helpers.llm_faults import (
    FaultyCompletions, StatusError, bare_orch, content_chunk, drop, fail, reply, stall, status, stream)


def test_steps_are_consumed_in_order_and_the_last_repeats():
    comp = FaultyCompletions([fail(ConnectionError("x")), reply("ok")])
    with pytest.raises(ConnectionError):
        comp.create(model="m1", messages=[])
    assert comp.create(model="m1", messages=[]).choices[0].message.content == "ok"
    assert comp.create(model="m1", messages=[]).choices[0].message.content == "ok"
    assert [c["model"] for c in comp.calls] == ["m1", "m1", "m1"]


def test_by_model_gives_each_model_its_own_script():
    comp = FaultyCompletions(by_model={"m1": [status(503)], "m2": [reply("two")]})
    with pytest.raises(StatusError) as caught:
        comp.create(model="m1", messages=[])
    assert caught.value.status_code == 503
    assert comp.create(model="m2", messages=[]).choices[0].message.content == "two"


def test_status_carries_retry_after():
    with pytest.raises(StatusError) as caught:
        FaultyCompletions([status(429, retry_after=7)]).create(model="m1", messages=[])
    assert caught.value.response.headers["retry-after"] == "7"


def test_stream_yields_chunks_then_a_finish_chunk():
    comp = FaultyCompletions([stream(content_chunk("a"), content_chunk("b"))])
    chunks = list(comp.create(model="m1", messages=[], stream=True))
    assert [c.choices[0].delta.content for c in chunks[:2]] == ["a", "b"]
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_drop_raises_after_the_given_chunks():
    gen = FaultyCompletions([drop(1, content_chunk("a"))]).create(model="m1", messages=[], stream=True)
    assert next(gen).choices[0].delta.content == "a"
    with pytest.raises(ConnectionError):
        next(gen)


def test_stall_blocks_until_released():
    comp = FaultyCompletions([stall(1, content_chunk("a"))])
    gen = comp.create(model="m1", messages=[], stream=True)
    assert next(gen).choices[0].delta.content == "a"
    seen = []
    worker = threading.Thread(target=lambda: seen.extend(gen))
    worker.start()
    worker.join(0.05)
    assert worker.is_alive()
    comp.release.set()
    worker.join(2)
    assert not worker.is_alive() and seen == []


async def test_bare_orch_runs_a_plain_call():
    orch = bare_orch(FaultyCompletions([reply("hello")]))
    msg, _usage = await orch._call_llm(None, [{"role": "user", "content": "hi"}])
    assert msg.content == "hello"
    assert orch._audits[-1]["outcome"] == "success"
