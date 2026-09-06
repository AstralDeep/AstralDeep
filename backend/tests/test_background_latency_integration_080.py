"""080-runtime-metrics: BackgroundTaskManager latency-observation seam (US2).

These integration tests drive the real ``BackgroundTaskManager`` lifecycle
(submit / completion / failure / cancellation / queued expiration) over the
existing explicit ``InMemoryWorkAdmissionRepository`` and a synthetic clock, and
assert that the new latency helper is invoked at the manager's existing
once-per-task ``_observe_terminal`` guard — not once per subscriber send.

Against unchanged ``main`` the manager never calls
``observe_background_operation``; every ``background_operations`` assertion is
therefore EXPECTED RED until feature 080 wires the seam.  A deliberately
throwing collector proves the task result, cancellation and cleanup still
succeed when telemetry fails.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.async_tasks import BackgroundTaskManager, TaskStatus
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationState,
    WorkAdmissionCoordinator,
)


@dataclass
class _Clock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


class _RecordingObservability:
    """Duck-typed collector capturing the new background-latency observations."""

    def __init__(self) -> None:
        self.operation_events: list[tuple] = []
        self.admission_statuses: list[tuple] = []
        self.retention_observations: list[tuple] = []
        self.background_operations: list = []

    def record_operation(
        self,
        event,
        *,
        operation_kind,
        result_code=None,
        phase=None,
    ) -> None:
        self.operation_events.append((event, operation_kind, result_code, phase))

    def observe_admission(self, status, *, operation_kind) -> None:
        self.admission_statuses.append((status, operation_kind))

    def observe_retention(self, *, purged_count, lag_seconds) -> None:
        self.retention_observations.append((purged_count, lag_seconds))

    def observe_background_operation(self, operation) -> None:
        self.background_operations.append(operation)


class _ThrowingBackgroundObservability(_RecordingObservability):
    """Records the attempt, then fails — lifecycle must remain unaffected."""

    def observe_background_operation(self, operation) -> None:
        super().observe_background_operation(operation)
        raise RuntimeError("private-observer-payload-080")


class _BrokenLookupObservability(_RecordingObservability):
    @property
    def observe_background_operation(self):
        self.background_operations.append(None)
        raise RuntimeError("private-observer-payload-080")


async def _collect(mgr: BackgroundTaskManager) -> None:
    tasks = [task.asyncio_task for task in mgr._tasks.values() if task.asyncio_task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition was not reached before the test deadline")
        await asyncio.sleep(0.005)


async def _settle(mgr: BackgroundTaskManager) -> None:
    await _collect(mgr)
    await _wait_until(
        lambda: mgr._admission_observer_task is None
        or mgr._admission_observer_task.done()
    )
    await mgr.drain(timeout_seconds=1)


def _manager(
    *,
    active_limit: int = 20,
    queue_limit: int = 20,
    max_wait_ms: int = 30_000,
    clock=None,
    dispatch_poll_seconds: float = 0.01,
) -> BackgroundTaskManager:
    clock = clock or _Clock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=queue_limit,
                max_wait_ms=max_wait_ms if queue_limit else None,
                config_revision="integration-080",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        slot_lease=timedelta(seconds=30),
    )
    return BackgroundTaskManager(
        coordinator=coordinator,
        dispatch_poll_seconds=dispatch_poll_seconds,
    )


@pytest.mark.asyncio
async def test_completed_task_observes_background_latency_once() -> None:
    mgr = _manager()
    observability = _RecordingObservability()
    mgr.bind(observability=observability)

    async def done(vws):
        return None

    task = await mgr.submit("c1", "u1", done, kind="background_chat")
    await _settle(mgr)

    assert task.status is TaskStatus.COMPLETED
    assert len(observability.background_operations) == 1
    observed = observability.background_operations[0]
    assert observed.state is OperationState.COMPLETED
    assert observed.accepted_at is not None
    assert observed.started_at is not None
    assert observed.terminal_at is not None


@pytest.mark.asyncio
async def test_failed_task_observes_background_latency_once() -> None:
    mgr = _manager()
    observability = _RecordingObservability()
    mgr.bind(observability=observability)

    async def boom(vws):
        raise ValueError("test-explosion")

    task = await mgr.submit("c1", "u1", boom, kind="background_chat")
    await _settle(mgr)

    assert task.status is TaskStatus.FAILED
    assert len(observability.background_operations) == 1
    assert observability.background_operations[0].state is OperationState.FAILED


@pytest.mark.asyncio
async def test_cancelled_task_observes_background_latency_once() -> None:
    mgr = _manager()
    observability = _RecordingObservability()
    mgr.bind(observability=observability)
    started = asyncio.Event()

    async def slow(vws):
        started.set()
        await asyncio.Event().wait()

    task = await mgr.submit("c1", "u1", slow, kind="background_chat")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await mgr.cancel(task.task_id) is True
    await _settle(mgr)

    assert task.status is TaskStatus.CANCELLED
    assert len(observability.background_operations) == 1
    assert observability.background_operations[0].state is OperationState.CANCELLED


@pytest.mark.asyncio
async def test_queue_expired_never_started_observation_has_no_execution_input() -> None:
    clock = _Clock()
    mgr = _manager(
        active_limit=1,
        queue_limit=1,
        max_wait_ms=50,
        clock=clock,
    )
    observability = _RecordingObservability()
    mgr.bind(observability=observability)
    release = asyncio.Event()
    queued_called = False

    async def blocker(vws):
        await release.wait()

    async def queued(vws):
        nonlocal queued_called
        queued_called = True

    try:
        await mgr.submit("c1", "u1", blocker, kind="background_chat")
        expired = await mgr.submit("c2", "u1", queued, kind="background_chat")
        clock.current += timedelta(milliseconds=51)
        await _wait_until(lambda: expired.status is TaskStatus.RETRYABLE)

        assert queued_called is False
        never_started = [
            operation
            for operation in observability.background_operations
            if operation.started_at is None
        ]
        assert len(never_started) == 1
        assert never_started[0].state is OperationState.RETRYABLE
        assert never_started[0].accepted_at is not None
        assert never_started[0].terminal_at is not None
    finally:
        release.set()
        await mgr.drain(timeout_seconds=1)


@pytest.mark.asyncio
async def test_repeated_terminal_observation_counts_latency_once() -> None:
    mgr = _manager()
    observability = _RecordingObservability()
    mgr.bind(observability=observability)

    async def done(vws):
        return None

    task = await mgr.submit("c1", "u1", done, kind="background_chat")
    await _settle(mgr)
    assert len(observability.background_operations) == 1

    # Redundant terminal observations (reconnect / duplicate delivery) must not
    # re-count latency: the once-per-task guard already fired.
    for _ in range(3):
        await mgr._observe_terminal(task)

    assert len(observability.background_operations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collector_type", (_ThrowingBackgroundObservability, _BrokenLookupObservability)
)
async def test_throwing_collector_does_not_break_completion(caplog, collector_type) -> None:
    caplog.set_level(logging.DEBUG, logger="orchestrator.async_tasks")
    mgr = _manager()
    observability = collector_type()
    mgr.bind(observability=observability)

    async def done(vws):
        return None

    task = await mgr.submit("c1", "u1", done, kind="background_chat")
    await _settle(mgr)

    assert task.status is TaskStatus.COMPLETED
    assert task._operation.state is OperationState.COMPLETED
    # The helper was attempted and its failure was contained.
    assert observability.background_operations
    assert "private-observer-payload-080" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert task.asyncio_task is None or task.asyncio_task.done()
    assert task._virtual_websocket is None
    assert mgr._coordinator.inspect_admission_class(
        AdmissionClass.BACKGROUND
    ).active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collector_type", (_ThrowingBackgroundObservability, _BrokenLookupObservability)
)
async def test_throwing_collector_does_not_break_cancellation_or_cleanup(
    collector_type,
) -> None:
    mgr = _manager()
    observability = collector_type()
    mgr.bind(observability=observability)
    started = asyncio.Event()

    async def slow(vws):
        started.set()
        await asyncio.Event().wait()

    task = await mgr.submit("c1", "u1", slow, kind="background_chat")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await mgr.cancel(task.task_id) is True
    await _settle(mgr)

    assert task.status is TaskStatus.CANCELLED
    assert task._operation.state is OperationState.CANCELLED
    assert task.asyncio_task is None or task.asyncio_task.done()
    assert task._virtual_websocket is None
    assert observability.background_operations


@pytest.mark.asyncio
async def test_real_collector_measures_completed_manager_operation() -> None:
    clock = _Clock()
    mgr = _manager(clock=clock)
    observability = RuntimeObservability(deployment_instance="integration_test")
    mgr.bind(observability=observability)

    async def done(vws):
        clock.current += timedelta(seconds=2.5)

    try:
        task = await mgr.submit("c1", "u1", done, kind="background_chat")
        await _settle(mgr)
        assert task.status is TaskStatus.COMPLETED
        totals = {
            sample.labels["phase"]: sample.value
            for sample in observability.snapshot()
            if sample.name == "background_operation_latency_seconds_sum"
        }
        assert totals == {"queue_wait": 0.0, "execution": 2.5, "end_to_end": 2.5}
    finally:
        await mgr.drain(timeout_seconds=1)


@pytest.mark.asyncio
async def test_real_collector_records_queued_cancellation_without_execution() -> None:
    clock = _Clock()
    mgr = _manager(clock=clock, active_limit=1, queue_limit=1)
    observability = RuntimeObservability(deployment_instance="integration_test")
    mgr.bind(observability=observability)
    release = asyncio.Event()
    queued_called = False

    async def blocker(vws):
        await release.wait()

    async def queued(vws):
        nonlocal queued_called
        queued_called = True

    try:
        await mgr.submit("c1", "u1", blocker)
        task = await mgr.submit("c2", "u1", queued)
        clock.current += timedelta(seconds=2)
        assert await mgr.cancel(task.task_id) is True
        assert queued_called is False
        totals = {
            sample.labels["phase"]: sample.value
            for sample in observability.snapshot()
            if sample.name == "background_operation_latency_seconds_sum"
            and sample.labels["result_code"] == "cancelled"
        }
        assert totals == {"queue_wait": 2.0, "end_to_end": 2.0}
    finally:
        release.set()
        await mgr.drain(timeout_seconds=1)
