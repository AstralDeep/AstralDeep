"""Owner-isolated mapping between a conversation's uploaded file display names and their
backend storage keys. Blob I/O, upload policy, and physical deletion stay with the
caller; only the mapping is tracked here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _positive_int,
    _required_id,
    _row_value,
    _single_returned,
)


@dataclass(frozen=True, slots=True)
class ConversationFileRecord:
    mapping_id: int
    conversation_id: str
    owner_id: str
    original_name: str = field(repr=False)
    backend_path: str = field(repr=False)
    uploaded_at: int | None


class ConversationFileRepository:
    _FIELDS = "id, chat_id, user_id, original_name, backend_path, uploaded_at"

    def add_mapping(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        original_name: str,
        backend_path: str,
        uploaded_at: int,
    ) -> ConversationFileRecord:
        owner = _required_id(owner_id, "owner_id")
        conversation = _required_id(conversation_id, "conversation_id")
        display_name = _bounded_text(original_name, "original_name", maximum=4096)
        storage_key = _bounded_text(backend_path, "backend_path", maximum=16_384)
        observed_at = _non_negative_int(uploaded_at, "uploaded_at")
        result = transaction.execute(
            f"""
            INSERT INTO chat_files (
                chat_id, user_id, original_name, backend_path, uploaded_at
            )
            SELECT chat.id, chat.user_id, %s, %s, %s
              FROM chats AS chat
             WHERE chat.id = %s AND chat.user_id = %s
            RETURNING {self._FIELDS}
            """,
            (display_name, storage_key, observed_at, conversation, owner),
        )
        returned = getattr(result, "returned_records", ())
        if not returned:
            raise RepositoryNotFoundError(
                "owner-scoped conversation was not found",
                metadata={"operation": "conversation_file.add"},
            )
        return _mapping(_single_returned(result, "conversation_file.add"))

    def get_mapping(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        mapping_id: int,
    ) -> ConversationFileRecord | None:
        owner = _required_id(owner_id, "owner_id")
        identity = _positive_int(mapping_id, "mapping_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM chat_files WHERE id = %s AND user_id = %s",
            (identity, owner),
        )
        return None if row is None else _mapping(row)

    def list_mappings(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        limit: int = 1000,
    ) -> tuple[ConversationFileRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        conversation = _required_id(conversation_id, "conversation_id")
        limit = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS} FROM chat_files
             WHERE chat_id = %s AND user_id = %s
             ORDER BY uploaded_at ASC NULLS LAST, id ASC
             LIMIT %s
            """,
            (conversation, owner, limit),
        )
        return tuple(_mapping(row) for row in rows)

    def delete_mapping(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        mapping_id: int,
    ) -> bool:
        owner = _required_id(owner_id, "owner_id")
        conversation = _required_id(conversation_id, "conversation_id")
        identity = _positive_int(mapping_id, "mapping_id")
        result = transaction.execute(
            "DELETE FROM chat_files WHERE id = %s AND chat_id = %s AND user_id = %s",
            (identity, conversation, owner),
        )
        return result.rowcount == 1


def _mapping(row: Mapping[str, Any]) -> ConversationFileRecord:
    try:
        mapping_id = _positive_int(_row_value(row, "id"), "mapping_id")
        uploaded_at = (
            None
            if row.get("uploaded_at") is None
            else _non_negative_int(row["uploaded_at"], "uploaded_at")
        )
        conversation_id = _required_id(_row_value(row, "chat_id"), "conversation_id")
        owner_id = _required_id(_row_value(row, "user_id"), "owner_id")
        original_name = _bounded_text(
            _row_value(row, "original_name"), "original_name", maximum=4096
        )
        backend_path = _bounded_text(
            _row_value(row, "backend_path"), "backend_path", maximum=16_384
        )
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted conversation file metadata is invalid") from exc
    return ConversationFileRecord(
        mapping_id=mapping_id,
        conversation_id=conversation_id,
        owner_id=owner_id,
        original_name=original_name,
        backend_path=backend_path,
        uploaded_at=uploaded_at,
    )


__all__ = ("ConversationFileRecord", "ConversationFileRepository")
