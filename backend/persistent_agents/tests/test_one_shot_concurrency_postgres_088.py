"""Two-supervisor claim races on one due operation against actual Plane.

Two AssignmentRunner instances with distinct worker ids share one
AssignmentStore and race the same due operation. External IAM replies are the
existing synthetic fixture; every row lives in a disposable PostgreSQL schema.
No model call, physical tool, one-shot ingress or live provider is involved.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.work_admission import (
    OperationOwner,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from persistent_agents.config import RunnerConfig
from persistent_agents.runner import AssignmentRunner, OneShotLifecycle
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.test_operation_session_authority_088 import create_operation
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import signing_key as signing_key

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def cohort(runtime, fixture):
    """One shared store/coordinator/service and a builder for peer runners."""
    store = AssignmentStore(plane_runtime=runtime)
    coordinator = await asyncio.to_thread(
        WorkAdmissionCoordinator.from_plane,
        plane_runtime=runtime,
        slot_lease=timedelta(seconds=60),
    )
    host = SimpleNamespace(
        work_admission=coordinator, _unbind_machine_turn=lambda _: None
    )
    service = AssignmentService(host, store, enabled=True, phi_gate=SimpleNamespace())
    runners = []

    def runner(handler):
        value = AssignmentRunner(
            host,
            service,
            config=RunnerConfig(concurrency=2, lease_seconds=30),
            one_shot=OneShotLifecycle(fixture[0], handler),
        )
        runners.append(value)
        return value

    value = SimpleNamespace(
        runtime=runtime,
        fixture=fixture,
        store=store,
        coordinator=coordinator,
        host=host,
        service=service,
        runner=runner,
    )
    try:
        yield value
    finally:
        for value_runner in runners:
            await value_runner.stop()
        store.close()


async def due_operation(cohort):
    return await asyncio.to_thread(create_operation, cohort.fixture, cohort.runtime)


def admission(cohort, operation_id):
    return asyncio.to_thread(
        cohort.coordinator.query_operation,
        owner=OperationOwner(OwnerScope.USER, cohort.fixture[1], None),
        operation_id=operation_id,
    )


async def discover(cohort):
    return await cohort.store.transaction(
        lambda tx, repo: repo.discover_due_operations_for_administration(tx, limit=20),
        bound_session_waits=True,
    )


async def test_two_runners_race_one_due_operation_claims_exactly_once(
    cohort, monkeypatch
):
    record = await due_operation(cohort)
    handler = AsyncMock(side_effect=AssertionError("no dispatch during a claim race"))
    runner_a = cohort.runner(handler)
    runner_b = cohort.runner(handler)
    assert runner_a.worker_id != runner_b.worker_id
    # Resolve the original operation authority once; both peers claim under it so
    # the winner is decided purely by the atomic DB claim, not by racing the
    # single original session's refresh (which is a separate serialized concern).
    authority = await runner_a._operation_authority(record)
    for value in (runner_a, runner_b):
        monkeypatch.setattr(
            value, "_operation_authority", AsyncMock(return_value=authority)
        )
    started = {runner_a.worker_id: [], runner_b.worker_id: []}
    for value in (runner_a, runner_b):
        monkeypatch.setattr(
            value,
            "_start_claim",
            lambda claim, worker=value.worker_id: started[worker].append(claim),
        )
    # Gate the shared claim transaction so both peers reach the DB claim together.
    entered, ready = [], asyncio.Event()
    original_transaction = cohort.store.operation_lifecycle_transaction

    async def gated(*args, **kwargs):
        entered.append(1)
        if len(entered) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return await original_transaction(*args, **kwargs)

    monkeypatch.setattr(cohort.store, "operation_lifecycle_transaction", gated)
    await asyncio.gather(runner_a._tick_operations(), runner_b._tick_operations())
    claims = started[runner_a.worker_id] + started[runner_b.worker_id]
    # Exactly one supervisor dispatches; the loser refused its claim.
    assert len(claims) == 1
    assert claims[0].assignment.assignment_id == record.assignment_id
    # Both peers reached the claim boundary and advanced discovery; the loser
    # attempted the claim and moved its cursor past the candidate without dispatch.
    assert len(entered) == 2
    assert runner_a._operation_cursor is not None
    assert runner_b._operation_cursor is not None
    # The claim is durable: the leased operation no longer appears due.
    page = await discover(cohort)
    assert record.assignment_id not in {candidate.assignment_id for candidate in page}


async def test_crash_after_bind_recovers_only_after_lease_expiry(cohort, monkeypatch):
    record = await due_operation(cohort)
    entered, captured = asyncio.Event(), []

    async def handler(executor):
        # bind_operation has already committed before the handler runs; the
        # admission generation is live. Block as if the process then died.
        captured.append(executor)
        entered.set()
        await asyncio.Event().wait()

    runner_a = cohort.runner(handler)
    runner_b = cohort.runner(handler)
    await runner_a._tick_operations()
    await asyncio.wait_for(entered.wait(), 15)
    operation_id = captured[0].operation_fence.operation_id
    assert (await admission(cohort, operation_id)).state == OperationState.RUNNING

    # Crash runner A after bind: cancel the in-flight episode task. Cancellation
    # re-raises, so the claim lease and admission generation both persist.
    [task] = list(runner_a._active.values())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The lease has not expired, so a peer's recovery reclaims nothing.
    assert (await runner_b._recover_operations()).reclaimed_assignment_ids == ()
    assert (await admission(cohort, operation_id)).state == OperationState.RUNNING

    # Expire the exact lease as the recovery suite does.
    expiry = datetime.now(UTC) - timedelta(seconds=1)
    with cohort.runtime.transaction() as tx:
        tx.execute(
            "UPDATE persistent_assignment SET lease_expires_at=%s, "
            "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(%s::text)) WHERE id=%s",
            (expiry, expiry.isoformat(), record.assignment_id),
        )
    recovered = await runner_b._recover_operations()
    assert recovered.reclaimed_assignment_ids == (record.assignment_id,)
    # Exactly one admission generation was recovered and retired.
    assert len(recovered.operation_bindings) == 1
    assert (await admission(cohort, operation_id)).state == OperationState.FAILED

    # Retirement is idempotent: a second sweep finds no expired lease and does
    # not touch the already-failed admission generation again.
    assert (await runner_b._recover_operations()).reclaimed_assignment_ids == ()
    assert (await admission(cohort, operation_id)).state == OperationState.FAILED
