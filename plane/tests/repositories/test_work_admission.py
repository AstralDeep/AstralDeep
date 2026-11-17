"""Tests for src/astralplane/repositories/work_admission.py: config load/publish
atomicity, submission replay idempotency, CAS fencing, voice capacity admission, and
purge-eligibility predicates.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories import work_admission as admission
from astralplane.repositories.work_admission import (
    AcceptedAdmission,
    AdmissionClass,
    AdmissionClassConfig,
    ExecutionFence,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleWorkExecutionFenceError,
    WorkAdmissionConfigurationError,
    WorkAdmissionIntegrityError,
    WorkAdmissionRepository,
)


@dataclass(frozen=True)
class _Result:
    rowcount: int = 0
    status_message: str | None = "OK"
    returned_records: tuple[dict[str, Any], ...] = ()


class _Transaction:
    def __init__(self, results: list[_Result] | None = None) -> None:
        self._results = deque(results or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> _Result:
        self.calls.append((statement, parameters))
        return self._results.popleft() if self._results else _Result()

    def fetch_one(self, statement: str, parameters: tuple[object, ...] = ()) -> None:
        raise AssertionError("repository must use its detached transaction session")

    def fetch_all(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> tuple[()]:
        raise AssertionError("repository must use its detached transaction session")

    @contextmanager
    def savepoint(self, name: str) -> Iterator[_Transaction]:
        yield self


def test_retryable_inventory_uses_bounded_detached_administrative_keyset():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    row = _running_row()
    row.update(state="retryable", terminal_code="execution_lease_expired")
    transaction = _Transaction([_Result(returned_records=(row,))])
    after = admission.RetryableOperationCursor(now, uuid.uuid4())

    records = WorkAdmissionRepository().list_retryable_for_administration(
        transaction, terminal_code="execution_lease_expired", now=now,
        after=after, limit=3, handled_idempotency_namespace="recovery.test",
    )

    assert isinstance(records, tuple) and records[0].operation_id == row["operation_id"]
    statement, parameters = transaction.calls[0]
    assert "state = 'retryable'" in statement
    assert "terminal_code = %s" in statement and "purge_after > %s" in statement
    assert "(operation.accepted_at, operation.operation_id) >" in statement
    assert "ORDER BY operation.accepted_at, operation.operation_id" in statement
    assert "LIMIT %s" in statement
    assert "child.parent_operation_id = operation.operation_id" in statement
    assert "child.owner_scope = operation.owner_scope" in statement
    assert "child.owner_user_id IS NOT DISTINCT FROM operation.owner_user_id" in statement
    assert (
        "child.connection_scope_id IS NOT DISTINCT FROM operation.connection_scope_id" in statement
    )
    assert "child.idempotency_namespace = %s" in statement
    assert "child.idempotency_key = operation.operation_id::text" in statement
    assert parameters == (
        "execution_lease_expired", now, now, now, str(after.operation_id),
        "recovery.test", "recovery.test", 3,
    )
    assert not any(word in statement.upper() for word in ("INSERT", "UPDATE", "DELETE"))


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1001, 1.5, "2"])
def test_retryable_inventory_refuses_invalid_page_limit_before_query(limit):
    transaction = _Transaction()
    with pytest.raises(admission.RepositoryValidationError):
        WorkAdmissionRepository().list_retryable_for_administration(
            transaction, terminal_code="execution_lease_expired", limit=limit,
        )
    assert transaction.calls == []


@pytest.mark.parametrize("accepted_at,operation_id", [
    (datetime(2026, 8, 14), uuid.uuid4()),
    ("2026-08-14", uuid.uuid4()),
    (datetime(2026, 8, 14, tzinfo=UTC), "not-a-uuid"),
])
def test_retryable_inventory_refuses_malformed_typed_cursor(accepted_at, operation_id):
    with pytest.raises(admission.RepositoryValidationError):
        admission.RetryableOperationCursor(accepted_at, operation_id)


@pytest.mark.parametrize("terminal_code,after", [(None, None), ("unsafe code", None),
                                                  ("execution_lease_expired", object())])
def test_retryable_inventory_refuses_invalid_predicates(terminal_code, after):
    transaction = _Transaction()
    with pytest.raises(admission.RepositoryValidationError):
        WorkAdmissionRepository().list_retryable_for_administration(
            transaction, terminal_code=terminal_code, after=after,
        )
    assert transaction.calls == []


def test_retryable_inventory_uses_database_time_and_empty_page():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    transaction = _Transaction([_Result(returned_records=({"current_time": now},)), _Result()])
    assert WorkAdmissionRepository().list_retryable_for_administration(
        transaction, terminal_code="execution_lease_expired",
    ) == ()
    assert len(transaction.calls) == 2
    assert transaction.calls[1][1] == (
        "execution_lease_expired", now, None, None, None, None, None, 100,
    )


@pytest.mark.parametrize("namespace", [False, 1, "", "x" * 129])
def test_retryable_inventory_refuses_invalid_handled_namespace(namespace):
    transaction = _Transaction()
    with pytest.raises(admission.RepositoryValidationError):
        WorkAdmissionRepository().list_retryable_for_administration(
            transaction, terminal_code="execution_lease_expired",
            handled_idempotency_namespace=namespace,
        )
    assert transaction.calls == []


@pytest.mark.parametrize("owner,owner_parameters", [
    (OperationOwner(OwnerScope.USER, "owner-a", None), ("user", "owner-a")),
    (OperationOwner(OwnerScope.CONNECTION, None, uuid.UUID(int=1)),
     ("connection", str(uuid.UUID(int=1)))),
    (OperationOwner(OwnerScope.MAINTENANCE, None, None), ("maintenance",)),
])
def test_read_only_idempotent_lookup_scopes_owner_and_does_not_lock(owner, owner_parameters):
    transaction = _Transaction([_Result(returned_records=(_running_row(),))])
    record = WorkAdmissionRepository().get_idempotent_operation(
        transaction, owner=owner, idempotency_namespace="test", idempotency_key="same",
    )
    assert record is not None and record.idempotency_key == "same"
    statement, parameters = transaction.calls[0]
    assert "owner_scope = %s" in statement and "LIMIT 1" in statement
    if owner.owner_scope is OwnerScope.USER:
        assert "owner_user_id = %s" in statement
    if owner.owner_scope is OwnerScope.CONNECTION:
        assert "connection_scope_id = %s" in statement
    assert parameters == (*owner_parameters, "test", "same")
    assert not any(word in statement.upper() for word in ("FOR UPDATE", "INSERT", "DELETE"))


@pytest.mark.parametrize("owner,namespace,key", [
    (None, "test", "key"),
    (OperationOwner(OwnerScope.USER, "owner-a", None), False, "key"),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "", "key"),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "x" * 129, "key"),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "test", ""),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "test", None),
    (OperationOwner(OwnerScope.USER, "owner-a", None), "test", "x" * 257),
])
def test_idempotent_lookup_refuses_invalid_identity_before_query(owner, namespace, key):
    transaction = _Transaction()
    with pytest.raises(admission.RepositoryValidationError):
        WorkAdmissionRepository().get_idempotent_operation(
            transaction, owner=owner, idempotency_namespace=namespace, idempotency_key=key,
        )
    assert transaction.calls == []


def test_idempotent_lookup_absence_is_not_an_admission():
    transaction = _Transaction()
    assert WorkAdmissionRepository().get_idempotent_operation(
        transaction, owner=OperationOwner(OwnerScope.USER, "owner-a", None),
        idempotency_namespace="test", idempotency_key="missing",
    ) is None
    assert len(transaction.calls) == 1


def _config_rows() -> tuple[dict[str, Any], ...]:
    parents = {
        AdmissionClass.GLOBAL: None,
        AdmissionClass.INTERACTIVE: AdmissionClass.GLOBAL,
        AdmissionClass.VOICE_INTERACTIVE: AdmissionClass.INTERACTIVE,
        AdmissionClass.MCP: AdmissionClass.GLOBAL,
        AdmissionClass.BACKGROUND: AdmissionClass.GLOBAL,
        AdmissionClass.SCHEDULED: AdmissionClass.GLOBAL,
        AdmissionClass.MAINTENANCE: AdmissionClass.GLOBAL,
        AdmissionClass.SYSTEM: AdmissionClass.GLOBAL,
    }
    return tuple(
        {
            "class_name": member.value,
            "parent_class_name": (
                None if parents[member] is None else parents[member].value
            ),
            "active_limit": 2,
            "queue_limit": 0 if member is AdmissionClass.VOICE_INTERACTIVE else 3,
            "max_wait_ms": (
                0 if member is AdmissionClass.VOICE_INTERACTIVE else 5_000
            ),
            "config_revision": "plane-work-admission-074",
        }
        for member in AdmissionClass
    )


def _running_row(
    *,
    operation_id: uuid.UUID | None = None,
    lease_token: uuid.UUID | None = None,
) -> dict[str, Any]:
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    return {
        "operation_id": operation_id or uuid.uuid4(),
        "operation_kind": "connection_frame",
        "admission_class": AdmissionClass.INTERACTIVE.value,
        "owner_scope": OwnerScope.USER.value,
        "owner_user_id": "owner-a",
        "connection_scope_id": None,
        "idempotency_namespace": "test",
        "idempotency_key": "same",
        "normalized_input_digest": hashlib.sha256(b"same").hexdigest(),
        "chat_id": "chat-a",
        "parent_operation_id": None,
        "connection_generation": uuid.uuid4(),
        "request_generation": uuid.uuid4(),
        "state": OperationState.RUNNING.value,
        "phase_code": None,
        "terminal_code": None,
        "safe_summary": None,
        "retry_after_ms": None,
        "execution_generation": 1,
        "execution_lease_token": lease_token or uuid.uuid4(),
        "state_revision": 1,
        "accepted_at": now,
        "updated_at": now,
        "queue_deadline_at": None,
        "started_at": now,
        "terminal_at": None,
        "cancel_requested_at": None,
        "purge_after": None,
    }


def _request(submission_id: uuid.UUID) -> OperationRequest:
    return OperationRequest(
        operation_kind="connection_frame",
        admission_class=AdmissionClass.INTERACTIVE,
        owner=OperationOwner(OwnerScope.USER, "owner-a", None),
        submission_id=submission_id,
        idempotency_namespace="test",
        idempotency_key="same",
        normalized_input_digest=hashlib.sha256(b"same").hexdigest(),
        chat_id="chat-a",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def test_load_existing_configs_is_detached_complete_and_read_only() -> None:
    repository = WorkAdmissionRepository()
    transaction = _Transaction([_Result(returned_records=_config_rows())])

    configs = repository.load_existing_configs(transaction)

    assert {config.class_name for config in configs} == set(AdmissionClass)
    assert repository._configs == {}
    repository.bind_configs(configs)
    assert repository._configs[AdmissionClass.INTERACTIVE].active_limit == 2
    sql = transaction.calls[0][0]
    assert "FOR SHARE" in sql
    assert "INSERT" not in sql
    assert "UPDATE" not in sql


def test_configure_does_not_publish_uncommitted_configuration() -> None:
    repository = WorkAdmissionRepository()
    configs = tuple(
        AdmissionClassConfig(
            class_name=AdmissionClass(row["class_name"]),
            parent_class_name=(
                None
                if row["parent_class_name"] is None
                else AdmissionClass(row["parent_class_name"])
            ),
            active_limit=row["active_limit"],
            queue_limit=row["queue_limit"],
            max_wait_ms=(row["max_wait_ms"] or None),
            config_revision=row["config_revision"],
        )
        for row in _config_rows()
    )

    repository.configure(_Transaction(), configs)

    assert repository._configs == {}
    repository.bind_configs(configs)
    assert set(repository._configs) == set(AdmissionClass)


def test_load_existing_configs_rejects_an_incomplete_structural_snapshot() -> None:
    repository = WorkAdmissionRepository()
    transaction = _Transaction(
        [_Result(returned_records=(_config_rows()[0], _config_rows()[1]))]
    )

    with pytest.raises(WorkAdmissionConfigurationError, match="every class"):
        repository.load_existing_configs(transaction)


def test_accepted_submission_replay_returns_original_without_inserting() -> None:
    repository = WorkAdmissionRepository()
    repository._configs = {
        AdmissionClass.GLOBAL: AdmissionClassConfig(
            AdmissionClass.GLOBAL, None, 1, 0, None, "test"
        ),
        AdmissionClass.INTERACTIVE: AdmissionClassConfig(
            AdmissionClass.INTERACTIVE,
            AdmissionClass.GLOBAL,
            1,
            1,
            1_000,
            "test",
        ),
    }
    operation_id = uuid.uuid4()
    operation = _running_row(operation_id=operation_id)
    submission_id = uuid.uuid4()
    transaction = _Transaction(
        [
            _Result(returned_records=({"current_time": operation["accepted_at"]},)),
            _Result(),
            _Result(),
            _Result(
                returned_records=(
                    {
                        "accepted": True,
                        "operation_id": operation_id,
                    },
                )
            ),
            _Result(returned_records=(operation,)),
        ]
    )

    result = repository.submit(
        transaction,
        _request(submission_id),
        now=None,
        retention=timedelta(hours=24),
        slot_lease=timedelta(seconds=30),
    )

    assert isinstance(result, AcceptedAdmission)
    assert result.operation_id == operation_id
    assert result.created is False
    assert all("INSERT INTO" not in statement for statement, _ in transaction.calls)
    assert all(
        "pg_advisory_xact_lock(%s)" in transaction.calls[index][0]
        for index in (1, 2)
    )
    assert all("?" not in statement for statement, _ in transaction.calls)


@pytest.mark.parametrize("preselected", [True, False])
def test_peek_next_detaches_owner_and_identity_without_locks_or_mutation(preselected) -> None:
    operation = _running_row()
    if not preselected:
        operation = operation | {"state": "queued"}
    transaction = _Transaction(
        ([_Result()] if not preselected else []) + [_Result(returned_records=(operation,))]
    )
    result = WorkAdmissionRepository().peek_next(transaction, AdmissionClass.INTERACTIVE)
    assert result.operation_id == operation["operation_id"]
    assert result.owner_user_id == "owner-a"
    assert result.state is (OperationState.RUNNING if preselected else OperationState.QUEUED)
    assert len(transaction.calls) == (1 if preselected else 2)
    for statement, parameters in transaction.calls:
        assert "FOR UPDATE" not in statement and "pg_advisory" not in statement
        assert "INSERT" not in statement and "UPDATE" not in statement
        assert "ORDER BY" in statement
        assert parameters == (AdmissionClass.INTERACTIVE.value,)


def test_peek_next_empty_class_has_no_side_effect() -> None:
    transaction = _Transaction()
    assert WorkAdmissionRepository().peek_next(transaction, AdmissionClass.INTERACTIVE) is None
    assert len(transaction.calls) == 2


def test_stale_fence_is_a_typed_compare_and_set_error() -> None:
    operation = _running_row(lease_token=uuid.uuid4())
    transaction = _Transaction([_Result(returned_records=(operation,))])
    repository = WorkAdmissionRepository()
    stale = ExecutionFence(
        operation_id=operation["operation_id"],
        execution_generation=1,
        execution_lease_token=uuid.uuid4(),
    )

    with pytest.raises(StaleWorkExecutionFenceError, match="stale"):
        repository.assert_current_execution(transaction, stale)


def test_admin_operation_read_and_request_generation_binding_are_typed_and_fenced() -> None:
    repository = WorkAdmissionRepository()
    operation = _running_row() | {"request_generation": None}
    operation_id = operation["operation_id"]
    lease_token = operation["execution_lease_token"]
    assert isinstance(operation_id, uuid.UUID)
    assert isinstance(lease_token, uuid.UUID)
    fence = ExecutionFence(operation_id, 1, lease_token)

    read = _Transaction([_Result(returned_records=(operation,))])
    admin_record = repository.get_operation_for_administration(
        read,
        operation_id=operation_id,
        for_update=True,
    )
    assert admin_record is not None
    assert admin_record.owner_user_id == "owner-a"
    assert "FOR UPDATE" in read.calls[0][0]

    request_generation = uuid.uuid4()
    bound_row = operation | {
        "request_generation": request_generation,
        "state_revision": 2,
    }
    bound = repository.bind_request_generation(
        _Transaction(
            [
                _Result(returned_records=(operation,)),
                _Result(returned_records=(bound_row,)),
            ]
        ),
        fence=fence,
        request_generation=request_generation,
    )
    assert bound.request_generation == request_generation

    replay_transaction = _Transaction([_Result(returned_records=(bound_row,))])
    replay = repository.bind_request_generation(
        replay_transaction,
        fence=fence,
        request_generation=request_generation,
    )
    assert replay == bound
    assert len(replay_transaction.calls) == 1

    with pytest.raises(RepositoryConflictError, match="different request"):
        repository.bind_request_generation(
            _Transaction([_Result(returned_records=(bound_row,))]),
            fence=fence,
            request_generation=uuid.uuid4(),
        )


class _VoiceCapacityHarness(WorkAdmissionRepository):
    def __init__(self) -> None:
        super().__init__()
        self._configs = {
            AdmissionClass.VOICE_INTERACTIVE: AdmissionClassConfig(
                AdmissionClass.VOICE_INTERACTIVE,
                AdmissionClass.INTERACTIVE,
                10,
                0,
                None,
                "voice-test",
            )
        }
        self.refusal: dict[str, object] | None = None

    @classmethod
    def _lock_request_identities(cls, cursor: object, request: object) -> None:
        del cursor, request

    def _submission_row(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    def _existing_idempotent_operation(
        self, *args: object, **kwargs: object
    ) -> None:
        del args, kwargs
        return None

    def _lock_class_chain(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    @staticmethod
    def _expire_queued_locked(*args: object, **kwargs: object) -> tuple[()]:
        del args, kwargs
        return ()

    def _expire_execution_leases_locked(
        self, *args: object, **kwargs: object
    ) -> tuple[()]:
        del args, kwargs
        return ()

    def _insert_submission(self, *args: object, **kwargs: object) -> None:
        del args
        self.refusal = kwargs

    def _select_free_slots(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("capacity must be refused before slot selection")


def test_voice_per_owner_capacity_refuses_before_slot_selection_or_insert() -> None:
    repository = _VoiceCapacityHarness()
    transaction = _Transaction(
        [_Result(returned_records=({"running_count": 2},))]
    )
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    request = OperationRequest(
        operation_kind="voice_turn",
        admission_class=AdmissionClass.VOICE_INTERACTIVE,
        owner=OperationOwner(OwnerScope.USER, "owner-a", None),
        submission_id=uuid.uuid4(),
        idempotency_namespace="voice_turn",
        idempotency_key="same",
        normalized_input_digest=hashlib.sha256(b"voice").hexdigest(),
        chat_id="chat-a",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )

    result = repository.submit(
        transaction,
        request,
        now=now,
        retention=timedelta(hours=24),
        slot_lease=timedelta(seconds=30),
    )

    assert result.accepted is False
    assert result.code == "capacity_exceeded"
    assert result.retry_after_ms == 1_000
    query, parameters = transaction.calls[0]
    assert "owner_user_id = %s" in query
    assert "state = 'running'" in query
    assert parameters == (
        AdmissionClass.VOICE_INTERACTIVE.value,
        OwnerScope.USER.value,
        "owner-a",
    )
    assert repository.refusal == {
        "current_time": now,
        "retention": timedelta(hours=24),
        "refusal_code": "capacity_exceeded",
        "retryable": True,
        "retry_after_ms": 1_000,
    }


def test_purge_uses_strict_expiry_and_rechecks_reconciliation_references() -> None:
    operation_id = uuid.uuid4()
    transaction = _Transaction(
        [
            _Result(returned_records=({"current_time": datetime.now(UTC)},)),
            _Result(returned_records=({"submission_result_id": uuid.uuid4()},)),
            _Result(returned_records=({"operation_id": operation_id},)),
            _Result(returned_records=({"operation_id": operation_id},)),
        ]
    )

    result = WorkAdmissionRepository().purge_expired(
        transaction,
        now=None,
        limit=10,
    )

    assert result.operations == 1
    assert result.submissions == 1
    sql = "\n".join(statement for statement, _ in transaction.calls)
    assert "purge_after < %s" in sql
    assert "purge_after <= %s" not in sql
    assert sql.count("NOT EXISTS") == 2


def test_oldest_purge_eligible_due_at_matches_operation_purge_predicate() -> None:
    repository = WorkAdmissionRepository()
    now = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    due_at = now - timedelta(minutes=7)
    transaction = _Transaction([_Result(returned_records=({"due_at": due_at},))])

    assert (
        repository.oldest_purge_eligible_due_at(transaction, now=now) == due_at
    )
    statement, parameters = transaction.calls[0]
    assert statement.count("purge_after < %s") == 3
    assert "background_task" not in statement
    assert "NOT EXISTS" in statement
    assert parameters == (now, now, now)


def test_oldest_purge_eligible_due_at_returns_none_for_an_empty_backlog() -> None:
    transaction = _Transaction([_Result(returned_records=({"due_at": None},))])

    assert (
        WorkAdmissionRepository().oldest_purge_eligible_due_at(
            transaction, now=datetime.now(UTC)
        )
        is None
    )


def test_oldest_purge_eligible_due_at_rejects_a_malformed_persisted_timestamp() -> None:
    transaction = _Transaction(
        [
            _Result(
                returned_records=(
                    {"due_at": datetime(2026, 8, 14, 11, 0)},
                )
            )
        ]
    )

    with pytest.raises(
        WorkAdmissionIntegrityError,
        match="persisted purge eligibility timestamp is invalid",
    ):
        WorkAdmissionRepository().oldest_purge_eligible_due_at(
            transaction,
            now=datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
        )


def test_repository_refuses_non_transaction_callers() -> None:
    with pytest.raises(ValueError, match="Plane Transaction"):
        WorkAdmissionRepository().load_existing_configs(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "call",
    (
        lambda repository, transaction, fence: repository.submit(
            transaction,
            object(),  # type: ignore[arg-type]
            now=datetime.now(UTC),
            retention=timedelta(days=1),
            slot_lease=timedelta(seconds=30),
        ),
        lambda repository, transaction, fence: repository.submit(
            transaction,
            _request(uuid.uuid4()),
            now=datetime.now(UTC),
            retention=timedelta(0),
            slot_lease=timedelta(seconds=30),
        ),
        lambda repository, transaction, fence: repository.claim_next(
            transaction,
            "scheduled",  # type: ignore[arg-type]
            now=datetime.now(UTC),
            retention=timedelta(days=1),
            slot_lease=timedelta(seconds=30),
        ),
        lambda repository, transaction, fence: repository.claim_operation(
            transaction,
            AdmissionClass.SCHEDULED,
            "operation",  # type: ignore[arg-type]
            now=datetime.now(UTC),
            retention=timedelta(days=1),
            slot_lease=timedelta(seconds=30),
        ),
        lambda repository, transaction, fence: repository.query_operation(
            transaction,
            object(),  # type: ignore[arg-type]
            uuid.uuid4(),
        ),
        lambda repository, transaction, fence: repository.cancel(
            transaction,
            OperationOwner(OwnerScope.USER, "owner-a", None),
            uuid.uuid4(),
            "Bad.Code",
            now=datetime.now(UTC),
            retention=timedelta(days=1),
        ),
        lambda repository, transaction, fence: repository.terminalize(
            transaction,
            fence,
            state=OperationState.RUNNING,
            terminal_code=None,
            safe_summary=None,
            retry_after_ms=None,
            now=datetime.now(UTC),
            retention=timedelta(days=1),
        ),
        lambda repository, transaction, fence: repository.terminalize(
            transaction,
            fence,
            state=OperationState.FAILED,
            terminal_code=None,
            safe_summary=None,
            retry_after_ms=None,
            now=datetime.now(UTC),
            retention=timedelta(days=1),
        ),
        lambda repository, transaction, fence: repository.terminalize(
            transaction,
            fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="x" * 513,
            retry_after_ms=None,
            now=datetime.now(UTC),
            retention=timedelta(days=1),
        ),
        lambda repository, transaction, fence: repository.terminalize(
            transaction,
            fence,
            state=OperationState.FAILED,
            terminal_code="failed",
            safe_summary=None,
            retry_after_ms=1,
            now=datetime.now(UTC),
            retention=timedelta(days=1),
        ),
        lambda repository, transaction, fence: repository.update_phase(
            transaction,
            fence,
            "Bad.Phase",
            now=datetime.now(UTC),
        ),
        lambda repository, transaction, fence: repository.bind_chat(
            transaction,
            fence,
            "",
            now=datetime.now(UTC),
        ),
        lambda repository, transaction, fence: repository.reselect_execution(
            transaction,
            fence,
            now=datetime.now(UTC),
            slot_lease=timedelta(0),
        ),
        lambda repository, transaction, fence: repository.purge_expired(
            transaction,
            now=datetime.now(UTC),
            limit=0,
        ),
        lambda repository, transaction, fence: repository.oldest_purge_eligible_due_at(
            transaction,
            now=datetime(2026, 8, 14),
        ),
    ),
)
def test_public_contract_rejects_invalid_values_before_sql(call: object) -> None:
    repository = WorkAdmissionRepository()
    transaction = _Transaction()
    fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())

    with pytest.raises(ValueError):
        call(repository, transaction, fence)  # type: ignore[operator]
    assert transaction.calls == []
