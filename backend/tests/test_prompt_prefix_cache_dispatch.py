"""Checks that cache flags preserve the existing provider route while native support is unqualified. Calls use the real orchestrator method and an offline provider boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_config.types import CredentialSource, LLMUnavailable
from orchestrator.context_engineering import compose_system_prompt
from orchestrator.orchestrator import CHAT_SYSTEM_TEMPLATE, Orchestrator
from tests.test_call_llm_wave0 import _FakeClient, _FakeCompletions


@pytest.mark.parametrize("cache_enabled", [False, True])
@pytest.mark.parametrize("base_url", ["https://api.anthropic.com/v1", "https://api.openai.com/v1"])
async def test_cache_flag_preserves_ordinary_request_and_usage(monkeypatch, cache_enabled, base_url):
    from orchestrator import model_router
    from shared.feature_flags import flags

    monkeypatch.setattr(model_router, "router_enabled", lambda: False)
    monkeypatch.setitem(flags._flags, "prompt_prefix_cache", cache_enabled)
    import llm_config.native_messages as native

    def blocked(**_):
        raise RuntimeError("unqualified native request attempted")

    monkeypatch.setattr(native, "send_native_message", blocked, raising=False)
    comp = _FakeCompletions(content="ordinary provider response")
    client = _FakeClient(comp)
    client.api_key = "owner-fixture-key"
    client.timeout = 30.0
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = None
    orch.audit_recorder = None
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    orch._llm_audit_principals = lambda _: ("owner-a", "owner-a")

    async def resolve(_):
        return client, CredentialSource.USER, SimpleNamespace(model="claude-sonnet-4-5", base_url=base_url)

    async def noop(*_, **__):
        return None

    orch._resolve_llm_client_for = resolve
    orch._record_llm_call = noop
    orch._record_llm_unconfigured = noop
    orch._emit_llm_usage_report = noop
    messages = [{"role": "system", "content": compose_system_prompt(CHAT_SYSTEM_TEMPLATE)}, {"role": "user", "content": "private owner question"}]
    message, usage = await orch._call_llm(None, messages, None)
    assert message.content == "ordinary provider response"
    assert usage.total_tokens == 10
    assert comp.calls == [{"model": "claude-sonnet-4-5", "messages": messages}]
