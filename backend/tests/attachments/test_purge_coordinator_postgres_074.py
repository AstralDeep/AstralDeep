"""Live-PostgreSQL proof for Deep's durable streaming-purge composition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from astralplane import create_streaming_blob_store
from astralplane import PurgeAttemptResult, PurgeAttemptState

from orchestrator.attachments.purge import (
    AttachmentPurgeCoordinator,
    AttachmentPurgeReadinessError,
)
from orchestrator.attachments.repository import AttachmentRepository
from tests.helpers.attachment_materialization import publish_attachment_for_test
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def plane_runtime():
    with isolated_plane_runtime("deep_purge_074") as runtime:
        yield runtime


def _register_attachment(
    runtime: PlaneTestRuntime,
    blobs,
    *,
    owner_id: str,
    attachment_id: str,
) -> AttachmentRepository:
    payload = f"payload:{owner_id}:{attachment_id}".encode()
    publish_attachment_for_test(
        runtime,
        runtime.repositories,
        blobs,
        owner_id=owner_id,
        attachment_id=attachment_id,
        filename="payload.bin",
        content_type="application/octet-stream",
        category="data",
        extension="bin",
        chunks=(payload,),
        max_bytes=1024,
    )
    repository = AttachmentRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    return repository


class _FailingExecutor:
    """Leave the real tombstone pending without bypassing Plane destruction."""

    def execute(self, **values):
        return PurgeAttemptResult(
            state=PurgeAttemptState.FAILED,
            tombstone_id=str(values["tombstone_id"]),
            attempt=1,
            error_code="simulated_storage_outage",
        )

    def reconcile_ready_for_administration(self, **_values):
        return ()


def test_failed_physical_delete_is_restart_recoverable_without_live_metadata(
    plane_runtime: PlaneTestRuntime,
    tmp_path,
) -> None:
    blobs = create_streaming_blob_store(root=tmp_path / "blobs")
    repository = _register_attachment(
        plane_runtime,
        blobs,
        owner_id="owner-1",
        attachment_id="attachment-1",
    )
    failing = AttachmentPurgeCoordinator(
        plane_runtime=plane_runtime,
        purge_repository=plane_runtime.repositories.purge,
        blobs=blobs,
        executor=_FailingExecutor(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=5),
    )
    failing.reconcile_startup()

    outcome = failing.schedule_attachment(
        owner_id="owner-1",
        attachment_id="attachment-1",
    )

    assert outcome.completed is False
    assert repository.get_by_id("attachment-1", "owner-1") is None
    assert blobs.is_prefix_absent(
        owner_id="owner-1", prefix="attachment-1"
    ) is False
    assert failing.ready is False

    restarted = AttachmentPurgeCoordinator(
        plane_runtime=plane_runtime,
        purge_repository=plane_runtime.repositories.purge,
        blobs=blobs,
        clock=lambda: NOW + timedelta(seconds=6),
        retry_delay=timedelta(seconds=5),
    )
    recovered = restarted.reconcile_startup()

    assert len(recovered) == 1
    assert all(result.error_code is None for result in recovered)
    assert restarted.ready is True
    assert blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-1")


def test_caller_transaction_rollback_preserves_metadata_and_no_tombstone(
    plane_runtime: PlaneTestRuntime,
    tmp_path,
) -> None:
    blobs = create_streaming_blob_store(root=tmp_path / "blobs")
    repository = _register_attachment(
        plane_runtime,
        blobs,
        owner_id="owner-1",
        attachment_id="attachment-rollback",
    )
    real_purge = plane_runtime.repositories.purge

    class FailAfterSchedule:
        def schedule_attachment_prefix(self, transaction, **values):
            real_purge.schedule_attachment_prefix(transaction, **values)
            raise RuntimeError("caller rollback")

    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=plane_runtime,
        purge_repository=FailAfterSchedule(),  # type: ignore[arg-type]
        blobs=blobs,
        executor=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="caller rollback"):
        coordinator.schedule_attachment(
            owner_id="owner-1",
            attachment_id="attachment-rollback",
        )

    assert repository.get_by_id("attachment-rollback", "owner-1") is not None
    assert blobs.is_prefix_absent(
        owner_id="owner-1", prefix="attachment-rollback"
    ) is False
    with plane_runtime.transaction() as transaction:
        assert real_purge.has_incomplete_for_administration(transaction) is False


def test_owner_namespace_purges_orphan_bytes_without_metadata(
    plane_runtime: PlaneTestRuntime,
    tmp_path,
) -> None:
    blob_root = tmp_path / "blobs"
    blobs = create_streaming_blob_store(root=blob_root)
    # Model a pre-cutover crash orphan directly inside this isolated fixture
    # root.  Production callers have no unfenced Plane write/delete surface.
    orphan = blob_root / "owner-orphan" / "orphan" / "file.bin"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    coordinator = AttachmentPurgeCoordinator(
        plane_runtime=plane_runtime,
        purge_repository=plane_runtime.repositories.purge,
        blobs=blobs,
        clock=lambda: NOW,
    )
    coordinator.reconcile_startup()

    outcome = coordinator.schedule_owner(owner_id="owner-orphan")

    assert outcome.completed is True
    assert outcome.metadata_rows_soft_deleted == 0
    assert blobs.is_owner_absent(owner_id="owner-orphan")
    coordinator.assert_ready()


def test_live_probe_detects_another_coordinators_committed_tombstone(
    plane_runtime: PlaneTestRuntime,
    tmp_path,
) -> None:
    blobs = create_streaming_blob_store(root=tmp_path / "blobs")
    _register_attachment(
        plane_runtime,
        blobs,
        owner_id="owner-cross-process",
        attachment_id="attachment-cross-process",
    )
    observer = AttachmentPurgeCoordinator(
        plane_runtime=plane_runtime,
        purge_repository=plane_runtime.repositories.purge,
        blobs=blobs,
        clock=lambda: NOW,
    )
    scheduler = AttachmentPurgeCoordinator(
        plane_runtime=plane_runtime,
        purge_repository=plane_runtime.repositories.purge,
        blobs=blobs,
        executor=_FailingExecutor(),  # type: ignore[arg-type]
        clock=lambda: NOW,
        retry_delay=timedelta(seconds=5),
    )
    observer.reconcile_startup()
    scheduler.reconcile_startup()

    outcome = scheduler.schedule_attachment(
        owner_id="owner-cross-process",
        attachment_id="attachment-cross-process",
    )
    assert outcome.completed is False
    observer.assert_ready()

    with plane_runtime.transaction() as transaction:
        with pytest.raises(
            AttachmentPurgeReadinessError,
            match="purge_reconciliation_incomplete",
        ):
            observer.assert_globally_ready(transaction)
    assert observer.ready is False
