"""Tests for astralplane.repositories.artifacts: pending/staged blob materialization,
lease renewal, owner-scoped attachment reads, and message-attachment linking, backed
by a fake transaction and database.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from astralplane.blob_store import ExplicitRootStreamingBlobStore
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.artifacts import (
    ArtifactRepository,
    ArtifactVersionRepository,
    AttachmentMaterializationBeginResult,
    AttachmentMaterializationCoordinator,
    AttachmentMaterializationState,
    AttachmentRecord,
    AttachmentRepository,
    BlobMetadataRepository,
    MaterializationRepository,
    MessageAttachmentRepository,
    _AttachmentMaterializationPublishFence,
    _digest,
    _pending_materialization,
)

NOW = datetime(2026, 8, 13, 23, 0, tzinfo=UTC)
DIGEST_A = "a" * 64


@dataclass(frozen=True)
class Result:
    rowcount: int = 1
    status_message: str | None = None
    returned_records: tuple[dict[str, Any], ...] = ()


class FakeTransaction:
    def __init__(self) -> None:
        self.execute_results: deque[Result] = deque()
        self.fetch_one_results: deque[dict[str, Any] | None] = deque()
        self.fetch_all_results: deque[tuple[dict[str, Any], ...]] = deque()
        self.calls: list[tuple[str, str, object]] = []

    def execute(self, statement: str, parameters: object = ()) -> Result:
        self.calls.append(("execute", statement, parameters))
        return self.execute_results.popleft() if self.execute_results else Result()

    def fetch_one(self, statement: str, parameters: object = ()) -> dict[str, Any] | None:
        self.calls.append(("fetch_one", statement, parameters))
        return self.fetch_one_results.popleft() if self.fetch_one_results else None

    def fetch_all(self, statement: str, parameters: object = ()) -> tuple[dict[str, Any], ...]:
        self.calls.append(("fetch_all", statement, parameters))
        return self.fetch_all_results.popleft() if self.fetch_all_results else ()


class FakeDatabase:
    def __init__(
        self,
        transaction: FakeTransaction,
        *,
        fail_commit: bool = False,
        commit_entered: threading.Event | None = None,
        release_commit: threading.Event | None = None,
    ) -> None:
        self._transaction = transaction
        self._fail_commit = fail_commit
        self._commit_entered = commit_entered
        self._release_commit = release_commit
        self.transaction_count = 0

    @contextmanager
    def transaction(self, **_: object):
        self.transaction_count += 1
        yield self._transaction
        if self._commit_entered is not None:
            self._commit_entered.set()
        if self._release_commit is not None:
            assert self._release_commit.wait(timeout=5)
        if self._fail_commit:
            raise RuntimeError("simulated commit failure")


def returned(row: dict[str, Any], *, rowcount: int = 1) -> Result:
    return Result(rowcount=rowcount, returned_records=(row,))


def attachment_record(**changes: Any) -> AttachmentRecord:
    values: dict[str, Any] = {
        "attachment_id": "attachment-1",
        "owner_id": "owner-1",
        "filename": "report.txt",
        "content_type": "text/plain",
        "category": "text",
        "extension": "txt",
        "size_bytes": 12,
        "sha256": DIGEST_A,
        "storage_locator": "owner-1/attachment-1/report.txt",
        "created_at": 10,
        "deleted_at": None,
    }
    values.update(changes)
    return AttachmentRecord(**values)


def attachment_row(**changes: Any) -> dict[str, Any]:
    record = attachment_record()
    row = {
        "attachment_id": record.attachment_id,
        "user_id": record.owner_id,
        "filename": record.filename,
        "content_type": record.content_type,
        "category": record.category,
        "extension": record.extension,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "storage_path": record.storage_locator,
        "created_at": record.created_at,
        "deleted_at": record.deleted_at,
    }
    row.update(changes)
    return row


def pending_row(**changes: Any) -> dict[str, Any]:
    row = attachment_row(
        content_type="application/x-astralplane-pending-materialization",
        size_bytes=0,
        sha256="0" * 64,
        storage_path="owner-1/attachment-1/report.txt",
    )
    row.update(
        materialization_state="pending",
        materialization_lease_id="lease-1",
        materialization_lease_version=0,
        materialization_lease_expires_at=NOW + timedelta(minutes=5),
        materialization_max_bytes=1024,
        materialization_storage_key="attachment-1/report.txt",
    )
    row.update(changes)
    return row


def blob_row(**changes: Any) -> dict[str, Any]:
    row = {
        "attachment_id": "attachment-1",
        "user_id": "owner-1",
        "storage_path": "owner-1/attachment-1.txt",
        "sha256": DIGEST_A,
        "size_bytes": 12,
        "created_at": 10,
        "deleted_at": None,
    }
    row.update(changes)
    return row


def link_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": "link-1",
        "chat_id": "chat-1",
        "message_id": "7",
        "attachment_id": "attachment-1",
        "user_id": "owner-1",
        "created_at": 20,
    }
    row.update(changes)
    return row


def version_row(**changes: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "component_id": "component-1",
        "version_no": 1,
        "component": {"type": "Card", "children": ["a"]},
        "reason": "refine",
        "created_at": NOW,
    }
    row.update(changes)
    return row


def test_materialization_result_and_pending_record_invariants_fail_closed() -> None:
    with pytest.raises(ValueError, match="pending begin result"):
        AttachmentMaterializationBeginResult(state=AttachmentMaterializationState.PENDING)
    with pytest.raises(ValueError, match="ready begin result"):
        AttachmentMaterializationBeginResult(state=AttachmentMaterializationState.READY)
    with pytest.raises(RepositoryValidationError, match="lowercase SHA-256"):
        _digest("not-a-digest")

    with pytest.raises(RepositoryDataError, match="is not pending"):
        _pending_materialization(pending_row(materialization_state="ready"))
    with pytest.raises(RepositoryDataError, match="is abandoned"):
        _pending_materialization(pending_row(deleted_at=10))
    with pytest.raises(RepositoryDataError, match="timezone-aware"):
        _pending_materialization(
            pending_row(materialization_lease_expires_at=datetime(2026, 8, 13))
        )


def test_pending_materialization_identity_collision_without_a_row_fails_closed() -> None:
    transaction = FakeTransaction()
    transaction.execute_results.extend((Result(), Result(rowcount=0)))
    transaction.fetch_one_results.extend(({"state": "active"}, None))
    with pytest.raises(RepositoryConflictError, match="another namespace"):
        MaterializationRepository().begin_pending_materialization(
            transaction,
            attachment_id="attachment-1",
            owner_id="owner-1",
            filename="report.txt",
            category="text",
            extension="txt",
            storage_locator="owner-1/attachment-1/report.txt",
            storage_key="attachment-1/report.txt",
            max_bytes=1024,
            created_at=10,
            lease_id="lease-1",
            lease_seconds=300,
        )


def test_pending_materialization_begins_and_replays_hidden_identity() -> None:
    repository = MaterializationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.extend((Result(), returned(pending_row())))
    transaction.fetch_one_results.append({"state": "active"})

    created = repository.begin_pending_materialization(
        transaction,
        attachment_id="attachment-1",
        owner_id="owner-1",
        filename="report.txt",
        category="text",
        extension="txt",
        storage_locator="owner-1/attachment-1/report.txt",
        storage_key="attachment-1/report.txt",
        max_bytes=1024,
        created_at=10,
        lease_id="lease-1",
        lease_seconds=300,
    )

    assert created.state is AttachmentMaterializationState.PENDING
    assert created.pending is not None
    assert created.pending.storage_key == "attachment-1/report.txt"
    assert "materialization_state" in transaction.calls[-1][1]
    assert "%s" in transaction.calls[-1][1]
    assert "?" not in transaction.calls[-1][1]

    replay = FakeTransaction()
    replay.execute_results.extend((Result(), Result(rowcount=0)))
    replay.fetch_one_results.extend(({"state": "active"}, pending_row()))
    assert (
        repository.begin_pending_materialization(
            replay,
            attachment_id="attachment-1",
            owner_id="owner-1",
            filename="report.txt",
            category="text",
            extension="txt",
            storage_locator="owner-1/attachment-1/report.txt",
            storage_key="attachment-1/report.txt",
            max_bytes=1024,
            created_at=10,
            lease_id="lease-1",
            lease_seconds=600,
        )
        == created
    )


def test_pending_materialization_conflicts_are_not_reported_as_success() -> None:
    repository = MaterializationRepository()
    foreign = FakeTransaction()
    foreign.execute_results.append(Result())
    foreign.fetch_one_results.append({"state": "retired"})
    with pytest.raises(RepositoryConflictError, match="retired"):
        repository.begin_pending_materialization(
            foreign,
            attachment_id="attachment-1",
            owner_id="owner-1",
            filename="report.txt",
            category="text",
            extension="txt",
            storage_locator="owner-1/attachment-1/report.txt",
            storage_key="attachment-1/report.txt",
            max_bytes=1024,
            created_at=10,
            lease_id="lease-1",
            lease_seconds=300,
        )

    changed = FakeTransaction()
    changed.execute_results.extend((Result(), Result(rowcount=0)))
    changed.fetch_one_results.extend(
        (
            {"state": "active"},
            pending_row(
                filename="other.txt",
                storage_path="owner-1/attachment-1/other.txt",
                materialization_storage_key="attachment-1/other.txt",
            ),
        )
    )
    with pytest.raises(RepositoryConflictError, match="different semantics"):
        repository.begin_pending_materialization(
            changed,
            attachment_id="attachment-1",
            owner_id="owner-1",
            filename="report.txt",
            category="text",
            extension="txt",
            storage_locator="owner-1/attachment-1/report.txt",
            storage_key="attachment-1/report.txt",
            max_bytes=1024,
            created_at=10,
            lease_id="lease-1",
            lease_seconds=300,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"attachment_id": "bad/path"},
        {"attachment_id": "CON"},
        {"storage_key": "other/report.txt"},
        {"storage_key": "attachment-1"},
        {"storage_key": "attachment-1/other.txt"},
        {"storage_key": ".astralplane-hidden/report.txt"},
        {"storage_locator": "attachment-1/report.txt"},
        {"storage_locator": "owner-2/attachment-1/report.txt"},
        {"storage_locator": "owner-1/.astralplane-hidden/report.txt"},
        {"filename": "nested/report.txt"},
        {"category": "unknown"},
        {"max_bytes": 0},
        {"max_bytes": 1 << 63},
        {"lease_id": "bad lease"},
        {"lease_id": "a" * 129},
        {"lease_seconds": 0},
    ],
)
def test_materialization_validates_bounded_pending_identity(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "attachment_id": "attachment-1",
        "owner_id": "owner-1",
        "filename": "report.txt",
        "category": "text",
        "extension": "txt",
        "storage_locator": "owner-1/attachment-1/report.txt",
        "storage_key": "attachment-1/report.txt",
        "max_bytes": 1024,
        "created_at": 10,
        "lease_id": "lease-1",
        "lease_seconds": 300,
    }
    values.update(kwargs)
    with pytest.raises(RepositoryValidationError):
        MaterializationRepository().begin_pending_materialization(
            FakeTransaction(),
            **values,
        )


def test_pending_lease_renews_by_db_clock_and_exact_version() -> None:
    repository = MaterializationRepository()
    transaction = FakeTransaction()
    transaction.execute_results.extend(
        (Result(), returned(pending_row(materialization_lease_version=1)))
    )
    transaction.fetch_one_results.append({"state": "active"})

    renewed = repository.renew_pending_materialization(
        transaction,
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
        lease_seconds=300,
    )

    assert renewed.lease_version == 1
    sql = transaction.calls[-1][1]
    assert "clock_timestamp()" in sql
    assert "materialization_lease_version = %s" in sql


def test_pending_lease_renewal_replays_exactly_and_rejects_stale_or_missing_rows() -> None:
    repository = MaterializationRepository()
    replay = FakeTransaction()
    replay.execute_results.extend((Result(), Result(rowcount=0)))
    replay.fetch_one_results.extend(
        (
            {"state": "active"},
            pending_row(materialization_lease_version=1),
        )
    )
    assert (
        repository.renew_pending_materialization(
            replay,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            lease_seconds=300,
        ).lease_version
        == 1
    )

    stale = FakeTransaction()
    stale.execute_results.extend((Result(), Result(rowcount=0)))
    stale.fetch_one_results.extend(
        (
            {"state": "active"},
            pending_row(materialization_lease_version=3),
        )
    )
    with pytest.raises(RepositoryConflictError):
        repository.renew_pending_materialization(
            stale,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            lease_seconds=300,
        )

    missing = FakeTransaction()
    missing.execute_results.extend((Result(), Result(rowcount=0)))
    missing.fetch_one_results.extend(({"state": "active"}, None))
    with pytest.raises(RepositoryNotFoundError):
        repository.renew_pending_materialization(
            missing,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            lease_seconds=300,
        )


def test_finalize_pending_materialization_replays_exact_evidence_and_rejects_drift() -> None:
    repository = MaterializationRepository()
    digest = hashlib.sha256(b"data").hexdigest()
    fence = _AttachmentMaterializationPublishFence(
        owner_id="owner-1",
        attachment_id="attachment-1",
        filename="report.txt",
        storage_key="attachment-1/report.txt",
        storage_locator="owner-1/attachment-1/report.txt",
        max_bytes=4,
        lease_id="lease-1",
        lease_version=0,
    )
    ready = attachment_row(size_bytes=4, sha256=digest) | {
        "materialization_state": "ready",
        "materialization_lease_id": "lease-1",
        "materialization_lease_version": 1,
        "materialization_lease_expires_at": NOW + timedelta(minutes=5),
        "materialization_max_bytes": 4,
        "materialization_storage_key": "attachment-1/report.txt",
    }

    replay = FakeTransaction()
    replay.execute_results.extend((Result(), Result(rowcount=0)))
    replay.fetch_one_results.extend(({"state": "active"}, ready))
    finalized = repository._finalize_pending_materialization(
        replay,
        fence=fence,
        content_type="text/plain",
        size_bytes=4,
        sha256=digest,
    )
    assert finalized.size_bytes == 4
    assert finalized.sha256 == digest

    drift = FakeTransaction()
    drift.execute_results.extend((Result(), Result(rowcount=0)))
    drift.fetch_one_results.extend(
        (
            {"state": "active"},
            ready | {"materialization_lease_version": 2},
        )
    )
    with pytest.raises(RepositoryConflictError):
        repository._finalize_pending_materialization(
            drift,
            fence=fence,
            content_type="text/plain",
            size_bytes=4,
            sha256=digest,
        )

    missing = FakeTransaction()
    missing.execute_results.extend((Result(), Result(rowcount=0)))
    missing.fetch_one_results.extend(({"state": "active"}, None))
    with pytest.raises(RepositoryNotFoundError):
        repository._finalize_pending_materialization(
            missing,
            fence=fence,
            content_type="text/plain",
            size_bytes=4,
            sha256=digest,
        )


@pytest.mark.parametrize(
    "content_type, digest, message",
    [
        (
            "application/x-astralplane-pending-materialization",
            DIGEST_A,
            "content_type is reserved",
        ),
        ("text/plain", "0" * 64, "sha256 is reserved"),
    ],
)
def test_finalize_pending_materialization_rejects_reserved_final_evidence(
    content_type: str,
    digest: str,
    message: str,
) -> None:
    transaction = FakeTransaction()
    transaction.execute_results.append(Result())
    transaction.fetch_one_results.append({"state": "active"})
    fence = _AttachmentMaterializationPublishFence(
        owner_id="owner-1",
        attachment_id="attachment-1",
        filename="report.txt",
        storage_key="attachment-1/report.txt",
        storage_locator="owner-1/attachment-1/report.txt",
        max_bytes=4,
        lease_id="lease-1",
        lease_version=0,
    )
    with pytest.raises(RepositoryValidationError, match=message):
        MaterializationRepository()._finalize_pending_materialization(
            transaction,
            fence=fence,
            content_type=content_type,
            size_bytes=4,
            sha256=digest,
        )


def test_staged_publish_is_the_only_public_creation_path_and_finalizes_in_caller_tx(
    tmp_path,
) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    digest = hashlib.sha256(b"data").hexdigest()
    transaction = FakeTransaction()
    transaction.execute_results.extend(
        (
            Result(),
            Result(),
            Result(),
            returned(attachment_row(size_bytes=4, sha256=digest)),
        )
    )
    transaction.fetch_one_results.extend(
        (
            {"state": "active"},
            pending_row(materialization_max_bytes=4),
            {"state": "active"},
            pending_row(materialization_max_bytes=4),
            {"state": "active"},
        )
    )
    reservation = blobs.reserve_materialization_staging(owner_id="owner-1")
    staging = repository.open_pending_materialization_staging(
        transaction,
        blobs=blobs,
        reservation=reservation,
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
    )
    staged = staging.write_chunks((b"data",))

    finalized = repository.publish_pending_materialization(
        transaction,
        blobs=blobs,
        staged=staged,
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
        content_type="text/plain",
    )

    assert finalized.size_bytes == 4
    assert finalized.sha256 == digest
    with blobs.open_reader(
        owner_id="owner-1",
        key="attachment-1/report.txt",
        max_bytes=4,
    ) as reader:
        assert reader.read() == b"data"
    assert "materialization_state = 'ready'" in transaction.calls[-1][1]
    assert not hasattr(repository, "register")


def test_invalid_final_metadata_never_publishes_staged_bytes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    opening = FakeTransaction()
    opening.execute_results.append(Result())
    opening.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=4))
    )
    staging = repository.open_pending_materialization_staging(
        opening,
        blobs=blobs,
        reservation=blobs.reserve_materialization_staging(owner_id="owner-1"),
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
    )
    staged = staging.write_chunks((b"data",))
    publish_calls: list[object] = []
    monkeypatch.setattr(
        blobs,
        "_publish_staged_materialization",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )
    transaction = FakeTransaction()

    with pytest.raises(RepositoryValidationError, match="content_type is reserved"):
        repository.publish_pending_materialization(
            transaction,
            blobs=blobs,
            staged=staged,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            content_type="application/x-astralplane-pending-materialization",
        )

    assert transaction.calls == []
    assert publish_calls == []
    staged.abort()
    assert blobs.is_absent(owner_id="owner-1", key="attachment-1/report.txt")


def test_materialization_publish_and_stage_open_reject_foreign_capabilities(
    tmp_path,
) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    with pytest.raises(RepositoryValidationError, match="configured Plane streaming"):
        repository.publish_pending_materialization(
            FakeTransaction(),
            blobs=object(),  # type: ignore[arg-type]
            staged=object(),  # type: ignore[arg-type]
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            content_type="text/plain",
        )
    with pytest.raises(RepositoryValidationError, match="BlobStagedWrite"):
        repository.publish_pending_materialization(
            FakeTransaction(),
            blobs=blobs,
            staged=object(),  # type: ignore[arg-type]
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            content_type="text/plain",
        )
    with pytest.raises(RepositoryValidationError, match="configured Plane streaming"):
        repository.open_pending_materialization_staging(
            FakeTransaction(),
            blobs=object(),  # type: ignore[arg-type]
            reservation=object(),  # type: ignore[arg-type]
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
        )
    with pytest.raises(RepositoryValidationError, match="reservation must be acquired"):
        repository.open_pending_materialization_staging(
            FakeTransaction(),
            blobs=blobs,
            reservation=object(),  # type: ignore[arg-type]
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
        )
    blobs.close()


def test_stage_open_conflict_releases_the_preacquired_owner_reservation(tmp_path) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    transaction = FakeTransaction()
    transaction.execute_results.append(Result())
    transaction.fetch_one_results.extend(({"state": "active"}, None))
    reservation = blobs.reserve_materialization_staging(owner_id="owner-1")

    with pytest.raises(RepositoryNotFoundError):
        repository.open_pending_materialization_staging(
            transaction,
            blobs=blobs,
            reservation=reservation,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
        )

    assert blobs._owner_locks._entries == {}
    blobs.close()


def test_publish_rejects_staged_bytes_above_the_durable_pending_limit(tmp_path) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    opening = FakeTransaction()
    opening.execute_results.append(Result())
    opening.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=8))
    )
    session = repository.open_pending_materialization_staging(
        opening,
        blobs=blobs,
        reservation=blobs.reserve_materialization_staging(owner_id="owner-1"),
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
    )
    staged = session.write_chunks((b"12345",))
    transaction = FakeTransaction()
    transaction.execute_results.append(Result())
    transaction.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=4))
    )

    with pytest.raises(RepositoryValidationError, match="durable maximum"):
        repository.publish_pending_materialization(
            transaction,
            blobs=blobs,
            staged=staged,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            content_type="text/plain",
        )

    staged.abort()
    assert blobs.is_absent(owner_id="owner-1", key="attachment-1/report.txt")
    blobs.close()


def test_corrupt_pending_physical_identity_never_publishes_staged_bytes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    opening = FakeTransaction()
    opening.execute_results.append(Result())
    opening.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=4))
    )
    staging = repository.open_pending_materialization_staging(
        opening,
        blobs=blobs,
        reservation=blobs.reserve_materialization_staging(owner_id="owner-1"),
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
    )
    staged = staging.write_chunks((b"data",))
    publish_calls: list[object] = []
    monkeypatch.setattr(
        blobs,
        "_publish_staged_materialization",
        lambda *args, **kwargs: publish_calls.append((args, kwargs)),
    )
    transaction = FakeTransaction()
    transaction.execute_results.append(Result())
    transaction.fetch_one_results.extend(
        (
            {"state": "active"},
            pending_row(
                storage_path="owner-1/attachment-1/other.txt",
                materialization_storage_key="attachment-1/other.txt",
            ),
        )
    )

    with pytest.raises(RepositoryDataError, match="physical storage identity"):
        repository.publish_pending_materialization(
            transaction,
            blobs=blobs,
            staged=staged,
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
            content_type="text/plain",
        )

    assert publish_calls == []
    staged.abort()
    assert blobs.is_absent(owner_id="owner-1", key="attachment-1/report.txt")


def test_staging_coordinator_aborts_hidden_bytes_when_stage_open_commit_fails(
    tmp_path,
) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    transaction = FakeTransaction()
    transaction.execute_results.append(Result())
    transaction.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=4))
    )
    coordinator = AttachmentMaterializationCoordinator(
        database=FakeDatabase(transaction, fail_commit=True),
        repository=repository,
        blobs=blobs,
    )

    with pytest.raises(RuntimeError, match="commit failure"):
        coordinator.open_pending_materialization_staging(
            owner_id="owner-1",
            attachment_id="attachment-1",
            lease_id="lease-1",
            expected_lease_version=0,
        )

    assert blobs._owner_locks._entries == {}
    assert blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-1")
    coordinator.close()
    blobs.close()


def test_materialization_coordinator_rejects_foreign_dependencies_and_closed_async_use(
    tmp_path,
) -> None:
    repository = MaterializationRepository()
    database = FakeDatabase(FakeTransaction())
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    with pytest.raises(RepositoryValidationError, match="explicit Plane transactions"):
        AttachmentMaterializationCoordinator(
            database=object(),  # type: ignore[arg-type]
            repository=repository,
            blobs=blobs,
        )
    with pytest.raises(RepositoryValidationError, match="MaterializationRepository"):
        AttachmentMaterializationCoordinator(
            database=database,
            repository=object(),  # type: ignore[arg-type]
            blobs=blobs,
        )
    with pytest.raises(RepositoryValidationError, match="configured Plane streaming"):
        AttachmentMaterializationCoordinator(
            database=database,
            repository=repository,
            blobs=object(),  # type: ignore[arg-type]
        )

    coordinator = AttachmentMaterializationCoordinator(
        database=database,
        repository=repository,
        blobs=blobs,
    )
    coordinator.close()
    coordinator.close()
    with pytest.raises(RepositoryValidationError, match="coordinator is closed"):
        asyncio.run(
            coordinator.abegin_pending_materialization(
                attachment_id="attachment-1",
                owner_id="owner-1",
                filename="report.txt",
                category="text",
                extension="txt",
                storage_locator="owner-1/attachment-1/report.txt",
                storage_key="attachment-1/report.txt",
                max_bytes=4,
                created_at=10,
                lease_id="lease-1",
                lease_seconds=300,
            )
        )
    with pytest.raises(RepositoryValidationError, match="coordinator is closed"):
        asyncio.run(
            coordinator.aopen_pending_materialization_staging(
                owner_id="owner-1",
                attachment_id="attachment-1",
                lease_id="lease-1",
                expected_lease_version=0,
            )
        )
    assert blobs._owner_locks._entries == {}
    assert blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-1")
    blobs.close()


def test_staging_coordinator_double_cancel_observes_commit_and_aborts_capability(
    tmp_path,
) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    transaction = FakeTransaction()
    transaction.execute_results.append(Result())
    transaction.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=4))
    )
    commit_entered = threading.Event()
    release_commit = threading.Event()
    coordinator = AttachmentMaterializationCoordinator(
        database=FakeDatabase(
            transaction,
            commit_entered=commit_entered,
            release_commit=release_commit,
        ),
        repository=repository,
        blobs=blobs,
    )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blocked-default")
        loop.set_default_executor(default)
        default_started = threading.Event()
        release_default = threading.Event()

        def block_default() -> None:
            default_started.set()
            assert release_default.wait(timeout=5)

        blocked = loop.run_in_executor(None, block_default)
        deadline = loop.time() + 2
        while not default_started.is_set():
            assert loop.time() < deadline
            await asyncio.sleep(0.01)

        task = asyncio.create_task(
            coordinator.aopen_pending_materialization_staging(
                owner_id="owner-1",
                attachment_id="attachment-1",
                lease_id="lease-1",
                expected_lease_version=0,
            )
        )
        while not commit_entered.is_set():
            assert loop.time() < deadline
            await asyncio.sleep(0.01)
        task.cancel()
        task.cancel()
        release_commit.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        release_default.set()
        await blocked

    asyncio.run(scenario())
    assert blobs._owner_locks._entries == {}
    assert blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-1")
    coordinator.close()
    blobs.close()


def test_stage_reservation_waits_before_any_db_lock_and_does_not_block_heartbeat(
    tmp_path,
) -> None:
    repository = MaterializationRepository()
    blobs = ExplicitRootStreamingBlobStore((tmp_path / "blobs").resolve(), create_root=True)
    first_tx = FakeTransaction()
    first_tx.execute_results.append(Result())
    first_tx.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=4))
    )
    first_reservation = blobs.reserve_materialization_staging(owner_id="owner-1")
    first = repository.open_pending_materialization_staging(
        first_tx,
        blobs=blobs,
        reservation=first_reservation,
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
    )

    second_tx = FakeTransaction()
    second_tx.execute_results.append(Result())
    second_tx.fetch_one_results.extend(
        ({"state": "active"}, pending_row(materialization_max_bytes=4))
    )
    waiting = threading.Event()
    reserved = threading.Event()
    failures: list[BaseException] = []

    def open_second() -> None:
        waiting.set()
        try:
            reservation = blobs.reserve_materialization_staging(owner_id="owner-1")
            reserved.set()
            second = repository.open_pending_materialization_staging(
                second_tx,
                blobs=blobs,
                reservation=reservation,
                owner_id="owner-1",
                attachment_id="attachment-1",
                lease_id="lease-1",
                expected_lease_version=0,
            )
            second.abort()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=open_second, daemon=True)
    thread.start()
    assert waiting.wait(timeout=1)
    assert not reserved.wait(timeout=0.05)
    assert second_tx.calls == []

    heartbeat_tx = FakeTransaction()
    heartbeat_tx.execute_results.extend(
        (Result(), returned(pending_row(materialization_lease_version=1)))
    )
    heartbeat_tx.fetch_one_results.append({"state": "active"})
    heartbeat = repository.renew_pending_materialization(
        heartbeat_tx,
        owner_id="owner-1",
        attachment_id="attachment-1",
        lease_id="lease-1",
        expected_lease_version=0,
        lease_seconds=300,
    )
    assert heartbeat.lease_version == 1

    first.abort()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert reserved.is_set()
    assert failures == []
    assert blobs._owner_locks._entries == {}


def test_pending_and_abandoned_rows_are_hidden_from_every_ordinary_read() -> None:
    query = FakeTransaction()
    query.fetch_one_results.extend((None, None))
    query.fetch_all_results.append(())

    assert (
        AttachmentRepository().get(
            query,
            owner_id="owner-1",
            attachment_id="attachment-1",
            include_deleted=True,
        )
        is None
    )
    assert (
        BlobMetadataRepository().get(
            query,
            owner_id="owner-1",
            object_id="attachment-1",
            include_deleted=True,
        )
        is None
    )
    assert AttachmentRepository().list_live(query, owner_id="owner-1") == ()
    read_sql = tuple(statement for operation, statement, _ in query.calls if operation != "execute")
    assert all("materialization_state = 'ready'" in statement for statement in read_sql)


def test_attachment_reads_are_owner_scoped_and_paginated() -> None:
    repository = AttachmentRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((attachment_row(), attachment_row(deleted_at=25), None))
    assert (
        repository.get(query, owner_id="owner-1", attachment_id="attachment-1")
        == attachment_record()
    )
    assert (
        repository.get(
            query,
            owner_id="owner-1",
            attachment_id="attachment-1",
            include_deleted=True,
        ).deleted_at
        == 25
    )  # type: ignore[union-attr]
    assert repository.get(query, owner_id="owner-1", attachment_id="missing") is None

    query.fetch_all_results.extend(
        (
            (attachment_row(),),
            (attachment_row(attachment_id="attachment-0", category="image"),),
        )
    )
    assert len(repository.list_live(query, owner_id="owner-1", limit=1)) == 1
    records = repository.list_live(
        query,
        owner_id="owner-1",
        category="image",
        limit=2,
        before_created_at=11,
        before_attachment_id="attachment-2",
    )
    assert records[0].attachment_id == "attachment-0"
    sql = query.calls[-1][1]
    assert "user_id = %s" in sql and "category = %s" in sql
    assert "(created_at, attachment_id) < (%s, %s)" in sql


@pytest.mark.parametrize(
    "kwargs",
    [
        {"before_created_at": 10},
        {"before_attachment_id": "attachment-1"},
        {"category": "invalid"},
        {"limit": 0},
        {"limit": True},
    ],
)
def test_attachment_list_rejects_invalid_cursor_or_bounds(kwargs: dict[str, Any]) -> None:
    with pytest.raises(RepositoryValidationError):
        AttachmentRepository().list_live(FakeTransaction(), owner_id="owner-1", **kwargs)


def test_attachment_repository_exposes_no_metadata_only_delete_surface() -> None:
    assert not hasattr(AttachmentRepository, "soft_delete")
    assert not hasattr(AttachmentRepository, "soft_delete_all")
    assert not hasattr(AttachmentRepository, "_soft_delete_for_purge")
    assert not hasattr(AttachmentRepository, "_soft_delete_all_for_purge")


def test_blob_metadata_get_is_owner_scoped_and_ready_only() -> None:
    repository = BlobMetadataRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((blob_row(), blob_row(deleted_at=40), None))
    assert (
        repository.get(query, owner_id="owner-1", object_id="attachment-1").object_kind
        == "attachment"
    )  # type: ignore[union-attr]
    assert (
        repository.get(
            query,
            owner_id="owner-1",
            object_id="attachment-1",
            include_deleted=True,
        ).deleted_at
        == 40
    )  # type: ignore[union-attr]
    assert repository.get(query, owner_id="owner-1", object_id="missing") is None
    assert all("materialization_state = 'ready'" in call[1] for call in query.calls)
    assert not hasattr(BlobMetadataRepository, "relocate")


def test_message_attachment_link_is_owner_validated_and_replay_safe() -> None:
    repository = MessageAttachmentRepository()
    transaction = FakeTransaction()
    transaction.execute_results.append(returned(link_row()))
    record = repository.link(
        transaction,
        link_id="link-1",
        owner_id="owner-1",
        conversation_id="chat-1",
        attachment_id="attachment-1",
        message_id=7,
        created_at=20,
    )
    assert record.message_id == "7"
    assert "attachment.user_id = chat.user_id" in transaction.calls[0][1]

    replay = FakeTransaction()
    replay.execute_results.append(Result(rowcount=0))
    replay.fetch_one_results.append(link_row())
    assert (
        repository.link(
            replay,
            link_id="link-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            attachment_id="attachment-1",
            message_id=7,
            created_at=20,
        )
        == record
    )


def test_message_attachment_link_conflict_and_missing_are_visible() -> None:
    repository = MessageAttachmentRepository()
    changed = FakeTransaction()
    changed.execute_results.append(Result(rowcount=0))
    changed.fetch_one_results.append(link_row(attachment_id="attachment-2"))
    with pytest.raises(RepositoryConflictError):
        repository.link(
            changed,
            link_id="link-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            attachment_id="attachment-1",
            message_id=None,
            created_at=20,
        )

    missing = FakeTransaction()
    missing.execute_results.append(Result(rowcount=0))
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.link(
            missing,
            link_id="link-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            attachment_id="attachment-1",
            message_id=None,
            created_at=20,
        )


def test_message_attachment_queries_return_detached_records() -> None:
    repository = MessageAttachmentRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((link_row(), None))
    query.fetch_all_results.extend(((link_row(),), (link_row(),)))
    assert repository.get(query, owner_id="owner-1", link_id="link-1").link_id == "link-1"  # type: ignore[union-attr]
    assert repository.get(query, owner_id="owner-1", link_id="missing") is None
    assert (
        len(repository.list_for_conversation(query, owner_id="owner-1", conversation_id="chat-1"))
        == 1
    )
    assert len(repository.list_for_message(query, owner_id="owner-1", message_id=7)) == 1
    for _, statement, _ in query.calls:
        assert "JOIN user_attachments AS attachment" in statement
        assert "attachment.materialization_state = 'ready'" in statement
        assert "attachment.deleted_at IS NULL" in statement
    assert query.calls[-1][2] == ("7", "owner-1")


def test_artifact_version_archive_serializes_and_prunes() -> None:
    repository = ArtifactVersionRepository()
    transaction = FakeTransaction()
    transaction.fetch_one_results.append({"id": "chat-1"})
    transaction.execute_results.extend(
        (returned(version_row(version_no=6, id=6)), Result(rowcount=1))
    )

    record = repository.archive(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        component_id="component-1",
        component={"type": "Card", "children": ["a"]},
        reason="restore",
        retain=5,
    )

    assert record.version_number == 6
    assert record.component["children"] == ("a",)
    assert "FOR UPDATE" in transaction.calls[0][1]
    assert "user_id = %s" in transaction.calls[-1][1]

    no_prune = FakeTransaction()
    no_prune.fetch_one_results.append({"id": "chat-1"})
    no_prune.execute_results.append(returned(version_row()))
    repository.archive(
        no_prune,
        owner_id="owner-1",
        conversation_id="chat-1",
        component_id="component-1",
        component={"type": "Card"},
    )
    assert len([call for call in no_prune.calls if call[0] == "execute"]) == 1


def test_artifact_version_archive_validation_and_missing_owner() -> None:
    repository = ArtifactVersionRepository()
    missing = FakeTransaction()
    missing.fetch_one_results.append(None)
    with pytest.raises(RepositoryNotFoundError):
        repository.archive(
            missing,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            component={"type": "Card"},
        )
    with pytest.raises(RepositoryValidationError):
        repository.archive(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            component={"type": "Card"},
            reason="unknown",
        )
    with pytest.raises(RepositoryValidationError):
        repository.archive(
            FakeTransaction(),
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            component={"bad": float("nan")},
        )


def test_artifact_version_queries_delete_and_corruption_visibility() -> None:
    repository = ArtifactVersionRepository()
    query = FakeTransaction()
    query.fetch_one_results.extend((version_row(), None))
    query.fetch_all_results.append((version_row(), version_row(id=2, version_no=2)))
    assert (
        repository.get(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            version_number=1,
        ).version_number
        == 1
    )  # type: ignore[union-attr]
    assert (
        repository.get(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            version_number=2,
        )
        is None
    )
    assert (
        len(
            repository.list_for_component(
                query,
                owner_id="owner-1",
                conversation_id="chat-1",
                component_id="component-1",
                limit=2,
            )
        )
        == 2
    )

    query.execute_results.extend((Result(rowcount=4), Result(rowcount=-1)))
    assert (
        repository.delete_for_component(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
        )
        == 4
    )
    assert (
        repository.delete_for_component(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
        )
        == 0
    )
    query.execute_results.append(Result(rowcount=6))
    assert (
        repository.delete_for_conversation(
            query,
            owner_id="owner-1",
            conversation_id="chat-1",
        )
        == 6
    )
    assert query.calls[-1][2] == ("chat-1", "owner-1")
    invalid_delete = FakeTransaction()
    invalid_delete.execute_results.append(Result(rowcount=-1))
    with pytest.raises(RepositoryDataError):
        repository.delete_for_conversation(
            invalid_delete,
            owner_id="owner-1",
            conversation_id="chat-1",
        )

    corrupt = FakeTransaction()
    corrupt.fetch_one_results.append(version_row(created_at="not-time"))
    with pytest.raises(RepositoryDataError, match="timestamp"):
        repository.get(
            corrupt,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            version_number=1,
        )
    corrupt.fetch_one_results.append(version_row(component="[]"))
    with pytest.raises(RepositoryDataError, match="JSON object"):
        repository.get(
            corrupt,
            owner_id="owner-1",
            conversation_id="chat-1",
            component_id="component-1",
            version_number=1,
        )


def test_artifact_facade_exposes_neutral_stores() -> None:
    facade = ArtifactRepository()
    assert isinstance(facade.materializations, MaterializationRepository)
    assert isinstance(facade.attachments, AttachmentRepository)
    assert isinstance(facade.blobs, BlobMetadataRepository)
    assert isinstance(facade.message_attachments, MessageAttachmentRepository)
    assert isinstance(facade.versions, ArtifactVersionRepository)
