"""Owner-scoped notification preference, inbox, and delivery-identity persistence.
Callers supply opaque ciphertext and bounded policy inputs inside their own transactions;
routing, provider, IAM, and rendering decisions remain with the embedding host.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from astralplane.contracts import Transaction
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
_MAX_CONTENT_BYTES: Final = 65536


class NotificationEventKind(StrEnum):
    COMPLETION = "completion"
    FAILURE = "failure"
    REQUIRED_ACTION = "required_action"


_EVENT_KINDS: Final = tuple(item.value for item in NotificationEventKind)
_PREFERENCE_FIELDS: Final = (
    "owner_id, in_app_completion, in_app_failure, in_app_required_action, "
    "inbox_completion, inbox_failure, inbox_required_action, "
    "push_completion, push_failure, push_required_action, revision, updated_at"
)
_ENTRY_FIELDS: Final = (
    "entry_id, owner_id, event_kind, source_id, deduplication_key, content_format, "
    "content_ciphertext, content_sha256, created_at, retention_expires_at, "
    "acknowledged_at, deleted_at, revision"
)
_ENTRY_TABLE_FIELDS: Final = (
    "notification_inbox_entry.entry_id, notification_inbox_entry.owner_id, "
    "notification_inbox_entry.event_kind, notification_inbox_entry.source_id, "
    "notification_inbox_entry.deduplication_key, notification_inbox_entry.content_format, "
    "notification_inbox_entry.content_ciphertext, "
    "notification_inbox_entry.content_sha256, notification_inbox_entry.created_at, "
    "notification_inbox_entry.retention_expires_at, "
    "notification_inbox_entry.acknowledged_at, notification_inbox_entry.deleted_at, "
    "notification_inbox_entry.revision"
)
_DELIVERY_FIELDS: Final = "delivery_id, entry_id, owner_id, delivery_key, created_at"


@dataclass(frozen=True, slots=True)
class NotificationPreferenceRecord:
    owner_id: str
    in_app_completion: bool
    in_app_failure: bool
    in_app_required_action: bool
    inbox_completion: bool
    inbox_failure: bool
    inbox_required_action: bool
    push_completion: bool
    push_failure: bool
    push_required_action: bool
    revision: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class NotificationInboxEntry:
    entry_id: str
    owner_id: str
    event_kind: str
    source_id: str
    deduplication_key: str
    content_format: str
    content_ciphertext: bytes
    content_sha256: str
    created_at: int
    retention_expires_at: int
    acknowledged_at: int | None
    deleted_at: int | None
    revision: int


@dataclass(frozen=True, slots=True)
class NotificationDeliveryIdentity:
    delivery_id: str
    entry_id: str
    owner_id: str
    delivery_key: str
    created_at: int


def _required_integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_INTEGER:
        raise RepositoryValidationError(
            f"{field} must be a bounded integer",
            metadata={"field": field, "minimum": minimum, "maximum": _MAX_INTEGER},
        )
    return value


def _required_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise RepositoryValidationError(f"{field} must be a boolean")
    return value


def _required_uuid(value: object, field: str) -> str:
    text = _required_id(value, field, maximum=36)
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise RepositoryValidationError(f"{field} must be a UUID string") from exc


def _required_bytes(value: object, field: str, *, maximum: int) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise RepositoryValidationError(f"{field} must be bytes")
    content = bytes(value)
    if not 1 <= len(content) <= maximum:
        raise RepositoryValidationError(
            f"{field} is outside the supported byte range",
            metadata={"field": field, "maximum": maximum},
        )
    return content


def _required_digest(value: object, field: str) -> str:
    digest = _bounded_text(value, field, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return digest


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


def _preference_record(row: Mapping[str, Any]) -> NotificationPreferenceRecord:
    return NotificationPreferenceRecord(
        owner_id=str(_row_value(row, "owner_id")),
        in_app_completion=bool(_row_value(row, "in_app_completion")),
        in_app_failure=bool(_row_value(row, "in_app_failure")),
        in_app_required_action=bool(_row_value(row, "in_app_required_action")),
        inbox_completion=bool(_row_value(row, "inbox_completion")),
        inbox_failure=bool(_row_value(row, "inbox_failure")),
        inbox_required_action=bool(_row_value(row, "inbox_required_action")),
        push_completion=bool(_row_value(row, "push_completion")),
        push_failure=bool(_row_value(row, "push_failure")),
        push_required_action=bool(_row_value(row, "push_required_action")),
        revision=int(_row_value(row, "revision")),
        updated_at=int(_row_value(row, "updated_at")),
    )


def _entry_record(row: Mapping[str, Any]) -> NotificationInboxEntry:
    content = _row_value(row, "content_ciphertext")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise RepositoryDataError("persisted notification content is not opaque bytes")
    return NotificationInboxEntry(
        entry_id=str(_row_value(row, "entry_id")),
        owner_id=str(_row_value(row, "owner_id")),
        event_kind=str(_row_value(row, "event_kind")),
        source_id=str(_row_value(row, "source_id")),
        deduplication_key=str(_row_value(row, "deduplication_key")),
        content_format=str(_row_value(row, "content_format")),
        content_ciphertext=bytes(content),
        content_sha256=str(_row_value(row, "content_sha256")),
        created_at=int(_row_value(row, "created_at")),
        retention_expires_at=int(_row_value(row, "retention_expires_at")),
        acknowledged_at=(
            None
            if _row_value(row, "acknowledged_at") is None
            else int(_row_value(row, "acknowledged_at"))
        ),
        deleted_at=(
            None
            if _row_value(row, "deleted_at") is None
            else int(_row_value(row, "deleted_at"))
        ),
        revision=int(_row_value(row, "revision")),
    )


def _delivery_record(row: Mapping[str, Any]) -> NotificationDeliveryIdentity:
    return NotificationDeliveryIdentity(
        delivery_id=str(_row_value(row, "delivery_id")),
        entry_id=str(_row_value(row, "entry_id")),
        owner_id=str(_row_value(row, "owner_id")),
        delivery_key=str(_row_value(row, "delivery_key")),
        created_at=int(_row_value(row, "created_at")),
    )


def _preference_values(
    in_app_completion: object,
    in_app_failure: object,
    in_app_required_action: object,
    inbox_completion: object,
    inbox_failure: object,
    inbox_required_action: object,
    push_completion: object,
    push_failure: object,
    push_required_action: object,
) -> tuple[bool, ...]:
    return (
        _required_bool(in_app_completion, "in_app_completion"),
        _required_bool(in_app_failure, "in_app_failure"),
        _required_bool(in_app_required_action, "in_app_required_action"),
        _required_bool(inbox_completion, "inbox_completion"),
        _required_bool(inbox_failure, "inbox_failure"),
        _required_bool(inbox_required_action, "inbox_required_action"),
        _required_bool(push_completion, "push_completion"),
        _required_bool(push_failure, "push_failure"),
        _required_bool(push_required_action, "push_required_action"),
    )


class NotificationRepository:
    def get_preferences(
        self, transaction: Transaction, *, owner_id: str
    ) -> NotificationPreferenceRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = transaction.fetch_one(
            f"SELECT {_PREFERENCE_FIELDS} FROM notification_preference WHERE owner_id = %s",
            (owner,),
        )
        return None if row is None else _preference_record(row)

    def put_preferences(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        expected_revision: int,
        in_app_completion: bool,
        in_app_failure: bool,
        in_app_required_action: bool,
        inbox_completion: bool,
        inbox_failure: bool,
        inbox_required_action: bool,
        push_completion: bool,
        push_failure: bool,
        push_required_action: bool,
        updated_at: int,
    ) -> NotificationPreferenceRecord:
        owner = _required_id(owner_id, "owner_id")
        revision = _required_integer(expected_revision, "expected_revision")
        updated = _required_integer(updated_at, "updated_at")
        values = _preference_values(
            in_app_completion,
            in_app_failure,
            in_app_required_action,
            inbox_completion,
            inbox_failure,
            inbox_required_action,
            push_completion,
            push_failure,
            push_required_action,
        )
        if revision == 0:
            inserted = _optional_row(
                transaction.execute(
                    f"""
                    INSERT INTO notification_preference (
                        owner_id, in_app_completion, in_app_failure,
                        in_app_required_action, inbox_completion, inbox_failure,
                        inbox_required_action, push_completion, push_failure,
                        push_required_action, revision, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                    ON CONFLICT (owner_id) DO NOTHING
                    RETURNING {_PREFERENCE_FIELDS}
                    """,
                    (owner, *values, updated),
                ),
                "notification_preference.insert",
            )
            if inserted is not None:
                return _preference_record(inserted)
            existing = self.get_preferences(transaction, owner_id=owner)
            if existing is None:
                raise RepositoryConflictError(
                    "notification preference creation lost its owner row"
                )
            if (
                (
                    existing.in_app_completion,
                    existing.in_app_failure,
                    existing.in_app_required_action,
                    existing.inbox_completion,
                    existing.inbox_failure,
                    existing.inbox_required_action,
                    existing.push_completion,
                    existing.push_failure,
                    existing.push_required_action,
                    existing.updated_at,
                )
                != (*values, updated)
            ):
                raise RepositoryConflictError(
                    "notification preference creation replay changed immutable values"
                )
            return existing

        row = transaction.fetch_one(
            f"SELECT {_PREFERENCE_FIELDS} FROM notification_preference "
            "WHERE owner_id = %s FOR UPDATE",
            (owner,),
        )
        if row is None:
            raise RepositoryNotFoundError("notification preference not found")
        existing = _preference_record(row)
        if existing.revision != revision:
            raise RepositoryConflictError("notification preference revision is stale")
        if updated < existing.updated_at:
            raise RepositoryConflictError("notification preference update time regressed")
        if (
            (
                existing.in_app_completion,
                existing.in_app_failure,
                existing.in_app_required_action,
                existing.inbox_completion,
                existing.inbox_failure,
                existing.inbox_required_action,
                existing.push_completion,
                existing.push_failure,
                existing.push_required_action,
                existing.updated_at,
            )
            == (*values, updated)
        ):
            return existing
        if revision >= _MAX_INTEGER:
            raise RepositoryConflictError("notification preference revision exhausted")
        changed = _optional_row(
            transaction.execute(
                f"""
                UPDATE notification_preference
                SET in_app_completion = %s, in_app_failure = %s,
                    in_app_required_action = %s, inbox_completion = %s,
                    inbox_failure = %s, inbox_required_action = %s,
                    push_completion = %s, push_failure = %s,
                    push_required_action = %s, revision = revision + 1,
                    updated_at = %s
                WHERE owner_id = %s AND revision = %s
                RETURNING {_PREFERENCE_FIELDS}
                """,
                (*values, updated, owner, revision),
            ),
            "notification_preference.update",
        )
        if changed is None:
            raise RepositoryConflictError("notification preference update fence rejected")
        return _preference_record(changed)

    def create_entry(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        event_kind: str,
        source_id: str,
        deduplication_key: str,
        content_format: str,
        content_ciphertext: bytes,
        content_sha256: str,
        created_at: int,
        retention_expires_at: int,
        entry_id: str | None = None,
    ) -> NotificationInboxEntry:
        owner = _required_id(owner_id, "owner_id")
        if event_kind not in _EVENT_KINDS:
            raise RepositoryValidationError(
                "event_kind is not a supported notification event",
                metadata={"allowed": ",".join(_EVENT_KINDS)},
            )
        source = _bounded_text(source_id, "source_id", maximum=512)
        key = _bounded_text(deduplication_key, "deduplication_key", maximum=512)
        content_type = _bounded_text(content_format, "content_format", maximum=128)
        content = _required_bytes(
            content_ciphertext,
            "content_ciphertext",
            maximum=_MAX_CONTENT_BYTES,
        )
        digest = _required_digest(content_sha256, "content_sha256")
        if hashlib.sha256(content).hexdigest() != digest:
            raise RepositoryValidationError("content_sha256 does not match ciphertext")
        created = _required_integer(created_at, "created_at")
        retention = _required_integer(
            retention_expires_at,
            "retention_expires_at",
            minimum=1,
        )
        if retention <= created:
            raise RepositoryValidationError(
                "retention_expires_at must be later than created_at"
            )
        entry = (
            str(uuid.uuid4())
            if entry_id is None
            else _required_uuid(entry_id, "entry_id")
        )
        inserted = _optional_row(
            transaction.execute(
                f"""
                INSERT INTO notification_inbox_entry (
                    entry_id, owner_id, event_kind, source_id,
                    deduplication_key, content_format, content_ciphertext,
                    content_sha256, created_at, retention_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING {_ENTRY_FIELDS}
                """,
                (
                    entry,
                    owner,
                    event_kind,
                    source,
                    key,
                    content_type,
                    content,
                    digest,
                    created,
                    retention,
                ),
            ),
            "notification_inbox_entry.insert",
        )
        if inserted is not None:
            return _entry_record(inserted)
        existing = self.get_entry_by_deduplication_key(
            transaction,
            owner_id=owner,
            deduplication_key=key,
            include_deleted=True,
        )
        if existing is None:
            raise RepositoryConflictError(
                "notification inbox entry identity already exists"
            )
        if (
            existing.event_kind,
            existing.source_id,
            existing.deduplication_key,
            existing.content_format,
            existing.content_ciphertext,
            existing.content_sha256,
            existing.created_at,
            existing.retention_expires_at,
        ) != (
            event_kind,
            source,
            key,
            content_type,
            content,
            digest,
            created,
            retention,
        ):
            raise RepositoryConflictError(
                "notification deduplication key replay changed immutable content"
            )
        return existing

    def get_entry(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        entry_id: str,
        include_deleted: bool = False,
    ) -> NotificationInboxEntry | None:
        owner = _required_id(owner_id, "owner_id")
        entry = _required_uuid(entry_id, "entry_id")
        if type(include_deleted) is not bool:
            raise RepositoryValidationError("include_deleted must be a boolean")
        deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
        row = transaction.fetch_one(
            f"SELECT {_ENTRY_FIELDS} FROM notification_inbox_entry "
            f"WHERE entry_id = %s AND owner_id = %s{deleted_filter}",
            (entry, owner),
        )
        return None if row is None else _entry_record(row)

    def get_entry_by_deduplication_key(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        deduplication_key: str,
        include_deleted: bool = False,
    ) -> NotificationInboxEntry | None:
        owner = _required_id(owner_id, "owner_id")
        key = _bounded_text(deduplication_key, "deduplication_key", maximum=512)
        if type(include_deleted) is not bool:
            raise RepositoryValidationError("include_deleted must be a boolean")
        deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
        row = transaction.fetch_one(
            f"SELECT {_ENTRY_FIELDS} FROM notification_inbox_entry "
            f"WHERE owner_id = %s AND deduplication_key = %s{deleted_filter}",
            (owner, key),
        )
        return None if row is None else _entry_record(row)

    def list_entries(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int,
        include_deleted: bool = False,
        unacknowledged_only: bool = False,
    ) -> tuple[NotificationInboxEntry, ...]:
        owner = _required_id(owner_id, "owner_id")
        bounded = _bounded_limit(limit, maximum=200)
        if type(include_deleted) is not bool or type(unacknowledged_only) is not bool:
            raise RepositoryValidationError(
                "include_deleted and unacknowledged_only must be booleans"
            )
        filters = ["owner_id = %s"]
        if not include_deleted:
            filters.append("deleted_at IS NULL")
        if unacknowledged_only:
            filters.append("acknowledged_at IS NULL")
        rows = transaction.fetch_all(
            f"SELECT {_ENTRY_FIELDS} FROM notification_inbox_entry "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY created_at DESC, entry_id DESC LIMIT %s",
            (owner, bounded),
        )
        return tuple(_entry_record(row) for row in rows)

    def acknowledge_entry(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        entry_id: str,
        expected_revision: int,
        acknowledged_at: int,
    ) -> NotificationInboxEntry:
        owner = _required_id(owner_id, "owner_id")
        entry = _required_uuid(entry_id, "entry_id")
        revision = _required_integer(expected_revision, "expected_revision")
        at = _required_integer(acknowledged_at, "acknowledged_at")
        changed = None
        if revision < _MAX_INTEGER:
            changed = _optional_row(
                transaction.execute(
                    f"""
                    UPDATE notification_inbox_entry
                    SET acknowledged_at = %s, revision = revision + 1
                    WHERE entry_id = %s AND owner_id = %s AND revision = %s
                      AND acknowledged_at IS NULL AND deleted_at IS NULL
                      AND created_at <= %s
                    RETURNING {_ENTRY_FIELDS}
                    """,
                    (at, entry, owner, revision, at),
                ),
                "notification_inbox_entry.acknowledge",
            )
        if changed is not None:
            return _entry_record(changed)
        existing = self.get_entry(
            transaction,
            owner_id=owner,
            entry_id=entry,
            include_deleted=True,
        )
        if existing is None:
            raise RepositoryNotFoundError("notification inbox entry not found")
        if existing.deleted_at is not None:
            raise RepositoryConflictError("deleted notification entry cannot be acknowledged")
        if existing.acknowledged_at is not None:
            if existing.acknowledged_at == at:
                return existing
            raise RepositoryConflictError("notification entry was already acknowledged")
        if existing.revision != revision:
            raise RepositoryConflictError("notification entry revision is stale")
        if at < existing.created_at:
            raise RepositoryConflictError("acknowledgment precedes entry creation")
        if revision >= _MAX_INTEGER:
            raise RepositoryConflictError("notification entry revision exhausted")
        raise RepositoryConflictError("notification acknowledgment fence rejected")

    def delete_entry(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        entry_id: str,
        expected_revision: int,
        deleted_at: int,
    ) -> NotificationInboxEntry:
        owner = _required_id(owner_id, "owner_id")
        entry = _required_uuid(entry_id, "entry_id")
        revision = _required_integer(expected_revision, "expected_revision")
        at = _required_integer(deleted_at, "deleted_at")
        changed = None
        if revision < _MAX_INTEGER:
            changed = _optional_row(
                transaction.execute(
                    f"""
                    UPDATE notification_inbox_entry
                    SET deleted_at = %s, revision = revision + 1
                    WHERE entry_id = %s AND owner_id = %s AND revision = %s
                      AND deleted_at IS NULL AND created_at <= %s
                    RETURNING {_ENTRY_FIELDS}
                    """,
                    (at, entry, owner, revision, at),
                ),
                "notification_inbox_entry.delete",
            )
        if changed is not None:
            return _entry_record(changed)
        existing = self.get_entry(
            transaction,
            owner_id=owner,
            entry_id=entry,
            include_deleted=True,
        )
        if existing is None:
            raise RepositoryNotFoundError("notification inbox entry not found")
        if existing.deleted_at is not None:
            if existing.deleted_at == at:
                return existing
            raise RepositoryConflictError("notification entry was already deleted")
        if existing.revision != revision:
            raise RepositoryConflictError("notification entry revision is stale")
        if at < existing.created_at:
            raise RepositoryConflictError("deletion precedes entry creation")
        if revision >= _MAX_INTEGER:
            raise RepositoryConflictError("notification entry revision exhausted")
        raise RepositoryConflictError("notification deletion fence rejected")

    def purge_expired_entries(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        expired_at: int,
        limit: int,
    ) -> tuple[NotificationInboxEntry, ...]:
        owner = _required_id(owner_id, "owner_id")
        at = _required_integer(expired_at, "expired_at")
        bounded = _bounded_limit(limit, maximum=200)
        rows = transaction.fetch_all(
            f"""
            WITH expired AS (
                SELECT entry_id
                FROM notification_inbox_entry
                WHERE owner_id = %s AND deleted_at IS NULL
                  AND retention_expires_at <= %s AND revision < %s
                ORDER BY retention_expires_at, entry_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE notification_inbox_entry
            SET deleted_at = %s, revision = notification_inbox_entry.revision + 1
            FROM expired
            WHERE notification_inbox_entry.entry_id = expired.entry_id
              AND notification_inbox_entry.owner_id = %s
              AND notification_inbox_entry.deleted_at IS NULL
            RETURNING {_ENTRY_TABLE_FIELDS}
            """,
            (owner, at, _MAX_INTEGER, bounded, at, owner),
        )
        records = (_entry_record(row) for row in rows)
        return tuple(
            sorted(records, key=lambda record: (record.retention_expires_at, record.entry_id))
        )

    def register_delivery_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        entry_id: str,
        delivery_key: str,
        created_at: int,
        delivery_id: str | None = None,
    ) -> NotificationDeliveryIdentity:
        owner = _required_id(owner_id, "owner_id")
        entry = _required_uuid(entry_id, "entry_id")
        key = _bounded_text(delivery_key, "delivery_key", maximum=512)
        created = _required_integer(created_at, "created_at")
        delivery = (
            str(uuid.uuid4())
            if delivery_id is None
            else _required_uuid(delivery_id, "delivery_id")
        )
        entry_row = transaction.fetch_one(
            "SELECT entry_id, deleted_at FROM notification_inbox_entry "
            "WHERE entry_id = %s AND owner_id = %s FOR UPDATE",
            (entry, owner),
        )
        if entry_row is None:
            raise RepositoryNotFoundError("notification inbox entry not found")
        if entry_row["deleted_at"] is not None:
            raise RepositoryConflictError("deleted notification entry has no delivery identity")
        inserted = _optional_row(
            transaction.execute(
                f"""
                INSERT INTO notification_delivery_identity (
                    delivery_id, entry_id, owner_id, delivery_key, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING {_DELIVERY_FIELDS}
                """,
                (delivery, entry, owner, key, created),
            ),
            "notification_delivery_identity.insert",
        )
        if inserted is not None:
            return _delivery_record(inserted)
        existing_row = transaction.fetch_one(
            f"SELECT {_DELIVERY_FIELDS} FROM notification_delivery_identity "
            "WHERE owner_id = %s AND delivery_key = %s",
            (owner, key),
        )
        if existing_row is None:
            raise RepositoryConflictError(
                "notification delivery identity already exists"
            )
        existing = _delivery_record(existing_row)
        if existing.entry_id != entry or existing.created_at != created:
            raise RepositoryConflictError(
                "notification delivery identity replay changed immutable semantics"
            )
        return existing

    def get_delivery_identity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        delivery_id: str,
    ) -> NotificationDeliveryIdentity | None:
        owner = _required_id(owner_id, "owner_id")
        delivery = _required_uuid(delivery_id, "delivery_id")
        row = transaction.fetch_one(
            f"SELECT {_DELIVERY_FIELDS} FROM notification_delivery_identity "
            "WHERE delivery_id = %s AND owner_id = %s",
            (delivery, owner),
        )
        return None if row is None else _delivery_record(row)

    def list_delivery_identities(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int,
        entry_id: str | None = None,
    ) -> tuple[NotificationDeliveryIdentity, ...]:
        owner = _required_id(owner_id, "owner_id")
        bounded = _bounded_limit(limit, maximum=200)
        parameters: tuple[object, ...]
        if entry_id is None:
            filters = "owner_id = %s"
            parameters = (owner, bounded)
        else:
            entry = _required_uuid(entry_id, "entry_id")
            filters = "owner_id = %s AND entry_id = %s"
            parameters = (owner, entry, bounded)
        rows = transaction.fetch_all(
            f"SELECT {_DELIVERY_FIELDS} FROM notification_delivery_identity "
            f"WHERE {filters} ORDER BY created_at DESC, delivery_id DESC LIMIT %s",
            parameters,
        )
        return tuple(_delivery_record(row) for row in rows)


__all__ = (
    "NotificationDeliveryIdentity",
    "NotificationEventKind",
    "NotificationInboxEntry",
    "NotificationPreferenceRecord",
    "NotificationRepository",
)