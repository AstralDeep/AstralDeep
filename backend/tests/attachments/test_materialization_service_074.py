"""Adversarial policy tests for Deep's one attachment publication state machine."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from astralplane import BlobWriteResult
from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.artifacts import (
    AttachmentMaterializationBeginResult,
    AttachmentMaterializationState,
    AttachmentRecord,
    PendingAttachmentMaterializationRecord,
)

from orchestrator.attachments.materialization import AttachmentMaterializationService

OWNER = "user-materialization"
ATTACHMENT = "attachment-materialization"
FILENAME = "sample.txt"
LEASE_ID = "deep-00000000-0000-0000-0000-000000000001"
PAYLOAD = b"bounded attachment payload"
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


def _ready(
    *,
    size_bytes: int = len(PAYLOAD),
    sha256: str = SHA256,
    content_type: str = "text/plain",
    deleted_at: int | None = None,
) -> AttachmentRecord:
    return AttachmentRecord(
        attachment_id=ATTACHMENT,
        owner_id=OWNER,
        filename=FILENAME,
        content_type=content_type,
        category="text",
        extension="txt",
        size_bytes=size_bytes,
        sha256=sha256,
        storage_locator=f"{OWNER}/{ATTACHMENT}/{FILENAME}",
        created_at=1_786_726_800_000,
        deleted_at=deleted_at,
    )


class _Staged:
    def __init__(
        self,
        payload: bytes,
        *,
        abort_error: BaseException | None = None,
        owner_released: asyncio.Event | None = None,
    ) -> None:
        self.payload = payload
        self.evidence = BlobWriteResult(
            storage_key=f"{ATTACHMENT}/{FILENAME}",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        self.aborted = False
        self.abort_error = abort_error
        self.owner_released = owner_released

    def read_prefix(self, *, max_bytes: int = 8192) -> bytes:
        return self.payload[:max_bytes]

    def abort(self) -> None:
        self.aborted = True
        if self.owner_released is not None:
            self.owner_released.set()
        if self.abort_error is not None:
            raise self.abort_error


class _Session:
    def __init__(self) -> None:
        self.aborted = False
        self.owner_released = asyncio.Event()
        self.staged_created = asyncio.Event()
        self.staged: _Staged | None = None
        self.abort_error: BaseException | None = None

    async def awrite_chunks(self, chunks: AsyncIterator[bytes]) -> _Staged:
        payload = bytearray()
        try:
            async for chunk in chunks:
                payload.extend(chunk)
        except BaseException:
            self.aborted = True
            self.owner_released.set()
            raise
        self.staged = _Staged(
            bytes(payload),
            abort_error=self.abort_error,
            owner_released=self.owner_released,
        )
        self.staged_created.set()
        return self.staged

    def write_chunks(self, chunks: Iterable[bytes]) -> _Staged:
        self.staged = _Staged(
            b"".join(chunks),
            abort_error=self.abort_error,
            owner_released=self.owner_released,
        )
        self.staged_created.set()
        return self.staged

    async def aabort(self) -> None:
        self.aborted = True
        self.owner_released.set()

    def abort(self) -> None:
        self.aborted = True
        self.owner_released.set()


class _Purges:
    def __init__(self) -> None:
        self.abandoned: list[dict[str, object]] = []
        self.scheduled: list[dict[str, object]] = []
        self.ready = True

    async def aabandon_pending_materialization(self, **values: object) -> None:
        self.ready = False
        self.abandoned.append(values)

    def abandon_pending_materialization(self, **values: object) -> None:
        self.ready = False
        self.abandoned.append(values)

    async def aschedule_attachment(self, **values: object) -> None:
        self.scheduled.append(values)

    def schedule_attachment(self, **values: object) -> None:
        self.scheduled.append(values)


class _Coordinator:
    def __init__(self) -> None:
        self.pending: PendingAttachmentMaterializationRecord | None = None
        self.ready: AttachmentRecord | None = None
        self.session = _Session()
        self.begin_lost_once = False
        self.begin_error_responses = 0
        self.publish_lost_once = False
        self.renew_lost_once = False
        self.begin_calls = 0
        self.publish_versions: list[int] = []
        self.renew_calls = 0
        self.renew_replayed = asyncio.Event()
        self.replay_started = asyncio.Event()
        self.replay_release = asyncio.Event()
        self.block_ready_replay = False

    def _begin(self, values: dict[str, object]) -> AttachmentMaterializationBeginResult:
        self.begin_calls += 1
        if self.ready is not None:
            return AttachmentMaterializationBeginResult(
                state=AttachmentMaterializationState.READY,
                ready=self.ready,
            )
        if self.pending is None:
            self.pending = PendingAttachmentMaterializationRecord(
                attachment_id=str(values["attachment_id"]),
                owner_id=str(values["owner_id"]),
                filename=str(values["filename"]),
                category=str(values["category"]),
                extension=str(values["extension"]),
                storage_locator=str(values["storage_locator"]),
                storage_key=str(values["storage_key"]),
                max_bytes=int(values["max_bytes"]),
                created_at=int(values["created_at"]),
                lease_id=str(values["lease_id"]),
                lease_version=0,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
        if self.begin_lost_once:
            self.begin_lost_once = False
            raise ConnectionError("begin commit response lost")
        if self.begin_error_responses:
            self.begin_error_responses -= 1
            raise ConnectionError("begin control response lost")
        return AttachmentMaterializationBeginResult(
            state=AttachmentMaterializationState.PENDING,
            pending=self.pending,
        )

    async def abegin_pending_materialization(
        self, **values: object
    ) -> AttachmentMaterializationBeginResult:
        if self.ready is not None and self.block_ready_replay:
            self.replay_started.set()
            await self.replay_release.wait()
        return self._begin(values)

    def begin_pending_materialization(
        self, **values: object
    ) -> AttachmentMaterializationBeginResult:
        return self._begin(values)

    async def arenew_pending_materialization(
        self, **values: object
    ) -> PendingAttachmentMaterializationRecord:
        self.renew_calls += 1
        assert self.pending is not None
        expected = int(values["expected_lease_version"])
        if self.pending.lease_version == expected:
            self.pending = PendingAttachmentMaterializationRecord(
                **{
                    **{
                        name: getattr(self.pending, name)
                        for name in self.pending.__dataclass_fields__
                    },
                    "lease_version": expected + 1,
                }
            )
            if self.renew_lost_once:
                self.renew_lost_once = False
                raise ConnectionError("renew commit response lost")
        elif self.pending.lease_version != expected + 1:
            raise RepositoryConflictError("stale renewal")
        self.renew_replayed.set()
        return self.pending

    async def aopen_pending_materialization_staging(self, **values: object) -> _Session:
        assert self.pending is not None
        if int(values["expected_lease_version"]) != self.pending.lease_version:
            raise RepositoryConflictError("stale stage open")
        return self.session

    def open_pending_materialization_staging(self, **values: object) -> _Session:
        assert self.pending is not None
        if int(values["expected_lease_version"]) != self.pending.lease_version:
            raise RepositoryConflictError("stale stage open")
        return self.session

    def _publish(self, values: dict[str, object]) -> AttachmentRecord:
        assert self.pending is not None
        staged = values["staged"]
        expected = int(values["expected_lease_version"])
        assert expected == self.pending.lease_version
        self.publish_versions.append(expected)
        self.ready = _ready(
            size_bytes=staged.evidence.size_bytes,
            sha256=staged.evidence.sha256,
            content_type=str(values["content_type"]),
        )
        if self.publish_lost_once:
            self.publish_lost_once = False
            raise ConnectionError("publish commit response lost")
        return self.ready

    async def apublish_pending_materialization(self, **values: object) -> AttachmentRecord:
        return self._publish(values)

    def publish_pending_materialization(self, **values: object) -> AttachmentRecord:
        return self._publish(values)


def _service(
    coordinator: _Coordinator,
    purges: _Purges,
    *,
    heartbeat_seconds: float = 0.1,
) -> AttachmentMaterializationService:
    return AttachmentMaterializationService(
        coordinator=coordinator,
        purge_coordinator=purges,
        clock_milliseconds=lambda: 1_786_726_800_000,
        uuid_factory=lambda: uuid.UUID(int=1),
        lease_seconds=30,
        heartbeat_seconds=heartbeat_seconds,
        policy_workers=1,
    )


async def _payload_stream() -> AsyncIterator[bytes]:
    yield PAYLOAD


@pytest.mark.asyncio
async def test_async_begin_and_publish_lost_responses_resolve_exact_ready() -> None:
    coordinator = _Coordinator()
    coordinator.begin_lost_once = True
    coordinator.publish_lost_once = True
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        record = await service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=_payload_stream(),
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
        )
        assert record == _ready()
        assert coordinator.begin_calls == 3
        assert purges.abandoned == []
        assert purges.scheduled == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_async_ready_replay_must_match_staged_evidence() -> None:
    coordinator = _Coordinator()
    coordinator.publish_lost_once = True
    original_publish = coordinator._publish

    def _mismatched_publish(values: dict[str, object]) -> AttachmentRecord:
        try:
            return original_publish(values)
        finally:
            coordinator.ready = _ready(sha256="f" * 64)

    coordinator._publish = _mismatched_publish  # type: ignore[method-assign]
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        with pytest.raises(RuntimeError, match="mismatched READY metadata"):
            await service.materialize_stream(
                owner_id=OWNER,
                attachment_id=ATTACHMENT,
                filename=FILENAME,
                category="text",
                extension="txt",
                chunks=_payload_stream(),
                max_bytes=1024,
                resolve_content_type=lambda _prefix: "text/plain",
            )
        assert purges.abandoned == []
        assert purges.scheduled == [
            {"owner_id": OWNER, "attachment_id": ATTACHMENT}
        ]
    finally:
        await service.close()


def test_sync_publish_lost_response_resolves_exact_ready() -> None:
    coordinator = _Coordinator()
    coordinator.publish_lost_once = True
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        record = service.materialize_bytes(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=(PAYLOAD,),
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
        )
        assert record == _ready()
        assert purges.abandoned == []
    finally:
        service.abort()


class _NeverSource:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    def __aiter__(self) -> _NeverSource:
        return self

    async def __anext__(self) -> bytes:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        self.closed.set()


class _HeldCloseSource(_NeverSource):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed.set()


class _HeldEofCloseSource(_HeldCloseSource):
    def __init__(self) -> None:
        super().__init__()
        self._emitted = False

    async def __anext__(self) -> bytes:
        if not self._emitted:
            self._emitted = True
            return PAYLOAD
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_heartbeat_failure_interrupts_never_yield_source_and_joins_close() -> None:
    coordinator = _Coordinator()

    async def _fail_renew(**_values: object) -> object:
        raise ConnectionError("lease authority unavailable")

    coordinator.arenew_pending_materialization = _fail_renew  # type: ignore[method-assign]
    purges = _Purges()
    service = _service(coordinator, purges)
    source = _NeverSource()
    try:
        with pytest.raises(ConnectionError, match="lease authority unavailable"):
            await asyncio.wait_for(
                service.materialize_stream(
                    owner_id=OWNER,
                    attachment_id=ATTACHMENT,
                    filename=FILENAME,
                    category="text",
                    extension="txt",
                    chunks=source,
                    max_bytes=1024,
                    resolve_content_type=lambda _prefix: "text/plain",
                ),
                timeout=2,
            )
        assert source.started.is_set()
        assert source.closed.is_set()
        assert coordinator.session.aborted is True
        assert purges.abandoned[0]["expected_lease_version"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stage_releases_owner_before_joining_held_source_close() -> None:
    coordinator = _Coordinator()

    async def _fail_renew(**_values: object) -> object:
        raise ConnectionError("lease authority unavailable")

    coordinator.arenew_pending_materialization = _fail_renew  # type: ignore[method-assign]
    purges = _Purges()
    service = _service(coordinator, purges)
    source = _HeldCloseSource()
    task = asyncio.create_task(
        service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=source,
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
        )
    )
    try:
        await asyncio.wait_for(source.close_started.wait(), timeout=2)
        await asyncio.wait_for(coordinator.session.owner_released.wait(), timeout=0.2)
        assert task.done() is False
        assert purges.abandoned == []
        source.close_release.set()
        with pytest.raises(ConnectionError, match="lease authority unavailable"):
            await asyncio.wait_for(task, timeout=2)
        assert source.closed.is_set()
        assert purges.abandoned
    finally:
        source.close_release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await service.close()


@pytest.mark.asyncio
async def test_completed_stage_abort_error_still_joins_held_source_and_abandons() -> None:
    coordinator = _Coordinator()
    coordinator.session.abort_error = OSError("staged cleanup failed")
    purges = _Purges()
    service = _service(coordinator, purges)
    source = _HeldEofCloseSource()
    upload = asyncio.create_task(
        service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=source,
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
        )
    )
    close = None
    try:
        await asyncio.wait_for(coordinator.session.staged_created.wait(), timeout=1)
        await asyncio.wait_for(source.close_started.wait(), timeout=1)
        close = asyncio.create_task(service.close())
        await asyncio.wait_for(coordinator.session.owner_released.wait(), timeout=1)
        assert source.closed.is_set() is False
        assert upload.done() is False
        source.close_release.set()
        with pytest.raises(RuntimeError, match="closing"):
            await asyncio.wait_for(upload, timeout=2)
        await asyncio.wait_for(close, timeout=2)
        assert source.closed.is_set()
        assert coordinator.session.staged is not None
        assert coordinator.session.staged.aborted is True
        assert purges.abandoned
    finally:
        source.close_release.set()
        if not upload.done():
            upload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await upload
        if close is None:
            await service.close()
        elif not close.done():
            await close


class _RaisingIteratorSource:
    def __aiter__(self):
        raise RuntimeError("source iterator unavailable")


class _IndependentCancelledSource:
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        self.closed.set()


class _FutureSource:
    def __init__(self) -> None:
        self.index = 0
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    def __anext__(self):
        future = asyncio.get_running_loop().create_future()
        if self.index == 0:
            self.index += 1
            future.set_result(PAYLOAD)
        else:
            future.set_exception(StopAsyncIteration())
        return future

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "error"),
    [
        (_RaisingIteratorSource(), RuntimeError),
        (_IndependentCancelledSource(), asyncio.CancelledError),
    ],
)
async def test_source_pump_terminal_failures_cannot_strand_plane_stage(
    source,
    error,
) -> None:
    coordinator = _Coordinator()
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        with pytest.raises(error):
            await asyncio.wait_for(
                service.materialize_stream(
                    owner_id=OWNER,
                    attachment_id=ATTACHMENT,
                    filename=FILENAME,
                    category="text",
                    extension="txt",
                    chunks=source,
                    max_bytes=1024,
                    resolve_content_type=lambda _prefix: "text/plain",
                ),
                timeout=1,
            )
        assert coordinator.session.aborted is True
        assert purges.abandoned
        closed = getattr(source, "closed", None)
        if closed is not None:
            assert closed.is_set()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_future_returning_anext_is_supported_and_joined() -> None:
    coordinator = _Coordinator()
    purges = _Purges()
    service = _service(coordinator, purges)
    source = _FutureSource()
    try:
        assert await service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=source,
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
        ) == _ready()
        assert source.closed.is_set()
    finally:
        await service.close()


class _CancelCleanupErrorSession(_Session):
    async def awrite_chunks(self, chunks: AsyncIterator[bytes]) -> _Staged:
        try:
            async for _chunk in chunks:
                pass
        except asyncio.CancelledError:
            self.aborted = True
            self.owner_released.set()
            raise OSError("stage cancellation cleanup failed") from None
        raise AssertionError("source unexpectedly completed")


@pytest.mark.asyncio
async def test_stage_cleanup_error_still_joins_held_source_and_abandons() -> None:
    coordinator = _Coordinator()
    coordinator.session = _CancelCleanupErrorSession()

    async def _fail_renew(**_values: object) -> object:
        raise ConnectionError("lease authority unavailable")

    coordinator.arenew_pending_materialization = _fail_renew  # type: ignore[method-assign]
    purges = _Purges()
    service = _service(coordinator, purges)
    source = _HeldCloseSource()
    task = asyncio.create_task(
        service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=source,
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
        )
    )
    try:
        await asyncio.wait_for(source.close_started.wait(), timeout=2)
        assert coordinator.session.owner_released.is_set()
        assert task.done() is False
        source.close_release.set()
        with pytest.raises(OSError, match="stage cancellation cleanup failed"):
            await asyncio.wait_for(task, timeout=2)
        assert source.closed.is_set()
        assert purges.abandoned
    finally:
        source.close_release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await service.close()


@pytest.mark.asyncio
async def test_async_abort_error_cannot_skip_durable_abandonment() -> None:
    coordinator = _Coordinator()
    coordinator.session.abort_error = OSError("staged cleanup failed")
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        with pytest.raises(ValueError, match="policy rejected"):
            await service.materialize_stream(
                owner_id=OWNER,
                attachment_id=ATTACHMENT,
                filename=FILENAME,
                category="text",
                extension="txt",
                chunks=_payload_stream(),
                max_bytes=1024,
                resolve_content_type=lambda _prefix: (_ for _ in ()).throw(
                    ValueError("policy rejected")
                ),
            )
        assert coordinator.session.staged is not None
        assert coordinator.session.staged.aborted is True
        assert purges.abandoned
    finally:
        await service.close()


def test_sync_abort_error_cannot_skip_durable_abandonment() -> None:
    coordinator = _Coordinator()
    coordinator.session.abort_error = OSError("staged cleanup failed")
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        with pytest.raises(ValueError, match="policy rejected"):
            service.materialize_bytes(
                owner_id=OWNER,
                attachment_id=ATTACHMENT,
                filename=FILENAME,
                category="text",
                extension="txt",
                chunks=(PAYLOAD,),
                max_bytes=1024,
                resolve_content_type=lambda _prefix: (_ for _ in ()).throw(
                    ValueError("policy rejected")
                ),
            )
        assert purges.abandoned
    finally:
        service.abort()


@pytest.mark.asyncio
async def test_invalid_sync_prebegin_input_releases_lifecycle_admission() -> None:
    coordinator = _Coordinator()
    purges = _Purges()
    service = _service(coordinator, purges)
    with pytest.raises(ValueError, match="creation time"):
        service.materialize_bytes(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=(PAYLOAD,),
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
            created_at=-1,
        )
    await asyncio.wait_for(service.close(), timeout=0.5)


@pytest.mark.asyncio
async def test_exhausted_async_begin_replay_degrades_via_exact_abandonment() -> None:
    coordinator = _Coordinator()
    coordinator.begin_error_responses = 3
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        with pytest.raises(ConnectionError, match="control response lost"):
            await service.materialize_stream(
                owner_id=OWNER,
                attachment_id=ATTACHMENT,
                filename=FILENAME,
                category="text",
                extension="txt",
                chunks=_payload_stream(),
                max_bytes=1024,
                resolve_content_type=lambda _prefix: "text/plain",
            )
        assert purges.ready is False
        assert purges.abandoned == [
            {
                "owner_id": OWNER,
                "attachment_id": ATTACHMENT,
                "lease_id": LEASE_ID,
                "expected_lease_version": 0,
            }
        ]
    finally:
        await service.close()


def test_exhausted_sync_begin_replay_degrades_via_exact_abandonment() -> None:
    coordinator = _Coordinator()
    coordinator.begin_error_responses = 3
    purges = _Purges()
    service = _service(coordinator, purges)
    try:
        with pytest.raises(ConnectionError, match="control response lost"):
            service.materialize_bytes(
                owner_id=OWNER,
                attachment_id=ATTACHMENT,
                filename=FILENAME,
                category="text",
                extension="txt",
                chunks=(PAYLOAD,),
                max_bytes=1024,
                resolve_content_type=lambda _prefix: "text/plain",
            )
        assert purges.ready is False
        assert purges.abandoned[0]["expected_lease_version"] == 0
    finally:
        service.abort()


@pytest.mark.asyncio
async def test_renew_lost_response_replays_version_before_publish() -> None:
    coordinator = _Coordinator()
    coordinator.renew_lost_once = True
    purges = _Purges()
    service = _service(coordinator, purges)

    async def _slow_stream() -> AsyncIterator[bytes]:
        yield PAYLOAD[:5]
        await coordinator.renew_replayed.wait()
        yield PAYLOAD[5:]

    try:
        record = await asyncio.wait_for(
            service.materialize_stream(
                owner_id=OWNER,
                attachment_id=ATTACHMENT,
                filename=FILENAME,
                category="text",
                extension="txt",
                chunks=_slow_stream(),
                max_bytes=1024,
                resolve_content_type=lambda _prefix: "text/plain",
            ),
            timeout=2,
        )
        assert record == _ready()
        assert coordinator.renew_calls == 2
        assert coordinator.publish_versions == [1]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_publish_replay_cancellation_marks_ready_before_cleanup() -> None:
    coordinator = _Coordinator()
    coordinator.publish_lost_once = True
    coordinator.block_ready_replay = True
    purges = _Purges()
    service = _service(coordinator, purges)
    task = asyncio.create_task(
        service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=_payload_stream(),
            max_bytes=1024,
            resolve_content_type=lambda _prefix: "text/plain",
        )
    )
    try:
        await asyncio.wait_for(coordinator.replay_started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        coordinator.replay_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert purges.abandoned == []
        assert purges.scheduled == [
            {"owner_id": OWNER, "attachment_id": ATTACHMENT}
        ]
    finally:
        coordinator.replay_release.set()
        await service.close()


@pytest.mark.asyncio
async def test_content_type_policy_runs_off_event_loop() -> None:
    coordinator = _Coordinator()
    purges = _Purges()
    service = _service(coordinator, purges)
    entered = threading.Event()
    release = threading.Event()
    loop_progress = asyncio.Event()

    def _blocking_resolver(_prefix: bytes) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "text/plain"

    task = asyncio.create_task(
        service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=_payload_stream(),
            max_bytes=1024,
            resolve_content_type=_blocking_resolver,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        asyncio.get_running_loop().call_soon(loop_progress.set)
        await asyncio.wait_for(loop_progress.wait(), timeout=0.2)
        release.set()
        assert await asyncio.wait_for(task, timeout=2) == _ready()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await service.close()


@pytest.mark.asyncio
async def test_close_during_policy_abandons_without_publishing_ready() -> None:
    coordinator = _Coordinator()
    purges = _Purges()
    service = _service(coordinator, purges)
    entered = threading.Event()
    release = threading.Event()

    def _blocking_resolver(_prefix: bytes) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "text/plain"

    upload = asyncio.create_task(
        service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=_payload_stream(),
            max_bytes=1024,
            resolve_content_type=_blocking_resolver,
        )
    )
    close = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        close = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="closing"):
            await service.materialize_stream(
                owner_id=OWNER,
                attachment_id="new-after-close",
                filename=FILENAME,
                category="text",
                extension="txt",
                chunks=_payload_stream(),
                max_bytes=1024,
                resolve_content_type=lambda _prefix: "text/plain",
            )
        release.set()
        with pytest.raises(RuntimeError, match="closing"):
            await asyncio.wait_for(upload, timeout=2)
        await asyncio.wait_for(close, timeout=2)
        assert coordinator.publish_versions == []
        assert coordinator.ready is None
        assert coordinator.session.staged is not None
        assert coordinator.session.staged.aborted is True
        assert purges.abandoned
    finally:
        release.set()
        if not upload.done():
            upload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await upload
        if close is None:
            await service.close()
        elif not close.done():
            await close


@pytest.mark.asyncio
async def test_concurrent_close_joins_once_and_rethrows_repeated_cancellation() -> None:
    coordinator = _Coordinator()
    purges = _Purges()
    service = _service(coordinator, purges)
    entered = threading.Event()
    release = threading.Event()

    def _blocking_resolver(_prefix: bytes) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "text/plain"

    upload = asyncio.create_task(
        service.materialize_stream(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            filename=FILENAME,
            category="text",
            extension="txt",
            chunks=_payload_stream(),
            max_bytes=1024,
            resolve_content_type=_blocking_resolver,
        )
    )
    first_close = None
    second_close = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first_close = asyncio.create_task(service.close())
        second_close = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        first_close.cancel()
        await asyncio.sleep(0)
        first_close.cancel()
        release.set()
        with pytest.raises(RuntimeError, match="closing"):
            await asyncio.wait_for(upload, timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first_close, timeout=2)
        await asyncio.wait_for(second_close, timeout=2)
        assert service._lifecycle_state == "closed"
        assert coordinator.publish_versions == []
        assert purges.abandoned
    finally:
        release.set()
        if not upload.done():
            upload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await upload
        for close in (first_close, second_close):
            if close is not None and not close.done():
                await close


@pytest.mark.asyncio
async def test_completed_open_conflict_retries_after_heartbeat_version_change() -> None:
    coordinator = _Coordinator()
    purges = _Purges()
    service = _service(coordinator, purges)
    begun = coordinator._begin(
        {
            "attachment_id": ATTACHMENT,
            "owner_id": OWNER,
            "filename": FILENAME,
            "category": "text",
            "extension": "txt",
            "storage_locator": f"{OWNER}/{ATTACHMENT}/{FILENAME}",
            "storage_key": f"{ATTACHMENT}/{FILENAME}",
            "max_bytes": 1024,
            "created_at": 1_786_726_800_000,
            "lease_id": LEASE_ID,
        }
    )
    assert begun.pending is not None
    lease = SimpleNamespace(version=0, failure=None)
    calls = 0

    async def _open(**_values: object) -> _Session:
        nonlocal calls
        calls += 1
        if calls == 1:
            lease.version = 1
            raise RepositoryConflictError("completed stale open")
        return coordinator.session

    coordinator.aopen_pending_materialization_staging = _open  # type: ignore[method-assign]
    heartbeat = asyncio.create_task(asyncio.Event().wait())
    try:
        session = await service._open_staging(
            owner_id=OWNER,
            attachment_id=ATTACHMENT,
            lease_id=LEASE_ID,
            lease=lease,
            heartbeat=heartbeat,
            shutdown=asyncio.Event(),
        )
        assert session is coordinator.session
        assert calls == 2
    finally:
        heartbeat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat
        await service.close()
