"""Tests for astralplane.repositories.tutorials: step creation and revisioning,
idempotent seeding, CAS-fenced update/archive/restore, and fail-closed handling of
corrupt persisted steps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _support import Result, ScriptedTransaction

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.tutorials import (
    TutorialRepository,
    TutorialStepRecord,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)


def step_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "slug": "welcome-tour",
        "audience": "user",
        "display_order": 10,
        "target_kind": "none",
        "target_key": None,
        "title": "Welcome",
        "body": "A bounded tutorial step.",
        "created_at": NOW,
        "updated_at": NOW,
        "archived_at": None,
    }
    row.update(changes)
    return row


def create_args(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "slug": "welcome-tour",
        "audience": "user",
        "display_order": 10,
        "target_kind": "none",
        "target_key": None,
        "title": "Welcome",
        "body": "A bounded tutorial step.",
        "editor_id": "admin-1",
        "observed_at": NOW,
    }
    values.update(changes)
    return values


def test_get_and_bounded_read_surfaces() -> None:
    repository = TutorialRepository()
    transaction = ScriptedTransaction(
        one=[step_row()],
        all_rows=[(step_row(),), (step_row(),)],
    )

    assert repository.get(transaction, step_id=7) == TutorialStepRecord(
        step_id=7,
        slug="welcome-tour",
        audience="user",
        display_order=10,
        target_kind="none",
        target_key=None,
        title="Welcome",
        body="A bounded tutorial step.",
        created_at=NOW,
        updated_at=NOW,
        archived_at=None,
    )
    assert len(
        repository.list_visible(
            transaction, audiences=("user", "admin"), include_archived=False, limit=2
        )
    ) == 1
    assert len(
        repository.list_for_administration(
            transaction, include_archived=False, limit=2
        )
    ) == 1
    sql = transaction.fetch_sql()
    assert "audience = ANY" in sql
    assert sql.count("archived_at IS NULL") == 2
    assert transaction.calls[1][2] == (["user", "admin"], 2)


def test_get_returns_none_and_reads_validate_inputs() -> None:
    repository = TutorialRepository()
    assert repository.get(ScriptedTransaction(one=[None]), step_id=1) is None
    with pytest.raises(RepositoryValidationError):
        repository.get(ScriptedTransaction(), step_id=0)
    with pytest.raises(RepositoryValidationError):
        repository.list_visible(ScriptedTransaction(), audiences=(), limit=1)
    with pytest.raises(RepositoryValidationError):
        repository.list_visible(ScriptedTransaction(), audiences=("other",), limit=1)
    with pytest.raises(RepositoryValidationError):
        repository.list_for_administration(ScriptedTransaction(), limit=501)


def test_create_inserts_step_and_revision_in_callers_transaction() -> None:
    transaction = ScriptedTransaction(one=[step_row()], execute=[Result(rowcount=1)])

    record = TutorialRepository().create_with_revision(transaction, **create_args())

    assert record.step_id == 7
    assert [call[0] for call in transaction.calls] == ["one", "execute"]
    assert "ON CONFLICT (slug) DO NOTHING" in transaction.calls[0][1]
    revision_parameters = transaction.calls[1][2]
    assert revision_parameters[0:3] == (7, "admin-1", NOW)
    assert revision_parameters[-1] == "create"


def test_create_reports_duplicate_slug_and_impossible_conflict() -> None:
    repository = TutorialRepository()
    with pytest.raises(RepositoryConflictError) as caught:
        repository.create_with_revision(
            ScriptedTransaction(one=[None, step_row()]), **create_args()
        )
    assert caught.value.metadata == (("slug", "welcome-tour"),)

    with pytest.raises(RepositoryDataError):
        repository.create_with_revision(
            ScriptedTransaction(one=[None, None]), **create_args()
        )


def test_seed_is_idempotent_without_overwriting_existing_copy() -> None:
    repository = TutorialRepository()
    created = repository.create_seed_if_absent(
        ScriptedTransaction(one=[step_row()], execute=[Result(rowcount=1)]),
        **create_args(),
    )
    existing_transaction = ScriptedTransaction(
        one=[None, step_row(title="Edited by admin")]
    )
    existing = repository.create_seed_if_absent(existing_transaction, **create_args())

    assert created.created
    assert not existing.created
    assert existing.record.title == "Edited by admin"
    assert [call[0] for call in existing_transaction.calls] == ["one", "one"]
    with pytest.raises(RepositoryDataError):
        repository.create_seed_if_absent(
            ScriptedTransaction(one=[None, None]), **create_args()
        )


def test_update_applies_only_real_changes_and_records_revision() -> None:
    updated = step_row(
        title="Updated title",
        target_kind="static",
        target_key="chat.input",
        updated_at=LATER,
    )
    transaction = ScriptedTransaction(
        one=[step_row(), updated], execute=[Result(rowcount=1)]
    )

    result = TutorialRepository().update_with_revision(
        transaction,
        step_id=7,
        expected_updated_at=NOW,
        changes={
            "title": "Updated title",
            "target_kind": "static",
            "target_key": "chat.input",
            "body": "A bounded tutorial step.",
        },
        editor_id="admin-1",
        updated_at=LATER,
    )

    assert result.record.title == "Updated title"
    assert result.changed_fields == ("target_kind", "target_key", "title")
    update_parameters = transaction.calls[1][2]
    assert update_parameters[-3:] == (LATER, 7, NOW)
    assert transaction.calls[-1][2][-1] == "update"


def test_update_noop_does_not_create_a_revision() -> None:
    transaction = ScriptedTransaction(one=[step_row()])
    result = TutorialRepository().update_with_revision(
        transaction,
        step_id=7,
        expected_updated_at=NOW,
        changes={"title": "Welcome"},
        editor_id="admin-1",
        updated_at=LATER,
    )
    assert result.changed_fields == ()
    assert len(transaction.calls) == 1


def test_update_rejects_not_found_stale_and_write_race() -> None:
    repository = TutorialRepository()
    arguments = {
        "step_id": 7,
        "expected_updated_at": NOW,
        "changes": {"title": "Changed"},
        "editor_id": "admin-1",
        "updated_at": LATER,
    }
    with pytest.raises(RepositoryNotFoundError):
        repository.update_with_revision(ScriptedTransaction(one=[None]), **arguments)
    with pytest.raises(RepositoryConflictError):
        repository.update_with_revision(
            ScriptedTransaction(one=[step_row(updated_at=NOW - timedelta(seconds=1))]),
            **arguments,
        )
    with pytest.raises(RepositoryConflictError):
        repository.update_with_revision(
            ScriptedTransaction(one=[step_row(), None]), **arguments
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"slug": "cannot-change"},
        {"audience": "other"},
        {"display_order": -1},
        {"target_kind": "other"},
        {"title": ""},
        {"body": ""},
    ],
)
def test_update_rejects_invalid_partial_fields(changes: dict[str, object]) -> None:
    with pytest.raises(RepositoryValidationError):
        TutorialRepository().update_with_revision(
            ScriptedTransaction(),
            step_id=7,
            expected_updated_at=NOW,
            changes=changes,
            editor_id="admin-1",
            updated_at=LATER,
        )


def test_update_validates_combined_target_and_monotonic_timestamp() -> None:
    repository = TutorialRepository()
    with pytest.raises(RepositoryValidationError, match="target_key"):
        repository.update_with_revision(
            ScriptedTransaction(one=[step_row()]),
            step_id=7,
            expected_updated_at=NOW,
            changes={"target_kind": "static"},
            editor_id="admin-1",
            updated_at=LATER,
        )
    with pytest.raises(RepositoryValidationError, match="later"):
        repository.update_with_revision(
            ScriptedTransaction(),
            step_id=7,
            expected_updated_at=NOW,
            changes={},
            editor_id="admin-1",
            updated_at=NOW,
        )


def test_archive_and_restore_are_cas_revisioned_and_idempotent() -> None:
    repository = TutorialRepository()
    archived = step_row(updated_at=LATER, archived_at=LATER)
    transaction = ScriptedTransaction(
        one=[step_row(), archived], execute=[Result(rowcount=1)]
    )
    changed = repository.set_archived_with_revision(
        transaction,
        step_id=7,
        expected_updated_at=NOW,
        archived=True,
        editor_id="admin-1",
        updated_at=LATER,
    )
    assert changed.changed_fields == ("archived_at",)
    assert changed.record.archived_at == LATER
    assert transaction.calls[-1][2][-1] == "archive"

    noop = repository.set_archived_with_revision(
        ScriptedTransaction(one=[archived]),
        step_id=7,
        expected_updated_at=LATER,
        archived=True,
        editor_id="admin-1",
        updated_at=LATER + timedelta(seconds=1),
    )
    assert noop.changed_fields == ()


def test_archive_rejects_invalid_missing_stale_and_write_race() -> None:
    repository = TutorialRepository()
    common = {
        "step_id": 7,
        "expected_updated_at": NOW,
        "archived": True,
        "editor_id": "admin-1",
        "updated_at": LATER,
    }
    with pytest.raises(RepositoryValidationError):
        repository.set_archived_with_revision(
            ScriptedTransaction(), **{**common, "archived": "yes"}
        )
    with pytest.raises(RepositoryNotFoundError):
        repository.set_archived_with_revision(ScriptedTransaction(one=[None]), **common)
    with pytest.raises(RepositoryConflictError):
        repository.set_archived_with_revision(
            ScriptedTransaction(one=[step_row(updated_at=LATER)]), **common
        )
    with pytest.raises(RepositoryConflictError):
        repository.set_archived_with_revision(
            ScriptedTransaction(one=[step_row(), None]), **common
        )


def test_list_revisions_returns_typed_immutable_snapshots() -> None:
    row = {
        "id": 11,
        "step_id": 7,
        "editor_user_id": "admin-1",
        "edited_at": LATER,
        "previous": None,
        "current": {"title": "Welcome"},
        "change_kind": "create",
    }
    records = TutorialRepository().list_revisions(
        ScriptedTransaction(all_rows=[(row,)]), step_id=7, limit=1
    )
    assert records[0].revision_id == 11
    assert records[0].current["title"] == "Welcome"
    with pytest.raises(TypeError):
        records[0].current["title"] = "mutate"  # type: ignore[index]


@pytest.mark.parametrize(
    "changes",
    [
        {"audience": "other"},
        {"target_kind": "none", "target_key": "chat.input"},
        {"target_kind": "static", "target_key": None},
        {"title": "x" * 121},
        {"created_at": NOW.replace(tzinfo=None)},
    ],
)
def test_persisted_step_corruption_fails_closed(changes: dict[str, object]) -> None:
    with pytest.raises(RepositoryDataError):
        TutorialRepository().get(
            ScriptedTransaction(one=[step_row(**changes)]), step_id=7
        )


def test_bad_revision_shape_kind_and_insert_rowcount_fail_closed() -> None:
    base = {
        "id": 11,
        "step_id": 7,
        "editor_user_id": "admin-1",
        "edited_at": LATER,
        "previous": None,
        "current": [],
        "change_kind": "create",
    }
    with pytest.raises(RepositoryDataError):
        TutorialRepository().list_revisions(
            ScriptedTransaction(all_rows=[(base,)]), step_id=7
        )
    with pytest.raises(RepositoryDataError):
        TutorialRepository().list_revisions(
            ScriptedTransaction(
                all_rows=[({**base, "current": {}, "change_kind": "other"},)]
            ),
            step_id=7,
        )
    with pytest.raises(RepositoryDataError):
        TutorialRepository().create_with_revision(
            ScriptedTransaction(one=[step_row()], execute=[Result(rowcount=0)]),
            **create_args(),
        )
