"""Tests for src/astralplane/repositories/workspaces.py:
canvas/layout/snapshot/publication contract tests covering owner scoping, revision
fencing, and atomic rebase/commit at head.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    CanvasRepository,
    LayoutRecord,
    LayoutRepository,
    PublicationRebaseComponent,
    PublicationRebaseLayout,
    PublicationRepository,
    WorkspaceRepository,
    WorkspaceSnapshotRepository,
)

NOW = datetime(2026, 8, 13, 23, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Result:
    rowcount: int = 1
    status_message: str | None = None
    returned_records: tuple[dict[str, Any], ...] = ()


class FakeTransaction:
    def __init__(self) -> None:
        self.execute_results: deque[Result] = deque()
        self.fetch_one_results: deque[dict[str, Any] | None] = deque()
        self.fetch_all_results: deque[tuple[dict[str, Any], ...]] = deque()
        self.calls: list[tuple[str, str, object]] = []

    def execute(self, statement: str, parameters: object = ()) -> Result:
        self.calls.append(("execute", statement, parameters))
        return self.execute_results.popleft() if self.execute_results else Result()

    def fetch_one(self, statement: str, parameters: object = ()) -> dict[str, Any] | None:
        self.calls.append(("fetch_one", statement, parameters))
        return self.fetch_one_results.popleft() if self.fetch_one_results else None

    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[dict[str, Any], ...]:
        self.calls.append(("fetch_all", statement, parameters))
        return self.fetch_all_results.popleft() if self.fetch_all_results else ()


def returned(row: dict[str, Any], *, rowcount: int = 1) -> Result:
    return Result(rowcount=rowcount, returned_records=(row,))


def canvas_record(**changes: Any) -> CanvasComponentRecord:
    values: dict[str, Any] = {
        "row_id": "row-1",
        "conversation_id": "chat-1",
        "owner_id": "owner-1",
        "component_id": "component-1",
        "payload": {"type": "Card", "children": ["hello"]},
        "component_type": "Card",
        "title": "Result",
        "position": 1,
        "created_at": 10,
        "updated_at": 10,
        "publication_id": None,
        "committed_render_revision": None,
    }
    values.update(changes)
    return CanvasComponentRecord(**values)


def canvas_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": "row-1",
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "component_id": "component-1",
        "component_data": '{"children":["hello"],"type":"Card"}',
        "component_type": "Card",
        "title": "Result",
        "position": 1,
        "created_at": 10,
        "updated_at": 10,
        "conversation_commit_id": None,
        "committed_render_revision": None,
    }
    row.update(changes)
    return row


def layout_record(**changes: Any) -> LayoutRecord:
    values: dict[str, Any] = {
        "layout_id": 0,
        "conversation_id": "chat-1",
        "owner_id": "owner-1",
        "layout_key": "round-1",
        "position": 2,
        "tree": [{"component_id": "component-1"}],
        "created_at": 10,
        "updated_at": 10,
        "publication_id": None,
        "committed_render_revision": None,
    }
    values.update(changes)
    return LayoutRecord(**values)


def layout_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": 4,
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "layout_key": "round-1",
        "position": 2,
        "layout": '[{"component_id":"component-1"}]',
        "created_at": 10,
        "updated_at": 10,
        "conversation_commit_id": None,
        "committed_render_revision": None,
    }
    row.update(changes)
    return row


def snapshot_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": 5,
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "turn_message_id": 7,
        "cause": "turn",
        "components": '[{"type":"Card"}]',
        "layouts": '[{"layout_key":"round-1"}]',
        "created_at": 30,
    }
    row.update(changes)
    return row


def publication_row(**changes: Any) -> dict[str, Any]:
    row = {
        "commit_id": "publication-1",
        "chat_id": "chat-1",
        "owner_user_id": "owner-1",
        "request_generation": "request-1",
        "operation_id": None,
        "operation_execution_generation": None,
        "base_render_revision": 0,
        "committed_render_revision": None,
        "state": "staged",
        "publication_role": "atomic",
        "parent_commit_id": None,
        "execution_base_commit_id": None,
        "execution_base_render_revision": None,
        "execution_base_components_sha256": None,
        "execution_base_layouts_sha256": None,
        "publication_rebase_count": 0,
        "started_at": NOW,
        "committed_at": None,
        "aborted_at": None,
    }
    row.update(changes)
    return row


def test_canvas_create_and_exact_replay_preserve_owner_scope() -> None:
    repository = CanvasRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(canvas_row()))
    created = repository.create(transaction, canvas_record())
    assert created.component_id == "component-1"
    assert created.payload["children"] == ("hello",)
    assert transaction.calls[0][2][1:3] == ("chat-1", "owner-1")
    assert "chat.user_id = %s" in transaction.calls[0][1]

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(canvas_row())
    assert repository.create(replay, canvas_record()) == created


def test_canvas_create_distinguishes_owner_missing_scope_and_semantic_conflict() -> None:
    repository = CanvasRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.extend((None, None))
    with pytest.raises(RepositoryNotFoundError):
        repository.create(missing, canvas_record())

    scope = FakeTransaction()
    scope.execute_results.append(Result(rowcount=0))
    scope.fetch_one_results.extend((None, {"id": "chat-1"}))
    with pytest.raises(RepositoryConflictError, match="scope"):
        repository.create(scope, canvas_record())

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(canvas_row(title="Other"))
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create(changed, canvas_record())


@pytest.mark.parametrize(
    "record",
    [
        canvas_record(row_id=""),
        canvas_record(component_id=None),
        canvas_record(component_type=""),
        canvas_record(title=3),
        canvas_record(position=None),
        canvas_record(position=-1),
        canvas_record(updated_at=None),
        canvas_record(created_at=11, updated_at=10),
        canvas_record(publication_id="publication-1"),
        canvas_record(committed_render_revision=1),
        canvas_record(publication_id="publication-1", committed_render_revision=0),
        canvas_record(payload={"bad": float("nan")}),
    ],
)
def test_canvas_create_validates_identity_json_time_and_scope(
    record: CanvasComponentRecord,
) -> None:
    with pytest.raises(RepositoryValidationError):
        CanvasRepository().create(FakeTransaction(), record)


def test_canvas_get_list_replace_and_remove() -> None:
    repository = CanvasRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((canvas_row(), None))
    assert (
        repository.get_scoped(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
        ).row_id
        == "row-1"
    )  # type: ignore[union-attr]
    assert (
        repository.get_scoped(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="missing",
        )
        is None
    )
    query.fetch_all_results.append(
        (
            canvas_row(),
            canvas_row(
                id="row-2",
                component_id="component-2",
                conversation_commit_id="publication-1",
                committed_render_revision=1,
            ),
            canvas_row(
                id="legacy-row",
                component_id=None,
                position=None,
                updated_at=None,
            ),
        )
    )
    records = repository.list_current(
        query, owner_id="owner-1", conversation_id="chat-1"
    )
    assert len(records) == 3
    assert records[-1].component_id is None
    assert records[-1].position is None
    assert records[-1].updated_at is None
    assert "component.user_id = %s" in query.calls[-1][1]

    query.execute_results.append(returned(canvas_row(updated_at=11)))
    replaced = repository.replace(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        component_id="component-1",
        payload={"type": "Card"},
        component_type="Card",
        title=None,
        expected_updated_at=10,
        updated_at=11,
    )
    assert replaced.updated_at == 11
    query.execute_results.extend((Result(rowcount=1), Result(rowcount=0)))
    assert repository.remove(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        component_id="component-1",
    )
    assert not repository.remove(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        component_id="component-1",
    )


def test_canvas_row_identity_reads_and_legacy_delete_preserve_head_scope() -> None:
    repository = CanvasRepository()
    query = FakeTransaction()
    query.fetch_one_results.append(canvas_row())
    current = repository.get_current_by_row_id(
        query,
        owner_id="owner-1",
        row_id="row-1",
        for_update=True,
    )
    assert current is not None and current.component_id == "component-1"
    assert query.calls[0][2] == ("row-1", "owner-1")
    assert "FOR UPDATE OF component" in query.calls[0][1]
    assert "chat.render_revision" in query.calls[0][1]

    query.fetch_all_results.append(
        (
            canvas_row(id="row-2", component_id="component-2", created_at=20),
            canvas_row(),
        )
    )
    listed = repository.list_current_for_owner(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        limit=2,
    )
    assert tuple(record.row_id for record in listed) == ("row-2", "row-1")
    assert query.calls[-1][2] == ("owner-1", "chat-1", "chat-1", 2)
    assert "component.created_at DESC, component.id DESC" in query.calls[-1][1]

    query.execute_results.extend((returned(canvas_row()), Result(rowcount=0)))
    removed = repository.remove_current_by_row_id(
        query,
        owner_id="owner-1",
        row_id="row-1",
    )
    assert removed is not None and removed.conversation_id == "chat-1"
    assert "COALESCE(chat.render_revision, 0) = 0" in query.calls[-1][1]
    assert "component.conversation_commit_id IS NULL" in query.calls[-1][1]
    assert (
        repository.remove_current_by_row_id(
            query,
            owner_id="owner-1",
            row_id="missing",
        )
        is None
    )

    with pytest.raises(RepositoryValidationError, match="for_update"):
        repository.get_current_by_row_id(
            FakeTransaction(),
            owner_id="owner-1",
            row_id="row-1",
            for_update="yes",  # type: ignore[arg-type]
        )


def test_canvas_list_scoped_requires_the_exact_staged_revision_fence() -> None:
    repository = CanvasRepository()
    query = FakeTransaction()
    query.fetch_all_results.append(
        (
            canvas_row(
                conversation_commit_id="publication-1",
                committed_render_revision=3,
            ),
        )
    )
    records = repository.list_scoped(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        committed_render_revision=3,
        expected_base_render_revision=2,
    )
    assert records[0].publication_id == "publication-1"
    assert records[0].committed_render_revision == 3
    assert "publication.state = 'staged'" in query.calls[0][1]
    assert "SELECT component.id, component.chat_id, component.user_id" in " ".join(
        query.calls[0][1].split()
    )
    assert query.calls[0][2] == (
        "chat-1",
        "owner-1",
        "publication-1",
        3,
        2,
    )

    with pytest.raises(RepositoryValidationError, match="follow"):
        repository.list_scoped(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            committed_render_revision=3,
            expected_base_render_revision=1,
        )


def test_canvas_and_layout_exact_publication_reads_are_owner_and_state_scoped() -> None:
    canvas = CanvasRepository()
    layouts = LayoutRepository()
    query = FakeTransaction()
    query.fetch_all_results.extend(
        (
            (
                canvas_row(
                    conversation_commit_id="publication-1",
                    committed_render_revision=4,
                ),
            ),
            (
                layout_row(
                    conversation_commit_id="publication-1",
                    committed_render_revision=4,
                ),
            ),
            (
                layout_row(
                    conversation_commit_id="publication-1",
                    committed_render_revision=4,
                ),
            ),
        )
    )
    component_records = canvas.list_for_publication(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        committed_render_revision=4,
        require_state="committed",
    )
    layout_records = layouts.list_for_publication(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        committed_render_revision=4,
        require_state="committed",
    )
    staged_layouts = layouts.list_scoped(
        query,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        committed_render_revision=4,
        expected_base_render_revision=3,
    )
    assert component_records[0].component_id == "component-1"
    assert layout_records[0].layout_key == "round-1"
    assert staged_layouts[0].committed_render_revision == 4
    assert query.calls[0][2] == (
        "chat-1",
        "owner-1",
        "publication-1",
        4,
        "committed",
        "committed",
    )
    assert "publication.owner_user_id = component.user_id" in query.calls[0][1]
    assert "publication.owner_user_id = layout.user_id" in query.calls[1][1]
    assert "publication.state = 'staged'" in query.calls[2][1]

    with pytest.raises(RepositoryValidationError, match="require_state"):
        canvas.list_for_publication(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            committed_render_revision=4,
            require_state="visible",
        )


def test_canvas_sync_legacy_presence_is_owner_scoped_and_fail_closed() -> None:
    repository = CanvasRepository()
    present = FakeTransaction()
    present.execute_results.append(returned({"has_saved_components": True}))
    assert repository.sync_legacy_presence(
        present,
        owner_id="owner-1",
        conversation_id="chat-1",
    )
    assert "component.user_id = chat.user_id" in present.calls[0][1]
    assert present.calls[0][2] == ("chat-1", "owner-1")

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.sync_legacy_presence(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
        )

    revisioned = FakeTransaction()
    revisioned.execute_results.append(Result(rowcount=0))
    revisioned.fetch_one_results.append({"render_revision": 2})
    with pytest.raises(RepositoryConflictError, match="atomic publication"):
        repository.sync_legacy_presence(
            revisioned,
            owner_id="owner-1",
            conversation_id="chat-1",
        )


def test_canvas_replace_distinguishes_missing_conflict_and_bad_fence() -> None:
    repository = CanvasRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.replace(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            payload={},
            component_type="Card",
            title="",
            expected_updated_at=10,
            updated_at=11,
        )
    conflict = FakeTransaction()
    conflict.execute_results.append(Result(rowcount=0))
    conflict.fetch_one_results.append(canvas_row(updated_at=12))
    with pytest.raises(RepositoryConflictError):
        repository.replace(
            conflict,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            payload={},
            component_type="Card",
            title=None,
            expected_updated_at=10,
            updated_at=11,
        )
    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.replace(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            payload={},
            component_type="Card",
            title=None,
            expected_updated_at=10,
            updated_at=10,
        )


def test_layout_create_replay_and_scope_queries() -> None:
    repository = LayoutRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(layout_row()))
    created = repository.create(transaction, layout_record())
    assert created.tree[0]["component_id"] == "component-1"

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(layout_row())
    assert repository.create(replay, layout_record()) == created

    query = FakeTransaction()
    query.fetch_one_results.extend((layout_row(), None))
    query.fetch_all_results.append((layout_row(),))
    assert (
        repository.get_scoped(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            layout_key="round-1",
        ).layout_id
        == 4
    )  # type: ignore[union-attr]
    assert (
        repository.get_scoped(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            layout_key="missing",
        )
        is None
    )
    assert len(repository.list_current(query, owner_id="owner-1", conversation_id="chat-1")) == 1


def test_layout_create_conflict_and_validation() -> None:
    repository = LayoutRepository()
    unavailable = FakeTransaction()
    unavailable.execute_results.append(Result(rowcount=0))
    unavailable.fetch_one_results.append(None)
    with pytest.raises(RepositoryConflictError, match="scope"):
        repository.create(unavailable, layout_record())

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(layout_row(position=3))
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.create(changed, layout_record())

    for record in (
        layout_record(layout_key=""),
        layout_record(position=-1),
        layout_record(created_at=11, updated_at=10),
        layout_record(tree=[float("nan")]),
    ):
        with pytest.raises(RepositoryValidationError):
            repository.create(FakeTransaction(), record)


def test_canvas_and_layout_creation_accept_zero_based_positions() -> None:
    canvas_transaction = FakeTransaction()
    canvas_transaction.execute_results.append(returned(canvas_row(position=0)))
    assert CanvasRepository().create(canvas_transaction, canvas_record(position=0)).position == 0

    layout_transaction = FakeTransaction()
    layout_transaction.execute_results.append(returned(layout_row(position=0)))
    assert LayoutRepository().create(layout_transaction, layout_record(position=0)).position == 0


def test_layout_replace_remove_and_cas_failures() -> None:
    repository = LayoutRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(layout_row(updated_at=11)))
    assert (
        repository.replace(
            transaction,
            owner_id="owner-1",
            conversation_id="chat-1",
            layout_key="round-1",
            tree=[],
            expected_updated_at=10,
            updated_at=11,
        ).updated_at
        == 11
    )

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.replace(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            layout_key="round-1",
            tree=[],
            expected_updated_at=10,
            updated_at=11,
        )
    conflict = FakeTransaction()
    conflict.execute_results.append(Result(rowcount=0))
    conflict.fetch_one_results.append(layout_row(updated_at=12))
    with pytest.raises(RepositoryConflictError):
        repository.replace(
            conflict,
            owner_id="owner-1",
            conversation_id="chat-1",
            layout_key="round-1",
            tree=[],
            expected_updated_at=10,
            updated_at=11,
        )
    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.replace(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            layout_key="round-1",
            tree=[],
            expected_updated_at=10,
            updated_at=10,
        )

    transaction.execute_results.extend((Result(rowcount=1), Result(rowcount=0)))
    assert repository.remove(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        layout_key="round-1",
    )
    assert not repository.remove(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        layout_key="round-1",
    )


def test_snapshot_capture_get_list_and_empty_layout_compatibility() -> None:
    repository = WorkspaceSnapshotRepository()
    transaction = FakeTransaction()
    transaction.execute_results.extend(
        (
            returned(snapshot_row()),
            returned(snapshot_row(id=6, layouts=None, turn_message_id=None)),
        )
    )
    captured = repository.capture(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        cause="turn",
        components=({"type": "Card"},),
        layouts=({"layout_key": "round-1"},),
        created_at=30,
        turn_message_id=7,
    )
    assert captured.layouts[0]["layout_key"] == "round-1"
    empty = repository.capture(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        cause="turn",
        components=(),
        created_at=31,
    )
    assert empty.layouts == ()

    query = FakeTransaction()
    query.fetch_one_results.extend((snapshot_row(), None))
    query.fetch_all_results.append((snapshot_row(),))
    assert repository.get(query, owner_id="owner-1", snapshot_id=5).snapshot_id == 5  # type: ignore[union-attr]
    assert repository.get(query, owner_id="owner-1", snapshot_id=6) is None
    assert (
        len(
            repository.list_for_conversation(
                query,
                owner_id="owner-1",
                conversation_id="chat-1",
                limit=5,
                offset=3,
            )
        )
        == 1
    )
    assert query.calls[-1][2] == ("chat-1", "owner-1", 5, 3)

    count_query = FakeTransaction()
    count_query.fetch_one_results.append({"snapshot_count": 4})
    assert (
        repository.count_for_conversation(
            count_query,
            owner_id="owner-1",
            conversation_id="chat-1",
        )
        == 4
    )


@pytest.mark.parametrize("offset", [-1, True])
def test_snapshot_listing_rejects_invalid_offsets(offset: object) -> None:
    with pytest.raises(RepositoryValidationError):
        WorkspaceSnapshotRepository().list_for_conversation(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            offset=offset,  # type: ignore[arg-type]
        )


def test_snapshot_missing_validation_and_corrupt_payload_are_visible() -> None:
    repository = WorkspaceSnapshotRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    with pytest.raises(RepositoryNotFoundError):
        repository.capture(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            cause="turn",
            components=(),
            created_at=1,
        )
    with pytest.raises(RepositoryValidationError):
        repository.capture(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            cause="",
            components=(),
            created_at=1,
        )
    corrupt = FakeTransaction()
    corrupt.fetch_one_results.append(snapshot_row(components='{"not":"array"}'))
    with pytest.raises(RepositoryDataError, match="arrays"):
        repository.get(corrupt, owner_id="owner-1", snapshot_id=5)
    corrupt.fetch_one_results.append(snapshot_row(layouts="broken"))
    with pytest.raises(RepositoryDataError, match="valid JSON"):
        repository.get(corrupt, owner_id="owner-1", snapshot_id=5)


def test_publication_stage_success_and_exact_replay() -> None:
    repository = PublicationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(
        returned(publication_row(operation_id="operation-1", operation_execution_generation=1))
    )
    staged = repository.stage(
        transaction,
        publication_id="publication-1",
        owner_id="owner-1",
        conversation_id="chat-1",
        request_generation="request-1",
        base_render_revision=0,
        started_at=NOW,
        operation_id="operation-1",
        operation_execution_generation=1,
    )
    assert staged.state == "staged"
    assert "user_id = %s" in transaction.calls[0][1]

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(
        publication_row(operation_id="operation-1", operation_execution_generation=1)
    )
    assert (
        repository.stage(
            replay,
            publication_id="publication-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            request_generation="request-1",
            base_render_revision=0,
            started_at=NOW,
            operation_id="operation-1",
            operation_execution_generation=1,
        )
        == staged
    )


def test_publication_stage_binds_exact_voice_lineage_and_digests() -> None:
    digest = "a" * 64
    repository = PublicationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(
        returned(
            publication_row(
                base_render_revision=1,
                publication_role="assistant_result",
                parent_commit_id="acceptance-1",
                execution_base_commit_id="acceptance-1",
                execution_base_render_revision=1,
                execution_base_components_sha256=digest,
                execution_base_layouts_sha256=digest,
            )
        )
    )
    record = repository.stage(
        transaction,
        publication_id="publication-1",
        owner_id="owner-1",
        conversation_id="chat-1",
        request_generation="request-1",
        base_render_revision=1,
        started_at=NOW,
        publication_role="assistant_result",
        parent_publication_id="acceptance-1",
        execution_base_publication_id="acceptance-1",
        execution_base_render_revision=1,
        execution_base_components_sha256=digest,
        execution_base_layouts_sha256=digest,
    )
    assert record.parent_publication_id == "acceptance-1"
    assert record.execution_base_publication_id == "acceptance-1"
    assert transaction.calls[0][2][7:13] == (
        "assistant_result",
        "acceptance-1",
        "acceptance-1",
        1,
        digest,
        digest,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"publication_role": "invalid"},
        {"parent_publication_id": "parent-1"},
        {
            "publication_role": "user_acceptance",
            "execution_base_render_revision": 1,
            "execution_base_components_sha256": "a" * 64,
            "execution_base_layouts_sha256": "a" * 64,
        },
        {
            "publication_role": "assistant_result",
            "parent_publication_id": "parent-1",
            "execution_base_publication_id": "parent-1",
            "execution_base_render_revision": 1,
            "execution_base_components_sha256": "A" * 64,
            "execution_base_layouts_sha256": "a" * 64,
        },
    ],
)
def test_publication_stage_rejects_incomplete_voice_metadata(
    changes: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "publication_id": "publication-1",
        "owner_id": "owner-1",
        "conversation_id": "chat-1",
        "request_generation": "request-1",
        "base_render_revision": 0,
        "started_at": NOW,
    }
    values.update(changes)
    with pytest.raises(RepositoryValidationError):
        PublicationRepository().stage(FakeTransaction(), **values)


def test_publication_stage_missing_revision_and_semantic_conflicts() -> None:
    repository = PublicationRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.extend((None, None))
    with pytest.raises(RepositoryNotFoundError):
        repository.stage(
            missing,
            publication_id="publication-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            request_generation="request-1",
            base_render_revision=0,
            started_at=NOW,
        )

    stale = FakeTransaction()
    stale.execute_results.append(Result(rowcount=0))
    stale.fetch_one_results.extend((None, {"id": "chat-1"}))
    with pytest.raises(RepositoryConflictError, match="revision"):
        repository.stage(
            stale,
            publication_id="publication-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            request_generation="request-1",
            base_render_revision=0,
            started_at=NOW,
        )

    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(publication_row(commit_id="other"))
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.stage(
            changed,
            publication_id="publication-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            request_generation="request-1",
            base_render_revision=0,
            started_at=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"started_at": datetime(2026, 1, 1)},
        {"operation_execution_generation": 0},
        {"base_render_revision": -1},
    ],
)
def test_publication_stage_validates_time_and_fences(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "publication_id": "publication-1",
        "owner_id": "owner-1",
        "conversation_id": "chat-1",
        "request_generation": "request-1",
        "base_render_revision": 0,
        "started_at": NOW,
        "operation_id": "operation-1",
        "operation_execution_generation": 1,
    }
    values.update(changes)
    with pytest.raises(RepositoryValidationError):
        PublicationRepository().stage(FakeTransaction(), **values)


def test_publication_queries_and_corrupt_timestamp_visibility() -> None:
    repository = PublicationRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((publication_row(), publication_row(), None))
    assert (
        repository.get(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            for_update=True,
        ).publication_id
        == "publication-1"
    )  # type: ignore[union-attr]
    assert "FOR UPDATE" in query.calls[0][1]
    assert (
        repository.get_by_request(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            request_generation="request-1",
        ).request_generation
        == "request-1"
    )  # type: ignore[union-attr]
    assert (
        repository.get(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="missing",
        )
        is None
    )
    corrupt = FakeTransaction()
    corrupt.fetch_one_results.append(publication_row(started_at=datetime(2026, 1, 1)))
    with pytest.raises(RepositoryDataError, match="timezone"):
        repository.get(
            corrupt,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
        )


def test_publication_owner_resolver_and_committed_assistant_projection() -> None:
    repository = PublicationRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend(
        (
            publication_row(),
            {
                "message_id": 8,
                "chat_id": "chat-1",
                "user_id": "owner-1",
                "conversation_commit_id": "publication-1",
                "commit_position": 2,
                "committed_render_revision": 4,
                "content": '{"type":"text","content":"done"}',
            },
            None,
        )
    )
    resolved = repository.get_for_owner(
        query,
        owner_id="owner-1",
        publication_id="publication-1",
        for_update=True,
    )
    content = repository.get_latest_committed_assistant_content(
        query,
        owner_id="owner-1",
        publication_id="publication-1",
    )
    missing = repository.get_latest_committed_assistant_content(
        query,
        owner_id="owner-1",
        publication_id="missing",
    )
    assert resolved is not None and resolved.conversation_id == "chat-1"
    assert "FOR UPDATE" in query.calls[0][1]
    assert query.calls[0][2] == ("publication-1", "owner-1")
    assert content is not None and content.content["content"] == "done"
    assert content.position == 2
    assert "publication.state = 'committed'" in query.calls[1][1]
    assert "message.user_id = publication.owner_user_id" in query.calls[1][1]
    assert missing is None


def test_publication_validate_stage_returns_bounded_owner_scoped_fences() -> None:
    repository = PublicationRepository()
    transaction = FakeTransaction()
    transaction.fetch_one_results.append(publication_row())
    transaction.fetch_all_results.extend(
        (
            (
                {"commit_position": 0, "committed_render_revision": 1},
                {"commit_position": 1, "committed_render_revision": 1},
            ),
            (
                {
                    "component_id": "component-1",
                    "position": 0,
                    "committed_render_revision": 1,
                },
            ),
            (
                {
                    "layout_key": "layout-1",
                    "position": 2,
                    "committed_render_revision": 1,
                },
            ),
        )
    )
    summary = repository.validate_stage(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
    )
    assert [fence.position for fence in summary.messages] == [0, 1]
    assert summary.components[0].identity == "component-1"
    assert summary.layouts[0].identity == "layout-1"
    assert all(
        call[2][0:3] == ("publication-1", "chat-1", "owner-1")
        for call in transaction.calls[1:]
    )


def test_publication_rebase_replaces_complete_stage_and_appends_notice_atomically() -> None:
    repository = PublicationRepository()
    staged = publication_row(
        base_render_revision=1,
        publication_role="assistant_result",
    )
    transaction = FakeTransaction()
    transaction.fetch_one_results.extend(
        (
            staged,
            {"render_revision": 2, "conversation_commit_id": "head-2"},
            {"observed_at": NOW},
        )
    )
    transaction.fetch_all_results.extend(
        (
            (
                {
                    "id": 7,
                    "role": "assistant",
                    "content": "draft result",
                    "commit_position": 0,
                    "committed_render_revision": 3,
                },
            ),
            (
                {
                    "id": "row-old",
                    "component_id": "component-old",
                    "component_data": '{"type":"Card"}',
                    "component_type": "Card",
                    "title": "Old",
                    "position": 0,
                    "committed_render_revision": 3,
                },
            ),
            (
                {
                    "id": 4,
                    "layout_key": "layout-old",
                    "position": 0,
                    "layout": "[]",
                    "committed_render_revision": 3,
                },
            ),
        )
    )
    transaction.execute_results.extend(
        (
            returned(
                publication_row(
                    base_render_revision=1,
                    publication_role="assistant_result",
                    publication_rebase_count=1,
                )
            ),
            Result(rowcount=1),
            Result(rowcount=1),
            returned({"id": "row-new"}),
            returned({"id": 5}),
            Result(rowcount=1),
            Result(rowcount=1),
        )
    )
    summary = repository.rebase_assistant_stage(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        expected_staged_base_render_revision=1,
        expected_head_render_revision=2,
        expected_head_publication_id="head-2",
        components=(
            PublicationRebaseComponent(
                row_id="row-new",
                component_id="component-new",
                payload={"type": "Card", "children": ["new"]},
                component_type="Card",
                title="New",
                position=0,
            ),
        ),
        layouts=(
            PublicationRebaseLayout(
                layout_key="layout-new",
                position=2,
                tree=[{"component_id": "component-new"}],
            ),
        ),
        append_conflict_notice=True,
    )
    assert summary.messages[0].committed_render_revision == 3
    assert summary.components[0].identity == "component-new"
    assert summary.layouts[0].identity == "layout-new"
    assert summary.layouts[0].position == 2
    execute_calls = [call for call in transaction.calls if call[0] == "execute"]
    assert len(execute_calls) == 7
    assert "publication_rebase_count = publication_rebase_count + 1" in (
        execute_calls[0][1]
    )
    assert all(
        "conversation_commit_id = %s" in call[1] for call in execute_calls[1:3]
    )
    assert execute_calls[3][2][-3:] == ("publication-1", "chat-1", "owner-1")
    notice_payload = execute_calls[-1][2][0]
    assert "Some canvas changes" in notice_payload
    assert execute_calls[-1][2][3:6] == (
        "publication-1",
        "chat-1",
        "owner-1",
    )


def test_publication_rebase_exact_replay_is_write_free_and_notice_safe() -> None:
    repository = PublicationRepository()
    notice_content = (
        '[{"content":"draft result","type":"text"},'
        '{"message":"Some canvas changes from this result conflicted with newer work. '
        'The newer canvas version was preserved.","type":"alert","variant":"warning"}]'
    )
    transaction = FakeTransaction()
    transaction.fetch_one_results.extend(
        (
            publication_row(
                base_render_revision=1,
                publication_role="assistant_result",
                publication_rebase_count=1,
            ),
            {"render_revision": 2, "conversation_commit_id": "head-2"},
        )
    )
    transaction.fetch_all_results.extend(
        (
            (
                {
                    "id": 7,
                    "role": "assistant",
                    "content": notice_content,
                    "commit_position": 0,
                    "committed_render_revision": 3,
                },
            ),
            (
                {
                    "id": "row-new",
                    "component_id": "component-new",
                    "component_data": '{"children":["new"],"type":"Card"}',
                    "component_type": "Card",
                    "title": "New",
                    "position": 0,
                    "committed_render_revision": 3,
                },
            ),
            (
                {
                    "id": 5,
                    "layout_key": "layout-new",
                    "position": 0,
                    "layout": '[{"component_id":"component-new"}]',
                    "committed_render_revision": 3,
                },
            ),
        )
    )
    summary = repository.rebase_assistant_stage(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        expected_staged_base_render_revision=1,
        expected_head_render_revision=2,
        expected_head_publication_id="head-2",
        components=(
            PublicationRebaseComponent(
                row_id="row-regenerated-on-retry",
                component_id="component-new",
                payload={"type": "Card", "children": ["new"]},
                component_type="Card",
                title="New",
                position=0,
            ),
        ),
        layouts=(
            PublicationRebaseLayout(
                layout_key="layout-new",
                position=0,
                tree=[{"component_id": "component-new"}],
            ),
        ),
        append_conflict_notice=True,
    )
    assert summary.messages[0].committed_render_revision == 3
    assert not any(call[0] == "execute" for call in transaction.calls)


def test_publication_rebase_rejects_incomplete_or_reused_generations() -> None:
    repository = PublicationRepository()
    incomplete = FakeTransaction()
    incomplete.fetch_one_results.extend(
        (
            publication_row(
                base_render_revision=1,
                publication_role="assistant_result",
            ),
            {"render_revision": 2, "conversation_commit_id": "head-2"},
        )
    )
    incomplete.fetch_all_results.extend(
        (
            (
                {
                    "id": 7,
                    "role": "assistant",
                    "content": "draft",
                    "commit_position": 0,
                    "committed_render_revision": 2,
                },
                {
                    "id": 8,
                    "role": "assistant",
                    "content": "draft",
                    "commit_position": 2,
                    "committed_render_revision": 2,
                },
            ),
            (),
            (),
        )
    )
    with pytest.raises(RepositoryDataError, match="incomplete"):
        repository.rebase_assistant_stage(
            incomplete,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=1,
            expected_head_render_revision=2,
            expected_head_publication_id="head-2",
            components=(),
            layouts=(),
            append_conflict_notice=False,
        )

    reused = FakeTransaction()
    reused.fetch_one_results.extend(
        (
            publication_row(
                base_render_revision=1,
                publication_role="assistant_result",
                publication_rebase_count=1,
            ),
            {"render_revision": 2, "conversation_commit_id": "head-2"},
        )
    )
    reused.fetch_all_results.extend(
        (
            (
                {
                    "id": 7,
                    "role": "assistant",
                    "content": "draft",
                    "commit_position": 0,
                    "committed_render_revision": 3,
                },
            ),
            (),
            (),
        )
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.rebase_assistant_stage(
            reused,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=1,
            expected_head_render_revision=2,
            expected_head_publication_id="head-2",
            components=(
                PublicationRebaseComponent(
                    row_id="row-new",
                    component_id="component-new",
                    payload={},
                    component_type="Card",
                    title=None,
                    position=0,
                ),
            ),
            layouts=(),
            append_conflict_notice=False,
        )

    with pytest.raises(RepositoryValidationError, match="positions"):
        repository.rebase_assistant_stage(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=1,
            expected_head_render_revision=2,
            expected_head_publication_id="head-2",
            components=(
                PublicationRebaseComponent(
                    row_id="row-new",
                    component_id="component-new",
                    payload={},
                    component_type="Card",
                    title=None,
                    position=1,
                ),
            ),
            layouts=(),
            append_conflict_notice=False,
        )


def test_publication_commit_at_head_is_atomic_and_replay_safe() -> None:
    repository = PublicationRepository()
    staged = publication_row()
    committed = publication_row(
        state="committed",
        committed_render_revision=1,
        committed_at=NOW + timedelta(seconds=1),
    )
    transaction = FakeTransaction()
    transaction.fetch_one_results.extend(
        (staged, {"render_revision": 0, "conversation_commit_id": None})
    )
    transaction.execute_results.extend((returned(committed), Result(rowcount=1)))
    record = repository.commit_at_head(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        expected_staged_base_render_revision=0,
        expected_head_render_revision=0,
        expected_head_publication_id=None,
        committed_at=NOW + timedelta(seconds=1),
        updated_at=50,
    )
    assert record.state == "committed"
    assert "FOR UPDATE" in transaction.calls[0][1]
    assert "render_revision = %s" in transaction.calls[3][1]
    assert transaction.calls[3][2][-4:] == ("chat-1", "owner-1", 0, None)

    replay = FakeTransaction()
    replay.fetch_one_results.extend((committed, {"matched": 1}))
    assert (
        repository.commit_at_head(
            replay,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=0,
            expected_head_render_revision=0,
            expected_head_publication_id=None,
            committed_at=NOW + timedelta(seconds=2),
            updated_at=51,
        ).state
        == "committed"
    )


def test_publication_commit_at_head_failures_are_typed_for_outer_rollback() -> None:
    repository = PublicationRepository()
    staged_row = publication_row()
    committed = publication_row(state="committed", committed_render_revision=1, committed_at=NOW)
    cas = FakeTransaction()
    cas.fetch_one_results.extend(
        (staged_row, {"render_revision": 0, "conversation_commit_id": None})
    )
    cas.execute_results.extend((returned(committed), Result(rowcount=0)))
    with pytest.raises(RepositoryConflictError, match="authority changed"):
        repository.commit_at_head(
            cas,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=0,
            expected_head_render_revision=0,
            expected_head_publication_id=None,
            committed_at=NOW,
            updated_at=50,
        )

    missing = FakeTransaction()
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.commit_at_head(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=0,
            expected_head_render_revision=0,
            expected_head_publication_id=None,
            committed_at=NOW,
            updated_at=50,
        )

    mismatch = FakeTransaction()
    mismatch.fetch_one_results.append(publication_row(base_render_revision=1))
    with pytest.raises(RepositoryConflictError, match="expected staged"):
        repository.commit_at_head(
            mismatch,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=0,
            expected_head_render_revision=0,
            expected_head_publication_id=None,
            committed_at=NOW,
            updated_at=50,
        )

    changed_head = FakeTransaction()
    changed_head.fetch_one_results.extend(
        (staged_row, {"render_revision": 1, "conversation_commit_id": "other"})
    )
    with pytest.raises(RepositoryConflictError, match="head changed"):
        repository.commit_at_head(
            changed_head,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            expected_staged_base_render_revision=0,
            expected_head_render_revision=0,
            expected_head_publication_id=None,
            committed_at=NOW,
            updated_at=50,
        )


def test_assistant_result_commit_rebases_only_against_the_exact_head() -> None:
    digest = "a" * 64
    repository = PublicationRepository()
    staged = publication_row(
        base_render_revision=1,
        publication_role="assistant_result",
        parent_commit_id="acceptance-1",
        execution_base_commit_id="acceptance-1",
        execution_base_render_revision=1,
        execution_base_components_sha256=digest,
        execution_base_layouts_sha256=digest,
    )
    committed = publication_row(
        base_render_revision=3,
        committed_render_revision=4,
        state="committed",
        publication_role="assistant_result",
        parent_commit_id="acceptance-1",
        execution_base_commit_id=None,
        execution_base_render_revision=1,
        execution_base_components_sha256=digest,
        execution_base_layouts_sha256=digest,
        publication_rebase_count=1,
        committed_at=NOW,
    )
    transaction = FakeTransaction()
    transaction.fetch_one_results.extend(
        (staged, {"render_revision": 3, "conversation_commit_id": "head-3"})
    )
    transaction.execute_results.extend((returned(committed), Result(rowcount=1)))
    record = repository.commit_at_head(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        expected_staged_base_render_revision=1,
        expected_head_render_revision=3,
        expected_head_publication_id="head-3",
        committed_at=NOW,
        updated_at=50,
    )
    assert record.publication_rebase_count == 1
    assert record.execution_base_publication_id is None
    assert "execution_base_commit_id = NULL" in transaction.calls[2][1]


def test_publication_abort_cleans_staged_rows_and_is_idempotent() -> None:
    repository = PublicationRepository()
    aborted = publication_row(state="aborted", aborted_at=NOW)
    transaction = FakeTransaction()
    transaction.execute_results.extend((returned(aborted), Result(), Result(), Result()))
    record = repository.abort(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        publication_id="publication-1",
        aborted_at=NOW,
    )
    assert record.state == "aborted"
    assert "execution_base_commit_id = NULL" in transaction.calls[0][1]
    cleanup_sql = "\n".join(call[1] for call in transaction.calls[1:])
    assert "saved_components" in cleanup_sql
    assert "workspace_layout" in cleanup_sql
    assert "messages" in cleanup_sql
    assert all(call[2] == ("publication-1", "chat-1", "owner-1") for call in transaction.calls[1:])

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(aborted)
    assert (
        repository.abort(
            replay,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            aborted_at=NOW,
        ).state
        == "aborted"
    )


def test_publication_abort_rejects_missing_or_committed_rows() -> None:
    repository = PublicationRepository()
    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.abort(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            aborted_at=NOW,
        )
    committed = FakeTransaction()
    committed.execute_results.append(Result(rowcount=0))
    committed.fetch_one_results.append(
        publication_row(state="committed", committed_render_revision=1, committed_at=NOW)
    )
    with pytest.raises(RepositoryConflictError, match="cannot be aborted"):
        repository.abort(
            committed,
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            aborted_at=NOW,
        )
    with pytest.raises(RepositoryValidationError):
        repository.abort(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            publication_id="publication-1",
            aborted_at=datetime(2026, 1, 1),
        )


def test_workspace_facade_exposes_neutral_stores() -> None:
    facade = WorkspaceRepository()
    assert isinstance(facade.canvas, CanvasRepository)
    assert isinstance(facade.layouts, LayoutRepository)
    assert isinstance(facade.snapshots, WorkspaceSnapshotRepository)
    assert isinstance(facade.publications, PublicationRepository)
