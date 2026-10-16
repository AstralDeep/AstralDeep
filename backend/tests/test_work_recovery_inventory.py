"""Verifies bounded retryable inventory discovery and replay through work admission.
The PostgreSQL companion suite checks database-backed restart and replay behavior.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from orchestrator.work_admission import (
    AdmissionClass,
    OperationOwner,
    OperationState,
    OwnerScope,
    RetryableOperationCursor,
)
from orchestrator.work_recovery import (
    RECOVERY_IDEMPOTENCY_NAMESPACE,
    RecoveryDisposition,
)

from tests.test_work_recovery import (
    _claimed_operation,
    _coordinator,
    _FakeClock,
    _record,
    _request,
    _service,
)


def test_periodic_pass_discovers_rows_expired_by_ordinary_admission():
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(clock, "ordinary-expiry")
    clock.advance(timedelta(seconds=30))
    assert coordinator.submit(_request("new-admission")).accepted
    assert _record(coordinator, accepted.operation_id).state is OperationState.RETRYABLE

    report = _service(coordinator).recover_expired_executors()

    assert report.expired == 0
    assert len(report.recovered) == 1
    assert report.recovered[0].operation_id == accepted.operation_id
    assert report.recovered[0].disposition is RecoveryDisposition.RESCHEDULED


def test_fresh_service_reconsiders_previously_stopped_inventory():
    clock = _FakeClock()
    coordinator, accepted, _claim = _claimed_operation(clock, "held-stop")
    clock.advance(timedelta(seconds=30))
    held = _service(coordinator, stop_gate=lambda _record: True)
    assert held.recover_expired_executors().count(RecoveryDisposition.PRESERVED_STOPPED) == 1

    report = _service(coordinator).recover_expired_executors()

    assert report.expired == 0
    assert report.recovered[0].operation_id == accepted.operation_id
    replacement = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert replacement.fence.operation_id == report.recovered[0].recovery_operation_id


def expired_inventory(count):
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=count, queue_limit=0, max_wait_ms=None)
    ids = []
    for index in range(count):
        accepted = coordinator.submit(_request(f"inventory-{index}"))
        ids.append(accepted.operation_id)
        assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is not None
    clock.advance(timedelta(seconds=30))
    assert len(coordinator.expire_execution_leases()) == count
    return clock, coordinator, sorted(ids, key=lambda value: value.int)


def test_bounded_pages_pass_held_rows_and_wrap_without_consuming_inventory():
    _clock, coordinator, ids = expired_inventory(3)
    service = _service(coordinator, page_size=1, stop_gate=lambda record: record.operation_id != ids[-1])

    passes = [service.recover_expired_executors() for _ in range(3)]

    assert [[entry.operation_id for entry in report.recovered] for report in passes] == [[identity] for identity in ids]
    assert [report.count(RecoveryDisposition.PRESERVED_STOPPED) for report in passes] == [1, 1, 0]
    assert passes[-1].count(RecoveryDisposition.RESCHEDULED) == 1
    wrapped = service.recover_expired_executors()
    assert wrapped.recovered[0].operation_id == ids[0]
    fresh = _service(coordinator, page_size=1).recover_expired_executors()
    assert fresh.recovered[0].operation_id == ids[0]


def test_read_failure_does_not_advance_a_partial_scan(monkeypatch):
    _clock, coordinator, ids = expired_inventory(3)
    service = _service(coordinator, page_size=1, stop_gate=lambda _record: True)
    assert service.recover_expired_executors().recovered[0].operation_id == ids[0]

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic read failure")

    with monkeypatch.context() as fault:
        fault.setattr(coordinator.repository, "get_operation_for_administration", unavailable)
        with pytest.raises(RuntimeError, match="synthetic read failure"):
            service.recover_expired_executors()
    assert service.recover_expired_executors().recovered[0].operation_id == ids[1]


def test_admission_failure_does_not_advance_or_consume_inventory(monkeypatch):
    _clock, coordinator, ids = expired_inventory(2)
    service = _service(coordinator, page_size=1)
    submit = coordinator.submit

    def unavailable(request):
        assert request.idempotency_namespace == RECOVERY_IDEMPOTENCY_NAMESPACE
        raise RuntimeError("synthetic admission failure")

    with monkeypatch.context() as fault:
        fault.setattr(coordinator, "submit", unavailable)
        with pytest.raises(RuntimeError, match="synthetic admission failure"):
            service.recover_expired_executors()
    report = service.recover_expired_executors()
    assert report.recovered[0].operation_id == ids[0]
    assert submit(_request("after-failure")).accepted


def test_retention_and_terminal_code_bound_inventory():
    clock, coordinator, _ids = expired_inventory(2)
    assert coordinator.list_retryable_for_administration(terminal_code="unrelated") == ()
    clock.advance(timedelta(hours=24))
    assert _service(coordinator).recover_expired_executors().recovered == ()


def test_retention_expiry_between_discovery_and_recovery_refuses_a_new_child():
    clock, coordinator, ids = expired_inventory(1)
    record = _record(coordinator, ids[0])
    clock.advance(timedelta(hours=24))

    outcome = _service(coordinator).reschedule_interrupted(record)

    assert outcome.disposition is RecoveryDisposition.UNAVAILABLE
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1001, 1.5, "2"])
def test_invalid_inventory_and_service_limits_are_refused(limit):
    coordinator = _coordinator(_FakeClock())
    with pytest.raises(ValueError, match="between 1 and 1000"):
        coordinator.list_retryable_for_administration(terminal_code="execution_lease_expired", limit=limit)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        _service(coordinator, page_size=limit)


@pytest.mark.parametrize("accepted_at,operation_id", [
    (datetime(2026, 8, 14), uuid.uuid4()),
    ("2026-08-14", uuid.uuid4()),
    (datetime(2026, 8, 14, tzinfo=UTC), "not-a-uuid"),
])
def test_invalid_inventory_cursor_is_refused(accepted_at, operation_id):
    with pytest.raises(ValueError):
        RetryableOperationCursor(accepted_at, operation_id)


@pytest.mark.parametrize("terminal_code,after", [(None, None), ("unsafe code", None),
                                                  ("execution_lease_expired", object())])
def test_invalid_inventory_predicate_is_refused(terminal_code, after):
    coordinator = _coordinator(_FakeClock())
    with pytest.raises(ValueError):
        coordinator.list_retryable_for_administration(terminal_code=terminal_code, after=after)


@pytest.mark.parametrize("namespace", [False, 1, "", "x" * 129])
def test_invalid_handled_namespace_is_refused(namespace):
    coordinator = _coordinator(_FakeClock())
    with pytest.raises(ValueError):
        coordinator.list_retryable_for_administration(
            terminal_code="execution_lease_expired", handled_idempotency_namespace=namespace,
        )


@pytest.mark.parametrize("owner,namespace,key", [
    (None, "test", "key"),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "", "key"),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "x" * 129, "key"),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "test", ""),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "test", "x" * 257),
])
def test_invalid_idempotent_lookup_identity_is_refused(owner, namespace, key):
    coordinator = _coordinator(_FakeClock())
    with pytest.raises(ValueError):
        coordinator.repository.get_idempotent_operation(
            owner=owner, idempotency_namespace=namespace, idempotency_key=key,
        )
