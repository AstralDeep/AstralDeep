"""Nested standard lifecycle accounting propagates each physical event to original invocation states.
Frozen attempt lineage settles late cancellation without losing prior pending or unknown accounting.
"""

import asyncio
from types import SimpleNamespace

import pytest

from llm_config import invocation as module
from orchestrator.harness.contract import HarnessOutcome, HarnessTurnRequest
from orchestrator.harness.standard_llm_adapter import StandardLLMHarness


def reply(tokens=7):
    return SimpleNamespace(usage=SimpleNamespace(total_tokens=tokens))


@pytest.mark.asyncio
async def test_nested_actual_attempt_updates_both_states_during_wait_without_extra_charge():
    outer = module.InvocationState(started=True, attempts=2, total_tokens=20, pending=1, unknown_usage=True)
    observed = []
    async def physical():
        observed.append((outer.attempts, outer.pending))
        return reply()
    async def selected():
        module.runner_started()
        value = await module.observe_provider(physical)
        return object(), value.usage
    async def parent():
        harness = StandardLLMHarness(invoke=selected)
        execution = await harness.start(HarnessTurnRequest("owner", ""))
        result = await harness.await_result(execution)
        assert result.outcome is HarnessOutcome.SUCCEEDED
        assert result.accounting.attempts == 1 and result.accounting.total_tokens == 7
        assert result.accounting.usage_complete is True
        return result.decision
    result = await module.invoke_accounted(parent, outer)
    assert observed == [(3, 2)]
    assert (outer.attempts, outer.pending, outer.total_tokens, outer.unknown_usage) == (3, 1, 27, True)
    assert result.attempts == 3 and result.total_tokens == 27 and result.usage_complete is False


@pytest.mark.asyncio
async def test_late_finish_after_parent_and_child_cancel_uses_original_frozen_lineage_once():
    outer, inner = module.InvocationState(), module.InvocationState()
    started, never = asyncio.Event(), asyncio.Event()
    attempts = []
    async def child():
        module.runner_started()
        attempts.append(module.provider_started())
        started.set()
        await never.wait()
    async def parent():
        return await module.invoke_accounted(child, inner)
    task = asyncio.create_task(module.invoke_accounted(parent, outer))
    await started.wait()
    assert (outer.pending, inner.pending) == (1, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unrelated = module.InvocationState()
    async def different():
        module.runner_started()
        module.provider_finished(attempts[0], {"total_tokens": 9})
        module.provider_finished(attempts[0], {"total_tokens": 1000})
        return None, None
    await module.invoke_accounted(different, unrelated)
    assert (outer.pending, inner.pending, outer.total_tokens, inner.total_tokens) == (0, 0, 9, 9)
    assert unrelated.attempts == 0 and unrelated.total_tokens is None


@pytest.mark.asyncio
async def test_shared_concurrent_state_is_refused_before_another_physical_call():
    state = module.InvocationState()
    async def illegal():
        raise AssertionError("shared invocation ran")
    async def parent():
        module.runner_started()
        with pytest.raises(module.InvocationContractError):
            await asyncio.create_task(module.invoke_accounted(illegal, state))
        return None, None
    await module.invoke_accounted(parent, state)


@pytest.mark.asyncio
async def test_same_task_same_state_remains_single_count_and_zero_attempt_denial_propagates():
    state = module.InvocationState()
    async def selected():
        module.runner_started()
        response = await module.observe_provider(lambda: asyncio.sleep(0, result=reply(5)))
        return object(), response.usage
    async def parent():
        result = await module.invoke_accounted(selected, state)
        return result.message, result.usage
    result = await module.invoke_accounted(parent, state)
    assert result.attempts == 1 and result.total_tokens == 5 and state.pending == 0
    outer, inner = module.InvocationState(), module.InvocationState()
    async def denied():
        module.runner_started()
        module.runner_unavailable()
        return None, None
    async def original():
        result = await module.invoke_accounted(denied, inner)
        assert result.outcome == "denied" and result.attempts == 0
        return None, None
    result = await module.invoke_accounted(original, outer)
    assert result.outcome == "denied" and outer.attempts == 0


@pytest.mark.asyncio
async def test_retired_callback_cannot_charge_reused_state_and_unsettled_state_cannot_reenter():
    import contextvars
    state = module.InvocationState()
    saved, attempts = [], []
    async def first():
        module.runner_started()
        saved.append(contextvars.copy_context())
        return None, None
    await module.invoke_accounted(first, state)
    async def stale():
        with pytest.raises(module.InvocationContractError):
            module.provider_started()
    async def reused():
        module.runner_started()
        await asyncio.create_task(stale(), context=saved[0])
        attempts.append(module.provider_started())
        return None, None
    await module.invoke_accounted(reused, state)
    assert state.attempts == 1 and state.pending == 1
    with pytest.raises(module.InvocationContractError):
        await module.invoke_accounted(first, state)
    module.provider_finished(attempts[0], sent=False)
    assert state.attempts == 0 and state.pending == 0
    await module.invoke_accounted(first, state)


@pytest.mark.asyncio
async def test_parent_scope_reuse_is_refused_while_an_inherited_child_is_active():
    outer, inner = module.InvocationState(), module.InvocationState()
    started, release = asyncio.Event(), asyncio.Event()
    children = []
    async def child():
        module.runner_started()
        started.set()
        await release.wait()
        return None, None
    async def parent():
        module.runner_started()
        children.append(asyncio.create_task(module.invoke_accounted(child, inner)))
        await started.wait()
        return None, None
    await module.invoke_accounted(parent, outer)
    with pytest.raises(module.InvocationContractError):
        await module.invoke_accounted(parent, outer)
    release.set()
    await children[0]
    assert outer._children == 0


@pytest.mark.asyncio
async def test_oversized_and_cyclic_lineage_denies_before_any_physical_event():
    states = []
    async def descend():
        state = module.InvocationState()
        states.append(state)
        return await module.invoke_accounted(descend, state)
    with pytest.raises(module.InvocationContractError):
        await descend()
    assert len(states) == 33 and all(state.attempts == 0 and state._children == 0 for state in states)
    invalid = module.InvocationState()
    invalid._parents = (invalid,)
    with pytest.raises(module.InvocationContractError):
        module._state_chain(invalid)


@pytest.mark.asyncio
async def test_frozen_finish_refuses_underflow_before_mutating_another_state():
    outer, inner = module.InvocationState(), module.InvocationState()
    captured = []
    async def child():
        module.runner_started()
        captured.append(module.provider_started())
        return None, None
    async def parent():
        await module.invoke_accounted(child, inner)
        return None, None
    await module.invoke_accounted(parent, outer)
    inner.pending = 0
    with pytest.raises(module.InvocationContractError):
        module.provider_finished(captured[0], {"total_tokens": 7})
    assert outer.pending == 1 and outer.total_tokens is None
    inner.pending = 1
    module.provider_finished(captured[0], {"total_tokens": 7})
    assert (outer.pending, inner.pending, outer.total_tokens, inner.total_tokens) == (0, 0, 7, 7)
