"""Offline fixtures for the opt-in prefix-cache policy: eligibility denials, marker
placement and sanitization, native payload/usage translation, and revision-bound
measurement, all without network access.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from llm_config.native_messages import (
    NativeMessagesError,
    NativeShapeError,
    build_native_payload,
    plan_native_cached_call,
    send_native_message,
    translate_native_response,
)
from llm_config.prompt_prefix_cache import (
    NATIVE_MESSAGES_BASE_URL,
    ApprovedPrefix,
    PrefixCacheDecision,
    PromptCachePolicy,
    apply_cache_markers,
    canonical_digest,
    config_revision,
    parse_native_usage,
)
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


def _policy(*, minimum=None, extra_approved=()):
    entries = (ApprovedPrefix("approved_core_v1", CORE_DIGEST), *extra_approved)
    return PromptCachePolicy(entries, minimum_prefix_tokens=minimum)


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


def _estimator_factory(tokens_by_prefix):
    def estimator(prefix, tools):
        if prefix not in tokens_by_prefix:
            raise ValueError("unestimated prefix")
        return tokens_by_prefix[prefix]
    return estimator


def _plan(*, enabled=True, policy=None, system=None, tools=None, static_names=None,
          estimator=None, owner="owner-a", model="claude-sonnet-4-5", api_key="sk-ant-test",
          base_url=NATIVE_BASE, static_prefix=APPROVED_CORE, temperature=None):
    return plan_native_cached_call(
        enabled=enabled,
        policy=policy if policy is not None else _policy(),
        base_url=base_url,
        model=model,
        api_key=api_key,
        messages=_messages(system if system is not None else APPROVED_CORE + "\n\nOwner dynamic context."),
        tools=tools if tools is not None else _tools("graph_data"),
        temperature=temperature,
        static_prefix=static_prefix,
        static_tool_names=static_names if static_names is not None else frozenset({"graph_data"}),
        owner_id=owner,
        prefix_token_estimator=estimator or _estimator_factory({APPROVED_CORE: 5000}),
    )


def test_disabled_by_default_and_flag_off():
    decision = _policy().evaluate(
        enabled=False, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
    )
    assert decision.enabled is False
    assert decision.reason == "policy_disabled"
    assert _plan(enabled=False) is None


def test_profile_gate_requires_exact_native_base_url():
    decision = _policy().evaluate(
        enabled=True, base_url="https://api.anthropic.com/v1/", model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
    )
    assert decision.enabled is True
    for base_url in ("https://api.openai.com/v1", "https://proxy.example/anthropic/v1", None, 42):
        decision = _policy().evaluate(
            enabled=True, base_url=base_url, model="claude-sonnet-4-5",
            static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
        )
        assert decision.enabled is False
        assert decision.reason == "profile_not_native"


def test_changed_prefix_denies_and_rollback_reapproves():
    drifted = APPROVED_CORE + "\nSmall instruction edit."
    policy = _policy()
    denied = policy.evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=drifted, system_content=drifted,
    )
    assert denied.enabled is False
    assert denied.reason == "prefix_not_approved"
    assert denied.prefix_digest == canonical_digest(drifted)
    rolled_back = policy.evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
    )
    assert rolled_back.enabled is True


def test_prefix_mismatch_and_missing_prefix_denies():
    policy = _policy()
    mismatch = policy.evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE,
        system_content="Different instructions entirely, not the approved core.",
    )
    assert mismatch.enabled is False
    assert mismatch.reason == "prefix_mismatch"
    missing = policy.evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=None,
    )
    assert missing.enabled is False


def test_user_skill_tool_denies_entire_prefix():
    call = _plan(static_names=frozenset({"graph_data"}), tools=_tools("graph_data", "owner_skill"))
    assert call is None
    denied = _policy().evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
        tool_defs=_tools("graph_data", "owner_skill"),
        static_tool_names=frozenset({"graph_data"}),
        prefix_token_estimator=_estimator_factory({APPROVED_CORE: 5000}),
    )
    assert denied.enabled is False
    assert denied.reason == "prefix_not_fully_approved"


def test_marker_placement_on_core_block_and_last_tool_only():
    call = _plan(system=APPROVED_CORE + "\n\nOwner dynamic context.")
    assert call is not None
    payload = call.request
    assert payload["system"][0]["text"] == APPROVED_CORE
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in payload["system"][1]
    assert payload["system"][1]["text"] == "\n\nOwner dynamic context."
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in tool for tool in payload["tools"][:-1])
    assert all("cache_control" not in message for message in payload["messages"])


def test_imported_and_model_supplied_markers_are_stripped():
    poisoned_tools = [
        {
            "type": "function",
            "function": {
                "name": "graph_data",
                "description": "d",
                "parameters": {"type": "object", "properties": {}, "cache_control": {"type": "ephemeral"}},
                "cache_control": {"type": "ephemeral"},
            },
        }
    ]
    call = _plan(tools=poisoned_tools)
    assert call is not None
    assert len(call.request["tools"]) == 1
    tool = call.request["tools"][0]
    assert tool["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tool["input_schema"]
    assert tool["input_schema"] == {"type": "object", "properties": {}}
    poisoned = {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}],
        "system": [{"type": "text", "text": APPROVED_CORE, "cache_control": {"type": "bogus"}}],
    }
    decision = PrefixCacheDecision(True, "approved", instructions=True, tools=False, static_core=APPROVED_CORE)
    marked = apply_cache_markers(poisoned, decision)
    assert marked["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked["messages"][0]["content"][0]


def test_below_minimum_skips_markers_without_padding():
    policy = _policy(minimum=100_000)
    call = _plan(policy=policy)
    assert call is None
    decision = policy.evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
        tool_defs=None, static_tool_names=frozenset(),
        prefix_token_estimator=_estimator_factory({APPROVED_CORE: 999}),
    )
    assert decision.enabled is False
    assert decision.reason == "below_minimum"
    assert decision.estimated_prefix_tokens == 999
    assert decision.minimum_prefix_tokens == 100_000


def test_admission_uncertainty_disables_caching():
    def broken_estimator(prefix, tools):
        raise ValueError("no estimate")
    decision = _policy().evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
        prefix_token_estimator=broken_estimator,
    )
    assert decision.enabled is False
    assert decision.reason == "admission_uncertain"


def test_opus_model_requires_higher_minimum():
    policy = _policy()
    decision = policy.evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-opus-4-1",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
        prefix_token_estimator=_estimator_factory({APPROVED_CORE: 1500}),
    )
    assert decision.enabled is False
    assert decision.reason == "below_minimum"
    assert decision.minimum_prefix_tokens == 2048


def test_concurrent_owner_isolation_keeps_dynamic_content_uncached():
    system_a = APPROVED_CORE + "\n\nOwner A private context: alpha"
    system_b = APPROVED_CORE + "\n\nOwner B private context: beta"
    call_a = _plan(system=system_a, owner="owner-a")
    call_b = _plan(system=system_b, owner="owner-b")
    assert call_a is not None and call_b is not None
    assert call_a.request["system"][0]["text"] == APPROVED_CORE
    assert call_b.request["system"][0]["text"] == APPROVED_CORE
    assert call_a.request["system"][0] == call_b.request["system"][0]
    assert "alpha" not in json.dumps(call_a.request["system"][0])
    assert call_a.request["system"][1]["text"].endswith("alpha")
    assert call_b.request["system"][1]["text"].endswith("beta")
    assert "cache_control" not in call_a.request["system"][1]
    assert "cache_control" not in call_b.request["system"][1]
    assert call_a.decision.policy_revision == call_b.decision.policy_revision


def test_no_tools_marks_system_only():
    call = _plan(tools=[], static_names=frozenset())
    assert call is not None
    assert "tools" not in call.request
    assert call.request["system"][0]["cache_control"] == {"type": "ephemeral"}


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


def test_send_native_message_requires_configuration():
    with pytest.raises(NativeMessagesError):
        send_native_message(base_url="", api_key="k", payload={})
    with pytest.raises(NativeMessagesError):
        send_native_message(base_url=NATIVE_BASE, api_key="", payload={})


def test_send_native_message_uses_injected_transport_and_headers():
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return {
            "id": "msg_9", "type": "message", "role": "assistant", "model": "m",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    response = send_native_message(
        base_url=NATIVE_BASE + "/", api_key="sk-ant-test",
        payload={"model": "m", "max_tokens": 8, "messages": []},
        timeout=7.5, transport=transport,
    )
    assert response["id"] == "msg_9"
    assert captured["url"] == NATIVE_BASE + "/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["timeout"] == 7.5


def test_failed_send_records_measurement_with_bound_identity(caplog):
    attempts = []

    def failing_transport(url, headers, payload, timeout):
        attempts.append(url)
        raise NativeMessagesError("native_messages_status_500", status_code=500)

    call = _plan(estimator=_estimator_factory({APPROVED_CORE: 5000}))
    assert call is not None
    import llm_config.native_messages as native_module
    original = native_module.send_native_message

    def patched(**kwargs):
        return send_native_message(transport=failing_transport, **{
            key: value for key, value in kwargs.items() if key != "transport"
        })

    native_module.send_native_message = patched
    try:
        with caplog.at_level(logging.INFO, logger="LLMConfig.PrefixCache"):
            with pytest.raises(NativeMessagesError):
                call.send()
    finally:
        native_module.send_native_message = original
    assert attempts == [NATIVE_BASE + "/messages"]
    records = [record for record in caplog.records if "prefix_cache_measurement" in record.getMessage()]
    assert records, "expected a prefix cache measurement record"
    payload = json.loads(records[0].getMessage().split("prefix_cache_measurement ", 1)[1])
    assert payload["outcome"] == "failed"
    assert payload["owner_id"] == "owner-a"
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "claude-sonnet-4-5"
    assert payload["profile"] == "native_messages"
    assert payload["policy_revision"] == call.decision.policy_revision
    assert payload["config_revision"] == config_revision(NATIVE_BASE, "claude-sonnet-4-5")


def test_hit_write_miss_measurement_outcomes(caplog):
    import llm_config.native_messages as native_module

    def make_transport(read, written):
        def transport(url, headers, payload, timeout):
            return {
                "id": "msg_x", "type": "message", "role": "assistant", "model": "m",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 10, "output_tokens": 5,
                    "cache_creation_input_tokens": written,
                    "cache_read_input_tokens": read,
                },
            }
        return transport

    call = _plan()
    original = native_module.send_native_message
    native_module.send_native_message = lambda **kwargs: send_native_message(
        transport=make_transport(2600, 0), **kwargs,
    )
    try:
        with caplog.at_level(logging.INFO, logger="LLMConfig.PrefixCache"):
            call.send()
    finally:
        native_module.send_native_message = original
    native_module.send_native_message = lambda **kwargs: send_native_message(
        transport=make_transport(0, 2700), **kwargs,
    )
    try:
        with caplog.at_level(logging.INFO, logger="LLMConfig.PrefixCache"):
            call.send()
    finally:
        native_module.send_native_message = original
    outcomes = []
    for record in caplog.records:
        text = record.getMessage()
        if "prefix_cache_measurement" in text:
            outcomes.append(json.loads(text.split("prefix_cache_measurement ", 1)[1])["outcome"])
    assert outcomes == ["hit", "write"]


def test_policy_revision_binds_approved_content():
    other = ApprovedPrefix("approved_core_v1", canonical_digest(APPROVED_CORE + "x"))
    assert _policy().policy_revision != _policy(extra_approved=(other,)).policy_revision
    assert _policy().policy_revision == _policy().policy_revision


def test_malformed_approved_prefix_rejected():
    with pytest.raises(ValueError):
        PromptCachePolicy((ApprovedPrefix("bad", "short"),))


def test_missing_static_prefix_denies():
    decision = _policy().evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=None, system_content=APPROVED_CORE,
    )
    assert decision.enabled is False
    assert decision.reason == "no_approved_prefix"


def test_non_sequence_tool_defs_denies():
    decision = _policy().evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
        tool_defs={"type": "function"},
        static_tool_names=frozenset(),
        prefix_token_estimator=_estimator_factory({APPROVED_CORE: 5000}),
    )
    assert decision.enabled is False
    assert decision.reason == "prefix_mismatch"


def test_tool_name_recognition_covers_native_and_malformed_defs():
    native_tool = {"name": "graph_data", "description": "d", "input_schema": {"type": "object"}}
    decision = _policy().evaluate(
        enabled=True, base_url=NATIVE_BASE, model="claude-sonnet-4-5",
        static_prefix=APPROVED_CORE, system_content=APPROVED_CORE,
        tool_defs=[native_tool, {"no": "name"}, 7],
        static_tool_names=frozenset({"graph_data"}),
        prefix_token_estimator=_estimator_factory({APPROVED_CORE: 5000}),
    )
    assert decision.enabled is False
    assert decision.reason == "prefix_not_fully_approved"


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


def test_default_transport_rejects_unparseable_body(monkeypatch):
    import httpx
    import llm_config.native_messages as native_module

    def handler(request):
        return httpx.Response(200, text="<html>gateway</html>")

    real_client = httpx.Client

    class _PatchedClient(real_client):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(native_module.httpx, "Client", _PatchedClient)
    with pytest.raises(NativeMessagesError) as excinfo:
        native_module.default_transport(
            NATIVE_BASE + "/messages", {"x-api-key": "k"}, {}, 5.0,
        )
    assert excinfo.value.status_code is None


def test_plan_returns_none_on_late_shape_failure():
    messages = [
        {"role": "system", "content": APPROVED_CORE},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": 5},
    ]
    call = plan_native_cached_call(
        enabled=True, policy=_policy(), base_url=NATIVE_BASE,
        model="claude-sonnet-4-5", api_key="sk-ant-test",
        messages=messages, tools=None, temperature=None,
        static_prefix=APPROVED_CORE,
        static_tool_names=frozenset(),
        prefix_token_estimator=_estimator_factory({APPROVED_CORE: 5000}),
    )
    assert call is None


def test_plan_requires_string_api_key():
    call = plan_native_cached_call(
        enabled=True, policy=_policy(), base_url=NATIVE_BASE,
        model="claude-sonnet-4-5", api_key=42,
        messages=_messages(APPROVED_CORE), tools=None, temperature=None,
        static_prefix=APPROVED_CORE,
        prefix_token_estimator=_estimator_factory({APPROVED_CORE: 5000}),
    )
    assert call is None


def test_leading_system_content_shapes():
    from llm_config.native_messages import _leading_system_content
    assert _leading_system_content(None) is None
    assert _leading_system_content([]) is None
    assert _leading_system_content([{"role": "user", "content": "x"}]) is None
    assert _leading_system_content([{"role": "system", "content": 5}]) is None
    assert _leading_system_content(_messages(APPROVED_CORE)) == APPROVED_CORE


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
