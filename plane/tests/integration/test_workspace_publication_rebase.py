"""Real-PostgreSQL tests for astralplane.repositories.workspaces and history.py: an
assistant-result publication rebase replays correctly, stays owner-scoped, and rolls
back atomically with the outer transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.database.transaction import PlaneDatabase
from astralplane.repositories.history import HistoryRepository
from astralplane.repositories.workspaces import (
    CanvasComponentRecord,
    LayoutRecord,
    PublicationRebaseComponent,
    PublicationRebaseLayout,
    WorkspaceRepository,
)
from tests.fixtures.migrated_template import MigratedDatabase

NOW = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)


def _identifier() -> str:
    return str(uuid.uuid4())


def _stage_atomic_head(
    database: PlaneDatabase,
    workspaces: WorkspaceRepository,
    *,
    owner_id: str,
    conversation_id: str,
    base_revision: int,
    expected_head_publication_id: str | None,
    offset: int,
) -> str:
    publication_id = _identifier()
    with database.transaction() as transaction:
        workspaces.publications.stage(
            transaction,
            publication_id=publication_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            request_generation=_identifier(),
            base_render_revision=base_revision,
            started_at=NOW + timedelta(seconds=offset),
        )
        workspaces.publications.commit_at_head(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=publication_id,
            expected_staged_base_render_revision=base_revision,
            expected_head_render_revision=base_revision,
            expected_head_publication_id=expected_head_publication_id,
            committed_at=NOW + timedelta(seconds=offset + 1),
            updated_at=1000 + offset,
        )
    return publication_id


def _stage_assistant_result(
    database: PlaneDatabase,
    history: HistoryRepository,
    workspaces: WorkspaceRepository,
    *,
    owner_id: str,
    conversation_id: str,
    base_publication_id: str,
    base_revision: int,
    offset: int,
) -> tuple[str, str]:
    publication_id = _identifier()
    component_row_id = _identifier()
    with database.transaction() as transaction:
        workspaces.publications.stage(
            transaction,
            publication_id=publication_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            request_generation=_identifier(),
            base_render_revision=base_revision,
            started_at=NOW + timedelta(seconds=offset),
            publication_role="assistant_result",
            parent_publication_id=base_publication_id,
            execution_base_publication_id=base_publication_id,
            execution_base_render_revision=base_revision,
            execution_base_components_sha256="a" * 64,
            execution_base_layouts_sha256="b" * 64,
        )
        history.messages.append(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            role="assistant",
            content="draft result",
            timestamp=1000 + offset,
            publication_id=publication_id,
            commit_position=0,
            committed_render_revision=base_revision + 1,
        )
        workspaces.canvas.create(
            transaction,
            CanvasComponentRecord(
                row_id=component_row_id,
                conversation_id=conversation_id,
                owner_id=owner_id,
                component_id="component-old",
                payload={"component_id": "component-old", "type": "Card"},
                component_type="Card",
                title="Old",
                position=0,
                created_at=1000 + offset,
                updated_at=1000 + offset,
                publication_id=publication_id,
                committed_render_revision=base_revision + 1,
            ),
        )
        workspaces.layouts.create(
            transaction,
            LayoutRecord(
                layout_id=0,
                conversation_id=conversation_id,
                owner_id=owner_id,
                layout_key="layout-old",
                position=3,
                tree=[{"component_id": "component-old"}],
                created_at=1000 + offset,
                updated_at=1000 + offset,
                publication_id=publication_id,
                committed_render_revision=base_revision + 1,
            ),
        )
    return publication_id, component_row_id


def test_assistant_rebase_replay_owner_scope_and_outer_rollback(
    migrated_clone: MigratedDatabase,
) -> None:
    database = migrated_clone.database
    history = HistoryRepository()
    workspaces = WorkspaceRepository()
    owner_id = "workspace-owner"
    conversation_id = "workspace-chat"
    with database.transaction() as transaction:
        history.conversations.create(
            transaction,
            conversation_id=conversation_id,
            owner_id=owner_id,
            title="Workspace",
            agent_id=None,
            created_at=1000,
        )
    base_publication_id = _stage_atomic_head(
        database,
        workspaces,
        owner_id=owner_id,
        conversation_id=conversation_id,
        base_revision=0,
        expected_head_publication_id=None,
        offset=1,
    )
    result_publication_id, _ = _stage_assistant_result(
        database,
        history,
        workspaces,
        owner_id=owner_id,
        conversation_id=conversation_id,
        base_publication_id=base_publication_id,
        base_revision=1,
        offset=3,
    )
    competing_head_id = _stage_atomic_head(
        database,
        workspaces,
        owner_id=owner_id,
        conversation_id=conversation_id,
        base_revision=1,
        expected_head_publication_id=base_publication_id,
        offset=5,
    )
    target_component = PublicationRebaseComponent(
        row_id=_identifier(),
        component_id="component-new",
        payload={"component_id": "component-new", "type": "Card"},
        component_type="Card",
        title="New",
        position=0,
    )
    target_layout = PublicationRebaseLayout(
        layout_key="layout-new",
        position=7,
        tree=[{"component_id": "component-new"}],
    )
    with database.transaction() as transaction:
        first = workspaces.publications.rebase_assistant_stage(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=result_publication_id,
            expected_staged_base_render_revision=1,
            expected_head_render_revision=2,
            expected_head_publication_id=competing_head_id,
            components=(target_component,),
            layouts=(target_layout,),
            append_conflict_notice=True,
        )
        replay = workspaces.publications.rebase_assistant_stage(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=result_publication_id,
            expected_staged_base_render_revision=1,
            expected_head_render_revision=2,
            expected_head_publication_id=competing_head_id,
            components=(
                PublicationRebaseComponent(
                    row_id=_identifier(),
                    component_id=target_component.component_id,
                    payload=target_component.payload,
                    component_type=target_component.component_type,
                    title=target_component.title,
                    position=target_component.position,
                ),
            ),
            layouts=(target_layout,),
            append_conflict_notice=True,
        )
        assert first == replay
        workspaces.publications.commit_at_head(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=result_publication_id,
            expected_staged_base_render_revision=1,
            expected_head_render_revision=2,
            expected_head_publication_id=competing_head_id,
            committed_at=NOW + timedelta(seconds=8),
            updated_at=1008,
        )

    with database.transaction() as query:
        content = workspaces.publications.get_latest_committed_assistant_content(
            query,
            owner_id=owner_id,
            publication_id=result_publication_id,
        )
        components = workspaces.canvas.list_for_publication(
            query,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=result_publication_id,
            committed_render_revision=3,
            require_state="committed",
        )
        layouts = workspaces.layouts.list_for_publication(
            query,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=result_publication_id,
            committed_render_revision=3,
            require_state="committed",
        )
        assert workspaces.publications.get_for_owner(
            query,
            owner_id="different-owner",
            publication_id=result_publication_id,
        ) is None
    assert content is not None
    assert len(content.content) == 2
    assert content.content[-1]["type"] == "alert"
    assert tuple(record.component_id for record in components) == ("component-new",)
    assert tuple(record.layout_key for record in layouts) == ("layout-new",)
    assert layouts[0].position == 7

    rollback_publication_id, original_row_id = _stage_assistant_result(
        database,
        history,
        workspaces,
        owner_id=owner_id,
        conversation_id=conversation_id,
        base_publication_id=result_publication_id,
        base_revision=3,
        offset=9,
    )
    rollback_head_id = _stage_atomic_head(
        database,
        workspaces,
        owner_id=owner_id,
        conversation_id=conversation_id,
        base_revision=3,
        expected_head_publication_id=result_publication_id,
        offset=11,
    )
    with (
        pytest.raises(RuntimeError, match="injected outer rollback"),
        database.transaction() as transaction,
    ):
        workspaces.publications.rebase_assistant_stage(
            transaction,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=rollback_publication_id,
            expected_staged_base_render_revision=3,
            expected_head_render_revision=4,
            expected_head_publication_id=rollback_head_id,
            components=(
                PublicationRebaseComponent(
                    row_id=_identifier(),
                    component_id="rollback-new",
                    payload={"component_id": "rollback-new", "type": "Card"},
                    component_type="Card",
                    title="Rollback",
                    position=0,
                ),
            ),
            layouts=(),
            append_conflict_notice=True,
        )
        raise RuntimeError("injected outer rollback")
    with database.transaction() as query:
        original = workspaces.canvas.list_for_publication(
            query,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=rollback_publication_id,
            committed_render_revision=4,
            require_state="staged",
        )
        original_message = history.messages.get_by_publication_position(
            query,
            owner_id=owner_id,
            conversation_id=conversation_id,
            publication_id=rollback_publication_id,
            commit_position=0,
        )
    assert tuple(record.row_id for record in original) == (original_row_id,)
    assert original_message is not None
    assert original_message.content == "draft result"
    assert original_message.committed_render_revision == 4
