"""Probes repeat-pass recovery through actual PostgreSQL admission state.
Controlled expiry and temporary failures do not stand in for executor delivery or staging.
"""

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from orchestrator.work_admission import (
    AdmissionClass,
    OperationNotFoundError,
    OperationOwner,
    OperationState,
    OwnerScope,
    PlaneWorkAdmissionRepository,
    RetryableOperationCursor,
    WorkAdmissionCoordinator,
)
from orchestrator.work_recovery import (
    RECOVERY_IDEMPOTENCY_NAMESPACE,
    ExecutorRecoveryService,
    RecoveryDisposition,
)

from tests.test_work_recovery_postgres import (
    _classes,
    _FakeClock,
    _node,
    _record,
    _request,
    _service,
)
from tests.test_work_recovery_postgres import (
    clean_database as clean_database,
)
from tests.test_work_recovery_postgres import (
    postgres_database as postgres_database,
)


def lost_operation(runtime, *, without_digest=False, without_delivery_binding=False,
                   operation_kind="connection_frame"):
    clock = _FakeClock()
    first, second = _node(runtime, clock), _node(runtime, clock)
    request = _request("interrupted")
    request = replace(request, operation_kind=operation_kind)
    if without_digest:
        request = replace(request, idempotency_namespace=None, idempotency_key=None, normalized_input_digest=None)
    if without_delivery_binding:
        request = replace(request, chat_id=None, connection_generation=None, request_generation=None)
    accepted = first.submit(request)
    claim = first.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    clock.advance(timedelta(seconds=30))
    return clock, first, second, accepted, claim


@pytest.mark.parametrize("kind", [
    "connection_frame", "background_chat", "mcp_request",
    "one_shot_assignment", "persistent_assignment",
])
def test_default_recovery_never_mints_a_child_without_an_execution_boundary(clean_database, kind):
    _clock, first, second, accepted, claim = lost_operation(clean_database, operation_kind=kind)
    before = clean_database.fetch_one("SELECT count(*) AS total FROM operation_submission_result")
    first_report = ExecutorRecoveryService(second, stop_gate=lambda _record: False).recover_expired_executors()
    entry, = first_report.recovered
    assert entry.operation_id == accepted.operation_id
    assert entry.disposition is RecoveryDisposition.UNAVAILABLE
    assert entry.refusal_code == "executor_recovery_unavailable"
    assert entry.recovery_operation_id is None
    assert _record(second, accepted.operation_id).state is OperationState.RETRYABLE
    assert second.claim_next(AdmissionClass.INTERACTIVE) is None
    again = ExecutorRecoveryService(second, stop_gate=lambda _record: False).recover_expired_executors()
    assert again.recovered[0].disposition is RecoveryDisposition.UNAVAILABLE
    assert clean_database.fetch_one("SELECT count(*) AS total FROM operation_record")["total"] == 1
    assert clean_database.fetch_one("SELECT count(*) AS total FROM operation_submission_result") == before
    from orchestrator.work_admission import StaleExecutionFenceError
    with pytest.raises(StaleExecutionFenceError):
        first.assert_current_execution(claim.fence)


def test_default_recovery_does_not_adopt_or_hide_an_existing_metadata_child(clean_database):
    _clock, _first, second, accepted, _claim = lost_operation(clean_database)
    metadata = _service(second).recover_expired_executors()
    child = _record(second, metadata.recovered[0].recovery_operation_id)
    before = clean_database.fetch_one("SELECT count(*) AS total FROM operation_submission_result")
    for _ in range(2):
        report = ExecutorRecoveryService(second, stop_gate=lambda _record: False).recover_expired_executors()
        original, = [entry for entry in report.recovered if entry.operation_id == accepted.operation_id]
        assert original.disposition is RecoveryDisposition.UNAVAILABLE
        assert original.recovery_operation_id is None
        assert _record(second, child.operation_id) == child
    assert clean_database.fetch_one("SELECT count(*) AS total FROM operation_record")["total"] == 2
    assert clean_database.fetch_one("SELECT count(*) AS total FROM operation_submission_result") == before


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


@pytest.mark.parametrize("has_digest", [True, False])
@pytest.mark.parametrize("has_delivery_binding", [True, False])
def test_durable_inventory_replays_one_settled_child_across_fresh_nodes(clean_database, has_digest, has_delivery_binding):
    clock, _first, second, accepted, _claim = lost_operation(
        clean_database, without_digest=not has_digest,
        without_delivery_binding=not has_delivery_binding,
    )
    first = _service(second).recover_expired_executors()
    child_id = first.recovered[0].recovery_operation_id
    child = second.claim_next(AdmissionClass.INTERACTIVE)
    assert child is not None and child.fence.operation_id == child_id
    second.terminalize(
        child.fence, state=OperationState.COMPLETED, terminal_code=None,
        safe_summary="Synthetic recovered admission settled", retry_after_ms=None,
    )
    fresh = _node(clean_database, clock)
    counts = clean_database.fetch_one(
        "SELECT (SELECT COUNT(*) FROM operation_record) AS operations, "
        "(SELECT COUNT(*) FROM operation_submission_result) AS submissions"
    )

    for _ in range(3):
        report = _service(fresh).recover_expired_executors()
        assert report.expired == 0

    assert clean_database.fetch_one(
        "SELECT (SELECT COUNT(*) FROM operation_record) AS operations, "
        "(SELECT COUNT(*) FROM operation_submission_result) AS submissions"
    ) == counts
    if has_digest:
        assert report.recovered == ()
    else:
        assert report.count(RecoveryDisposition.REPLAYED) == 1
        assert report.recovered[0].recovery_operation_id == child_id
    explicit = _service(fresh).reschedule_interrupted(_record(fresh, accepted.operation_id))
    assert explicit.disposition is RecoveryDisposition.REPLAYED
    assert explicit.recovery_operation_id == child_id
    assert fresh.claim_next(AdmissionClass.INTERACTIVE) is None
    with pytest.raises(OperationNotFoundError):
        fresh.query_operation(
            owner=OperationOwner(OwnerScope.USER, "foreign-owner", None), operation_id=child_id,
        )


def test_durable_keyset_pages_do_not_mutate_or_consume_held_rows(clean_database):
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=tuple(replace(c, active_limit=3, queue_limit=0) for c in _classes()),
        repository=PlaneWorkAdmissionRepository(
            plane_runtime=clean_database, plane_repositories=clean_database.repositories,
        ), clock=clock,
    )
    ids = []
    for index in range(3):
        ids.append(coordinator.submit(_request(f"page-{index}")).operation_id)
        assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is not None
    clock.advance(timedelta(seconds=30))
    coordinator.expire_execution_leases()
    ids.sort(key=lambda identity: identity.int)
    first = coordinator.list_retryable_for_administration(
        terminal_code="execution_lease_expired", limit=1,
    )
    assert len(first) == 1 and first[0].operation_id == ids[0]
    after = RetryableOperationCursor(first[0].accepted_at, first[0].operation_id)
    second = coordinator.list_retryable_for_administration(
        terminal_code="execution_lease_expired", limit=1, after=after,
    )
    assert len(second) == 1 and second[0].operation_id == ids[1]
    assert _record(coordinator, ids[0]) == first[0]

    service = ExecutorRecoveryService(coordinator, stop_gate=lambda _record: True, page_size=1)
    reports = [service.recover_expired_executors() for _ in range(4)]
    assert [report.recovered[0].operation_id for report in reports] == [*ids, ids[0]]
    assert all(report.count(RecoveryDisposition.PRESERVED_STOPPED) == 1 for report in reports)


@pytest.mark.parametrize("mismatch", ["owner", "connection", "namespace", "parent", "key", "kind", "class", "digest",
                                        "chat", "connection_generation", "request_generation"])
def test_unrelated_child_cannot_hide_a_durable_pending_recovery(clean_database, mismatch):
    _clock, _first, coordinator, accepted, _claim = lost_operation(clean_database)
    coordinator.expire_execution_leases()
    source = _record(coordinator, accepted.operation_id)
    owner = OperationOwner(source.owner_scope, source.owner_user_id, source.connection_scope_id)
    request = replace(
        _request("unrelated-child"), owner=owner,
        idempotency_namespace=RECOVERY_IDEMPOTENCY_NAMESPACE,
        idempotency_key=str(source.operation_id),
        parent_operation_id=source.operation_id,
        normalized_input_digest=source.normalized_input_digest,
        chat_id=source.chat_id, connection_generation=source.connection_generation,
        request_generation=source.request_generation,
    )
    if mismatch == "owner":
        request = replace(request, owner=OperationOwner(OwnerScope.USER, "foreign-owner", None))
    elif mismatch == "connection":
        request = replace(request, owner=OperationOwner(source.owner_scope, source.owner_user_id, uuid.uuid4()))
    elif mismatch == "namespace":
        request = replace(request, idempotency_namespace="unrelated.namespace")
    elif mismatch == "parent":
        other = coordinator.submit(_request("other-parent"))
        request = replace(request, parent_operation_id=other.operation_id)
    elif mismatch == "key":
        request = replace(request, idempotency_key="unrelated-key")
    elif mismatch == "kind":
        request = replace(request, operation_kind="other_operation")
    elif mismatch == "class":
        coordinator.repository.configure((*_classes(), replace(_classes()[1], class_name=AdmissionClass.BACKGROUND)))
        request = replace(request, admission_class=AdmissionClass.BACKGROUND)
    elif mismatch == "chat":
        request = replace(request, chat_id="different-delivery-chat")
    elif mismatch == "connection_generation":
        request = replace(request, connection_generation=uuid.uuid4())
    elif mismatch == "request_generation":
        request = replace(request, request_generation=uuid.uuid4())
    else:
        request = replace(request, normalized_input_digest="a" * 64)
    assert coordinator.submit(request).accepted

    pending = coordinator.list_retryable_for_administration(
        terminal_code="execution_lease_expired",
        handled_idempotency_namespace=RECOVERY_IDEMPOTENCY_NAMESPACE,
    )

    assert [record.operation_id for record in pending] == [accepted.operation_id]
    if mismatch in {"connection", "parent", "kind", "class", "digest", "chat", "connection_generation", "request_generation"}:
        counts = clean_database.fetch_one(
            "SELECT (SELECT COUNT(*) FROM operation_record) AS operations, "
            "(SELECT COUNT(*) FROM operation_submission_result) AS submissions"
        )
        for _ in range(3):
            report = _service(coordinator).recover_expired_executors()
            assert report.count(RecoveryDisposition.RESCHEDULE_REFUSED) == 1
            assert report.recovered[0].refusal_code == "idempotency_conflict"
            assert not report.recovered[0].retryable_refusal
        assert clean_database.fetch_one(
            "SELECT (SELECT COUNT(*) FROM operation_record) AS operations, "
            "(SELECT COUNT(*) FROM operation_submission_result) AS submissions"
        ) == counts


def test_expired_original_cannot_return_when_its_handled_child_is_purged(clean_database):
    clock, _first, coordinator, accepted, _claim = lost_operation(clean_database)
    report = _service(coordinator).recover_expired_executors()
    child_id = report.recovered[0].recovery_operation_id
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    coordinator.terminalize(
        claim.fence, state=OperationState.COMPLETED, terminal_code=None,
        safe_summary="Synthetic child settled", retry_after_ms=None,
    )
    clock.advance(timedelta(days=2))
    assert coordinator.purge_expired().operations == 2
    assert coordinator.repository.get_operation_for_administration(child_id) is None
    assert coordinator.repository.get_operation_for_administration(accepted.operation_id) is None
    assert _service(coordinator).recover_expired_executors().recovered == ()


def test_durable_orphan_cannot_reset_the_original_recovery_depth(clean_database):
    clock, _first, coordinator, accepted, _claim = lost_operation(clean_database)
    child_id = _service(coordinator).recover_expired_executors().recovered[0].recovery_operation_id
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE).fence.operation_id == child_id
    clock.advance(timedelta(hours=24))
    assert _service(coordinator).recover_expired_executors().count(RecoveryDisposition.PRESERVED_EXHAUSTED) == 1
    clock.advance(timedelta(seconds=1))
    assert coordinator.purge_expired().operations == 1
    assert coordinator.repository.get_operation_for_administration(accepted.operation_id) is None

    report = _service(_node(clean_database, clock)).recover_expired_executors()

    assert report.count(RecoveryDisposition.PRESERVED_EXHAUSTED) == 1
    assert report.recovered[0].operation_id == child_id
    assert report.recovered[0].recovery_operation_id is None
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None


def test_legacy_conflicting_child_is_visible_and_refused_without_submission_growth(clean_database):
    _clock, _first, coordinator, accepted, _claim = lost_operation(clean_database, without_digest=True)
    coordinator.expire_execution_leases()
    source = _record(coordinator, accepted.operation_id)
    child = replace(
        _request("conflicting-legacy-child"),
        parent_operation_id=accepted.operation_id,
        idempotency_namespace=RECOVERY_IDEMPOTENCY_NAMESPACE,
        idempotency_key=str(accepted.operation_id), normalized_input_digest="a" * 64,
        chat_id=source.chat_id, connection_generation=source.connection_generation,
        request_generation=source.request_generation,
    )
    assert coordinator.submit(child).accepted
    counts = clean_database.fetch_one(
        "SELECT (SELECT COUNT(*) FROM operation_record) AS operations, "
        "(SELECT COUNT(*) FROM operation_submission_result) AS submissions"
    )

    for _ in range(3):
        report = _service(coordinator).recover_expired_executors()
        assert report.count(RecoveryDisposition.RESCHEDULE_REFUSED) == 1
        assert report.recovered[0].refusal_code == "idempotency_conflict"
        assert not report.recovered[0].retryable_refusal
    assert clean_database.fetch_one(
        "SELECT (SELECT COUNT(*) FROM operation_record) AS operations, "
        "(SELECT COUNT(*) FROM operation_submission_result) AS submissions"
    ) == counts


def test_idempotent_lookup_enforces_owner_and_does_not_change_lease_or_retention(clean_database):
    clock, _first, coordinator, accepted, _claim = lost_operation(clean_database)
    record = _record(coordinator, accepted.operation_id)
    identity = {"idempotency_namespace": record.idempotency_namespace, "idempotency_key": record.idempotency_key}
    owner = OperationOwner(record.owner_scope, record.owner_user_id, record.connection_scope_id)
    assert coordinator.repository.get_idempotent_operation(owner=owner, **identity) == record
    assert _record(coordinator, accepted.operation_id) == record
    for foreign in (
        OperationOwner(OwnerScope.USER, "foreign-owner", None),
        OperationOwner(OwnerScope.SCHEDULE, record.owner_user_id, None),
        OperationOwner(OwnerScope.CONNECTION, None, uuid.uuid4()),
    ):
        assert coordinator.repository.get_idempotent_operation(owner=foreign, **identity) is None
    coordinator.expire_execution_leases()
    terminal = _record(coordinator, accepted.operation_id)
    clock.advance(timedelta(days=2))
    assert coordinator.repository.get_idempotent_operation(owner=owner, **identity) == terminal
    assert _service(coordinator).recover_expired_executors().recovered == ()
    assert _record(coordinator, accepted.operation_id) == terminal


def test_parent_purged_during_admission_is_not_reported_as_a_valid_recovery(clean_database, monkeypatch):
    clock, _first, coordinator, accepted, _claim = lost_operation(clean_database)
    submit = coordinator.submit
    children = []

    def retire_parent_after_admission(request):
        admitted = submit(request)
        children.append(admitted.operation_id)
        clock.advance(timedelta(days=2))
        assert coordinator.purge_expired().operations == 1
        return admitted

    with monkeypatch.context() as fault:
        fault.setattr(coordinator, "submit", retire_parent_after_admission)
        report = _service(coordinator).recover_expired_executors()

    assert coordinator.repository.get_operation_for_administration(accepted.operation_id) is None
    assert _record(coordinator, children[0]).parent_operation_id is None
    assert report.count(RecoveryDisposition.RESCHEDULE_REFUSED) == 1
    assert report.recovered[0].refusal_code == "idempotency_conflict"
    assert report.recovered[0].recovery_operation_id is None
