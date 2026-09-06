"""Concurrent delegated analyses cannot overwrite another child's authority."""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.tests.test_execution import action_record
from persistent_agents.tests.test_execution import executor as shared_executor
from persistent_agents.tests.test_service import service as shared_service

service = shared_service
executor = shared_executor


@pytest.mark.asyncio
async def test_children_share_durable_claim_and_budget_but_use_distinct_authority_sockets(executor):
    first, second = Mock(), Mock()
    child_a, child_b = executor.fork(first), executor.fork(second)
    assert child_a.claim is child_b.claim is executor.claim
    assert child_a.operation_fence is child_b.operation_fence is executor.operation_fence
    assert child_a.store is child_b.store is executor.store
    assert child_a.websocket is first and child_b.websocket is second
    assert child_a.websocket is not executor.websocket
    executor.interactive = True
    with pytest.raises(DispatchDenied, match="foreground_fanout_denied"):
        executor.fork(Mock())


@pytest.mark.asyncio
async def test_reconciled_effect_without_result_is_never_presented_as_recovered_output(executor):
    action = replace(action_record(executor.record), state="succeeded", result={
        "outcome": "reconciled_applied", "result_available": False, "result": {}})
    with pytest.raises(DispatchDenied, match="result_requires_reconciliation"):
        await executor.execute(action)
    executor.orch.execute_single_tool.assert_not_called()
