"""Safe attachment publication helpers for focused Deep tests.

Production upload policy lives in
``orchestrator.attachments.materialization``.  Tests that need a real Plane
row and real blob bytes still have to use Plane's public pending -> staged ->
ready lifecycle; they must not recreate the retired blob-first publication
path.  Historical orphan fixtures write only inside their isolated temporary
root; Plane intentionally exposes no unfenced writer or physical delete helper.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

from astralplane import create_attachment_materialization_coordinator
from astralplane.repositories.artifacts import AttachmentMaterializationState

from orchestrator.attachments.blob_access import (
    attachment_storage_key,
    metadata_storage_path,
)


def publish_attachment_for_test(
    runtime,
    repositories,
    blobs,
    *,
    owner_id: str,
    attachment_id: str,
    filename: str,
    content_type: str,
    category: str,
    extension: str,
    chunks: Iterable[bytes],
    max_bytes: int,
    created_at: int | None = None,
):
    """Publish one small fixture through Plane's complete durable lifecycle."""

    materializations = create_attachment_materialization_coordinator(
        database=runtime,
        materializations=repositories.artifacts.materializations,
        blobs=blobs,
    )
    storage_key = attachment_storage_key(attachment_id, filename)
    storage_locator = metadata_storage_path(owner_id, storage_key)
    lease_id = f"test-{uuid.uuid4()}"
    observed_at = int(time.time() * 1000) if created_at is None else created_at

    try:
        begun = materializations.begin_pending_materialization(
            attachment_id=attachment_id,
            owner_id=owner_id,
            filename=filename,
            category=category,
            extension=extension,
            storage_locator=storage_locator,
            storage_key=storage_key,
            max_bytes=max_bytes,
            created_at=observed_at,
            lease_id=lease_id,
            lease_seconds=300,
        )
        if begun.state is AttachmentMaterializationState.READY:
            assert begun.ready is not None
            return begun.ready
        assert begun.pending is not None
        lease_version = begun.pending.lease_version

        staging = materializations.open_pending_materialization_staging(
            owner_id=owner_id,
            attachment_id=attachment_id,
            lease_id=lease_id,
            expected_lease_version=lease_version,
        )
        staged = staging.write_chunks(chunks)
        try:
            return materializations.publish_pending_materialization(
                staged=staged,
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                expected_lease_version=lease_version,
                content_type=content_type,
            )
        except BaseException:
            staged.abort()
            raise
    finally:
        materializations.close()


__all__ = ("publish_attachment_for_test",)
