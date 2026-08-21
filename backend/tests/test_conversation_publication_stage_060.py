"""Feature 060 durable staged-canvas publication seam tests.

These tests use an isolated PostgreSQL database because the defining safety
property is visibility across committed transactions: workspace mutations may
durably prepare the next conversation revision while every reader outside the
active task still sees the prior complete canvas.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from orchestrator.conversation_publication import (
    ConversationPublicationStage,
    activate_conversation_publication,
    current_conversation_publication,
    reset_conversation_publication,
)
from orchestrator.history import HistoryManager
from orchestrator.workspace import WorkspaceManager, iter_layout_refs
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


OWNER = "canvas-owner-060"
CHAT_ID = "33333333-3333-4333-8333-333333333333"
COMMIT_ID = "44444444-4444-4444-8444-444444444444"
REQUEST_GENERATION = "55555555-5555-4555-8555-555555555555"


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("canvas_stage") as runtime:
        yield runtime


@pytest.fixture
def database(postgres_database: PlaneTestRuntime) -> PlaneTestRuntime:
    with postgres_database.transaction() as transaction:
        transaction.execute("DELETE FROM workspace_snapshot")
        transaction.execute("DELETE FROM workspace_layout")
        transaction.execute("DELETE FROM saved_components")
        transaction.execute("UPDATE chats SET conversation_commit_id = NULL")
        transaction.execute("DELETE FROM conversation_commit")
        transaction.execute("DELETE FROM chats")
    return postgres_database


def _manager(database: PlaneTestRuntime) -> tuple[HistoryManager, WorkspaceManager]:
    history = HistoryManager(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )
    return history, WorkspaceManager(
        history,
        plane_runtime=database,
        plane_repositories=database.repositories,
    )


def _seed_authoritative_canvas(database: PlaneTestRuntime) -> None:
    database.execute(
        "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
        "VALUES (?, ?, 'Canvas staging', 1, 1)",
        (CHAT_ID, OWNER),
    )
    for position, component_id in enumerate(("component-a", "component-b")):
        component = {
            "type": "text",
            "component_id": component_id,
            "content": f"authoritative-{component_id}",
        }
        database.execute(
            "INSERT INTO saved_components ("
            "id, chat_id, user_id, component_data, component_type, title, "
            "created_at, component_id, position, updated_at"
            ") VALUES (?, ?, ?, ?, 'text', ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                CHAT_ID,
                OWNER,
                json.dumps(component),
                component_id,
                position + 1,
                component_id,
                position + 1,
                position + 1,
            ),
        )
    database.execute(
        "INSERT INTO workspace_layout ("
        "chat_id, user_id, layout_key, position, layout, created_at, updated_at"
        ") VALUES (?, ?, 'authoritative-layout', 3, ?, 1, 1)",
        (
            CHAT_ID,
            OWNER,
            json.dumps(
                [
                    {
                        "type": "stack",
                        "children": [
                            {"type": "ref", "component_id": "component-a"},
                            {"type": "ref", "component_id": "component-b"},
                        ],
                    }
                ]
            ),
        ),
    )


def _stage_complete_copy(
    database: PlaneTestRuntime,
    history: HistoryManager,
    manager: WorkspaceManager,
) -> ConversationPublicationStage:
    authoritative_rows = manager.live_rows(CHAT_ID, OWNER)
    authoritative_layouts = manager.live_layouts(CHAT_ID, OWNER)
    database.execute(
        "INSERT INTO conversation_commit ("
        "commit_id, chat_id, owner_user_id, request_generation, "
        "base_render_revision, state"
        ") VALUES (?, ?, ?, ?, 0, 'staged')",
        (COMMIT_ID, CHAT_ID, OWNER, REQUEST_GENERATION),
    )
    for row in authoritative_rows:
        database.execute(
            "INSERT INTO saved_components ("
            "id, chat_id, user_id, component_data, component_type, title, "
            "created_at, component_id, position, updated_at, "
            "conversation_commit_id, committed_render_revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                str(uuid.uuid4()),
                CHAT_ID,
                OWNER,
                json.dumps(row["component_data"]),
                row["component_type"],
                row["title"],
                row["created_at"],
                row["component_id"],
                row["position"],
                row["updated_at"],
                COMMIT_ID,
            ),
        )
    return ConversationPublicationStage(
        history=history,
        commit_id=COMMIT_ID,
        chat_id=CHAT_ID,
        user_id=OWNER,
        base_render_revision=0,
        next_render_revision=1,
        operation_fence=SimpleNamespace(name="fence-060"),
        layouts=copy.deepcopy(authoritative_layouts),
    )


def _contents(manager: WorkspaceManager) -> dict[str, str]:
    return {
        component["component_id"]: component["content"]
        for component in manager.live_components(CHAT_ID, OWNER)
    }


def test_staged_workspace_mutations_are_task_local_and_authority_is_unchanged(
    database: PlaneTestRuntime,
) -> None:
    _seed_authoritative_canvas(database)
    history, manager = _manager(database)
    stage = _stage_complete_copy(database, history, manager)
    authoritative = _contents(manager)
    authoritative_layout = manager.live_layouts(CHAT_ID, OWNER)

    token = activate_conversation_publication(stage)
    try:
        assert current_conversation_publication() is stage
        assert stage.matches(history, CHAT_ID, OWNER)
        other_history = HistoryManager(
            plane_runtime=database,
            plane_repositories=database.repositories,
        )
        assert not stage.matches(other_history, CHAT_ID, OWNER)
        assert _contents(manager) == authoritative

        ops = manager.upsert(
            CHAT_ID,
            OWNER,
            [
                {
                    "type": "text",
                    "component_id": "component-a",
                    "content": "staged-a",
                },
                {
                    "type": "text",
                    "component_id": "component-c",
                    "content": "staged-c",
                },
            ],
        )
        assert [op["component_id"] for op in ops] == ["component-a", "component-c"]
        assert manager.remove(CHAT_ID, OWNER, "component-b") is True
        assert manager.upsert_layout(
            CHAT_ID,
            OWNER,
            "next-layout",
            [{"type": "ref", "component_id": "component-a"}],
        )

        assert _contents(manager) == {
            "component-a": "staged-a",
            "component-c": "staged-c",
        }
        layouts = manager.live_layouts(CHAT_ID, OWNER)
        refs = {
            layout["layout_key"]: list(iter_layout_refs(layout["layout"]))
            for layout in layouts
        }
        assert refs == {"authoritative-layout": [], "next-layout": ["component-a"]}

        # Timeline snapshots are authoritative history, never a staging store.
        assert manager.snapshot(CHAT_ID, OWNER, "staged-turn") is None
        assert manager.count_snapshots(CHAT_ID, OWNER) == 0

        # ContextVar state must cross the exact asyncio.to_thread seam used by
        # WorkspaceManager's async facade.
        async def read_in_thread() -> dict[str, str]:
            components = await manager.alive_components(CHAT_ID, OWNER)
            return {item["component_id"]: item["content"] for item in components}

        assert asyncio.run(read_in_thread()) == {
            "component-a": "staged-a",
            "component-c": "staged-c",
        }

        stage.seal(committed=False)
        with pytest.raises(RuntimeError, match="sealed"):
            manager.remove(CHAT_ID, OWNER, "component-a")
    finally:
        reset_conversation_publication(token)

    assert current_conversation_publication() is None
    assert _contents(manager) == authoritative
    assert manager.live_layouts(CHAT_ID, OWNER) == authoritative_layout
    assert database.fetch_one(
        "SELECT has_saved_components FROM chats WHERE id = ? AND user_id = ?",
        (CHAT_ID, OWNER),
    )["has_saved_components"] is False


def test_staged_empty_canvas_is_explicit_without_clearing_authority(
    database: PlaneTestRuntime,
) -> None:
    _seed_authoritative_canvas(database)
    history, manager = _manager(database)
    stage = _stage_complete_copy(database, history, manager)

    token = activate_conversation_publication(stage)
    try:
        assert manager.remove(CHAT_ID, OWNER, "component-a")
        assert manager.remove(CHAT_ID, OWNER, "component-b")
        assert manager.live_rows(CHAT_ID, OWNER) == []
        assert manager.live_components(CHAT_ID, OWNER) == []
        assert len(stage.layouts) == 1
        assert stage.layouts[0]["layout_key"] == "authoritative-layout"
        assert stage.layouts[0]["position"] == 3
        assert stage.layouts[0]["layout"] == [
            {"type": "stack", "children": []}
        ]
        stage.seal(committed=True)
        assert stage.sealed is True
        assert stage.committed is True
    finally:
        reset_conversation_publication(token)

    assert set(_contents(manager)) == {"component-a", "component-b"}
    staged_count = database.fetch_one(
        "SELECT COUNT(*) AS count FROM saved_components "
        "WHERE conversation_commit_id = ? AND committed_render_revision = 1",
        (COMMIT_ID,),
    )
    assert staged_count["count"] == 0


def test_unmatched_stage_and_current_revision_filter_hide_non_authoritative_rows(
    database: PlaneTestRuntime,
) -> None:
    _seed_authoritative_canvas(database)
    history, manager = _manager(database)
    stage = _stage_complete_copy(database, history, manager)

    token = activate_conversation_publication(stage)
    try:
        # A different chat/user/history must never inherit this task's stage.
        assert manager.live_rows(CHAT_ID, "another-owner") == []
        other_history = HistoryManager(
            plane_runtime=database,
            plane_repositories=database.repositories,
        )
        other_manager = WorkspaceManager(
            other_history,
            plane_runtime=database,
            plane_repositories=database.repositories,
        )
        assert set(_contents(other_manager)) == {"component-a", "component-b"}
    finally:
        reset_conversation_publication(token)

    with database.transaction() as transaction:
        transaction.execute(
            "UPDATE conversation_commit SET state = 'committed', "
            "committed_render_revision = 1, committed_at = now() "
            "WHERE commit_id = %s",
            (COMMIT_ID,),
        )
        transaction.execute(
            "UPDATE chats SET render_revision = 1, conversation_commit_id = %s, "
            "snapshot_committed_at = now() WHERE id = %s AND user_id = %s",
            (COMMIT_ID, CHAT_ID, OWNER),
        )

    # Legacy revision-zero rows coexist physically but the current committed
    # revision is the only authoritative read outside a publication stage.
    assert set(_contents(manager)) == {"component-a", "component-b"}
    assert all(
        row["id"]
        != database.fetch_one(
            "SELECT id FROM saved_components WHERE chat_id = ? "
            "AND component_id = ? AND conversation_commit_id IS NULL",
            (CHAT_ID, row["component_id"]),
        )["id"]
        for row in manager.live_rows(CHAT_ID, OWNER)
    )


def test_stage_validation_and_context_reset_are_fail_closed() -> None:
    history = object()
    with pytest.raises(ValueError, match="next_render_revision"):
        ConversationPublicationStage(
            history=history,
            commit_id=COMMIT_ID,
            chat_id=CHAT_ID,
            user_id=OWNER,
            base_render_revision=2,
            next_render_revision=4,
        )
    with pytest.raises(TypeError, match="stage"):
        activate_conversation_publication(object())  # type: ignore[arg-type]

    stage = ConversationPublicationStage(
        history=history,
        commit_id=COMMIT_ID,
        chat_id=CHAT_ID,
        user_id=OWNER,
        base_render_revision=0,
        next_render_revision=1,
    )
    token = activate_conversation_publication(stage)
    assert current_conversation_publication() is stage
    reset_conversation_publication(token)
    assert current_conversation_publication() is None
    stage.seal(committed=False)
    with pytest.raises(ValueError, match="committed"):
        stage.seal(committed=True)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"commit_id": "not-a-uuid"}, "commit_id"),
        ({"chat_id": uuid.uuid1()}, "chat_id"),
        ({"history": None}, "history"),
        ({"user_id": "  "}, "user_id"),
        ({"base_render_revision": True}, "base_render_revision"),
        ({"layouts": ["not-a-layout"]}, "layouts"),
        ({"committed": True}, "committed"),
    ],
)
def test_stage_constructor_rejects_ambiguous_authority(
    override: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "history": object(),
        "commit_id": COMMIT_ID,
        "chat_id": CHAT_ID,
        "user_id": OWNER,
        "base_render_revision": 0,
        "next_render_revision": 1,
    }
    values.update(override)
    with pytest.raises(ValueError, match=message):
        ConversationPublicationStage(**values)  # type: ignore[arg-type]


def test_stage_copies_layouts_and_same_outcome_seal_is_idempotent() -> None:
    layouts = [{"layout_key": "copy", "position": 1, "layout": []}]
    stage = ConversationPublicationStage(
        history=object(),
        commit_id=COMMIT_ID,
        chat_id=CHAT_ID,
        user_id=f" {OWNER} ",
        base_render_revision=0,
        next_render_revision=1,
        layouts=layouts,
    )
    layouts[0]["layout_key"] = "mutated"
    assert stage.layouts[0]["layout_key"] == "copy"
    assert stage.user_id == OWNER
    stage.seal(committed=False)
    stage.seal(committed=False)


def test_source_contract_avoids_orchestrator_global_or_filesystem_authority() -> None:
    source = (Path(__file__).parents[1] / "orchestrator" / "conversation_publication.py").read_text(
        encoding="utf-8"
    )
    assert "ContextVar" in source
    assert "\nimport asyncio" not in source
    assert "from orchestrator.orchestrator" not in source
    assert "os.environ" not in source
    assert "open(" not in source
