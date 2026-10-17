"""Checks assignment recovery and current authority through PostgreSQL and signed session fixtures.
A controlled governed reader proves local fence/authority behavior, not live two-node mesh acceptance.
"""

import asyncio
from datetime import timedelta

import pytest

from orchestrator.session_authority import SessionAuthorityUnavailable
from orchestrator.work_admission import StaleExecutionFenceError
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError
from persistent_agents.research_episode import run_research_episode
from persistent_agents.tests.test_operation_reader_postgres_088 import (
    REQUEST,
    actions,
    current,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    plane as plane,
    signing_key as signing_key,
)
from persistent_agents.tests.test_research_continuation_postgres_088 import fresh_executor
from persistent_agents.tests.test_research_episode_postgres_088 import attach_runner
from tests.helpers.database_clock import wait_for_database_time
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record

runtime = plane
pytestmark = pytest.mark.asyncio


async def recover(op):
    runner = attach_runner(op, run_research_episode)
    original = await current(op)

    def expire():
        with op.runtime.transaction() as tx:
            expiry = tx.fetch_one("SELECT clock_timestamp() AS now")["now"] - timedelta(seconds=1)
            tx.execute("UPDATE persistent_assignment SET lease_expires_at=%s, "
                "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
                (expiry, expiry.isoformat(), original.assignment_id))

    await asyncio.to_thread(expire)
    result = await runner._recover_operations()
    assert result.reclaimed_assignment_ids == (original.assignment_id,)
    recovered = await current(op)
    assert recovered.assignment_id == original.assignment_id
    await wait_for_database_time(op.runtime, recovered.next_wake_at)
    return attach_runner(op, run_research_episode)


async def test_recovered_assignment_reacquires_fences_and_governed_reader_authority(operation):
    op = operation
    old = op.executor
    runner = await recover(op)
    executor = await fresh_executor(op, runner)
    assert executor.record.assignment_id == old.record.assignment_id
    assert executor.claim.fence.claim_generation > old.claim.fence.claim_generation
    assert executor.operation_fence.operation_id != old.operation_fence.operation_id
    result = await executor.action("recovered-read", REQUEST)
    assert "Public release 088" in result["text"]
    assert len(op.physical) == len(op.delegations) == 1
    assert len(await actions(op)) == 1
    with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
        await old.action("stale-read", REQUEST)
    with pytest.raises(StaleExecutionFenceError):
        op.executor.orch.work_admission.assert_current_execution(old.operation_fence)
    assert len(op.physical) == 1


@pytest.mark.parametrize("loss", ["session", "permission"])
async def test_recovered_assignment_refuses_current_authority_loss_before_effect(operation, loss):
    op = operation
    runner = await recover(op)
    if loss == "session":
        session = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
        await asyncio.to_thread(replace_session_record, op.runtime, session)
        with pytest.raises(SessionAuthorityUnavailable):
            await fresh_executor(op, runner)
    else:
        executor = await fresh_executor(op, runner)
        await asyncio.to_thread(op.executor.orch.tool_permissions.set_agent_scopes,
                                op.owner, "web-research-1", {"tools:read": False})
        with pytest.raises((DispatchDenied, AssignmentError)):
            await executor.action("refused-read", REQUEST)
    assert op.physical == []
    assert await actions(op) == ()
