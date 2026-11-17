"""Tests for astralplane.repositories.conversation_files: file-mapping creation proves
conversation ownership, redacts the storage key, and keeps reads and deletes owner-
and conversation-scoped.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import (
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.conversation_files import ConversationFileRepository
from tests.repositories._support import Result, ScriptedTransaction


def _mapping_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "original_name": "report.pdf",
        "backend_path": "uploads/opaque-id.pdf",
        "uploaded_at": 100,
    }
    row.update(overrides)
    return row


def test_add_mapping_proves_conversation_ownership_and_redacts_storage_key() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_mapping_row(),))]
    )

    record = ConversationFileRepository().add_mapping(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="chat-1",
        original_name="report.pdf",
        backend_path="uploads/opaque-id.pdf",
        uploaded_at=100,
    )

    assert record.mapping_id == 7
    assert record.backend_path == "uploads/opaque-id.pdf"
    assert "report.pdf" not in repr(record)
    assert "uploads/opaque-id.pdf" not in repr(record)
    assert "FROM chats AS chat" in transaction.fetch_sql()
    assert transaction.calls[0][2] == (
        "report.pdf",
        "uploads/opaque-id.pdf",
        100,
        "chat-1",
        "owner-1",
    )


def test_add_mapping_rejects_missing_or_foreign_conversation() -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)])

    with pytest.raises(RepositoryNotFoundError):
        ConversationFileRepository().add_mapping(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            conversation_id="chat-1",
            original_name="report.pdf",
            backend_path="uploads/opaque-id.pdf",
            uploaded_at=100,
        )


def test_get_and_list_are_owner_scoped_and_deterministically_ordered() -> None:
    transaction = ScriptedTransaction(
        one=[_mapping_row()],
        all_rows=[(_mapping_row(), _mapping_row(id=8, uploaded_at=None))],
    )
    repository = ConversationFileRepository()

    loaded = repository.get_mapping(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        mapping_id=7,
    )
    listed = repository.list_mappings(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="chat-1",
        limit=2,
    )

    assert loaded is not None and loaded.original_name == "report.pdf"
    assert [record.mapping_id for record in listed] == [7, 8]
    assert listed[1].uploaded_at is None
    assert transaction.calls[0][2] == (7, "owner-1")
    assert transaction.calls[1][2] == ("chat-1", "owner-1", 2)
    assert "uploaded_at ASC NULLS LAST, id ASC" in transaction.calls[1][1]


def test_absent_mapping_is_explicit() -> None:
    transaction = ScriptedTransaction(one=[None])
    assert (
        ConversationFileRepository().get_mapping(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            mapping_id=7,
        )
        is None
    )


def test_delete_is_owner_and_conversation_scoped_and_idempotent() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(rowcount=1), Result(rowcount=0)]
    )
    repository = ConversationFileRepository()

    assert repository.delete_mapping(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="chat-1",
        mapping_id=7,
    )
    assert not repository.delete_mapping(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="chat-1",
        mapping_id=7,
    )
    assert transaction.calls[0][2] == (7, "chat-1", "owner-1")
    assert "chat_id = %s AND user_id = %s" in transaction.calls[0][1]


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("owner_id", ""),
        ("conversation_id", ""),
        ("original_name", ""),
        ("backend_path", ""),
        ("uploaded_at", -1),
    ],
)
def test_add_mapping_rejects_invalid_values(argument: str, value: object) -> None:
    arguments: dict[str, object] = {
        "owner_id": "owner-1",
        "conversation_id": "chat-1",
        "original_name": "report.pdf",
        "backend_path": "uploads/opaque-id.pdf",
        "uploaded_at": 100,
    }
    arguments[argument] = value

    with pytest.raises(RepositoryValidationError):
        ConversationFileRepository().add_mapping(  # type: ignore[arg-type]
            ScriptedTransaction(),  # type: ignore[arg-type]
            **arguments,
        )


@pytest.mark.parametrize(
    "row",
    [
        _mapping_row(id=0),
        _mapping_row(uploaded_at=-1),
        _mapping_row(backend_path=None),
    ],
)
def test_corrupt_persisted_mapping_fails_closed(row: dict[str, object]) -> None:
    transaction = ScriptedTransaction(one=[row])
    with pytest.raises(RepositoryDataError):
        ConversationFileRepository().get_mapping(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            mapping_id=7,
        )


def test_add_requires_exactly_one_returned_mapping() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_mapping_row(), _mapping_row(id=8)))]
    )
    with pytest.raises(RepositoryDataError, match="exactly one"):
        ConversationFileRepository().add_mapping(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            conversation_id="chat-1",
            original_name="report.pdf",
            backend_path="uploads/opaque-id.pdf",
            uploaded_at=100,
        )
