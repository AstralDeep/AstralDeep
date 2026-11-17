"""Attachment, materialization-lease, blob-metadata, and artifact-version stores, plus
AttachmentMaterializationCoordinator's deadlock-safe two-phase commit around
blob_store.py. Used by orchestrator/attachments/materialization.py and purge.py.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from astralplane.blob_store import (
    BlobStagedWrite,
    BlobStagingReservation,
    BlobStagingSession,
    ExplicitRootStreamingBlobStore,
    StreamingBlobStore,
    _cancel_safe_in_executor,
    _create_blob_publish_authority,
    validate_blob_owner_id,
    validate_blob_storage_key,
)
from astralplane.contracts import PlaneDatabase, QueryExecutor, Transaction
from astralplane.errors import SQLContractError
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _non_negative_int,
    _positive_int,
    _required_id,
    _row_value,
    _single_returned,
    _structured_json,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_LEASE_ID = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._:@/-]{0,126}[A-Za-z0-9])?$")
_PHYSICAL_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,254}$")
_ATTACHMENT_CATEGORIES = frozenset(
    {
        "archive",
        "data",
        "document",
        "image",
        "medical",
        "presentation",
        "spreadsheet",
        "text",
    }
)
_ARTIFACT_REASONS = frozenset({"refine", "restore"})
_PENDING_CONTENT_TYPE: Final = "application/x-astralplane-pending-materialization"
_PENDING_SHA256: Final = "0" * 64
_MAX_MATERIALIZATION_LEASE_SECONDS: Final = 86_400
_MAX_MATERIALIZATION_BYTES: Final = (1 << 63) - 1


class AttachmentMaterializationState(StrEnum):
    READY = "ready"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    attachment_id: str
    owner_id: str
    filename: str
    content_type: str
    category: str
    extension: str
    size_bytes: int
    sha256: str
    storage_locator: str
    created_at: int
    deleted_at: int | None = None


@dataclass(frozen=True, slots=True)
class PendingAttachmentMaterializationRecord:
    attachment_id: str
    owner_id: str
    filename: str
    category: str
    extension: str
    storage_locator: str
    storage_key: str
    max_bytes: int
    created_at: int
    lease_id: str
    lease_version: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AttachmentMaterializationBeginResult:
    state: AttachmentMaterializationState
    pending: PendingAttachmentMaterializationRecord | None = None
    ready: AttachmentRecord | None = None

    def __post_init__(self) -> None:
        if self.state is AttachmentMaterializationState.PENDING:
            if self.pending is None or self.ready is not None:
                raise ValueError("pending begin result must carry only pending metadata")
        elif self.state is AttachmentMaterializationState.READY and (
            self.ready is None or self.pending is not None
        ):
            raise ValueError("ready begin result must carry only ready metadata")


@dataclass(frozen=True, slots=True)
class _AttachmentMaterializationPublishFence:
    owner_id: str
    attachment_id: str
    filename: str
    storage_key: str
    storage_locator: str
    max_bytes: int
    lease_id: str
    lease_version: int


@dataclass(frozen=True, slots=True)
class BlobMetadataRecord:
    object_id: str
    owner_id: str
    object_kind: str
    storage_locator: str
    sha256: str
    size_bytes: int
    created_at: int
    deleted_at: int | None


@dataclass(frozen=True, slots=True)
class MessageAttachmentRecord:
    link_id: str
    conversation_id: str
    message_id: str | None
    attachment_id: str
    owner_id: str
    created_at: int


@dataclass(frozen=True, slots=True)
class ArtifactVersionRecord:
    version_id: int
    conversation_id: str
    owner_id: str
    component_id: str
    version_number: int
    component: Any
    reason: str
    created_at: datetime


def _optional_returned(result: object, operation: str) -> Any:
    if not getattr(result, "returned_records", ()):
        return None
    return _single_returned(result, operation)


def _digest(value: object, field: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RepositoryValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _lease_seconds(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_MATERIALIZATION_LEASE_SECONDS
    ):
        raise RepositoryValidationError("lease_seconds must be an integer in [1, 86400]")
    return value


def _lease_id(value: object) -> str:
    if not isinstance(value, str) or _CANONICAL_LEASE_ID.fullmatch(value) is None:
        raise RepositoryValidationError(
            "lease_id must be a canonical identifier of at most 128 characters"
        )
    return value


def _physical_attachment_id(value: object) -> str:
    if not isinstance(value, str) or _PHYSICAL_ATTACHMENT_ID.fullmatch(value) is None:
        raise RepositoryValidationError("attachment_id must be one safe bounded storage component")
    try:
        normalized = validate_blob_storage_key(value)
    except SQLContractError as exc:
        raise RepositoryValidationError(
            "attachment_id must be one safe bounded storage component"
        ) from exc
    if "/" in normalized:
        raise RepositoryValidationError("attachment_id must be one safe bounded storage component")
    return normalized


def _materialization_storage_key(
    value: object,
    *,
    attachment_id: str,
    filename: str,
) -> str:
    try:
        normalized = validate_blob_storage_key(value)  # type: ignore[arg-type]
    except SQLContractError as exc:
        raise RepositoryValidationError(
            "storage_key must be a safe owner-relative blob key"
        ) from exc
    expected = f"{attachment_id}/{filename}"
    if normalized != expected:
        raise RepositoryValidationError(
            "storage_key must equal the exact attachment_id/filename identity"
        )
    return normalized


def _materialization_physical_identity(
    *,
    owner_id: str,
    attachment_id: str,
    filename: str,
    storage_locator: object,
    storage_key: object,
) -> tuple[str, str]:
    try:
        normalized_filename = validate_blob_storage_key(filename)
        normalized_locator = validate_blob_storage_key(storage_locator)  # type: ignore[arg-type]
    except SQLContractError as exc:
        raise RepositoryValidationError(
            "attachment materialization has an invalid physical storage identity"
        ) from exc
    if normalized_filename != filename or "/" in normalized_filename:
        raise RepositoryValidationError("filename must be one exact safe storage component")
    normalized_key = _materialization_storage_key(
        storage_key,
        attachment_id=attachment_id,
        filename=filename,
    )
    expected_locator = f"{owner_id}/{normalized_key}"
    if normalized_locator != expected_locator:
        raise RepositoryValidationError(
            "storage_locator must equal the exact owner_id/storage_key identity"
        )
    return normalized_locator, normalized_key


def _materialization_max_bytes(value: object) -> int:
    maximum = _positive_int(value, "max_bytes")
    if maximum > _MAX_MATERIALIZATION_BYTES:
        raise RepositoryValidationError("max_bytes exceeds PostgreSQL BIGINT capacity")
    return maximum


def _lock_active_blob_owner(transaction: Transaction, owner_id: str, *, operation: str) -> None:
    transaction.execute(
        """
        INSERT INTO astralplane_blob_owner_state (
            owner_id, state, version, retired_at, updated_at
        ) VALUES (%s, 'active', 0, NULL, clock_timestamp())
        ON CONFLICT DO NOTHING
        """,
        (owner_id,),
    )
    row = transaction.fetch_one(
        """
        SELECT state
        FROM astralplane_blob_owner_state
        WHERE owner_id = %s
        FOR UPDATE
        """,
        (owner_id,),
    )
    if row is None or row.get("state") != "active":
        raise RepositoryConflictError(
            "blob owner is retired and cannot publish attachments",
            metadata={"operation": operation},
        )


def _attachment(row: Mapping[str, Any]) -> AttachmentRecord:
    return AttachmentRecord(
        attachment_id=str(_row_value(row, "attachment_id")),
        owner_id=str(_row_value(row, "user_id")),
        filename=str(_row_value(row, "filename")),
        content_type=str(_row_value(row, "content_type")),
        category=str(_row_value(row, "category")),
        extension=str(_row_value(row, "extension")),
        size_bytes=int(_row_value(row, "size_bytes")),
        sha256=str(_row_value(row, "sha256")),
        storage_locator=str(_row_value(row, "storage_path")),
        created_at=int(_row_value(row, "created_at")),
        deleted_at=(None if row.get("deleted_at") is None else int(row["deleted_at"])),
    )


def _pending_materialization(
    row: Mapping[str, Any],
) -> PendingAttachmentMaterializationRecord:
    state = str(_row_value(row, "materialization_state"))
    expires_at = _row_value(row, "materialization_lease_expires_at")
    if state != AttachmentMaterializationState.PENDING.value:
        raise RepositoryDataError("attachment materialization is not pending")
    if row.get("deleted_at") is not None:
        raise RepositoryDataError("pending attachment materialization is abandoned")
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise RepositoryDataError("pending attachment lease expiry is not timezone-aware")
    try:
        owner_id = validate_blob_owner_id(str(_row_value(row, "user_id")))
        attachment_id = _physical_attachment_id(_row_value(row, "attachment_id"))
        filename = str(_row_value(row, "filename"))
        storage_locator, storage_key = _materialization_physical_identity(
            owner_id=owner_id,
            attachment_id=attachment_id,
            filename=filename,
            storage_locator=_row_value(row, "storage_path"),
            storage_key=_row_value(row, "materialization_storage_key"),
        )
    except (RepositoryValidationError, SQLContractError) as exc:
        raise RepositoryDataError(
            "pending attachment has an invalid physical storage identity"
        ) from exc
    return PendingAttachmentMaterializationRecord(
        attachment_id=attachment_id,
        owner_id=owner_id,
        filename=filename,
        category=str(_row_value(row, "category")),
        extension=str(_row_value(row, "extension")),
        storage_locator=storage_locator,
        storage_key=storage_key,
        max_bytes=int(_row_value(row, "materialization_max_bytes")),
        created_at=int(_row_value(row, "created_at")),
        lease_id=str(_row_value(row, "materialization_lease_id")),
        lease_version=int(_row_value(row, "materialization_lease_version")),
        lease_expires_at=expires_at,
    )


def _blob(row: Mapping[str, Any]) -> BlobMetadataRecord:
    return BlobMetadataRecord(
        object_id=str(_row_value(row, "attachment_id")),
        owner_id=str(_row_value(row, "user_id")),
        object_kind="attachment",
        storage_locator=str(_row_value(row, "storage_path")),
        sha256=str(_row_value(row, "sha256")),
        size_bytes=int(_row_value(row, "size_bytes")),
        created_at=int(_row_value(row, "created_at")),
        deleted_at=(None if row.get("deleted_at") is None else int(row["deleted_at"])),
    )


def _message_attachment(row: Mapping[str, Any]) -> MessageAttachmentRecord:
    return MessageAttachmentRecord(
        link_id=str(_row_value(row, "id")),
        conversation_id=str(_row_value(row, "chat_id")),
        message_id=(None if row.get("message_id") is None else str(row["message_id"])),
        attachment_id=str(_row_value(row, "attachment_id")),
        owner_id=str(_row_value(row, "user_id")),
        created_at=int(_row_value(row, "created_at")),
    )


def _artifact_version(row: Mapping[str, Any]) -> ArtifactVersionRecord:
    created_at = _row_value(row, "created_at")
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise RepositoryDataError(
            "artifact version timestamp is not timezone-aware",
            metadata={"field": "created_at"},
        )
    component = _structured_json(_row_value(row, "component"), "component")
    if not isinstance(component, Mapping):
        raise RepositoryDataError("artifact version component must be a JSON object")
    return ArtifactVersionRecord(
        version_id=int(_row_value(row, "id")),
        conversation_id=str(_row_value(row, "chat_id")),
        owner_id=str(_row_value(row, "user_id")),
        component_id=str(_row_value(row, "component_id")),
        version_number=int(_row_value(row, "version_no")),
        component=component,
        reason=str(_row_value(row, "reason")),
        created_at=created_at,
    )


class MaterializationRepository:
    _FIELDS = """
        attachment_id, user_id, filename, content_type, category, extension,
        size_bytes, sha256, storage_path, created_at, deleted_at
    """
    _LIFECYCLE_FIELDS = """
        attachment_id, user_id, filename, content_type, category, extension,
        size_bytes, sha256, storage_path, created_at, deleted_at,
        materialization_state, materialization_lease_id,
        materialization_lease_version, materialization_lease_expires_at,
        materialization_max_bytes, materialization_storage_key
    """

    def begin_pending_materialization(
        self,
        transaction: Transaction,
        *,
        attachment_id: str,
        owner_id: str,
        filename: str,
        category: str,
        extension: str,
        storage_locator: str,
        storage_key: str,
        max_bytes: int,
        created_at: int,
        lease_id: str,
        lease_seconds: int,
    ) -> AttachmentMaterializationBeginResult:
        attachment_id = _physical_attachment_id(attachment_id)
        owner_id = validate_blob_owner_id(owner_id)
        filename = _bounded_text(filename, "filename", maximum=1024)
        if category not in _ATTACHMENT_CATEGORIES:
            raise RepositoryValidationError("attachment category is unsupported")
        extension = _bounded_text(extension, "extension", maximum=64, allow_empty=True)
        locator, key = _materialization_physical_identity(
            owner_id=owner_id,
            attachment_id=attachment_id,
            filename=filename,
            storage_locator=_bounded_text(
                storage_locator,
                "storage_locator",
                maximum=4096,
            ),
            storage_key=storage_key,
        )
        maximum = _materialization_max_bytes(max_bytes)
        created = _non_negative_int(created_at, "created_at")
        lease = _lease_id(lease_id)
        duration = _lease_seconds(lease_seconds)
        _lock_active_blob_owner(
            transaction,
            owner_id,
            operation="materialization.begin_pending",
        )
        result = transaction.execute(
            f"""
            INSERT INTO user_attachments (
                attachment_id, user_id, filename, content_type, category,
                extension, size_bytes, sha256, storage_path, created_at,
                deleted_at, materialization_state, materialization_lease_id,
                materialization_lease_version, materialization_lease_expires_at,
                materialization_max_bytes, materialization_storage_key
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 0, %s, %s, %s,
                NULL, 'pending', %s, 0,
                clock_timestamp() + (%s * INTERVAL '1 second'), %s, %s
            )
            ON CONFLICT (attachment_id) DO NOTHING
            RETURNING {self._LIFECYCLE_FIELDS}
            """,
            (
                attachment_id,
                owner_id,
                filename,
                _PENDING_CONTENT_TYPE,
                category,
                extension,
                _PENDING_SHA256,
                locator,
                created,
                lease,
                duration,
                maximum,
                key,
            ),
        )
        row = _optional_returned(result, "materialization.begin_pending")
        if row is not None:
            return AttachmentMaterializationBeginResult(
                state=AttachmentMaterializationState.PENDING,
                pending=_pending_materialization(row),
            )
        existing = self._get_lifecycle(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
        )
        if existing is None:
            raise RepositoryConflictError(
                "materialization identity is owned by another namespace",
                metadata={"operation": "materialization.begin_pending"},
            )
        expected = (
            filename,
            category,
            extension,
            locator,
            key,
            maximum,
            created,
            lease,
            None,
        )
        observed = (
            str(_row_value(existing, "filename")),
            str(_row_value(existing, "category")),
            str(_row_value(existing, "extension")),
            str(_row_value(existing, "storage_path")),
            existing.get("materialization_storage_key"),
            existing.get("materialization_max_bytes"),
            int(_row_value(existing, "created_at")),
            existing.get("materialization_lease_id"),
            existing.get("deleted_at"),
        )
        if observed != expected:
            raise RepositoryConflictError(
                "pending materialization identity was reused with different semantics",
                metadata={"operation": "materialization.begin_pending"},
            )
        state = AttachmentMaterializationState(str(_row_value(existing, "materialization_state")))
        if state is AttachmentMaterializationState.PENDING:
            return AttachmentMaterializationBeginResult(
                state=state,
                pending=_pending_materialization(existing),
            )
        return AttachmentMaterializationBeginResult(
            state=state,
            ready=_attachment(existing),
        )

    def renew_pending_materialization(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
        lease_seconds: int,
    ) -> PendingAttachmentMaterializationRecord:
        owner_id = validate_blob_owner_id(owner_id)
        attachment_id = _physical_attachment_id(attachment_id)
        lease_id = _lease_id(lease_id)
        expected = _non_negative_int(expected_lease_version, "expected_lease_version")
        duration = _lease_seconds(lease_seconds)
        _lock_active_blob_owner(
            transaction,
            owner_id,
            operation="materialization.renew_pending",
        )
        result = transaction.execute(
            f"""
            UPDATE user_attachments
            SET materialization_lease_version = materialization_lease_version + 1,
                materialization_lease_expires_at =
                    clock_timestamp() + (%s * INTERVAL '1 second')
            WHERE attachment_id = %s AND user_id = %s
              AND materialization_state = 'pending'
              AND deleted_at IS NULL
              AND materialization_lease_id = %s
              AND materialization_lease_version = %s
              AND materialization_lease_expires_at > clock_timestamp()
            RETURNING {self._LIFECYCLE_FIELDS}
            """,
            (duration, attachment_id, owner_id, lease_id, expected),
        )
        row = _optional_returned(result, "materialization.renew_pending")
        if row is not None:
            return _pending_materialization(row)
        existing = self._get_lifecycle(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
        )
        if (
            existing is not None
            and str(existing.get("materialization_state"))
            == AttachmentMaterializationState.PENDING.value
            and existing.get("deleted_at") is None
            and existing.get("materialization_lease_id") == lease_id
            and existing.get("materialization_lease_version") == expected + 1
        ):
            return _pending_materialization(existing)
        self._raise_lifecycle_conflict(
            existing,
            operation="materialization.renew_pending",
        )

    def _finalize_pending_materialization(
        self,
        transaction: Transaction,
        *,
        fence: _AttachmentMaterializationPublishFence,
        content_type: str,
        size_bytes: int,
        sha256: str,
    ) -> AttachmentRecord:
        if not isinstance(fence, _AttachmentMaterializationPublishFence):
            raise RepositoryValidationError("fence must be locked attachment publication evidence")
        owner_id = validate_blob_owner_id(fence.owner_id)
        attachment_id = _physical_attachment_id(fence.attachment_id)
        lease_id = _lease_id(fence.lease_id)
        expected = _non_negative_int(fence.lease_version, "lease_version")
        locator, storage_key = _materialization_physical_identity(
            owner_id=owner_id,
            attachment_id=attachment_id,
            filename=fence.filename,
            storage_locator=_bounded_text(
                fence.storage_locator,
                "storage_locator",
                maximum=4096,
            ),
            storage_key=fence.storage_key,
        )
        maximum = _materialization_max_bytes(fence.max_bytes)
        _lock_active_blob_owner(
            transaction,
            owner_id,
            operation="materialization.finalize_pending",
        )
        content_type = _bounded_text(content_type, "content_type", maximum=255)
        if content_type == _PENDING_CONTENT_TYPE:
            raise RepositoryValidationError("final content_type is reserved")
        size = _non_negative_int(size_bytes, "size_bytes")
        digest = _digest(sha256)
        if digest == _PENDING_SHA256:
            raise RepositoryValidationError("final sha256 is reserved")
        result = transaction.execute(
            f"""
            UPDATE user_attachments
            SET content_type = %s,
                size_bytes = %s,
                sha256 = %s,
                materialization_state = 'ready',
                materialization_lease_version = materialization_lease_version + 1
            WHERE attachment_id = %s AND user_id = %s
              AND storage_path = %s
              AND materialization_storage_key = %s
              AND materialization_max_bytes = %s
              AND materialization_state = 'pending'
              AND deleted_at IS NULL
              AND materialization_lease_id = %s
              AND materialization_lease_version = %s
              AND materialization_lease_expires_at > clock_timestamp()
              AND %s <= materialization_max_bytes
            RETURNING {self._FIELDS}
            """,
            (
                content_type,
                size,
                digest,
                attachment_id,
                owner_id,
                locator,
                storage_key,
                maximum,
                lease_id,
                expected,
                size,
            ),
        )
        row = _optional_returned(result, "materialization.finalize_pending")
        if row is not None:
            return _attachment(row)
        existing = self._get_lifecycle(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
        )
        if existing is not None:
            replay = (
                str(existing.get("materialization_state")),
                existing.get("deleted_at"),
                existing.get("materialization_lease_id"),
                existing.get("materialization_lease_version"),
                str(existing.get("storage_path")),
                existing.get("materialization_storage_key"),
                existing.get("materialization_max_bytes"),
                str(existing.get("content_type")),
                existing.get("size_bytes"),
                existing.get("sha256"),
            )
            expected_replay = (
                AttachmentMaterializationState.READY.value,
                None,
                lease_id,
                expected + 1,
                locator,
                storage_key,
                maximum,
                content_type,
                size,
                digest,
            )
            if replay == expected_replay:
                return _attachment(existing)
        self._raise_lifecycle_conflict(
            existing,
            operation="materialization.finalize_pending",
        )

    def _lock_pending_materialization_for_publish(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> _AttachmentMaterializationPublishFence:
        owner_id = validate_blob_owner_id(owner_id)
        attachment_id = _physical_attachment_id(attachment_id)
        lease_id = _lease_id(lease_id)
        expected = _non_negative_int(expected_lease_version, "expected_lease_version")
        _lock_active_blob_owner(
            transaction,
            owner_id,
            operation="materialization.lock_pending_for_publish",
        )
        row = transaction.fetch_one(
            f"""
            SELECT {self._LIFECYCLE_FIELDS}
            FROM user_attachments
            WHERE attachment_id = %s AND user_id = %s
              AND materialization_state = 'pending'
              AND deleted_at IS NULL
              AND materialization_lease_id = %s
              AND materialization_lease_version = %s
              AND materialization_lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (attachment_id, owner_id, lease_id, expected),
        )
        if row is None:
            self._raise_lifecycle_conflict(
                self._get_lifecycle(
                    transaction,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                ),
                operation="materialization.lock_pending_for_publish",
            )
        assert row is not None
        pending = _pending_materialization(row)
        return _AttachmentMaterializationPublishFence(
            owner_id=pending.owner_id,
            attachment_id=pending.attachment_id,
            filename=pending.filename,
            storage_key=pending.storage_key,
            storage_locator=pending.storage_locator,
            max_bytes=pending.max_bytes,
            lease_id=pending.lease_id,
            lease_version=pending.lease_version,
        )

    def publish_pending_materialization(
        self,
        transaction: Transaction,
        *,
        blobs: StreamingBlobStore,
        staged: BlobStagedWrite,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
        content_type: str,
    ) -> AttachmentRecord:
        if not isinstance(blobs, ExplicitRootStreamingBlobStore):
            raise RepositoryValidationError(
                "blobs must be the configured Plane streaming blob store"
            )
        if not isinstance(staged, BlobStagedWrite):
            raise RepositoryValidationError("staged must be BlobStagedWrite")
        owner_id = validate_blob_owner_id(owner_id)
        attachment_id = _physical_attachment_id(attachment_id)
        lease_id = _lease_id(lease_id)
        expected_lease_version = _non_negative_int(
            expected_lease_version,
            "expected_lease_version",
        )
        content_type = _bounded_text(content_type, "content_type", maximum=255)
        if content_type == _PENDING_CONTENT_TYPE:
            raise RepositoryValidationError("final content_type is reserved")
        _non_negative_int(staged.evidence.size_bytes, "size_bytes")
        _digest(staged.evidence.sha256)
        fence = self._lock_pending_materialization_for_publish(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
            lease_id=lease_id,
            expected_lease_version=expected_lease_version,
        )
        if staged.evidence.size_bytes > fence.max_bytes:
            raise RepositoryValidationError("staged materialization exceeds its durable maximum")
        authority = _create_blob_publish_authority(
            owner_id=fence.owner_id,
            storage_key=fence.storage_key,
            max_bytes=fence.max_bytes,
            lease_id=fence.lease_id,
        )
        written = blobs._publish_staged_materialization(
            staged,
            authority=authority,
        )
        return self._finalize_pending_materialization(
            transaction,
            fence=fence,
            content_type=content_type,
            size_bytes=written.size_bytes,
            sha256=written.sha256,
        )

    def open_pending_materialization_staging(
        self,
        transaction: Transaction,
        *,
        blobs: StreamingBlobStore,
        reservation: BlobStagingReservation,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> BlobStagingSession:
        if not isinstance(blobs, ExplicitRootStreamingBlobStore):
            raise RepositoryValidationError(
                "blobs must be the configured Plane streaming blob store"
            )
        if not isinstance(reservation, BlobStagingReservation):
            raise RepositoryValidationError(
                "reservation must be acquired from the configured Plane blob store"
            )
        try:
            fence = self._lock_pending_materialization_for_publish(
                transaction,
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                expected_lease_version=expected_lease_version,
            )
            authority = _create_blob_publish_authority(
                owner_id=fence.owner_id,
                storage_key=fence.storage_key,
                max_bytes=fence.max_bytes,
                lease_id=fence.lease_id,
            )
            return blobs._begin_staged_materialization(
                authority=authority,
                reservation=reservation,
            )
        except BaseException:
            reservation.release()
            raise

    def _get_lifecycle(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        attachment_id: str,
    ) -> Mapping[str, Any] | None:
        return query.fetch_one(
            f"""
            SELECT {self._LIFECYCLE_FIELDS}
            FROM user_attachments
            WHERE attachment_id = %s AND user_id = %s
            """,
            (attachment_id, owner_id),
        )

    @staticmethod
    def _raise_lifecycle_conflict(
        existing: Mapping[str, Any] | None,
        *,
        operation: str,
    ) -> None:
        if existing is None:
            raise RepositoryNotFoundError(
                "pending attachment materialization was not found",
                metadata={"operation": operation},
            )
        raise RepositoryConflictError(
            "pending attachment materialization lease changed, expired, or was abandoned",
            metadata={"operation": operation},
        )


class AttachmentRepository:
    _SELECT = """
        SELECT attachment_id, user_id, filename, content_type, category,
               extension, size_bytes, sha256, storage_path, created_at, deleted_at
        FROM user_attachments
    """

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        attachment_id: str,
        include_deleted: bool = False,
        for_update: bool = False,
    ) -> AttachmentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        attachment_id = _required_id(attachment_id, "attachment_id")
        if not isinstance(for_update, bool):
            raise RepositoryValidationError("for_update must be a boolean")
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        lock_clause = " FOR UPDATE" if for_update else ""
        row = query.fetch_one(
            self._SELECT
            + " WHERE attachment_id = %s AND user_id = %s"
            + " AND materialization_state = 'ready'"
            + deleted_clause
            + lock_clause,
            (attachment_id, owner_id),
        )
        return None if row is None else _attachment(row)

    def list_live(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        category: str | None = None,
        limit: int = 50,
        before_created_at: int | None = None,
        before_attachment_id: str | None = None,
    ) -> tuple[AttachmentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        limit = _bounded_limit(limit)
        if (before_created_at is None) != (before_attachment_id is None):
            raise RepositoryValidationError(
                "attachment cursor time and identity must be supplied together"
            )
        parameters: list[object] = [owner_id]
        clauses = [
            "user_id = %s",
            "materialization_state = 'ready'",
            "deleted_at IS NULL",
        ]
        if category is not None:
            if category not in _ATTACHMENT_CATEGORIES:
                raise RepositoryValidationError("attachment category is unsupported")
            clauses.append("category = %s")
            parameters.append(category)
        if before_created_at is not None and before_attachment_id is not None:
            before_created_at = _non_negative_int(before_created_at, "before_created_at")
            before_attachment_id = _required_id(before_attachment_id, "before_attachment_id")
            clauses.append("(created_at, attachment_id) < (%s, %s)")
            parameters.extend((before_created_at, before_attachment_id))
        parameters.append(limit)
        rows = query.fetch_all(
            self._SELECT
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC, attachment_id DESC LIMIT %s",
            tuple(parameters),
        )
        return tuple(_attachment(row) for row in rows)


class BlobMetadataRepository:
    _FIELDS = """
        attachment_id, user_id, storage_path, sha256, size_bytes,
        created_at, deleted_at
    """
    _SELECT = f"SELECT {_FIELDS} FROM user_attachments"

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        object_id: str,
        include_deleted: bool = False,
    ) -> BlobMetadataRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        object_id = _required_id(object_id, "object_id")
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        row = query.fetch_one(
            self._SELECT
            + " WHERE attachment_id = %s AND user_id = %s"
            + " AND materialization_state = 'ready'"
            + deleted_clause,
            (object_id, owner_id),
        )
        return None if row is None else _blob(row)


class MessageAttachmentRepository:
    _FIELDS = "id, chat_id, message_id, attachment_id, user_id, created_at"
    _VISIBLE_FIELDS = """
        link.id AS id,
        link.chat_id AS chat_id,
        link.message_id AS message_id,
        link.attachment_id AS attachment_id,
        link.user_id AS user_id,
        link.created_at AS created_at
    """
    _VISIBLE_FROM = """
        FROM message_attachment AS link
        JOIN user_attachments AS attachment
          ON attachment.attachment_id = link.attachment_id
         AND attachment.user_id = link.user_id
         AND attachment.materialization_state = 'ready'
         AND attachment.deleted_at IS NULL
    """

    def link(
        self,
        transaction: Transaction,
        *,
        link_id: str,
        owner_id: str,
        conversation_id: str,
        attachment_id: str,
        created_at: int,
        message_id: int | str | None = None,
    ) -> MessageAttachmentRecord:
        link_id = _required_id(link_id, "link_id")
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        attachment_id = _required_id(attachment_id, "attachment_id")
        created_at = _non_negative_int(created_at, "created_at")
        stored_message_id = None if message_id is None else str(message_id)
        result = transaction.execute(
            f"""
            INSERT INTO message_attachment (
                id, chat_id, message_id, attachment_id, user_id, created_at
            )
            SELECT %s, %s, %s, %s, %s, %s
            FROM chats AS chat
            JOIN user_attachments AS attachment
              ON attachment.attachment_id = %s
             AND attachment.user_id = chat.user_id
             AND attachment.materialization_state = 'ready'
             AND attachment.deleted_at IS NULL
            WHERE chat.id = %s AND chat.user_id = %s
              AND (
                %s IS NULL OR EXISTS (
                    SELECT 1 FROM messages
                    WHERE id::text = %s AND chat_id = chat.id AND user_id = chat.user_id
                )
              )
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                link_id,
                conversation_id,
                stored_message_id,
                attachment_id,
                owner_id,
                created_at,
                attachment_id,
                conversation_id,
                owner_id,
                stored_message_id,
                stored_message_id,
            ),
        )
        row = _optional_returned(result, "message_attachment.link")
        if row is not None:
            return _message_attachment(row)
        existing = self.get(transaction, owner_id=owner_id, link_id=link_id)
        if existing is None:
            raise RepositoryNotFoundError(
                "owner-scoped attachment link prerequisites are unavailable",
                metadata={"operation": "message_attachment.link"},
            )
        expected = (conversation_id, stored_message_id, attachment_id, created_at)
        observed = (
            existing.conversation_id,
            existing.message_id,
            existing.attachment_id,
            existing.created_at,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "attachment link identity was reused with different semantics",
                metadata={"operation": "message_attachment.link"},
            )
        return existing

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        link_id: str,
    ) -> MessageAttachmentRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        link_id = _required_id(link_id, "link_id")
        row = query.fetch_one(
            f"""
            SELECT {self._VISIBLE_FIELDS}
            {self._VISIBLE_FROM}
            WHERE link.id = %s AND link.user_id = %s
            """,
            (link_id, owner_id),
        )
        return None if row is None else _message_attachment(row)

    def list_for_conversation(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> tuple[MessageAttachmentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        rows = query.fetch_all(
            f"""
            SELECT {self._VISIBLE_FIELDS}
            {self._VISIBLE_FROM}
            WHERE link.chat_id = %s AND link.user_id = %s
            ORDER BY link.created_at, link.id
            """,
            (conversation_id, owner_id),
        )
        return tuple(_message_attachment(row) for row in rows)

    def list_for_message(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        message_id: int | str,
    ) -> tuple[MessageAttachmentRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        stored_message_id = _required_id(str(message_id), "message_id")
        rows = query.fetch_all(
            f"""
            SELECT {self._VISIBLE_FIELDS}
            {self._VISIBLE_FROM}
            WHERE link.message_id = %s AND link.user_id = %s
            ORDER BY link.created_at, link.id
            """,
            (stored_message_id, owner_id),
        )
        return tuple(_message_attachment(row) for row in rows)


class ArtifactVersionRepository:
    _FIELDS = """
        id, chat_id, user_id, component_id, version_no,
        component, reason, created_at
    """

    def archive(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        component_id: str,
        component: Mapping[str, object],
        reason: str = "refine",
        retain: int = 5,
    ) -> ArtifactVersionRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        component_id = _required_id(component_id, "component_id")
        if reason not in _ARTIFACT_REASONS:
            raise RepositoryValidationError("artifact version reason is unsupported")
        retain = _bounded_limit(retain, maximum=100)
        payload = _canonical_json(component, "component")
        authority = transaction.fetch_one(
            "SELECT id FROM chats WHERE id = %s AND user_id = %s FOR UPDATE",
            (conversation_id, owner_id),
        )
        if authority is None:
            raise RepositoryNotFoundError(
                "artifact conversation was not found",
                metadata={"operation": "artifact_version.archive"},
            )
        result = transaction.execute(
            f"""
            INSERT INTO component_version (
                chat_id, user_id, component_id, version_no, component, reason
            )
            SELECT %s, %s, %s, COALESCE(MAX(version_no), 0) + 1, %s::jsonb, %s
            FROM component_version
            WHERE chat_id = %s AND user_id = %s AND component_id = %s
            RETURNING {self._FIELDS}
            """,
            (
                conversation_id,
                owner_id,
                component_id,
                payload,
                reason,
                conversation_id,
                owner_id,
                component_id,
            ),
        )
        record = _artifact_version(_single_returned(result, "artifact_version.archive"))
        if record.version_number > retain:
            transaction.execute(
                """
                DELETE FROM component_version
                WHERE chat_id = %s AND user_id = %s AND component_id = %s
                  AND version_no <= %s
                """,
                (
                    conversation_id,
                    owner_id,
                    component_id,
                    record.version_number - retain,
                ),
            )
        return record

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        component_id: str,
        version_number: int,
    ) -> ArtifactVersionRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        component_id = _required_id(component_id, "component_id")
        version_number = _positive_int(version_number, "version_number")
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM component_version
            WHERE chat_id = %s AND user_id = %s AND component_id = %s
              AND version_no = %s
            """,
            (conversation_id, owner_id, component_id, version_number),
        )
        return None if row is None else _artifact_version(row)

    def list_for_component(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        component_id: str,
        limit: int = 5,
    ) -> tuple[ArtifactVersionRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        component_id = _required_id(component_id, "component_id")
        limit = _bounded_limit(limit, maximum=100)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM component_version
            WHERE chat_id = %s AND user_id = %s AND component_id = %s
            ORDER BY version_no DESC
            LIMIT %s
            """,
            (conversation_id, owner_id, component_id, limit),
        )
        return tuple(_artifact_version(row) for row in rows)

    def delete_for_component(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        component_id: str,
    ) -> int:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        component_id = _required_id(component_id, "component_id")
        result = transaction.execute(
            """
            DELETE FROM component_version
            WHERE chat_id = %s AND user_id = %s AND component_id = %s
            """,
            (conversation_id, owner_id, component_id),
        )
        return max(0, result.rowcount)

    def delete_for_conversation(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> int:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        result = transaction.execute(
            "DELETE FROM component_version WHERE chat_id = %s AND user_id = %s",
            (conversation_id, owner_id),
        )
        if result.rowcount < 0:
            raise RepositoryDataError(
                "artifact conversation deletion returned an invalid row count"
            )
        return result.rowcount


class AttachmentMaterializationCoordinator:
    def __init__(
        self,
        *,
        database: PlaneDatabase,
        repository: MaterializationRepository,
        blobs: StreamingBlobStore,
    ) -> None:
        if not isinstance(database, PlaneDatabase):
            raise RepositoryValidationError("database must own explicit Plane transactions")
        if not isinstance(repository, MaterializationRepository):
            raise RepositoryValidationError("repository must be a MaterializationRepository")
        if not isinstance(blobs, ExplicitRootStreamingBlobStore):
            raise RepositoryValidationError(
                "blobs must be the configured Plane streaming blob store"
            )
        self._database = database
        self._repository = repository
        self._blobs = blobs
        self._control_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="astralplane-materialization-control",
        )
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryValidationError("materialization coordinator is closed")

    async def _run_control(self, function: Any, /, *args: object, **kwargs: object) -> Any:
        self._ensure_open()
        return await _cancel_safe_in_executor(
            self._control_executor,
            function,
            *args,
            **kwargs,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._control_executor.shutdown(wait=True, cancel_futures=False)

    def begin_pending_materialization(
        self,
        *,
        attachment_id: str,
        owner_id: str,
        filename: str,
        category: str,
        extension: str,
        storage_locator: str,
        storage_key: str,
        max_bytes: int,
        created_at: int,
        lease_id: str,
        lease_seconds: int,
    ) -> AttachmentMaterializationBeginResult:
        with self._database.transaction() as transaction:
            return self._repository.begin_pending_materialization(
                transaction,
                attachment_id=attachment_id,
                owner_id=owner_id,
                filename=filename,
                category=category,
                extension=extension,
                storage_locator=storage_locator,
                storage_key=storage_key,
                max_bytes=max_bytes,
                created_at=created_at,
                lease_id=lease_id,
                lease_seconds=lease_seconds,
            )

    async def abegin_pending_materialization(
        self,
        *,
        attachment_id: str,
        owner_id: str,
        filename: str,
        category: str,
        extension: str,
        storage_locator: str,
        storage_key: str,
        max_bytes: int,
        created_at: int,
        lease_id: str,
        lease_seconds: int,
    ) -> AttachmentMaterializationBeginResult:
        return await self._run_control(
            self.begin_pending_materialization,
            attachment_id=attachment_id,
            owner_id=owner_id,
            filename=filename,
            category=category,
            extension=extension,
            storage_locator=storage_locator,
            storage_key=storage_key,
            max_bytes=max_bytes,
            created_at=created_at,
            lease_id=lease_id,
            lease_seconds=lease_seconds,
        )

    def renew_pending_materialization(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
        lease_seconds: int,
    ) -> PendingAttachmentMaterializationRecord:
        with self._database.transaction() as transaction:
            return self._repository.renew_pending_materialization(
                transaction,
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                expected_lease_version=expected_lease_version,
                lease_seconds=lease_seconds,
            )

    async def arenew_pending_materialization(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
        lease_seconds: int,
    ) -> PendingAttachmentMaterializationRecord:
        return await self._run_control(
            self.renew_pending_materialization,
            owner_id=owner_id,
            attachment_id=attachment_id,
            lease_id=lease_id,
            expected_lease_version=expected_lease_version,
            lease_seconds=lease_seconds,
        )

    def open_pending_materialization_staging(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> BlobStagingSession:
        reservation = self._blobs.reserve_materialization_staging(owner_id=owner_id)
        return self._open_pending_materialization_staging_reserved(
            reservation=reservation,
            owner_id=owner_id,
            attachment_id=attachment_id,
            lease_id=lease_id,
            expected_lease_version=expected_lease_version,
        )

    def _open_pending_materialization_staging_reserved(
        self,
        *,
        reservation: BlobStagingReservation,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> BlobStagingSession:
        staging: BlobStagingSession | None = None
        try:
            with self._database.transaction() as transaction:
                staging = self._repository.open_pending_materialization_staging(
                    transaction,
                    blobs=self._blobs,
                    reservation=reservation,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=expected_lease_version,
                )
            return staging
        except BaseException:
            if staging is None:
                reservation.release()
            else:
                staging.abort()
            raise

    async def aopen_pending_materialization_staging(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> BlobStagingSession:
        self._ensure_open()
        reservation = await self._blobs.areserve_materialization_staging(owner_id=owner_id)
        try:
            return await _cancel_safe_in_executor(
                self._control_executor,
                self._open_pending_materialization_staging_reserved,
                reservation=reservation,
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                expected_lease_version=expected_lease_version,
                cleanup_on_cancel=lambda staging: staging.abort(),
            )
        except BaseException:
            reservation.release()
            raise

    def publish_pending_materialization(
        self,
        *,
        staged: BlobStagedWrite,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
        content_type: str,
    ) -> AttachmentRecord:
        with self._database.transaction() as transaction:
            return self._repository.publish_pending_materialization(
                transaction,
                blobs=self._blobs,
                staged=staged,
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                expected_lease_version=expected_lease_version,
                content_type=content_type,
            )

    async def apublish_pending_materialization(
        self,
        *,
        staged: BlobStagedWrite,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
        content_type: str,
    ) -> AttachmentRecord:
        return await self._run_control(
            self.publish_pending_materialization,
            staged=staged,
            owner_id=owner_id,
            attachment_id=attachment_id,
            lease_id=lease_id,
            expected_lease_version=expected_lease_version,
            content_type=content_type,
        )


class ArtifactRepository:
    def __init__(self) -> None:
        self.materializations = MaterializationRepository()
        self.attachments = AttachmentRepository()
        self.blobs = BlobMetadataRepository()
        self.message_attachments = MessageAttachmentRepository()
        self.versions = ArtifactVersionRepository()


__all__ = (
    "ArtifactRepository",
    "ArtifactVersionRecord",
    "ArtifactVersionRepository",
    "AttachmentMaterializationBeginResult",
    "AttachmentMaterializationCoordinator",
    "AttachmentMaterializationState",
    "AttachmentRecord",
    "AttachmentRepository",
    "BlobMetadataRecord",
    "BlobMetadataRepository",
    "MaterializationRepository",
    "MessageAttachmentRecord",
    "MessageAttachmentRepository",
    "PendingAttachmentMaterializationRecord",
)
