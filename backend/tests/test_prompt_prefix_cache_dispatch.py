"""Dispatch-level tests for the opt-in native prefix-cache path in
Orchestrator._call_llm: flag-off compat equivalence, native engagement with cache
markers, denial fallbacks, and failure accounting — all with offline fakes.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm_config.native_messages import NativeMessagesError  # noqa: E402
from llm_config.prompt_prefix_cache import NATIVE_MESSAGES_BASE_URL  # noqa: E402
from llm_config.types import CredentialSource, LLMUnavailable  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from tests.test_call_llm_wave0 import _FakeClient, _FakeCompletions  # noqa: E402


class _KeyedClient(_FakeClient):
    def __init__(self, completions, api_key="sk-ant-test"):
        super().__init__(completions)
        self.api_key = api_key
        self.timeout = 30.0


def _native_response(*, read=0, written=0, text="native says hi", usage=None, omit_usage=False):
    return {
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        **({} if omit_usage else {
            "usage": usage if usage is not None else {
                "input_tokens": 120, "output_tokens": 30,
                "cache_creation_input_tokens": written,
                "cache_read_input_tokens": read,
            },
        }),
    }


def _orch(monkeypatch, *, base_url=NATIVE_MESSAGES_BASE_URL, model="claude-sonnet-4-5",
          api_key="sk-ant-test"):
    monkeypatch.setenv("FF_LLM_STREAMING", "false")
    monkeypatch.delenv("FF_MODEL_ROUTER", raising=False)
    from orchestrator import model_router
    monkeypatch.setattr(model_router, "router_enabled", lambda: False)
    comp = _FakeCompletions(content="compat says hi")
    resolved = types.SimpleNamespace(model=model, base_url=base_url)

    async def _resolve(ws):
        return (_KeyedClient(comp, api_key=api_key), CredentialSource.SYSTEM, resolved)

    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = None
    orch.audit_recorder = types.SimpleNamespace(record=AsyncMock())
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    orch._llm_audit_principals = lambda ws: ("owner-1", "owner-1")
    orch._resolve_llm_client_for = _resolve

    async def _noop(*a, **k):
        return None

    orch._record_llm_call = _noop
    orch._record_llm_unconfigured = _noop
    orch._emit_llm_usage_report = _noop
    orch._compat = comp
    return orch


def _approved_system():
    core = Orchestrator._approved_chat_core()
    return core + "\n\nOWNER CONTEXT: private-but-uncached."


def _messages(system):
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "plot the patients"},
    ]


@pytest.fixture
def cache_enabled(monkeypatch):
    from shared.feature_flags import flags

    prior = flags._flags.get("prompt_prefix_cache")
    flags._flags["prompt_prefix_cache"] = True
    yield
    if prior is None:
        flags._flags.pop("prompt_prefix_cache", None)
    else:
        flags._flags["prompt_prefix_cache"] = prior


@pytest.fixture
def attested_tools():
    from orchestrator.orchestrator import _STATIC_TOOL_ATTESTATION

    token = _STATIC_TOOL_ATTESTATION.set(frozenset({"graph_data"}))
    yield
    _STATIC_TOOL_ATTESTATION.reset(token)


@pytest.fixture
def native_transport(monkeypatch):
    import llm_config.native_messages as native_module
    captured = {"calls": [], "response": _native_response()}

    def fake_send(*, base_url, api_key, payload, timeout=None, **_ignored):
        assert base_url.rstrip("/") == NATIVE_MESSAGES_BASE_URL.rstrip("/")
        captured["calls"].append({
            "url": base_url.rstrip("/") + "/messages",
            "payload": payload,
            "headers": {"x-api-key": api_key},
        })
        response = captured["response"]
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(native_module, "send_native_message", fake_send)
    return captured


def test_default_transport_over_mock_http(monkeypatch):
    import httpx
    import llm_config.native_messages as native_module

    def handler(request):
        assert request.headers["x-api-key"] == "sk-ant-test"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.url.path.endswith("/messages")
        return httpx.Response(200, json=_native_response(text="mocked"))

    real_client = httpx.Client

    class _PatchedClient(real_client):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr(native_module.httpx, "Client", _PatchedClient)
    response = native_module.default_transport(
        NATIVE_MESSAGES_BASE_URL + "/messages",
        {"x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01"},
        {"model": "m"}, 5.0,
    )
    assert response["content"][0]["text"] == "mocked"

    def failing(request):
        return httpx.Response(500, json={"error": {"message": "boom"}})

    class _FailingClient(real_client):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(failing)
            super().__init__(**kwargs)

    monkeypatch.setattr(native_module.httpx, "Client", _FailingClient)
    with pytest.raises(NativeMessagesError) as excinfo:
        native_module.default_transport(
            NATIVE_MESSAGES_BASE_URL + "/messages", {"x-api-key": "k"}, {}, 5.0,
        )
    assert excinfo.value.status_code == 500


async def test_flag_off_keeps_compat_path(monkeypatch, native_transport, attested_tools):
    monkeypatch.delenv("FF_PROMPT_PREFIX_CACHE", raising=False)
    orch = _orch(monkeypatch)
    tools = [{
        "type": "function",
        "function": {"name": "graph_data", "description": "d", "parameters": {"type": "object", "properties": {}}},
    }]
    msg, usage = await orch._call_llm(
        None, _messages(_approved_system()), tools,
    )
    assert msg.content == "compat says hi"
    assert usage.total_tokens == 10
    assert native_transport["calls"] == []


async def test_native_path_engages_when_enabled(monkeypatch, native_transport, caplog, cache_enabled, attested_tools):
    monkeypatch.setenv("FF_PROMPT_PREFIX_CACHE", "true")
    native_transport["response"] = _native_response(read=2700)
    orch = _orch(monkeypatch)
    tools = [{
        "type": "function",
        "function": {"name": "graph_data", "description": "d", "parameters": {"type": "object", "properties": {}}},
    }]
    with caplog.at_level(logging.INFO, logger="LLMConfig.PrefixCache"):
        msg, usage = await orch._call_llm(
            None, _messages(_approved_system()), tools,
        )
    assert msg.content == "native says hi"
    assert orch._compat.calls == []
    assert usage.total_tokens == 120 + 30 + 2700
    assert len(native_transport["calls"]) == 1
    payload = native_transport["calls"][0]["payload"]
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["system"][0]["text"] == Orchestrator._approved_chat_core()
    assert "cache_control" not in payload["system"][1]
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["max_tokens"] > 0
    records = [r for r in caplog.records if "prefix_cache_measurement" in r.getMessage()]
    assert records
    measurement = json.loads(
        records[0].getMessage().split("prefix_cache_measurement ", 1)[1])
    assert measurement["outcome"] == "hit"
    assert measurement["owner_id"] == "owner-1"
    assert measurement["cache_read_tokens"] == 2700


async def test_denied_prefix_falls_back_to_compat(monkeypatch, native_transport, cache_enabled, attested_tools):
    orch = _orch(monkeypatch)
    dynamic_system = "You are a fully dynamic system prompt with no approved core."
    msg, _ = await orch._call_llm(None, _messages(dynamic_system), None)
    assert msg.content == "compat says hi"
    assert native_transport["calls"] == []


async def test_unattested_tools_disengage(monkeypatch, native_transport, cache_enabled, attested_tools):
    orch = _orch(monkeypatch)
    msg, _ = await orch._call_llm(
        None, _messages(_approved_system()),
        [{"type": "function", "function": {"name": "user_skill", "description": "d", "parameters": {"type": "object", "properties": {}}}}],
    )
    assert msg.content == "compat says hi"
    assert native_transport["calls"] == []


async def test_response_format_disengages(monkeypatch, native_transport, cache_enabled, attested_tools):
    orch = _orch(monkeypatch)
    msg, _ = await orch._call_llm(
        None, _messages(_approved_system()), None,
        response_format={"type": "json_object"},
    )
    assert msg.content == "compat says hi"
    assert native_transport["calls"] == []


async def test_forced_tool_choice_disengages(monkeypatch, native_transport, cache_enabled, attested_tools):
    orch = _orch(monkeypatch)
    msg, _ = await orch._call_llm(
        None, _messages(_approved_system()), None,
        tool_choice={"type": "function", "function": {"name": "graph_data"}},
    )
    assert msg.content == "compat says hi"
    assert native_transport["calls"] == []


async def test_keyless_disengages(monkeypatch, native_transport, cache_enabled, attested_tools):
    monkeypatch.setenv("FF_PROMPT_PREFIX_CACHE", "true")
    orch = _orch(monkeypatch, api_key="not-needed")
    msg, _ = await orch._call_llm(None, _messages(_approved_system()), None)
    assert msg.content == "compat says hi"
    assert native_transport["calls"] == []


async def test_non_native_profile_disengages(monkeypatch, native_transport, cache_enabled, attested_tools):
    monkeypatch.setenv("FF_PROMPT_PREFIX_CACHE", "true")
    orch = _orch(monkeypatch, base_url="https://api.openai.com/v1")
    msg, _ = await orch._call_llm(None, _messages(_approved_system()), None)
    assert msg.content == "compat says hi"
    assert native_transport["calls"] == []


async def test_native_auth_failure_audits_and_fails_closed(monkeypatch, native_transport, cache_enabled, attested_tools):
    from llm_config import audit_events
    native_transport["response"] = NativeMessagesError("native_messages_status_401", status_code=401)
    orch = _orch(monkeypatch)
    orch._record_llm_call = audit_events.record_llm_call
    msg, usage = await orch._call_llm(None, _messages(_approved_system()), None)
    assert msg is None and usage is None
    assert native_transport["calls"]
    assert orch._compat.calls == []
    event = orch.audit_recorder.record.await_args.args[0]
    assert event.event_class == "llm_call"
    assert event.outcome == "failure"


async def test_native_rate_limit_retries_then_fails(monkeypatch, native_transport, cache_enabled, attested_tools):
    monkeypatch.setenv("FF_PROMPT_PREFIX_CACHE", "true")
    native_transport["response"] = NativeMessagesError("native_messages_status_429", status_code=429)
    orch = _orch(monkeypatch)
    msg, _ = await orch._call_llm(None, _messages(_approved_system()), None)
    assert msg is None
    assert len(native_transport["calls"]) == orch.MAX_RETRIES
    assert orch._compat.calls == []


async def test_native_missing_usage_stays_unknown(monkeypatch, native_transport, cache_enabled, attested_tools):
    native_transport["response"] = _native_response(omit_usage=True)
    orch = _orch(monkeypatch)
    msg, usage = await orch._call_llm(None, _messages(_approved_system()), None)
    assert msg.content == "native says hi"
    assert usage.total_tokens is None


async def test_native_tool_round_trip_dispatch_shape(monkeypatch, native_transport, cache_enabled, attested_tools):
    from openai.types.chat.chat_completion_message_tool_call import (
        ChatCompletionMessageToolCall,
        Function,
    )
    native_transport["response"] = {
        "id": "msg_tool", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [
            {"type": "text", "text": "plotting"},
            {"type": "tool_use", "id": "toolu_1", "name": "graph_data", "input": {"x": 1}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 100, "output_tokens": 10,
                  "cache_creation_input_tokens": 2700, "cache_read_input_tokens": 0},
    }
    orch = _orch(monkeypatch)
    tools = [{
        "type": "function",
        "function": {"name": "graph_data", "description": "d", "parameters": {"type": "object", "properties": {}}},
    }]
    messages = _messages(_approved_system())
    messages.append({"role": "assistant", "content": "", "tool_calls": [
        ChatCompletionMessageToolCall(
            id="toolu_0", type="function",
            function=Function(name="graph_data", arguments=json.dumps({"y": 2})),
        )
    ]})
    messages.append({"role": "tool", "tool_call_id": "toolu_0", "content": "plotted"})
    msg, _ = await orch._call_llm(None, messages, tools)
    assert msg.content == "plotting"
    assert msg.tool_calls[0].id == "toolu_1"
    assert json.loads(msg.tool_calls[0].function.arguments) == {"x": 1}
    sent = native_transport["calls"][0]["payload"]
    assistant_blocks = [b for b in sent["messages"] if b["role"] == "assistant"]
    assert assistant_blocks[0]["content"][0]["type"] == "tool_use"
    assert assistant_blocks[0]["content"][0]["input"] == {"y": 2}
    result_blocks = [b for b in sent["messages"] if b["role"] == "user" and isinstance(b["content"], list)
                     and b["content"] and b["content"][0].get("type") == "tool_result"]
    assert result_blocks[0]["content"][0]["tool_use_id"] == "toolu_0"
