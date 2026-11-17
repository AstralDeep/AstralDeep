"""Real-PostgreSQL tests for astralplane.purge: concurrent casefold-identity contention,
schedule rollback and physical reconciliation, and two executors converging on one
purge prefix without forking.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from astralplane.api import (
    create_attachment_materialization_coordinator,
    create_durable_purge_executor,
    create_repository_catalog,
    create_streaming_blob_store,
)
from astralplane.blob_store import (
    BlobDeleteResult,
    ExplicitRootStreamingBlobStore,
    StreamingBlobStore,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import PlaneError
from astralplane.purge import PurgeAttemptState
from tests.fixtures.migrated_template import DatabaseTemplate, bound_clone
from tests.integration.test_lifecycle_acceptance import concurrent_database

_NOW = datetime(2026, 8, 14, 22, tzinfo=UTC)


@dataclass(slots=True)
class _PurgeFixture:
    database_url: str
    connection: Any
    schema: str
    pool: ConnectionPool
    database: PlaneDatabase

    @property
    def dsn(self):
        return self.database_url


@pytest.fixture(scope="module")
def purge_database(migrated_template: DatabaseTemplate) -> Iterator[_PurgeFixture]:
    with bound_clone(migrated_template) as clone:
        yield _PurgeFixture(clone.dsn, clone.connection, clone.schema, clone.pool, clone.database)


def _materialize_attachment(
    fixture: _PurgeFixture,
    *,
    owner_id: str,
    attachment_id: str,
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    catalog = create_repository_catalog()
    coordinator = create_attachment_materialization_coordinator(
        database=fixture.database,
        materializations=catalog.artifacts.materializations,
        blobs=blobs,
    )
    lease_id = f"lease-{uuid.uuid4().hex}"
    try:
        begun = coordinator.begin_pending_materialization(
            attachment_id=attachment_id,
            owner_id=owner_id,
            filename="fixture.bin",
            category="data",
            extension="bin",
            storage_locator=f"{owner_id}/{attachment_id}/fixture.bin",
            storage_key=f"{attachment_id}/fixture.bin",
            max_bytes=4,
            created_at=1,
            lease_id=lease_id,
            lease_seconds=300,
        )
        assert begun.pending is not None
        version = begun.pending.lease_version
        session = coordinator.open_pending_materialization_staging(
            owner_id=owner_id,
            attachment_id=attachment_id,
            lease_id=lease_id,
            expected_lease_version=version,
        )
        staged = session.write_chunks((b"data",))
        ready = coordinator.publish_pending_materialization(
            staged=staged,
            owner_id=owner_id,
            attachment_id=attachment_id,
            lease_id=lease_id,
            expected_lease_version=version,
            content_type="application/octet-stream",
        )
        assert ready.sha256 == hashlib.sha256(b"data").hexdigest()
    finally:
        coordinator.close()


def test_concurrent_casefold_attachment_identities_cannot_both_begin(
    purge_database: _PurgeFixture,
) -> None:
    fixture = purge_database
    owner_id = f"case-owner-{uuid.uuid4().hex}"
    base = f"case-{uuid.uuid4().hex}"
    attachment_ids = (base.lower(), base.upper())
    first_inserted = threading.Event()
    allow_first_commit = threading.Event()
    outcomes: list[tuple[str, object]] = []
    outcome_guard = threading.Lock()

    def begin(index: int) -> None:
        with concurrent_database(fixture) as database:
            try:
                with database.transaction() as transaction:
                    materializations = create_repository_catalog().artifacts.materializations
                    result = materializations.begin_pending_materialization(
                        transaction,
                        attachment_id=attachment_ids[index],
                        owner_id=owner_id,
                        filename="fixture.bin",
                        category="data",
                        extension="bin",
                        storage_locator=(f"{owner_id}/{attachment_ids[index]}/fixture.bin"),
                        storage_key=f"{attachment_ids[index]}/fixture.bin",
                        max_bytes=4,
                        created_at=1,
                        lease_id=f"case-lease-{index}",
                        lease_seconds=300,
                    )
                    if index == 0:
                        first_inserted.set()
                        assert allow_first_commit.wait(timeout=10)
                with outcome_guard:
                    outcomes.append(("success", result))
            except BaseException as exc:
                with outcome_guard:
                    outcomes.append(("failure", exc))

    first = threading.Thread(target=begin, args=(0,))
    second = threading.Thread(target=begin, args=(1,))
    first.start()
    assert first_inserted.wait(timeout=10)
    second.start()
    time.sleep(0.1)
    assert second.is_alive(), "the case-fold contender must wait for the first transaction"
    allow_first_commit.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert [state for state, _ in outcomes].count("success") == 1
    assert [state for state, _ in outcomes].count("failure") == 1

    with fixture.database.transaction() as transaction:
        with pytest.raises(PlaneError) as alias:
            create_repository_catalog().purge.schedule_attachment_prefix(
                transaction,
                owner_id=owner_id,
                attachment_id=attachment_ids[1],
                requested_at=_NOW,
                deleted_at=2,
            )
        assert alias.value.code == "purge_object_not_found"
        transaction.execute(
            "DELETE FROM user_attachments WHERE user_id = %s",
            (owner_id,),
        )
        transaction.execute(
            "DELETE FROM astralplane_blob_owner_state WHERE owner_id = %s",
            (owner_id,),
        )


def test_schedule_rollback_then_commit_and_physical_reconciliation(
    purge_database: _PurgeFixture,
    tmp_path: Path,
) -> None:
    fixture = purge_database
    catalog = create_repository_catalog()
    owner_id = f"purge-owner-{uuid.uuid4().hex}"
    attachment_id = f"attachment-{uuid.uuid4().hex}"
    blob_root = (tmp_path / "blobs").resolve()
    blobs = create_streaming_blob_store(root=blob_root)
    assert isinstance(blobs, ExplicitRootStreamingBlobStore)
    _materialize_attachment(
        fixture,
        owner_id=owner_id,
        attachment_id=attachment_id,
        blobs=blobs,
    )

    scheduled_id = ""
    with (
        pytest.raises(RuntimeError, match="forced rollback"),
        fixture.database.transaction() as transaction,
    ):
        scheduled = catalog.purge.schedule_attachment_prefix(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
            requested_at=_NOW,
            deleted_at=2,
        )
        scheduled_id = scheduled.tombstone.tombstone_id
        raise RuntimeError("forced rollback")

    with fixture.database.transaction() as transaction:
        metadata = catalog.artifacts.attachments.get(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
            include_deleted=True,
        )
        assert metadata is not None and metadata.deleted_at is None
        assert (
            catalog.purge.load(
                transaction,
                owner_id=owner_id,
                tombstone_id=scheduled_id,
            )
            is None
        )
        scheduled = catalog.purge.schedule_attachment_prefix(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
            requested_at=_NOW + timedelta(seconds=1),
            deleted_at=3,
        )

    executor = create_durable_purge_executor(
        database=fixture.database,
        purge_store=catalog.purge,
        blobs=blobs,
    )
    assert executor.has_incomplete_for_administration() is True
    result = executor.execute(
        owner_id=owner_id,
        tombstone_id=scheduled.tombstone.tombstone_id,
        now=_NOW + timedelta(seconds=1),
        retry_at=_NOW + timedelta(minutes=1),
    )

    assert result.state is PurgeAttemptState.PURGED
    assert blobs.is_prefix_absent(owner_id=owner_id, prefix=attachment_id)
    assert executor.has_incomplete_for_administration() is False
    blobs.close()


class _BarrierBlobStore:
    def __init__(self, delegate: StreamingBlobStore) -> None:
        self._delegate = delegate
        self._barrier = threading.Barrier(2)

    def _delete_for_purge(self, authority: Any) -> BlobDeleteResult:
        self._barrier.wait(timeout=10)
        return self._delegate._delete_for_purge(authority)

    def is_absent(self, *, owner_id: str, key: str) -> bool:
        return self._delegate.is_absent(owner_id=owner_id, key=key)

    def is_prefix_absent(self, *, owner_id: str, prefix: str) -> bool:
        return self._delegate.is_prefix_absent(owner_id=owner_id, prefix=prefix)

    def is_owner_absent(self, *, owner_id: str) -> bool:
        return self._delegate.is_owner_absent(owner_id=owner_id)


def test_two_postgres_executors_converge_one_prefix_without_fork(
    purge_database: _PurgeFixture,
    tmp_path: Path,
) -> None:
    fixture = purge_database
    catalog = create_repository_catalog()
    owner_id = f"concurrent-owner-{uuid.uuid4().hex}"
    attachment_id = f"attachment-{uuid.uuid4().hex}"
    blob_root = (tmp_path / "blobs").resolve()
    blobs = create_streaming_blob_store(root=blob_root)
    assert isinstance(blobs, ExplicitRootStreamingBlobStore)
    _materialize_attachment(
        fixture,
        owner_id=owner_id,
        attachment_id=attachment_id,
        blobs=blobs,
    )
    with fixture.database.transaction() as transaction:
        scheduled = catalog.purge.schedule_attachment_prefix(
            transaction,
            owner_id=owner_id,
            attachment_id=attachment_id,
            requested_at=_NOW,
            deleted_at=2,
        )
    synchronized_blobs = _BarrierBlobStore(blobs)
    second_scope = ExitStack()
    second_database = second_scope.enter_context(concurrent_database(fixture))
    executors = (
        create_durable_purge_executor(
            database=fixture.database,
            purge_store=catalog.purge,
            blobs=synchronized_blobs,
        ),
        create_durable_purge_executor(
            database=second_database,
            purge_store=catalog.purge,
            blobs=synchronized_blobs,
        ),
    )
    results: list[PurgeAttemptState] = []
    errors: list[BaseException] = []

    def run(executor) -> None:
        try:
            result = executor.execute(
                owner_id=owner_id,
                tombstone_id=scheduled.tombstone.tombstone_id,
                now=_NOW,
                retry_at=_NOW + timedelta(minutes=1),
            )
            results.append(result.state)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = tuple(threading.Thread(target=run, args=(executor,)) for executor in executors)
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert sorted(results) == sorted(
            (PurgeAttemptState.PURGED, PurgeAttemptState.ALREADY_PURGED)
        )
        assert blobs.is_prefix_absent(owner_id=owner_id, prefix=attachment_id)
        with fixture.database.transaction() as transaction:
            final = catalog.purge.load(
                transaction,
                owner_id=owner_id,
                tombstone_id=scheduled.tombstone.tombstone_id,
            )
        assert final is not None
        assert final.attempt_count == 1
        assert final.version == 1
    finally:
        second_scope.close()
        blobs.close()


def test_owner_namespace_schedule_deletes_orphan_bytes_and_all_metadata(
    purge_database: _PurgeFixture,
    tmp_path: Path,
) -> None:
    fixture = purge_database
    catalog = create_repository_catalog()
    owner_id = f"__verif__{uuid.uuid4().hex}_everyday_primary"
    attachment_ids = (
        f"attachment-{uuid.uuid4().hex}",
        f"attachment-{uuid.uuid4().hex}",
    )
    blob_root = (tmp_path / "blobs").resolve()
    blobs = create_streaming_blob_store(root=blob_root)
    assert isinstance(blobs, ExplicitRootStreamingBlobStore)
    for attachment_id in attachment_ids:
        _materialize_attachment(
            fixture,
            owner_id=owner_id,
            attachment_id=attachment_id,
            blobs=blobs,
        )
    with fixture.database.transaction() as transaction:
        scheduled = catalog.purge.schedule_owner_namespace(
            transaction,
            owner_id=owner_id,
            requested_at=_NOW,
            deleted_at=2,
        )
    for attachment_id in ("orphan-without-metadata",):
        fixture_blob = blob_root / owner_id / attachment_id / "fixture.bin"
        fixture_blob.parent.mkdir(parents=True)
        fixture_blob.write_bytes(b"data")

    executor = create_durable_purge_executor(
        database=fixture.database,
        purge_store=catalog.purge,
        blobs=blobs,
    )
    result = executor.execute(
        owner_id=owner_id,
        tombstone_id=scheduled.tombstone.tombstone_id,
        now=_NOW,
        retry_at=_NOW + timedelta(minutes=1),
    )

    assert scheduled.metadata_rows_soft_deleted == 2
    assert result.state is PurgeAttemptState.PURGED
    assert blobs.is_owner_absent(owner_id=owner_id)
    blobs.close()
