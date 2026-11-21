"""Tests for astralplane.repositories.saved_components: the saved-component facade
reuses publication-aware creation and preserves ordered, authoritative CAS reads.
"""

from __future__ import annotations

from astralplane.repositories.saved_components import (
    SavedComponentRecord,
    SavedComponentRepository,
)
from tests.repositories._support import Result, ScriptedTransaction


def _component_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "row-1",
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "component_id": "component-1",
        "component_data": '{"type":"Card"}',
        "component_type": "Card",
        "title": "Card",
        "position": 0,
        "created_at": 100,
        "updated_at": 100,
        "conversation_commit_id": None,
        "committed_render_revision": None,
    }
    row.update(overrides)
    return row


def _record() -> SavedComponentRecord:
    return SavedComponentRecord(
        row_id="row-1",
        conversation_id="chat-1",
        owner_id="owner-1",
        component_id="component-1",
        payload={"type": "Card"},
        component_type="Card",
        title="Card",
        position=0,
        created_at=100,
        updated_at=100,
    )


def test_saved_component_name_uses_existing_publication_aware_create() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_component_row(),))]
    )

    record = SavedComponentRepository().create(  # type: ignore[arg-type]
        transaction,
        _record(),
    )

    assert record.component_id == "component-1"
    assert "FROM chats AS chat" in transaction.fetch_sql()
    assert "conversation_commit" in transaction.fetch_sql()
    assert transaction.calls[0][2][12:14] == ("chat-1", "owner-1")  # type: ignore[index]


def test_saved_component_name_preserves_ordered_authoritative_read_and_cas() -> None:
    transaction = ScriptedTransaction(
        all_rows=[(_component_row(),)],
        execute=[Result(returned_records=(_component_row(updated_at=200),))],
    )
    repository = SavedComponentRepository()

    listed = repository.list_current(  # type: ignore[arg-type]
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
    )
    replaced = repository.replace(  # type: ignore[arg-type]
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        component_id="component-1",
        payload={"type": "Card", "value": "updated"},
        component_type="Card",
        title="Card",
        expected_updated_at=100,
        updated_at=200,
    )

    assert listed[0].position == 0
    assert replaced.updated_at == 200
    assert "ORDER BY COALESCE(component.position" in transaction.calls[0][1]
    assert "AND updated_at = %s" in transaction.calls[1][1]
    assert transaction.calls[1][2][4:7] == (  # type: ignore[index]
        "chat-1",
        "owner-1",
        "component-1",
    )
