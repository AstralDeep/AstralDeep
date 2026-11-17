"""Tests for src/astralplane/blob_store.py: cross-process owner locking, atomic
publish/replace/purge, digest-verified bounded readers, and parser lease lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
from typing import Any

import pytest

import astralplane
import astralplane.blob_store as blob_module
from astralplane.blob_store import (
    BlobIntegrityError,
    BlobSizeLimitError,
    ExplicitRootStreamingBlobStore,
    StreamingBlobStore,
    _AtomicWriteSession,
    _cancel_safe_in_executor,
    _cancel_safe_to_thread,
    _create_blob_publish_authority,
    _create_blob_purge_authority,
    _DirectoryAnchor,
)
from astralplane.errors import PlaneError, SQLContractError


@pytest.fixture
def blob_root(tmp_path: Path) -> Path:
    root = (tmp_path / "durable-blobs").resolve()
    root.mkdir()
    return root


@pytest.fixture
def blobs(blob_root: Path) -> ExplicitRootStreamingBlobStore:
    return ExplicitRootStreamingBlobStore(blob_root, io_chunk_bytes=4)


def read_all(
    blobs: ExplicitRootStreamingBlobStore,
    *,
    owner_id: str,
    key: str,
    max_bytes: int = 1024,
) -> bytes:
    return b"".join(blobs.iter_chunks(owner_id=owner_id, key=key, max_bytes=max_bytes))


_TEST_STAGING_IDS = count(1)


def _test_publish_authority(
    *,
    owner_id: str,
    key: str,
    max_bytes: int,
):
    return _create_blob_publish_authority(
        owner_id=owner_id,
        storage_key=key,
        max_bytes=max_bytes,
        lease_id=f"test-staging-{next(_TEST_STAGING_IDS)}",
    )


def _publish_chunks_for_test(
    blobs: ExplicitRootStreamingBlobStore,
    *,
    owner_id: str,
    key: str,
    chunks: Iterable[bytes],
    max_bytes: int,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
):
    authority = _test_publish_authority(
        owner_id=owner_id,
        key=key,
        max_bytes=max_bytes,
    )
    reservation = blobs.reserve_materialization_staging(owner_id=owner_id)
    session = blobs._begin_staged_materialization(
        authority=authority,
        reservation=reservation,
    )
    try:
        staged = session.write_chunks(chunks)
    except BaseException:
        session.abort()
        raise
    try:
        evidence = staged.evidence
        if expected_size_bytes is not None and evidence.size_bytes != expected_size_bytes:
            raise BlobIntegrityError("blob size does not match expected_size_bytes")
        if expected_sha256 is not None and evidence.sha256 != expected_sha256:
            raise BlobIntegrityError("blob digest does not match expected_sha256")
        return blobs._publish_staged_materialization(staged, authority=authority)
    except BaseException:
        staged.abort()
        raise


async def _apublish_chunks_for_test(
    blobs: ExplicitRootStreamingBlobStore,
    *,
    owner_id: str,
    key: str,
    chunks: AsyncIterable[bytes],
    max_bytes: int,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
):
    authority = _test_publish_authority(
        owner_id=owner_id,
        key=key,
        max_bytes=max_bytes,
    )
    reservation = await blobs.areserve_materialization_staging(owner_id=owner_id)
    try:
        session = await _cancel_safe_in_executor(
            blobs._control_io_executor,
            blobs._begin_staged_materialization,
            authority=authority,
            reservation=reservation,
            cleanup_on_cancel=lambda value: value.abort(),
        )
    except BaseException:
        reservation.release()
        raise
    try:
        staged = await session.awrite_chunks(chunks)
    except BaseException:
        await session.aabort()
        raise
    try:
        evidence = staged.evidence
        if expected_size_bytes is not None and evidence.size_bytes != expected_size_bytes:
            raise BlobIntegrityError("blob size does not match expected_size_bytes")
        if expected_sha256 is not None and evidence.sha256 != expected_sha256:
            raise BlobIntegrityError("blob digest does not match expected_sha256")
        return blobs._publish_staged_materialization(staged, authority=authority)
    except BaseException:
        await blobs._run_stage_io(staged.abort)
        raise


def _purge_prefix_for_test(
    blobs: ExplicitRootStreamingBlobStore,
    *,
    owner_id: str,
    prefix: str,
):
    return blobs._delete_for_purge(
        _create_blob_purge_authority(
            owner_id=owner_id,
            target_scope="attachment_prefix",
            storage_key=prefix,
        )
    )


def _purge_owner_for_test(
    blobs: ExplicitRootStreamingBlobStore,
    *,
    owner_id: str,
):
    return blobs._delete_for_purge(
        _create_blob_purge_authority(
            owner_id=owner_id,
            target_scope="owner_namespace",
            storage_key="owner-namespace",
        )
    )


def _seed_blob_fixture(
    blob_root: Path,
    *,
    owner_id: str,
    key: str,
    payload: bytes,
) -> None:
    target = blob_root.joinpath(owner_id, *key.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


async def wait_for_thread_event(event: threading.Event, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() >= deadline:
            pytest.fail("background blob operation did not reach the expected boundary")
        await asyncio.sleep(0.01)


def test_factory_exports_path_independent_contract(blob_root: Path) -> None:
    store = astralplane.create_streaming_blob_store(root=blob_root, io_chunk_bytes=4)

    assert isinstance(store, StreamingBlobStore)
    assert isinstance(store, ExplicitRootStreamingBlobStore)
    assert not hasattr(store, "root")
    assert astralplane.BlobWriteResult.__module__ == "astralplane.blob_store"
    assert astralplane.BlobReadStream.__module__ == "astralplane.blob_store"


def test_root_identity_digest_is_stable_opaque_and_tracks_relocation(
    blob_root: Path,
) -> None:
    first = ExplicitRootStreamingBlobStore(blob_root)
    digest = first.root_identity_digest
    assert len(digest) == 64 and digest == digest.lower()
    assert str(blob_root) not in digest
    repeat = ExplicitRootStreamingBlobStore(blob_root)
    assert repeat.root_identity_digest == digest
    repeat.close()
    first.close()
    with pytest.raises(PlaneError) as closed:
        _ = first.root_identity_digest
    assert closed.value.code == "blob_store_closed"

    second = ExplicitRootStreamingBlobStore(blob_root)
    relocated = blob_root.with_name("relocated-blobs")
    blob_root.rename(relocated)
    with pytest.raises(PlaneError) as moved:
        _ = second.root_identity_digest
    assert moved.value.code == "blob_path_unsafe"
    second.close()

    third = ExplicitRootStreamingBlobStore(relocated)
    assert third.root_identity_digest != digest
    replacement = relocated.with_name("replacement-blobs")
    displaced = relocated.with_name("displaced-blobs")
    replacement.mkdir()
    relocated.rename(displaced)
    replacement.rename(relocated)
    with pytest.raises(PlaneError) as replaced:
        _ = third.root_identity_digest
    assert replaced.value.code == "blob_path_unsafe"
    third.close()


def test_factory_securely_provisions_missing_absolute_root(tmp_path: Path) -> None:
    root = (tmp_path / "new" / "durable" / "blobs").resolve()

    store = astralplane.create_streaming_blob_store(root=root, io_chunk_bytes=4)

    assert root.is_dir()
    assert isinstance(store, StreamingBlobStore)
    _publish_chunks_for_test(
        store,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    assert (
        b"".join(
            store.iter_chunks(
                owner_id="owner-1",
                key="attachment-1/file.bin",
                max_bytes=4,
            )
        )
        == b"data"
    )


def test_windows_api_handles_bounded_storage_paths_beyond_legacy_max_path(
    blob_root: Path,
) -> None:
    store = ExplicitRootStreamingBlobStore(blob_root, io_chunk_bytes=4)
    owner_id = "owner-" + "a" * 58
    key = f"{'b' * 100}/{'c' * 100}/fixture.bin"
    assert len(os.fspath(blob_root / owner_id / Path(key))) > 260
    authority = _create_blob_publish_authority(
        owner_id=owner_id,
        storage_key=key,
        max_bytes=4,
        lease_id="long-path-lease",
    )

    try:
        reservation = store.reserve_materialization_staging(owner_id=owner_id)
        session = store._begin_staged_materialization(
            authority=authority,
            reservation=reservation,
        )
        staged = session.write_chunks((b"data",))
        stored = store._publish_staged_materialization(staged, authority=authority)
        assert stored.size_bytes == 4
        assert read_all(store, owner_id=owner_id, key=key, max_bytes=4) == b"data"
        with (
            store.open_parser_lease(
                owner_id=owner_id,
                key=key,
                max_bytes=4,
                expected_size_bytes=4,
                expected_sha256=hashlib.sha256(b"data").hexdigest(),
            ) as capability,
            open(capability, "rb") as parser_input,
        ):
            assert parser_input.read() == b"data"

        deleted = _purge_prefix_for_test(
            store,
            owner_id=owner_id,
            prefix=key.split("/", 1)[0],
        )
        assert deleted.absent_verified is True

        abort_authority = _create_blob_publish_authority(
            owner_id=owner_id,
            storage_key=key,
            max_bytes=4,
            lease_id="long-path-abort",
        )
        abort_reservation = store.reserve_materialization_staging(owner_id=owner_id)
        abort_session = store._begin_staged_materialization(
            authority=abort_authority,
            reservation=abort_reservation,
        )
        abort_session.write_chunks((b"data",)).abort()
        assert store.is_prefix_absent(
            owner_id=owner_id,
            prefix=key.split("/", 1)[0],
        )

        _publish_chunks_for_test(
            store,
            owner_id=owner_id,
            key=key,
            chunks=(b"data",),
            max_bytes=4,
        )
        owner_deleted = _purge_owner_for_test(store, owner_id=owner_id)
        assert owner_deleted.absent_verified is True
    finally:
        store.close()


def test_cross_process_owner_reservation_releases_after_forced_exit(
    blob_root: Path,
    tmp_path: Path,
) -> None:
    ready = tmp_path / "child-ready"
    source_root = Path(__file__).parents[1] / "src"
    child_code = "\n".join(
        (
            "import time",
            "from pathlib import Path",
            "from astralplane.blob_store import ExplicitRootStreamingBlobStore",
            f"store = ExplicitRootStreamingBlobStore({str(blob_root)!r})",
            "reservation = store.reserve_materialization_staging(owner_id='owner-1')",
            f"Path({str(ready)!r}).write_text('ready', encoding='ascii')",
            "while True: time.sleep(1)",
        )
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(source_root), environment.get("PYTHONPATH", "")))
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if child.poll() is not None:
        pytest.fail(f"owner-lock child failed: {child.stderr.read()}")
    assert ready.exists()

    second = ExplicitRootStreamingBlobStore(blob_root)
    acquired = threading.Event()
    reservations: list[Any] = []
    errors: list[BaseException] = []

    def reserve_after_child() -> None:
        try:
            reservations.append(second.reserve_materialization_staging(owner_id="owner-1"))
            acquired.set()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    waiter = threading.Thread(target=reserve_after_child)
    waiter.start()
    try:
        time.sleep(0.15)
        assert not acquired.is_set()
        child.terminate()
        child.wait(timeout=10)
        waiter.join(timeout=10)
        assert not waiter.is_alive()
        assert errors == []
        assert acquired.is_set()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for reservation in reservations:
            reservation.release()


def test_cross_process_lock_registry_reparse_objects_fail_closed(
    blob_root: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-locks"
    outside.mkdir()
    registry = blob_root / ".astralplane-owner-locks"
    try:
        os.symlink(outside, registry, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    store = ExplicitRootStreamingBlobStore(blob_root)
    with pytest.raises(PlaneError) as raised:
        store.reserve_materialization_staging(owner_id="owner-1")
    assert raised.value.code == "blob_path_unsafe"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("scope", ["prefix", "owner"])
def test_owner_reservation_blocks_destructive_operations_across_store_instances(
    blob_root: Path,
    scope: str,
) -> None:
    first = ExplicitRootStreamingBlobStore(blob_root)
    second = ExplicitRootStreamingBlobStore(blob_root)
    reservation = first.reserve_materialization_staging(owner_id="owner-1")
    completed = threading.Event()
    errors: list[BaseException] = []

    def delete() -> None:
        try:
            if scope == "prefix":
                _purge_prefix_for_test(second, owner_id="owner-1", prefix="attachment-1")
            else:
                _purge_owner_for_test(second, owner_id="owner-1")
            completed.set()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    worker = threading.Thread(target=delete)
    worker.start()
    try:
        time.sleep(0.15)
        assert not completed.is_set()
        reservation.release()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == []
        assert completed.is_set()
    finally:
        reservation.release()


def test_contended_owner_never_holds_global_lifecycle_admission(
    blob_root: Path,
) -> None:
    first = ExplicitRootStreamingBlobStore(blob_root)
    second = ExplicitRootStreamingBlobStore(blob_root)
    held = first.reserve_materialization_staging(owner_id="owner-a")
    owner_a_finished = threading.Event()
    owner_b_finished = threading.Event()
    failures: list[BaseException] = []

    def wait_for_owner_a() -> None:
        try:
            second.is_owner_absent(owner_id="owner-a")
            owner_a_finished.set()
        except BaseException as exc:  # pragma: no cover
            failures.append(exc)

    def reserve_owner_b() -> None:
        try:
            reservation = second.reserve_materialization_staging(owner_id="owner-b")
            reservation.release()
            owner_b_finished.set()
        except BaseException as exc:  # pragma: no cover
            failures.append(exc)

    blocked = threading.Thread(target=wait_for_owner_a)
    unrelated = threading.Thread(target=reserve_owner_b)
    blocked.start()
    try:
        deadline = time.monotonic() + 2
        while second._pending_owner_acquisitions != 1:
            if time.monotonic() >= deadline:
                pytest.fail("contended owner acquisition did not enter its pending state")
            time.sleep(0.01)
        unrelated.start()
        assert owner_b_finished.wait(timeout=1)
        assert not owner_a_finished.is_set()
        with pytest.raises(PlaneError) as busy:
            second.close()
        assert busy.value.code == "blob_store_busy"
    finally:
        held.release()
    blocked.join(timeout=2)
    unrelated.join(timeout=2)
    assert not blocked.is_alive()
    assert not unrelated.is_alive()
    assert failures == []
    assert owner_a_finished.is_set()
    second.close()
    first.close()


@pytest.mark.asyncio
async def test_async_owner_reservation_cancellation_releases_eventual_os_lock(
    blob_root: Path,
) -> None:
    first = ExplicitRootStreamingBlobStore(blob_root)
    second = ExplicitRootStreamingBlobStore(blob_root)
    held = first.reserve_materialization_staging(owner_id="owner-1")
    waiting = asyncio.create_task(second.areserve_materialization_staging(owner_id="owner-1"))
    await asyncio.sleep(0.05)
    waiting.cancel()
    waiting.cancel()
    held.release()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    follow_up = second.reserve_materialization_staging(owner_id="owner-1")
    follow_up.release()
    assert second._owner_locks._entries == {}


@pytest.mark.asyncio
async def test_async_contended_reservation_keeps_close_admission_between_polls(
    blob_root: Path,
) -> None:
    first = ExplicitRootStreamingBlobStore(blob_root)
    second = ExplicitRootStreamingBlobStore(blob_root)
    held = first.reserve_materialization_staging(owner_id="owner-1")
    waiting = asyncio.create_task(
        second.areserve_materialization_staging(owner_id="owner-1")
    )
    try:
        deadline = time.monotonic() + 2
        while second._pending_owner_acquisitions != 1:
            if time.monotonic() >= deadline:
                pytest.fail("async owner acquisition did not retain lifecycle admission")
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.03)
        assert second._pending_owner_acquisitions == 1
        with pytest.raises(PlaneError) as busy:
            second.close()
        assert busy.value.code == "blob_store_busy"
    finally:
        held.release()
    reservation = await asyncio.wait_for(waiting, timeout=2)
    reservation.release()
    second.close()
    first.close()


def test_sync_write_is_bounded_atomic_digest_verified_and_owner_scoped(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    payload = b"abcdefghij"
    digest = hashlib.sha256(payload).hexdigest()

    result = _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"", payload),
        max_bytes=len(payload),
        expected_size_bytes=len(payload),
        expected_sha256=digest,
    )
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-2",
        key="attachment-1/file.bin",
        chunks=(b"other",),
        max_bytes=5,
    )

    assert result.storage_key == "attachment-1/file.bin"
    assert result.size_bytes == len(payload)
    assert result.sha256 == digest
    assert read_all(blobs, owner_id="owner-1", key=result.storage_key) == payload
    assert read_all(blobs, owner_id="owner-2", key=result.storage_key) == b"other"


@pytest.mark.parametrize(
    ("stored_owner", "stored_key", "alias_owner", "alias_key"),
    (
        ("Owner-1", "attachment-1/file.bin", "owner-1", "attachment-1/file.bin"),
        (
            "__verif__Run_owner",
            "attachment-1/file.bin",
            "__verif__run_owner",
            "attachment-1/file.bin",
        ),
        ("owner-1", "Attachment-1/file.bin", "owner-1", "attachment-1/file.bin"),
        ("owner-1", "attachment-1/File.bin", "owner-1", "attachment-1/file.bin"),
    ),
)
def test_case_folded_owner_and_key_aliases_never_cross_identity(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    stored_owner: str,
    stored_key: str,
    alias_owner: str,
    alias_key: str,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id=stored_owner,
        key=stored_key,
        chunks=(b"secret",),
        max_bytes=6,
    )

    with pytest.raises(PlaneError) as denied:
        tuple(
            blobs.iter_chunks(
                owner_id=alias_owner,
                key=alias_key,
                max_bytes=6,
            )
        )
    assert denied.value.code in {"blob_not_found", "blob_path_unsafe"}

    alias_resolves_to_stored_entry = blob_root.joinpath(
        alias_owner,
        *alias_key.split("/"),
    ).exists()
    if alias_resolves_to_stored_entry:
        with pytest.raises(PlaneError) as collision:
            _publish_chunks_for_test(
                blobs,
                owner_id=alias_owner,
                key=alias_key,
                chunks=(b"overwrite",),
                max_bytes=9,
            )
        assert collision.value.code == "blob_path_unsafe"

    assert (
        read_all(
            blobs,
            owner_id=stored_owner,
            key=stored_key,
            max_bytes=6,
        )
        == b"secret"
    )


@pytest.mark.parametrize(
    "key",
    (
        "",
        ".",
        "..",
        "../escape",
        "nested/../escape",
        "nested/./file",
        "nested//file",
        "/absolute",
        "\\absolute",
        "C:\\absolute",
        "alternate:stream",
        "attachment/CON.txt",
        "attachment/trailing. ",
        "attachment/wild?.bin",
        "nul\x00byte",
    ),
)
def test_key_validation_denies_absolute_dot_separator_and_platform_escapes(
    blobs: ExplicitRootStreamingBlobStore,
    key: str,
) -> None:
    with pytest.raises(SQLContractError):
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key=key,
            chunks=(b"data",),
            max_bytes=4,
        )


@pytest.mark.parametrize("owner_id", ("", ".", "../owner", "owner/path", "x" * 256))
def test_owner_validation_fails_before_storage_access(
    blobs: ExplicitRootStreamingBlobStore,
    owner_id: str,
) -> None:
    with pytest.raises(SQLContractError):
        blobs.is_owner_absent(owner_id=owner_id)


def test_reserved_verification_owner_can_write_read_and_purge_exact_namespace(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    owner_id = "__verif__20260814T010203Z_everyday_primary"
    key = "attachment-1/fixture.csv"
    payload = b"id,value\n1,2\n"

    async def chunks() -> AsyncIterator[bytes]:
        yield payload

    result = asyncio.run(
        _apublish_chunks_for_test(
            blobs,
            owner_id=owner_id,
            key=key,
            chunks=chunks(),
            max_bytes=len(payload),
        )
    )

    assert result.storage_key == key
    assert read_all(blobs, owner_id=owner_id, key=key) == payload
    deleted = _purge_owner_for_test(blobs, owner_id=owner_id)
    assert deleted.deleted_files == 2
    assert deleted.absent_verified is True
    assert blobs.is_owner_absent(owner_id=owner_id)


@pytest.mark.parametrize(
    "owner_id",
    (
        "_owner",
        "__owner",
        "___verif__run",
        "__Verif__run",
        "__verif__",
        "__verif__-run",
        "__verif__.run",
        "__verif__run/escape",
        "__verif__run\\escape",
        "__verif__run@domain",
        "__verif__" + "x" * 247,
    ),
)
def test_owner_validation_allows_only_canonical_reserved_verification_namespace(
    blobs: ExplicitRootStreamingBlobStore,
    owner_id: str,
) -> None:
    with pytest.raises(SQLContractError):
        blobs.is_owner_absent(owner_id=owner_id)


def test_failed_replacement_preserves_published_bytes_and_removes_temporary(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    _seed_blob_fixture(
        blob_root,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        payload=b"old",
    )

    with pytest.raises(BlobSizeLimitError) as raised:
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=(b"new-data",),
            max_bytes=3,
        )

    assert raised.value.code == "blob_size_limit_exceeded"
    assert read_all(blobs, owner_id="owner-1", key="attachment-1/file.bin") == b"old"
    assert list(blob_root.rglob(".astralplane-*.tmp")) == []


@pytest.mark.parametrize(
    "expected_size,expected_digest",
    ((4, None), (None, "0" * 64)),
)
def test_integrity_fence_failure_does_not_publish(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    expected_size: int | None,
    expected_digest: str | None,
) -> None:
    with pytest.raises(BlobIntegrityError) as raised:
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=(b"bad",),
            max_bytes=4,
            expected_size_bytes=expected_size,
            expected_sha256=expected_digest,
        )

    assert raised.value.code == "blob_integrity_mismatch"
    assert not (blob_root / "owner-1" / "attachment-1" / "file.bin").exists()
    assert list(blob_root.rglob(".astralplane-*.tmp")) == []


def test_post_flush_publish_failure_aborts_once_and_releases_resources(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(
        _anchor: _DirectoryAnchor,
        _source: str,
        _target: str,
    ) -> None:
        raise RuntimeError("synthetic post-flush publish failure")

    monkeypatch.setattr(_DirectoryAnchor, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="post-flush publish failure"):
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=(b"data",),
            max_bytes=4,
        )

    assert list(blob_root.rglob(".astralplane-*.tmp")) == []
    assert not (blob_root / "owner-1" / "attachment-1" / "file.bin").exists()
    assert blobs._owner_locks._entries == {}


def test_temporary_name_substitution_never_leaves_substitute_published(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = _DirectoryAnchor.replace
    parked: Path | None = None

    def substitute_before_replace(
        anchor: _DirectoryAnchor,
        source: str,
        target: str,
    ) -> None:
        nonlocal parked
        parked = anchor.path / f"{source}.parked"
        os.replace(anchor.path / source, parked)
        (anchor.path / source).write_bytes(b"other")
        original_replace(anchor, source, target)

    monkeypatch.setattr(_DirectoryAnchor, "replace", substitute_before_replace)
    try:
        with pytest.raises(BlobIntegrityError, match="identity changed"):
            _publish_chunks_for_test(
                blobs,
                owner_id="owner-1",
                key="attachment-1/file.bin",
                chunks=(b"valid",),
                max_bytes=5,
            )

        assert not (blob_root / "owner-1" / "attachment-1" / "file.bin").exists()
        assert blobs._owner_locks._entries == {}
    finally:
        if parked is not None and parked.exists():
            parked.unlink()


def test_cleanup_failure_is_visible_and_never_relabelled_as_success(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_unlink = _DirectoryAnchor.unlink

    def fail_temporary(anchor: _DirectoryAnchor, name: str) -> None:
        if name.startswith(".astralplane-"):
            raise PermissionError("synthetic cleanup denial")
        original_unlink(anchor, name)

    def broken_chunks() -> Iterator[bytes]:
        yield b"partial"
        raise RuntimeError("source failed")

    monkeypatch.setattr(_DirectoryAnchor, "unlink", fail_temporary)
    with pytest.raises(PlaneError) as raised:
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=broken_chunks(),
            max_bytes=100,
        )
    assert raised.value.code == "blob_cleanup_failed"


def test_abort_holds_owner_exclusion_through_empty_directory_pruning(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prune_entered = threading.Event()
    continue_prune = threading.Event()
    replacement_started = threading.Event()
    replacement_finished = threading.Event()
    first_errors: list[BaseException] = []
    replacement_errors: list[BaseException] = []
    original_prune = blobs._prune_empty_parents

    def paused_prune(directory: Path) -> None:
        prune_entered.set()
        if not continue_prune.wait(timeout=2):
            raise RuntimeError("test did not release pruning")
        original_prune(directory)

    def broken_chunks() -> Iterator[bytes]:
        yield b"partial"
        raise RuntimeError("source failed")

    def fail_first_write() -> None:
        try:
            _publish_chunks_for_test(
                blobs,
                owner_id="owner-1",
                key="attachment-1/failed.bin",
                chunks=broken_chunks(),
                max_bytes=100,
            )
        except BaseException as exc:
            first_errors.append(exc)

    def replace_after_abort() -> None:
        replacement_started.set()
        try:
            _publish_chunks_for_test(
                blobs,
                owner_id="owner-1",
                key="attachment-1/final.bin",
                chunks=(b"final",),
                max_bytes=5,
            )
        except BaseException as exc:
            replacement_errors.append(exc)
        finally:
            replacement_finished.set()

    monkeypatch.setattr(blobs, "_prune_empty_parents", paused_prune)
    first = threading.Thread(target=fail_first_write)
    replacement = threading.Thread(target=replace_after_abort)
    first.start()
    try:
        assert prune_entered.wait(timeout=2)
        replacement.start()
        assert replacement_started.wait(timeout=2)
        assert not replacement_finished.wait(timeout=0.1)
    finally:
        continue_prune.set()
    first.join(timeout=2)
    replacement.join(timeout=2)

    assert not first.is_alive()
    assert not replacement.is_alive()
    assert len(first_errors) == 1
    assert isinstance(first_errors[0], RuntimeError)
    assert str(first_errors[0]) == "source failed"
    assert replacement_errors == []
    assert read_all(blobs, owner_id="owner-1", key="attachment-1/final.bin") == b"final"
    assert blobs._owner_locks._entries == {}


def test_reader_is_bounded_pathless_and_verifies_complete_digest(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    payload = b"abcdefgh"
    digest = hashlib.sha256(payload).hexdigest()
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(payload,),
        max_bytes=len(payload),
    )

    reader = blobs.open_reader(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=len(payload),
        expected_size_bytes=len(payload),
        expected_sha256=digest,
    )
    assert not hasattr(reader, "name")
    with reader:
        assert reader.read() == b"abcd"
        assert reader.read(-1) == b"efgh"
        assert reader.read() == b""
    assert reader.closed
    reader.close()
    with pytest.raises(PlaneError) as reenter:
        reader.__enter__()
    assert reenter.value.code == "blob_reader_closed"
    with pytest.raises(PlaneError) as raised:
        reader.read(1)
    assert raised.value.code == "blob_reader_closed"

    with (
        pytest.raises(SQLContractError, match="size"),
        blobs.open_reader(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=len(payload),
        ) as bounded,
    ):
        bounded.read(5)


def test_reader_reports_missing_oversize_and_persisted_integrity_mismatch(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    with pytest.raises(PlaneError) as missing:
        blobs.open_reader(
            owner_id="owner-1",
            key="attachment-1/missing.bin",
            max_bytes=10,
        )
    assert missing.value.code == "blob_not_found"

    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"payload",),
        max_bytes=7,
    )
    with pytest.raises(BlobSizeLimitError):
        blobs.open_reader(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=6,
        )
    with pytest.raises(BlobIntegrityError):
        tuple(
            blobs.iter_chunks(
                owner_id="owner-1",
                key="attachment-1/file.bin",
                max_bytes=7,
                expected_sha256="0" * 64,
            )
        )


def test_reader_exact_size_close_still_verifies_expected_digest(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    reader = blobs.open_reader(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=4,
        expected_size_bytes=4,
        expected_sha256="0" * 64,
    )

    with pytest.raises(BlobIntegrityError, match="digest"), reader:
        reader.read(4)

    assert reader.closed


def test_reader_fails_closed_when_file_is_truncated_after_open(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    reader = blobs.open_reader(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=4,
    )
    (blob_root / "owner-1" / "attachment-1" / "file.bin").write_bytes(b"x")

    with reader, pytest.raises(BlobIntegrityError, match="changed"):
        tuple(reader.iter_chunks())


def test_reader_growth_never_emits_past_open_snapshot_or_declared_maximum(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    reader = blobs.open_reader(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=4,
    )
    with (blob_root / "owner-1" / "attachment-1" / "file.bin").open("ab") as stream:
        stream.write(b"overflow")

    emitted = bytearray()
    with pytest.raises(BlobIntegrityError, match="changed"), reader:
        while chunk := reader.read():
            emitted.extend(chunk)

    assert len(emitted) <= 4
    assert b"data".startswith(emitted)


def test_parser_lease_is_narrow_no_copy_and_repeat_validated(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    payload = b"abcd" * (2 * 1024 * 1024)
    result = _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/large.bin",
        chunks=(payload,),
        max_bytes=len(payload),
    )
    expected_path = blob_root / "owner-1" / result.storage_key

    with blobs.open_parser_lease(
        owner_id="owner-1",
        key=result.storage_key,
        max_bytes=len(payload),
    ) as capability:
        assert isinstance(capability, os.PathLike)
        capability_path = Path(os.fspath(capability))
        if os.name == "nt":
            assert capability_path == expected_path
        else:
            assert capability_path.parent in {Path("/proc/self/fd"), Path("/dev/fd")}
            assert os.path.samefile(capability_path, expected_path)
        assert not hasattr(capability, "open")
        assert not hasattr(capability, "write_bytes")
        assert not hasattr(capability, "unlink")
        assert not hasattr(capability, "parent")
        assert os.path.getsize(capability) == len(payload)
        assert list(blob_root.rglob(".astralplane-*.tmp")) == []
    with pytest.raises(PlaneError) as closed:
        os.fspath(capability)
    assert closed.value.code == "blob_lease_closed"

    lease = blobs.open_parser_lease(
        owner_id="owner-1",
        key=result.storage_key,
        max_bytes=len(payload),
    )
    with lease:
        pass
    with pytest.raises(PlaneError) as reused:
        lease.__enter__()
    assert reused.value.code == "blob_lease_closed"


def test_parser_lease_expected_size_mismatch_releases_owner_lock(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )

    with pytest.raises(BlobIntegrityError, match="expected_size_bytes"):
        blobs.open_parser_lease(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=5,
            expected_size_bytes=5,
        )

    assert blobs._owner_locks._entries == {}


def test_parser_lease_expected_digest_mismatch_releases_owner_lock(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )

    with pytest.raises(BlobIntegrityError, match="expected_sha256"):
        blobs.open_parser_lease(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=4,
            expected_sha256="0" * 64,
        )

    assert blobs._owner_locks._entries == {}


def test_parser_lease_matching_digest_is_checked_on_the_held_descriptor(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"descriptor-bound-data"
    digest = hashlib.sha256(payload).hexdigest()
    verified_descriptors: list[int] = []
    original_verify = blobs._verify_descriptor_digest

    def observed_verify(
        stream: Any,
        *,
        size_bytes: int,
        expected_sha256: str | None,
    ) -> None:
        descriptor = stream.fileno()
        assert not stream.closed
        assert os.fstat(descriptor).st_size == len(payload)
        original_verify(
            stream,
            size_bytes=size_bytes,
            expected_sha256=expected_sha256,
        )
        assert stream.tell() == 0
        verified_descriptors.append(descriptor)

    monkeypatch.setattr(blobs, "_verify_descriptor_digest", observed_verify)
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(payload,),
        max_bytes=len(payload),
    )

    with (
        blobs.open_parser_lease(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=len(payload),
            expected_size_bytes=len(payload),
            expected_sha256=digest,
        ) as capability,
        open(capability, "rb") as parser_input,
    ):
        assert parser_input.read() == payload

    assert len(verified_descriptors) == 1
    assert blobs._owner_locks._entries == {}


def test_parser_lease_capability_is_descriptor_bound_or_path_delete_locked(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    payload = b"stable-parser-input"
    result = _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(payload,),
        max_bytes=len(payload),
    )
    target = blob_root / "owner-1" / result.storage_key
    parked = target.with_suffix(".parked")
    lease = blobs.open_parser_lease(
        owner_id="owner-1",
        key=result.storage_key,
        max_bytes=len(payload),
    )

    if os.name == "nt":
        with lease as capability:
            assert Path(os.fspath(capability)) == target
            with pytest.raises(PermissionError):
                os.replace(target, parked)
            with open(capability, "rb") as parser_input:
                assert parser_input.read() == payload
        os.replace(target, parked)
        os.replace(parked, target)
    else:
        try:
            with (
                pytest.raises(BlobIntegrityError, match="identity changed"),
                lease as capability,
            ):
                capability_path = Path(os.fspath(capability))
                assert capability_path.parent in {
                    Path("/proc/self/fd"),
                    Path("/dev/fd"),
                }
                assert os.path.samefile(capability_path, target)
                os.replace(target, parked)
                assert capability_path.read_bytes() == payload
        finally:
            if parked.exists():
                os.replace(parked, target)

    assert target.read_bytes() == payload
    assert blobs._owner_locks._entries == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-mode contract")
def test_windows_parser_lease_denies_in_place_write_access(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    payload = b"stable-parser-input"
    result = _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(payload,),
        max_bytes=len(payload),
    )
    target = blob_root / "owner-1" / result.storage_key

    with blobs.open_parser_lease(
        owner_id="owner-1",
        key=result.storage_key,
        max_bytes=len(payload),
        expected_size_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    ) as capability:
        assert Path(os.fspath(capability)) == target
        with pytest.raises(PermissionError), target.open("r+b") as writer:
            writer.write(b"changed-parser-data")
        assert Path(os.fspath(capability)).read_bytes() == payload

    assert target.read_bytes() == payload
    assert blobs._owner_locks._entries == {}


def test_parser_lease_excludes_same_owner_mutation_until_exit(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"before",),
        max_bytes=6,
    )
    started = threading.Event()
    finished = threading.Event()

    def replace() -> None:
        started.set()
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-2/after.bin",
            chunks=(b"after",),
            max_bytes=5,
        )
        finished.set()

    with blobs.open_parser_lease(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=6,
    ):
        worker = threading.Thread(target=replace)
        worker.start()
        assert started.wait(timeout=1)
        time.sleep(0.05)
        assert not finished.is_set()

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert finished.is_set()
    assert read_all(blobs, owner_id="owner-1", key="attachment-1/file.bin") == b"before"
    assert read_all(blobs, owner_id="owner-1", key="attachment-2/after.bin") == b"after"


def test_parser_lease_fails_closed_on_external_replace_race(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"before",),
        max_bytes=6,
    )
    target = blob_root / "owner-1" / "attachment-1" / "file.bin"
    replacement = target.with_suffix(".replacement")

    lease = blobs.open_parser_lease(owner_id="owner-1", key="attachment-1/file.bin", max_bytes=6)
    if os.name == "nt":
        replacement.write_bytes(b"after!")
        with lease:
            with pytest.raises(PermissionError):
                os.replace(replacement, target)
            with pytest.raises(PermissionError):
                target.write_bytes(b"after!")
        replacement.unlink()
        assert target.read_bytes() == b"before"
    else:
        with pytest.raises(BlobIntegrityError), lease:
            replacement.write_bytes(b"after!")
            os.replace(replacement, target)


def test_parser_lease_reports_missing_oversize_and_reparse_boundary(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(PlaneError) as missing:
        blobs.open_parser_lease(
            owner_id="owner-1",
            key="attachment-1/missing.bin",
            max_bytes=10,
        )
    assert missing.value.code == "blob_not_found"

    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    with pytest.raises(BlobSizeLimitError):
        blobs.open_parser_lease(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=3,
        )

    outside = tmp_path / "outside-parser.bin"
    outside.write_bytes(b"data")
    link = blob_root / "owner-1" / "attachment-2"
    try:
        os.symlink(outside.parent, link, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(PlaneError) as unsafe:
        blobs.open_parser_lease(
            owner_id="owner-1",
            key="attachment-2/outside-parser.bin",
            max_bytes=4,
        )
    assert unsafe.value.code == "blob_path_unsafe"


@pytest.mark.asyncio
async def test_async_write_and_read_keep_disk_work_off_event_loop_and_bound_chunks(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_thread = threading.get_ident()
    write_threads: list[int] = []
    original_write = _AtomicWriteSession.write

    def observed_write(session: _AtomicWriteSession, chunk: memoryview) -> None:
        write_threads.append(threading.get_ident())
        original_write(session, chunk)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"0123456789"

    monkeypatch.setattr(_AtomicWriteSession, "write", observed_write)
    result = await _apublish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=chunks(),
        max_bytes=10,
    )
    observed = b"".join(
        [
            chunk
            async for chunk in blobs.aiter_chunks(
                owner_id="owner-1",
                key=result.storage_key,
                max_bytes=10,
                chunk_size=3,
            )
        ]
    )

    assert result.size_bytes == 10
    assert observed == b"0123456789"
    assert len(write_threads) == 3
    assert all(identifier != event_thread for identifier in write_threads)


@pytest.mark.asyncio
async def test_async_source_failure_removes_unpublished_bytes(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("synthetic source failure")

    with pytest.raises(RuntimeError, match="source failure"):
        await _apublish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=broken(),
            max_bytes=100,
        )
    residual = [
        path.relative_to(blob_root).as_posix()
        for path in blob_root.rglob("*")
        if ".astralplane-owner-locks" not in path.parts
    ]
    assert residual == []
    assert blobs._owner_locks._entries == {}


@pytest.mark.asyncio
async def test_cancelled_async_begin_releases_owner_lock_and_temporary(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/original.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    entered = threading.Event()
    continue_begin = threading.Event()
    returned = threading.Event()
    sessions: list[_AtomicWriteSession] = []
    original_begin = blobs._begin_authorized_staged_write

    def observed_begin(**kwargs: Any) -> _AtomicWriteSession:
        session = original_begin(**kwargs)
        sessions.append(session)
        entered.set()
        if not continue_begin.wait(timeout=2):
            raise RuntimeError("test did not release the staged constructor")
        returned.set()
        return session

    async def chunks() -> AsyncIterator[bytes]:
        yield b"next"

    monkeypatch.setattr(blobs, "_begin_authorized_staged_write", observed_begin)
    task = asyncio.create_task(
        _apublish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/new.bin",
            chunks=chunks(),
            max_bytes=4,
        )
    )
    await wait_for_thread_event(entered)
    task.cancel()
    continue_begin.set()

    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_for_thread_event(returned)
        assert list(blob_root.rglob(".astralplane-*.tmp")) == []
        assert blobs._owner_locks._entries == {}
        assert blobs.is_absent(owner_id="owner-1", key="attachment-1/new.bin")
    finally:
        continue_begin.set()
        for session in sessions:
            session.abort()


@pytest.mark.asyncio
async def test_async_invalid_chunk_size_releases_any_open_reader(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    readers: list[Any] = []
    original_open = blobs.open_reader

    def observed_open(**kwargs: Any) -> Any:
        reader = original_open(**kwargs)
        readers.append(reader)
        return reader

    monkeypatch.setattr(blobs, "open_reader", observed_open)
    stream = blobs.aiter_chunks(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=4,
        chunk_size=0,
    )
    try:
        with pytest.raises(SQLContractError, match="chunk_size"):
            await anext(stream)
        assert all(reader.closed for reader in readers)
        assert blobs._owner_locks._entries == {}
    finally:
        await stream.aclose()
        for reader in readers:
            reader.close()


def test_exact_prefix_and_owner_deletion_are_scoped_and_absence_verified(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    for owner, key in (
        ("owner-1", "attachment-1/a.bin"),
        ("owner-1", "attachment-1/nested/b.bin"),
        ("owner-1", "attachment-2/c.bin"),
        ("owner-2", "attachment-1/d.bin"),
    ):
        _publish_chunks_for_test(
            blobs,
            owner_id=owner,
            key=key,
            chunks=(key.encode(),),
            max_bytes=100,
        )

    deleted = _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1")

    assert deleted.deleted_files == 4
    assert deleted.deleted_directories == 2
    assert deleted.absent_verified
    assert blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-1")
    assert not blobs.is_prefix_absent(owner_id="owner-1", prefix="attachment-2")
    assert not blobs.is_prefix_absent(owner_id="owner-2", prefix="attachment-1")
    assert (
        _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1").deleted_files == 0
    )

    owner_deleted = _purge_owner_for_test(blobs, owner_id="owner-1")
    assert owner_deleted.deleted_files == 2
    assert owner_deleted.absent_verified
    assert blobs.is_owner_absent(owner_id="owner-1")
    assert not blobs.is_owner_absent(owner_id="owner-2")


def test_owner_deletion_does_not_materialize_a_full_tree_inventory(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(12):
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key=f"attachment-{index % 3}/nested-{index}/file.bin",
            chunks=(b"x",),
            max_bytes=1,
        )

    def forbidden_inventory(_root: Path) -> tuple[list[Path], list[Path]]:
        raise AssertionError("deletion must not accumulate the complete blob tree in memory")

    if hasattr(blobs, "_inventory_tree"):
        monkeypatch.setattr(blobs, "_inventory_tree", forbidden_inventory)

    result = _purge_owner_for_test(blobs, owner_id="owner-1")

    assert result.deleted_files == 24
    assert result.absent_verified
    assert blobs.is_owner_absent(owner_id="owner-1")


def test_authorized_prefix_delete_is_idempotent_and_surfaces_filesystem_failure(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    original_unlink = _DirectoryAnchor.unlink

    def denied(anchor: _DirectoryAnchor, name: str) -> None:
        raise PermissionError("synthetic denial")

    monkeypatch.setattr(_DirectoryAnchor, "unlink", denied)
    with pytest.raises(PlaneError) as raised:
        _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1")
    assert raised.value.code == "blob_delete_failed"

    monkeypatch.setattr(_DirectoryAnchor, "unlink", original_unlink)
    first = _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1")
    replay = _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1")
    assert first.deleted_files == 2
    assert first.absent_verified is True
    assert replay.deleted_files == 0
    assert replay.absent_verified is True


def test_symlink_or_reparse_ancestry_and_tree_entries_fail_closed(
    blob_root: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = blob_root / "owner-1"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    blobs = ExplicitRootStreamingBlobStore(blob_root)
    with pytest.raises(PlaneError) as raised:
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=(b"escape",),
            max_bytes=6,
        )
    assert raised.value.code == "blob_path_unsafe"
    assert list(outside.iterdir()) == []

    link.unlink()
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    os.symlink(outside, blob_root / "owner-1" / "attachment-1" / "unsafe", target_is_directory=True)
    with pytest.raises(PlaneError) as raised:
        _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1")
    assert raised.value.code == "blob_path_unsafe"
    assert read_all(blobs, owner_id="owner-1", key="attachment-1/file.bin") == b"data"


def test_ancestor_swap_after_validation_cannot_redirect_publication(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside-swap"
    outside.mkdir()
    (outside / "attachment-1").mkdir()
    probe = blob_root / "symlink-probe"
    try:
        os.symlink(outside, probe, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    owner = blob_root / "owner-1"
    parked = tmp_path / "parked-owner"
    target_parent = owner / "attachment-1"
    original_check = blobs._check_chain
    attempted = False
    swapped = False
    swap_blocked: OSError | None = None

    def swap_after_check(directory: Path) -> None:
        nonlocal attempted
        nonlocal swapped
        nonlocal swap_blocked
        original_check(directory)
        if not swapped and directory == target_parent and owner.is_dir():
            attempted = True
            try:
                os.replace(owner, parked)
            except OSError as exc:
                swap_blocked = exc
                return
            os.symlink(outside, owner, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(blobs, "_check_chain", swap_after_check)

    operation_error: PlaneError | None = None
    try:
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/escaped.bin",
            chunks=(b"escape",),
            max_bytes=6,
        )
    except PlaneError as exc:
        operation_error = exc
    monkeypatch.setattr(blobs, "_check_chain", original_check)

    assert attempted, "the regression hook must attempt an ancestor substitution"
    assert not (outside / "attachment-1" / "escaped.bin").exists()
    if swap_blocked is None:
        assert swapped
        assert operation_error is not None
        assert operation_error.code == "blob_path_unsafe"
    else:
        assert not swapped
        assert operation_error is None
        assert (
            read_all(
                blobs,
                owner_id="owner-1",
                key="attachment-1/escaped.bin",
                max_bytes=6,
            )
            == b"escape"
        )


def test_constructor_rejects_missing_relative_file_and_link_roots(tmp_path: Path) -> None:
    with pytest.raises(SQLContractError, match="absolute"):
        ExplicitRootStreamingBlobStore(Path("relative"))
    with pytest.raises(PlaneError) as missing:
        ExplicitRootStreamingBlobStore((tmp_path / "missing").resolve())
    assert missing.value.code == "blob_root_unavailable"

    file_root = (tmp_path / "file-root").resolve()
    file_root.write_bytes(b"not a directory")
    with pytest.raises(SQLContractError, match="real directory"):
        ExplicitRootStreamingBlobStore(file_root)

    real_root = (tmp_path / "real-root").resolve()
    real_root.mkdir()
    link_root = (tmp_path / "link-root").resolve()
    try:
        os.symlink(real_root, link_root, target_is_directory=True)
    except OSError:
        return
    with pytest.raises(SQLContractError, match="real directory"):
        ExplicitRootStreamingBlobStore(link_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows local-drive root contract")
@pytest.mark.parametrize(
    "unsafe_root",
    [
        r"\\server\share\blobs",
        r"\\?\C:\durable\blobs",
        r"\\.\PhysicalDrive0",
    ],
)
def test_windows_constructor_rejects_unc_extended_and_device_roots(
    unsafe_root: str,
) -> None:
    with pytest.raises(SQLContractError, match="local drive-rooted"):
        ExplicitRootStreamingBlobStore(unsafe_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy MAX_PATH contract")
def test_windows_long_root_provision_lock_and_constructor_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    successful_parts = ("a" * 90, "b" * 90, "c" * 90)
    successful_directories: list[Path] = []
    successful_root = tmp_path
    for part in successful_parts:
        successful_root /= part
        successful_directories.append(successful_root)
    assert len(os.fspath(successful_root)) > 285

    store = ExplicitRootStreamingBlobStore(successful_root, create_root=True)
    reservation = store.reserve_materialization_staging(owner_id="long-root-owner")
    reservation.release()
    store.close()
    lock_registry = successful_root / ".astralplane-owner-locks"
    lock_file = lock_registry / (hashlib.sha256(b"long-root-owner").hexdigest() + ".lock")
    os.unlink(blob_module._windows_extended_path(lock_file))
    os.rmdir(blob_module._windows_extended_path(lock_registry))
    for directory in reversed(successful_directories):
        os.rmdir(blob_module._windows_extended_path(directory))
    assert not successful_directories[0].exists()

    rollback_parts = ("d" * 90, "e" * 90, "f" * 90)
    rollback_directories: list[Path] = []
    rollback_root = tmp_path
    for part in rollback_parts:
        rollback_root /= part
        rollback_directories.append(rollback_root)
    original_identity = blob_module._metadata_identity
    monkeypatch.setattr(blob_module, "_metadata_identity", lambda _metadata: None)
    with pytest.raises(PlaneError) as failed:
        ExplicitRootStreamingBlobStore(rollback_root, create_root=True)
    assert failed.value.code == "blob_path_unsafe"
    monkeypatch.setattr(blob_module, "_metadata_identity", original_identity)
    assert not rollback_directories[0].exists()


def test_public_parameter_validation_is_bounded(blobs: ExplicitRootStreamingBlobStore) -> None:
    for kwargs in (
        {"max_bytes": 0},
        {"max_bytes": True},
        {"max_bytes": 3, "expected_size_bytes": 4},
        {"max_bytes": 3, "expected_sha256": "BAD"},
    ):
        with pytest.raises(SQLContractError):
            blobs.open_reader(
                owner_id="owner-1",
                key="attachment-1/file.bin",
                **kwargs,  # type: ignore[arg-type]
            )
    with pytest.raises(SQLContractError, match="bytes"):
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=("not bytes",),  # type: ignore[arg-type]
            max_bytes=10,
        )
    with pytest.raises(SQLContractError, match="iterable"):
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key="attachment-1/file.bin",
            chunks=None,  # type: ignore[arg-type]
            max_bytes=10,
        )
    with pytest.raises(SQLContractError, match="io_chunk_bytes"):
        ExplicitRootStreamingBlobStore(blobs._root, io_chunk_bytes=0)
    with pytest.raises(SQLContractError, match="filesystem path"):
        ExplicitRootStreamingBlobStore(object())  # type: ignore[arg-type]
    with pytest.raises(SQLContractError, match="create_root"):
        ExplicitRootStreamingBlobStore(blobs._root, create_root=1)  # type: ignore[arg-type]

    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-2/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    with pytest.raises(BlobIntegrityError, match="size"):
        blobs.open_reader(
            owner_id="owner-1",
            key="attachment-2/file.bin",
            max_bytes=5,
            expected_size_bytes=5,
        )


@pytest.mark.parametrize(
    "key",
    (
        "/".join(["a" * 200] * 32),
        "/".join(["a"] * 33),
    ),
    ids=("total-length", "segment-count"),
)
def test_total_key_length_and_segment_count_are_bounded_before_storage_access(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    key: str,
) -> None:
    with pytest.raises(SQLContractError, match="key"):
        _publish_chunks_for_test(
            blobs,
            owner_id="owner-1",
            key=key,
            chunks=(b"data",),
            max_bytes=4,
        )

    assert list(blob_root.iterdir()) == []


def test_async_parameter_requires_async_iterable(blobs: ExplicitRootStreamingBlobStore) -> None:
    with pytest.raises(SQLContractError, match="async iterable"):
        asyncio.run(
            _apublish_chunks_for_test(
                blobs,
                owner_id="owner-1",
                key="attachment-1/file.bin",
                chunks=(b"data",),  # type: ignore[arg-type]
                max_bytes=4,
            )
        )


def test_owner_lock_token_and_closed_directory_capabilities_are_not_reusable(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    token = blobs._owner_locks.acquire("owner-1")
    token.release()
    token.release()
    assert blobs._owner_locks._entries == {}

    anchor = _DirectoryAnchor(blobs, components=(), create=False)
    anchor.close()
    with pytest.raises(RuntimeError, match="descriptor"):
        _ = anchor.descriptor
    with pytest.raises(RuntimeError, match="closed"):
        anchor.__enter__()


def test_cancel_safe_worker_failure_is_visible_after_cancellation() -> None:
    async def scenario() -> None:
        started = threading.Event()
        finish = threading.Event()

        def fail_after_release() -> None:
            started.set()
            if not finish.wait(timeout=2):
                raise AssertionError("worker was not released")
            raise RuntimeError("worker failed after cancellation")

        task = asyncio.create_task(_cancel_safe_to_thread(fail_after_release))
        await wait_for_thread_event(started)
        task.cancel()
        finish.set()
        with pytest.raises(RuntimeError, match="worker failed after cancellation"):
            await task

    asyncio.run(scenario())


def test_cancel_safe_already_finished_worker_error_outranks_cancellation() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def fail_before_cancel_is_observed() -> None:
            started.set()
            assert release.wait(timeout=2)
            finished.set()
            raise RuntimeError("already-finished worker failure")

        task = asyncio.create_task(_cancel_safe_to_thread(fail_before_cancel_is_observed))
        await wait_for_thread_event(started)
        task.cancel()
        release.set()
        assert finished.wait(timeout=2)
        with pytest.raises(RuntimeError, match="already-finished worker failure"):
            await task

    asyncio.run(scenario())


def test_cancel_safe_worker_is_joined_and_cleaned_after_repeated_cancellation() -> None:
    async def scenario() -> None:
        worker_started = threading.Event()
        finish_worker = threading.Event()
        cleanup_started = threading.Event()
        finish_cleanup = threading.Event()
        cleaned: list[object] = []
        resource = object()

        def create_resource() -> object:
            worker_started.set()
            assert finish_worker.wait(timeout=2)
            return resource

        def cleanup_resource(value: object) -> None:
            cleanup_started.set()
            assert finish_cleanup.wait(timeout=2)
            cleaned.append(value)

        task = asyncio.create_task(
            _cancel_safe_to_thread(
                create_resource,
                cleanup_on_cancel=cleanup_resource,
            )
        )
        await wait_for_thread_event(worker_started)
        task.cancel()
        finish_worker.set()
        await wait_for_thread_event(cleanup_started)
        task.cancel()
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [resource]

    asyncio.run(scenario())


def test_cancel_safe_cleanup_failure_is_visible_after_cancellation() -> None:
    async def scenario() -> None:
        worker_started = threading.Event()
        finish_worker = threading.Event()

        def create_resource() -> object:
            worker_started.set()
            assert finish_worker.wait(timeout=2)
            return object()

        def fail_cleanup(_value: object) -> None:
            raise RuntimeError("cleanup failed after cancellation")

        task = asyncio.create_task(
            _cancel_safe_to_thread(create_resource, cleanup_on_cancel=fail_cleanup)
        )
        await wait_for_thread_event(worker_started)
        task.cancel()
        finish_worker.set()
        with pytest.raises(RuntimeError, match="cleanup failed after cancellation"):
            await task

    asyncio.run(scenario())


def test_owned_async_lanes_progress_and_cleanup_with_saturated_default_executor(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="reader-owner",
        key="attachment-2/file.bin",
        chunks=(b"read",),
        max_bytes=4,
    )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blocked-default")
        loop.set_default_executor(default)
        blocker_started = threading.Event()
        release_blocker = threading.Event()

        def block_default() -> None:
            blocker_started.set()
            assert release_blocker.wait(timeout=5)

        blocked = loop.run_in_executor(None, block_default)
        await wait_for_thread_event(blocker_started)

        authority = _pending_publish_authority()
        reservation = await blobs.areserve_materialization_staging(owner_id=authority.owner_id)
        session = blobs._begin_staged_materialization(
            authority=authority,
            reservation=reservation,
        )

        async def chunks() -> AsyncIterator[bytes]:
            yield b"data"

        staged = await asyncio.wait_for(session.awrite_chunks(chunks()), timeout=2)

        async def contend() -> None:
            contender = await blobs.areserve_materialization_staging(owner_id=authority.owner_id)
            contender.release()

        contenders = tuple(asyncio.create_task(contend()) for _ in range(6))
        observed = b"".join(
            [
                chunk
                async for chunk in blobs.aiter_chunks(
                    owner_id="reader-owner",
                    key="attachment-2/file.bin",
                    max_bytes=4,
                    chunk_size=2,
                )
            ]
        )
        assert observed == b"read"
        await blobs._run_stage_io(staged.abort)
        await asyncio.wait_for(asyncio.gather(*contenders), timeout=2)

        cleanup_threads: list[str] = []
        resource_started = threading.Event()
        release_resource = threading.Event()

        def create_resource() -> object:
            resource_started.set()
            assert release_resource.wait(timeout=2)
            return object()

        def cleanup_resource(_resource: object) -> None:
            cleanup_threads.append(threading.current_thread().name)

        task = asyncio.create_task(
            _cancel_safe_in_executor(
                blobs._control_io_executor,
                create_resource,
                cleanup_on_cancel=cleanup_resource,
            )
        )
        await wait_for_thread_event(resource_started)
        task.cancel()
        release_resource.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup_threads and cleanup_threads[0].startswith("astralplane-blob-control")

        release_blocker.set()
        await blocked

    asyncio.run(scenario())
    assert blobs._owner_locks._entries == {}


@pytest.mark.asyncio
async def test_abandoned_verified_async_reads_do_not_drain_or_starve_control_lane(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"verified-reader-payload"
    digest = hashlib.sha256(payload).hexdigest()
    for owner in ("slow-owner-a", "slow-owner-b", "unrelated-owner"):
        _publish_chunks_for_test(
            blobs,
            owner_id=owner,
            key="attachment-1/file.bin",
            chunks=(payload,),
            max_bytes=len(payload),
        )

    release_drain = threading.Event()
    blocked_drains: list[str] = []
    observed_readers: list[Any] = []
    original_open = ExplicitRootStreamingBlobStore._open_reader_prelocked

    class _BlockAfterFirstRead:
        def __init__(self, stream: Any, owner: str) -> None:
            self._stream = stream
            self._owner = owner
            self._reads = 0

        def read(self, size: int = -1) -> bytes:
            self._reads += 1
            if self._reads > 1:
                blocked_drains.append(self._owner)
                assert release_drain.wait(timeout=3)
            return self._stream.read(size)

        def close(self) -> None:
            self._stream.close()

    def observed_open(
        store: ExplicitRootStreamingBlobStore,
        **kwargs: Any,
    ) -> Any:
        reader = original_open(store, **kwargs)
        owner = str(kwargs["owner"])
        if owner.startswith("slow-owner-"):
            reader._stream = _BlockAfterFirstRead(reader._stream, owner)
            observed_readers.append(reader)
        return reader

    monkeypatch.setattr(
        ExplicitRootStreamingBlobStore,
        "_open_reader_prelocked",
        observed_open,
    )

    streams = [
        blobs.aiter_chunks(
            owner_id=owner,
            key="attachment-1/file.bin",
            max_bytes=len(payload),
            chunk_size=1,
            expected_sha256=digest,
        )
        for owner in ("slow-owner-a", "slow-owner-b")
    ]
    for stream in streams:
        assert await anext(stream) == payload[:1]

    close_tasks = tuple(asyncio.create_task(stream.aclose()) for stream in streams)

    async def read_unrelated_owner() -> bytes:
        return b"".join(
            [
                chunk
                async for chunk in blobs.aiter_chunks(
                    owner_id="unrelated-owner",
                    key="attachment-1/file.bin",
                    max_bytes=len(payload),
                    chunk_size=4,
                    expected_sha256=digest,
                )
            ]
        )

    reservation_task = asyncio.create_task(
        blobs.areserve_materialization_staging(owner_id="reservation-owner")
    )
    read_task = asyncio.create_task(read_unrelated_owner())
    deadline = asyncio.get_running_loop().time() + 1
    while (
        not all(task.done() for task in (*close_tasks, reservation_task, read_task))
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.01)
    progressed_without_drain = all(
        task.done() for task in (*close_tasks, reservation_task, read_task)
    )

    release_drain.set()
    await asyncio.gather(*close_tasks)
    reservation = await reservation_task
    reservation.release()
    assert await read_task == payload

    assert progressed_without_drain
    assert blocked_drains == []
    assert len(observed_readers) == 2
    assert all(reader.closed and not reader._verified for reader in observed_readers)
    assert blobs._owner_locks._entries == {}


def test_store_close_fails_busy_then_closes_and_rejects_new_capabilities(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    reservation = blobs.reserve_materialization_staging(owner_id="owner-1")
    with pytest.raises(PlaneError) as busy:
        blobs.close()
    assert busy.value.code == "blob_store_busy"
    reservation.release()

    blobs.close()
    blobs.close()
    with pytest.raises(PlaneError) as closed:
        blobs.reserve_materialization_staging(owner_id="owner-1")
    assert closed.value.code == "blob_store_closed"


def test_directory_anchor_failure_guards_close_resources(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _DirectoryAnchor(blobs, components=(), create=False)
    original_lstat = os.lstat
    expected_root = (
        blob_module._windows_extended_path(blob_root) if os.name == "nt" else os.fspath(blob_root)
    )

    def missing_root(path: os.PathLike[str] | str) -> os.stat_result:
        if os.fspath(path) == expected_root:
            raise FileNotFoundError(os.fspath(path))
        return original_lstat(path)

    monkeypatch.setattr(os, "lstat", missing_root)
    with pytest.raises(PlaneError) as unavailable:
        anchor.assert_current()
    assert unavailable.value.code == "blob_path_unsafe"
    anchor.close()
    monkeypatch.setattr(os, "lstat", original_lstat)

    existing = blob_root / "already-present"
    existing.mkdir()
    anchor = _DirectoryAnchor(blobs, components=(), create=False)
    try:
        anchor._mkdir("already-present", blob_root)
        with pytest.raises(FileNotFoundError):
            anchor._assert_exact_file_case("missing.bin", allow_missing=False)
    finally:
        anchor.close()


def test_unstable_root_identity_is_rejected(
    blob_root: Path,
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(blob_module, "_metadata_identity", lambda _metadata: None)
    with pytest.raises(PlaneError) as unavailable:
        ExplicitRootStreamingBlobStore(blob_root)
    assert unavailable.value.code == "blob_path_unsafe"

    monkeypatch.undo()
    original_identity = blobs._root_identity
    monkeypatch.setattr(
        blobs,
        "_root_identity",
        (original_identity[0], original_identity[1] + 1),
    )
    with pytest.raises(PlaneError) as changed:
        _DirectoryAnchor(blobs, components=(), create=False)
    assert changed.value.code == "blob_path_unsafe"


def test_reader_close_drains_and_verifies_unread_expected_digest(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    payload = b"unread-digest-data"
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(payload,),
        max_bytes=len(payload),
    )
    reader = blobs.open_reader(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    reader.close()
    reader.close()

    assert reader.closed
    assert blobs._owner_locks._entries == {}
    with pytest.raises(PlaneError) as closed:
        reader.read()
    assert closed.value.code == "blob_reader_closed"


def test_parser_enter_validation_failure_closes_every_capability(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    lease = blobs.open_parser_lease(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=4,
    )

    def fail_validation(*_args: Any, **_kwargs: Any) -> None:
        raise BlobIntegrityError("synthetic lease-entry validation failure")

    monkeypatch.setattr(blobs, "_validate_lease_target", fail_validation)
    with pytest.raises(BlobIntegrityError, match="lease-entry"):
        lease.__enter__()
    assert blobs._owner_locks._entries == {}


def test_partial_low_level_write_and_abort_replay_fail_closed(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    authority = _test_publish_authority(
        owner_id="owner-1",
        key="attachment-1/file.bin",
        max_bytes=4,
    )
    reservation = blobs.reserve_materialization_staging(owner_id="owner-1")
    owner_lock = reservation.take(blobs, owner="owner-1")
    session = blobs._begin_authorized_staged_write(
        authority=authority,
        owner_lock=owner_lock,
    )
    original_stream = session._stream

    class PartialWriter:
        @property
        def closed(self) -> bool:
            return original_stream.closed

        def write(self, value: memoryview) -> int:
            return max(0, len(value) - 1)

        def close(self) -> None:
            original_stream.close()

    session._stream = PartialWriter()  # type: ignore[assignment]
    with pytest.raises(PlaneError) as incomplete:
        session.write(memoryview(b"data"))
    assert incomplete.value.code == "blob_write_incomplete"
    session.abort()
    session.abort()
    assert list(blob_root.rglob(".astralplane-*.tmp")) == []
    assert blobs._owner_locks._entries == {}


def test_open_reader_detects_size_and_identity_changes_during_open(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    original_open = _DirectoryAnchor.open_file

    def grow_after_open(
        anchor: _DirectoryAnchor,
        name: str,
        **kwargs: Any,
    ) -> int:
        descriptor = original_open(anchor, name, **kwargs)
        (anchor.path / name).write_bytes(b"larger")
        return descriptor

    monkeypatch.setattr(_DirectoryAnchor, "open_file", grow_after_open)
    with pytest.raises(BlobIntegrityError, match="changed"):
        blobs.open_reader(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=10,
        )
    assert blobs._owner_locks._entries == {}

    monkeypatch.setattr(_DirectoryAnchor, "open_file", original_open)
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/other.bin",
        chunks=(b"larger",),
        max_bytes=6,
    )

    def open_other(
        anchor: _DirectoryAnchor,
        _name: str,
        **kwargs: Any,
    ) -> int:
        return original_open(anchor, "other.bin", **kwargs)

    monkeypatch.setattr(_DirectoryAnchor, "open_file", open_other)
    with pytest.raises(BlobIntegrityError, match="identity"):
        blobs.open_reader(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=10,
        )
    assert blobs._owner_locks._entries == {}


def test_parser_descriptor_open_failure_releases_owner_and_anchor(
    blobs: ExplicitRootStreamingBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )

    def fail_fdopen(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("synthetic fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen"):
        blobs.open_parser_lease(
            owner_id="owner-1",
            key="attachment-1/file.bin",
            max_bytes=4,
        )
    assert blobs._owner_locks._entries == {}


def test_leaf_object_type_and_delete_postcondition_fail_closed(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = blob_root / "owner-1" / "attachment-1" / "directory.bin"
    leaf.mkdir(parents=True)
    for operation in (
        lambda: blobs.open_reader(
            owner_id="owner-1", key="attachment-1/directory.bin", max_bytes=4
        ),
        lambda: blobs.open_parser_lease(
            owner_id="owner-1", key="attachment-1/directory.bin", max_bytes=4
        ),
        lambda: blobs.is_absent(owner_id="owner-1", key="attachment-1/directory.bin"),
    ):
        with pytest.raises(PlaneError) as unsafe:
            operation()
        assert unsafe.value.code == "blob_path_unsafe"

    leaf.rmdir()
    _publish_chunks_for_test(
        blobs,
        owner_id="owner-1",
        key="attachment-1/file.bin",
        chunks=(b"data",),
        max_bytes=4,
    )
    original_unlink = _DirectoryAnchor.unlink

    def retain_target(anchor: _DirectoryAnchor, name: str) -> None:
        if name != "file.bin":
            original_unlink(anchor, name)

    monkeypatch.setattr(_DirectoryAnchor, "unlink", retain_target)
    with pytest.raises(PlaneError) as incomplete:
        _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1")
    assert incomplete.value.code == "blob_delete_failed"


def test_digest_validation_and_private_path_guards_cover_failure_edges(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    with pytest.raises(BlobIntegrityError, match="changed"):
        blobs._verify_descriptor_digest(
            io.BytesIO(b"ab"),
            size_bytes=4,
            expected_sha256=hashlib.sha256(b"abcd").hexdigest(),
        )
    with pytest.raises(BlobIntegrityError, match="changed"):
        blobs._verify_descriptor_digest(
            io.BytesIO(b"abc"),
            size_bytes=2,
            expected_sha256=hashlib.sha256(b"ab").hexdigest(),
        )
    with pytest.raises(PlaneError) as escaped:
        blobs._relative(blob_root.parent)
    assert escaped.value.code == "blob_path_unsafe"
    with pytest.raises(SQLContractError, match="purge authority"):
        blobs._delete_components(  # type: ignore[arg-type]
            object(),
            components=(),
            prune_parent=False,
        )

    anchor = _DirectoryAnchor(blobs, components=(), create=False)
    try:
        with pytest.raises(SQLContractError, match="purge authority"):
            blobs._delete_anchor_contents(  # type: ignore[arg-type]
                object(),
                anchor,
                depth=100,
            )
        with pytest.raises(PlaneError, match="bounded depth"):
            blobs._validate_anchor_tree(anchor, depth=100)
    finally:
        anchor.close()


def test_missing_prune_path_and_unsafe_root_provisioning_are_bounded(
    blobs: ExplicitRootStreamingBlobStore,
    tmp_path: Path,
) -> None:
    blobs._prune_empty_components(("absent-owner", "absent-prefix"))
    assert blobs.is_owner_absent(owner_id="absent-owner")

    file_ancestor = (tmp_path / "file-ancestor").resolve()
    file_ancestor.write_bytes(b"not a directory")
    with pytest.raises(SQLContractError, match="real directory ancestry"):
        ExplicitRootStreamingBlobStore(
            file_ancestor / "child",
            create_root=True,
        )


def _pending_publish_authority(lease_id: str = "lease-1"):
    return _create_blob_publish_authority(
        owner_id="owner-1",
        storage_key="attachment-1/file.bin",
        max_bytes=4,
        lease_id=lease_id,
    )


def _stage(
    blobs: ExplicitRootStreamingBlobStore,
    chunks: Iterable[bytes],
    *,
    lease_id: str = "lease-1",
):
    authority = _pending_publish_authority(lease_id)
    reservation = blobs.reserve_materialization_staging(owner_id=authority.owner_id)
    return blobs._begin_staged_materialization(
        authority=authority,
        reservation=reservation,
    ).write_chunks(chunks)


def test_staged_write_is_hidden_single_writer_and_prefix_purge_removes_fence(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    staged = _stage(blobs, (b"data",))
    attachment_root = blob_root / "owner-1" / "attachment-1"
    names = {path.name for path in attachment_root.iterdir()}
    assert "file.bin" not in names
    assert len([name for name in names if name.endswith(".tmp")]) == 1
    assert len([name for name in names if name.endswith(".lock")]) == 1

    result = blobs._publish_staged_materialization(
        staged,
        authority=_pending_publish_authority(),
    )
    assert result.sha256 == hashlib.sha256(b"data").hexdigest()
    assert (attachment_root / "file.bin").read_bytes() == b"data"
    assert len([path for path in attachment_root.iterdir() if path.suffix == ".lock"]) == 1

    with pytest.raises(PlaneError) as stale:
        _stage(blobs, (b"late",))
    assert stale.value.code == "blob_staging_conflict"
    deleted = _purge_prefix_for_test(blobs, owner_id="owner-1", prefix="attachment-1")
    assert deleted.deleted_files == 2
    assert deleted.absent_verified


def test_staged_abort_is_idempotent_and_reserved_internal_keys_are_unreachable(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    staged = _stage(blobs, (b"data",))
    staged.abort()
    staged.abort()
    assert list(blob_root.rglob(".astralplane-stage-*")) == []
    assert blobs._owner_locks._entries == {}

    for operation in (
        lambda: blobs.open_reader(
            owner_id="owner-1",
            key="attachment-1/.astralplane-stage-forged.lock",
            max_bytes=1,
        ),
    ):
        with pytest.raises(SQLContractError, match="reserved Plane"):
            operation()


def test_staged_publish_rejects_lease_identity_mismatch_without_visibility(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    staged = _stage(blobs, (b"data",), lease_id="lease-2")
    with pytest.raises(PlaneError) as mismatch:
        blobs._publish_staged_materialization(
            staged,
            authority=_pending_publish_authority(),
        )
    assert mismatch.value.code == "blob_publish_fence_conflict"
    staged.abort()
    assert blobs.is_absent(owner_id="owner-1", key="attachment-1/file.bin")


@pytest.mark.asyncio
async def test_async_staging_failure_aborts_deterministic_temp_and_sentinel(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield b"data"
        raise RuntimeError("source failed")

    with pytest.raises(RuntimeError, match="source failed"):
        authority = _pending_publish_authority()
        reservation = blobs.reserve_materialization_staging(owner_id=authority.owner_id)
        session = blobs._begin_staged_materialization(
            authority=authority,
            reservation=reservation,
        )
        await session.awrite_chunks(broken())
    assert list(blob_root.rglob(".astralplane-stage-*")) == []
    assert blobs._owner_locks._entries == {}


def test_staging_second_fdopen_failure_removes_temp_sentinel_and_owner_lock(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = blob_module.os.fdopen
    calls = 0

    def fail_second(descriptor: int, mode: str, *, closefd: bool = True):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected temporary fdopen failure")
        return original(descriptor, mode, closefd=closefd)

    monkeypatch.setattr(blob_module.os, "fdopen", fail_second)
    authority = _pending_publish_authority()
    reservation = blobs.reserve_materialization_staging(owner_id=authority.owner_id)
    with pytest.raises(OSError, match="temporary fdopen failure"):
        blobs._begin_staged_materialization(
            authority=authority,
            reservation=reservation,
        )
    assert list(blob_root.rglob(".astralplane-stage-*")) == []
    assert blobs._owner_locks._entries == {}


def test_staging_constructor_failure_removes_temp_sentinel_and_owner_lock(
    blobs: ExplicitRootStreamingBlobStore,
    blob_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_constructor(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected staging constructor failure")

    monkeypatch.setattr(blob_module, "_AtomicWriteSession", fail_constructor)
    authority = _pending_publish_authority()
    reservation = blobs.reserve_materialization_staging(owner_id=authority.owner_id)
    with pytest.raises(RuntimeError, match="staging constructor failure"):
        blobs._begin_staged_materialization(
            authority=authority,
            reservation=reservation,
        )
    assert list(blob_root.rglob(".astralplane-stage-*")) == []
    assert blobs._owner_locks._entries == {}


def test_unfenced_physical_creation_is_not_a_public_blob_store_contract(
    blobs: ExplicitRootStreamingBlobStore,
) -> None:
    assert not hasattr(blobs, "write_chunks")
    assert not hasattr(blobs, "awrite_chunks")
    assert not hasattr(blobs, "stage_chunks")
    assert not hasattr(blobs, "astage_chunks")
    assert not hasattr(blobs, "_write_chunks_unfenced_for_testing")
    assert not hasattr(blobs, "_awrite_chunks_unfenced_for_testing")
    assert not hasattr(blobs, "_delete_key_for_testing")
    assert not hasattr(blobs, "_delete_prefix_for_testing")
    assert not hasattr(blobs, "_delete_owner_for_testing")
    assert not hasattr(blobs, "_begin_write")


def test_physical_authorities_and_staging_reservations_fail_closed(
    blobs: ExplicitRootStreamingBlobStore,
    tmp_path: Path,
) -> None:
    with pytest.raises(SQLContractError, match="publish authority is not constructible"):
        blob_module._BlobPublishAuthority(
            object(),
            owner_id="owner-1",
            storage_key="attachment-1/file.bin",
            max_bytes=4,
            lease_id="lease-1",
        )
    with pytest.raises(SQLContractError, match="purge authority is not constructible"):
        blob_module._BlobPurgeAuthority(
            object(),
            owner_id="owner-1",
            target_scope="attachment_prefix",
            storage_key="attachment-1",
        )
    with pytest.raises(SQLContractError, match="scope is unsupported"):
        _create_blob_purge_authority(
            owner_id="owner-1",
            target_scope="exact_key",
            storage_key="attachment-1/file.bin",
        )

    other = ExplicitRootStreamingBlobStore(
        (tmp_path / "other-root").resolve(),
        create_root=True,
    )
    reservation = blobs.reserve_materialization_staging(owner_id="owner-1")
    try:
        with pytest.raises(PlaneError, match="reservation is invalid"):
            reservation.take(other, owner="owner-1")
    finally:
        reservation.release()
        other.close()
