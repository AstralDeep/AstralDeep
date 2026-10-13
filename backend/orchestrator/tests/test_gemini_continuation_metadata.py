"""Checks bounded Gemini metadata retention and OpenAI message reconstruction.
These offline cases qualify parsing and typing without asserting outgoing provider behavior.
"""
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall

from orchestrator.orchestrator import (
    Orchestrator,
    _bounded_extra_content,
)

SIG = "opaque-signature-bytes=="


def _delta(extra=None):
    return SimpleNamespace(extra_content=extra)


def test_allowlisted_signature_is_kept_verbatim():
    got = _bounded_extra_content(_delta({"google": {"thought_signature": SIG, "x": 1}, "other": 2}))
    assert got == {"google": {"thought_signature": SIG}}


def test_malformed_or_oversized_metadata_is_dropped():
    assert _bounded_extra_content(_delta(None)) is None
    assert _bounded_extra_content(_delta("nope")) is None
    assert _bounded_extra_content(_delta({"google": "x"})) is None
    assert _bounded_extra_content(_delta({"google": {"thought_signature": 5}})) is None
    assert _bounded_extra_content(_delta({"google": {"thought_signature": ""}})) is None
    assert _bounded_extra_content(_delta({"google": {"thought_signature": "a" * 9000}})) is None


def test_first_call_only_signature_in_parallel_calls_is_not_invented():
    acc = {
        0: {"id": "c0", "name": "a", "arguments": "{}", "extra_content": {"google": {"thought_signature": SIG}}},
        1: {"id": "c1", "name": "b", "arguments": "{}"},
    }
    msg = Orchestrator._assemble_streamed_message("", acc)
    assert isinstance(msg, ChatCompletionMessage)
    assert all(isinstance(call, ChatCompletionMessageToolCall) for call in msg.tool_calls)
    dumped = [tc.model_dump() if hasattr(tc, "model_dump") else vars(tc) for tc in msg.tool_calls]
    assert dumped[0]["extra_content"] == {"google": {"thought_signature": SIG}}
    assert "extra_content" not in dumped[1] or dumped[1]["extra_content"] is None
    assert dumped[0]["function"]["arguments"] == "{}"


@pytest.mark.parametrize("signature", ["\ud800", "\udfff", "opaque\ud800tail"])
def test_malformed_utf8_signature_is_refused_without_an_exception(signature):
    assert _bounded_extra_content(
        _delta({"google": {"thought_signature": signature}})
    ) is None


def test_multibyte_signature_retains_exact_bytes_at_the_boundary():
    signature = "🙂" * 2048
    assert _bounded_extra_content(_delta({"google": {"thought_signature": signature}})) == {
        "google": {"thought_signature": signature}
    }
    assert _bounded_extra_content(
        _delta({"google": {"thought_signature": signature + "🙂"}})
    ) is None


def test_model_extra_fallback_keeps_only_the_qualified_field():
    delta = SimpleNamespace(model_extra={
        "extra_content": {"google": {"thought_signature": SIG}, "unqualified": "ignored"}
    })
    assert _bounded_extra_content(delta) == {"google": {"thought_signature": SIG}}
    assert _bounded_extra_content(SimpleNamespace(model_extra="invalid")) is None


def test_openai_reconstruction_keeps_each_signature_with_its_call():
    accumulator = {
        8: {"id": "later", "name": "review", "arguments": '{"n":2}',
            "extra_content": {"google": {"thought_signature": "later-signature=="}}},
        2: {"id": "earlier", "name": "inspect", "arguments": '{"n":1}',
            "extra_content": {"google": {"thought_signature": SIG}}},
    }
    message = Orchestrator._assemble_streamed_message("reviewing", accumulator)
    assert isinstance(message, ChatCompletionMessage)
    assert all(isinstance(call, ChatCompletionMessageToolCall) for call in message.tool_calls)
    assert [(call.id, call.function.name, call.function.arguments,
             call.model_dump()["extra_content"]["google"]["thought_signature"])
            for call in message.tool_calls] == [
        ("earlier", "inspect", '{"n":1}', SIG),
        ("later", "review", '{"n":2}', "later-signature=="),
    ]


@pytest.mark.asyncio
async def test_typed_stream_fragments_retain_first_call_metadata_without_copying_it():
    from tests.test_llm_streaming import _FakeCompletions, _bare_orch, _stream_frames

    calls = [
        [{"index": 0, "id": "first", "type": "function",
          "function": {"name": "inspect", "arguments": "{"},
          "extra_content": {"google": {"thought_signature": SIG}}},
         {"index": 1, "id": "second", "type": "function",
          "function": {"name": "review", "arguments": "{"}}],
        [{"index": 0, "function": {"arguments": '"n":1}'}},
         {"index": 1, "function": {"arguments": '"n":2}'}}],
    ]
    chunks = [ChatCompletionChunk.model_validate({
        "id": "offline-chunk", "object": "chat.completion.chunk", "created": 0,
        "model": "offline-profile", "choices": [{"index": 0,
        "delta": {"tool_calls": delta}, "finish_reason": None}],
    }) for delta in calls]
    completions = _FakeCompletions(chunks=chunks)
    orch = _bare_orch(completions)
    message, _usage = await orch._call_llm(
        object(), [{"role": "user", "content": "inspect the synthetic fixture"}],
        tools_desc=[{"type": "function", "function": {"name": "inspect"}}],
        allow_stream=True, stream_chat_id="offline-chat",
    )
    assert isinstance(message, ChatCompletionMessage)
    assert all(isinstance(call, ChatCompletionMessageToolCall) for call in message.tool_calls)
    first, second = message.model_dump()["tool_calls"]
    assert first["extra_content"] == {"google": {"thought_signature": SIG}}
    assert "extra_content" not in second
    assert [(call.id, call.function.arguments) for call in message.tool_calls] == [
        ("first", '{"n":1}'), ("second", '{"n":2}'),
    ]
    assert _stream_frames(orch) == []


def test_fallback_tool_call_reconstruction_retains_only_associated_metadata(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def without_tool_call_type(name, *args, **kwargs):
        if name == "openai.types.chat.chat_completion_message_tool_call":
            raise ImportError("synthetic unavailable optional type")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_tool_call_type)
    message = Orchestrator._assemble_streamed_message("", {
        0: {"id": "first", "name": "inspect", "arguments": "{}",
            "extra_content": {"google": {"thought_signature": SIG}}},
        1: {"id": "second", "name": "review", "arguments": "{}"},
    })
    first, second = message.tool_calls
    assert first.extra_content == {"google": {"thought_signature": SIG}}
    assert not hasattr(second, "extra_content")
