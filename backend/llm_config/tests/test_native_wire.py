"""Exercises the bounded native Messages contract with synthetic request and fragmented SSE bytes. No external provider is used."""

import json

import pytest

from llm_config.tests.native_fixtures import native_offline as native_offline

from llm_config.native_wire import NativeContractError, build_request, decode_response, decode_events

MODEL = "claude-sonnet-4-5-20250929"
TOOLS = [{"type": "function", "function": {"name": "plot", "description": "Plot public values.", "strict": True,
          "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"], "additionalProperties": False}}}]
USAGE = {"input_tokens": 10, "output_tokens": 3, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30}


def request(**kwargs):
    return build_request(bound_model=MODEL, model=MODEL,
        messages=[{"role": "system", "content": "Public instructions"}, {"role": "developer", "content": "Additional policy"},
                  {"role": "user", "content": "Plot 1"}], tools=TOOLS, **kwargs)


def response(**kwargs):
    value = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
             "content": [{"type": "tool_use", "id": "toolu_plot", "name": "plot", "input": {"x": 1}}],
             "stop_reason": "tool_use", "usage": USAGE}
    value.update(kwargs)
    return value


def test_golden_native_request_preserves_strict_schema_and_output_cap():
    payload = request(max_completion_tokens=80)
    assert payload == {"model": MODEL, "max_tokens": 80, "stream": False,
        "system": [{"type": "text", "text": "Public instructions"}, {"type": "text", "text": "Additional policy"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Plot 1"}]}],
        "tools": [{"name": "plot", "description": "Plot public values.", "strict": True, "input_schema": TOOLS[0]["function"]["parameters"]}]}


def test_tool_response_returns_ordinary_shape_and_followup_round_trips():
    result = decode_response(response(), request())
    message = result.choices[0].message
    assert message.tool_calls[0].id == "toolu_plot"
    assert json.loads(message.tool_calls[0].function.arguments) == {"x": 1}
    assert result.usage.prompt_tokens == 60 and result.usage.total_tokens == 63
    assert result.usage.prompt_tokens_details.cached_tokens == 30
    payload = build_request(bound_model=MODEL, model=MODEL, tools=TOOLS,
        messages=[{"role": "user", "content": "Plot 1"}, message,
                  {"role": "tool", "tool_call_id": "toolu_plot", "content": "plotted"}])
    assert payload["messages"][-1] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_plot", "content": [{"type": "text", "text": "plotted"}]}]}


@pytest.mark.parametrize("kwargs", [{"thinking": {"type": "enabled"}}, {"reasoning_effort": "high"},
    {"extra_headers": {"x-api-key": "other"}}, {"cache_control": {"type": "ephemeral"}},
    {"max_tokens": 0}, {"max_tokens": 8193}, {"max_tokens": True},
    {"response_format": {"type": "json_object"}}, {"temperature": float("nan")}])
def test_unsupported_and_unbounded_request_options_are_refused(kwargs):
    with pytest.raises(NativeContractError):
        request(**kwargs)


@pytest.mark.parametrize("content,stop", [
    ([{"type": "thinking", "thinking": "PRIVATE", "signature": "opaque"}], "end_turn"),
    ([{"type": "server_tool_use", "name": "web_search"}], "tool_use"),
    ([{"type": "tool_use", "name": "plot", "input": {"x": 1}}], "tool_use"),
    ([{"type": "tool_use", "id": "t", "name": "plot", "input": {"x": "private"}}], "tool_use"),
    ([{"type": "tool_use", "id": "t", "name": "unapproved", "input": {}}], "tool_use"),
    ([{"type": "tool_use", "id": "t", "name": "plot", "input": {"x": 1}}], "max_tokens"),
    ([{"type": "text", "text": "partial"}], "pause_turn"),
])
def test_unqualified_blocks_or_malformed_tool_calls_never_reach_dispatch(content, stop):
    with pytest.raises(NativeContractError):
        decode_response(response(content=content, stop_reason=stop), request())


def test_missing_native_usage_remains_unknown_without_zero_fill():
    usage = {"input_tokens": 10, "output_tokens": 3}
    result = decode_response(response(usage=usage), request())
    assert result.usage.total_tokens is None and result.usage.prompt_tokens is None
    with pytest.raises(NativeContractError):
        decode_response(response(usage=None), request())


def event(kind, **kwargs):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **kwargs}) + "\n\n").encode()


def stream_bytes():
    return b"".join([
        event("message_start", message=response(content=[], stop_reason=None, usage={**USAGE, "output_tokens": 1})),
        event("content_block_start", index=0, content_block={"type": "tool_use", "id": "toolu_plot", "name": "plot", "input": {}}),
        event("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": '{"x":'}),
        event("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": '1}'}),
        event("content_block_stop", index=0),
        event("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 2}),
        event("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 3}),
        event("message_stop"),
    ])


async def parts(raw):
    for offset in range(0, len(raw), 7):
        yield raw[offset:offset + 7]


async def test_fragmented_sse_tool_json_only_delivers_complete_validated_call_and_cumulative_usage():
    chunks = [item async for item in decode_events(parts(stream_bytes()), request())]
    calls = [call for chunk in chunks for choice in chunk.choices for call in (choice.delta.tool_calls or [])]
    assert len(calls) == 1 and json.loads(calls[0].function.arguments) == {"x": 1}
    assert chunks[-1].usage.total_tokens == 63


async def test_truncated_sse_cannot_deliver_a_tool_call():
    received = []
    raw = stream_bytes().rsplit(b"event: message_stop", 1)[0]
    with pytest.raises(NativeContractError):
        async for chunk in decode_events(parts(raw), request()):
            received.extend(call for choice in chunk.choices for call in (choice.delta.tool_calls or []))
    assert not received


@pytest.mark.parametrize("changes", [
    {"messages": []}, {"messages": "private"}, {"messages": [{"role": "assistant", "content": "prefill"}]},
    {"messages": [{"role": "user", "content": "question", "private_extra": "owner"}]},
    {"messages": [object()]}, {"messages": [{"role": "user", "content": [{"type": "image", "source": "private"}]}]},
    {"messages": [{"role": "user", "content": "question"}, {"role": "system", "content": "late"}]},
    {"messages": [{"role": "unexpected", "content": "private"}]},
    {"messages": [{"role": "user", "content": ""}]},
    {"messages": [{"role": "tool", "tool_call_id": "unbound", "content": "private"}]},
    {"messages": [{"role": "user", "content": "question"}, {"role": "assistant", "tool_calls": [{"id": "t", "function": {"name": "plot", "arguments": "{bad"}}]}]},
    {"messages": [{"role": "user", "content": "question"}, {"role": "assistant", "tool_calls": [{"id": "t", "function": {"name": "plot", "arguments": "{\"x\":1,\"x\":2}"}}]}]},
    {"messages": [{"role": "user", "content": "question"}, {"role": "assistant", "tool_calls": [{"id": "t", "function": {"name": 1, "arguments": "{}"}}]}]},
    {"messages": [{"role": "user", "content": "question"}, {"role": "assistant", "tool_calls": [{"function": {"name": "plot", "arguments": "{}"}}]}]},
    {"messages": [{"role": "user", "content": "question"}, {"role": "assistant", "tool_calls": [{"id": "t", "function": {"name": "plot", "arguments": "[]"}}]}]},
    {"messages": [{"role": "user", "content": "question"}, {"role": "assistant", "tool_calls": [{"id": "t", "function": {"name": "plot", "arguments": "{}"}}]}, {"role": "user", "content": "missing result"}]},
    {"tools": [{"type": "server_tool", "name": "search"}]}, {"tools": "private"},
    {"tools": [{"type": "function", "function": {"name": 1, "parameters": {"type": "object"}}}]},
    {"tools": [{"type": "function", "function": {"name": "plot", "parameters": {"type": "object", "properties": []}}}]},
    {"tools": [{"type": "function", "function": {"name": "plot", "parameters": {"type": "object"}, "strict": "true"}}]},
    {"tools": [{"type": "function", "function": {"name": "plot", "parameters": {"type": "object"}, "description": 7}}]},
    {"tool_choice": {"type": "function", "function": "private"}}, {"tool_choice": "unknown"},
    {"tool_choice": {"type": "function", "function": {"name": "unknown"}}},
    {"max_tokens": 1, "max_completion_tokens": 1}, {"stream": "true"},
    {"model": "different"}, {"messages": [{"role": "user", "content": "x" * 40000}]},
])
def test_malformed_request_inputs_raise_safe_capability_errors(changes):
    kwargs = {"bound_model": MODEL, "model": MODEL, "messages": [{"role": "user", "content": "question"}], "tools": TOOLS}
    kwargs.update(changes)
    with pytest.raises(NativeContractError) as caught:
        build_request(**kwargs)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("choice,want", [("none", {"type": "none"}), ("required", {"type": "any"}),
    ({"type": "function", "function": {"name": "plot"}}, {"type": "tool", "name": "plot"})])
def test_supported_tool_choices_are_explicit(choice, want):
    assert request(tool_choice=choice, temperature=0.5)["tool_choice"] == want


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": True}, {"input_tokens": 1, "output_tokens": 9000},
    {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 2**54}])
def test_missing_or_malformed_required_usage_cannot_authorize_delivery(usage):
    with pytest.raises(NativeContractError):
        decode_response(response(usage=usage), request())


@pytest.mark.parametrize("changes", [{"content": []}, {"model": "other"}, {"role": "user"}, {"id": ""},
    {"content": [None]}, {"content": [{"type": "text", "text": "safe", "citations": [{"private": True}]}]},
    {"content": [{"type": "tool_use", "id": "t", "name": "plot", "input": {"x": 1}, "private": True}]},
    {"content": [{"type": "tool_use", "id": "t", "name": "plot", "input": []}]}])
def test_malformed_responses_never_become_tool_calls(changes):
    with pytest.raises(NativeContractError):
        decode_response(response(**changes), request())


def test_json_bounds_depth_duplicate_nonfinite_and_marker_injection_are_closed():
    from llm_config.native_wire import encode, parse
    for raw in (b'{"x":1,"x":2}', b'{bad', b'{"n":NaN}', b'\xff', 7, "x" * (2 * 1024 * 1024 + 1)):
        with pytest.raises(NativeContractError):
            parse(raw)
    for value in ({"cache_control": {"type": "ephemeral"}}, {"$ref": "https://private"}, {"x": object()}, {"n": float("inf")}):
        with pytest.raises(NativeContractError):
            encode(value)
    recursive = {}
    recursive["x"] = recursive
    with pytest.raises(NativeContractError):
        encode(recursive)
    with pytest.raises(NativeContractError):
        encode({"large": "x" * 100}, maximum=10)


@pytest.mark.parametrize("suffix", [event("ping"), event("message_stop"), b"unfinished"])
async def test_trailing_sse_content_never_delivers_tools(suffix):
    calls = []
    with pytest.raises(NativeContractError):
        async for chunk in decode_events(parts(stream_bytes() + suffix), request()):
            calls.extend(call for choice in chunk.choices for call in (choice.delta.tool_calls or []))
    assert calls == []


@pytest.mark.parametrize("stop,finish", [("end_turn", "stop"), ("stop_sequence", "stop"), ("refusal", "content_filter")])
def test_complete_text_and_refusal_states_remain_explicit(stop, finish):
    result = decode_response(response(content=[{"type": "text", "text": "answer"}], stop_reason=stop), request())
    assert result.choices[0].finish_reason == finish
    assert result.choices[0].message.content == "answer"
    if stop == "refusal":
        assert result.choices[0].message.refusal == "answer"


def test_truncated_text_is_not_presented_as_complete_but_retains_reported_usage():
    with pytest.raises(NativeContractError, match="native_response_truncated") as caught:
        decode_response(response(content=[{"type": "text", "text": "partial"}], stop_reason="max_tokens"), request())
    assert caught.value.usage.total_tokens == 63


async def test_complete_text_sse_delivers_text_only_after_block_stop():
    start = response(content=[], stop_reason=None)
    raw = b"".join([b": keepalive\n\n", event("message_start", message=start), event("ping"),
        event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        event("content_block_delta", index=0, delta={"type": "text_delta", "text": "answer"}),
        event("content_block_stop", index=0),
        event("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 3}), event("message_stop")])
    chunks = [chunk async for chunk in decode_events(parts(raw.replace(b"\n", b"\r\n")), request())]
    assert [choice.delta.content for chunk in chunks for choice in chunk.choices if choice.delta.content] == ["answer"]
    assert chunks[-1].usage.total_tokens == 63


@pytest.mark.parametrize("raw", [
    event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
    event("message_start", message=None),
    event("message_start", message=response(content=[{"type": "text", "text": "unexpected"}])),
    event("message_start", message=response(content=[], stop_reason=None, model="other")),
    event("message_start", message=response(content=[], stop_reason=None)) + event("message_start", message=response(content=[], stop_reason=None)),
    event("message_start", message=response(content=[], stop_reason=None)) + event("content_block_start", index=7, content_block={"type": "text", "text": ""}),
    event("message_start", message=response(content=[], stop_reason=None)) + event("content_block_start", index=0, content_block={"type": "thinking", "thinking": "private"}),
    event("message_start", message=response(content=[], stop_reason=None)) + event("content_block_delta", index=0, delta={"type": "text_delta", "text": "private"}),
    event("message_start", message=response(content=[], stop_reason=None)) + event("content_block_stop", index=0),
    event("message_start", message=response(content=[], stop_reason=None)) + event("message_delta", delta={"model": "private"}, usage={}),
    event("message_start", message=response(content=[], stop_reason=None)) + event("message_delta", delta={}, usage={"output_tokens": 0}),
    event("error", error={"message": "PRIVATE UPSTREAM"}),
    b"data: []\n\n", b"data: \xff\n\n", b"x" * 65537,
])
async def test_malformed_sse_states_never_deliver_tools_or_leak_errors(raw):
    with pytest.raises(NativeContractError) as caught:
        async for chunk in decode_events(parts(raw), request()):
            assert not any(choice.delta.tool_calls for choice in chunk.choices)
    assert "PRIVATE" not in str(caught.value) and "private" not in str(caught.value)


def test_missing_native_cache_counters_remain_unknown_in_existing_context_usage():
    from orchestrator.context_usage import _usage
    result = decode_response(response(usage={"input_tokens": 10, "output_tokens": 3}), request())
    assert _usage(result.usage)["prompt_tokens"] is None
    assert _usage(result.usage)["total_tokens"] is None


def test_rejected_complete_tool_response_preserves_reported_consumption():
    with pytest.raises(NativeContractError) as caught:
        decode_response(response(content=[{"type": "tool_use", "id": "t", "name": "plot", "input": {"x": "wrong"}}]), request())
    assert caught.value.usage.total_tokens == 63


def test_unqualified_regex_schema_constraints_are_rejected_before_validation():
    tools = [{"type": "function", "function": {"name": "pattern_tool", "parameters": {
        "type": "object", "properties": {"text": {"type": "string", "pattern": "(a+)+$"}}}}}]
    with pytest.raises(NativeContractError, match="native_schema_unsupported"):
        build_request(bound_model=MODEL, model=MODEL, messages=[{"role": "user", "content": "question"}], tools=tools)
