"""Checks goal turns keep provider streaming while buffering all narrative publication."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.orchestrator import (  # noqa: E402
    _GOAL_BUFFERED_RESPONSE,
    _NARRATIVE_STREAMED,
)
from tests.test_llm_streaming import (  # noqa: E402
    _FakeCompletions,
    _content_chunk,
    _stream_frames,
    _tool_chunk,
    _bare_orch,
)

pytestmark = pytest.mark.asyncio


def _terminal_chunk(reason):
    delta = SimpleNamespace(role=None, content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=reason)], usage=None)


class _StreamingOnlyCompletions(_FakeCompletions):
    def create(self, **kwargs):
        if not kwargs.get("stream"):
            raise AssertionError("goal planner must retain provider streaming")
        return super().create(**kwargs)


async def test_goal_buffered_text_streams_transport_without_publishing_narrative():
    completions = _StreamingOnlyCompletions(chunks=[
        _content_chunk("The project files "),
        _content_chunk("are still being checked."),
        _terminal_chunk("stop"),
    ])
    orch = _bare_orch(completions)
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        message, _usage = await orch._call_llm(
            object(), [{"role": "user", "content": "Verify the project"}],
            stream_chat_id="chat-1",
        )
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)

    assert message.content == "The project files are still being checked."
    assert completions.calls[0].get("stream") is True
    assert _stream_frames(orch) == []
    assert _NARRATIVE_STREAMED.get() is False


async def test_goal_buffered_tool_stream_reassembles_complete_arguments_without_frames():
    completions = _StreamingOnlyCompletions(chunks=[
        _tool_chunk(0, call_id="call-1", name="run_command", arguments='{"cwd":"/work",'),
        _tool_chunk(0, arguments='"argv":["pytest"]}'),
        _terminal_chunk("tool_calls"),
    ])
    orch = _bare_orch(completions)
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        message, _usage = await orch._call_llm(
            object(), [{"role": "user", "content": "Run the tests"}],
            tools_desc=[{"type": "function", "function": {"name": "run_command"}}],
            stream_chat_id="chat-1",
        )
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)

    assert completions.calls[0].get("stream") is True
    assert _stream_frames(orch) == []
    assert _NARRATIVE_STREAMED.get() is False
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0].function.arguments == '{"cwd":"/work","argv":["pytest"]}'


async def test_goal_buffered_clean_eof_discards_complete_looking_tool_call_and_falls_back():
    completions = _FakeCompletions(chunks=[
        _tool_chunk(
            0, call_id="call-incomplete", name="run_command",
            arguments='{"cwd":"/work","argv":["pytest"]}',
        ),
    ], content="use the non-streamed fallback")
    orch = _bare_orch(completions)
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        message, _usage = await orch._call_llm(
            object(), [{"role": "user", "content": "Run the tests"}],
            tools_desc=[{"type": "function", "function": {"name": "run_command"}}],
            stream_chat_id="chat-1",
        )
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)

    assert [call.get("stream", False) for call in completions.calls] == [True, False]
    assert message.content == "use the non-streamed fallback"
    assert not message.tool_calls
    assert _stream_frames(orch) == []
    assert _NARRATIVE_STREAMED.get() is False


async def test_goal_buffered_length_finish_reason_is_preserved_for_outer_gate():
    completions = _FakeCompletions(chunks=[
        _content_chunk("partial but bounded"),
        _terminal_chunk("length"),
    ])
    orch = _bare_orch(completions)
    client, _source, _resolved = await orch._resolve_llm_client_for(object())
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        response = await orch._call_llm_streamed(
            object(), client, {"model": "m1", "messages": []}, None,
        )
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)

    assert response.choices[0].finish_reason == "length"
    assert response.choices[0].message.content == "partial but bounded"
    assert _stream_frames(orch) == []


async def test_goal_buffering_does_not_override_streaming_kill_switch(monkeypatch):
    monkeypatch.setenv("FF_LLM_STREAMING", "false")
    completions = _FakeCompletions(content="verified after review")
    orch = _bare_orch(completions)
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        message, _usage = await orch._call_llm(
            object(), [{"role": "user", "content": "Verify the project"}],
        )
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)

    assert message.content == "verified after review"
    assert all(call.get("stream") is not True for call in completions.calls)
    assert _stream_frames(orch) == []
    assert _NARRATIVE_STREAMED.get() is False


class _PartiallyFailingCompletions(_FakeCompletions):
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("stream"):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="recovered", tool_calls=None),
                finish_reason="stop",
            )], usage=None)

        def _gen():
            yield _content_chunk("partial unverified text")
            raise RuntimeError("stream connection dropped")

        return _gen()


async def test_goal_buffered_stream_error_falls_back_without_publishing_partial_text():
    completions = _PartiallyFailingCompletions()
    orch = _bare_orch(completions)
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    try:
        message, _usage = await orch._call_llm(
            object(), [{"role": "user", "content": "Verify the project"}],
            stream_chat_id="chat-1",
        )
    finally:
        _GOAL_BUFFERED_RESPONSE.reset(token)

    assert [call.get("stream", False) for call in completions.calls] == [True, False]
    assert message.content == "recovered"
    assert "partial unverified text" not in message.content
    assert _stream_frames(orch) == []
    assert _NARRATIVE_STREAMED.get() is False


class _BlockingCompletions(_FakeCompletions):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("stream"):
            raise AssertionError("cancelled goal stream must not retry non-streaming")

        def _gen():
            yield _content_chunk("partial unverified text")
            self.started.set()
            self.release.wait(5)
            yield _content_chunk("more unverified text")

        return _gen()


async def test_goal_buffered_stream_cancellation_emits_no_partial_or_terminal_frame():
    completions = _BlockingCompletions()
    orch = _bare_orch(completions)
    token = _GOAL_BUFFERED_RESPONSE.set(True)
    call = asyncio.create_task(orch._call_llm(
        object(), [{"role": "user", "content": "Verify the project"}],
        stream_chat_id="chat-1",
    ))
    try:
        started = await asyncio.to_thread(completions.started.wait, 5)
        assert started
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    finally:
        completions.release.set()
        _GOAL_BUFFERED_RESPONSE.reset(token)

    assert completions.calls[0].get("stream") is True
    assert _stream_frames(orch) == []
    assert _NARRATIVE_STREAMED.get() is False
