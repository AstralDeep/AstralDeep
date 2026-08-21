"""PostgreSQL repository tests for feature-060 work admission.

The integration cases use a throwaway database.  They never mutate the
configured development database, and they exercise a second coordinator to
prove that accepted work and reconciliation truth are process-independent.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    AdmissionConfigurationError,
    ExecutionFence,
    InMemoryWorkAdmissionRepository,
    OperationNotFoundError,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    PlaneWorkAdmissionRepository,
    StaleExecutionFenceError,
    WorkAdmissionConflictError,
    WorkAdmissionCoordinator,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


def _plane_repository(runtime: PlaneTestRuntime) -> PlaneWorkAdmissionRepository:
    return PlaneWorkAdmissionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def _plane_coordinator_from_existing(
    runtime: PlaneTestRuntime,
    *,
    slot_lease: timedelta = timedelta(seconds=30),
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator.from_plane(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        slot_lease=slot_lease,
    )


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _classes(
    *, active_limit: int = 1, queue_limit: int = 2, max_wait_ms: int | None = 5_000
):
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=active_limit,
            queue_limit=0,
            max_wait_ms=None,
            config_revision="test-060-postgres",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=active_limit,
            queue_limit=queue_limit,
            max_wait_ms=max_wait_ms,
            config_revision="test-060-postgres",
        ),
    )


def _owner(user_id: str = "owner-a") -> OperationOwner:
    return OperationOwner(OwnerScope.USER, user_id, None)


def _request(label: str, *, owner: OperationOwner | None = None) -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=owner or _owner(),
        submission_id=submission_id,
        idempotency_namespace="repository_test",
        idempotency_key=label,
        normalized_input_digest=hashlib.sha256(label.encode()).hexdigest(),
        chat_id=f"chat-{label}",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("work_admission") as runtime:
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


def test_production_construction_requires_an_explicit_durable_dependency() -> None:
    clock = _FakeClock()
    with pytest.raises(ValueError, match="explicit work-admission repository"):
        WorkAdmissionCoordinator(admission_classes=_classes(), clock=clock)

    memory = InMemoryWorkAdmissionRepository()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(), repository=memory, clock=clock
    )
    assert coordinator.submit(_request("explicit-memory")).accepted is True

    with pytest.raises(ValueError, match="in-memory test repository requires"):
        WorkAdmissionCoordinator(
            admission_classes=_classes(),
            repository=InMemoryWorkAdmissionRepository(),
        ).submit(_request("missing-test-clock"))


def test_boundary_values_and_graphs_fail_closed() -> None:
    config_values = {
        "class_name": AdmissionClass.INTERACTIVE,
        "parent_class_name": None,
        "active_limit": 1,
        "queue_limit": 1,
        "max_wait_ms": 1,
        "config_revision": "test",
    }
    for changes in (
        {"active_limit": 0},
        {"queue_limit": -1},
        {"max_wait_ms": None},
        {"queue_limit": 0, "max_wait_ms": -1},
        {"config_revision": ""},
        {"parent_class_name": AdmissionClass.INTERACTIVE},
    ):
        with pytest.raises(AdmissionConfigurationError):
            AdmissionClassConfig(**(config_values | changes))

    for values in (
        (OwnerScope.USER, None, None),
        (OwnerScope.SYSTEM, "payload-owner", None),
        (OwnerScope.CONNECTION, None, None),
        (OwnerScope.SYSTEM, None, "not-a-uuid"),
    ):
        with pytest.raises(ValueError):
            OperationOwner(*values)  # type: ignore[arg-type]

    valid = _request("validation")
    invalid_requests = (
        {"operation_kind": "Not-Snake"},
        {"admission_class": AdmissionClass.GLOBAL},
        {"submission_id": "not-a-uuid"},
        {"parent_operation_id": "not-a-uuid"},
        {"idempotency_key": None},
        {"idempotency_namespace": ""},
        {"idempotency_key": ""},
        {"normalized_input_digest": "A" * 64},
    )
    for changes in invalid_requests:
        with pytest.raises(ValueError):
            dataclasses.replace(valid, **changes)

    for values in (
        ("not-a-uuid", 1, uuid.uuid4()),
        (uuid.uuid4(), 0, uuid.uuid4()),
        (uuid.uuid4(), 1, "not-a-uuid"),
    ):
        with pytest.raises(ValueError):
            ExecutionFence(*values)  # type: ignore[arg-type]

    repository = InMemoryWorkAdmissionRepository()
    with pytest.raises(AdmissionConfigurationError):
        WorkAdmissionCoordinator(
            admission_classes=(), repository=repository, clock=_FakeClock()
        )
    duplicate = _classes()[1]
    with pytest.raises(AdmissionConfigurationError):
        WorkAdmissionCoordinator(
            admission_classes=(duplicate, duplicate),
            repository=repository,
            clock=_FakeClock(),
        )
    with pytest.raises(AdmissionConfigurationError):
        WorkAdmissionCoordinator(
            admission_classes=(
                dataclasses.replace(duplicate, parent_class_name=AdmissionClass.GLOBAL),
            ),
            repository=repository,
            clock=_FakeClock(),
        )
    cycle = (
        dataclasses.replace(duplicate, parent_class_name=AdmissionClass.BACKGROUND),
        AdmissionClassConfig(
            AdmissionClass.BACKGROUND,
            AdmissionClass.INTERACTIVE,
            1,
            1,
            1,
            "test",
        ),
    )
    with pytest.raises(AdmissionConfigurationError):
        WorkAdmissionCoordinator(
            admission_classes=cycle, repository=repository, clock=_FakeClock()
        )

    with pytest.raises(ValueError):
        WorkAdmissionCoordinator(
            admission_classes=_classes(),
            repository=repository,
            clock=_FakeClock(),
            operation_retention=timedelta(0),
        )
    with pytest.raises(ValueError):
        WorkAdmissionCoordinator(
            admission_classes=_classes(),
            repository=repository,
            clock=_FakeClock(),
            slot_lease=timedelta(0),
        )
    with pytest.raises(TypeError):
        PlaneWorkAdmissionRepository(plane_runtime=object())


def test_in_memory_phase_and_lease_seams_are_explicit_and_fenced() -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
    )
    accepted = coordinator.submit(_request("memory-lease"))
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None and claim.operation.operation_id == accepted.operation_id
    with pytest.raises(ValueError):
        coordinator.update_phase(claim.fence, "Not-Safe")
    with pytest.raises(ValueError):
        coordinator.terminalize(
            claim.fence,
            state=OperationState.RUNNING,
            terminal_code=None,
            safe_summary=None,
            retry_after_ms=None,
        )
    with pytest.raises(ValueError):
        coordinator.terminalize(
            claim.fence,
            state=OperationState.FAILED,
            terminal_code=None,
            safe_summary=None,
            retry_after_ms=None,
        )
    with pytest.raises(ValueError):
        coordinator.terminalize(
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="x" * 513,
            retry_after_ms=None,
        )
    with pytest.raises(ValueError):
        coordinator.terminalize(
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary=None,
            retry_after_ms=1,
        )
    with pytest.raises(ValueError):
        coordinator.terminalize(
            claim.fence,
            state=OperationState.RETRYABLE,
            terminal_code="retry_later",
            safe_summary=None,
            retry_after_ms=-1,
        )
    with pytest.raises(ValueError):
        coordinator.purge_expired(limit=0)
    with pytest.raises(ValueError):
        coordinator.query_operation(
            owner=_owner(),
            operation_id="not-a-uuid",  # type: ignore[arg-type]
        )
    phase = coordinator.update_phase(claim.fence, "awaiting_model")
    assert phase.phase_code == "awaiting_model"
    assert coordinator.update_phase(claim.fence, "awaiting_model") == phase
    with coordinator.fenced_transaction(claim.fence) as transaction:
        assert isinstance(transaction, InMemoryWorkAdmissionRepository)
        assert (
            coordinator.assert_current_execution(
                claim.fence, transaction=transaction
            ).operation_id
            == accepted.operation_id
        )
    renewal = coordinator.renew_execution_lease(claim.fence)
    assert renewal.lease_expires_at == clock.current + timedelta(seconds=30)
    clock.advance(timedelta(seconds=30))
    expired = coordinator.expire_execution_leases()
    assert len(expired) == 1
    assert expired[0].terminal_code == "execution_lease_expired"
    with pytest.raises(StaleExecutionFenceError):
        coordinator.update_phase(claim.fence, "late_phase")
    with pytest.raises(StaleExecutionFenceError):
        coordinator.renew_execution_lease(claim.fence)
    with pytest.raises(StaleExecutionFenceError):
        coordinator.terminalize(
            ExecutionFence(uuid.uuid4(), 1, uuid.uuid4()),
            state=OperationState.FAILED,
            terminal_code="operation_failed",
            safe_summary=None,
            retry_after_ms=None,
        )


def test_in_memory_repository_corruption_and_unknown_inputs_fail_closed() -> None:
    naive_clock = _FakeClock(datetime(2026, 7, 15, 12, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        WorkAdmissionCoordinator(
            admission_classes=_classes(),
            repository=InMemoryWorkAdmissionRepository(),
            clock=naive_clock,
        ).submit(_request("naive-clock"))

    clock = _FakeClock()
    repository = InMemoryWorkAdmissionRepository()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(), repository=repository, clock=clock
    )
    with pytest.raises(AdmissionConfigurationError):
        coordinator.claim_next(AdmissionClass.BACKGROUND)
    with pytest.raises(AdmissionConfigurationError):
        coordinator.inspect_admission_class(AdmissionClass.BACKGROUND)
    with pytest.raises(AdmissionConfigurationError):
        coordinator.submit(
            dataclasses.replace(
                _request("unknown-class"),
                admission_class=AdmissionClass.BACKGROUND,
            )
        )

    request = dataclasses.replace(
        _request("memory-no-idempotency"),
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
    )
    accepted = coordinator.submit(request)
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None and claim.operation.operation_id == accepted.operation_id
    wrong_fence = dataclasses.replace(claim.fence, execution_lease_token=uuid.uuid4())
    with pytest.raises(StaleExecutionFenceError):
        coordinator.terminalize(
            wrong_fence,
            state=OperationState.FAILED,
            terminal_code="operation_failed",
            safe_summary=None,
            retry_after_ms=None,
        )
    with pytest.raises(StaleExecutionFenceError):
        coordinator.reselect_execution(wrong_fence)

    for slots in repository._slots.values():  # noqa: SLF001 - corruption test
        slots[:] = [
            dataclasses.replace(
                slot,
                operation_id=None,
                lease_token=None,
                lease_expires_at=None,
            )
            if slot.operation_id == accepted.operation_id
            else slot
            for slot in slots
        ]
    with pytest.raises(StaleExecutionFenceError, match="capacity lease is missing"):
        coordinator.renew_execution_lease(claim.fence)
    assert (
        repository._claim_free_slots_locked(  # noqa: SLF001 - invariant test
            AdmissionClass.INTERACTIVE,
            uuid.uuid4(),
            lease_token=None,
            lease_expires_at=clock.current + timedelta(seconds=30),
        )
        is True
    )
    assert (
        repository._claim_free_slots_locked(  # noqa: SLF001 - invariant test
            AdmissionClass.INTERACTIVE,
            uuid.uuid4(),
            lease_token=None,
            lease_expires_at=clock.current + timedelta(seconds=30),
        )
        is False
    )

    missing_repository = InMemoryWorkAdmissionRepository()
    missing_coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(), repository=missing_repository, clock=clock
    )
    missing_request = _request("missing-reconciliation-operation")
    missing = missing_coordinator.submit(missing_request)
    missing_repository._operations.pop(missing.operation_id)  # noqa: SLF001
    with pytest.raises(OperationNotFoundError):
        missing_coordinator.reconcile_submission(
            owner=missing_request.owner,
            submission_id=missing_request.submission_id,
        )
    with pytest.raises(OperationNotFoundError):
        missing_coordinator.submit(missing_request)


def test_postgres_truth_survives_coordinator_reconstruction_and_fences_commits(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    first = WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=_plane_repository(clean_database),
        clock=clock,
    )
    request = _request("durable")
    acceptance = first.submit(request)
    assert acceptance.accepted is True

    second = WorkAdmissionCoordinator(
        admission_classes=tuple(reversed(_classes())),
        repository=_plane_repository(clean_database),
        clock=clock,
    )
    assert second.submit(request) == acceptance
    transport_retry = dataclasses.replace(request, submission_id=uuid.uuid4())
    assert second.submit(transport_retry).operation_id == acceptance.operation_id
    conflict = second.submit(
        dataclasses.replace(
            request,
            submission_id=uuid.uuid4(),
            normalized_input_digest="f" * 64,
        )
    )
    assert conflict.accepted is False
    assert conflict.code == "idempotency_conflict"
    assert (
        second.reconcile_submission(
            owner=request.owner, submission_id=request.submission_id
        ).operation.operation_id
        == acceptance.operation_id
    )
    with pytest.raises(OperationNotFoundError):
        second.query_operation(
            owner=_owner("owner-b"), operation_id=acceptance.operation_id
        )

    claim = second.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    phase = second.update_phase(claim.fence, "committing_effect")
    assert phase.phase_code == "committing_effect"
    assert second.update_phase(claim.fence, "committing_effect") == phase
    renewal = second.renew_execution_lease(claim.fence)
    assert renewal.execution_generation == 1
    assert renewal.lease_expires_at == clock.current + timedelta(seconds=30)

    with pytest.raises(RuntimeError, match="rollback-fenced-effect"):
        with second.fenced_transaction(claim.fence) as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admission_test_effect (
                    effect_id UUID PRIMARY KEY, operation_id UUID NOT NULL
                )
                """
            )
            cursor.execute(
                "INSERT INTO admission_test_effect (effect_id, operation_id) "
                "VALUES (%s, %s)",
                (str(uuid.uuid4()), str(acceptance.operation_id)),
            )
            raise RuntimeError("rollback-fenced-effect")

    with second.fenced_transaction(claim.fence) as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admission_test_effect (
                effect_id UUID PRIMARY KEY, operation_id UUID NOT NULL
            )
            """
        )
        assert (
            second.assert_current_execution(
                claim.fence, transaction=cursor
            ).operation_id
            == acceptance.operation_id
        )
        cursor.execute(
            "INSERT INTO admission_test_effect (effect_id, operation_id) VALUES (%s, %s)",
            (str(uuid.uuid4()), str(acceptance.operation_id)),
        )
        cursor.execute(
            "UPDATE operation_record SET execution_generation = %s "
            "WHERE operation_id = %s",
            (2**40, str(acceptance.operation_id)),
        )

    large_fence = dataclasses.replace(claim.fence, execution_generation=2**40)
    assert second.assert_current_execution(large_fence).execution_generation == 2**40
    replacement = second.reselect_execution(large_fence)
    assert replacement.execution_generation == 2**40 + 1
    with pytest.raises(StaleExecutionFenceError):
        with second.fenced_transaction(large_fence):
            pass

    terminal = second.terminalize(
        replacement,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary="Unable to complete",
        retry_after_ms=None,
    )
    assert terminal.state is OperationState.FAILED
    assert (
        second.terminalize(
            large_fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Late success",
            retry_after_ms=None,
        )
        == terminal
    )
    assert second.submit(request).state is OperationState.FAILED
    assert (
        clean_database.fetch_one("SELECT COUNT(*) AS count FROM admission_test_effect")[
            "count"
        ]
        == 1
    )


def test_postgres_request_generation_binding_reuses_and_rolls_back_caller_tx(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    repository = _plane_repository(clean_database)
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=repository,
        clock=clock,
    )
    request = dataclasses.replace(
        _request("request-generation-transaction"),
        request_generation=None,
    )
    accepted = coordinator.submit(request)
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    request_generation = uuid.uuid4()

    with pytest.raises(RuntimeError, match="rollback-request-generation"):
        with coordinator.fenced_transaction(claim.fence) as transaction:
            administrative = repository.get_operation_for_administration(
                accepted.operation_id,
                for_update=True,
                transaction=transaction,
            )
            assert administrative is not None
            assert administrative.owner_user_id == "owner-a"
            assert administrative.request_generation is None
            repository.bind_request_generation(
                claim.fence,
                request_generation,
                transaction=transaction,
            )
            raise RuntimeError("rollback-request-generation")

    after_rollback = repository.get_operation_for_administration(
        accepted.operation_id
    )
    assert after_rollback is not None
    assert after_rollback.request_generation is None

    with coordinator.fenced_transaction(claim.fence) as transaction:
        bound = repository.bind_request_generation(
            claim.fence,
            request_generation,
            transaction=transaction,
        )
        replay = repository.bind_request_generation(
            claim.fence,
            request_generation,
            transaction=transaction,
        )
    assert bound.request_generation == request_generation
    assert replay == bound

    with pytest.raises(WorkAdmissionConflictError, match="different request"):
        repository.bind_request_generation(claim.fence, uuid.uuid4())


def test_cold_pool_and_zero_queue_preselect_once_then_refuse_at_capacity(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(queue_limit=0, max_wait_ms=None),
        repository=_plane_repository(clean_database),
        clock=clock,
    )
    first = coordinator.submit(_request("zero-queue-first"))
    assert first.accepted is True
    assert first.state is OperationState.RUNNING
    assert first.queue_position is None
    assert first.queue_deadline_at is None

    refused = coordinator.submit(_request("zero-queue-refused"))
    assert refused.accepted is False
    assert refused.code == "capacity_exceeded"
    status = coordinator.inspect_admission_class(AdmissionClass.INTERACTIVE)
    assert status.active_count == 1
    assert status.queued_count == 0

    handoff = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert handoff is not None
    assert handoff.operation.operation_id == first.operation_id
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None
    coordinator.terminalize(
        handoff.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    replacement = coordinator.submit(_request("zero-queue-replacement"))
    assert replacement.accepted is True
    assert replacement.state is OperationState.RUNNING


def test_pre_handoff_cancel_and_expiry_revoke_effect_authority(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(queue_limit=0, max_wait_ms=None),
        repository=_plane_repository(clean_database),
        clock=clock,
    )
    cancel_request = _request("cancel-before-handoff")
    selected = coordinator.submit(cancel_request)
    internal = clean_database.fetch_one(
        "SELECT execution_generation, execution_lease_token FROM operation_record "
        "WHERE operation_id = ?",
        (str(selected.operation_id),),
    )
    cancel_fence = ExecutionFence(
        selected.operation_id,
        int(internal["execution_generation"]),
        uuid.UUID(str(internal["execution_lease_token"])),
    )
    cancelled = coordinator.cancel(
        owner=cancel_request.owner,
        operation_id=selected.operation_id,
        terminal_code="cancelled_by_user",
    )
    assert cancelled.state is OperationState.CANCELLED
    assert cancelled.execution_lease_token is None
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None
    assert coordinator.inspect_admission_class(AdmissionClass.GLOBAL).active_count == 0
    with pytest.raises(StaleExecutionFenceError):
        with coordinator.fenced_transaction(cancel_fence):
            pass

    expiry_request = _request("expire-before-handoff")
    expiring = coordinator.submit(expiry_request)
    internal = clean_database.fetch_one(
        "SELECT execution_generation, execution_lease_token FROM operation_record "
        "WHERE operation_id = ?",
        (str(expiring.operation_id),),
    )
    expiry_fence = ExecutionFence(
        expiring.operation_id,
        int(internal["execution_generation"]),
        uuid.UUID(str(internal["execution_lease_token"])),
    )
    clock.advance(timedelta(seconds=30))
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None
    visible = coordinator.query_operation(
        owner=expiry_request.owner, operation_id=expiring.operation_id
    )
    assert visible.state is OperationState.RETRYABLE
    assert visible.terminal_code == "execution_lease_expired"
    assert coordinator.inspect_admission_class(AdmissionClass.GLOBAL).active_count == 0
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution(expiry_fence)


def test_incomplete_parent_child_handoff_marker_fails_closed_and_recovers(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(queue_limit=0, max_wait_ms=None),
        repository=_plane_repository(clean_database),
        clock=clock,
    )
    request = _request("incomplete-handoff")
    selected = coordinator.submit(request)
    clean_database.execute(
        """
        UPDATE operation_admission_slot
        SET operation_id = NULL, lease_token = NULL, lease_expires_at = NULL
        WHERE class_name = ? AND operation_id = ?
        """,
        (AdmissionClass.GLOBAL.value, str(selected.operation_id)),
    )

    with pytest.raises(RuntimeError, match="handoff marker is incomplete"):
        coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert (
        coordinator.query_operation(
            owner=request.owner, operation_id=selected.operation_id
        ).state
        is OperationState.RUNNING
    )
    clock.advance(timedelta(seconds=30))
    expired = coordinator.expire_execution_leases()
    assert len(expired) == 1
    assert expired[0].operation_id == selected.operation_id
    assert expired[0].state is OperationState.RETRYABLE
    assert coordinator.inspect_admission_class(AdmissionClass.GLOBAL).active_count == 0


def test_postgres_queue_and_slot_limits_hold_across_coordinators(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinators = tuple(
        WorkAdmissionCoordinator(
            admission_classes=_classes(active_limit=1, queue_limit=2),
        repository=_plane_repository(clean_database),
            clock=clock,
        )
        for _ in range(2)
    )
    requests = (_request("parallel-a"), _request("parallel-b"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        acceptances = tuple(
            executor.map(
                lambda pair: pair[0].submit(pair[1]), zip(coordinators, requests)
            )
        )
    assert all(result.accepted for result in acceptances)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda coordinator: coordinator.claim_next(AdmissionClass.INTERACTIVE),
                coordinators,
            )
        )
    assert sum(claim is not None for claim in claims) == 1
    status = coordinators[0].inspect_admission_class(AdmissionClass.GLOBAL)
    assert status.active_count == status.active_limit == 1

    refused = coordinators[0].submit(_request("queue-full"))
    assert refused.accepted is True
    immutable_refusal_request = _request("immutable-refusal")
    immutable_refusal = coordinators[1].submit(immutable_refusal_request)
    assert immutable_refusal.accepted is False
    running_claim = next(claim for claim in claims if claim is not None)
    coordinators[0].terminalize(
        running_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    assert coordinators[1].submit(immutable_refusal_request) == immutable_refusal


def test_postgres_exact_handoff_is_targeted_and_one_time_across_coordinators(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinators = tuple(
        WorkAdmissionCoordinator(
            admission_classes=_classes(active_limit=2, queue_limit=2),
        repository=_plane_repository(clean_database),
            clock=clock,
        )
        for _ in range(2)
    )
    older = coordinators[0].submit(_request("postgres-exact-older"))
    newer = coordinators[0].submit(_request("postgres-exact-newer"))
    assert older.accepted and newer.accepted

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda coordinator: coordinator.claim_operation(
                    AdmissionClass.INTERACTIVE, newer.operation_id
                ),
                coordinators,
            )
        )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].operation.operation_id == newer.operation_id

    older_claim = coordinators[1].claim_next(AdmissionClass.INTERACTIVE)
    assert older_claim is not None
    assert older_claim.operation.operation_id == older.operation_id
    assert (
        coordinators[0].claim_operation(
            AdmissionClass.INTERACTIVE, newer.operation_id
        )
        is None
    )


def test_postgres_exact_queued_claim_cannot_bypass_fifo_or_capacity(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(active_limit=1, queue_limit=3),
        repository=_plane_repository(clean_database),
        clock=clock,
    )
    blocker = coordinator.submit(_request("postgres-exact-blocker"))
    blocker_claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, blocker.operation_id
    )
    assert blocker_claim is not None
    older = coordinator.submit(_request("postgres-exact-queued-older"))
    clock.advance(timedelta(microseconds=1))
    newer = coordinator.submit(_request("postgres-exact-queued-newer"))

    assert (
        coordinator.claim_operation(AdmissionClass.INTERACTIVE, newer.operation_id)
        is None
    )
    coordinator.terminalize(
        blocker_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    assert (
        coordinator.claim_operation(AdmissionClass.INTERACTIVE, newer.operation_id)
        is None
    )
    older_claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, older.operation_id
    )
    assert older_claim is not None
    assert coordinator.inspect_admission_class(
        AdmissionClass.INTERACTIVE
    ).active_count == 1
    coordinator.terminalize(
        older_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    newer_claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, newer.operation_id
    )
    assert newer_claim is not None
    assert newer_claim.operation.operation_id == newer.operation_id


def test_postgres_terminalize_unselected_is_atomic_and_matches_memory(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinators = tuple(
        WorkAdmissionCoordinator(
            admission_classes=_classes(active_limit=1, queue_limit=2),
        repository=_plane_repository(clean_database),
            clock=clock,
            operation_retention=timedelta(hours=24),
        )
        for _ in range(2)
    )
    preselected = coordinators[0].submit(_request("postgres-unselected-running"))
    queued = coordinators[0].submit(_request("postgres-unselected-queued"))

    queued_terminal = coordinators[1].terminalize_unselected(
        queued.operation_id,
        terminal_code="claim_lost",
        safe_summary="Scheduled claim lost before start",
        retry_after_ms=0,
    )
    assert queued_terminal is not None
    assert queued_terminal.state is OperationState.RETRYABLE
    assert queued_terminal.terminal_code == "claim_lost"
    assert queued_terminal.safe_summary == "Scheduled claim lost before start"
    assert queued_terminal.execution_generation == 0
    assert queued_terminal.execution_lease_token is None
    assert queued_terminal.terminal_at == clock.current
    assert queued_terminal.purge_after == clock.current + timedelta(hours=24)

    def settle_preselection(coordinator):
        return coordinator.terminalize_unselected(
            preselected.operation_id,
            terminal_code="claim_lost",
            safe_summary="Scheduled claim lost before handoff",
            retry_after_ms=0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminals = tuple(executor.map(settle_preselection, coordinators))

    assert terminals[0] is not None
    assert terminals[0] == terminals[1]
    assert terminals[0].state is OperationState.RETRYABLE
    assert terminals[0].terminal_code == "claim_lost"
    assert terminals[0].execution_generation == 1
    assert terminals[0].execution_lease_token is None
    assert (
        coordinators[0]
        .inspect_admission_class(AdmissionClass.INTERACTIVE)
        .active_count
        == 0
    )

    handed_off = coordinators[0].submit(_request("postgres-unselected-handoff"))
    claim = coordinators[0].claim_operation(
        AdmissionClass.INTERACTIVE, handed_off.operation_id
    )
    assert claim is not None
    before = coordinators[0].assert_current_execution(claim.fence)
    assert (
        coordinators[1].terminalize_unselected(
            handed_off.operation_id,
            terminal_code="claim_lost",
            safe_summary="Stale scheduler recovery",
            retry_after_ms=0,
        )
        is None
    )
    assert coordinators[0].assert_current_execution(claim.fence) == before

    replacement = coordinators[0].reselect_execution(claim.fence)
    replacement_before = coordinators[0].assert_current_execution(replacement)
    assert (
        coordinators[1].terminalize_unselected(
            handed_off.operation_id,
            terminal_code="claim_lost",
            safe_summary="Stale scheduler recovery",
            retry_after_ms=0,
        )
        is None
    )
    assert (
        coordinators[0].assert_current_execution(replacement) == replacement_before
    )

    completed = coordinators[0].terminalize(
        replacement,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    clock.advance(timedelta(seconds=1))
    assert (
        coordinators[1].terminalize_unselected(
            handed_off.operation_id,
            terminal_code="claim_lost",
            safe_summary="Must not replace completion",
            retry_after_ms=0,
        )
        == completed
    )
    assert (
        coordinators[1].terminalize_unselected(
            uuid.uuid4(),
            terminal_code="claim_lost",
            safe_summary="Missing operation",
            retry_after_ms=0,
        )
        is None
    )


def test_postgres_cancellation_queue_expiry_and_owner_partitions(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(active_limit=1, queue_limit=3),
        repository=_plane_repository(clean_database),
        clock=clock,
    )
    running_request = _request("cancel-running")
    running = coordinator.submit(running_request)
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None and claim.operation.operation_id == running.operation_id

    queued_request = _request("cancel-queued")
    queued = coordinator.submit(queued_request)
    cancelled = coordinator.cancel(
        owner=queued_request.owner,
        operation_id=queued.operation_id,
        terminal_code="cancelled_by_user",
    )
    assert cancelled.state is OperationState.CANCELLED
    assert (
        coordinator.cancel(
            owner=queued_request.owner,
            operation_id=queued.operation_id,
            terminal_code="cancelled_by_user",
        )
        == cancelled
    )
    with pytest.raises(OperationNotFoundError):
        coordinator.cancel(
            owner=_owner("owner-b"),
            operation_id=running.operation_id,
            terminal_code="cancelled_by_user",
        )

    cancellation_requested = coordinator.cancel(
        owner=running_request.owner,
        operation_id=running.operation_id,
        terminal_code="cancelled_by_user",
    )
    assert cancellation_requested.state is OperationState.RUNNING
    assert cancellation_requested.cancel_requested_at == clock.current
    assert (
        coordinator.cancel(
            owner=running_request.owner,
            operation_id=running.operation_id,
            terminal_code="cancelled_by_user",
        )
        == cancellation_requested
    )
    coordinator.terminalize(
        claim.fence,
        state=OperationState.CANCELLED,
        terminal_code="cancelled_by_user",
        safe_summary="Cancelled",
        retry_after_ms=None,
    )

    blocker = coordinator.submit(_request("queue-expiry-blocker"))
    blocker_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert blocker_claim is not None
    assert blocker_claim.operation.operation_id == blocker.operation_id
    expiring_request = _request("queue-expiry")
    expiring = coordinator.submit(expiring_request)
    clock.advance(timedelta(seconds=5))
    expired = coordinator.expire_queued()
    assert len(expired) == 1
    assert expired[0].operation_id == expiring.operation_id
    assert expired[0].terminal_code == "queue_wait_expired"
    assert (
        coordinator.reconcile_submission(
            owner=expiring_request.owner,
            submission_id=expiring_request.submission_id,
        ).operation.state
        is OperationState.RETRYABLE
    )

    no_idempotency = dataclasses.replace(
        _request("no-idempotency"),
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
    )
    assert coordinator.submit(no_idempotency).accepted is True
    with pytest.raises(AdmissionConfigurationError):
        coordinator.claim_next(AdmissionClass.BACKGROUND)

    connection_owner = OperationOwner(OwnerScope.CONNECTION, None, uuid.uuid4())
    connection_request = dataclasses.replace(
        _request("connection-owner", owner=connection_owner),
        idempotency_namespace=None,
        idempotency_key=None,
        normalized_input_digest=None,
    )
    assert coordinator.submit(connection_request).accepted is True
    assert (
        coordinator.query_operation(
            owner=connection_owner,
            operation_id=coordinator.submit(connection_request).operation_id,
        ).owner_scope
        is OwnerScope.CONNECTION
    )
    system_owner = OperationOwner(OwnerScope.SYSTEM, None, None)
    system_request = _request("system-owner", owner=system_owner)
    system_acceptance = coordinator.submit(system_request)
    assert system_acceptance.accepted is True
    assert (
        coordinator.query_operation(
            owner=system_owner, operation_id=system_acceptance.operation_id
        ).owner_scope
        is OwnerScope.SYSTEM
    )


def test_postgres_lease_recovery_and_strict_retention_boundary(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(active_limit=1, queue_limit=1, max_wait_ms=60_000),
        repository=_plane_repository(clean_database),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )
    running_request = _request("lease-expiry")
    running = coordinator.submit(running_request)
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    refusal_request = _request("retained-refusal")
    refusal = coordinator.submit(refusal_request)
    assert refusal.accepted is True
    refused_request = _request("actual-refusal")
    assert coordinator.submit(refused_request).accepted is False

    clock.advance(timedelta(seconds=30))
    expired = coordinator.expire_execution_leases()
    assert len(expired) == 1
    assert expired[0].operation_id == running.operation_id
    assert expired[0].state is OperationState.RETRYABLE
    assert expired[0].terminal_code == "execution_lease_expired"
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is not None

    clock.advance(timedelta(hours=24) - timedelta(seconds=30))
    assert coordinator.oldest_purge_eligible_due_at() is None
    assert coordinator.purge_expired(limit=100).operations == 0
    clock.advance(timedelta(hours=1))
    oldest_due = coordinator.oldest_purge_eligible_due_at()
    assert oldest_due is not None and oldest_due < clock.current
    purged = coordinator.purge_expired(limit=100)
    assert purged.operations == 1
    assert purged.submissions == 2


def test_postgres_default_clock_is_database_owned(
    clean_database: PlaneTestRuntime,
) -> None:
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=_plane_repository(clean_database),
    )
    before = clean_database.fetch_one("SELECT CURRENT_TIMESTAMP AS current_time")[
        "current_time"
    ]
    request = _request("database-time")
    acceptance = coordinator.submit(request)
    visible = coordinator.query_operation(
        owner=request.owner, operation_id=acceptance.operation_id
    )
    after = clean_database.fetch_one("SELECT CURRENT_TIMESTAMP AS current_time")[
        "current_time"
    ]
    assert before <= visible.accepted_at <= after
    assert visible.accepted_at.tzinfo is not None
