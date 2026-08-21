"""Feature-060 contract tests for durable work admission and execution fences.

These tests use a manually advanced UTC clock.  They intentionally contain no
wall-clock sleeps: queue expiry, reconciliation retention, and purge behavior
must be deterministic state transitions rather than timing races.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    ExecutionFence,
    InMemoryWorkAdmissionRepository,
    OperationNotFoundError,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleExecutionFenceError,
    WorkAdmissionConflictError,
    WorkAdmissionCoordinator,
)


_SAFE_OPERATION_FIELDS = {
    "operation_id",
    "operation_kind",
    "admission_class",
    "owner_scope",
    "chat_id",
    "parent_operation_id",
    "connection_generation",
    "request_generation",
    "state",
    "phase_code",
    "terminal_code",
    "safe_summary",
    "retry_after_ms",
    "state_revision",
    "accepted_at",
    "queue_deadline_at",
    "started_at",
    "terminal_at",
    "updated_at",
    "purge_after",
}


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _owner(user_id: str) -> OperationOwner:
    return OperationOwner(
        owner_scope=OwnerScope.USER,
        owner_user_id=user_id,
        connection_scope_id=None,
    )


def _request(
    label: str,
    *,
    owner: OperationOwner | None = None,
    admission_class: AdmissionClass = AdmissionClass.INTERACTIVE,
    submission_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    digest_label: str | None = None,
) -> OperationRequest:
    submission_id = submission_id or uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=admission_class,
        owner=owner or _owner("owner-a"),
        submission_id=submission_id,
        idempotency_namespace="ui_submission",
        idempotency_key=idempotency_key or str(submission_id),
        normalized_input_digest=hashlib.sha256(
            (digest_label or label).encode("utf-8")
        ).hexdigest(),
        chat_id=f"chat-{label}",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _coordinator(
    clock: _FakeClock,
    *,
    active_limit: int = 1,
    queue_limit: int = 2,
    max_wait_ms: int = 5_000,
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=queue_limit,
                max_wait_ms=max_wait_ms,
                config_revision="test-060",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )


def _terminalize_completed(coordinator, fence):
    return coordinator.terminalize(
        fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )


def test_acceptance_preselects_free_capacity_with_a_durable_execution() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    request = _request("first")

    accepted = coordinator.submit(request)

    assert accepted.accepted is True
    assert isinstance(accepted.operation_id, uuid.UUID)
    assert accepted.operation_id.version == 4
    assert accepted.state is OperationState.RUNNING
    assert accepted.state_revision == 1
    assert accepted.queue_position is None
    assert accepted.queue_deadline_at is None

    visible = coordinator.query_operation(
        owner=request.owner,
        operation_id=accepted.operation_id,
    )
    assert set(dataclasses.asdict(visible)) == _SAFE_OPERATION_FIELDS
    assert visible.operation_id == accepted.operation_id
    assert visible.operation_kind == request.operation_kind
    assert visible.owner_scope is OwnerScope.USER
    assert visible.state is OperationState.RUNNING
    assert visible.accepted_at == clock.current
    assert visible.updated_at == clock.current
    assert visible.started_at == clock.current
    assert visible.terminal_at is None
    assert visible.purge_after is None

    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    assert claim.operation.operation_id == accepted.operation_id
    assert claim.operation.state is OperationState.RUNNING
    assert claim.operation.state_revision == 1
    assert claim.operation.execution_generation == 1
    assert claim.fence.operation_id == accepted.operation_id
    assert claim.fence.execution_generation == 1
    assert claim.fence.execution_lease_token.version == 4


def test_in_memory_admin_read_and_request_generation_binding_match_plane() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    request = dataclasses.replace(_request("request-bind"), request_generation=None)
    accepted = coordinator.submit(request)
    assert accepted.accepted
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    repository = coordinator.repository

    missing = repository.get_operation_for_administration(uuid.uuid4())
    assert missing is None
    administrative = repository.get_operation_for_administration(
        accepted.operation_id,
        for_update=True,
    )
    assert administrative is not None
    assert administrative.owner_user_id == "owner-a"
    assert administrative.request_generation is None

    request_generation = uuid.uuid4()
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
        with pytest.raises(WorkAdmissionConflictError, match="different request"):
            repository.bind_request_generation(
                claim.fence,
                uuid.uuid4(),
                transaction=transaction,
            )

    assert bound.request_generation == request_generation
    assert bound.state_revision == administrative.state_revision + 1
    assert replay == bound


def test_exact_handoff_never_consumes_an_older_preselection() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=2, queue_limit=2)
    older = coordinator.submit(_request("exact-older"))
    clock.advance(timedelta(microseconds=1))
    newer = coordinator.submit(_request("exact-newer"))
    assert older.accepted and newer.accepted

    newer_claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, newer.operation_id
    )
    assert newer_claim is not None
    assert newer_claim.operation.operation_id == newer.operation_id
    assert (
        coordinator.claim_operation(AdmissionClass.INTERACTIVE, newer.operation_id)
        is None
    )

    older_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert older_claim is not None
    assert older_claim.operation.operation_id == older.operation_id


def test_exact_queued_claim_preserves_fifo_and_capacity() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=1, queue_limit=3)
    blocker = coordinator.submit(_request("exact-blocker"))
    blocker_claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, blocker.operation_id
    )
    assert blocker_claim is not None
    older = coordinator.submit(_request("exact-queued-older"))
    clock.advance(timedelta(microseconds=1))
    newer = coordinator.submit(_request("exact-queued-newer"))

    assert (
        coordinator.claim_operation(AdmissionClass.INTERACTIVE, newer.operation_id)
        is None
    )
    _terminalize_completed(coordinator, blocker_claim.fence)
    assert (
        coordinator.claim_operation(AdmissionClass.INTERACTIVE, newer.operation_id)
        is None
    )
    older_claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, older.operation_id
    )
    assert older_claim is not None
    _terminalize_completed(coordinator, older_claim.fence)
    newer_claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, newer.operation_id
    )
    assert newer_claim is not None


def test_terminalize_unselected_settles_only_queued_or_preselected_work() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=1, queue_limit=2)
    preselected_request = _request("unselected-preselected")
    preselected = coordinator.submit(preselected_request)
    queued_request = _request("unselected-queued")
    queued = coordinator.submit(queued_request)

    queued_terminal = coordinator.terminalize_unselected(
        queued.operation_id,
        terminal_code="claim_lost",
        safe_summary="Scheduled claim lost before start",
        retry_after_ms=0,
    )

    assert queued_terminal is not None
    assert queued_terminal.operation_id == queued.operation_id
    assert queued_terminal.state is OperationState.RETRYABLE
    assert queued_terminal.terminal_code == "claim_lost"
    assert queued_terminal.safe_summary == "Scheduled claim lost before start"
    assert queued_terminal.retry_after_ms == 0
    assert queued_terminal.execution_generation == 0
    assert queued_terminal.execution_lease_token is None
    assert queued_terminal.started_at is None
    assert queued_terminal.terminal_at == clock.current
    assert queued_terminal.purge_after == clock.current + timedelta(hours=24)
    status = coordinator.inspect_admission_class(AdmissionClass.INTERACTIVE)
    assert status.active_count == 1
    assert status.queued_count == 0

    preselected_terminal = coordinator.terminalize_unselected(
        preselected.operation_id,
        terminal_code="claim_lost",
        safe_summary="Scheduled claim lost before handoff",
        retry_after_ms=0,
    )

    assert preselected_terminal is not None
    assert preselected_terminal.operation_id == preselected.operation_id
    assert preselected_terminal.state is OperationState.RETRYABLE
    assert preselected_terminal.terminal_code == "claim_lost"
    assert preselected_terminal.execution_generation == 1
    assert preselected_terminal.execution_lease_token is None
    assert preselected_terminal.terminal_at == clock.current
    assert preselected_terminal.purge_after == clock.current + timedelta(hours=24)
    assert (
        coordinator.inspect_admission_class(AdmissionClass.INTERACTIVE).active_count
        == 0
    )

    # The first terminal result is immutable even when recovery is replayed.
    clock.advance(timedelta(seconds=1))
    assert (
        coordinator.terminalize_unselected(
            preselected.operation_id,
            terminal_code="different_recovery",
            safe_summary="Must not replace the first terminal result",
            retry_after_ms=5_000,
        )
        == preselected_terminal
    )
    assert (
        coordinator.terminalize_unselected(
            uuid.uuid4(),
            terminal_code="claim_lost",
            safe_summary="Missing operation",
            retry_after_ms=0,
        )
        is None
    )


def test_terminalize_unselected_never_revokes_a_handed_off_execution() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted = coordinator.submit(_request("unselected-handoff"))
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, accepted.operation_id
    )
    assert claim is not None
    before = coordinator.assert_current_execution(claim.fence)

    assert (
        coordinator.terminalize_unselected(
            accepted.operation_id,
            terminal_code="claim_lost",
            safe_summary="Stale scheduler recovery",
            retry_after_ms=0,
        )
        is None
    )
    assert coordinator.assert_current_execution(claim.fence) == before

    replacement = coordinator.reselect_execution(claim.fence)
    replacement_before = coordinator.assert_current_execution(replacement)
    assert (
        coordinator.terminalize_unselected(
            accepted.operation_id,
            terminal_code="claim_lost",
            safe_summary="Stale scheduler recovery",
            retry_after_ms=0,
        )
        is None
    )
    assert coordinator.assert_current_execution(replacement) == replacement_before


def test_terminalize_unselected_validates_public_fields() -> None:
    coordinator = _coordinator(_FakeClock())
    operation_id = uuid.uuid4()

    invalid_calls = (
        {"operation_id": "not-a-uuid"},
        {"terminal_code": "Not-Safe"},
        {"safe_summary": "x" * 513},
        {"retry_after_ms": -1},
    )
    defaults = {
        "operation_id": operation_id,
        "terminal_code": "claim_lost",
        "safe_summary": "Scheduled claim lost",
        "retry_after_ms": 0,
    }
    for changes in invalid_calls:
        with pytest.raises(ValueError):
            coordinator.terminalize_unselected(**(defaults | changes))


def test_finite_queue_is_fifo_and_full_refusal_is_immutable() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=1, queue_limit=2)
    first_request = _request("first")
    first = coordinator.submit(first_request)
    first_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert first_claim is not None
    assert first_claim.operation.operation_id == first.operation_id

    clock.advance(timedelta(milliseconds=1))
    second_request = _request("second")
    second = coordinator.submit(second_request)
    clock.advance(timedelta(milliseconds=1))
    third_request = _request("third")
    third = coordinator.submit(third_request)
    refused_request = _request("fourth")
    refused = coordinator.submit(refused_request)

    assert second.accepted is True and second.queue_position == 1
    assert third.accepted is True and third.queue_position == 2
    assert refused.accepted is False
    assert refused.code == "capacity_exceeded"
    assert refused.retryable is True
    assert refused.retry_after_ms is not None
    assert getattr(refused, "operation_id", None) is None
    status = coordinator.inspect_admission_class(AdmissionClass.INTERACTIVE)
    assert status.active_count == 1
    assert status.queued_count == 2
    assert status.active_count <= status.active_limit == 1
    assert status.queued_count <= status.queue_limit == 2

    _terminalize_completed(coordinator, first_claim.fence)
    second_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert second_claim is not None
    assert second_claim.operation.operation_id == second.operation_id
    _terminalize_completed(coordinator, second_claim.fence)
    third_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert third_claim is not None
    assert third_claim.operation.operation_id == third.operation_id

    # A retained submission refusal cannot silently become accepted just
    # because capacity later changes.
    assert coordinator.submit(refused_request) == refused
    reconciled = coordinator.reconcile_submission(
        owner=refused_request.owner,
        submission_id=refused_request.submission_id,
    )
    assert reconciled == refused


def test_ordinary_interactive_class_keeps_existing_per_user_capacity_behavior() -> None:
    """Feature 065's voice limit must not narrow ordinary typed chat."""

    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=3, queue_limit=1)

    running = tuple(
        coordinator.submit(_request(f"typed-{index}")) for index in range(3)
    )
    queued = coordinator.submit(_request("typed-queued"))

    assert all(result.accepted for result in running)
    assert all(result.state is OperationState.RUNNING for result in running)
    assert queued.accepted is True
    assert queued.state is OperationState.QUEUED
    assert queued.queue_position == 1


def test_idempotency_reuses_original_operation_and_conflicts_on_new_input() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=2, queue_limit=8)
    stable_key = "chat-submit-42"
    original = _request("original", idempotency_key=stable_key)

    accepted = coordinator.submit(original)
    exact_retry = coordinator.submit(original)
    transport_retry = dataclasses.replace(original, submission_id=uuid.uuid4())
    replayed = coordinator.submit(transport_retry)

    assert exact_retry == accepted
    assert replayed.accepted is True
    assert replayed.operation_id == accepted.operation_id
    status = coordinator.inspect_admission_class(AdmissionClass.INTERACTIVE)
    assert status.active_count == 1
    assert status.queued_count == 0
    assert (
        coordinator.reconcile_submission(
            owner=transport_retry.owner,
            submission_id=transport_retry.submission_id,
        ).operation.operation_id
        == accepted.operation_id
    )

    conflicting = dataclasses.replace(
        original,
        submission_id=uuid.uuid4(),
        normalized_input_digest="b" * 64,
    )
    conflict = coordinator.submit(conflicting)
    assert conflict.accepted is False
    assert conflict.code == "idempotency_conflict"
    assert conflict.retryable is False
    assert getattr(conflict, "operation_id", None) is None

    # Idempotency is partitioned by authenticated owner, not globally by key.
    other_owner = dataclasses.replace(
        original,
        owner=_owner("owner-b"),
        submission_id=uuid.uuid4(),
    )
    other_result = coordinator.submit(other_owner)
    assert other_result.accepted is True
    assert other_result.operation_id != accepted.operation_id


def test_queries_are_owner_scoped_safe_and_non_disclosing() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, queue_limit=1)
    accepted_request = _request("accepted")
    accepted = coordinator.submit(accepted_request)
    queued_request = _request("queued")
    queued = coordinator.submit(queued_request)
    assert queued.accepted is True
    assert queued.state is OperationState.QUEUED
    refused_request = _request("refused")
    refused = coordinator.submit(refused_request)
    assert refused.accepted is False

    accepted_reconciliation = coordinator.reconcile_submission(
        owner=accepted_request.owner,
        submission_id=accepted_request.submission_id,
    )
    assert accepted_reconciliation.accepted is True
    assert set(dataclasses.asdict(accepted_reconciliation.operation)) == (
        _SAFE_OPERATION_FIELDS
    )
    assert not hasattr(accepted_reconciliation.operation, "owner_user_id")
    assert not hasattr(accepted_reconciliation.operation, "idempotency_key")
    assert not hasattr(accepted_reconciliation.operation, "normalized_input_digest")
    assert not hasattr(accepted_reconciliation.operation, "execution_generation")
    assert not hasattr(accepted_reconciliation.operation, "execution_lease_token")

    refused_reconciliation = coordinator.reconcile_submission(
        owner=refused_request.owner,
        submission_id=refused_request.submission_id,
    )
    assert refused_reconciliation == refused
    assert not hasattr(refused_reconciliation, "normalized_input_digest")

    wrong_owner_errors = []
    for operation_id in (accepted.operation_id, uuid.uuid4()):
        with pytest.raises(OperationNotFoundError) as error:
            coordinator.query_operation(
                owner=_owner("owner-b"),
                operation_id=operation_id,
            )
        wrong_owner_errors.append((type(error.value), str(error.value)))
    assert wrong_owner_errors[0] == wrong_owner_errors[1]

    submission_errors = []
    for submission_id in (accepted_request.submission_id, uuid.uuid4()):
        with pytest.raises(OperationNotFoundError) as error:
            coordinator.reconcile_submission(
                owner=_owner("owner-b"),
                submission_id=submission_id,
            )
        submission_errors.append((type(error.value), str(error.value)))
    assert submission_errors[0] == submission_errors[1]


def test_parent_and_child_admission_slots_are_claimed_atomically() -> None:
    clock = _FakeClock()
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=1,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="test-060",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=1,
                queue_limit=2,
                max_wait_ms=5_000,
                config_revision="test-060",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=1,
                queue_limit=2,
                max_wait_ms=30_000,
                config_revision="test-060",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=clock,
        operation_retention=timedelta(hours=24),
    )
    background = coordinator.submit(
        _request("background", admission_class=AdmissionClass.BACKGROUND)
    )
    interactive = coordinator.submit(_request("interactive"))

    background_claim = coordinator.claim_next(AdmissionClass.BACKGROUND)
    assert background_claim is not None
    assert background_claim.operation.operation_id == background.operation_id
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None
    assert coordinator.inspect_admission_class(AdmissionClass.GLOBAL).active_count == 1
    assert (
        coordinator.inspect_admission_class(AdmissionClass.BACKGROUND).active_count == 1
    )
    assert (
        coordinator.inspect_admission_class(AdmissionClass.INTERACTIVE).active_count
        == 0
    )

    _terminalize_completed(coordinator, background_claim.fence)
    interactive_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert interactive_claim is not None
    assert interactive_claim.operation.operation_id == interactive.operation_id
    assert coordinator.inspect_admission_class(AdmissionClass.GLOBAL).active_count == 1


def test_queued_and_running_cancellation_are_idempotent() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=1, queue_limit=3)
    running_request = _request("running")
    running = coordinator.submit(running_request)
    running_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert running_claim is not None
    queued_request = _request("queued")
    queued = coordinator.submit(queued_request)

    queued_cancelled = coordinator.cancel(
        owner=queued_request.owner,
        operation_id=queued.operation_id,
        terminal_code="cancelled_by_user",
    )
    assert queued_cancelled.state is OperationState.CANCELLED
    assert queued_cancelled.execution_generation == 0
    assert queued_cancelled.execution_lease_token is None
    assert queued_cancelled.cancel_requested_at == clock.current
    assert queued_cancelled.terminal_at == clock.current
    assert queued_cancelled.purge_after == clock.current + timedelta(hours=24)
    assert (
        coordinator.cancel(
            owner=queued_request.owner,
            operation_id=queued.operation_id,
            terminal_code="cancelled_by_user",
        )
        == queued_cancelled
    )

    cancellation_requested = coordinator.cancel(
        owner=running_request.owner,
        operation_id=running.operation_id,
        terminal_code="cancelled_by_user",
    )
    assert cancellation_requested.state is OperationState.RUNNING
    assert cancellation_requested.cancel_requested_at == clock.current
    assert cancellation_requested.state_revision == (
        running_claim.operation.state_revision + 1
    )
    assert coordinator.assert_current_execution(running_claim.fence).state is (
        OperationState.RUNNING
    )
    assert (
        coordinator.cancel(
            owner=running_request.owner,
            operation_id=running.operation_id,
            terminal_code="cancelled_by_user",
        ).state_revision
        == cancellation_requested.state_revision
    )

    terminal = coordinator.terminalize(
        running_claim.fence,
        state=OperationState.CANCELLED,
        terminal_code="cancelled_by_user",
        safe_summary="Cancelled",
        retry_after_ms=None,
    )
    assert terminal.state is OperationState.CANCELLED
    assert terminal.execution_lease_token is None
    assert terminal.execution_generation == running_claim.fence.execution_generation

    # The first terminal state owns the result; a late success cannot replace it.
    assert _terminalize_completed(coordinator, running_claim.fence) == terminal
    with pytest.raises(OperationNotFoundError):
        coordinator.cancel(
            owner=_owner("owner-b"),
            operation_id=running.operation_id,
            terminal_code="cancelled_by_user",
        )


def test_queue_deadline_expires_to_retryable_without_running_user_code() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=1, queue_limit=2, max_wait_ms=5_000)
    running = coordinator.submit(_request("running"))
    running_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert running_claim is not None
    assert running_claim.operation.operation_id == running.operation_id
    queued_request = _request("queued")
    queued = coordinator.submit(queued_request)

    clock.advance(timedelta(seconds=5))
    expired = coordinator.expire_queued()

    assert len(expired) == 1
    assert expired[0].operation_id == queued.operation_id
    assert expired[0].state is OperationState.RETRYABLE
    assert expired[0].terminal_code == "queue_wait_expired"
    assert expired[0].execution_generation == 0
    assert expired[0].execution_lease_token is None
    assert expired[0].started_at is None
    assert expired[0].terminal_at == clock.current
    assert expired[0].purge_after == clock.current + timedelta(hours=24)
    assert coordinator.claim_next(AdmissionClass.INTERACTIVE) is None
    assert (
        coordinator.reconcile_submission(
            owner=queued_request.owner,
            submission_id=queued_request.submission_id,
        ).operation.state
        is OperationState.RETRYABLE
    )


def test_generation_and_lease_token_jointly_fence_execution() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock)
    accepted = coordinator.submit(_request("fenced"))
    claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert claim is not None
    assert claim.operation.operation_id == accepted.operation_id
    fence = claim.fence

    assert coordinator.assert_current_execution(fence).state is OperationState.RUNNING
    wrong_token = dataclasses.replace(
        fence,
        execution_lease_token=uuid.uuid4(),
    )
    big_stale_generation = dataclasses.replace(
        fence,
        execution_generation=2**40,
    )
    for stale in (wrong_token, big_stale_generation):
        with pytest.raises(StaleExecutionFenceError):
            coordinator.assert_current_execution(stale)

    replacement = coordinator.reselect_execution(fence)
    assert replacement.operation_id == fence.operation_id
    assert replacement.execution_generation == fence.execution_generation + 1
    assert replacement.execution_lease_token != fence.execution_lease_token
    assert isinstance(replacement.execution_lease_token, uuid.UUID)
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution(fence)
    assert coordinator.assert_current_execution(replacement).execution_generation == (
        replacement.execution_generation
    )

    terminal = coordinator.terminalize(
        replacement,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary="Unable to complete",
        retry_after_ms=None,
    )
    assert terminal.state is OperationState.FAILED
    assert terminal.execution_generation == replacement.execution_generation
    assert terminal.execution_lease_token is None
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution(replacement)

    # A stale executor may clean up, but its late terminal cannot mutate truth.
    late = coordinator.terminalize(
        fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Late success",
        retry_after_ms=None,
    )
    assert late == terminal


def test_terminal_records_are_queryable_for_24h_and_purged_by_25h() -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clock, active_limit=1, queue_limit=1)
    assert coordinator.current_time() == clock.current
    terminal_request = _request("terminal")
    terminal_acceptance = coordinator.submit(terminal_request)
    terminal_claim = coordinator.claim_next(AdmissionClass.INTERACTIVE)
    assert terminal_claim is not None
    retained_request = _request("retained")
    retained_acceptance = coordinator.submit(retained_request)
    refusal_request = _request("refusal")
    refusal = coordinator.submit(refusal_request)
    assert refusal.accepted is False
    terminal = _terminalize_completed(coordinator, terminal_claim.fence)
    assert terminal.purge_after == clock.current + timedelta(hours=24)

    clock.advance(timedelta(hours=24))
    assert coordinator.oldest_purge_eligible_due_at() is None
    purge_at_boundary = coordinator.purge_expired(limit=100)
    assert purge_at_boundary.operations == 0
    assert purge_at_boundary.submissions == 0
    assert (
        coordinator.query_operation(
            owner=terminal_request.owner,
            operation_id=terminal_acceptance.operation_id,
        ).state
        is OperationState.COMPLETED
    )
    assert (
        coordinator.reconcile_submission(
            owner=refusal_request.owner,
            submission_id=refusal_request.submission_id,
        )
        == refusal
    )

    clock.advance(timedelta(hours=1))
    assert coordinator.oldest_purge_eligible_due_at() == terminal.purge_after
    purged = coordinator.purge_expired(limit=100)
    assert purged.operations == 1
    assert purged.submissions == 2

    with pytest.raises(OperationNotFoundError):
        coordinator.query_operation(
            owner=terminal_request.owner,
            operation_id=terminal_acceptance.operation_id,
        )
    for request in (terminal_request, refusal_request):
        with pytest.raises(OperationNotFoundError):
            coordinator.reconcile_submission(
                owner=request.owner,
                submission_id=request.submission_id,
            )

    # Non-terminal accepted work and its reconciliation identity do not age
    # out behind the worker that still owns them.
    assert (
        coordinator.query_operation(
            owner=retained_request.owner,
            operation_id=retained_acceptance.operation_id,
        ).state
        is OperationState.QUEUED
    )
    assert (
        coordinator.reconcile_submission(
            owner=retained_request.owner,
            submission_id=retained_request.submission_id,
        ).accepted
        is True
    )


def test_execution_fence_value_is_frozen_and_requires_uuid_token() -> None:
    fence = ExecutionFence(
        operation_id=uuid.uuid4(),
        execution_generation=2**40,
        execution_lease_token=uuid.uuid4(),
    )

    assert fence.execution_generation > 2**31
    assert fence.execution_lease_token.version == 4
    with pytest.raises(dataclasses.FrozenInstanceError):
        fence.execution_generation = 1  # type: ignore[misc]
