"""Owner-scoped opaque push destinations and a fenced delivery outbox. Callers supply
encrypted destination tokens, payload snapshots, lease times, and bounded retry policy;
provider calls, token custody, encryption, IAM, and delivery decisions stay with the host.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
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
_MAX_CONTENT_BYTES: Final = 65536
_MAX_ATTEMPTS: Final = 100
_MAX_LEASE_SECONDS: Final = 86400
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_DELIVERY_STATUSES: Final = (
    "pending",
    "claimed",
    "retry",
    "delivered",
    "dead_letter",
    "superseded",
)
_EVENT_KINDS: Final = ("completion", "failure", "required_action")

_DESTINATION_FIELDS: Final = (
    "destination_id, owner_id, destination_key, token_format, token_ciphertext, "
    "token_sha256, token_version, revision, created_at, updated_at, revoked_at, deleted_at"
)
_DELIVERY_FIELDS: Final = (
    "delivery_id, owner_id, entry_id, destination_id, delivery_key, event_kind, "
    "content_format, content_ciphertext, content_sha256, token_version, "
    "destination_revision, status, attempt_count, max_attempts, available_at, "
    "lease_owner, claim_id, lease_expires_at, claimed_at, acknowledged_at, "
    "last_error_code, created_at, updated_at, version"
)
_DELIVERY_ALIASED_FIELDS: Final = ", ".join(
    f"delivery.{name.strip()}" for name in _DELIVERY_FIELDS.split(",")
)
_CLAIM_FIELDS: Final = (
    _DELIVERY_ALIASED_FIELDS
    + ", destination.token_format AS destination_token_format, "
    + "destination.token_ciphertext AS destination_token_ciphertext, "
    + "destination.token_sha256 AS destination_token_sha256"
)

_INSERT_DESTINATION_SQL: Final = f"""
INSERT INTO notification_device_destination (
    destination_id, owner_id, destination_key, token_format, token_ciphertext,
    token_sha256, token_version, revision, created_at, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s, %s)
ON CONFLICT DO NOTHING
RETURNING {_DESTINATION_FIELDS}
""".strip()

_ROTATE_DESTINATION_SQL: Final = f"""
UPDATE notification_device_destination
SET token_format = %s, token_ciphertext = %s, token_sha256 = %s,
    token_version = token_version + 1, revision = revision + 1, updated_at = %s
WHERE destination_id = %s AND owner_id = %s AND revision = %s
  AND token_version = %s AND revoked_at IS NULL AND deleted_at IS NULL
RETURNING {_DESTINATION_FIELDS}
""".strip()

_REVOKE_DESTINATION_SQL: Final = f"""
UPDATE notification_device_destination
SET revoked_at = %s, revision = revision + 1, updated_at = %s
WHERE destination_id = %s AND owner_id = %s AND revision = %s
  AND revoked_at IS NULL AND deleted_at IS NULL
RETURNING {_DESTINATION_FIELDS}
""".strip()

_DELETE_DESTINATION_SQL: Final = f"""
UPDATE notification_device_destination
SET deleted_at = %s, updated_at = %s, token_format = NULL,
    token_ciphertext = NULL, token_sha256 = NULL, revision = revision + 1
WHERE destination_id = %s AND owner_id = %s AND revision = %s
  AND deleted_at IS NULL
RETURNING {_DESTINATION_FIELDS}
""".strip()

_INSERT_DELIVERY_SQL: Final = f"""
INSERT INTO notification_push_delivery (
    delivery_id, owner_id, entry_id, destination_id, delivery_key, event_kind,
    content_format, content_ciphertext, content_sha256, token_version,
    destination_revision, status, attempt_count, max_attempts, available_at,
    created_at, updated_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 0, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
RETURNING {_DELIVERY_FIELDS}
""".strip()

_CLAIM_SQL: Final = f"""
WITH exhausted AS (
    UPDATE notification_push_delivery AS delivery
    SET status = 'dead_letter', lease_owner = NULL, lease_expires_at = NULL,
        last_error_code = 'lease_expired', version = delivery.version + 1,
        updated_at = %s
    WHERE delivery.owner_id = %s AND delivery.status = 'claimed'
      AND delivery.lease_expires_at <= %s
      AND delivery.attempt_count >= delivery.max_attempts
    RETURNING delivery.delivery_id
),
candidates AS (
    SELECT delivery.delivery_id
    FROM notification_push_delivery AS delivery
    JOIN notification_device_destination AS destination
      ON destination.destination_id = delivery.destination_id
     AND destination.owner_id = delivery.owner_id
    WHERE delivery.owner_id = %s
      AND delivery.attempt_count < delivery.max_attempts
      AND (
          (delivery.status IN ('pending','retry') AND delivery.available_at <= %s)
          OR (delivery.status = 'claimed' AND delivery.lease_expires_at <= %s)
      )
      AND destination.deleted_at IS NULL
      AND destination.revoked_at IS NULL
      AND destination.token_version = delivery.token_version
      AND destination.revision = delivery.destination_revision
    ORDER BY delivery.available_at, delivery.delivery_id
    FOR UPDATE OF delivery SKIP LOCKED
    FOR SHARE OF destination
    LIMIT %s
)
UPDATE notification_push_delivery AS delivery
SET status = 'claimed', lease_owner = %s, claim_id = %s,
    lease_expires_at = %s, claimed_at = %s,
    attempt_count = delivery.attempt_count + 1, last_error_code = NULL,
    version = delivery.version + 1, updated_at = %s
FROM candidates, notification_device_destination AS destination
WHERE delivery.delivery_id = candidates.delivery_id
  AND destination.destination_id = delivery.destination_id
  AND destination.owner_id = delivery.owner_id
  AND destination.deleted_at IS NULL
  AND destination.revoked_at IS NULL
  AND destination.token_version = delivery.token_version
  AND destination.revision = delivery.destination_revision
  AND delivery.version < %s
RETURNING {_CLAIM_FIELDS}
""".strip()

_ACK_SQL: Final = f"""
UPDATE notification_push_delivery
SET status = 'delivered', lease_owner = NULL, lease_expires_at = NULL,
    acknowledged_at = %s, last_error_code = NULL,
    version = version + 1, updated_at = %s
WHERE delivery_id = %s AND owner_id = %s AND claim_id = %s
  AND status IN ('claimed','retry','dead_letter','superseded')
  AND acknowledged_at IS NULL AND claimed_at <= %s
  AND updated_at <= %s AND version < %s
RETURNING {_DELIVERY_FIELDS}
""".strip()

_FAIL_SQL: Final = f"""
UPDATE notification_push_delivery
SET status = CASE
        WHEN attempt_count >= max_attempts OR %s::bigint IS NULL THEN 'dead_letter'
        ELSE 'retry'
    END,
    available_at = CASE
        WHEN attempt_count >= max_attempts OR %s::bigint IS NULL THEN available_at
        ELSE %s
    END,
    lease_owner = NULL, lease_expires_at = NULL, last_error_code = %s,
    version = version + 1, updated_at = %s
WHERE delivery_id = %s AND owner_id = %s AND claim_id = %s
  AND status IN ('claimed','retry') AND acknowledged_at IS NULL
  AND claimed_at <= %s AND updated_at < %s AND version < %s
RETURNING {_DELIVERY_FIELDS}
""".strip()

_EXPIRE_STALE_SQL: Final = f"""
WITH stale AS (
    SELECT delivery.delivery_id
    FROM notification_push_delivery AS delivery
    JOIN notification_device_destination AS destination
      ON destination.destination_id = delivery.destination_id
     AND destination.owner_id = delivery.owner_id
    WHERE delivery.owner_id = %s
      AND delivery.status IN ('pending','retry')
      AND delivery.version < %s
      AND (
          destination.deleted_at IS NOT NULL
          OR destination.revoked_at IS NOT NULL
          OR destination.token_version <> delivery.token_version
          OR destination.revision <> delivery.destination_revision
      )
    ORDER BY delivery.created_at, delivery.delivery_id
    FOR UPDATE OF delivery SKIP LOCKED
    LIMIT %s
)
UPDATE notification_push_delivery AS delivery
SET status = 'superseded', lease_owner = NULL, lease_expires_at = NULL,
    version = delivery.version + 1, updated_at = %s
FROM stale
WHERE delivery.delivery_id = stale.delivery_id
RETURNING {_DELIVERY_ALIASED_FIELDS}
""".strip()


@dataclass(frozen=True, slots=True)
class PushDestinationRecord:
    owner_id: str
    destination_id: str
    destination_key: str
    token_format: str | None
    token_ciphertext: bytes | None = field(repr=False)
    token_sha256: str | None
    token_version: int
    revision: int
    created_at: int
    updated_at: int
    revoked_at: int | None
    deleted_at: int | None


@dataclass(frozen=True, slots=True)
class PushDeliveryRecord:
    delivery_id: str
    owner_id: str
    entry_id: str
    destination_id: str
    delivery_key: str
    event_kind: str
    content_format: str
    content_ciphertext: bytes = field(repr=False)
    content_sha256: str
    token_version: int
    destination_revision: int
    status: str
    attempt_count: int
    max_attempts: int
    available_at: int
    lease_owner: str | None
    claim_id: str | None
    lease_expires_at: int | None
    claimed_at: int | None
    acknowledged_at: int | None
    last_error_code: str | None
    created_at: int
    updated_at: int
    version: int


@dataclass(frozen=True, slots=True)
class ClaimedPushDelivery:
    delivery: PushDeliveryRecord
    worker_id: str
    token_format: str
    token_ciphertext: bytes = field(repr=False)
    token_sha256: str


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


def _required_bytes(value: object, field_name: str, *, maximum: int) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise RepositoryValidationError(f"{field_name} must be bytes")
    content = bytes(value)
    if not 1 <= len(content) <= maximum:
        raise RepositoryValidationError(
            f"{field_name} is outside the supported byte range",
            metadata={"field": field_name, "maximum": maximum},
        )
    return content


def _required_digest(value: object, field_name: str) -> str:
    digest = _bounded_text(value, field_name, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise RepositoryValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _required_error_code(value: object) -> str:
    if not isinstance(value, str) or _ERROR_CODE.fullmatch(value) is None:
        raise RepositoryValidationError("last_error_code is not a bounded error code")
    return value


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


def _optional_integer(row: Mapping[str, Any], field_name: str) -> int | None:
    value = _row_value(row, field_name)
    if value is None:
        return None
    if type(value) is not int:
        raise RepositoryDataError(
            "persisted push record contains a non-integer field",
            metadata={"field": field_name},
        )
    return int(value)


def _destination_record(row: Mapping[str, Any]) -> PushDestinationRecord:
    token_ciphertext = _row_value(row, "token_ciphertext")
    if token_ciphertext is not None and not isinstance(
        token_ciphertext, (bytes, bytearray, memoryview)
    ):
        raise RepositoryDataError("persisted destination token is not opaque bytes")
    record = PushDestinationRecord(
        owner_id=str(_row_value(row, "owner_id")),
        destination_id=str(_row_value(row, "destination_id")),
        destination_key=str(_row_value(row, "destination_key")),
        token_format=(
            None
            if _row_value(row, "token_format") is None
            else str(_row_value(row, "token_format"))
        ),
        token_ciphertext=(
            None if token_ciphertext is None else bytes(token_ciphertext)
        ),
        token_sha256=(
            None
            if _row_value(row, "token_sha256") is None
            else str(_row_value(row, "token_sha256"))
        ),
        token_version=int(_row_value(row, "token_version")),
        revision=int(_row_value(row, "revision")),
        created_at=int(_row_value(row, "created_at")),
        updated_at=int(_row_value(row, "updated_at")),
        revoked_at=_optional_integer(row, "revoked_at"),
        deleted_at=_optional_integer(row, "deleted_at"),
    )
    live_shape = (
        record.token_format is not None
        and record.token_ciphertext is not None
        and record.token_sha256 is not None
    )
    if record.deleted_at is None and not live_shape:
        raise RepositoryDataError("live destination is missing its opaque token")
    if record.deleted_at is not None and live_shape:
        raise RepositoryDataError("deleted destination retained its opaque token")
    return record


def _delivery_record(row: Mapping[str, Any]) -> PushDeliveryRecord:
    content = _row_value(row, "content_ciphertext")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise RepositoryDataError("persisted push payload is not opaque bytes")
    status = str(_row_value(row, "status"))
    if status not in _DELIVERY_STATUSES:
        raise RepositoryDataError("persisted push delivery has an unknown status")
    record = PushDeliveryRecord(
        delivery_id=str(_row_value(row, "delivery_id")),
        owner_id=str(_row_value(row, "owner_id")),
        entry_id=str(_row_value(row, "entry_id")),
        destination_id=str(_row_value(row, "destination_id")),
        delivery_key=str(_row_value(row, "delivery_key")),
        event_kind=str(_row_value(row, "event_kind")),
        content_format=str(_row_value(row, "content_format")),
        content_ciphertext=bytes(content),
        content_sha256=str(_row_value(row, "content_sha256")),
        token_version=int(_row_value(row, "token_version")),
        destination_revision=int(_row_value(row, "destination_revision")),
        status=status,
        attempt_count=int(_row_value(row, "attempt_count")),
        max_attempts=int(_row_value(row, "max_attempts")),
        available_at=int(_row_value(row, "available_at")),
        lease_owner=(
            None if _row_value(row, "lease_owner") is None else str(_row_value(row, "lease_owner"))
        ),
        claim_id=(
            None if _row_value(row, "claim_id") is None else str(_row_value(row, "claim_id"))
        ),
        lease_expires_at=_optional_integer(row, "lease_expires_at"),
        claimed_at=_optional_integer(row, "claimed_at"),
        acknowledged_at=_optional_integer(row, "acknowledged_at"),
        last_error_code=(
            None
            if _row_value(row, "last_error_code") is None
            else str(_row_value(row, "last_error_code"))
        ),
        created_at=int(_row_value(row, "created_at")),
        updated_at=int(_row_value(row, "updated_at")),
        version=int(_row_value(row, "version")),
    )
    if record.status == "claimed" and (
        record.lease_owner is None
        or record.claim_id is None
        or record.lease_expires_at is None
        or record.claimed_at is None
    ):
        raise RepositoryDataError("claimed push delivery has an incomplete lease")
    if record.attempt_count == 0 and (
        record.claim_id is not None or record.claimed_at is not None
    ):
        raise RepositoryDataError("unclaimed push delivery carries claim state")
    if record.attempt_count > 0 and (
        record.claim_id is None or record.claimed_at is None
    ):
        raise RepositoryDataError("attempted push delivery lost its claim identity")
    return record


def _claimed_record(row: Mapping[str, Any]) -> ClaimedPushDelivery:
    delivery = _delivery_record(row)
    token_format = _row_value(row, "destination_token_format")
    token_ciphertext = _row_value(row, "destination_token_ciphertext")
    token_sha256 = _row_value(row, "destination_token_sha256")
    if (
        not isinstance(token_format, str)
        or not isinstance(token_ciphertext, (bytes, bytearray, memoryview))
        or not isinstance(token_sha256, str)
    ):
        raise RepositoryDataError("claimed push delivery is missing its opaque destination token")
    return ClaimedPushDelivery(
        delivery=delivery,
        worker_id=delivery.lease_owner or "",
        token_format=token_format,
        token_ciphertext=bytes(token_ciphertext),
        token_sha256=token_sha256,
    )


def _lease_bounds(now: object, lease_expires_at: object) -> tuple[int, int]:
    current = _required_integer(now, "now")
    expiry = _required_integer(lease_expires_at, "lease_expires_at", minimum=1)
    if expiry <= current:
        raise RepositoryValidationError("lease_expires_at must be later than now")
    if expiry - current > _MAX_LEASE_SECONDS:
        raise RepositoryValidationError("lease duration exceeds its supported bound")
    return current, expiry


class PushDeliveryRepository:
    def register_destination(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        destination_key: str,
        token_format: str,
        token_ciphertext: bytes,
        token_sha256: str,
        created_at: int,
        destination_id: str | None = None,
    ) -> PushDestinationRecord:
        owner = _required_id(owner_id, "owner_id")
        key = _bounded_text(destination_key, "destination_key", maximum=512)
        token_type = _bounded_text(token_format, "token_format", maximum=128)
        token = _required_bytes(
            token_ciphertext, "token_ciphertext", maximum=_MAX_CONTENT_BYTES
        )
        digest = _required_digest(token_sha256, "token_sha256")
        if hashlib.sha256(token).hexdigest() != digest:
            raise RepositoryValidationError("token_sha256 does not match ciphertext")
        created = _required_integer(created_at, "created_at")
        destination = (
            str(uuid.uuid4())
            if destination_id is None
            else _required_uuid(destination_id, "destination_id")
        )
        inserted = _optional_row(
            transaction.execute(
                _INSERT_DESTINATION_SQL,
                (destination, owner, key, token_type, token, digest, created, created),
            ),
            "notification_device_destination.insert",
        )
        if inserted is not None:
            return _destination_record(inserted)
        existing = self.get_destination(
            transaction,
            owner_id=owner,
            destination_id=None,
            destination_key=key,
            include_deleted=True,
        )
        if existing is None:
            raise RepositoryConflictError(
                "push destination identity already exists"
            )
        if (
            existing.destination_key,
            existing.token_format,
            existing.token_ciphertext,
            existing.token_sha256,
            existing.created_at,
        ) != (key, token_type, token, digest, created):
            raise RepositoryConflictError(
                "push destination replay changed immutable token semantics"
            )
        return existing

    def get_destination(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        destination_id: str | None = None,
        destination_key: str | None = None,
        include_deleted: bool = False,
    ) -> PushDestinationRecord | None:
        owner = _required_id(owner_id, "owner_id")
        if (destination_id is None) == (destination_key is None):
            raise RepositoryValidationError(
                "exactly one destination identity must be supplied"
            )
        if type(include_deleted) is not bool:
            raise RepositoryValidationError("include_deleted must be a boolean")
        filters = ["owner_id = %s"]
        parameters: tuple[object, ...] = (owner,)
        if destination_id is not None:
            filters.append("destination_id = %s")
            parameters = (*parameters, _required_uuid(destination_id, "destination_id"))
        else:
            filters.append("destination_key = %s")
            parameters = (
                *parameters,
                _bounded_text(destination_key, "destination_key", maximum=512),
            )
        if not include_deleted:
            filters.append("deleted_at IS NULL")
        row = transaction.fetch_one(
            f"SELECT {_DESTINATION_FIELDS} FROM notification_device_destination "
            f"WHERE {' AND '.join(filters)}",
            parameters,
        )
        return None if row is None else _destination_record(row)

    def list_destinations(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int,
        include_deleted: bool = False,
    ) -> tuple[PushDestinationRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        bounded = _bounded_limit(limit, maximum=200)
        if type(include_deleted) is not bool:
            raise RepositoryValidationError("include_deleted must be a boolean")
        filter_sql = "" if include_deleted else " AND deleted_at IS NULL"
        rows = transaction.fetch_all(
            f"SELECT {_DESTINATION_FIELDS} FROM notification_device_destination "
            f"WHERE owner_id = %s{filter_sql} "
            "ORDER BY created_at DESC, destination_id DESC LIMIT %s",
            (owner, bounded),
        )
        return tuple(_destination_record(row) for row in rows)

    def rotate_destination_token(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        destination_id: str,
        expected_revision: int,
        expected_token_version: int,
        token_format: str,
        token_ciphertext: bytes,
        token_sha256: str,
        rotated_at: int,
    ) -> PushDestinationRecord:
        owner = _required_id(owner_id, "owner_id")
        destination = _required_uuid(destination_id, "destination_id")
        revision = _required_integer(expected_revision, "expected_revision", minimum=1)
        token_version = _required_integer(
            expected_token_version, "expected_token_version", minimum=1
        )
        token_type = _bounded_text(token_format, "token_format", maximum=128)
        token = _required_bytes(
            token_ciphertext, "token_ciphertext", maximum=_MAX_CONTENT_BYTES
        )
        digest = _required_digest(token_sha256, "token_sha256")
        if hashlib.sha256(token).hexdigest() != digest:
            raise RepositoryValidationError("token_sha256 does not match ciphertext")
        rotated = _required_integer(rotated_at, "rotated_at")
        if revision >= _MAX_INTEGER or token_version >= _MAX_INTEGER:
            raise RepositoryConflictError("push destination revision is exhausted")
        changed = _optional_row(
            transaction.execute(
                _ROTATE_DESTINATION_SQL,
                (
                    token_type,
                    token,
                    digest,
                    rotated,
                    destination,
                    owner,
                    revision,
                    token_version,
                ),
            ),
            "notification_device_destination.rotate",
        )
        if changed is not None:
            return _destination_record(changed)
        existing = self.get_destination(
            transaction,
            owner_id=owner,
            destination_id=destination,
            include_deleted=True,
        )
        if existing is None:
            raise RepositoryNotFoundError("push destination not found")
        if (
            existing.revision == revision + 1
            and existing.token_version == token_version + 1
            and existing.updated_at == rotated
            and existing.token_format == token_type
            and existing.token_ciphertext == token
            and existing.token_sha256 == digest
            and existing.revoked_at is None
            and existing.deleted_at is None
        ):
            return existing
        if existing.deleted_at is not None:
            raise RepositoryConflictError("deleted push destination cannot rotate")
        if existing.revoked_at is not None:
            raise RepositoryConflictError("revoked push destination cannot rotate")
        if existing.revision != revision:
            raise RepositoryConflictError("push destination revision is stale")
        if existing.token_version != token_version:
            raise RepositoryConflictError("push destination token version is stale")
        raise RepositoryConflictError("push destination rotation fence rejected")

    def revoke_destination(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        destination_id: str,
        expected_revision: int,
        revoked_at: int,
    ) -> PushDestinationRecord:
        owner = _required_id(owner_id, "owner_id")
        destination = _required_uuid(destination_id, "destination_id")
        revision = _required_integer(expected_revision, "expected_revision", minimum=1)
        revoked = _required_integer(revoked_at, "revoked_at")
        if revision >= _MAX_INTEGER:
            raise RepositoryConflictError("push destination revision is exhausted")
        changed = _optional_row(
            transaction.execute(
                _REVOKE_DESTINATION_SQL,
                (revoked, revoked, destination, owner, revision),
            ),
            "notification_device_destination.revoke",
        )
        if changed is not None:
            return _destination_record(changed)
        existing = self.get_destination(
            transaction,
            owner_id=owner,
            destination_id=destination,
            include_deleted=True,
        )
        if existing is None:
            raise RepositoryNotFoundError("push destination not found")
        if (
            existing.revoked_at == revoked
            and existing.updated_at == revoked
            and existing.deleted_at is None
        ):
            return existing
        if existing.deleted_at is not None:
            raise RepositoryConflictError("deleted push destination cannot be revoked")
        if existing.revoked_at is not None:
            raise RepositoryConflictError("push destination was already revoked")
        if existing.revision != revision:
            raise RepositoryConflictError("push destination revision is stale")
        if revoked < existing.created_at:
            raise RepositoryConflictError("revocation precedes destination creation")
        raise RepositoryConflictError("push destination revocation fence rejected")

    def delete_destination(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        destination_id: str,
        expected_revision: int,
        deleted_at: int,
    ) -> PushDestinationRecord:
        owner = _required_id(owner_id, "owner_id")
        destination = _required_uuid(destination_id, "destination_id")
        revision = _required_integer(expected_revision, "expected_revision", minimum=1)
        deleted = _required_integer(deleted_at, "deleted_at")
        if revision >= _MAX_INTEGER:
            raise RepositoryConflictError("push destination revision is exhausted")
        changed = _optional_row(
            transaction.execute(
                _DELETE_DESTINATION_SQL,
                (deleted, deleted, destination, owner, revision),
            ),
            "notification_device_destination.delete",
        )
        if changed is not None:
            return _destination_record(changed)
        existing = self.get_destination(
            transaction,
            owner_id=owner,
            destination_id=destination,
            include_deleted=True,
        )
        if existing is None:
            raise RepositoryNotFoundError("push destination not found")
        if existing.deleted_at == deleted and existing.updated_at == deleted:
            return existing
        if existing.deleted_at is not None:
            raise RepositoryConflictError("push destination was already deleted")
        if existing.revision != revision:
            raise RepositoryConflictError("push destination revision is stale")
        if deleted < existing.created_at:
            raise RepositoryConflictError("deletion precedes destination creation")
        if existing.revoked_at is not None and deleted < existing.revoked_at:
            raise RepositoryConflictError("deletion precedes destination revocation")
        raise RepositoryConflictError("push destination deletion fence rejected")

    def enqueue_delivery(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        entry_id: str,
        destination_id: str,
        delivery_key: str,
        expected_destination_revision: int,
        expected_token_version: int,
        max_attempts: int,
        available_at: int,
        created_at: int,
        delivery_id: str | None = None,
    ) -> PushDeliveryRecord:
        owner = _required_id(owner_id, "owner_id")
        entry = _required_uuid(entry_id, "entry_id")
        destination = _required_uuid(destination_id, "destination_id")
        key = _bounded_text(delivery_key, "delivery_key", maximum=512)
        destination_revision = _required_integer(
            expected_destination_revision,
            "expected_destination_revision",
            minimum=1,
        )
        token_version = _required_integer(
            expected_token_version, "expected_token_version", minimum=1
        )
        attempts = _required_integer(
            max_attempts, "max_attempts", minimum=1, maximum=_MAX_ATTEMPTS
        )
        available = _required_integer(available_at, "available_at")
        created = _required_integer(created_at, "created_at")
        if available < created:
            raise RepositoryValidationError("available_at must not precede created_at")
        delivery = (
            str(uuid.uuid4())
            if delivery_id is None
            else _required_uuid(delivery_id, "delivery_id")
        )
        destination_row = transaction.fetch_one(
            f"SELECT {_DESTINATION_FIELDS} FROM notification_device_destination "
            "WHERE destination_id = %s AND owner_id = %s FOR SHARE",
            (destination, owner),
        )
        if destination_row is None:
            raise RepositoryNotFoundError("push destination not found")
        target = _destination_record(destination_row)
        if target.deleted_at is not None:
            raise RepositoryConflictError("deleted push destination cannot receive deliveries")
        if target.revoked_at is not None:
            raise RepositoryConflictError("revoked push destination cannot receive deliveries")
        if target.revision != destination_revision:
            raise RepositoryConflictError("push destination revision is stale")
        if target.token_version != token_version:
            raise RepositoryConflictError("push destination token version is stale")
        entry_row = transaction.fetch_one(
            "SELECT event_kind, content_format, content_ciphertext, content_sha256 "
            "FROM notification_inbox_entry "
            "WHERE entry_id = %s AND owner_id = %s AND deleted_at IS NULL FOR SHARE",
            (entry, owner),
        )
        if entry_row is None:
            raise RepositoryNotFoundError("notification inbox entry not found")
        event_kind = str(_row_value(entry_row, "event_kind"))
        content_format = str(_row_value(entry_row, "content_format"))
        content = _row_value(entry_row, "content_ciphertext")
        content_sha256 = str(_row_value(entry_row, "content_sha256"))
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise RepositoryDataError("persisted notification content is not opaque bytes")
        inserted = _optional_row(
            transaction.execute(
                _INSERT_DELIVERY_SQL,
                (
                    delivery,
                    owner,
                    entry,
                    destination,
                    key,
                    event_kind,
                    content_format,
                    bytes(content),
                    content_sha256,
                    token_version,
                    destination_revision,
                    attempts,
                    available,
                    created,
                    created,
                ),
            ),
            "notification_push_delivery.insert",
        )
        if inserted is not None:
            return _delivery_record(inserted)
        existing = self.get_delivery_by_key(
            transaction,
            owner_id=owner,
            delivery_key=key,
        )
        if existing is None:
            raise RepositoryConflictError("push delivery identity already exists")
        if (
            existing.entry_id,
            existing.destination_id,
            existing.delivery_key,
            existing.event_kind,
            existing.content_format,
            existing.content_ciphertext,
            existing.content_sha256,
            existing.token_version,
            existing.destination_revision,
            existing.max_attempts,
            existing.available_at,
            existing.created_at,
        ) != (
            entry,
            destination,
            key,
            event_kind,
            content_format,
            bytes(content),
            content_sha256,
            token_version,
            destination_revision,
            attempts,
            available,
            created,
        ):
            raise RepositoryConflictError(
                "push delivery replay changed immutable delivery semantics"
            )
        return existing

    def get_delivery(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        delivery_id: str,
    ) -> PushDeliveryRecord | None:
        owner = _required_id(owner_id, "owner_id")
        delivery = _required_uuid(delivery_id, "delivery_id")
        row = transaction.fetch_one(
            f"SELECT {_DELIVERY_FIELDS} FROM notification_push_delivery "
            "WHERE delivery_id = %s AND owner_id = %s",
            (delivery, owner),
        )
        return None if row is None else _delivery_record(row)

    def get_delivery_by_key(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        delivery_key: str,
    ) -> PushDeliveryRecord | None:
        owner = _required_id(owner_id, "owner_id")
        key = _bounded_text(delivery_key, "delivery_key", maximum=512)
        row = transaction.fetch_one(
            f"SELECT {_DELIVERY_FIELDS} FROM notification_push_delivery "
            "WHERE owner_id = %s AND delivery_key = %s",
            (owner, key),
        )
        return None if row is None else _delivery_record(row)

    def list_deliveries(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int,
        status: str | None = None,
        destination_id: str | None = None,
    ) -> tuple[PushDeliveryRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        bounded = _bounded_limit(limit, maximum=200)
        filters = ["owner_id = %s"]
        parameters: tuple[object, ...] = (owner,)
        if status is not None:
            if status not in _DELIVERY_STATUSES:
                raise RepositoryValidationError("status is not a supported delivery status")
            filters.append("status = %s")
            parameters = (*parameters, status)
        if destination_id is not None:
            filters.append("destination_id = %s")
            parameters = (
                *parameters,
                _required_uuid(destination_id, "destination_id"),
            )
        rows = transaction.fetch_all(
            f"SELECT {_DELIVERY_FIELDS} FROM notification_push_delivery "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY created_at DESC, delivery_id DESC LIMIT %s",
            (*parameters, bounded),
        )
        return tuple(_delivery_record(row) for row in rows)

    def claim_deliveries(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        worker_id: str,
        now: int,
        lease_expires_at: int,
        limit: int,
    ) -> tuple[ClaimedPushDelivery, ...]:
        owner = _required_id(owner_id, "owner_id")
        worker = _bounded_text(worker_id, "worker_id", maximum=512)
        current, expiry = _lease_bounds(now, lease_expires_at)
        bounded = _bounded_limit(limit, maximum=100)
        claim_id = str(uuid.uuid4())
        if dialect_of(transaction).is_sqlite:
            rows = self._claim_sqlite(
                transaction, owner, worker, current, expiry, bounded, claim_id
            )
        else:
            rows = transaction.fetch_all(
                _CLAIM_SQL,
                (
                    current,
                    owner,
                    current,
                    owner,
                    current,
                    current,
                    bounded,
                    worker,
                    claim_id,
                    expiry,
                    current,
                    current,
                    _MAX_INTEGER,
                ),
            )
        claimed = tuple(_claimed_record(row) for row in rows)
        if any(item.worker_id != worker for item in claimed):
            raise RepositoryDataError("push claim returned a different worker")
        return tuple(
            sorted(
                claimed,
                key=lambda item: (
                    item.delivery.available_at,
                    item.delivery.delivery_id,
                ),
            )
        )

    @staticmethod
    def _claim_sqlite(transaction, owner, worker, current, expiry, bounded, claim_id):
        transaction.execute(
            "UPDATE notification_push_delivery "
            "SET status='dead_letter',lease_owner=NULL,lease_expires_at=NULL,"
            "last_error_code='lease_expired',version=version+1,updated_at=%s "
            "WHERE owner_id=%s AND status='claimed' AND lease_expires_at<=%s "
            "AND attempt_count>=max_attempts",
            (current, owner, current),
        )
        candidates = transaction.fetch_all(
            "SELECT delivery.delivery_id FROM notification_push_delivery AS delivery "
            "JOIN notification_device_destination AS destination "
            "ON destination.destination_id=delivery.destination_id "
            "AND destination.owner_id=delivery.owner_id "
            "WHERE delivery.owner_id=%s AND delivery.attempt_count<delivery.max_attempts "
            "AND ((delivery.status IN ('pending','retry') AND delivery.available_at<=%s) "
            "OR (delivery.status='claimed' AND delivery.lease_expires_at<=%s)) "
            "AND destination.deleted_at IS NULL AND destination.revoked_at IS NULL "
            "AND destination.token_version=delivery.token_version "
            "AND destination.revision=delivery.destination_revision "
            "ORDER BY delivery.available_at,delivery.delivery_id LIMIT %s",
            (owner, current, current, bounded),
        )
        for candidate in candidates:
            transaction.execute(
                "UPDATE notification_push_delivery SET status='claimed',lease_owner=%s,"
                "claim_id=%s,lease_expires_at=%s,claimed_at=%s,attempt_count=attempt_count+1,"
                "last_error_code=NULL,version=version+1,updated_at=%s "
                "WHERE owner_id=%s AND delivery_id=%s AND version<%s",
                (
                    worker,
                    claim_id,
                    expiry,
                    current,
                    current,
                    owner,
                    candidate["delivery_id"],
                    _MAX_INTEGER,
                ),
            )
        return transaction.fetch_all(
            f"SELECT {_CLAIM_FIELDS} FROM notification_push_delivery AS delivery "
            "JOIN notification_device_destination AS destination "
            "ON destination.destination_id=delivery.destination_id "
            "AND destination.owner_id=delivery.owner_id "
            "WHERE delivery.owner_id=%s AND delivery.claim_id=%s",
            (owner, claim_id),
        )

    def acknowledge_delivery(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        delivery_id: str,
        claim_id: str,
        acknowledged_at: int,
    ) -> PushDeliveryRecord:
        owner = _required_id(owner_id, "owner_id")
        delivery = _required_uuid(delivery_id, "delivery_id")
        claim = _required_uuid(claim_id, "claim_id")
        acknowledged = _required_integer(acknowledged_at, "acknowledged_at")
        changed = _optional_row(
            transaction.execute(
                _ACK_SQL,
                (
                    acknowledged,
                    acknowledged,
                    delivery,
                    owner,
                    claim,
                    acknowledged,
                    acknowledged,
                    _MAX_INTEGER,
                ),
            ),
            "notification_push_delivery.acknowledge",
        )
        if changed is not None:
            return _delivery_record(changed)
        existing = self.get_delivery(
            transaction,
            owner_id=owner,
            delivery_id=delivery,
        )
        if existing is None:
            raise RepositoryNotFoundError("push delivery not found")
        if (
            existing.status == "delivered"
            and existing.claim_id == claim
            and existing.acknowledged_at == acknowledged
        ):
            return existing
        if existing.status == "delivered":
            raise RepositoryConflictError("push delivery was already acknowledged")
        if existing.claim_id != claim:
            raise RepositoryConflictError("push delivery claim was superseded")
        if existing.acknowledged_at is not None:
            raise RepositoryConflictError("push delivery already carries an acknowledgment")
        if existing.claimed_at is None:
            raise RepositoryConflictError("push delivery has no claim to acknowledge")
        if acknowledged < existing.claimed_at or acknowledged < existing.updated_at:
            raise RepositoryConflictError("acknowledgment precedes the claimed delivery state")
        if existing.version >= _MAX_INTEGER:
            raise RepositoryConflictError("push delivery version is exhausted")
        raise RepositoryConflictError("push delivery acknowledgment fence rejected")

    def fail_delivery(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        delivery_id: str,
        claim_id: str,
        failed_at: int,
        error_code: str,
        retry_at: int | None = None,
    ) -> PushDeliveryRecord:
        owner = _required_id(owner_id, "owner_id")
        delivery = _required_uuid(delivery_id, "delivery_id")
        claim = _required_uuid(claim_id, "claim_id")
        failed = _required_integer(failed_at, "failed_at")
        error = _required_error_code(error_code)
        retry = (
            None
            if retry_at is None
            else _required_integer(retry_at, "retry_at", minimum=failed)
        )
        changed = _optional_row(
            transaction.execute(
                _FAIL_SQL,
                (
                    retry,
                    retry,
                    retry,
                    error,
                    failed,
                    delivery,
                    owner,
                    claim,
                    failed,
                    failed,
                    _MAX_INTEGER,
                ),
            ),
            "notification_push_delivery.fail",
        )
        if changed is not None:
            return _delivery_record(changed)
        existing = self.get_delivery(
            transaction,
            owner_id=owner,
            delivery_id=delivery,
        )
        if existing is None:
            raise RepositoryNotFoundError("push delivery not found")
        if (
            existing.claim_id == claim
            and existing.status in ("retry", "dead_letter")
            and existing.last_error_code == error
            and existing.available_at == (existing.available_at if retry is None else retry)
            and existing.updated_at == failed
        ):
            return existing
        if existing.claim_id != claim:
            raise RepositoryConflictError("push delivery claim was superseded")
        if existing.acknowledged_at is not None or existing.status == "delivered":
            raise RepositoryConflictError("acknowledged push delivery cannot fail")
        if existing.status not in ("claimed", "retry"):
            raise RepositoryConflictError("push delivery is not claimable for failure")
        if existing.updated_at >= failed:
            raise RepositoryConflictError("push delivery failure time regressed")
        if existing.version >= _MAX_INTEGER:
            raise RepositoryConflictError("push delivery version is exhausted")
        raise RepositoryConflictError("push delivery failure fence rejected")

    def expire_stale_deliveries(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        expired_at: int,
        limit: int,
    ) -> tuple[PushDeliveryRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        expired = _required_integer(expired_at, "expired_at")
        bounded = _bounded_limit(limit, maximum=100)
        rows = transaction.fetch_all(
            _EXPIRE_STALE_SQL,
            (owner, _MAX_INTEGER, bounded, expired),
        )
        records = tuple(_delivery_record(row) for row in rows)
        return tuple(
            sorted(records, key=lambda record: (record.created_at, record.delivery_id))
        )


__all__ = (
    "ClaimedPushDelivery",
    "PushDeliveryRecord",
    "PushDeliveryRepository",
    "PushDestinationRecord",
)