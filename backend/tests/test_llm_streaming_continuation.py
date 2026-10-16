"""Transport-level fixtures for Gemini tool-continuation metadata through the
orchestrator's stream accumulator, message reconstruction, and next-request wire.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from openai import OpenAI

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm_config.tool_continuation import (  # noqa: E402
    GEMINI_OPENAI_COMPAT_BASE_URL,
    MAX_THOUGHT_SIGNATURE_BYTES,
    ContinuationStateError,
)
from llm_config.types import CredentialSource, LLMUnavailable  # noqa: E402
from orchestrator.compaction import compact_messages  # noqa: E402
from orchestrator.context_engineering import TOMBSTONE, edit_context  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402

pytestmark = pytest.mark.asyncio

SIG_A = "R/thoughtSIG-A-wErJk2mQ=="
SIG_B = "R/thoughtSIG-B-9xZpLm4nQ=="


def _sse_completion(chunks):
    lines = []
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk) + "\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _chunk(delta, finish_reason=None):
    return {
        "id": "chatcmpl-g", "object": "chat.completion.chunk", "created": 1,
        "model": "gemini-3-pro",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _delta_tool_call(index=0, call_id=None, name=None, arguments=None,
                     signature=None):
    call = {"index": index}
    if call_id is not None:
        call["id"] = call_id
    fn = {}
    if name is not None:
        fn["name"] = name
    if arguments is not None:
        fn["arguments"] = arguments
    if fn:
        call["function"] = fn
    if signature is not None:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


def _completion(tool_calls=None, content="done"):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-n", "object": "chat.completion", "created": 1,
        "model": "gemini-3-pro",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }


def _wire_tool_call(call_id, name, arguments, signature=None):
    call = {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}
    if signature is not None:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


class _ScriptedTransport(httpx.BaseTransport):
    def __init__(self, responses):
        self.requests = []
        self._responses = list(responses)

    def handle_request(self, request):
        self.requests.append(json.loads(request.content.decode("utf-8")))
        return self._responses.pop(0)


def _gemini_client(script):
    transport = _ScriptedTransport(script)
    client = OpenAI(
        api_key="test-key", base_url=GEMINI_OPENAI_COMPAT_BASE_URL,
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )
    return client, transport


def _stub_completions(response):
    class _Completions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return _object_response(response)

    return _Completions()


def _object_response(completion):
    message = completion["choices"][0]["message"]
    tool_calls = None
    if message.get("tool_calls") is not None:
        from openai.types.chat.chat_completion_message_tool_call import (
            ChatCompletionMessageToolCall, Function)
        tool_calls = []
        for tc in message["tool_calls"]:
            call = ChatCompletionMessageToolCall(
                id=tc["id"], type="function",
                function=Function(name=tc["function"]["name"],
                                  arguments=tc["function"]["arguments"]))
            if tc.get("extra_content") is not None:
                call.extra_content = tc["extra_content"]
            tool_calls.append(call)
    from openai.types.chat import ChatCompletionMessage
    msg = ChatCompletionMessage(
        role="assistant", content=message.get("content"), tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=msg,
                                       finish_reason=completion["choices"][0]["finish_reason"])],
        usage=types.SimpleNamespace(total_tokens=12))


def _orch(client, base_url=GEMINI_OPENAI_COMPAT_BASE_URL):
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = None
    orch.audit_recorder = None
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    resolved = types.SimpleNamespace(model="gemini-3-pro", base_url=base_url)
    orch._llm_audit_principals = lambda ws: ("owner-1", "principal-1")

    async def _resolve(ws, **kwargs):
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
        return True

    orch._safe_send = _capture
    orch._sent_frames = sent
    return orch


def _tools_desc(name="get_weather"):
    return [{"type": "function", "function": {
        "name": name, "parameters": {"type": "object", "properties": {}}}}]


async def test_unknown_provider_fields_stay_outside_arguments_and_reconstruction():
    extra = {"google": {"thought_signature": SIG_A, "prompt_feedback": "x"},
             "acme": {"track": True}}
    script = [
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [
                _delta_tool_call(0, "call_a", "get_weather", None, None)]}),
            _chunk({"tool_calls": [
                {"index": 0, "function": {"arguments": '{"city": "Rome"}'},
                 "extra_content": extra}]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
        httpx.Response(200, json=_completion(content="done")),
    ]
    client, transport = _gemini_client(script)
    orch = _orch(client)
    msg, _ = await orch._call_llm(
        object(), [{"role": "user", "content": "weather?"}],
        tools_desc=_tools_desc(), allow_stream=True, stream_chat_id="chat-1")
    call = msg.tool_calls[0]
    assert getattr(call, "extra_content") == {
        "google": {"thought_signature": SIG_A}}
    assert json.loads(call.function.arguments) == {"city": "Rome"}
    messages = [{"role": "user", "content": "weather?"}, msg,
                {"role": "tool", "tool_call_id": "call_a", "content": "21C"}]
    await orch._call_llm(object(), messages, allow_stream=False)
    continuation = _assistant_wire_calls(transport.requests[1])[0]
    wire_call = continuation["tool_calls"][0]
    assert wire_call["extra_content"] == {"google": {"thought_signature": SIG_A}}
    assert "prompt_feedback" not in wire_call["function"]["arguments"]
    assert json.loads(wire_call["function"]["arguments"]) == {"city": "Rome"}


async def test_assembly_fallback_namespace_carries_signature(monkeypatch):
    import sys as _sys

    acc = {0: {"id": "call_a", "name": "get_weather",
               "arguments": '{"city": "Rome"}', "signature": SIG_A},
           1: {"id": "call_b", "name": "get_time",
               "arguments": '{}', "signature": None}}
    monkeypatch.setitem(
        _sys.modules, "openai.types.chat.chat_completion_message_tool_call", None)
    message = Orchestrator._assemble_streamed_message("", acc)
    calls = message.tool_calls
    assert type(calls[0]).__name__ == "SimpleNamespace"
    assert calls[0].extra_content == {"google": {"thought_signature": SIG_A}}
    assert calls[0].function.arguments == '{"city": "Rome"}'
    assert not hasattr(calls[1], "extra_content")


async def test_signature_metadata_never_enters_dispatch_arguments():
    script = [
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [
                _delta_tool_call(0, "call_a", "get_weather", None, SIG_A)]}),
            _chunk({"tool_calls": [
                _delta_tool_call(0, arguments='{"city": "Rome"}')]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
    ]
    client, _transport = _gemini_client(script)
    orch = _orch(client)
    msg, _ = await orch._call_llm(
        object(), [{"role": "user", "content": "weather?"}],
        tools_desc=_tools_desc(), allow_stream=True, stream_chat_id="chat-1")
    call = msg.tool_calls[0]
    assert call.function.arguments == '{"city": "Rome"}'
    assert SIG_A not in call.function.arguments
    assert json.loads(call.function.arguments) == {"city": "Rome"}
    assert getattr(call, "extra_content", None) is not None


def _assistant_wire_calls(body):
    return [m for m in body["messages"]
            if m.get("role") == "assistant" and m.get("tool_calls")]


async def test_sequential_calls_retain_every_required_signature_in_turn():
    script = [
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [
                _delta_tool_call(0, "call_a", "get_weather",
                                 '{"city": "Rome"}', SIG_A)]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [
                _delta_tool_call(0, "call_b", "get_weather",
                                 '{"city": "Naples"}', SIG_B)]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
        httpx.Response(200, content=_sse_completion([
            _chunk({"content": "all "}),
            _chunk({"content": "done"}),
            _chunk({}, finish_reason="stop"),
        ]), headers={"content-type": "text/event-stream"}),
    ]
    client, transport = _gemini_client(script)
    orch = _orch(client)
    ws = object()
    messages = [{"role": "user", "content": "weather?"}]

    msg_a, _ = await orch._call_llm(
        ws, messages, tools_desc=_tools_desc(), allow_stream=True,
        stream_chat_id="chat-1")
    assert msg_a.tool_calls[0].id == "call_a"
    assert getattr(msg_a.tool_calls[0], "extra_content") == {
        "google": {"thought_signature": SIG_A}}
    messages.append(msg_a)
    messages.append({"role": "tool", "tool_call_id": "call_a",
                     "content": "sunny, 21C"})

    msg_b, _ = await orch._call_llm(
        ws, messages, tools_desc=_tools_desc(), allow_stream=True,
        stream_chat_id="chat-1")
    assert getattr(msg_b.tool_calls[0], "extra_content") == {
        "google": {"thought_signature": SIG_B}}
    messages.append(msg_b)
    messages.append({"role": "tool", "tool_call_id": "call_b",
                     "content": "clear, 18C"})

    final, _ = await orch._call_llm(
        ws, messages, allow_stream=True, stream_chat_id="chat-1")
    assert final.content == "all done"
    first_continuation = transport.requests[1]
    second_continuation = transport.requests[2]
    carries_a = _assistant_wire_calls(first_continuation)
    assert carries_a[0]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": SIG_A}}
    both = _assistant_wire_calls(second_continuation)
    assert len(both) == 2
    assert both[0]["tool_calls"][0]["id"] == "call_a"
    assert both[0]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": SIG_A}}
    assert both[0]["tool_calls"][0]["function"]["arguments"] == '{"city": "Rome"}'
    assert both[1]["tool_calls"][0]["id"] == "call_b"
    assert both[1]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": SIG_B}}
    assert both[1]["tool_calls"][0]["function"]["arguments"] == '{"city": "Naples"}'
    for frame in orch._sent_frames:
        assert SIG_A not in json.dumps(frame)
        assert SIG_B not in json.dumps(frame)


async def test_parallel_first_call_only_signature_is_not_demanded_or_fabricated():
    script = [
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [
                _delta_tool_call(0, "call_a", "get_weather", None, SIG_A),
                _delta_tool_call(1, "call_b", "get_time", None),
            ]}),
            _chunk({"tool_calls": [
                _delta_tool_call(0, arguments='{"city": "Rome"}'),
                _delta_tool_call(1, arguments='{"tz": "CET"}'),
            ]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
        httpx.Response(200, json=_completion(content="both done")),
    ]
    client, transport = _gemini_client(script)
    orch = _orch(client)
    messages = [{"role": "user", "content": "weather and time?"}]

    msg, _ = await orch._call_llm(
        object(), messages, tools_desc=_tools_desc(), allow_stream=True,
        stream_chat_id="chat-1")
    assert len(msg.tool_calls) == 2
    assert getattr(msg.tool_calls[0], "extra_content", None) == {
        "google": {"thought_signature": SIG_A}}
    assert getattr(msg.tool_calls[1], "extra_content", None) is None
    messages.append(msg)
    messages.append({"role": "tool", "tool_call_id": "call_a", "content": "21C"})
    messages.append({"role": "tool", "tool_call_id": "call_b", "content": "10:00"})
    await orch._call_llm(object(), messages, allow_stream=True,
                         stream_chat_id="chat-1")

    continuation = _assistant_wire_calls(transport.requests[1])[0]
    calls = continuation["tool_calls"]
    assert calls[0]["extra_content"] == {"google": {"thought_signature": SIG_A}}
    assert "extra_content" not in calls[1]
    assert calls[1]["function"]["arguments"] == '{"tz": "CET"}'


async def test_stream_and_non_stream_produce_equivalent_outgoing_wire():
    async def run_mode(allow_stream):
        if allow_stream:
            script = [
                httpx.Response(200, content=_sse_completion([
                    _chunk({"tool_calls": [
                        _delta_tool_call(0, "call_a", "get_weather",
                                         '{"city":', SIG_A)]}),
                    _chunk({"tool_calls": [
                        _delta_tool_call(0, arguments=' "Rome"}')]}),
                    _chunk({}, finish_reason="tool_calls"),
                ]), headers={"content-type": "text/event-stream"}),
                httpx.Response(200, json=_completion(content="done")),
            ]
        else:
            script = [
                httpx.Response(200, json=_completion(
                    tool_calls=[_wire_tool_call(
                        "call_a", "get_weather", '{"city": "Rome"}', SIG_A)])),
                httpx.Response(200, json=_completion(content="done")),
            ]
        client, transport = _gemini_client(script)
        orch = _orch(client)
        messages = [{"role": "user", "content": "weather?"}]
        msg, _ = await orch._call_llm(
            object(), messages, tools_desc=_tools_desc(),
            allow_stream=allow_stream, stream_chat_id="chat-1")
        messages.append(msg)
        messages.append({"role": "tool", "tool_call_id": "call_a",
                         "content": "21C"})
        await orch._call_llm(object(), messages, allow_stream=False)
        continuation = _assistant_wire_calls(transport.requests[1])[0]
        return continuation["tool_calls"]

    streamed = await run_mode(allow_stream=True)
    plain = await run_mode(allow_stream=False)
    assert streamed == plain
    assert streamed[0]["extra_content"] == {"google": {"thought_signature": SIG_A}}
    assert streamed[0]["function"]["arguments"] == '{"city": "Rome"}'


@pytest.mark.parametrize(
    "bad_signature", [42, "", "s" * (MAX_THOUGHT_SIGNATURE_BYTES + 1)],
    ids=["non-string", "empty", "oversized"],
)
async def test_unusable_stream_metadata_falls_back_to_non_stream(bad_signature):
    script = [
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [
                _delta_tool_call(0, "call_a", "get_weather",
                                 '{"city": "Rome"}', bad_signature)]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
        httpx.Response(200, json=_completion(
            tool_calls=[_wire_tool_call(
                "call_a", "get_weather", '{"city": "Rome"}', SIG_A)])),
    ]
    client, transport = _gemini_client(script)
    orch = _orch(client)
    messages = [{"role": "user", "content": "weather?"}]
    msg, _ = await orch._call_llm(
        object(), messages, tools_desc=_tools_desc(), allow_stream=True,
        stream_chat_id="chat-1")
    assert len(transport.requests) == 2
    assert msg.tool_calls[0].id == "call_a"
    assert getattr(msg.tool_calls[0], "extra_content") == {
        "google": {"thought_signature": SIG_A}}
    messages.append(msg)
    messages.append({"role": "tool", "tool_call_id": "call_a", "content": "21C"})
    await orch._call_llm(object(), messages, allow_stream=False)
    continuation = _assistant_wire_calls(transport.requests[2])[0]
    assert continuation["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": SIG_A}}


async def test_contradictory_stream_signatures_fall_back_without_fabrication():
    script = [
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [
                _delta_tool_call(0, "call_a", "get_weather",
                                 '{"city":', SIG_A)]}),
            _chunk({"tool_calls": [
                _delta_tool_call(0, arguments='"Rome"}',
                                 signature="different-sig")]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
        httpx.Response(200, json=_completion(content="recovered")),
    ]
    client, transport = _gemini_client(script)
    orch = _orch(client)
    msg, _ = await orch._call_llm(
        object(), [{"role": "user", "content": "weather?"}],
        tools_desc=_tools_desc(), allow_stream=True, stream_chat_id="chat-1")
    assert len(transport.requests) == 2
    assert msg.content == "recovered"
    assert msg.tool_calls is None


async def test_truncated_stream_discards_partial_state_and_falls_back():
    class _TruncatingCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("stream"):
                def _gen():
                    yield _object_chunk_response(_chunk({"tool_calls": [
                        _delta_tool_call(0, "call_a", "get_weather",
                                         '{"city":', SIG_A)]}))
                    raise RuntimeError("provider dropped the stream")
                return _gen()
            return _object_response(_completion(content="recovered"))

    completions = _TruncatingCompletions()
    orch = _orch(types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=completions)))
    msg, _ = await orch._call_llm(
        object(), [{"role": "user", "content": "weather?"}],
        tools_desc=_tools_desc(), allow_stream=True, stream_chat_id="chat-1")
    assert len(completions.calls) == 2
    assert msg.content == "recovered"
    assert not msg.tool_calls
    for frame in orch._sent_frames:
        assert SIG_A not in json.dumps(frame)


async def test_malformed_wire_state_fails_typed_before_any_request():
    completions = _stub_completions(_completion(content="never"))
    orch = _orch(types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=completions)))
    messages = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": 5}}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "21C"},
    ]
    with pytest.raises(ContinuationStateError):
        await orch._call_llm(object(), messages, tools_desc=_tools_desc())
    assert completions.calls == []


async def test_non_gemini_profile_is_not_guarded_or_rewritten():
    completions = _stub_completions(_completion(content="ok"))
    orch = _orch(types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=completions)), base_url="https://my-proxy.example/v1")
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "f", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": 5}}}]},
    ]
    msg, _ = await orch._call_llm(object(), messages, tools_desc=_tools_desc())
    assert msg.content == "ok"
    assert len(completions.calls) == 1
    sent_messages = completions.calls[0]["messages"]
    assert sent_messages[0]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": 5}}


@pytest.mark.parametrize("base_url", [
    "https://api.openai.com/v1",
    "http://localhost:11434/v1",
    "https://my-proxy.example/v1",
])
@pytest.mark.parametrize("extra_content", [
    "opaque-provider-state",
    {"google": "another-provider-value"},
    {"google": {"thought_signature": SIG_A}},
])
async def test_non_gemini_stream_does_not_interpret_continuation_metadata(
    base_url, extra_content,
):
    streamed_call = _delta_tool_call(
        0, "call_a", "get_weather", '{"city":"Rome"}')
    streamed_call["extra_content"] = extra_content
    transport = _ScriptedTransport([
        httpx.Response(200, content=_sse_completion([
            _chunk({"tool_calls": [streamed_call]}),
            _chunk({}, finish_reason="tool_calls"),
        ]), headers={"content-type": "text/event-stream"}),
    ])
    client = OpenAI(
        api_key="test-key", base_url=base_url, max_retries=0,
        http_client=httpx.Client(transport=transport),
    )
    orch = _orch(client, base_url=base_url)
    message, _ = await orch._call_llm(
        object(), [{"role": "user", "content": "weather?"}],
        tools_desc=_tools_desc(), allow_stream=True, stream_chat_id="chat-1")

    assert len(transport.requests) == 1
    assert transport.requests[0]["stream"] is True
    assert message.tool_calls[0].id == "call_a"
    assert message.tool_calls[0].function.arguments == '{"city":"Rome"}'
    assert getattr(message.tool_calls[0], "extra_content", None) is None


def _object_chunk_response(chunk):
    delta = chunk["choices"][0]["delta"]
    tool_calls = None
    if delta.get("tool_calls"):
        parsed = []
        for tc in delta["tool_calls"]:
            fn = tc.get("function") or {}
            parsed.append(types.SimpleNamespace(
                index=tc.get("index", 0), id=tc.get("id"),
                function=types.SimpleNamespace(
                    name=fn.get("name"), arguments=fn.get("arguments")),
                extra_content=tc.get("extra_content")))
        tool_calls = parsed
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        delta=types.SimpleNamespace(
            content=delta.get("content"), tool_calls=tool_calls),
        finish_reason=chunk["choices"][0]["finish_reason"])], usage=None)


async def test_cancelled_stream_discards_state_and_emits_no_signature():
    import asyncio
    import threading

    gate = threading.Event()

    class _BlockingCompletions:
        def __init__(self):
            self.stream_calls = 0

        def create(self, **kwargs):
            if not kwargs.get("stream"):
                return _object_response(_completion(content="never"))
            self.stream_calls += 1

            def _gen():
                yield _object_chunk_response(_chunk({"tool_calls": [
                    _delta_tool_call(0, "call_a", "get_weather",
                                     '{"city":', SIG_A)]}))
                gate.wait(timeout=5)
                yield _object_chunk_response(_chunk({}, finish_reason="tool_calls"))
            return _gen()

    completions = _BlockingCompletions()
    orch = _orch(types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=completions)))
    task = asyncio.ensure_future(orch._call_llm(
        object(), [{"role": "user", "content": "weather?"}],
        tools_desc=_tools_desc(), allow_stream=True, stream_chat_id="chat-1"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if completions.stream_calls:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    assert completions.stream_calls == 1
    for frame in orch._sent_frames:
        assert SIG_A not in json.dumps(frame)


async def test_context_editing_keeps_current_turn_signature_state():
    big_tool_output = "x" * 4000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": "SIG_1"}}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": big_tool_output},
        {"role": "assistant", "tool_calls": [
            {"id": "call_2", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": "SIG_2"}}}]},
        {"role": "tool", "tool_call_id": "call_2", "content": big_tool_output},
        {"role": "assistant", "tool_calls": [
            {"id": "call_3", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": "SIG_3"}}}]},
        {"role": "tool", "tool_call_id": "call_3", "content": big_tool_output},
        {"role": "assistant", "tool_calls": [
            {"id": "call_4", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": "SIG_4"}}}]},
        {"role": "tool", "tool_call_id": "call_4", "content": big_tool_output},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": SIG_A}}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "21C"},
    ]
    edited, n = edit_context(messages)
    current = edited[11]
    assert current["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": SIG_A}}
    assert current["tool_calls"][0]["function"]["arguments"] == "{}"
    assert edited[12]["content"] != big_tool_output
    tombstoned = [m for m in edited
                  if isinstance(m, dict) and m.get("content") == TOMBSTONE]
    assert n == len(tombstoned) == 2
    for message in edited:
        if isinstance(message, dict) and message.get("tool_calls"):
            for call in message["tool_calls"]:
                extra = call.get("extra_content")
                assert extra is None or extra == {
                    "google": {"thought_signature": {"call_1": "SIG_1",
                                                     "call_2": "SIG_2",
                                                     "call_3": "SIG_3",
                                                     "call_4": "SIG_4",
                                                     "call_a": SIG_A}[call["id"]]}}


async def test_compaction_keeps_recent_turn_signature_state():
    filler = "y" * 30000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1 " + filler},
        {"role": "assistant", "content": "a1 " + filler},
        {"role": "user", "content": "q2 " + filler},
        {"role": "assistant", "content": "a2 " + filler},
        {"role": "user", "content": "q3 " + filler},
        {"role": "assistant", "content": "a3 " + filler},
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'},
             "extra_content": {"google": {"thought_signature": SIG_A}}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "21C"},
    ]

    async def _summary_llm(ws, prompt):
        return types.SimpleNamespace(content="summary"), None

    compacted, was_compacted = await compact_messages(
        messages, "default", _summary_llm)
    assert was_compacted
    survivors = [m for m in compacted
                 if isinstance(m, dict) and m.get("role") == "assistant"
                 and m.get("tool_calls")]
    assert len(survivors) == 1
    assert survivors[0]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": SIG_A}}
    assert survivors[0]["tool_calls"][0]["function"]["arguments"] == '{"city": "Rome"}'
