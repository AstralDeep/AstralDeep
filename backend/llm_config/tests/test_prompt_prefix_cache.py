"""Offline fixtures for the opt-in prefix-cache policy: eligibility denials, marker
placement and sanitization, native payload/usage translation, and revision-bound
measurement, all without network access.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from llm_config.native_messages import (
    NativeMessagesError,
    NativeShapeError,
    build_native_payload,
    translate_native_response,
)
from llm_config.prompt_prefix_cache import NATIVE_MESSAGES_BASE_URL, canonical_digest, parse_native_usage
from llm_config.prefix_cache_synthetic import (
    bundled_fixture,
    ineffective_fixture,
    simulate,
)

NATIVE_BASE = NATIVE_MESSAGES_BASE_URL

APPROVED_CORE = (
    "You are a public static assistant core. "
    "These instructions are public, static, and owner-independent. " * 8
)
CORE_DIGEST = canonical_digest(APPROVED_CORE)


def _tools(*names):
    return [
        {
            "type": "function",
            "function": {"name": name, "description": f"{name} tool", "parameters": {"type": "object", "properties": {}}},
        }
        for name in names
    ]


def _messages(system, *rest):
    return [{"role": "system", "content": system}, *rest]


def test_tool_round_trip_translation():
    try:
        from openai.types.chat.chat_completion_message_tool_call import (
            ChatCompletionMessageToolCall,
            Function,
        )
        tool_call = ChatCompletionMessageToolCall(
            id="call_1", type="function",
            function=Function(name="graph_data", arguments=json.dumps({"x": 1})),
        )
    except Exception:
        tool_call = SimpleNamespace(
            id="call_1", type="function",
            function=SimpleNamespace(name="graph_data", arguments=json.dumps({"x": 1})),
        )
    messages = [
        {"role": "system", "content": APPROVED_CORE},
        {"role": "user", "content": "plot it"},
        {"role": "assistant", "content": "", "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "plotted"},
    ]
    payload = build_native_payload(
        model="claude-sonnet-4-5", messages=messages,
        tools=_tools("graph_data"), temperature=0.2,
    )
    roles = [message["role"] for message in payload["messages"]]
    assert roles == ["user", "assistant", "user"]
    assistant = payload["messages"][1]["content"]
    assert assistant[0]["type"] == "tool_use"
    assert assistant[0]["id"] == "call_1"
    assert assistant[0]["name"] == "graph_data"
    assert assistant[0]["input"] == {"x": 1}
    result = payload["messages"][2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "call_1"
    assert result["content"] == [{"type": "text", "text": "plotted"}]
    assert payload["temperature"] == 0.2


def test_unsupported_shapes_fail_closed():
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=[
                {"role": "system", "content": APPROVED_CORE},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]},
            ],
        )
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "no system"}],
        )
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=_messages(APPROVED_CORE, {"role": "user", "content": "hi"}),
            tool_choice={"type": "function", "function": {"name": "graph_data"}},
        )
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=_messages(APPROVED_CORE, {"role": "user", "content": ""}),
        )
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=_messages(APPROVED_CORE, {"role": "user", "content": "hi"}),
            tools=[{"type": "function", "function": {"name": "broken"}}],
        )


def test_native_response_translation_text_and_usage():
    response = translate_native_response({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100, "output_tokens": 20,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2600,
        },
    })
    assert response.choices[0].message.content == "hello world"
    assert response.choices[0].message.tool_calls is None
    assert response.choices[0].finish_reason == "stop"
    assert response.usage.total_tokens == 2720
    assert response.usage.prompt_tokens == 2700
    assert response.usage.completion_tokens == 20
    assert response.usage.cache_read_input_tokens == 2600


def test_native_response_tool_use_translation():
    response = translate_native_response({
        "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
        "content": [
            {"type": "text", "text": "plotting"},
            {"type": "tool_use", "id": "toolu_1", "name": "graph_data", "input": {"x": 2}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    message = response.choices[0].message
    assert message.content == "plotting"
    assert message.tool_calls is not None
    assert message.tool_calls[0].id == "toolu_1"
    assert message.tool_calls[0].function.name == "graph_data"
    assert json.loads(message.tool_calls[0].function.arguments) == {"x": 2}
    assert response.choices[0].finish_reason == "tool_calls"


def test_missing_native_usage_stays_unknown():
    response = translate_native_response({
        "id": "msg_3", "type": "message", "role": "assistant", "model": "m",
        "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
    })
    assert response.usage.total_tokens is None
    assert response.usage.prompt_tokens is None
    parsed = parse_native_usage(None)
    assert parsed.total_tokens is None
    assert parsed.cache_read_input_tokens is None


def test_native_error_payload_raises_with_status():
    with pytest.raises(NativeMessagesError) as excinfo:
        translate_native_response({
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "slow down"},
            "status_code": 429,
        })
    assert excinfo.value.status_code == 429


def test_payload_builder_content_and_tool_edge_cases():
    messages = [
        {"role": "system", "content": APPROVED_CORE},
        {"role": "user", "content": [{"type": "text", "text": "list part"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "worked"}], "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "graph_data", "arguments": {"z": 3}}},
        ]},
        {"role": "tool", "tool_call_id": "c9", "content": None},
        {"role": "user", "content": "again"},
    ]
    payload = build_native_payload(
        model="claude-sonnet-4-5", messages=messages,
        tools=[{"name": "graph_data", "input_schema": {"type": "object"}}],
        max_tokens=512,
    )
    assert payload["max_tokens"] == 512
    assert payload["messages"][0]["content"] == [{"type": "text", "text": "list part"}]
    use_block = payload["messages"][1]["content"][-1]
    assert use_block["type"] == "tool_use" and use_block["input"] == {"z": 3}
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    assert payload["tools"][0]["name"] == "graph_data"


def test_payload_builder_rejects_malformed_edges():
    base = [{"role": "system", "content": APPROVED_CORE}, {"role": "user", "content": "hi"}]
    cases = [
        {"model": "", "messages": base},
        {"model": "claude-sonnet-4-5", "messages": []},
        {"model": "claude-sonnet-4-5", "messages": base + [{"role": "x", "content": "?"}]},
        {"model": "claude-sonnet-4-5", "messages": [{"role": "system", "content": APPROVED_CORE}, "not-a-dict"]},
        {"model": "claude-sonnet-4-5", "messages": [{"role": "system", "content": APPROVED_CORE}, {"role": "assistant", "content": "", "tool_calls": [{"function": "flat"}]}]},
        {"model": "claude-sonnet-4-5", "messages": [{"role": "system", "content": 5}, {"role": "user", "content": "hi"}]},
        {"model": "claude-sonnet-4-5", "messages": base, "tools": "nope"},
        {"model": "claude-sonnet-4-5", "messages": base, "tools": [7]},
        {"model": "claude-sonnet-4-5", "messages": base + [{"role": "assistant", "content": "", "tool_calls": [
            {"function": {"arguments": "{}"}}]}]},
        {"model": "claude-sonnet-4-5", "messages": base + [{"role": "assistant", "content": ""}]},
        {"model": "claude-sonnet-4-5", "messages": base + [{"role": "tool", "tool_call_id": 5, "content": "x"}]},
        {"model": "claude-sonnet-4-5", "messages": base + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "n", "arguments": "{bad json"}}]}]},
        {"model": "claude-sonnet-4-5", "messages": base, "tools": [
            {"type": "function", "function": {"name": "n", "parameters": "not-a-schema"}}]},
    ]
    for kwargs in cases:
        with pytest.raises(NativeShapeError):
            build_native_payload(**kwargs)


def test_tool_call_fallback_without_openai_types(monkeypatch):
    import sys
    response = translate_native_response({
        "id": "msg_f", "type": "message", "role": "assistant", "model": "m",
        "content": [{"type": "tool_use", "id": "t1", "name": "n", "input": {"a": 1}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    assert response.choices[0].message.tool_calls[0].function.name == "n"
    monkeypatch.setitem(
        sys.modules, "openai.types.chat.chat_completion_message_tool_call", None)
    response = translate_native_response({
        "id": "msg_f", "type": "message", "role": "assistant", "model": "m",
        "content": [{"type": "tool_use", "id": "t2", "name": "n2", "input": {"b": 2}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    calls = response.choices[0].message.tool_calls
    assert calls[0].function.name == "n2"
    assert json.loads(calls[0].function.arguments) == {"b": 2}


def test_native_response_rejects_malformed_payloads():
    with pytest.raises(NativeMessagesError):
        translate_native_response({"content": "not-a-list"})
    with pytest.raises(NativeMessagesError):
        translate_native_response({
            "content": [{"type": "tool_use", "name": "n", "input": {}}],
        })
    with pytest.raises(NativeMessagesError):
        translate_native_response({"type": "error", "error": "flat"})


def test_synthetic_empty_fixture_is_indeterminate():
    report = simulate([], fixture_id="empty").to_dict()
    assert report["verdict"] == "indeterminate"
    assert report["savings_percent"] is None


def test_render_report_and_cli_main(capsys):
    from llm_config.prefix_cache_synthetic import main, render_report
    fixture_id, fixture = bundled_fixture()
    report = simulate(fixture, fixture_id=fixture_id)
    rendered = render_report(report)
    assert """verdict""" in rendered and """denominators""" in rendered
    assert main() == 0
    out = capsys.readouterr().out
    assert json.loads(out)["fixture_id"] == fixture_id


def test_synthetic_report_is_deterministic_and_honest():
    fixture_id, fixture = bundled_fixture()
    first = simulate(fixture, fixture_id=fixture_id, policy_revision="rev-1")
    second = simulate(list(fixture), fixture_id=fixture_id, policy_revision="rev-1")
    assert first.to_dict() == second.to_dict()
    assert first.verdict == "effective"
    assert first.requests == len(fixture)
    assert first.hits + first.misses == first.eligible_requests
    assert first.savings_percent is not None and first.savings_percent > 0
    assert first.cached_total_cost_tokens < first.uncached_total_cost_tokens
    report = first.to_dict()
    assert report["basis"].startswith("offline simulation")
    assert report["assumptions"]["effectiveness_threshold_percent"] == 10.0


def test_synthetic_ineffective_result_reported_honestly():
    fixture_id, fixture = ineffective_fixture()
    report = simulate(fixture, fixture_id=fixture_id).to_dict()
    assert report["verdict"] == "ineffective"
    assert report["savings_percent"] is not None and report["savings_percent"] <= 0
    assert report["denominators"]["hits"] == 0
    assert report["denominators"]["eligible_requests"] == len(fixture)


def test_expired_ttl_entry_misses_then_rewrites():
    from llm_config.prefix_cache_synthetic import SyntheticRequest
    fixture = [
        SyntheticRequest(0, "owner-a", "p1", 2000, 100, 50, cached=True),
        SyntheticRequest(301, "owner-a", "p1", 2000, 100, 50, cached=True),
    ]
    report = simulate(fixture, fixture_id="ttl-fixture")
    assert report.hits == 0
    assert report.writes == 2
    assert report.expired == 1

def test_remaining_builder_and_response_edges():
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=_messages(APPROVED_CORE, {"role": "user", "content": 5}),
        )
    payload = build_native_payload(
        model="claude-sonnet-4-5",
        messages=_messages(
            APPROVED_CORE,
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "graph_data", "arguments": ""}},
            ]},
            {"role": "system", "content": "Trailing addendum stays in system."},
            {"role": "user", "content": "next"},
        ),
        tools=None,
    )
    blocks = payload["messages"][1]["content"]
    assert blocks[-1] == {"type": "tool_use", "id": "c1", "name": "graph_data", "input": {}}
    assert payload["system"][-1]["text"] == "Trailing addendum stays in system."
    assert "cache_control" not in payload["system"][-1]
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=_messages(APPROVED_CORE, {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"arguments": "{}"}},
            ]}),
        )
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=_messages(APPROVED_CORE, {"role": "user", "content": "hi"}),
            tools=[{"type": "function", "function": {}}],
        )
    with pytest.raises(NativeShapeError):
        build_native_payload(
            model="claude-sonnet-4-5",
            messages=_messages(APPROVED_CORE, {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "graph_data", "arguments": 5}},
            ]}),
        )
    response = translate_native_response({
        "id": "m", "type": "message", "role": "assistant", "model": "m",
        "content": ["junk-block", {"type": "text", "text": "kept"}],
        "stop_reason": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    assert response.choices[0].message.content == "kept"
    assert response.choices[0].finish_reason is None
