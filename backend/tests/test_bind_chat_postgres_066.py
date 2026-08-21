"""Feature-066 PostgreSQL pins for Plane-owned work-admission ``bind_chat``.

The coordinator + in-memory twin are pinned by ``test_bind_chat_066.py``;
these cases prove the DURABLE variant honors the same contract against a real
PostgreSQL database: adopt-on-None, idempotent same-chat re-bind (early
return, no UPDATE, no revision spin), cross-conversation refusal, and the
fenced UPDATE's row-gone stale-fence conversion. Same throwaway-database
pattern as ``test_work_admission_repository.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterator

import pytest

from astralplane.repositories import work_admission as plane_admission

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
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@dataclass
class _FakeClock:
    current: datetime = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _classes():
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=2,
            queue_limit=0,
            max_wait_ms=None,
            config_revision="test-066-postgres",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=2,
            queue_limit=2,
            max_wait_ms=5_000,
            config_revision="test-066-postgres",
        ),
    )


def _owner(user_id: str = "owner-a") -> OperationOwner:
    return OperationOwner(OwnerScope.USER, user_id, None)


def _request(label: str, *, chat_id: str | None = None) -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=_owner(),
        submission_id=submission_id,
        idempotency_namespace="bind_chat_repository_test",
        idempotency_key=label,
        normalized_input_digest=hashlib.sha256(label.encode()).hexdigest(),
        chat_id=chat_id,
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("bind_chat") as runtime:
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


def _coordinator(
    clean_database: PlaneTestRuntime,
    clock: _FakeClock,
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(),
        repository=PlaneWorkAdmissionRepository(
            plane_runtime=clean_database,
            plane_repositories=clean_database.repositories,
        ),
        clock=clock,
    )


def _claimed(coordinator, request):
    accepted = coordinator.submit(request)
    assert accepted.accepted is True
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, accepted.operation_id
    )
    assert claim is not None
    return accepted, claim


def test_postgres_bind_chat_adopts_the_created_conversation(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clean_database, clock)
    request = _request("pg-first-message", chat_id=None)
    accepted, claim = _claimed(coordinator, request)
    assert claim.operation.chat_id is None

    updated = coordinator.bind_chat(claim.fence, "chat-created-066")

    assert updated.chat_id == "chat-created-066"
    assert updated.state is OperationState.RUNNING
    assert updated.state_revision == claim.operation.state_revision + 1
    # Durable: a second coordinator over the same database sees the binding.
    second = _coordinator(clean_database, clock)
    projection = second.query_operation(
        owner=request.owner, operation_id=accepted.operation_id
    )
    assert projection.chat_id == "chat-created-066"


def test_postgres_bind_chat_same_chat_rebind_is_a_no_op(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clean_database, clock)
    accepted, claim = _claimed(coordinator, _request("pg-idempotent", chat_id=None))

    first = coordinator.bind_chat(claim.fence, "chat-a")
    second = coordinator.bind_chat(claim.fence, "chat-a")

    assert first.chat_id == second.chat_id == "chat-a"
    # The early return skips the UPDATE entirely — no revision spin.
    assert second.state_revision == first.state_revision
    row = clean_database.fetch_one(
        "SELECT chat_id, state_revision FROM operation_record "
        "WHERE operation_id = ?",
        (str(accepted.operation_id),),
    )
    assert row["chat_id"] == "chat-a"
    assert int(row["state_revision"]) == first.state_revision


def test_postgres_bind_chat_refuses_a_cross_conversation_rebind(
    clean_database: PlaneTestRuntime,
) -> None:
    clock = _FakeClock()
    coordinator = _coordinator(clean_database, clock)
    _, claim = _claimed(coordinator, _request("pg-adopted", chat_id=None))
    coordinator.bind_chat(claim.fence, "chat-a")
    with pytest.raises(ValueError, match="different conversation"):
        coordinator.bind_chat(claim.fence, "chat-b")

    # Same refusal for an operation admitted WITH its conversation; the
    # same-chat "re-bind" of that operation is a no-op success.
    _, scoped_claim = _claimed(
        coordinator, _request("pg-scoped", chat_id="chat-original")
    )
    with pytest.raises(ValueError, match="different conversation"):
        coordinator.bind_chat(scoped_claim.fence, "chat-other")
    unchanged = coordinator.bind_chat(scoped_claim.fence, "chat-original")
    assert unchanged.chat_id == "chat-original"


def test_postgres_bind_chat_converts_a_lost_fenced_update_to_stale(
    clean_database: PlaneTestRuntime,
) -> None:
    """A row the fenced UPDATE no longer matches surfaces as a stale fence.

    The UPDATE's WHERE clause re-checks the fence, so a superseded lease can
    never bind even when the row-lock assert raced ahead of it. Simulate that
    narrow race by letting the Plane transaction assert see the CURRENT fence
    while the UPDATE itself runs with a stale lease token: zero rows return
    and the repository must convert that to ``StaleExecutionFenceError``.
    """
    clock = _FakeClock()
    coordinator = _coordinator(clean_database, clock)
    _, claim = _claimed(coordinator, _request("pg-raced", chat_id=None))
    adapter = coordinator._repository  # noqa: SLF001 - race simulation
    repository = adapter._plane_repository  # noqa: SLF001 - Plane race simulation
    stale = dataclasses.replace(claim.fence, execution_lease_token=uuid.uuid4())
    current_fence = plane_admission.ExecutionFence(
        claim.fence.operation_id,
        claim.fence.execution_generation,
        claim.fence.execution_lease_token,
    )
    current_assert = repository._assert_current_execution_session  # noqa: SLF001
    repository._assert_current_execution_session = (  # noqa: SLF001
        lambda session, fence: current_assert(session, current_fence)
    )
    try:
        with pytest.raises(StaleExecutionFenceError, match="stale"):
            adapter.bind_chat(stale, "chat-raced", now=clock.current)
    finally:
        del repository._assert_current_execution_session  # noqa: SLF001
    # The losing write bound nothing.
    assert coordinator.assert_current_execution(claim.fence).chat_id is None
