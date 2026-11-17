"""Owner- and chat-scoped chat read receipts: one durable row per owner and chat
holding the last-seen commit position and a monotonic revision. Unread policy,
rendering, and audit stay with the host; deleting the chat cascades the receipt so
a re-created chat id never inherits prior read state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _non_negative_int,
    _required_id,
    _row_value,
)

_MAX_INTEGER: Final = 9007199254740991

_FIELDS: Final = "owner_id, chat_id, last_seen_position, revision"

_MARK_SEEN_SQL: Final = f"""
INSERT INTO chat_read_receipt (owner_id, chat_id, last_seen_position, revision)
SELECT %s, %s, %s, 1
FROM chats
WHERE chats.id = %s AND chats.user_id = %s
ON CONFLICT (owner_id, chat_id) DO UPDATE SET
    last_seen_position = EXCLUDED.last_seen_position,
    revision = chat_read_receipt.revision + 1
WHERE chat_read_receipt.revision = %s
  AND chat_read_receipt.last_seen_position < EXCLUDED.last_seen_position
RETURNING {_FIELDS}
""".strip()

_GET_SQL: Final = f"""
SELECT {_FIELDS}
FROM chat_read_receipt
WHERE owner_id = %s AND chat_id = %s
""".strip()

_CHAT_SQL: Final = """
SELECT 1 AS present
FROM chats
WHERE id = %s AND user_id = %s
""".strip()

_LIST_SQL: Final = """
SELECT chat.id AS chat_id, chat.user_id AS owner_id,
       receipt.last_seen_position, receipt.revision
FROM chats AS chat
LEFT JOIN chat_read_receipt AS receipt
  ON receipt.owner_id = chat.user_id AND receipt.chat_id = chat.id
WHERE chat.user_id = %s
ORDER BY chat.updated_at DESC, chat.id ASC
LIMIT %s
""".strip()


@dataclass(frozen=True, slots=True)
class ChatReadReceiptRecord:
    owner_id: str
    chat_id: str
    last_seen_position: int
    revision: int


@dataclass(frozen=True, slots=True)
class ChatReadStateRecord:
    conversation_id: str
    owner_id: str
    receipt: ChatReadReceiptRecord | None


def _bounded_position(value: object, field_name: str) -> int:
    position = _non_negative_int(value, field_name)
    if position > _MAX_INTEGER:
        raise RepositoryValidationError(
            f"{field_name} must be a bounded integer",
            metadata={"field": field_name, "maximum": _MAX_INTEGER},
        )
    return position


def _receipt(row: Mapping[str, Any]) -> ChatReadReceiptRecord:
    return ChatReadReceiptRecord(
        owner_id=str(_row_value(row, "owner_id")),
        chat_id=str(_row_value(row, "chat_id")),
        last_seen_position=_bounded_position(
            _row_value(row, "last_seen_position"), "last_seen_position"
        ),
        revision=_bounded_position(_row_value(row, "revision"), "revision"),
    )


def _returned(result: object, operation: str) -> Mapping[str, Any] | None:
    rows = getattr(result, "returned_records", ())
    if len(rows) > 1:
        raise RepositoryDataError(
            "write returned multiple detached records",
            metadata={"operation": operation, "returned": len(rows)},
        )
    return rows[0] if rows else None


class ChatReadReceiptRepository:
    def mark_seen(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        last_seen_position: int,
        expected_revision: int,
    ) -> ChatReadReceiptRecord:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        position = _bounded_position(last_seen_position, "last_seen_position")
        expected = _bounded_position(expected_revision, "expected_revision")
        row = _returned(
            transaction.execute(
                _MARK_SEEN_SQL,
                (owner, chat, position, chat, owner, expected),
            ),
            "chat_read_receipt.mark_seen",
        )
        if row is not None:
            return _receipt(row)
        existing = self.get(transaction, owner_id=owner, chat_id=chat)
        if existing is None:
            if transaction.fetch_one(_CHAT_SQL, (chat, owner)) is None:
                raise RepositoryNotFoundError(
                    "chat was not found",
                    metadata={"operation": "chat_read_receipt.mark_seen"},
                )
            raise RepositoryConflictError(
                "read receipt revision is stale",
                metadata={"operation": "chat_read_receipt.mark_seen"},
            )
        if existing.revision != expected:
            raise RepositoryConflictError(
                "read receipt revision is stale",
                metadata={"operation": "chat_read_receipt.mark_seen"},
            )
        return existing

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        chat_id: str,
    ) -> ChatReadReceiptRecord | None:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        row = query.fetch_one(_GET_SQL, (owner, chat))
        return None if row is None else _receipt(row)

    def list_receipts(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        limit: int = 200,
    ) -> tuple[ChatReadStateRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        bounded = _bounded_limit(limit)
        rows = query.fetch_all(_LIST_SQL, (owner, bounded))
        states: list[ChatReadStateRecord] = []
        for row in rows:
            state_owner = str(_row_value(row, "owner_id"))
            if state_owner != owner:
                raise RepositoryDataError("batched read crossed the owner boundary")
            conversation_id = str(_row_value(row, "chat_id"))
            receipt = (
                None
                if _row_value(row, "revision") is None
                else _receipt(
                    {
                        "owner_id": state_owner,
                        "chat_id": conversation_id,
                        "last_seen_position": _row_value(row, "last_seen_position"),
                        "revision": _row_value(row, "revision"),
                    }
                )
            )
            states.append(
                ChatReadStateRecord(
                    conversation_id=conversation_id,
                    owner_id=state_owner,
                    receipt=receipt,
                )
            )
        return tuple(states)


__all__ = (
    "ChatReadReceiptRecord",
    "ChatReadReceiptRepository",
    "ChatReadStateRecord",
)
