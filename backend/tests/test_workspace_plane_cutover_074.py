"""Focused Plane-boundary tests for the Deep workspace coordinator."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    LayoutRecord,
    WorkspaceSnapshotRecord,
)
from orchestrator.conversation_publication import (
    ConversationPublicationStage,
    activate_conversation_publication,
    reset_conversation_publication,
)
from orchestrator.workspace import WorkspaceManager


class CanvasStore:
    def __init__(self) -> None:
        self.records: list[CanvasComponentRecord] = []
        self.calls: list[tuple[str, object]] = []

    def list_current(self, transaction, **_scope):
        self.calls.append(("list_current", transaction))
        return tuple(self.records)

    def list_scoped(self, transaction, **_scope):
        self.calls.append(("list_scoped", transaction))
        return tuple(self.records)

    def create(self, transaction, *, record):
        self.calls.append(("create", transaction))
        self.records.append(record)
        return record

    def replace(self, transaction, *, component_id, payload, component_type,
                title, updated_at, **_scope):
        self.calls.append(("replace", transaction))
        index = next(
            i for i, record in enumerate(self.records)
            if record.component_id == component_id
        )
        self.records[index] = replace(
            self.records[index],
            payload=payload,
            component_type=component_type,
            title=title,
            updated_at=updated_at,
        )
        return self.records[index]

    def remove(self, transaction, *, component_id, **_scope):
        self.calls.append(("remove", transaction))
        before = len(self.records)
        self.records = [
            record for record in self.records
            if record.component_id != component_id
        ]
        return len(self.records) != before

    def sync_legacy_presence(self, transaction, **_scope):
        self.calls.append(("sync", transaction))
        return bool(self.records)


class LayoutStore:
    def __init__(self) -> None:
        self.records: list[LayoutRecord] = []
        self.calls: list[tuple[str, object]] = []

    def list_current(self, transaction, **_scope):
        self.calls.append(("list_current", transaction))
        return tuple(self.records)

    def get_scoped(self, transaction, *, layout_key, **_scope):
        self.calls.append(("get_scoped", transaction))
        return next(
            (record for record in self.records if record.layout_key == layout_key),
            None,
        )

    def create(self, transaction, *, record):
        self.calls.append(("create", transaction))
        self.records.append(record)
        return record

    def replace(self, transaction, *, layout_key, tree, updated_at, **_scope):
        self.calls.append(("replace", transaction))
        index = next(
            i for i, record in enumerate(self.records)
            if record.layout_key == layout_key
        )
        self.records[index] = replace(
            self.records[index], tree=tree, updated_at=updated_at
        )
        return self.records[index]


class SnapshotStore:
    def __init__(self) -> None:
        self.records: list[WorkspaceSnapshotRecord] = []
        self.calls: list[tuple[str, object]] = []

    def capture(self, transaction, *, owner_id, conversation_id, cause,
                components, layouts, created_at, turn_message_id):
        self.calls.append(("capture", transaction))
        record = WorkspaceSnapshotRecord(
            snapshot_id=len(self.records) + 1,
            conversation_id=conversation_id,
            owner_id=owner_id,
            turn_message_id=turn_message_id,
            cause=cause,
            components=tuple(components),
            layouts=tuple(layouts),
            created_at=created_at,
        )
        self.records.append(record)
        return record

    def list_for_conversation(self, transaction, *, limit, offset, **_scope):
        self.calls.append(("list", transaction))
        return tuple(reversed(self.records))[offset : offset + limit]

    def count_for_conversation(self, transaction, **_scope):
        self.calls.append(("count", transaction))
        return len(self.records)

    def get(self, transaction, *, owner_id, snapshot_id):
        self.calls.append(("get", transaction))
        return next(
            (
                record
                for record in self.records
                if record.owner_id == owner_id
                and record.snapshot_id == snapshot_id
            ),
            None,
        )


class ArtifactVersions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def delete_for_component(self, transaction, **_scope):
        self.calls.append(("delete", transaction))
        return 1


class Runtime:
    def __init__(self, repositories, *, fail_commit: bool = False) -> None:
        self.repositories = repositories
        self.fail_commit = fail_commit
        self.transactions: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def transaction(self):
        transaction = object()
        self.transactions.append(transaction)
        try:
            yield transaction
            if self.fail_commit:
                raise RuntimeError("commit failed")
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1


def manager(*, fail_commit: bool = False):
    canvas = CanvasStore()
    layouts = LayoutStore()
    snapshots = SnapshotStore()
    versions = ArtifactVersions()
    conversations = SimpleNamespace(
        get=lambda transaction, **_scope: SimpleNamespace(render_revision=0)
    )
    repositories = SimpleNamespace(
        workspaces=SimpleNamespace(
            canvas=canvas,
            layouts=layouts,
            snapshots=snapshots,
        ),
        history=SimpleNamespace(conversations=conversations),
        artifacts=SimpleNamespace(versions=versions),
    )
    runtime = Runtime(repositories, fail_commit=fail_commit)
    history = SimpleNamespace()
    return (
        WorkspaceManager(
            history,
            plane_runtime=runtime,
            plane_repositories=repositories,
        ),
        runtime,
        canvas,
        layouts,
        snapshots,
        versions,
        history,
    )


def test_workspace_fails_closed_without_the_application_plane_runtime() -> None:
    workspace = WorkspaceManager(SimpleNamespace())
    with pytest.raises(RuntimeError, match="application AstralPlane runtime"):
        workspace.live_rows("chat-1", "owner-1")


def test_workspace_upsert_and_remove_share_one_plane_transaction_per_mutation() -> None:
    workspace, runtime, canvas, layouts, _snapshots, versions, _history = manager()
    ops = workspace.upsert(
        "chat-1",
        "owner-1",
        [{"type": "card", "title": "Original"}],
    )
    component_id = ops[0]["component_id"]
    assert runtime.commits == 1
    assert {transaction for _name, transaction in canvas.calls} == {
        runtime.transactions[0]
    }

    layouts.records.append(
        LayoutRecord(
            layout_id=1,
            conversation_id="chat-1",
            owner_id="owner-1",
            layout_key="layout-1",
            position=2,
            tree=(
                MappingProxyType(
                    {"type": "ref", "component_id": component_id}
                ),
            ),
            created_at=10,
            updated_at=10,
        )
    )
    assert workspace.remove("chat-1", "owner-1", component_id)
    removal_transaction = runtime.transactions[1]
    removal_calls = (
        canvas.calls[3:]
        + layouts.calls
        + versions.calls
    )
    assert removal_calls
    assert {transaction for _name, transaction in removal_calls} == {
        removal_transaction
    }
    assert layouts.records[0].tree == []
    assert not canvas.records


def test_workspace_snapshot_round_trip_thaws_plane_records() -> None:
    workspace, runtime, canvas, layouts, snapshots, _versions, _history = manager()
    canvas.records.append(
        CanvasComponentRecord(
            row_id="row-1",
            conversation_id="chat-1",
            owner_id="owner-1",
            component_id="component-1",
            payload=MappingProxyType(
                {"type": "card", "children": ("one", "two")}
            ),
            component_type="card",
            title="Card",
            position=1,
            created_at=10,
            updated_at=10,
        )
    )
    layouts.records.append(
        LayoutRecord(
            layout_id=1,
            conversation_id="chat-1",
            owner_id="owner-1",
            layout_key="layout-1",
            position=2,
            tree=(MappingProxyType({"type": "ref", "component_id": "component-1"}),),
            created_at=10,
            updated_at=10,
        )
    )
    snapshot_id = workspace.snapshot(
        "chat-1", "owner-1", "turn", turn_message_id=7
    )
    assert snapshot_id == 1
    assert len(runtime.transactions) == 1
    assert snapshots.calls == [("capture", runtime.transactions[0])]
    assert workspace.count_snapshots("chat-1", "owner-1") == 1
    assert workspace.list_snapshots("chat-1", "owner-1")[0]["id"] == 1
    restored = workspace.get_snapshot(1, "owner-1")
    assert restored is not None
    assert restored["components"][0]["children"] == ["one", "two"]
    assert restored["layouts"][0]["layout"][0]["component_id"] == "component-1"


def test_failed_plane_commit_restores_task_local_stage_metadata() -> None:
    workspace, runtime, _canvas, _layouts, _snapshots, _versions, history = manager(
        fail_commit=True
    )
    chat_id = str(uuid.uuid4())
    stage = ConversationPublicationStage(
        history=history,
        commit_id=str(uuid.uuid4()),
        chat_id=chat_id,
        user_id="owner-1",
        base_render_revision=0,
        next_render_revision=1,
        layouts=[{"layout_key": "old", "position": 1, "layout": []}],
    )
    token = activate_conversation_publication(stage)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            workspace.upsert_layout(
                chat_id,
                "owner-1",
                "new",
                [{"type": "ref", "component_id": "component-1"}],
            )
    finally:
        reset_conversation_publication(token)
    assert runtime.rollbacks == 1
    assert stage.layouts == [{"layout_key": "old", "position": 1, "layout": []}]
    assert not stage.dirty
    assert stage.snapshot_cause is None
