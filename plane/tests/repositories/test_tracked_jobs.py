"""Tests for astralplane.repositories.tracked_jobs: owner-scoped create/replay
idempotency, poll updates CAS-fenced by owner/fail-count/timestamp, and count-checked
account-retirement deletes.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.tracked_jobs import TrackedJobRecord, TrackedJobRepository
from tests.repositories._support import Result, ScriptedTransaction


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "tracked_job_id": "tracked-1",
        "owner_user_id": "owner-1",
        "machine_id": "machine-1",
        "chat_id": "chat-1",
        "scheduler_job_id": "42",
        "submit_marker": "marker",
        "output_path": "output.log",
        "component_id": "component-1",
        "job_name": "job",
        "state": "submitted",
        "exit_code": None,
        "terminal": False,
        "notify_on_finish": True,
        "notified": False,
        "fail_count": 0,
        "created_at": 100,
        "last_polled_at": None,
        "finished_at": None,
    }
    row.update(overrides)
    return row


def _record(**overrides: object) -> TrackedJobRecord:
    values: dict[str, object] = {
        "tracked_job_id": "tracked-1",
        "owner_id": "owner-1",
        "machine_id": "machine-1",
        "conversation_id": "chat-1",
        "scheduler_job_id": "42",
        "submit_marker": "marker",
        "output_path": "output.log",
        "component_id": "component-1",
        "job_name": "job",
        "notify_on_finish": True,
        "created_at": 100,
    }
    values.update(overrides)
    return TrackedJobRecord(**values)  # type: ignore[arg-type]


def test_create_and_exact_replay_are_idempotent() -> None:
    inserted = ScriptedTransaction(execute=[Result(returned_records=(_row(),))])
    result = TrackedJobRepository().create(inserted, _record())  # type: ignore[arg-type]
    assert result.scheduler_job_id == "42"
    assert "owner-1" not in repr(result)

    replay = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_row()])
    assert TrackedJobRepository().create(replay, _record()) == result  # type: ignore[arg-type]


def test_create_rejects_changed_or_foreign_unique_identity() -> None:
    changed = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_row(job_name="other")])
    with pytest.raises(RepositoryConflictError, match="replay"):
        TrackedJobRepository().create(changed, _record())  # type: ignore[arg-type]
    foreign = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None])
    with pytest.raises(RepositoryConflictError, match="elsewhere"):
        TrackedJobRepository().create(foreign, _record())  # type: ignore[arg-type]


def test_get_and_lists_are_explicit_about_owner_or_administration() -> None:
    repository = TrackedJobRepository()
    transaction = ScriptedTransaction(
        one=[_row(), _row()],
        all_rows=[(_row(),), (_row(owner_user_id="owner-2"),)],
    )
    assert repository.get(
        transaction, owner_id="owner-1", tracked_job_id="tracked-1"  # type: ignore[arg-type]
    )
    assert repository.get_by_scheduler_job(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        scheduler_job_id="42",
        machine_id="machine-1",
    )
    assert len(repository.list_for_owner(transaction, owner_id="owner-1", limit=7)) == 1  # type: ignore[arg-type]
    assert len(repository.list_open_for_administration(transaction, limit=8)) == 1  # type: ignore[arg-type]
    assert transaction.calls[0][2] == ("tracked-1", "owner-1")
    assert transaction.calls[1][2] == ("owner-1", "42", "machine-1", "machine-1")


def test_owner_read_rejects_foreign_driver_row() -> None:
    transaction = ScriptedTransaction(one=[_row(owner_user_id="owner-2")])
    with pytest.raises(RepositoryDataError, match="another owner's"):
        TrackedJobRepository().get(
            transaction, owner_id="owner-1", tracked_job_id="tracked-1"  # type: ignore[arg-type]
        )


def test_poll_update_is_owner_fail_count_and_timestamp_cas() -> None:
    updated = _row(
        state="RUNNING",
        fail_count=0,
        last_polled_at=110,
    )
    transaction = ScriptedTransaction(
        one=[None, None], execute=[Result(returned_records=(updated,))]
    )
    record = TrackedJobRepository().apply_poll(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        tracked_job_id="tracked-1",
        expected_fail_count=0,
        expected_last_polled_at=None,
        state="RUNNING",
        exit_code=None,
        terminal=False,
        fail_count=0,
        polled_at=110,
    )
    assert record.state == "RUNNING"
    statement = transaction.calls[-1][1]
    assert "last_polled_at IS NOT DISTINCT FROM %s" in statement
    assert "owner_user_id = %s" in statement


def test_terminal_poll_sets_stable_finished_timestamp() -> None:
    transaction = ScriptedTransaction(
        one=[None, {"state": "active"}],
        execute=[
            Result(
                returned_records=(
                    _row(
                        state="COMPLETED",
                        exit_code="0:0",
                        terminal=True,
                        last_polled_at=120,
                        finished_at=120,
                    ),
                )
            )
        ]
    )
    record = TrackedJobRepository().apply_poll(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        tracked_job_id="tracked-1",
        expected_fail_count=0,
        expected_last_polled_at=None,
        state="COMPLETED",
        exit_code="0:0",
        terminal=True,
        fail_count=0,
        polled_at=120,
    )
    assert record.terminal and record.finished_at == 120
    assert "COALESCE(finished_at, %s)" in transaction.calls[-1][1]


@pytest.mark.parametrize("existing", [_row(), None])
def test_poll_miss_distinguishes_stale_fence_from_absent_owner(existing: object) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[None, None, existing])  # type: ignore[list-item]
    error = RepositoryConflictError if existing else RepositoryNotFoundError
    with pytest.raises(error):
        TrackedJobRepository().apply_poll(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            tracked_job_id="tracked-1",
            expected_fail_count=0,
            expected_last_polled_at=None,
            state="RUNNING",
            exit_code=None,
            terminal=False,
            fail_count=0,
            polled_at=110,
        )


def test_notification_is_terminal_owner_cas_with_ambiguous_count_guard() -> None:
    repository = TrackedJobRepository()
    assert repository.mark_notified(
        ScriptedTransaction(execute=[Result(rowcount=1)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        tracked_job_id="tracked-1",
    )
    assert not repository.mark_notified(
        ScriptedTransaction(execute=[Result(rowcount=0)]),  # type: ignore[arg-type]
        owner_id="owner-1",
        tracked_job_id="tracked-1",
    )
    with pytest.raises(RepositoryDataError):
        repository.mark_notified(
            ScriptedTransaction(execute=[Result(rowcount=2)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            tracked_job_id="tracked-1",
        )


def test_account_retirement_deletes_only_owner_jobs_with_count_evidence() -> None:
    repository = TrackedJobRepository()
    transaction = ScriptedTransaction(execute=[Result(rowcount=4)])
    assert repository.delete_owner(transaction, owner_id="owner-1") == 4
    assert transaction.calls[0][2] == ("owner-1",)
    assert "WHERE owner_user_id = %s" in transaction.calls[0][1]
    with pytest.raises(RepositoryValidationError):
        repository.delete_owner(ScriptedTransaction(), owner_id="")
    with pytest.raises(RepositoryDataError):
        repository.delete_owner(
            ScriptedTransaction(execute=[Result(rowcount=-1)]), owner_id="owner-1"
        )


@pytest.mark.parametrize(
    "record",
    [
        _record(owner_id=""),
        _record(fail_count=-1),
        _record(last_polled_at=99),
        _record(terminal=True, finished_at=None),
        _record(state=""),
    ],
)
def test_record_validation_is_bounded(record: TrackedJobRecord) -> None:
    with pytest.raises(RepositoryValidationError):
        TrackedJobRepository().create(ScriptedTransaction(), record)  # type: ignore[arg-type]


def test_persisted_invalid_shape_and_list_bounds_fail_closed() -> None:
    with pytest.raises(RepositoryDataError):
        TrackedJobRepository().get(
            ScriptedTransaction(one=[_row(terminal=True, finished_at=None)]),  # type: ignore[arg-type]
            owner_id="owner-1",
            tracked_job_id="tracked-1",
        )
    with pytest.raises(RepositoryValidationError):
        TrackedJobRepository().list_open_for_administration(
            ScriptedTransaction(), limit=1001  # type: ignore[arg-type]
        )
