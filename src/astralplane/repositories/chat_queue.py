"""Owner- and chat-scoped queued follow-ups with revision-fenced mutations, full-permutation
reordering, and an atomic head claim that hands each item to exactly one turn dispatcher.
Callers own transactions and all dispatch, rendering, audit, and retention policy; this
store persists only the neutral queue mechanics.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from astralplane.contracts import Transaction
from astralplane.database.dialect import dialect_of
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _required_id,
    _row_value,
)

_MAX_INTEGER: Final = 9007199254740991
_MAX_TEXT: Final = 16384
_MAX_QUEUE_DEPTH: Final = 100
_QUEUE_LOCK_SEED: Final = 101

_FIELDS: Final = (
    "item_id, owner_id, chat_id, text, position, revision, created_at, updated_at"
)
_ITEM_FIELDS: Final = ", ".join(f"item.{name.strip()}" for name in _FIELDS.split(","))

_INSERT_SQL: Final = f"""
INSERT INTO chat_queue_item (
    item_id, owner_id, chat_id, text, position, revision, created_at, updated_at
)
SELECT %s, %s, %s, %s,
       COALESCE((
           SELECT MAX(queued.position) + 1
           FROM chat_queue_item AS queued
           WHERE queued.owner_id = %s AND queued.chat_id = %s
       ), 0),
       1, %s, %s
FROM chats
WHERE chats.id = %s AND chats.user_id = %s
ON CONFLICT (item_id) DO NOTHING
RETURNING {_FIELDS}
""".strip()

_CLAIM_SQL: Final = f"""
DELETE FROM chat_queue_item
WHERE item_id = (
    SELECT queued.item_id
    FROM chat_queue_item AS queued
    WHERE queued.owner_id = %s AND queued.chat_id = %s
    ORDER BY queued.position, queued.item_id
    LIMIT 1
)
RETURNING {_FIELDS}
""".strip()

_UPDATE_SQL: Final = f"""
UPDATE chat_queue_item
SET text = %s, revision = revision + 1, updated_at = %s
WHERE item_id = %s AND owner_id = %s AND chat_id = %s AND revision = %s
RETURNING {_FIELDS}
""".strip()

_DELETE_SQL: Final = """
DELETE FROM chat_queue_item
WHERE item_id = %s AND owner_id = %s AND chat_id = %s AND revision = %s
""".strip()

_PRESENCE_SQL: Final = """
SELECT 1 AS present
FROM chat_queue_item
WHERE owner_id = %s AND chat_id = %s
LIMIT 1
""".strip()


@dataclass(frozen=True, slots=True)
class QueuedFollowUpRecord:
    item_id: str
    owner_id: str
    chat_id: str
    text: str
    position: int
    revision: int
    created_at: int
    updated_at: int


def _required_integer(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int = _MAX_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RepositoryValidationError(
            f"{field_name} must be a bounded integer",
            metadata={"field": field_name, "minimum": minimum, "maximum": maximum},
        )
    return value


def _required_uuid(value: object, field_name: str) -> str:
    text = _required_id(value, field_name, maximum=36)
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise RepositoryValidationError(f"{field_name} must be a UUID string") from exc


def _optional_row(result: object, operation: str) -> Mapping[str, Any] | None:
    rows = getattr(result, "returned_records", ())
    if len(rows) > 1:
        raise RepositoryDataError(
            "write returned multiple detached records",
            metadata={"operation": operation, "returned": len(rows)},
        )
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, Mapping):
        raise RepositoryDataError(
            "write returned a non-mapping record",
            metadata={"operation": operation},
        )
    return row


def _queued_record(row: Mapping[str, Any]) -> QueuedFollowUpRecord:
    record = QueuedFollowUpRecord(
        item_id=str(_row_value(row, "item_id")),
        owner_id=str(_row_value(row, "owner_id")),
        chat_id=str(_row_value(row, "chat_id")),
        text=str(_row_value(row, "text")),
        position=_required_integer(_row_value(row, "position"), "position"),
        revision=_required_integer(_row_value(row, "revision"), "revision", minimum=1),
        created_at=_required_integer(_row_value(row, "created_at"), "created_at"),
        updated_at=_required_integer(_row_value(row, "updated_at"), "updated_at"),
    )
    if record.updated_at < record.created_at:
        raise RepositoryDataError("persisted queue item regressed its retention clock")
    return record


def _lock_chat_queue(transaction: Transaction, owner_id: str, chat_id: str) -> None:
    transaction.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
        (f"{owner_id}\x1f{chat_id}", _QUEUE_LOCK_SEED),
    )


def _reorder_sql(count: int) -> str:
    rows = ", ".join(["(%s::uuid, %s::bigint, %s::bigint)"] * count)
    return f"""
UPDATE chat_queue_item AS item
SET position = ordering.next_position, revision = ordering.expected_revision + 1,
    updated_at = %s
FROM (VALUES {rows}) AS ordering(item_id, next_position, expected_revision)
WHERE item.item_id = ordering.item_id AND item.owner_id = %s AND item.chat_id = %s
  AND item.revision = ordering.expected_revision
RETURNING {_ITEM_FIELDS}
""".strip()


class ChatQueueRepository:
    def enqueue(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        text: str,
        created_at: int,
        item_id: str | None = None,
    ) -> QueuedFollowUpRecord:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        body = _bounded_text(text, "text", maximum=_MAX_TEXT)
        created = _required_integer(created_at, "created_at")
        identifier = (
            str(uuid.uuid4()) if item_id is None else _required_uuid(item_id, "item_id")
        )
        _lock_chat_queue(transaction, owner, chat)
        chat_row = transaction.fetch_one(
            "SELECT 1 AS present FROM chats WHERE id = %s AND user_id = %s",
            (chat, owner),
        )
        if chat_row is None:
            raise RepositoryNotFoundError(
                "chat was not found", metadata={"operation": "chat_queue.enqueue"}
            )
        depth_row = transaction.fetch_one(
            "SELECT count(*) AS depth FROM chat_queue_item WHERE owner_id = %s AND chat_id = %s",
            (owner, chat),
        )
        if int(_row_value(depth_row, "depth")) >= _MAX_QUEUE_DEPTH:
            raise RepositoryConflictError(
                "chat follow-up queue is at capacity",
                metadata={"operation": "chat_queue.enqueue", "maximum": _MAX_QUEUE_DEPTH},
            )
        inserted = _optional_row(
            transaction.execute(
                _INSERT_SQL,
                (identifier, owner, chat, body, owner, chat, created, created, chat, owner),
            ),
            "chat_queue.enqueue",
        )
        if inserted is not None:
            return _queued_record(inserted)
        existing = self.get(transaction, owner_id=owner, chat_id=chat, item_id=identifier)
        if existing is None:
            raise RepositoryConflictError(
                "queue item identity already exists",
                metadata={"operation": "chat_queue.enqueue"},
            )
        if (existing.text, existing.created_at) != (body, created):
            raise RepositoryConflictError(
                "queue item replay changed durable semantics",
                metadata={"operation": "chat_queue.enqueue"},
            )
        return existing

    def get(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        item_id: str,
    ) -> QueuedFollowUpRecord | None:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        identifier = _required_uuid(item_id, "item_id")
        row = transaction.fetch_one(
            f"SELECT {_FIELDS} FROM chat_queue_item "
            "WHERE item_id = %s AND owner_id = %s AND chat_id = %s",
            (identifier, owner, chat),
        )
        return None if row is None else _queued_record(row)

    def list(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        limit: int = _MAX_QUEUE_DEPTH,
    ) -> tuple[QueuedFollowUpRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        bounded = _bounded_limit(limit, maximum=_MAX_QUEUE_DEPTH)
        rows = transaction.fetch_all(
            f"SELECT {_FIELDS} FROM chat_queue_item "
            "WHERE owner_id = %s AND chat_id = %s ORDER BY position, item_id LIMIT %s",
            (owner, chat, bounded),
        )
        return tuple(_queued_record(row) for row in rows)

    def update_text(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        item_id: str,
        text: str,
        expected_revision: int,
        updated_at: int,
    ) -> QueuedFollowUpRecord:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        identifier = _required_uuid(item_id, "item_id")
        body = _bounded_text(text, "text", maximum=_MAX_TEXT)
        revision = _required_integer(expected_revision, "expected_revision", minimum=1)
        updated = _required_integer(updated_at, "updated_at")
        if revision >= _MAX_INTEGER:
            raise RepositoryConflictError(
                "queue item revision is exhausted",
                metadata={"operation": "chat_queue.update_text"},
            )
        changed = _optional_row(
            transaction.execute(
                _UPDATE_SQL,
                (body, updated, identifier, owner, chat, revision),
            ),
            "chat_queue.update_text",
        )
        if changed is not None:
            return _queued_record(changed)
        existing = self.get(transaction, owner_id=owner, chat_id=chat, item_id=identifier)
        if existing is None:
            raise RepositoryNotFoundError(
                "queue item was not found",
                metadata={"operation": "chat_queue.update_text"},
            )
        if existing.revision != revision:
            raise RepositoryConflictError(
                "queue item revision is stale",
                metadata={"operation": "chat_queue.update_text"},
            )
        raise RepositoryConflictError(
            "queue item update fence rejected",
            metadata={"operation": "chat_queue.update_text"},
        )

    def delete(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        item_id: str,
        expected_revision: int,
    ) -> bool:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        identifier = _required_uuid(item_id, "item_id")
        revision = _required_integer(expected_revision, "expected_revision", minimum=1)
        result = transaction.execute(
            _DELETE_SQL,
            (identifier, owner, chat, revision),
        )
        if result.rowcount == 1:
            return True
        existing = self.get(transaction, owner_id=owner, chat_id=chat, item_id=identifier)
        if existing is None:
            raise RepositoryNotFoundError(
                "queue item was not found",
                metadata={"operation": "chat_queue.delete"},
            )
        if existing.revision != revision:
            raise RepositoryConflictError(
                "queue item revision is stale",
                metadata={"operation": "chat_queue.delete"},
            )
        raise RepositoryConflictError(
            "queue item deletion fence rejected",
            metadata={"operation": "chat_queue.delete"},
        )

    def reorder(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        ordered_item_ids: Sequence[str],
        expected_revisions: Mapping[str, int],
        reordered_at: int,
    ) -> tuple[QueuedFollowUpRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        updated = _required_integer(reordered_at, "reordered_at")
        if isinstance(ordered_item_ids, (str, bytes)) or not isinstance(ordered_item_ids, Sequence):
            raise RepositoryValidationError("ordered_item_ids must be a sequence of item ids")
        if not isinstance(expected_revisions, Mapping):
            raise RepositoryValidationError("expected_revisions must be a revision mapping")
        ordering: list[tuple[str, int, int]] = []
        seen: set[str] = set()
        for value in ordered_item_ids:
            identifier = _required_uuid(value, "ordered_item_ids entry")
            if identifier in seen:
                raise RepositoryValidationError("ordered_item_ids must not repeat an item")
            seen.add(identifier)
            expected = expected_revisions.get(identifier)
            if expected is None:
                raise RepositoryValidationError(
                    "expected_revisions must fence every reordered item",
                    metadata={"field": "expected_revisions"},
                )
            ordering.append(
                (
                    identifier,
                    len(ordering),
                    _required_integer(expected, "expected_revision", minimum=1),
                )
            )
        if len(ordering) > _MAX_QUEUE_DEPTH:
            raise RepositoryValidationError(
                "reorder exceeds the chat follow-up queue capacity",
                metadata={"field": "ordered_item_ids", "maximum": _MAX_QUEUE_DEPTH},
            )
        if not ordering:
            return ()
        if set(expected_revisions) != seen:
            raise RepositoryValidationError(
                "expected_revisions must not fence items outside the reorder",
                metadata={"field": "expected_revisions"},
            )
        _lock_chat_queue(transaction, owner, chat)
        if not dialect_of(transaction).is_sqlite:
            transaction.execute("SET CONSTRAINTS chat_queue_item_chat_order DEFERRED")
        current = transaction.fetch_all(
            f"SELECT {_FIELDS} FROM chat_queue_item "
            "WHERE owner_id = %s AND chat_id = %s ORDER BY position, item_id FOR UPDATE",
            (owner, chat),
        )
        current_ids = {str(_row_value(row, "item_id")) for row in current}
        if seen != current_ids:
            missing = ",".join(sorted(current_ids - seen))[:256]
            unknown = ",".join(sorted(seen - current_ids))[:256]
            raise RepositoryValidationError(
                "reorder must be a full permutation of the chat queue",
                metadata={"missing": missing, "unknown": unknown},
            )
        current_revisions = {
            str(_row_value(row, "item_id")): _required_integer(
                _row_value(row, "revision"), "revision", minimum=1
            )
            for row in current
        }
        for identifier, _next_position, expected in ordering:
            if current_revisions[identifier] != expected:
                raise RepositoryConflictError(
                    "queue item revision is stale",
                    metadata={"operation": "chat_queue.reorder"},
                )
        parameters: list[object] = [updated]
        for identifier, next_position, expected in ordering:
            parameters.extend((identifier, next_position, expected))
        parameters.extend((owner, chat))
        if dialect_of(transaction).is_sqlite:
            occupied = {row["position"] for row in current} | {item[1] for item in ordering}
            temporary = (
                position for position in range(3 * len(ordering)) if position not in occupied
            )
            for identifier, _next_position, _expected in ordering:
                transaction.execute(
                    "UPDATE chat_queue_item SET position=%s "
                    "WHERE owner_id=%s AND chat_id=%s AND item_id=%s",
                    (next(temporary), owner, chat, identifier),
                )
            rows = tuple(
                row
                for identifier, next_position, expected in ordering
                for row in transaction.execute(
                    f"UPDATE chat_queue_item SET position=%s, revision=revision+1, updated_at=%s "
                    f"WHERE owner_id=%s AND chat_id=%s AND item_id=%s AND revision=%s "
                    f"RETURNING {_FIELDS}",
                    (next_position, updated, owner, chat, identifier, expected),
                ).returned_records
            )
        else:
            result = transaction.execute(_reorder_sql(len(ordering)), tuple(parameters))
            rows = getattr(result, "returned_records", ())
        if len(rows) != len(ordering):
            raise RepositoryConflictError(
                "queue reorder revision fence rejected",
                metadata={"operation": "chat_queue.reorder"},
            )
        records = sorted(
            (_queued_record(row) for row in rows),
            key=lambda record: record.position,
        )
        if [record.item_id for record in records] != [item[0] for item in ordering]:
            raise RepositoryDataError(
                "queue reorder returned an unexpected permutation",
                metadata={"operation": "chat_queue.reorder"},
            )
        return tuple(records)

    def claim_next(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
    ) -> QueuedFollowUpRecord | None:
        owner = _required_id(owner_id, "owner_id")
        chat = _required_id(chat_id, "chat_id")
        claimed = _optional_row(
            transaction.execute(_CLAIM_SQL, (owner, chat)),
            "chat_queue.claim_next",
        )
        if claimed is not None:
            return _queued_record(claimed)
        remaining = transaction.fetch_one(_PRESENCE_SQL, (owner, chat))
        if remaining is None:
            return None
        raise RepositoryConflictError(
            "queue head was claimed by another dispatcher",
            metadata={"operation": "chat_queue.claim_next"},
        )


__all__ = (
    "ChatQueueRepository",
    "QueuedFollowUpRecord",
)
