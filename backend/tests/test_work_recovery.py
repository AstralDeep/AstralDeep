"""Tests for orchestrator/work_recovery.py: executor-node failure recovery over the
work-admission coordinator with a manually advanced clock — reschedule, idempotent
replay, denial preservation (cancellation/stop/uncertainty/depth), refusals, and
stale-executor fencing after recovery.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRecord,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from orchestrator.work_recovery import (
    RECOVERY_IDEMPOTENCY_NAMESPACE,
    ExecutorRecoveryService,
    RecoveryDisposition,
    owner_stop_gate,
    recovery_interval_seconds,
)


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class _StopStore:
    def __init__(self) -> None:
        self.engaged: set[str] = set()
        self.fail = False

    def load(self, owner_id: str) -> dict | None:
        if self.fail:
            raise RuntimeError("stop store unavailable")
        if owner_id in self.engaged:
            return {"engaged": True, "epoch": 3}
        return None


def _owner(user_id: str = "owner-a") -> OperationOwner:
    return OperationOwner(OwnerScope.USER, user_id, None)


def _request(label: str) -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=_owner(),
        submission_id=submission_id,
        idempotency_namespace="ui_submission",
        idempotency_key=label,
        normalized_input_digest=hashlib.sha256(label.encode("utf-8")).hexdigest(),
        chat_id=f"chat-{label}",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _coordinator(
    clock: _FakeClock,
    *,
    active_limit: int = 2,
    queue_limit: int = 2,
    max_wait_ms: int | None = 5_000,
    slot_lease: timedelta = timedelta(seconds=30),
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=queue_limit,
                max_wait_ms=max_wait_ms,
                config_revision="test-recovery",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        slot_lease=slot_lease,
        operation_retention=timedelta(hours=24),
    )


def _service(
    coordinator: WorkAdmissionCoordinator,
    *,
    stop_gate=None,
    **kwargs,
) -> ExecutorRecoveryService:
    return ExecutorRecoveryService(
        coordinator,
        stop_gate=stop_gate or (lambda record: False),
        **kwargs,
    )


def _claimed_operation(clock: _FakeClock, label: str, **kwargs):
    coordinator = _coordinator(clock, **kwargs)
    accepted = coordinator.submit(_request(label))
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    assert claim.operation.operation_id == accepted.operation_id
    return coordinator, accepted, claim


def _record(coordinator: WorkAdmissionCoordinator, operation_id: uuid.UUID):
    record = coordinator.repository.get_operation_for_administration(operation_id)
    assert record is not None
    return record


def test_executor_lease_expiry_reschedules_supported_work() -> None:
    clock = _FakeClock()
    coordinator, accepted, claim = _claimed_operation(clock, "lost")
    original = _record(coordinator, accepted.operation_id)

    clock.advance(timedelta(seconds=30))
    report = _service(coordinator).recover_expired_executors()

    assert report.expired == 1
    assert report.count(RecoveryDisposition.RESCHEDULED) == 1
    entry = report.recovered[0]
    assert entry.operation_id == accepted.operation_id
    assert entry.recovery_operation_id is not None

    recovered = _record(coordinator, entry.recovery_operation_id)
    assert recovered.parent_operation_id == accepted.operation_id
    assert recovered.operation_kind == original.operation_kind
    assert recovered.owner_scope is OwnerScope.USER
    assert recovered.owner_user_id == "owner-a"
    assert recovered.chat_id == original.chat_id
    assert recovered.state in {OperationState.QUEUED, OperationState.RUNNING}

    resubmission = coordinator.submit(_request("lost"))
    assert resubmission.operation_id == accepted.operation_id

    replacement_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement_claim is not None
    assert replacement_claim.fence.operation_id == entry.recovery_operation_id
    assert replacement_claim.fence.execution_lease_token != claim.fence.execution_lease_token


def test_recovered_execution_continues_and_settles_under_the_new_fence() -> None:
    clock = _FakeClock()
    coordinator, accepted, stale_claim = _claimed_operation(clock, "resumed")
    clock.advance(timedelta(seconds=30))
    report = _service(coordinator).recover_expired_executors()
    recovery_id = report.recovered[0].recovery_operation_id
    assert recovery_id is not None

    with pytest.raises(StaleExecutionFenceError):
        coordinator.renew_execution_lease(stale_claim.fence)
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution(stale_claim.fence)
    with pytest.raises(StaleExecutionFenceError):
        coordinator.update_phase(stale_claim.fence, "running")
    late = coordinator.terminalize(
        stale_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Late success",
        retry_after_ms=None,
    )
    assert late.state is OperationState.RETRYABLE
    assert late.terminal_code == "execution_lease_expired"

    replacement = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement is not None
    coordinator.update_phase(replacement.fence, "running")
    settled = coordinator.terminalize(
        replacement.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Recovered work completed",
        retry_after_ms=None,
    )
    assert settled.state is OperationState.COMPLETED

    original = _record(coordinator, accepted.operation_id)
    assert original.state is OperationState.RETRYABLE
    assert original.terminal_code == "execution_lease_expired"


def test_replay_reuses_the_settled_recovery_operation() -> None:
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(clock, "replay")
    clock.advance(timedelta(seconds=30))
    service = _service(coordinator)

    first = service.recover_expired_executors()
    assert first.count(RecoveryDisposition.RESCHEDULED) == 1
    recovery_id = first.recovered[0].recovery_operation_id
    assert recovery_id is not None

    second = service.recover_expired_executors()
    assert second.expired == 0

    running_replay = service.reschedule_interrupted(
        _record(coordinator, accepted.operation_id)
    )
    assert running_replay.disposition is RecoveryDisposition.RESCHEDULED
    assert running_replay.recovery_operation_id == recovery_id

    recovery_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert recovery_claim is not None
    coordinator.terminalize(
        recovery_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Recovered work completed",
        retry_after_ms=None,
    )

    settled_replay = service.reschedule_interrupted(
        _record(coordinator, accepted.operation_id)
    )
    assert settled_replay.disposition is RecoveryDisposition.REPLAYED
    assert settled_replay.recovery_operation_id == recovery_id


def test_concurrent_reschedules_admit_exactly_one_recovery() -> None:
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(clock, "race")
    clock.advance(timedelta(seconds=30))
    expired = coordinator.expire_execution_leases()
    assert len(expired) == 1
    record = expired[0]
    service = _service(coordinator)

    start = threading.Barrier(2)
    outcomes: list = []

    def run() -> None:
        start.wait()
        outcomes.append(service.reschedule_interrupted(record))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 2
    recovery_ids = {entry.recovery_operation_id for entry in outcomes}
    assert len(recovery_ids) == 1
    recovery_id = recovery_ids.pop()
    assert recovery_id is not None
    assert recovery_id != accepted.operation_id
    third = service.reschedule_interrupted(record)
    assert third.recovery_operation_id == recovery_id


def test_concurrent_full_passes_never_duplicate_recovery() -> None:
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(clock, "sweep")
    clock.advance(timedelta(seconds=30))
    service = _service(coordinator)

    start = threading.Barrier(2)
    outcomes: list = []

    def run() -> None:
        start.wait()
        outcomes.append(service.recover_expired_executors())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = [entry for report in outcomes for entry in report.recovered]
    assert len(entries) == 1
    assert entries[0].disposition is RecoveryDisposition.RESCHEDULED
    assert entries[0].operation_id == accepted.operation_id
    assert sum(report.expired for report in outcomes) == 1


def test_cancellation_survives_lease_expiry_and_is_never_rescheduled() -> None:
    clock = _FakeClock()
    coordinator, accepted, claim = _claimed_operation(clock, "cancelled")
    cancelled = coordinator.cancel(
        owner=_owner(),
        operation_id=accepted.operation_id,
        terminal_code="cancelled_by_user",
    )
    assert cancelled.state is OperationState.RUNNING
    assert cancelled.cancel_requested_at is not None

    clock.advance(timedelta(seconds=30))
    report = _service(coordinator).recover_expired_executors()

    assert report.expired == 1
    assert report.count(RecoveryDisposition.PRESERVED_CANCELLED) == 1
    entry = report.recovered[0]
    assert entry.recovery_operation_id is None
    terminal = _record(coordinator, accepted.operation_id)
    assert terminal.state is OperationState.RETRYABLE
    assert terminal.cancel_requested_at is not None
    with pytest.raises(StaleExecutionFenceError):
        coordinator.renew_execution_lease(claim.fence)
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None


def test_owner_stop_engagement_survives_reassignment() -> None:
    clock = _FakeClock()
    coordinator, _accepted, _claim = _claimed_operation(clock, "stopped")
    store = _StopStore()
    store.engaged.add("owner-a")

    clock.advance(timedelta(seconds=30))
    report = _service(
        coordinator, stop_gate=owner_stop_gate(store)
    ).recover_expired_executors()

    assert report.count(RecoveryDisposition.PRESERVED_STOPPED) == 1
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None


def test_stop_gate_failure_fails_closed_and_preserves_work() -> None:
    clock = _FakeClock()
    coordinator, _accepted, _claim = _claimed_operation(clock, "gate-fail")
    store = _StopStore()
    store.fail = True

    clock.advance(timedelta(seconds=30))
    report = _service(
        coordinator, stop_gate=owner_stop_gate(store)
    ).recover_expired_executors()

    assert report.count(RecoveryDisposition.PRESERVED_STOPPED) == 1


def test_recovery_walk_tolerates_a_purged_ancestor() -> None:
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(clock, "purged-parent")
    clock.advance(timedelta(seconds=30))
    service = _service(coordinator)
    first = service.recover_expired_executors()
    recovery_id = first.recovered[0].recovery_operation_id
    assert recovery_id is not None

    replacement = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement is not None
    clock.advance(timedelta(hours=24))
    second = service.recover_expired_executors()
    assert second.expired == 1
    interrupted_id = replacement.fence.operation_id
    clock.advance(timedelta(seconds=1))
    purged = coordinator.purge_expired()
    assert purged.operations >= 1
    assert coordinator.repository.get_operation_for_administration(
        accepted.operation_id
    ) is None

    entry = service.reschedule_interrupted(_record(coordinator, interrupted_id))
    assert entry.disposition is RecoveryDisposition.RESCHEDULED
    assert entry.recovery_operation_id != interrupted_id


def test_stop_gate_fails_closed_without_a_user_identity() -> None:
    record = OperationRecord(
        operation_id=uuid.uuid4(),
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner_scope=OwnerScope.USER,
        owner_user_id="",
        connection_scope_id=None,
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
        chat_id=None,
        parent_operation_id=None,
        connection_generation=None,
        request_generation=None,
        state=OperationState.RETRYABLE,
        phase_code=None,
        terminal_code="execution_lease_expired",
        safe_summary=None,
        retry_after_ms=1000,
        execution_generation=1,
        execution_lease_token=None,
        state_revision=2,
        accepted_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        queue_deadline_at=None,
        started_at=datetime.now(UTC),
        terminal_at=datetime.now(UTC),
        cancel_requested_at=None,
        purge_after=None,
    )
    assert owner_stop_gate(_StopStore())(record) is True


def test_stop_gate_ignores_non_user_scopes() -> None:
    record = OperationRecord(
        operation_id=uuid.uuid4(),
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner_scope=OwnerScope.CONNECTION,
        owner_user_id=None,
        connection_scope_id=uuid.uuid4(),
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
        chat_id=None,
        parent_operation_id=None,
        connection_generation=None,
        request_generation=None,
        state=OperationState.RETRYABLE,
        phase_code=None,
        terminal_code="execution_lease_expired",
        safe_summary=None,
        retry_after_ms=1000,
        execution_generation=1,
        execution_lease_token=None,
        state_revision=2,
        accepted_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        queue_deadline_at=None,
        started_at=datetime.now(UTC),
        terminal_at=datetime.now(UTC),
        cancel_requested_at=None,
        purge_after=None,
    )
    store = _StopStore()
    store.engaged.add("someone")
    assert owner_stop_gate(store)(record) is False


def test_uncertain_phase_is_preserved_until_supported_reconciliation() -> None:
    clock = _FakeClock()
    coordinator, accepted, claim = _claimed_operation(clock, "uncertain")
    coordinator.update_phase(claim.fence, "awaiting_tool")

    clock.advance(timedelta(seconds=30))
    report = _service(coordinator).recover_expired_executors()

    assert report.count(RecoveryDisposition.PRESERVED_UNCERTAIN) == 1
    preserved = _record(coordinator, accepted.operation_id)
    assert preserved.phase_code == "awaiting_tool"
    assert preserved.state is OperationState.RETRYABLE
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution(claim.fence)


def test_recovery_depth_is_bounded_by_max_recovery_depth() -> None:
    clock = _FakeClock()
    coordinator, original, _claim = _claimed_operation(clock, "depth")
    clock.advance(timedelta(seconds=30))
    service = _service(coordinator, max_recovery_depth=1)

    first = service.recover_expired_executors()
    assert first.count(RecoveryDisposition.RESCHEDULED) == 1
    recovery_id = first.recovered[0].recovery_operation_id
    assert recovery_id is not None
    replacement = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement is not None

    clock.advance(timedelta(seconds=30))
    second = service.recover_expired_executors()

    assert second.expired == 1
    assert second.count(RecoveryDisposition.PRESERVED_EXHAUSTED) == 1
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None

    relaxed = _service(coordinator, max_recovery_depth=2)
    retried = relaxed.reschedule_interrupted(_record(coordinator, recovery_id))
    assert retried.disposition is RecoveryDisposition.RESCHEDULED
    assert retried.recovery_operation_id != recovery_id


def test_capacity_refusal_reports_reschedule_refused_and_recovers_later() -> None:
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(
        clock, "held", active_limit=1, queue_limit=0, max_wait_ms=None
    )
    clock.advance(timedelta(seconds=30))
    replacement = coordinator.submit(_request("replacement"))
    assert replacement.state is OperationState.RUNNING

    service = _service(coordinator)
    refused = service.reschedule_interrupted(_record(coordinator, accepted.operation_id))

    assert refused.disposition is RecoveryDisposition.RESCHEDULE_REFUSED
    assert refused.refusal_code == "capacity_exceeded"
    assert refused.retryable_refusal is True
    assert refused.retry_after_ms is not None
    assert refused.recovery_operation_id is None

    replacement_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement_claim is not None
    coordinator.terminalize(
        replacement_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Replacement completed",
        retry_after_ms=None,
    )

    retried = service.reschedule_interrupted(_record(coordinator, accepted.operation_id))
    assert retried.disposition is RecoveryDisposition.RESCHEDULED
    assert retried.recovery_operation_id is not None


def test_non_expired_or_missing_record_is_unavailable() -> None:
    clock = _FakeClock()
    coordinator, accepted, claim = _claimed_operation(clock, "gone")
    service = _service(coordinator)

    running = _record(coordinator, accepted.operation_id)
    assert service.reschedule_interrupted(running).disposition is (
        RecoveryDisposition.UNAVAILABLE
    )
    missing = service.reschedule_interrupted(
        replace(running, operation_id=uuid.uuid4())
    )
    assert missing.disposition is RecoveryDisposition.UNAVAILABLE

    coordinator.terminalize(
        claim.fence,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary=None,
        retry_after_ms=None,
    )
    failed = _record(coordinator, accepted.operation_id)
    assert service.reschedule_interrupted(failed).disposition is (
        RecoveryDisposition.UNAVAILABLE
    )


def test_recovery_configuration_validates_inputs() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    with pytest.raises(ValueError):
        _service(coordinator, max_recovery_depth=-1)
    with pytest.raises(ValueError):
        _service(coordinator, idempotency_namespace="")


def test_recovery_idempotency_identity_conflicts_on_divergent_semantics() -> None:
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(clock, "identity")
    clock.advance(timedelta(seconds=30))
    report = _service(coordinator).recover_expired_executors()
    recovery_id = report.recovered[0].recovery_operation_id
    assert recovery_id is not None

    duplicate = coordinator.submit(
        OperationRequest(
            operation_kind="connection_frame",
            admission_class=AdmissionClass.INTERACTIVE,
            owner=_owner(),
            submission_id=uuid.uuid4(),
            idempotency_namespace=RECOVERY_IDEMPOTENCY_NAMESPACE,
            idempotency_key=str(accepted.operation_id),
            normalized_input_digest="0" * 64,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        )
    )
    assert duplicate.accepted is False
    assert duplicate.code == "idempotency_conflict"


def test_recovery_interval_seconds_validates_environment(monkeypatch) -> None:
    monkeypatch.delenv("WORK_RECOVERY_INTERVAL_SECONDS", raising=False)
    assert recovery_interval_seconds() == 15
    monkeypatch.setenv("WORK_RECOVERY_INTERVAL_SECONDS", "45")
    assert recovery_interval_seconds() == 45
    monkeypatch.setenv("WORK_RECOVERY_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError):
        recovery_interval_seconds()
    monkeypatch.setenv("WORK_RECOVERY_INTERVAL_SECONDS", "later")
    with pytest.raises(ValueError):
        recovery_interval_seconds()
