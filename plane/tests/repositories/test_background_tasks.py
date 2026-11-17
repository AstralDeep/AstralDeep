"""Tests for astralplane.repositories.background_tasks: owner-scoped create/replay
idempotency, monotonic operation-projection transitions, CAS-fenced status changes,
and administrative retention/purge queries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.background_tasks import (
    BackgroundTaskRecord,
    BackgroundTaskRepository,
    BackgroundTaskStatus,
)
from tests.repositories._support import Result, ScriptedTransaction

NOW = datetime(2026, 8, 14, tzinfo=UTC)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "task_id": "task-1",
        "user_id": "owner-1",
        "chat_id": "chat-1",
        "kind": "async_chat",
        "status": "queued",
        "title": "Title",
        "summary": None,
        "created_at": NOW,
        "completed_at": None,
        "notified": False,
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "operation_execution_generation": 1,
    }
    row.update(overrides)
    return row


def _record(**overrides: object) -> BackgroundTaskRecord:
    values: dict[str, object] = {
        "task_id": "task-1",
        "owner_id": "owner-1",
        "conversation_id": "chat-1",
        "kind": "async_chat",
        "status": BackgroundTaskStatus.QUEUED,
        "title": "Title",
        "created_at": NOW,
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "operation_execution_generation": 1,
    }
    values.update(overrides)
    return BackgroundTaskRecord(**values)  # type: ignore[arg-type]


def test_create_inserts_and_returns_owner_redacted_record() -> None:
    transaction = ScriptedTransaction(execute=[Result(returned_records=(_row(),))])
    result = BackgroundTaskRepository().create(transaction, _record())  # type: ignore[arg-type]
    assert result.status is BackgroundTaskStatus.QUEUED
    assert "owner-1" not in repr(result)
    assert "ON CONFLICT (task_id) DO NOTHING" in transaction.calls[0][1]


def test_create_replay_is_idempotent_but_conflicting_or_foreign_is_rejected() -> None:
    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_row()])
    assert BackgroundTaskRepository().create(replay, _record()).task_id == "task-1"  # type: ignore[arg-type]

    changed = ScriptedTransaction(
        execute=[Result(rowcount=0)], one=[_row(title="different")]
    )
    with pytest.raises(RepositoryConflictError, match="replay"):
        BackgroundTaskRepository().create(changed, _record())  # type: ignore[arg-type]

    foreign = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="elsewhere"):
        BackgroundTaskRepository().create(foreign, _record())  # type: ignore[arg-type]


def test_operation_projection_inserts_or_advances_monotonically() -> None:
    repository = BackgroundTaskRepository()
    inserted = repository.apply_operation_projection(
        ScriptedTransaction(execute=[Result(returned_records=(_row(),))]),
        _record(),
    )
    assert inserted.status is BackgroundTaskStatus.QUEUED

    running_record = _record(
        status=BackgroundTaskStatus.RUNNING,
        operation_execution_generation=2,
    )
    advanced = ScriptedTransaction(
        execute=[
            Result(rowcount=0),
            Result(
                returned_records=(
                    _row(status="running", operation_execution_generation=2),
                )
            ),
        ],
        one=[_row()],
    )
    result = repository.apply_operation_projection(advanced, running_record)
    assert result.status is BackgroundTaskStatus.RUNNING
    assert result.operation_execution_generation == 2
    assert "FOR UPDATE" in advanced.calls[1][1]
    assert "operation_execution_generation IS NOT DISTINCT FROM %s" in advanced.calls[2][1]


def test_operation_projection_allows_direct_terminal_and_exact_enrichment() -> None:
    repository = BackgroundTaskRepository()
    terminal = _record(
        status=BackgroundTaskStatus.COMPLETED,
        completed_at=NOW,
        summary="safe",
        notified=True,
        operation_execution_generation=2,
    )
    direct = ScriptedTransaction(
        execute=[
            Result(rowcount=0),
            Result(
                returned_records=(
                    _row(
                        status="completed",
                        completed_at=NOW,
                        summary="safe",
                        notified=True,
                        operation_execution_generation=2,
                    ),
                )
            ),
        ],
        one=[_row(operation_execution_generation=None)],
    )
    assert repository.apply_operation_projection(direct, terminal).notified

    existing_terminal = _row(
        status="completed",
        completed_at=NOW,
        summary=None,
        notified=False,
        operation_execution_generation=2,
    )
    enriched = ScriptedTransaction(
        execute=[
            Result(rowcount=0),
            Result(
                returned_records=(
                    existing_terminal | {"summary": "safe", "notified": True},
                )
            ),
        ],
        one=[existing_terminal],
    )
    result = repository.apply_operation_projection(enriched, terminal)
    assert result.summary == "safe" and result.notified


@pytest.mark.parametrize(
    ("existing", "projected", "match"),
    [
        (
            _row(chat_id="other"),
            _record(),
            "immutable identity",
        ),
        (
            _row(status="completed", completed_at=NOW),
            _record(status="failed", completed_at=NOW),
            "terminal projection",
        ),
        (
            _row(status="running", operation_execution_generation=2),
            _record(
                status="running",
                operation_execution_generation=1,
            ),
            "generation moved backwards",
        ),
        (
            _row(status="running"),
            _record(status="queued"),
            "moved backwards",
        ),
    ],
)
def test_operation_projection_rejects_identity_terminal_and_monotonic_drift(
    existing: dict[str, object],
    projected: BackgroundTaskRecord,
    match: str,
) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[existing])
    with pytest.raises(RepositoryConflictError, match=match):
        BackgroundTaskRepository().apply_operation_projection(transaction, projected)


def test_operation_projection_exact_replay_avoids_a_second_write() -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_row()])
    assert BackgroundTaskRepository().apply_operation_projection(
        transaction, _record()
    ).status is BackgroundTaskStatus.QUEUED
    assert [call[0] for call in transaction.calls] == ["execute", "one"]


def test_owner_get_and_bounded_list_never_cross_scope() -> None:
    repository = BackgroundTaskRepository()
    transaction = ScriptedTransaction(one=[_row()], all_rows=[(_row(),)])
    assert repository.get(transaction, owner_id="owner-1", task_id="task-1") is not None  # type: ignore[arg-type]
    listed = repository.list_for_owner(
        transaction, owner_id="owner-1", status="queued", limit=7  # type: ignore[arg-type]
    )
    assert len(listed) == 1
    assert transaction.calls[0][2] == ("task-1", "owner-1")
    assert transaction.calls[1][2] == ("owner-1", "queued", "queued", 7)


def test_foreign_driver_row_fails_closed() -> None:
    transaction = ScriptedTransaction(one=[_row(user_id="owner-2")])
    with pytest.raises(RepositoryDataError, match="another owner's"):
        BackgroundTaskRepository().get(
            transaction, owner_id="owner-1", task_id="task-1"  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("expected", "target", "terminal"),
    [
        ("queued", "running", False),
        ("running", "completed", True),
        ("running", "failed", True),
        ("retryable", "queued", False),
    ],
)
def test_transition_uses_owner_status_and_operation_generation_cas(
    expected: str, target: str, terminal: bool
) -> None:
    completed_at = NOW if terminal else None
    transaction = ScriptedTransaction(
        execute=[
            Result(
                returned_records=(
                    _row(status=target, completed_at=completed_at, summary="safe"),
                )
            )
        ]
    )
    record = BackgroundTaskRepository().transition(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        task_id="task-1",
        expected_status=expected,
        status=target,
        completed_at=completed_at,
        summary="safe",
        expected_operation_execution_generation=1,
    )
    assert record.status.value == target
    assert "operation_execution_generation IS NOT DISTINCT FROM %s" in transaction.calls[0][1]


def test_transition_rejects_illegal_edge_and_inconsistent_terminal_time() -> None:
    repository = BackgroundTaskRepository()
    with pytest.raises(RepositoryValidationError, match="unsupported"):
        repository.transition(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            task_id="task-1",
            expected_status="completed",
            status="running",
        )
    with pytest.raises(RepositoryValidationError, match="completed_at"):
        repository.transition(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            task_id="task-1",
            expected_status="running",
            status="completed",
        )


@pytest.mark.parametrize("existing", [_row(), None])
def test_transition_distinguishes_stale_from_missing(existing: object) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[existing])  # type: ignore[list-item]
    error = RepositoryConflictError if existing else RepositoryNotFoundError
    with pytest.raises(error):
        BackgroundTaskRepository().transition(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            task_id="task-1",
            expected_status="queued",
            status="running",
            expected_operation_execution_generation=1,
        )


def test_mark_notified_is_an_unambiguous_owner_cas() -> None:
    repository = BackgroundTaskRepository()
    assert repository.mark_notified(
        ScriptedTransaction(execute=[Result(rowcount=1)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        task_id="task-1",
    )
    assert not repository.mark_notified(
        ScriptedTransaction(execute=[Result(rowcount=0)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        task_id="task-1",
    )
    with pytest.raises(RepositoryDataError, match="ambiguous"):
        repository.mark_notified(
            ScriptedTransaction(execute=[Result(rowcount=2)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            task_id="task-1",
        )


def test_administrative_legacy_retention_query_and_skip_locked_purge() -> None:
    repository = BackgroundTaskRepository()
    cutoff = NOW - timedelta(days=1)
    retained_at = cutoff - timedelta(seconds=17)
    query = ScriptedTransaction(one=[{"retained_at": retained_at}])
    assert repository.oldest_overdue_for_administration(
        query,
        cutoff_at=cutoff,
    ) == retained_at
    assert "operation_id IS NULL" in query.fetch_sql()
    assert query.calls[0][2] == (cutoff,)

    empty = ScriptedTransaction(one=[None])
    assert repository.oldest_overdue_for_administration(
        empty,
        cutoff_at=cutoff,
    ) is None

    purge = ScriptedTransaction(
        execute=[
            Result(
                returned_records=(
                    {"task_id": "deadbeef"},
                    {"task_id": "11111111-1111-4111-8111-111111111111"},
                )
            )
        ]
    )
    assert repository.purge_overdue_for_administration(
        purge,
        cutoff_at=cutoff,
        limit=2,
    ) == ("deadbeef", "11111111-1111-4111-8111-111111111111")
    assert "FOR UPDATE SKIP LOCKED" in purge.fetch_sql()
    assert purge.calls[0][2] == (cutoff, 2)


@pytest.mark.parametrize(
    "record",
    [
        _record(owner_id=""),
        _record(status="unknown"),
        _record(operation_id=None),
        _record(status="completed", completed_at=None),
    ],
)
def test_create_validation_is_fail_closed(record: BackgroundTaskRecord) -> None:
    with pytest.raises(RepositoryValidationError):
        BackgroundTaskRepository().create(ScriptedTransaction(), record)  # type: ignore[arg-type]


def test_persisted_invalid_state_is_a_data_error_and_limits_are_bounded() -> None:
    with pytest.raises(RepositoryDataError):
        BackgroundTaskRepository().get(
            ScriptedTransaction(one=[_row(status="unknown")]),  # type: ignore[arg-type]
            owner_id="owner-1",
            task_id="task-1",
        )
    with pytest.raises(RepositoryValidationError):
        BackgroundTaskRepository().list_for_owner(
            ScriptedTransaction(), owner_id="owner-1", limit=1001  # type: ignore[arg-type]
        )
