"""Tests for orchestrator/orchestrator.py's _call_llm streaming path: incremental frames
for prose narrative, silence during tool-call rounds, fallback to a non-streaming
retry on a mid-stream error, and the FF_LLM_STREAMING kill switch.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm_config.types import CredentialSource, LLMUnavailable  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402

pytestmark = pytest.mark.asyncio


class _Msg:
    def __init__(self, content="ok"):
        self.content = content
        self.tool_calls = None


class _Resp:
    def __init__(self, content="ok"):
        self.choices = [types.SimpleNamespace(message=_Msg(content), finish_reason="stop")]
        self.usage = types.SimpleNamespace(total_tokens=10)


def _content_chunk(text):
    delta = types.SimpleNamespace(role=None, content=text, tool_calls=None)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


def _tool_chunk(index=0, call_id=None, name=None, arguments=None):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    tc = types.SimpleNamespace(index=index, id=call_id, function=fn)
    delta = types.SimpleNamespace(role=None, content=None, tool_calls=[tc])
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


class _FakeCompletions:
    def __init__(
        self,
        chunks=None,
        content="plain",
        raise_after=None,
        error_message="provider dropped the stream",
    ):
        self.calls = []
        self._chunks = chunks or []
        self._content = content
        self._raise_after = raise_after
        self._error_message = error_message

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("stream"):
            return _Resp(self._content)

        def _gen():
            for i, chunk in enumerate(self._chunks):
                if self._raise_after is not None and i >= self._raise_after:
                    raise RuntimeError(self._error_message)
                yield chunk
            if self._raise_after is not None:
                raise RuntimeError(self._error_message)
        return _gen()


def _bare_orch(completions):
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = None
    orch.audit_recorder = None
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    resolved = types.SimpleNamespace(model="m1", base_url="https://ep/v1")
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=completions))
    orch._llm_audit_principals = lambda ws: ("u", "p")

    async def _resolve(ws):
        return (client, CredentialSource.SYSTEM, resolved)

    orch._resolve_llm_client_for = _resolve

    async def _noop(*a, **k):
        return None

    orch._record_llm_call = _noop
    orch._record_llm_unconfigured = _noop
    orch._emit_llm_usage_report = _noop
    orch.rote = types.SimpleNamespace(
        get_profile=lambda ws: None, adapt=lambda ws, comps: comps)

    sent = []

    async def _capture(ws, payload):
        sent.append(json.loads(payload))

    orch._safe_send = _capture
    orch._sent_frames = sent
    return orch


def _stream_frames(orch):
    return [f for f in orch._sent_frames if f.get("type") == "ui_stream_data"]


async def test_content_path_streams_frames_and_returns_full_text():
    comp = _FakeCompletions(chunks=[
        _content_chunk("Hello"), _content_chunk(" world"), _content_chunk("!")])
    orch = _bare_orch(comp)
    ws = object()
    msg, _usage = await orch._call_llm(
        ws, [{"role": "user", "content": "hi"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "Hello world!"
    assert not msg.tool_calls
    assert comp.calls[0].get("stream") is True
    frames = _stream_frames(orch)
    assert frames, "content path must emit ui_stream_data frames"
    assert frames[0]["session_id"] == "chat-1"
    assert frames[0]["terminal"] is False
    assert frames[0]["components"][0]["content"].startswith("Hello")
    assert frames[-1]["terminal"] is True
    seqs = [f["seq"] for f in frames]
    assert seqs == sorted(seqs)


async def test_tool_call_path_emits_no_frames_and_returns_tool_calls():
    comp = _FakeCompletions(chunks=[
        _tool_chunk(0, call_id="call_a", name="get_weather", arguments='{"cit'),
        _tool_chunk(0, arguments='y": "Rome"}'),
    ])
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "weather?"}],
        tools_desc=[{"type": "function", "function": {"name": "get_weather"}}],
        allow_stream=True, stream_chat_id="chat-1")
    assert _stream_frames(orch) == []
    assert msg.tool_calls and len(msg.tool_calls) == 1
    assert msg.tool_calls[0].function.name == "get_weather"
    assert msg.tool_calls[0].function.arguments == '{"city": "Rome"}'
    assert msg.tool_calls[0].id == "call_a"


async def test_json_shaped_content_stays_silent():
    comp = _FakeCompletions(chunks=[
        _content_chunk('{"type": "card",'), _content_chunk(' "title": "x"}')])
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "hi"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert _stream_frames(orch) == []
    assert msg.content == '{"type": "card", "title": "x"}'


async def test_mid_stream_error_falls_back_to_non_streaming(caplog):
    sentinel = "SENTINEL_STREAM_BODY_MUST_NOT_ESCAPE"
    comp = _FakeCompletions(
        chunks=[_content_chunk("partial ")],
        raise_after=1,
        content="recovered",
        error_message=sentinel,
    )
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "hi"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "recovered"
    assert comp.calls[0].get("stream") is True
    assert "stream" not in comp.calls[-1]
    frames = _stream_frames(orch)
    if frames:
        assert frames[-1]["terminal"] is True, "partial text must be cleared"
    assert sentinel not in caplog.text


async def test_flag_off_never_attempts_streaming(monkeypatch):
    monkeypatch.setenv("FF_LLM_STREAMING", "false")
    comp = _FakeCompletions(content="plain")
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "hi"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "plain"
    assert all("stream" not in c for c in comp.calls)
    assert _stream_frames(orch) == []


async def test_allow_stream_defaults_off_for_other_callers():
    comp = _FakeCompletions(content="plain")
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(object(), [{"role": "user", "content": "hi"}])
    assert msg.content == "plain"
    assert all("stream" not in c for c in comp.calls)


async def test_chat_loop_context_opts_in_without_new_kwargs():
    from orchestrator.orchestrator import _NARRATIVE_STREAM_CHAT
    comp = _FakeCompletions(chunks=[_content_chunk("Hi"), _content_chunk(" there")])
    orch = _bare_orch(comp)
    token = _NARRATIVE_STREAM_CHAT.set("chat-ctx")
    try:
        msg, _usage = await orch._call_llm(object(), [{"role": "user", "content": "hi"}])
    finally:
        _NARRATIVE_STREAM_CHAT.reset(token)
    assert msg.content == "Hi there"
    assert comp.calls[0].get("stream") is True
    frames = _stream_frames(orch)
    assert frames and frames[0]["session_id"] == "chat-ctx"


async def test_no_websocket_never_streams():
    comp = _FakeCompletions(content="plain")
    orch = _bare_orch(comp)
    msg, _usage = await orch._call_llm(
        None, [{"role": "user", "content": "hi"}],
        allow_stream=True, stream_chat_id="chat-1")
    assert msg.content == "plain"
    assert all("stream" not in c for c in comp.calls)
