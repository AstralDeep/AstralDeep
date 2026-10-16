"""PostgreSQL two-node staging tests for orchestrator/work_recovery.py: one shared
AstralPlane runtime hosts a failing executor node and a recovering supervisor node,
with deterministic loss, race, denial, and fencing outcomes across coordinators.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    PlaneWorkAdmissionRepository,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from orchestrator.work_recovery import (
    ExecutorRecoveryService,
    RecoveryDisposition,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _owner(user_id: str = "owner-a") -> OperationOwner:
    return OperationOwner(OwnerScope.USER, user_id, None)


def _request(label: str) -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=_owner(),
        submission_id=submission_id,
        idempotency_namespace="recovery_stage",
        idempotency_key=label,
        normalized_input_digest=hashlib.sha256(label.encode("utf-8")).hexdigest(),
        chat_id=f"chat-{label}",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _classes():
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=2,
            queue_limit=0,
            max_wait_ms=None,
            config_revision="test-recovery-stage",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=2,
            queue_limit=1,
            max_wait_ms=5_000,
            config_revision="test-recovery-stage",
        ),
    )


def _node(
    runtime: PlaneTestRuntime, clock: _FakeClock
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=PlaneWorkAdmissionRepository(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        ),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )


def _service(coordinator: WorkAdmissionCoordinator) -> ExecutorRecoveryService:
    return ExecutorRecoveryService(
        coordinator, stop_gate=lambda record: False
    )


def _record(coordinator: WorkAdmissionCoordinator, operation_id: uuid.UUID):
    record = coordinator.repository.get_operation_for_administration(operation_id)
    assert record is not None
    return record


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("work_recovery") as runtime:
        yield runtime


@pytest.fixture
def clean_database(postgres_database: PlaneTestRuntime) -> PlaneTestRuntime:
    postgres_database.execute("DELETE FROM operation_submission_result")
    postgres_database.execute(
        """
        UPDATE operation_admission_slot
        SET operation_id = NULL, lease_token = NULL, lease_expires_at = NULL
        """
    )
    postgres_database.execute("DELETE FROM operation_record")
    return postgres_database


def test_failed_executor_node_is_recovered_by_the_supervisor_node(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    node_b = _node(clean_database, clock)

    accepted = node_a.submit(_request("lost-executor"))
    claim_a = node_a.claim_next(AdmissionClass.INTERACTIVE)
    assert claim_a is not None
    assert claim_a.fence.operation_id == accepted.operation_id

    clock.advance(timedelta(seconds=30))
    report = _service(node_b).recover_expired_executors()

    assert report.expired == 1
    assert report.count(RecoveryDisposition.RESCHEDULED) == 1
    recovery_id = report.recovered[0].recovery_operation_id
    assert recovery_id is not None
    assert recovery_id != accepted.operation_id

    recovered = _record(node_b, recovery_id)
    assert recovered.parent_operation_id == accepted.operation_id
    assert recovered.state in {OperationState.QUEUED, OperationState.RUNNING}

    claim_b = node_b.claim_next(AdmissionClass.INTERACTIVE)
    assert claim_b is not None
    assert claim_b.fence.operation_id == recovery_id
    node_b.update_phase(claim_b.fence, "running")
    settled = node_b.terminalize(
        claim_b.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Recovered on the second node",
        retry_after_ms=None,
    )
    assert settled.state is OperationState.COMPLETED

    with pytest.raises(StaleExecutionFenceError):
        node_a.renew_execution_lease(claim_a.fence)
    with pytest.raises(StaleExecutionFenceError):
        node_a.assert_current_execution(claim_a.fence)
    with pytest.raises(StaleExecutionFenceError):
        node_a.reselect_execution(claim_a.fence)
    late = node_a.terminalize(
        claim_a.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Late executor success",
        retry_after_ms=None,
    )
    assert late.state is OperationState.RETRYABLE
    assert late.terminal_code == "execution_lease_expired"

    projection = node_a.query_operation(
        owner=_owner(), operation_id=recovery_id
    )
    assert projection.state is OperationState.COMPLETED
    original = node_a.query_operation(owner=_owner(), operation_id=accepted.operation_id)
    assert original.state is OperationState.RETRYABLE
    assert original.terminal_code == "execution_lease_expired"


def test_cancellation_race_survives_supervisor_recovery(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    node_b = _node(clean_database, clock)

    accepted = node_a.submit(_request("cancelled-race"))
    claim_a = node_a.claim_next(AdmissionClass.INTERACTIVE)
    assert claim_a is not None
    cancelled = node_a.cancel(
        owner=_owner(),
        operation_id=accepted.operation_id,
        terminal_code="cancelled_by_user",
    )
    assert cancelled.state is OperationState.RUNNING
    assert cancelled.cancel_requested_at is not None

    clock.advance(timedelta(seconds=30))
    report = _service(node_b).recover_expired_executors()

    assert report.expired == 1
    assert report.count(RecoveryDisposition.PRESERVED_CANCELLED) == 1
    projection = node_b.query_operation(
        owner=_owner(), operation_id=accepted.operation_id
    )
    assert projection.state is OperationState.RETRYABLE
    assert projection.terminal_code == "execution_lease_expired"
    assert node_b.claim_next(AdmissionClass.INTERACTIVE) is None
    with pytest.raises(StaleExecutionFenceError):
        node_a.renew_execution_lease(claim_a.fence)


def test_uncertain_phase_is_preserved_across_nodes(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    node_b = _node(clean_database, clock)

    accepted = node_a.submit(_request("uncertain-effect"))
    claim_a = node_a.claim_next(AdmissionClass.INTERACTIVE)
    assert claim_a is not None
    node_a.update_phase(claim_a.fence, "awaiting_tool")

    clock.advance(timedelta(seconds=30))
    report = _service(node_b).recover_expired_executors()

    assert report.count(RecoveryDisposition.PRESERVED_UNCERTAIN) == 1
    assert node_b.claim_next(AdmissionClass.INTERACTIVE) is None
    projection = node_b.query_operation(
        owner=_owner(), operation_id=accepted.operation_id
    )
    assert projection.phase_code == "awaiting_tool"
    assert projection.state is OperationState.RETRYABLE


def test_concurrent_supervisor_recovery_admits_exactly_one_operation(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    node_a = _node(clean_database, clock)
    node_b = _node(clean_database, clock)

    accepted = node_a.submit(_request("concurrent-recovery"))
    claim_a = node_a.claim_next(AdmissionClass.INTERACTIVE)
    assert claim_a is not None

    clock.advance(timedelta(seconds=30))
    expired = node_b.expire_execution_leases()
    assert len(expired) == 1
    record = expired[0]
    service_b = _service(node_b)

    start = threading.Barrier(2)
    outcomes: list = []
    errors: list[Exception] = []

    def run() -> None:
        try:
            start.wait()
            outcomes.append(service_b.reschedule_interrupted(record))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(outcomes) == 2
    recovery_ids = {entry.recovery_operation_id for entry in outcomes}
    assert len(recovery_ids) == 1
    recovery_id = recovery_ids.pop()
    assert recovery_id is not None
    assert recovery_id != accepted.operation_id

    projection = node_a.query_operation(owner=_owner(), operation_id=recovery_id)
    assert projection.parent_operation_id == accepted.operation_id


def test_capacity_refusal_is_reported_and_retried_later(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()

    def build_node() -> WorkAdmissionCoordinator:
        return WorkAdmissionCoordinator(
            admission_classes=(
                AdmissionClassConfig(
                    class_name=AdmissionClass.GLOBAL,
                    parent_class_name=None,
                    active_limit=1,
                    queue_limit=0,
                    max_wait_ms=None,
                    config_revision="test-recovery-stage",
                ),
                AdmissionClassConfig(
                    class_name=AdmissionClass.INTERACTIVE,
                    parent_class_name=AdmissionClass.GLOBAL,
                    active_limit=1,
                    queue_limit=0,
                    max_wait_ms=None,
                    config_revision="test-recovery-stage",
                ),
            ),
            repository=PlaneWorkAdmissionRepository(
                plane_runtime=clean_database,
                plane_repositories=clean_database.repositories,
            ),
            clock=clock,
            operation_retention=timedelta(hours=24),
        )

    node_a = build_node()
    node_b = build_node()

    accepted = node_a.submit(_request("held-capacity"))
    claim_a = node_a.claim_next(AdmissionClass.INTERACTIVE)
    assert claim_a is not None

    clock.advance(timedelta(seconds=30))
    replacement = node_b.submit(_request("replacement-work"))
    assert replacement.state is OperationState.RUNNING

    service_b = _service(node_b)
    refused = service_b.reschedule_interrupted(
        _record(node_b, accepted.operation_id)
    )
    assert refused.disposition is RecoveryDisposition.RESCHEDULE_REFUSED
    assert refused.refusal_code == "capacity_exceeded"
    assert refused.retryable_refusal is True

    replacement_claim = node_b.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement_claim is not None
    node_b.terminalize(
        replacement_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Replacement completed",
        retry_after_ms=None,
    )

    retried = service_b.reschedule_interrupted(
        _record(node_b, accepted.operation_id)
    )
    assert retried.disposition is RecoveryDisposition.RESCHEDULED
    assert retried.recovery_operation_id is not None
    with pytest.raises(StaleExecutionFenceError):
        node_a.renew_execution_lease(claim_a.fence)
