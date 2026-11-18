"""Tests for persistent_agents/runner.py's one-shot lifecycle against real
Plane/Postgres: claim, renewal and completion transactions, recovery of expired
leases, discovery pagination and shutdown, and the fixed reader's delegation-window
renewal.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import AssignmentEpisodeCompletion
from orchestrator.work_admission import (
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OwnerScope,
    WorkAdmissionCoordinator,
)
from persistent_agents.config import RunnerConfig
from persistent_agents.runner import (
    AssignmentRunner,
    OneShotEpisodeResult,
    OneShotLifecycle,
)
from persistent_agents.runtime_values import digest
from tests.test_work_admission_repository import _classes
from tests.helpers.database_clock import database_now


from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator


async def current(op):
    return (
        await op.store.call(
            "get_operation",
            owner_id=op.record.owner_id,
            assignment_id=op.record.assignment_id,
        )
    ).assignment


async def authority(op):
    return await op.runner._operation_authority(op.record)


def completion(record, **changes):
    values = dict(
        expected_state_version=record.state_version,
        checkpoint=record.checkpoint,
        completion_digest=digest(["repository-contract", str(uuid4())]),
        phase="waiting",
        wake_reason="explicit_yield",
        next_wake_at=datetime.now(UTC) + timedelta(seconds=20),
    )
    values.update(changes)
    return AssignmentEpisodeCompletion(**values)


def assert_retry_due(before, after, backoff=timedelta(seconds=5)):
    assert before.updated_at + backoff <= after.next_wake_at <= after.updated_at + backoff


async def admission(op):
    return await asyncio.to_thread(
        op.coordinator.query_operation,
        owner=OperationOwner(OwnerScope.USER, op.record.owner_id, None),
        operation_id=op.executor.operation_fence.operation_id,
    )


def test_in_memory_renewal_refuses_foreign_transaction():
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(), repository=InMemoryWorkAdmissionRepository()
    )
    with pytest.raises(ValueError, match="external transaction"):
        coordinator.renew_execution_lease(object(), transaction=object())


@pytest.mark.parametrize(
    "value", [object(), {"sessions": object(), "episode": lambda _: None}]
)
def test_untyped_capability_refused(value):
    with pytest.raises(TypeError):
        AssignmentRunner(object(), SimpleNamespace(store=object()), one_shot=value)


@pytest.mark.parametrize("sessions,handler", [(None, lambda _: None), (object(), None)])
def test_missing_capability_fields_refused(sessions, handler):
    with pytest.raises(ValueError):
        OneShotLifecycle(sessions, handler)


async def reader_runner(op, handler):
    executor = op.executor
    runner = AssignmentRunner(
        executor.orch,
        executor.service,
        config=RunnerConfig(concurrency=2),
        one_shot=OneShotLifecycle(op.sessions, handler),
    )
    snapshot = (
        await executor.store.call(
            "get_operation",
            owner_id=op.owner,
            assignment_id=executor.record.assignment_id,
        )
    ).assignment
    due = await asyncio.to_thread(database_now, executor.store.plane_runtime)
    await runner._finish_operation(
        executor,
        OneShotEpisodeResult(snapshot, completion(snapshot, next_wake_at=due)),
    )
    return runner


