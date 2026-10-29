"""Selected execution uses the actual standard lifecycle with unchanged guarded input and accounting.
Lifecycle metadata copies no prompt, credential or callback and never grants profile qualification.
"""

import asyncio
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from llm_config import invocation
from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
from orchestrator.harness.contract import HarnessOutcome


@pytest.mark.asyncio
async def test_selected_standard_lifecycle_is_real_and_preserves_outer_state_and_result(monkeypatch):
    from orchestrator.harness.runtime import run_standard_invocation
    phases, outcomes, calls = [], [], []
    original_start, original_await = StandardLLMHarness.start, StandardLLMHarness.await_result
    async def start(self, request):
        phases.append("start")
        assert request.message == "" and request.history == () and request.transport is None and request.api_key is None
        return await original_start(self, request)
    async def awaited(self, execution):
        phases.append("await")
        result = await original_await(self, execution)
        outcomes.append(result)
        return result
    monkeypatch.setattr(StandardLLMHarness, "start", start)
    monkeypatch.setattr(StandardLLMHarness, "await_result", awaited)
    marker = ContextVar("synthetic-original-proof", default=None)
    original, usage = object(), SimpleNamespace(total_tokens=7)
    async def physical():
        calls.append(marker.get())
        return SimpleNamespace(usage=usage)
    async def guarded():
        invocation.runner_started()
        await invocation.observe_provider(physical)
        return original, usage
    outer = invocation.InvocationState()
    async def caller():
        return await run_standard_invocation(guarded, owner_id="owner",
                                            adapter_identity=StandardLLMHarness().identity)
    token = marker.set("original-proof")
    try:
        result = await invocation.invoke_accounted(caller, outer)
    finally:
        marker.reset(token)
    assert result.message is original and result.usage is usage
    assert result.attempts == 1 and result.total_tokens == 7
    assert calls == ["original-proof"] and phases == ["start", "await"]
    assert outcomes[0].outcome is HarnessOutcome.SUCCEEDED and outcomes[0].accounting.attempts == 1


@pytest.mark.asyncio
async def test_selected_denial_has_zero_physical_attempts_and_failure_preserves_original_error():
    from orchestrator.harness.runtime import run_standard_invocation
    identity = StandardLLMHarness().identity
    async def denied():
        invocation.runner_started()
        invocation.runner_unavailable()
        return None, None
    assert await run_standard_invocation(denied, owner_id="owner", adapter_identity=identity) == (None, None)
    error = ValueError("synthetic private provider error")
    async def failed():
        invocation.runner_started()
        raise error
    with pytest.raises(ValueError) as caught:
        await run_standard_invocation(failed, owner_id="owner", adapter_identity=identity)
    assert caught.value is error


@pytest.mark.asyncio
async def test_selected_parent_cancel_uses_real_cancel_without_another_attempt(monkeypatch):
    from orchestrator.harness.runtime import run_standard_invocation
    started, never, cancelled = asyncio.Event(), asyncio.Event(), []
    original = StandardLLMHarness.cancel
    async def cancel(self, execution):
        result = await original(self, execution)
        cancelled.append(result)
        return result
    monkeypatch.setattr(StandardLLMHarness, "cancel", cancel)
    async def physical():
        started.set()
        await never.wait()
    async def guarded():
        invocation.runner_started()
        await invocation.observe_provider(physical)
    outer = invocation.InvocationState()
    async def caller():
        return await run_standard_invocation(guarded, owner_id="owner",
                                            adapter_identity=StandardLLMHarness().identity)
    task = asyncio.create_task(invocation.invoke_accounted(caller, outer))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert outer.attempts == 1 and outer.pending == 0 and outer.usage_complete is False
    assert len(cancelled) == 1 and cancelled[0].outcome is HarnessOutcome.CANCELLED


@pytest.mark.parametrize("value", [{}, [None], [{"function": {"name": ""}}],
    [{"function": {"name": "private\nname"}}], [{"function": {"name": "x" * 257}}],
    [{}] * 1025])
def test_selected_tool_metadata_rejects_malformed_unbounded_values(value):
    from orchestrator.harness.runtime import tool_metadata
    from orchestrator.harness.production_profiles import HarnessSelectionError
    with pytest.raises(HarnessSelectionError, match="harness_request_invalid"):
        tool_metadata(value)


def test_selected_tool_metadata_carries_names_without_prompt_or_authority():
    from orchestrator.harness.runtime import tool_metadata
    assert tool_metadata(None) == ()
    result = tool_metadata([{"function": {"name": "allowed_tool", "description": "private prompt"}}])
    assert len(result) == 1 and result[0].tool_name == "allowed_tool"
    assert result[0].agent_id == "" and result[0].description == ""


@pytest.mark.asyncio
async def test_selected_changed_adapter_denies_before_guarded_runner():
    from dataclasses import replace
    from orchestrator.harness.runtime import run_standard_invocation
    from orchestrator.harness.production_profiles import HarnessSelectionError
    calls = []
    async def guarded():
        calls.append(True)
    with pytest.raises(HarnessSelectionError, match="harness_adapter_changed"):
        await run_standard_invocation(guarded, owner_id="owner",
            adapter_identity=replace(StandardLLMHarness().identity, adapter_version="9.0.0"))
    assert calls == []


@pytest.mark.asyncio
async def test_selected_guarded_cancellation_remains_cancellation():
    from orchestrator.harness.runtime import run_standard_invocation
    async def guarded():
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run_standard_invocation(guarded, owner_id="owner",
            adapter_identity=StandardLLMHarness().identity)
