"""Exercises cancellation that outlives the caller's bounded settlement budget.
Both adapters must retain the running execution instead of reporting a false terminal result.
"""

import asyncio

import pytest

from llm_config.invocation import InvocationResult, InvocationState
from orchestrator.harness import (
    HarnessContractError,
    HarnessExecution,
    HarnessTurnRequest,
)
from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
from orchestrator.harness.typesafe_adapter import TypeSafeRoutingHarness
from orchestrator.typesafe_routing.budget import Outcome
from orchestrator.typesafe_routing.runner import RoutingOutcome


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", [StandardLLMHarness, TypeSafeRoutingHarness])
async def test_uncooperative_provider_keeps_execution_pending_after_cancellation_budget(
    adapter,
):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    harness = adapter()
    harness.cancel_timeout_seconds = 0

    async def uncooperative():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        if adapter is StandardLLMHarness:
            return InvocationResult("cancelled", 1)
        return RoutingOutcome(outcome=Outcome.CANCELLED, attempts=1)

    task = asyncio.create_task(uncooperative())
    execution = HarnessExecution(
        harness.identity,
        HarnessTurnRequest(user_id="synthetic-owner", message="x"),
        task,
        0,
        InvocationState(),
    )
    await entered.wait()
    cancellation = asyncio.create_task(harness.cancel(execution))
    try:
        await cancelled.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert cancellation.done(), (
            "cancellation waited indefinitely for the uncooperative provider"
        )
        with pytest.raises(HarnessContractError, match="running"):
            await cancellation
        assert execution.running
    finally:
        release.set()
        await asyncio.gather(cancellation, task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [None, True, float("nan"), float("inf"), -1, 31])
async def test_malformed_cancellation_budget_rejected_before_cancelling_execution(
    timeout,
):
    release = asyncio.Event()
    harness = StandardLLMHarness(cancel_timeout_seconds=timeout)
    task = asyncio.create_task(release.wait())
    execution = HarnessExecution(
        harness.identity,
        HarnessTurnRequest(user_id="synthetic-owner", message="x"),
        task,
        0,
        InvocationState(),
    )
    try:
        with pytest.raises(HarnessContractError, match="budget"):
            await harness.cancel(execution)
        assert not task.done() and not task.cancelling()
    finally:
        release.set()
        await task
