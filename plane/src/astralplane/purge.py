"""Durable purge tombstones and blob-destruction mechanics: schedules logical deletion,
then converges physical cleanup via PostgresPurgeStore and DurablePurgeExecutor.
Builds on blob_store.py and repositories/artifacts.py; used by astralplane.api.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from astralplane.blob_store import (
    BlobDeleteResult,
    _BlobPurgeAuthority,
    _create_blob_purge_authority,
    validate_blob_owner_id,
    validate_blob_storage_key,
)
from astralplane.contracts import (
    CommandResultContract,
    PlaneDatabase,
    QueryExecutor,
    Record,
    Transaction,
)
from astralplane.errors import PlaneError, SQLContractError
from astralplane.repositories.artifacts import AttachmentRepository

_SAFE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$")
_SAFE_ERROR_CODE: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_OBJECT_KINDS: Final = frozenset({"attachment", "artifact", "knowledge", "generated_agent"})
_OWNER_NAMESPACE_SELECTOR: Final = "owner-namespace"
_MAX_RECOVERY_BATCH: Final = 1000

_ENQUEUE_SQL: Final = """
INSERT INTO astralplane_purge_tombstone (
    tombstone_id,
    owner_id,
    object_kind,
    object_id,
    storage_key,
    target_scope,
    storage_locator_sha256,
    requested_at,
    status,
    attempt_count,
    version,
    available_at,
    verified_absent_at,
    manual_resolution_evidence_sha256,
    manual_resolved_at,
    last_error_code
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s,
    'pending', 0, 0, %s, NULL, NULL, NULL, NULL
)
ON CONFLICT DO NOTHING
RETURNING tombstone_id
""".strip()

_LOAD_SQL: Final = """
SELECT
    tombstone_id,
    owner_id,
    object_kind,
    object_id,
    storage_key,
    target_scope,
    storage_locator_sha256,
    requested_at,
    status,
    attempt_count,
    version,
    available_at,
    verified_absent_at,
    manual_resolution_evidence_sha256,
    manual_resolved_at,
    last_error_code
FROM astralplane_purge_tombstone
WHERE owner_id = %s
  AND tombstone_id = %s
""".strip()

_LOAD_BY_OBJECT_SQL: Final = """
SELECT
    tombstone_id,
    owner_id,
    object_kind,
    object_id,
    storage_key,
    target_scope,
    storage_locator_sha256,
    requested_at,
    status,
    attempt_count,
    version,
    available_at,
    verified_absent_at,
    manual_resolution_evidence_sha256,
    manual_resolved_at,
    last_error_code
FROM astralplane_purge_tombstone
WHERE owner_id = %s
  AND object_kind = %s
  AND object_id = %s
""".strip()

_LOAD_LEGACY_FOR_ADMINISTRATION_SQL: Final = """
SELECT
    tombstone_id,
    owner_id,
    object_kind,
    object_id,
    storage_key,
    target_scope,
    storage_locator_sha256,
    requested_at,
    status,
    attempt_count,
    version,
    available_at,
    verified_absent_at,
    manual_resolution_evidence_sha256,
    manual_resolved_at,
    last_error_code
FROM astralplane_purge_tombstone
WHERE tombstone_id = %s
  AND target_scope = 'exact_key'
""".strip()

_LIST_INCOMPLETE_SQL: Final = """
SELECT
    tombstone_id,
    owner_id,
    object_kind,
    object_id,
    storage_key,
    target_scope,
    storage_locator_sha256,
    requested_at,
    status,
    attempt_count,
    version,
    available_at,
    verified_absent_at,
    manual_resolution_evidence_sha256,
    manual_resolved_at,
    last_error_code
FROM astralplane_purge_tombstone
WHERE owner_id = %s
  AND status <> 'purged'
ORDER BY requested_at, tombstone_id
""".strip()

_LIST_READY_FOR_ADMINISTRATION_SQL: Final = """
SELECT
    tombstone_id,
    owner_id,
    object_kind,
    object_id,
    storage_key,
    target_scope,
    storage_locator_sha256,
    requested_at,
    status,
    attempt_count,
    version,
    available_at,
    verified_absent_at,
    last_error_code
FROM astralplane_purge_tombstone
WHERE status IN ('pending', 'failed')
  AND available_at <= %s
ORDER BY available_at, requested_at, tombstone_id
LIMIT %s
""".strip()

_HAS_INCOMPLETE_FOR_ADMINISTRATION_SQL: Final = """
SELECT EXISTS (
    SELECT 1
    FROM astralplane_purge_tombstone
    WHERE status <> 'purged'
) OR EXISTS (
    SELECT 1
    FROM user_attachments
    WHERE materialization_state = 'pending'
      AND deleted_at IS NULL
      AND materialization_lease_expires_at <= clock_timestamp()
) AS has_incomplete
""".strip()

_HAS_EXPIRED_PENDING_MATERIALIZATIONS_FOR_ADMINISTRATION_SQL: Final = """
SELECT EXISTS (
    SELECT 1
    FROM user_attachments
    WHERE materialization_state = 'pending'
      AND deleted_at IS NULL
      AND materialization_lease_expires_at <= clock_timestamp()
) AS has_expired_pending
""".strip()

_ENSURE_BLOB_OWNER_SQL: Final = """
INSERT INTO astralplane_blob_owner_state (
    owner_id, state, version, retired_at, updated_at
) VALUES (%s, 'active', 0, NULL, clock_timestamp())
ON CONFLICT DO NOTHING
""".strip()

_RETIRE_BLOB_OWNER_SQL: Final = """
UPDATE astralplane_blob_owner_state
SET state = 'retired',
    version = CASE
        WHEN astralplane_blob_owner_state.state = 'active'
        THEN astralplane_blob_owner_state.version + 1
        ELSE astralplane_blob_owner_state.version
    END,
    retired_at = COALESCE(
        astralplane_blob_owner_state.retired_at,
        %s
    ),
    updated_at = CASE
        WHEN astralplane_blob_owner_state.state = 'active'
        THEN clock_timestamp()
        ELSE astralplane_blob_owner_state.updated_at
    END
WHERE owner_id = %s
RETURNING state, version, retired_at
""".strip()

_ABANDON_PENDING_MATERIALIZATION_SQL: Final = """
UPDATE user_attachments
SET deleted_at = %s,
    materialization_lease_version = materialization_lease_version + 1
WHERE attachment_id = %s
  AND user_id = %s
  AND materialization_state = 'pending'
  AND deleted_at IS NULL
  AND materialization_lease_id = %s
  AND materialization_lease_version = %s
RETURNING
    attachment_id,
    user_id,
    materialization_lease_id,
    materialization_lease_version,
    statement_timestamp() AS purge_requested_at
""".strip()

_CLAIM_EXPIRED_PENDING_MATERIALIZATIONS_SQL: Final = """
WITH candidates AS (
    SELECT attachment_id, user_id
    FROM user_attachments
    WHERE materialization_state = 'pending'
      AND deleted_at IS NULL
      AND materialization_lease_expires_at <= clock_timestamp()
    ORDER BY materialization_lease_expires_at, attachment_id
    FOR UPDATE SKIP LOCKED
    LIMIT %s
)
UPDATE user_attachments AS attachment
SET deleted_at = FLOOR(
        EXTRACT(EPOCH FROM statement_timestamp()) * 1000
    )::BIGINT,
    materialization_lease_version =
        attachment.materialization_lease_version + 1
FROM candidates
WHERE attachment.attachment_id = candidates.attachment_id
  AND attachment.user_id = candidates.user_id
RETURNING
    attachment.attachment_id,
    attachment.user_id,
    attachment.materialization_lease_id,
    attachment.materialization_lease_version,
    statement_timestamp() AS purge_requested_at
""".strip()

_LOAD_MATERIALIZATION_LIFECYCLE_SQL: Final = """
SELECT
    materialization_state,
    materialization_lease_id,
    materialization_lease_version,
    deleted_at
FROM user_attachments
WHERE attachment_id = %s AND user_id = %s
""".strip()

_MARK_PURGED_SQL: Final = """
UPDATE astralplane_purge_tombstone
SET status = 'purged',
    attempt_count = attempt_count + 1,
    version = version + 1,
    verified_absent_at = %s,
    available_at = %s,
    last_error_code = NULL,
    updated_at = clock_timestamp()
WHERE owner_id = %s
  AND tombstone_id = %s
  AND storage_locator_sha256 = %s
  AND version = %s
  AND status IN ('pending', 'failed')
""".strip()

_MARK_FAILED_SQL: Final = """
UPDATE astralplane_purge_tombstone
SET status = 'failed',
    attempt_count = attempt_count + 1,
    version = version + 1,
    available_at = %s,
    verified_absent_at = NULL,
    last_error_code = %s,
    updated_at = clock_timestamp()
WHERE owner_id = %s
  AND tombstone_id = %s
  AND version = %s
  AND status IN ('pending', 'failed')
""".strip()

_MARK_MANUAL_REVIEW_SQL: Final = """
UPDATE astralplane_purge_tombstone
SET status = 'manual_review',
    version = version + 1,
    last_error_code = %s,
    updated_at = clock_timestamp()
WHERE owner_id = %s
  AND tombstone_id = %s
  AND version = %s
  AND status IN ('pending', 'failed')
""".strip()

_RESOLVE_LEGACY_EXACT_SQL: Final = """
UPDATE astralplane_purge_tombstone
SET status = 'purged',
    attempt_count = attempt_count + 1,
    version = version + 1,
    verified_absent_at = %s,
    manual_resolution_evidence_sha256 = %s,
    manual_resolved_at = %s,
    last_error_code = NULL,
    updated_at = %s
WHERE owner_id = %s
  AND tombstone_id = %s
  AND storage_locator_sha256 = %s
  AND version = %s
  AND target_scope = 'exact_key'
  AND status = 'manual_review'
""".strip()


class PurgeStatus(StrEnum):
    PENDING = "pending"
    PURGED = "purged"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class PurgeTargetScope(StrEnum):
    EXACT_KEY = "exact_key"
    ATTACHMENT_PREFIX = "attachment_prefix"
    OWNER_NAMESPACE = "owner_namespace"


@dataclass(frozen=True, slots=True)
class PurgeTombstone:
    tombstone_id: str
    owner_id: str
    object_kind: str
    object_id: str
    storage_key: str = field(repr=False)
    storage_locator_sha256: str
    requested_at: datetime
    target_scope: PurgeTargetScope = PurgeTargetScope.EXACT_KEY
    status: PurgeStatus = PurgeStatus.PENDING
    attempt_count: int = 0
    version: int = 0
    available_at: datetime | None = None
    verified_absent_at: datetime | None = None
    manual_resolution_evidence_sha256: str | None = None
    manual_resolved_at: datetime | None = None
    last_error_code: str | None = None

@dataclass(frozen=True, slots=True)
class PurgeScheduleResult:
    tombstone: PurgeTombstone
    tombstone_created: bool
    metadata_rows_soft_deleted: int


class PurgeAttemptState(StrEnum):
    PURGED = "purged"
    ALREADY_PURGED = "already_purged"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PurgeAttemptResult:
    state: PurgeAttemptState
    tombstone_id: str
    attempt: int
    error_code: str | None = None


@runtime_checkable
class _StreamingPurgeBlobStore(Protocol):
    def _delete_for_purge(
        self,
        authority: _BlobPurgeAuthority,
    ) -> BlobDeleteResult: ...

    def is_absent(self, *, owner_id: str, key: str) -> bool: ...

    def is_prefix_absent(self, *, owner_id: str, prefix: str) -> bool: ...

    def is_owner_absent(self, *, owner_id: str) -> bool: ...


_PURGE_EXECUTOR_AUTHORITY_TOKEN: Final = object()


class _PurgeExecutorAuthority:
    __slots__ = ("_token",)

    def __init__(self, token: object) -> None:
        if token is not _PURGE_EXECUTOR_AUTHORITY_TOKEN:
            raise SQLContractError("purge executor authority is not constructible by callers")
        self._token = token


def _require_purge_executor_authority(authority: _PurgeExecutorAuthority) -> None:
    if (
        not isinstance(authority, _PurgeExecutorAuthority)
        or authority._token is not _PURGE_EXECUTOR_AUTHORITY_TOKEN
    ):
        raise SQLContractError("purge transition requires executor authority")


def _bounded_identifier(value: str, *, name: str) -> str:
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise SQLContractError(f"{name} is not a valid bounded identifier")
    return value


def _bounded_owner_id(value: str) -> str:
    return validate_blob_owner_id(value)


def _bounded_legacy_owner_id(value: str) -> str:
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise SQLContractError("legacy owner_id is not a bounded predecessor identifier")
    return value


def _bounded_legacy_storage_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
    ):
        raise SQLContractError("legacy storage key is not a bounded predecessor locator")
    return value


def _bounded_recovery_limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_RECOVERY_BATCH
    ):
        raise SQLContractError(
            f"limit must be an integer in [1, {_MAX_RECOVERY_BATCH}]"
        )
    return value


def _deleted_at(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SQLContractError("deleted_at must be a non-negative integer")
    return value


def _lowercase_digest(value: str, *, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SQLContractError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _owner_namespace_object_id(owner_id: str) -> str:
    digest = hashlib.sha256(owner_id.encode()).hexdigest()
    return f"owner-namespace:{digest}"


def _scheduled_tombstone_id(*, scope: PurgeTargetScope, owner_id: str, object_id: str) -> str:
    digest = hashlib.sha256(f"{scope.value}\0{owner_id}\0{object_id}".encode()).hexdigest()
    return f"purge-{scope.value}-{digest}"


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SQLContractError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def storage_locator_sha256(*, owner_id: str, key: str) -> str:
    owner = _bounded_owner_id(owner_id)
    normalized = validate_blob_storage_key(key)
    return hashlib.sha256(f"{owner}\0{normalized}".encode()).hexdigest()


def _validate_tombstone(tombstone: PurgeTombstone) -> PurgeTombstone:
    if not isinstance(tombstone, PurgeTombstone):
        raise SQLContractError("tombstone must be a PurgeTombstone")
    tombstone_id = _bounded_identifier(tombstone.tombstone_id, name="tombstone_id")
    object_id = _bounded_identifier(tombstone.object_id, name="object_id")
    if tombstone.object_kind not in _OBJECT_KINDS:
        raise SQLContractError("object_kind is not a supported durable object kind")
    try:
        target_scope = PurgeTargetScope(tombstone.target_scope)
    except ValueError as exc:
        raise SQLContractError("purge target_scope is unsupported") from exc
    if target_scope is PurgeTargetScope.EXACT_KEY:
        owner_id = _bounded_legacy_owner_id(tombstone.owner_id)
        storage_key = _bounded_legacy_storage_key(tombstone.storage_key)
    else:
        owner_id = _bounded_owner_id(tombstone.owner_id)
        storage_key = validate_blob_storage_key(tombstone.storage_key)
    if target_scope is PurgeTargetScope.ATTACHMENT_PREFIX:
        expected_id = _scheduled_tombstone_id(
            scope=target_scope,
            owner_id=owner_id,
            object_id=object_id,
        )
        if (
            tombstone.object_kind != "attachment"
            or storage_key != object_id
            or tombstone_id != expected_id
        ):
            raise SQLContractError(
                "attachment-prefix tombstone does not match its typed deletion identity"
            )
    elif target_scope is PurgeTargetScope.OWNER_NAMESPACE:
        expected_object_id = _owner_namespace_object_id(owner_id)
        expected_id = _scheduled_tombstone_id(
            scope=target_scope,
            owner_id=owner_id,
            object_id=expected_object_id,
        )
        if (
            tombstone.object_kind != "attachment"
            or object_id != expected_object_id
            or storage_key != _OWNER_NAMESPACE_SELECTOR
            or tombstone_id != expected_id
        ):
            raise SQLContractError(
                "owner-namespace tombstone does not match its typed deletion identity"
            )
    elif tombstone.status not in {PurgeStatus.MANUAL_REVIEW, PurgeStatus.PURGED}:
        raise SQLContractError(
            "legacy exact-key tombstones must be in manual review or operator-resolved"
        )
    if target_scope is PurgeTargetScope.EXACT_KEY:
        expected_digest = tombstone.storage_locator_sha256
        if (
            not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        ):
            raise SQLContractError("legacy storage locator digest is invalid")
    else:
        expected_digest = storage_locator_sha256(owner_id=owner_id, key=storage_key)
        if tombstone.storage_locator_sha256 != expected_digest:
            raise SQLContractError("storage locator does not match its SHA-256 digest")
    if (
        isinstance(tombstone.attempt_count, bool)
        or not isinstance(tombstone.attempt_count, int)
        or tombstone.attempt_count < 0
    ):
        raise SQLContractError("attempt_count must be a non-negative integer")
    if (
        isinstance(tombstone.version, bool)
        or not isinstance(tombstone.version, int)
        or tombstone.version < 0
    ):
        raise SQLContractError("version must be a non-negative integer")
    available_at = _utc(
        tombstone.available_at or tombstone.requested_at,
        name="available_at",
    )
    verified_absent_at = (
        None
        if tombstone.verified_absent_at is None
        else _utc(tombstone.verified_absent_at, name="verified_absent_at")
    )
    resolution_digest = tombstone.manual_resolution_evidence_sha256
    if resolution_digest is not None and (
        not isinstance(resolution_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", resolution_digest) is None
    ):
        raise SQLContractError("manual resolution evidence must be a lowercase SHA-256 digest")
    manual_resolved_at = (
        None
        if tombstone.manual_resolved_at is None
        else _utc(tombstone.manual_resolved_at, name="manual_resolved_at")
    )
    if tombstone.status is PurgeStatus.PURGED and verified_absent_at is None:
        raise SQLContractError("purged tombstones require verified_absent_at")
    if tombstone.status is not PurgeStatus.PURGED and verified_absent_at is not None:
        raise SQLContractError("incomplete tombstones cannot claim verified absence")
    if target_scope is PurgeTargetScope.EXACT_KEY:
        if tombstone.status is PurgeStatus.PURGED:
            if resolution_digest is None or manual_resolved_at is None:
                raise SQLContractError(
                    "operator-resolved exact tombstones require durable resolution evidence"
                )
        elif resolution_digest is not None or manual_resolved_at is not None:
            raise SQLContractError("manual-review tombstones cannot claim resolution evidence")
    elif resolution_digest is not None or manual_resolved_at is not None:
        raise SQLContractError("typed purge tombstones cannot carry manual resolution evidence")
    if tombstone.last_error_code is not None and (
        _SAFE_ERROR_CODE.fullmatch(tombstone.last_error_code) is None
    ):
        raise SQLContractError("last_error_code is not a safe bounded code")
    return PurgeTombstone(
        tombstone_id=tombstone_id,
        owner_id=owner_id,
        object_kind=tombstone.object_kind,
        object_id=object_id,
        storage_key=storage_key,
        storage_locator_sha256=expected_digest,
        requested_at=_utc(tombstone.requested_at, name="requested_at"),
        target_scope=target_scope,
        status=PurgeStatus(tombstone.status),
        attempt_count=tombstone.attempt_count,
        version=tombstone.version,
        available_at=available_at,
        verified_absent_at=verified_absent_at,
        manual_resolution_evidence_sha256=resolution_digest,
        manual_resolved_at=manual_resolved_at,
        last_error_code=tombstone.last_error_code,
    )


def _record_text(record: Record, field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str):
        raise PlaneError(
            "purge store returned an invalid field",
            code="purge_record_invalid",
            metadata={"field": field_name},
        )
    return value


def _record_int(record: Record, field_name: str) -> int:
    value = record.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlaneError(
            "purge store returned an invalid field",
            code="purge_record_invalid",
            metadata={"field": field_name},
        )
    return value


def _record_datetime(record: Record, field_name: str, *, optional: bool = False) -> datetime | None:
    value = record.get(field_name)
    if optional and value is None:
        return None
    try:
        return _utc(value, name=field_name)  # type: ignore[arg-type]
    except SQLContractError as exc:
        raise PlaneError(
            "purge store returned an invalid field",
            code="purge_record_invalid",
            metadata={"field": field_name},
        ) from exc


def _from_record(record: Record) -> PurgeTombstone:
    try:
        status = PurgeStatus(_record_text(record, "status"))
        target_scope = PurgeTargetScope(_record_text(record, "target_scope"))
    except ValueError as exc:
        raise PlaneError(
            "purge store returned an invalid status or scope",
            code="purge_record_invalid",
        ) from exc
    return _validate_tombstone(
        PurgeTombstone(
            tombstone_id=_record_text(record, "tombstone_id"),
            owner_id=_record_text(record, "owner_id"),
            object_kind=_record_text(record, "object_kind"),
            object_id=_record_text(record, "object_id"),
            storage_key=_record_text(record, "storage_key"),
            storage_locator_sha256=_record_text(record, "storage_locator_sha256"),
            requested_at=_record_datetime(record, "requested_at"),  # type: ignore[arg-type]
            target_scope=target_scope,
            status=status,
            attempt_count=_record_int(record, "attempt_count"),
            version=_record_int(record, "version"),
            available_at=_record_datetime(record, "available_at", optional=True),
            verified_absent_at=_record_datetime(record, "verified_absent_at", optional=True),
            manual_resolution_evidence_sha256=(
                None
                if record.get("manual_resolution_evidence_sha256") is None
                else _record_text(record, "manual_resolution_evidence_sha256")
            ),
            manual_resolved_at=_record_datetime(
                record, "manual_resolved_at", optional=True
            ),
            last_error_code=(
                None
                if record.get("last_error_code") is None
                else _record_text(record, "last_error_code")
            ),
        )
    )


def _require_update(
    result: CommandResultContract,
    *,
    tombstone_id: str,
    operation: str,
) -> CommandResultContract:
    if result.rowcount != 1:
        raise PlaneError(
            "purge tombstone version fence rejected the transition",
            code="purge_fence_conflict",
            metadata={"tombstone_id": tombstone_id, "operation": operation},
        )
    return result


class PostgresPurgeStore:
    def schedule_attachment_prefix(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        attachment_id: str,
        requested_at: datetime,
        deleted_at: int,
    ) -> PurgeScheduleResult:
        owner = _bounded_owner_id(owner_id)
        attachment = _bounded_identifier(attachment_id, name="attachment_id")
        requested = _utc(requested_at, name="requested_at")
        deleted = _deleted_at(deleted_at)
        existing = self._load_by_object(
            transaction,
            owner_id=owner,
            object_kind="attachment",
            object_id=attachment,
        )
        if existing is None:
            metadata = AttachmentRepository().get(
                transaction,
                owner_id=owner,
                attachment_id=attachment,
                include_deleted=True,
                for_update=True,
            )
            if metadata is None:
                raise PlaneError(
                    "purge object was not found in the owner scope",
                    code="purge_object_not_found",
                    metadata={"object_kind": "attachment"},
                )
            candidate = self._scheduled_tombstone(
                scope=PurgeTargetScope.ATTACHMENT_PREFIX,
                owner_id=owner,
                object_id=attachment,
                storage_key=attachment,
                requested_at=requested,
            )
            tombstone, created = self._enqueue_scheduled(transaction, candidate)
        else:
            self._require_scheduled_target(
                existing,
                scope=PurgeTargetScope.ATTACHMENT_PREFIX,
                storage_key=attachment,
            )
            tombstone, created = existing, False
        changed = transaction.execute(
            """
            UPDATE user_attachments
            SET deleted_at = %s
            WHERE attachment_id = %s AND user_id = %s
              AND materialization_state = 'ready' AND deleted_at IS NULL
            """,
            (deleted, attachment, owner),
        )
        if changed.rowcount not in (0, 1):
            raise PlaneError(
                "attachment purge updated an invalid number of metadata rows",
                code="purge_write_invalid",
            )
        return PurgeScheduleResult(
            tombstone=tombstone,
            tombstone_created=created,
            metadata_rows_soft_deleted=changed.rowcount,
        )

    def abandon_pending_materialization(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
        deleted_at: int,
    ) -> PurgeScheduleResult:
        owner = _bounded_owner_id(owner_id)
        attachment = _bounded_identifier(attachment_id, name="attachment_id")
        lease = _bounded_identifier(lease_id, name="lease_id")
        if (
            isinstance(expected_lease_version, bool)
            or not isinstance(expected_lease_version, int)
            or expected_lease_version < 0
        ):
            raise SQLContractError("expected_lease_version must be non-negative")
        deleted = _deleted_at(deleted_at)
        result = transaction.execute(
            _ABANDON_PENDING_MATERIALIZATION_SQL,
            (deleted, attachment, owner, lease, expected_lease_version),
        )
        if result.rowcount == 1 and len(result.returned_records) == 1:
            record = result.returned_records[0]
            requested_at = _record_datetime(record, "purge_requested_at")
            assert requested_at is not None
            candidate = self._scheduled_tombstone(
                scope=PurgeTargetScope.ATTACHMENT_PREFIX,
                owner_id=owner,
                object_id=attachment,
                storage_key=attachment,
                requested_at=requested_at,
            )
            tombstone, created = self._enqueue_scheduled(transaction, candidate)
            return PurgeScheduleResult(
                tombstone=tombstone,
                tombstone_created=created,
                metadata_rows_soft_deleted=1,
            )
        if result.rowcount not in (0, 1) or result.returned_records:
            raise PlaneError(
                "pending materialization abandonment returned invalid evidence",
                code="purge_write_invalid",
            )
        lifecycle = transaction.fetch_one(
            _LOAD_MATERIALIZATION_LIFECYCLE_SQL,
            (attachment, owner),
        )
        existing = self._load_by_object(
            transaction,
            owner_id=owner,
            object_kind="attachment",
            object_id=attachment,
        )
        if (
            lifecycle is not None
            and lifecycle.get("materialization_state") == "pending"
            and lifecycle.get("materialization_lease_id") == lease
            and lifecycle.get("materialization_lease_version")
            == expected_lease_version + 1
            and lifecycle.get("deleted_at") is not None
            and existing is not None
        ):
            self._require_scheduled_target(
                existing,
                scope=PurgeTargetScope.ATTACHMENT_PREFIX,
                storage_key=attachment,
            )
            return PurgeScheduleResult(
                tombstone=existing,
                tombstone_created=False,
                metadata_rows_soft_deleted=0,
            )
        raise PlaneError(
            "pending materialization lease changed or was finalized",
            code="purge_materialization_fence_conflict",
        )

    def schedule_expired_pending_materializations_for_administration(
        self,
        transaction: Transaction,
        *,
        limit: int = 100,
    ) -> tuple[PurgeScheduleResult, ...]:
        bound = _bounded_recovery_limit(limit)
        result = transaction.execute(
            _CLAIM_EXPIRED_PENDING_MATERIALIZATIONS_SQL,
            (bound,),
        )
        if result.rowcount < 0 or result.rowcount > bound:
            raise PlaneError(
                "expired materialization recovery exceeded its bound",
                code="purge_write_invalid",
            )
        if len(result.returned_records) != result.rowcount:
            raise PlaneError(
                "expired materialization recovery returned incomplete evidence",
                code="purge_write_invalid",
            )
        schedules: list[PurgeScheduleResult] = []
        for record in result.returned_records:
            owner = _bounded_owner_id(_record_text(record, "user_id"))
            attachment = _bounded_identifier(
                _record_text(record, "attachment_id"),
                name="attachment_id",
            )
            requested_at = _record_datetime(record, "purge_requested_at")
            assert requested_at is not None
            candidate = self._scheduled_tombstone(
                scope=PurgeTargetScope.ATTACHMENT_PREFIX,
                owner_id=owner,
                object_id=attachment,
                storage_key=attachment,
                requested_at=requested_at,
            )
            tombstone, created = self._enqueue_scheduled(transaction, candidate)
            schedules.append(
                PurgeScheduleResult(
                    tombstone=tombstone,
                    tombstone_created=created,
                    metadata_rows_soft_deleted=1,
                )
            )
        return tuple(schedules)

    def has_expired_pending_materializations_for_administration(
        self,
        query: QueryExecutor,
    ) -> bool:
        record = query.fetch_one(
            _HAS_EXPIRED_PENDING_MATERIALIZATIONS_FOR_ADMINISTRATION_SQL
        )
        if record is None or not isinstance(record.get("has_expired_pending"), bool):
            raise PlaneError(
                "expired materialization query returned invalid evidence",
                code="purge_record_invalid",
            )
        return bool(record["has_expired_pending"])

    def schedule_owner_namespace(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        requested_at: datetime,
        deleted_at: int,
    ) -> PurgeScheduleResult:
        owner = _bounded_owner_id(owner_id)
        requested = _utc(requested_at, name="requested_at")
        deleted = _deleted_at(deleted_at)
        transaction.execute(_ENSURE_BLOB_OWNER_SQL, (owner,))
        retired = transaction.execute(_RETIRE_BLOB_OWNER_SQL, (requested, owner))
        if retired.rowcount != 1 or len(retired.returned_records) != 1:
            raise PlaneError(
                "blob owner retirement returned invalid evidence",
                code="purge_write_invalid",
            )
        if retired.returned_records[0].get("state") != "retired":
            raise PlaneError(
                "blob owner retirement failed closed",
                code="purge_write_invalid",
            )
        object_id = _owner_namespace_object_id(owner)
        existing = self._load_by_object(
            transaction,
            owner_id=owner,
            object_kind="attachment",
            object_id=object_id,
        )
        if existing is None:
            candidate = self._scheduled_tombstone(
                scope=PurgeTargetScope.OWNER_NAMESPACE,
                owner_id=owner,
                object_id=object_id,
                storage_key=_OWNER_NAMESPACE_SELECTOR,
                requested_at=requested,
            )
            tombstone, created = self._enqueue_scheduled(transaction, candidate)
        else:
            self._require_scheduled_target(
                existing,
                scope=PurgeTargetScope.OWNER_NAMESPACE,
                storage_key=_OWNER_NAMESPACE_SELECTOR,
            )
            tombstone, created = existing, False
        changed = transaction.execute(
            """
            UPDATE user_attachments
            SET deleted_at = %s,
                materialization_lease_version = CASE
                    WHEN materialization_state = 'pending'
                    THEN materialization_lease_version + 1
                    ELSE materialization_lease_version
                END
            WHERE user_id = %s AND deleted_at IS NULL
            """,
            (deleted, owner),
        )
        if changed.rowcount < 0:
            raise PlaneError(
                "owner purge returned an invalid metadata row count",
                code="purge_write_invalid",
            )
        return PurgeScheduleResult(
            tombstone=tombstone,
            tombstone_created=created,
            metadata_rows_soft_deleted=changed.rowcount,
        )

    def load(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        tombstone_id: str,
    ) -> PurgeTombstone | None:
        owner = _bounded_owner_id(owner_id)
        identifier = _bounded_identifier(tombstone_id, name="tombstone_id")
        record = transaction.fetch_one(_LOAD_SQL, (owner, identifier))
        return None if record is None else _from_record(record)

    def load_legacy_exact_for_administration(
        self,
        query: QueryExecutor,
        *,
        tombstone_id: str,
    ) -> PurgeTombstone | None:
        identifier = _bounded_identifier(tombstone_id, name="tombstone_id")
        record = query.fetch_one(
            _LOAD_LEGACY_FOR_ADMINISTRATION_SQL,
            (identifier,),
        )
        return None if record is None else _from_record(record)

    def list_incomplete(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
    ) -> tuple[PurgeTombstone, ...]:
        owner = _bounded_owner_id(owner_id)
        records = transaction.fetch_all(_LIST_INCOMPLETE_SQL, (owner,))
        return tuple(_from_record(record) for record in records)

    def list_ready_for_administration(
        self,
        query: QueryExecutor,
        *,
        observed_at: datetime,
        limit: int = 100,
    ) -> tuple[PurgeTombstone, ...]:
        timestamp = _utc(observed_at, name="observed_at")
        bound = _bounded_recovery_limit(limit)
        records = query.fetch_all(
            _LIST_READY_FOR_ADMINISTRATION_SQL,
            (timestamp, bound),
        )
        if len(records) > bound:
            raise PlaneError(
                "purge recovery query exceeded its requested bound",
                code="purge_record_invalid",
            )
        return tuple(_from_record(record) for record in records)

    def has_incomplete_for_administration(self, query: QueryExecutor) -> bool:
        record = query.fetch_one(_HAS_INCOMPLETE_FOR_ADMINISTRATION_SQL)
        if record is None or not isinstance(record.get("has_incomplete"), bool):
            raise PlaneError(
                "purge incomplete-state query returned invalid evidence",
                code="purge_record_invalid",
            )
        return bool(record["has_incomplete"])

    def _mark_purged_for_executor(
        self,
        transaction: Transaction,
        *,
        authority: _PurgeExecutorAuthority,
        owner_id: str,
        tombstone_id: str,
        expected_storage_locator_sha256: str,
        expected_version: int,
        verified_absent_at: datetime,
    ) -> CommandResultContract:
        _require_purge_executor_authority(authority)
        owner, identifier, version = self._fence(owner_id, tombstone_id, expected_version)
        locator_digest = _lowercase_digest(
            expected_storage_locator_sha256,
            name="expected_storage_locator_sha256",
        )
        timestamp = _utc(verified_absent_at, name="verified_absent_at")
        result = transaction.execute(
            _MARK_PURGED_SQL,
            (timestamp, timestamp, owner, identifier, locator_digest, version),
        )
        return _require_update(result, tombstone_id=identifier, operation="mark-purged")

    def _mark_failed_for_executor(
        self,
        transaction: Transaction,
        *,
        authority: _PurgeExecutorAuthority,
        owner_id: str,
        tombstone_id: str,
        expected_version: int,
        available_at: datetime,
        error_code: str,
    ) -> CommandResultContract:
        _require_purge_executor_authority(authority)
        owner, identifier, version = self._fence(owner_id, tombstone_id, expected_version)
        if not isinstance(error_code, str) or _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise SQLContractError("error_code is not a safe bounded code")
        result = transaction.execute(
            _MARK_FAILED_SQL,
            (_utc(available_at, name="available_at"), error_code, owner, identifier, version),
        )
        return _require_update(result, tombstone_id=identifier, operation="mark-failed")

    def _mark_manual_review_for_executor(
        self,
        transaction: Transaction,
        *,
        authority: _PurgeExecutorAuthority,
        owner_id: str,
        tombstone_id: str,
        expected_version: int,
        error_code: str,
    ) -> CommandResultContract:
        _require_purge_executor_authority(authority)
        owner, identifier, version = self._fence(owner_id, tombstone_id, expected_version)
        if not isinstance(error_code, str) or _SAFE_ERROR_CODE.fullmatch(error_code) is None:
            raise SQLContractError("error_code is not a safe bounded code")
        result = transaction.execute(
            _MARK_MANUAL_REVIEW_SQL,
            (error_code, owner, identifier, version),
        )
        return _require_update(result, tombstone_id=identifier, operation="manual-review")

    def _mark_legacy_exact_resolved_for_administration(
        self,
        transaction: Transaction,
        *,
        authority: _PurgeExecutorAuthority,
        owner_id: str,
        tombstone_id: str,
        expected_storage_locator_sha256: str,
        expected_version: int,
        verified_absent_at: datetime,
        resolution_evidence_sha256: str,
    ) -> CommandResultContract:
        _require_purge_executor_authority(authority)
        owner = _bounded_legacy_owner_id(owner_id)
        identifier = _bounded_identifier(tombstone_id, name="tombstone_id")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise SQLContractError("expected_version must be a non-negative integer")
        expected_digest = _lowercase_digest(
            expected_storage_locator_sha256,
            name="expected_storage_locator_sha256",
        )
        timestamp = _utc(verified_absent_at, name="verified_absent_at")
        evidence_digest = _lowercase_digest(
            resolution_evidence_sha256,
            name="resolution_evidence_sha256",
        )
        result = transaction.execute(
            _RESOLVE_LEGACY_EXACT_SQL,
            (
                timestamp,
                evidence_digest,
                timestamp,
                timestamp,
                owner,
                identifier,
                expected_digest,
                expected_version,
            ),
        )
        return _require_update(
            result,
            tombstone_id=identifier,
            operation="resolve-legacy-exact",
        )

    @staticmethod
    def _fence(owner_id: str, tombstone_id: str, expected_version: int) -> tuple[str, str, int]:
        owner = _bounded_owner_id(owner_id)
        identifier = _bounded_identifier(tombstone_id, name="tombstone_id")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise SQLContractError("expected_version must be a non-negative integer")
        return owner, identifier, expected_version

    def _load_by_object(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        object_kind: str,
        object_id: str,
    ) -> PurgeTombstone | None:
        record = transaction.fetch_one(
            _LOAD_BY_OBJECT_SQL,
            (owner_id, object_kind, object_id),
        )
        return None if record is None else _from_record(record)

    def _enqueue_scheduled(
        self,
        transaction: Transaction,
        tombstone: PurgeTombstone,
    ) -> tuple[PurgeTombstone, bool]:
        exact = _validate_tombstone(tombstone)
        result = transaction.execute(
            _ENQUEUE_SQL,
            (
                exact.tombstone_id,
                exact.owner_id,
                exact.object_kind,
                exact.object_id,
                exact.storage_key,
                exact.target_scope.value,
                exact.storage_locator_sha256,
                exact.requested_at,
                exact.available_at,
            ),
        )
        if result.rowcount == 1:
            return exact, True
        if result.rowcount != 0:
            raise PlaneError(
                "purge enqueue returned an invalid row count", code="purge_write_invalid"
            )
        existing = self._load_by_object(
            transaction,
            owner_id=exact.owner_id,
            object_kind=exact.object_kind,
            object_id=exact.object_id,
        )
        if existing is None:
            raise PlaneError(
                "purge schedule identity was reused by different work",
                code="purge_idempotency_conflict",
                metadata={"tombstone_id": exact.tombstone_id},
            )
        self._require_scheduled_target(
            existing,
            scope=exact.target_scope,
            storage_key=exact.storage_key,
        )
        return existing, False

    @staticmethod
    def _scheduled_tombstone(
        *,
        scope: PurgeTargetScope,
        owner_id: str,
        object_id: str,
        storage_key: str,
        requested_at: datetime,
    ) -> PurgeTombstone:
        tombstone_id = _scheduled_tombstone_id(
            scope=scope,
            owner_id=owner_id,
            object_id=object_id,
        )
        return PurgeTombstone(
            tombstone_id=tombstone_id,
            owner_id=owner_id,
            object_kind="attachment",
            object_id=object_id,
            storage_key=storage_key,
            storage_locator_sha256=storage_locator_sha256(
                owner_id=owner_id,
                key=storage_key,
            ),
            requested_at=requested_at,
            target_scope=scope,
            available_at=requested_at,
        )

    @staticmethod
    def _require_scheduled_target(
        tombstone: PurgeTombstone,
        *,
        scope: PurgeTargetScope,
        storage_key: str,
    ) -> None:
        if tombstone.target_scope is not scope or tombstone.storage_key != storage_key:
            raise PlaneError(
                "purge owner object already represents different work",
                code="purge_idempotency_conflict",
                metadata={"tombstone_id": tombstone.tombstone_id},
            )


class DurablePurgeExecutor:
    def __init__(
        self,
        *,
        database: PlaneDatabase,
        store: PostgresPurgeStore,
        blobs: _StreamingPurgeBlobStore,
    ) -> None:
        if not isinstance(blobs, _StreamingPurgeBlobStore):
            raise SQLContractError(
                "blobs must support streaming-store purge and absence verification"
            )
        self._database = database
        self._store = store
        self._blobs = blobs
        self._authority = _PurgeExecutorAuthority(_PURGE_EXECUTOR_AUTHORITY_TOKEN)

    def discover_ready_for_administration(
        self,
        *,
        observed_at: datetime,
        limit: int = 100,
    ) -> tuple[PurgeTombstone, ...]:
        timestamp = _utc(observed_at, name="observed_at")
        bound = _bounded_recovery_limit(limit)
        with self._database.transaction() as transaction:
            return self._store.list_ready_for_administration(
                transaction,
                observed_at=timestamp,
                limit=bound,
            )

    def has_incomplete_for_administration(self) -> bool:
        with self._database.transaction() as transaction:
            return self._store.has_incomplete_for_administration(transaction)

    def reconcile_ready_for_administration(
        self,
        *,
        observed_at: datetime,
        retry_at: datetime,
        limit: int = 100,
    ) -> tuple[PurgeAttemptResult, ...]:
        timestamp = _utc(observed_at, name="observed_at")
        next_retry = _utc(retry_at, name="retry_at")
        if next_retry <= timestamp:
            raise SQLContractError("retry_at must be later than observed_at")
        ready = self.discover_ready_for_administration(
            observed_at=timestamp,
            limit=limit,
        )
        return tuple(
            self.execute(
                owner_id=tombstone.owner_id,
                tombstone_id=tombstone.tombstone_id,
                now=timestamp,
                retry_at=next_retry,
            )
            for tombstone in ready
        )

    def execute(
        self,
        *,
        owner_id: str,
        tombstone_id: str,
        now: datetime,
        retry_at: datetime,
    ) -> PurgeAttemptResult:
        timestamp = _utc(now, name="now")
        next_retry = _utc(retry_at, name="retry_at")
        if next_retry <= timestamp:
            raise SQLContractError("retry_at must be later than now")
        tombstone = self._load(owner_id=owner_id, tombstone_id=tombstone_id)
        if tombstone is None:
            raise PlaneError(
                "purge tombstone was not found in the owner scope",
                code="purge_not_found",
                metadata={"tombstone_id": tombstone_id},
            )
        if tombstone.status is PurgeStatus.MANUAL_REVIEW:
            raise PlaneError(
                "purge tombstone requires manual review",
                code="purge_manual_review",
                metadata={"tombstone_id": tombstone_id},
            )
        if tombstone.status is PurgeStatus.PURGED:
            if tombstone.target_scope is PurgeTargetScope.EXACT_KEY:
                return PurgeAttemptResult(
                    state=PurgeAttemptState.ALREADY_PURGED,
                    tombstone_id=tombstone_id,
                    attempt=tombstone.attempt_count,
                )
            if not self._is_absent(tombstone):
                raise PlaneError(
                    "purged tombstone no longer matches physical storage",
                    code="purge_integrity_failure",
                    metadata={"tombstone_id": tombstone_id},
                )
            return PurgeAttemptResult(
                state=PurgeAttemptState.ALREADY_PURGED,
                tombstone_id=tombstone_id,
                attempt=tombstone.attempt_count,
            )
        if tombstone.available_at is not None and timestamp < tombstone.available_at:
            raise PlaneError(
                "purge tombstone is not yet available for retry",
                code="purge_retry_not_ready",
                metadata={"tombstone_id": tombstone_id},
            )

        if tombstone.target_scope is PurgeTargetScope.EXACT_KEY:
            try:
                with self._database.transaction() as transaction:
                    self._store._mark_manual_review_for_executor(
                        transaction,
                        authority=self._authority,
                        owner_id=owner_id,
                        tombstone_id=tombstone_id,
                        expected_version=tombstone.version,
                        error_code="publication_fence_required",
                    )
            except PlaneError as conflict:
                if conflict.code != "purge_fence_conflict":
                    raise
                return self._reconcile_concurrent_transition(
                    owner_id=owner_id,
                    tombstone_id=tombstone_id,
                )
            return PurgeAttemptResult(
                state=PurgeAttemptState.FAILED,
                tombstone_id=tombstone_id,
                attempt=tombstone.attempt_count,
                error_code="publication_fence_required",
            )

        try:
            self._delete_and_verify(tombstone)
        except Exception:
            try:
                with self._database.transaction() as transaction:
                    self._store._mark_failed_for_executor(
                        transaction,
                        authority=self._authority,
                        owner_id=owner_id,
                        tombstone_id=tombstone_id,
                        expected_version=tombstone.version,
                        available_at=next_retry,
                        error_code="blob_delete_failed",
                    )
            except PlaneError as conflict:
                if conflict.code != "purge_fence_conflict":
                    raise
                return self._reconcile_concurrent_transition(
                    owner_id=owner_id,
                    tombstone_id=tombstone_id,
                )
            return PurgeAttemptResult(
                state=PurgeAttemptState.FAILED,
                tombstone_id=tombstone_id,
                attempt=tombstone.attempt_count + 1,
                error_code="blob_delete_failed",
            )

        try:
            with self._database.transaction() as transaction:
                self._store._mark_purged_for_executor(
                    transaction,
                    authority=self._authority,
                    owner_id=owner_id,
                    tombstone_id=tombstone_id,
                    expected_storage_locator_sha256=(
                        tombstone.storage_locator_sha256
                    ),
                    expected_version=tombstone.version,
                    verified_absent_at=timestamp,
                )
        except PlaneError as conflict:
            if conflict.code != "purge_fence_conflict":
                raise
            return self._reconcile_concurrent_transition(
                owner_id=owner_id,
                tombstone_id=tombstone_id,
            )
        return PurgeAttemptResult(
            state=PurgeAttemptState.PURGED,
            tombstone_id=tombstone_id,
            attempt=tombstone.attempt_count + 1,
        )

    def resolve_legacy_exact_for_administration(
        self,
        *,
        tombstone_id: str,
        expected_owner_id: str,
        expected_storage_locator_sha256: str,
        observed_at: datetime,
        resolution_evidence_sha256: str,
    ) -> PurgeAttemptResult:
        timestamp = _utc(observed_at, name="observed_at")
        expected_owner = _bounded_legacy_owner_id(expected_owner_id)
        expected_locator_digest = _lowercase_digest(
            expected_storage_locator_sha256,
            name="expected_storage_locator_sha256",
        )
        evidence_digest = _lowercase_digest(
            resolution_evidence_sha256,
            name="resolution_evidence_sha256",
        )
        tombstone = self._load_legacy_for_administration(tombstone_id=tombstone_id)
        if tombstone is None:
            raise PlaneError(
                "legacy purge tombstone was not found",
                code="purge_not_found",
                metadata={"tombstone_id": tombstone_id},
            )
        if (
            tombstone.owner_id != expected_owner
            or tombstone.storage_locator_sha256 != expected_locator_digest
        ):
            raise PlaneError(
                "legacy purge resolution identity changed after operator inspection",
                code="purge_resolution_identity_conflict",
                metadata={"tombstone_id": tombstone_id},
            )
        if tombstone.status is PurgeStatus.PURGED:
            if (
                tombstone.manual_resolution_evidence_sha256
                != evidence_digest
            ):
                raise PlaneError(
                    "legacy purge resolution replay changed its evidence digest",
                    code="purge_resolution_evidence_conflict",
                    metadata={"tombstone_id": tombstone_id},
                )
            return PurgeAttemptResult(
                state=PurgeAttemptState.ALREADY_PURGED,
                tombstone_id=tombstone_id,
                attempt=tombstone.attempt_count,
            )
        if tombstone.status is not PurgeStatus.MANUAL_REVIEW:
            raise PlaneError(
                "legacy exact-key tombstone is not awaiting operator resolution",
                code="purge_resolution_state_invalid",
                metadata={"tombstone_id": tombstone_id},
            )
        try:
            with self._database.transaction() as transaction:
                self._store._mark_legacy_exact_resolved_for_administration(
                    transaction,
                    authority=self._authority,
                    owner_id=expected_owner,
                    tombstone_id=tombstone_id,
                    expected_storage_locator_sha256=expected_locator_digest,
                    expected_version=tombstone.version,
                    verified_absent_at=timestamp,
                    resolution_evidence_sha256=evidence_digest,
                )
        except PlaneError as conflict:
            if conflict.code != "purge_fence_conflict":
                raise
            current = self._load_legacy_for_administration(tombstone_id=tombstone_id)
            if current is None:
                raise PlaneError(
                    "purge tombstone disappeared during operator resolution",
                    code="purge_integrity_failure",
                    metadata={"tombstone_id": tombstone_id},
                ) from conflict
            if (
                current.owner_id != expected_owner
                or current.storage_locator_sha256 != expected_locator_digest
            ):
                raise PlaneError(
                    "concurrent legacy purge resolution changed the inspected identity",
                    code="purge_resolution_identity_conflict",
                    metadata={"tombstone_id": tombstone_id},
                ) from conflict
            if current.status is PurgeStatus.PURGED:
                if (
                    current.manual_resolution_evidence_sha256
                    != evidence_digest
                ):
                    raise PlaneError(
                        "concurrent legacy purge resolution used different evidence",
                        code="purge_resolution_evidence_conflict",
                        metadata={"tombstone_id": tombstone_id},
                    ) from conflict
                return PurgeAttemptResult(
                    state=PurgeAttemptState.ALREADY_PURGED,
                    tombstone_id=tombstone_id,
                    attempt=current.attempt_count,
                )
            raise conflict
        return PurgeAttemptResult(
            state=PurgeAttemptState.PURGED,
            tombstone_id=tombstone_id,
            attempt=tombstone.attempt_count + 1,
        )

    def _load(self, *, owner_id: str, tombstone_id: str) -> PurgeTombstone | None:
        with self._database.transaction() as transaction:
            return self._store.load(
                transaction,
                owner_id=owner_id,
                tombstone_id=tombstone_id,
            )

    def _load_legacy_for_administration(
        self,
        *,
        tombstone_id: str,
    ) -> PurgeTombstone | None:
        with self._database.transaction() as transaction:
            return self._store.load_legacy_exact_for_administration(
                transaction,
                tombstone_id=tombstone_id,
            )

    def _delete_and_verify(self, tombstone: PurgeTombstone) -> None:
        scope = tombstone.target_scope
        if scope not in {
            PurgeTargetScope.OWNER_NAMESPACE,
            PurgeTargetScope.ATTACHMENT_PREFIX,
        }:
            raise PlaneError(
                "legacy exact-key deletion requires external operator attestation",
                code="purge_manual_review",
                metadata={"tombstone_id": tombstone.tombstone_id},
            )
        authority = _create_blob_purge_authority(
            owner_id=tombstone.owner_id,
            target_scope=scope.value,
            storage_key=tombstone.storage_key,
        )
        result = self._blobs._delete_for_purge(authority)
        if result is not None and not result.absent_verified:
            raise PlaneError(
                "blob deletion did not prove absence", code="blob_delete_incomplete"
            )
        if not self._is_absent(tombstone):
            raise PlaneError(
                "blob remained present after deletion", code="blob_delete_incomplete"
            )

    def _is_absent(self, tombstone: PurgeTombstone) -> bool:
        scope = tombstone.target_scope
        if scope is PurgeTargetScope.OWNER_NAMESPACE:
            return self._blobs.is_owner_absent(owner_id=tombstone.owner_id)
        if scope is PurgeTargetScope.ATTACHMENT_PREFIX:
            return self._blobs.is_prefix_absent(
                owner_id=tombstone.owner_id,
                prefix=tombstone.storage_key,
            )
        raise PlaneError(
            "legacy exact-key absence is represented only by operator evidence",
            code="purge_manual_review",
            metadata={"tombstone_id": tombstone.tombstone_id},
        )

    def _reconcile_concurrent_transition(
        self,
        *,
        owner_id: str,
        tombstone_id: str,
    ) -> PurgeAttemptResult:
        current = self._load(owner_id=owner_id, tombstone_id=tombstone_id)
        if current is None:
            raise PlaneError(
                "purge tombstone disappeared during reconciliation",
                code="purge_integrity_failure",
                metadata={"tombstone_id": tombstone_id},
            )
        if (
            current.status is PurgeStatus.PURGED
            and current.target_scope is PurgeTargetScope.EXACT_KEY
        ):
            return PurgeAttemptResult(
                state=PurgeAttemptState.ALREADY_PURGED,
                tombstone_id=tombstone_id,
                attempt=current.attempt_count,
            )
        if current.status is PurgeStatus.PURGED and self._is_absent(current):
            return PurgeAttemptResult(
                state=PurgeAttemptState.ALREADY_PURGED,
                tombstone_id=tombstone_id,
                attempt=current.attempt_count,
            )
        if current.status is PurgeStatus.FAILED:
            return PurgeAttemptResult(
                state=PurgeAttemptState.FAILED,
                tombstone_id=tombstone_id,
                attempt=current.attempt_count,
                error_code=current.last_error_code,
            )
        if current.status is PurgeStatus.MANUAL_REVIEW:
            return PurgeAttemptResult(
                state=PurgeAttemptState.FAILED,
                tombstone_id=tombstone_id,
                attempt=current.attempt_count,
                error_code=current.last_error_code,
            )
        raise PlaneError(
            "purge concurrent transition did not converge",
            code="purge_fence_conflict",
            metadata={"tombstone_id": tombstone_id},
        )


__all__ = (
    "DurablePurgeExecutor",
    "PostgresPurgeStore",
    "PurgeAttemptResult",
    "PurgeAttemptState",
    "PurgeScheduleResult",
    "PurgeStatus",
    "PurgeTargetScope",
    "PurgeTombstone",
    "storage_locator_sha256",
)
