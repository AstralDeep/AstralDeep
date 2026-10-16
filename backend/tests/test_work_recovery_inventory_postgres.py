"""Probes repeat-pass recovery through actual PostgreSQL admission state.
Controlled expiry and temporary failures do not stand in for executor delivery or staging.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    OperationState,
    PlaneWorkAdmissionRepository,
    WorkAdmissionCoordinator,
)
from orchestrator.work_recovery import (
    ExecutorRecoveryService,
    RECOVERY_IDEMPOTENCY_NAMESPACE,
    RecoveryDisposition,
)
from tests.test_work_recovery_postgres import (
    _FakeClock,
    _classes,
    _node,
    _record,
    _request,
    _service,
    clean_database as clean_database,
    postgres_database as postgres_database,
)


def lost_operation(runtime):
    clock = _FakeClock()
    first, second = _node(runtime, clock), _node(runtime, clock)
    accepted = first.submit(_request("interrupted"))
    claim = first.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    clock.advance(timedelta(seconds=30))
    return clock, first, second, accepted, claim


def assert_reconsidered(report, operation_id):
    assert any(entry.operation_id == operation_id for entry in report.recovered), report


def test_ordinary_admission_expiry_remains_in_periodic_recovery_inventory(clean_database):
    _clock, _first, second, accepted, _claim = lost_operation(clean_database)
    ordinary = second.submit(_request("ordinary-new-work"))
    assert ordinary.accepted
    original = _record(second, accepted.operation_id)
    assert original.state is OperationState.RETRYABLE
    assert original.terminal_code == "execution_lease_expired"
    report = _service(second).recover_expired_executors()
    assert report.expired == 0
    assert_reconsidered(report, accepted.operation_id)


def test_stopped_recovery_survives_a_fresh_service_pass(clean_database):
    _clock, _first, second, accepted, _claim = lost_operation(clean_database)
    stopped = ExecutorRecoveryService(second, stop_gate=lambda _record: True)
    first_pass = stopped.recover_expired_executors()
    assert first_pass.count(RecoveryDisposition.PRESERVED_STOPPED) == 1
    report = _service(second).recover_expired_executors()
    assert _record(second, accepted.operation_id).state is OperationState.RETRYABLE
    assert_reconsidered(report, accepted.operation_id)


def test_pass_read_failure_does_not_consume_recovery_inventory(clean_database, monkeypatch):
    _clock, _first, second, accepted, _claim = lost_operation(clean_database)
    read = second.repository.get_operation_for_administration

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Synthetic transient facade read failure")

    with monkeypatch.context() as fault:
        fault.setattr(second.repository, "get_operation_for_administration", unavailable)
        with pytest.raises(RuntimeError, match="facade read failure"):
            _service(second).recover_expired_executors()
    assert read(accepted.operation_id).state is OperationState.RETRYABLE
    report = _service(second).recover_expired_executors()
    assert_reconsidered(report, accepted.operation_id)


def test_capacity_refusal_is_discoverable_by_next_periodic_pass(clean_database, monkeypatch):
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=tuple(replace(c, active_limit=1, queue_limit=0) for c in _classes()),
        repository=PlaneWorkAdmissionRepository(
            plane_runtime=clean_database, plane_repositories=clean_database.repositories
        ),
        clock=clock,
    )
    accepted = coordinator.submit(_request("capacity-interrupted"))
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    clock.advance(timedelta(seconds=30))
    submit = coordinator.submit
    occupants = []

    def occupy_before_recovery(request):
        if request.idempotency_namespace == RECOVERY_IDEMPOTENCY_NAMESPACE and not occupants:
            occupants.append(submit(_request("other-live-work")))
        return submit(request)

    with monkeypatch.context() as fault:
        fault.setattr(coordinator, "submit", occupy_before_recovery)
        first = _service(coordinator).recover_expired_executors()
    assert first.count(RecoveryDisposition.RESCHEDULE_REFUSED) == 1
    assert first.recovered[0].refusal_code == "capacity_exceeded"
    assert first.recovered[0].retryable_refusal
    occupied = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert occupied is not None and occupied.fence.operation_id == occupants[0].operation_id
    coordinator.terminalize(
        occupied.fence, state=OperationState.COMPLETED,
        terminal_code=None, safe_summary="Synthetic occupancy released", retry_after_ms=None,
    )
    report = _service(coordinator).recover_expired_executors()
    assert_reconsidered(report, accepted.operation_id)
