"""Idempotent upserts of chat and message rows that a host derived from mesh events. Each
row carries the id of the event that produced it, so replaying the same event changes
nothing and a different event reusing a row is refused. The fold that decides what to
write stays with the host; this module only stores and traces it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    _bounded_text,
    _canonical_json,
    _content_value,
    _required_id,
    _row_value,
)
from astralplane.repositories._mesh_values import (
    bounded_integer,
    hash_value,
    returned_row,
    stored_integer,
)

_LOCK_SEED: Final = 105_031
_CHAT_FIELDS: Final = "id, user_id, title, agent_id, created_at, updated_at, event_id"
_MESSAGE_FIELDS: Final = (
    "id, chat_id, user_id, role, content, timestamp, parent_message_id, event_id"
)

_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))"

_CHAT_BY_EVENT_SQL: Final = f"SELECT {_CHAT_FIELDS} FROM chats WHERE event_id = %s"

_OWNED_CHAT_BY_EVENT_SQL: Final = (
    f"SELECT {_CHAT_FIELDS} FROM chats WHERE event_id = %s AND user_id = %s"
)

_CHAT_BY_ID_SQL: Final = f"SELECT {_CHAT_FIELDS} FROM chats WHERE id = %s"

_INSERT_CHAT_SQL: Final = f"""
INSERT INTO chats (
    id, user_id, title, agent_id, created_at, updated_at,
    has_saved_components, render_revision, event_id
) VALUES (%s, %s, %s, %s, %s, %s, FALSE, 0, %s)
RETURNING {_CHAT_FIELDS}
""".strip()

_UPDATE_CHAT_SQL: Final = f"""
UPDATE chats SET title = %s, agent_id = %s, updated_at = GREATEST(updated_at, %s)
WHERE event_id = %s AND user_id = %s
RETURNING {_CHAT_FIELDS}
""".strip()

_BRANCH_HEAD_SQL: Final = """
INSERT INTO chat_branch_head (chat_id, user_id, head_message_id, branch_snapshot_id, version)
VALUES (%s, %s, NULL, NULL, 0)
ON CONFLICT (chat_id) DO NOTHING
""".strip()

_MESSAGE_BY_EVENT_SQL: Final = f"SELECT {_MESSAGE_FIELDS} FROM messages WHERE event_id = %s"

_OWNED_MESSAGE_BY_EVENT_SQL: Final = (
    f"SELECT {_MESSAGE_FIELDS} FROM messages WHERE event_id = %s AND user_id = %s"
)

_INSERT_MESSAGE_SQL: Final = f"""
INSERT INTO messages (
    chat_id, user_id, role, content, timestamp, parent_message_id, event_id
)
SELECT chat.id, chat.user_id, %s, %s, %s,
       (SELECT parent.id FROM messages AS parent
        WHERE parent.event_id = %s AND parent.chat_id = chat.id AND parent.user_id = chat.user_id),
       %s
FROM chats AS chat
WHERE chat.id = %s AND chat.user_id = %s
RETURNING {_MESSAGE_FIELDS}
""".strip()

_CHAT_PRESENT_SQL: Final = "SELECT 1 AS present FROM chats WHERE id = %s AND user_id = %s"


@dataclass(frozen=True, slots=True)
class DerivedChatRecord:
    chat_id: str
    owner_id: str
    event_id: str | None
    title: str
    agent_id: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class DerivedMessageRecord:
    message_id: int
    chat_id: str
    owner_id: str
    event_id: str | None
    role: str
    content: Any
    timestamp: int
    parent_message_id: int | None


def _chat(row: Mapping[str, Any]) -> DerivedChatRecord:
    event_id = _row_value(row, "event_id")
    agent_id = _row_value(row, "agent_id")
    return DerivedChatRecord(
        chat_id=str(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        event_id=None if event_id is None else str(event_id),
        title=str(_row_value(row, "title")),
        agent_id=None if agent_id is None else str(agent_id),
        created_at=stored_integer(row, "created_at"),
        updated_at=stored_integer(row, "updated_at"),
    )


def _message(row: Mapping[str, Any]) -> DerivedMessageRecord:
    event_id = _row_value(row, "event_id")
    parent = _row_value(row, "parent_message_id")
    return DerivedMessageRecord(
        message_id=stored_integer(row, "id"),
        chat_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "user_id")),
        event_id=None if event_id is None else str(event_id),
        role=str(_row_value(row, "role")),
        content=_content_value(_row_value(row, "content")),
        timestamp=stored_integer(row, "timestamp"),
        parent_message_id=None if parent is None else stored_integer(row, "parent_message_id"),
    )


def _lock_event(transaction: Transaction, event_id: str) -> None:
    transaction.fetch_one(_LOCK_SQL, (event_id, _LOCK_SEED))


class DerivedChatRepository:
    def upsert_chat(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        event_id: str,
        chat_id: str,
        title: str,
        agent_id: str | None,
        created_at: int,
        updated_at: int | None = None,
    ) -> DerivedChatRecord:
        owner = _required_id(owner_id, "owner_id")
        event = hash_value(event_id, "event_id")
        chat = _required_id(chat_id, "chat_id")
        title_value = _bounded_text(title, "title", maximum=512)
        agent = None if agent_id is None else _required_id(agent_id, "agent_id")
        created = bounded_integer(created_at, "created_at")
        updated = created if updated_at is None else bounded_integer(updated_at, "updated_at")
        _lock_event(transaction, event)
        linked = transaction.fetch_one(_CHAT_BY_EVENT_SQL, (event,))
        if linked is not None:
            record = _chat(linked)
            if (record.chat_id, record.owner_id) != (chat, owner):
                raise RepositoryConflictError(
                    "event already derived a different chat",
                    metadata={"operation": "derived_chat.upsert"},
                )
            row = returned_row(
                transaction.execute(_UPDATE_CHAT_SQL, (title_value, agent, updated, event, owner)),
                "derived_chat.update",
            )
            return _chat(row) if row is not None else record
        if transaction.fetch_one(_CHAT_BY_ID_SQL, (chat,)) is not None:
            raise RepositoryConflictError(
                "chat identity already exists without this event",
                metadata={"operation": "derived_chat.upsert"},
            )
        row = returned_row(
            transaction.execute(
                _INSERT_CHAT_SQL, (chat, owner, title_value, agent, created, updated, event)
            ),
            "derived_chat.insert",
        )
        if row is None:
            raise RepositoryNotFoundError(
                "derived chat was not stored", metadata={"operation": "derived_chat.insert"}
            )
        transaction.execute(_BRANCH_HEAD_SQL, (chat, owner))
        return _chat(row)

    def upsert_message(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        event_id: str,
        role: str,
        content: object,
        timestamp: int,
        parent_event_id: str | None = None,
    ) -> DerivedMessageRecord:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        event = hash_value(event_id, "event_id")
        role_value = _bounded_text(role, "role", maximum=64)
        stored_content = _canonical_json(content, "content")
        moment = bounded_integer(timestamp, "timestamp")
        parent = None if parent_event_id is None else hash_value(parent_event_id, "parent_event_id")
        _lock_event(transaction, event)
        existing = transaction.fetch_one(_MESSAGE_BY_EVENT_SQL, (event,))
        if existing is not None:
            record = _message(existing)
            expected = (owner, chat, role_value, json.loads(stored_content), moment)
            observed = (
                record.owner_id,
                record.chat_id,
                record.role,
                _plain(record.content),
                record.timestamp,
            )
            if observed != expected:
                raise RepositoryConflictError(
                    "event already derived a different message",
                    metadata={"operation": "derived_message.upsert"},
                )
            return record
        if (
            parent is not None
            and transaction.fetch_one(_OWNED_MESSAGE_BY_EVENT_SQL, (parent, owner)) is None
        ):
            raise RepositoryNotFoundError(
                "parent event has not derived a message",
                metadata={"operation": "derived_message.upsert"},
            )
        row = returned_row(
            transaction.execute(
                _INSERT_MESSAGE_SQL,
                (role_value, stored_content, moment, parent, event, chat, owner),
            ),
            "derived_message.insert",
        )
        if row is None:
            raise RepositoryNotFoundError(
                "chat was not found", metadata={"operation": "derived_message.upsert"}
            )
        return _message(row)

    def get_chat_by_event(
        self, query: QueryExecutor, *, owner_id: str, event_id: str
    ) -> DerivedChatRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = query.fetch_one(_OWNED_CHAT_BY_EVENT_SQL, (hash_value(event_id, "event_id"), owner))
        return None if row is None else _chat(row)

    def get_message_by_event(
        self, query: QueryExecutor, *, owner_id: str, event_id: str
    ) -> DerivedMessageRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            _OWNED_MESSAGE_BY_EVENT_SQL, (hash_value(event_id, "event_id"), owner)
        )
        return None if row is None else _message(row)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


__all__ = (
    "DerivedChatRecord",
    "DerivedChatRepository",
    "DerivedMessageRecord",
)
