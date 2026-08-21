"""Deep policy for durable Plane attachment materialization.

Plane owns the pending row, filesystem reservation, hidden staging bytes, atomic
publication, and exact replay fences.  This module owns product policy around
content sniffing, lease heartbeats, cancellation, and lifecycle admission.  All
publishers use this one application-scoped service; none may publish a blob and
then register metadata as a separate operation.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.artifacts import (
    AttachmentMaterializationState,
    AttachmentRecord,
)

from orchestrator.attachments.blob_access import (
    attachment_storage_key,
    metadata_storage_path,
)

logger = logging.getLogger("Orchestrator.AttachmentMaterialization")

_DEFAULT_LEASE_SECONDS = 120
_DEFAULT_HEARTBEAT_SECONDS = 30.0
_DEFAULT_POLICY_WORKERS = 2
_CONTROL_REPLAY_ATTEMPTS = 3
_STREAM_POLL_SECONDS = 0.05


class AttachmentContentTypeMismatchError(ValueError):
    """The bounded staged prefix does not match the declared extension."""

    def __init__(self, detected_content_type: str) -> None:
        self.detected_content_type = detected_content_type
        super().__init__(detected_content_type)


@dataclass(slots=True)
class _LeaseState:
    version: int
    failure: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _SourceTerminal:
    error: BaseException | None = None


class AttachmentMaterializationService:
    """One lifecycle-bound pending -> staged -> ready attachment publisher."""

    def __init__(
        self,
        *,
        coordinator: Any,
        purge_coordinator: Any,
        clock_milliseconds: Callable[[], int] | None = None,
        uuid_factory: Callable[[], uuid.UUID] | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
        policy_workers: int = _DEFAULT_POLICY_WORKERS,
    ) -> None:
        if coordinator is None:
            raise TypeError("attachment materialization requires the Plane coordinator")
        if purge_coordinator is None:
            raise TypeError("attachment materialization requires durable purge")
        if isinstance(lease_seconds, bool) or not 30 <= lease_seconds <= 86_400:
            raise ValueError("attachment materialization lease must be 30..86400 seconds")
        if not 0.1 <= heartbeat_seconds < lease_seconds / 2:
            raise ValueError("attachment heartbeat must be positive and below half the lease")
        if (
            isinstance(policy_workers, bool)
            or not isinstance(policy_workers, int)
            or not 1 <= policy_workers <= 4
        ):
            raise ValueError("attachment policy workers must be between 1 and 4")
        self._coordinator = coordinator
        self._purges = purge_coordinator
        self._clock_milliseconds = clock_milliseconds or (
            lambda: int(time.time() * 1000)
        )
        self._uuid_factory = uuid_factory or uuid.uuid4
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._policy_executor = ThreadPoolExecutor(
            max_workers=policy_workers,
            thread_name_prefix="attachment-materialization-policy",
        )
        self._lifecycle = threading.Condition()
        self._lifecycle_state = "open"
        self._active_operations = 0
        self._shutdown_signals: set[asyncio.Event] = set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def coordinator(self) -> Any:
        return self._coordinator

    async def materialize_stream(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        filename: str,
        category: str,
        extension: str,
        chunks: AsyncIterable[bytes],
        max_bytes: int,
        resolve_content_type: Callable[[bytes], str],
        created_at: int | None = None,
    ) -> AttachmentRecord:
        """Publish one async stream with bounded sniffing and durable cleanup."""

        shutdown = asyncio.Event()
        self._begin_operation(shutdown)
        try:
            return await self._materialize_stream_admitted(
                owner_id=owner_id,
                attachment_id=attachment_id,
                filename=filename,
                category=category,
                extension=extension,
                chunks=chunks,
                max_bytes=max_bytes,
                resolve_content_type=resolve_content_type,
                created_at=created_at,
                shutdown=shutdown,
            )
        finally:
            self._end_operation(shutdown)

    async def _materialize_stream_admitted(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        filename: str,
        category: str,
        extension: str,
        chunks: AsyncIterable[bytes],
        max_bytes: int,
        resolve_content_type: Callable[[bytes], str],
        created_at: int | None,
        shutdown: asyncio.Event,
    ) -> AttachmentRecord:
        lease_id = f"deep-{self._uuid_factory()}"
        observed_at = self._observed_milliseconds(created_at)
        storage_key = attachment_storage_key(attachment_id, filename)
        storage_locator = metadata_storage_path(owner_id, storage_key)
        begin_values = {
            "attachment_id": attachment_id,
            "owner_id": owner_id,
            "filename": filename,
            "category": category,
            "extension": extension,
            "storage_locator": storage_locator,
            "storage_key": storage_key,
            "max_bytes": max_bytes,
            "created_at": observed_at,
            "lease_id": lease_id,
            "lease_seconds": self._lease_seconds,
        }
        try:
            begun, cancellation = await self._abegin_with_exact_replay(
                begin_values=begin_values,
                attachment_id=attachment_id,
            )
        except BaseException:
            try:
                await self._abandon_pending(
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=0,
                )
            except BaseException:
                logger.warning(
                    "attachment begin ambiguity cleanup did not resolve immediately",
                    extra={"attachment_id": attachment_id},
                    exc_info=True,
                )
            raise
        if begun.state is AttachmentMaterializationState.READY:
            assert begun.ready is not None
            if cancellation is not None or shutdown.is_set():
                await self._schedule_committed_cancellation(
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                )
                if cancellation is not None:
                    raise cancellation
                raise RuntimeError("attachment materialization is closing")
            return begun.ready
        assert begun.pending is not None
        lease = _LeaseState(version=begun.pending.lease_version)
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                lease=lease,
                stop=stop_heartbeat,
                shutdown=shutdown,
            ),
            name=f"attachment-heartbeat:{attachment_id}",
        )
        session = None
        staged = None
        published = False
        try:
            if cancellation is not None:
                raise cancellation
            session = await self._open_staging(
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                lease=lease,
                heartbeat=heartbeat,
                shutdown=shutdown,
            )
            staged = await self._stage_stream(
                session=session,
                chunks=chunks,
                heartbeat=heartbeat,
                lease=lease,
                shutdown=shutdown,
                attachment_id=attachment_id,
            )
            session = None
            prepublication_failure = _stream_interrupt(heartbeat, lease, shutdown)
            if prepublication_failure is not None:
                raise prepublication_failure
            content_type, cancellation = await _join_task_through_cancellation(
                asyncio.create_task(
                    self._run_policy(
                        _resolve_staged_content_type,
                        staged=staged,
                        resolver=resolve_content_type,
                    ),
                    name=f"attachment-sniff:{attachment_id}",
                )
            )
            if cancellation is not None:
                raise cancellation
            prepublication_failure = _stream_interrupt(heartbeat, lease, shutdown)
            if prepublication_failure is not None:
                raise prepublication_failure

            stop_heartbeat.set()
            heartbeat_cancellation = await _join_heartbeat(heartbeat, lease)
            if heartbeat_cancellation is not None:
                raise heartbeat_cancellation
            if lease.failure is not None:
                raise lease.failure
            if shutdown.is_set():
                raise RuntimeError("attachment materialization is closing")
            publish_task = asyncio.create_task(
                self._coordinator.apublish_pending_materialization(
                    staged=staged,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=lease.version,
                    content_type=content_type,
                ),
                name=f"attachment-publish:{attachment_id}",
            )
            record, publish_error, publish_cancellation = (
                await _observe_task_through_cancellation(publish_task)
            )
            if publish_error is not None:
                try:
                    record, replay_cancellation = await self._resolve_publish_ambiguity(
                        begin_values=begin_values,
                        original_error=publish_error,
                        attachment_id=attachment_id,
                    )
                except BaseException:
                    if publish_cancellation is not None:
                        raise publish_cancellation
                    raise
                publish_cancellation = publish_cancellation or replay_cancellation
            assert record is not None
            published = True
            staged_evidence = staged.evidence
            staged = None
            try:
                record = _require_exact_ready_record(
                    record,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    filename=filename,
                    category=category,
                    extension=extension,
                    storage_locator=storage_locator,
                    evidence=staged_evidence,
                    content_type=content_type,
                )
            except BaseException:
                await self._schedule_committed_cancellation(
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                )
                raise
            if publish_cancellation is not None or shutdown.is_set():
                await self._schedule_committed_cancellation(
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                )
                if publish_cancellation is not None:
                    raise publish_cancellation
                raise RuntimeError("attachment materialization is closing")
            return record
        except BaseException:
            stop_heartbeat.set()
            cleanup_errors: list[BaseException] = []
            try:
                await _join_heartbeat(heartbeat, lease)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                if staged is not None:
                    await self._abort_staged(staged)
                elif session is not None:
                    await _join_task_through_cancellation(
                        asyncio.create_task(session.aabort())
                    )
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if not published:
                try:
                    await self._abandon_pending(
                        owner_id=owner_id,
                        attachment_id=attachment_id,
                        lease_id=lease_id,
                        expected_lease_version=lease.version,
                    )
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                logger.error(
                    "attachment failure cleanup reported %d additional error(s)",
                    len(cleanup_errors),
                    extra={
                        "attachment_id": attachment_id,
                        "cleanup_error_type": type(cleanup_errors[0]).__name__,
                    },
                )
            raise

    def materialize_bytes(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        filename: str,
        category: str,
        extension: str,
        chunks: Iterable[bytes],
        max_bytes: int,
        resolve_content_type: Callable[[bytes], str],
        created_at: int | None = None,
    ) -> AttachmentRecord:
        """Synchronous small-payload twin used by already-threaded tool calls."""

        self._begin_operation(None)
        begun = None
        session = None
        staged = None
        published = False
        try:
            lease_id = f"deep-{self._uuid_factory()}"
            observed_at = self._observed_milliseconds(created_at)
            storage_key = attachment_storage_key(attachment_id, filename)
            storage_locator = metadata_storage_path(owner_id, storage_key)
            begin_values = {
                "attachment_id": attachment_id,
                "owner_id": owner_id,
                "filename": filename,
                "category": category,
                "extension": extension,
                "storage_locator": storage_locator,
                "storage_key": storage_key,
                "max_bytes": max_bytes,
                "created_at": observed_at,
                "lease_id": lease_id,
                "lease_seconds": self._lease_seconds,
            }
            try:
                begun = self._begin_with_exact_replay(begin_values=begin_values)
            except BaseException:
                try:
                    self._purges.abandon_pending_materialization(
                        owner_id=owner_id,
                        attachment_id=attachment_id,
                        lease_id=lease_id,
                        expected_lease_version=0,
                    )
                except BaseException:
                    logger.warning(
                        "attachment begin ambiguity cleanup did not resolve immediately",
                        extra={"attachment_id": attachment_id},
                        exc_info=True,
                    )
                raise
            if begun.state is AttachmentMaterializationState.READY:
                assert begun.ready is not None
                return begun.ready
            assert begun.pending is not None
            version = begun.pending.lease_version
            session = self._coordinator.open_pending_materialization_staging(
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                expected_lease_version=version,
            )
            staged = session.write_chunks(chunks)
            session = None
            content_type = resolve_content_type(staged.read_prefix(max_bytes=8192))
            if not isinstance(content_type, str) or not content_type:
                raise ValueError("attachment content-type resolver returned no type")
            publish_error: BaseException | None = None
            try:
                record = self._coordinator.publish_pending_materialization(
                    staged=staged,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=version,
                    content_type=content_type,
                )
            except BaseException as exc:
                publish_error = exc
                record = self._resolve_publish_ambiguity_sync(
                    begin_values=begin_values,
                    original_error=exc,
                )
            published = True
            staged_evidence = staged.evidence
            staged = None
            try:
                return _require_exact_ready_record(
                    record,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    filename=filename,
                    category=category,
                    extension=extension,
                    storage_locator=storage_locator,
                    evidence=staged_evidence,
                    content_type=content_type,
                )
            except BaseException:
                self._purges.schedule_attachment(
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                )
                if publish_error is not None:
                    logger.error(
                        "attachment publish ambiguity resolved to mismatched READY metadata",
                        extra={"attachment_id": attachment_id},
                    )
                raise
        except BaseException:
            cleanup_errors: list[BaseException] = []
            try:
                if staged is not None:
                    staged.abort()
                elif session is not None:
                    session.abort()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if (
                not published
                and begun is not None
                and begun.state is AttachmentMaterializationState.PENDING
            ):
                try:
                    self._purges.abandon_pending_materialization(
                        owner_id=owner_id,
                        attachment_id=attachment_id,
                        lease_id=lease_id,
                        expected_lease_version=begun.pending.lease_version,
                    )
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                logger.error(
                    "attachment failure cleanup reported %d additional error(s)",
                    len(cleanup_errors),
                    extra={
                        "attachment_id": attachment_id,
                        "cleanup_error_type": type(cleanup_errors[0]).__name__,
                    },
                )
            raise
        finally:
            self._end_operation(None)

    async def _heartbeat(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        lease: _LeaseState,
        stop: asyncio.Event,
        shutdown: asyncio.Event,
    ) -> None:
        try:
            while not stop.is_set() and not shutdown.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
                    return
                except TimeoutError:
                    pass
                if shutdown.is_set():
                    return
                renewed, cancellation = await self._arenew_with_exact_replay(
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=lease.version,
                )
                expected_version = lease.version + 1
                if (
                    renewed.owner_id != owner_id
                    or renewed.attachment_id != attachment_id
                    or renewed.lease_id != lease_id
                    or renewed.lease_version != expected_version
                ):
                    raise RuntimeError("attachment lease replay returned mismatched authority")
                lease.version = renewed.lease_version
                if cancellation is not None:
                    raise cancellation
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            lease.failure = exc

    async def _open_staging(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        lease: _LeaseState,
        heartbeat: asyncio.Task[None],
        shutdown: asyncio.Event,
    ) -> Any:
        while True:
            expected = lease.version
            task = asyncio.create_task(
                self._coordinator.aopen_pending_materialization_staging(
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=expected,
                ),
                name=f"attachment-open:{attachment_id}",
            )
            try:
                while not task.done():
                    if lease.failure is not None or heartbeat.done() or shutdown.is_set():
                        opened, _cancellation = await _cancel_and_join_task(task)
                        if opened is not None:
                            await _join_task_through_cancellation(
                                asyncio.create_task(opened.aabort())
                            )
                        if lease.failure is not None:
                            raise lease.failure
                        raise RuntimeError("attachment materialization is closing")
                    try:
                        return await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
                    except TimeoutError:
                        continue
                return task.result()
            except asyncio.CancelledError:
                opened, _cancellation = await _cancel_and_join_task(task)
                if opened is not None:
                    await _join_task_through_cancellation(
                        asyncio.create_task(opened.aabort())
                    )
                raise
            except RepositoryConflictError:
                if lease.failure is None and lease.version != expected:
                    continue
                raise

    async def _stage_stream(
        self,
        *,
        session: Any,
        chunks: AsyncIterable[bytes],
        heartbeat: asyncio.Task[None],
        lease: _LeaseState,
        shutdown: asyncio.Event,
        attachment_id: str,
    ) -> Any:
        queue: asyncio.Queue[bytes | _SourceTerminal] = asyncio.Queue(maxsize=1)
        source_task = asyncio.create_task(
            self._pump_source(
                chunks=chunks,
                queue=queue,
                attachment_id=attachment_id,
            ),
            name=f"attachment-source:{attachment_id}",
        )
        task = asyncio.create_task(
            session.awrite_chunks(
                self._queued_chunks(
                    queue=queue,
                    heartbeat=heartbeat,
                    lease=lease,
                    shutdown=shutdown,
                    attachment_id=attachment_id,
                )
            ),
            name=f"attachment-stage:{attachment_id}",
        )
        try:
            while not task.done():
                if lease.failure is not None or heartbeat.done() or shutdown.is_set():
                    await self._stop_stage_then_source(task, source_task)
                    failure = _stream_interrupt(heartbeat, lease, shutdown)
                    assert failure is not None
                    raise failure
                if source_task.done():
                    _source_result, source_error, _source_cancellation = (
                        await _observe_task_through_cancellation(source_task)
                    )
                    if source_error is not None:
                        await self._stop_stage_then_source(task, source_task)
                        raise source_error
                try:
                    await asyncio.wait({task}, timeout=_STREAM_POLL_SECONDS)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            await self._stop_stage_then_source(task, source_task)
            raise
        try:
            staged = task.result()
        except BaseException as stage_error:
            try:
                await _cancel_and_join_task(source_task)
            except BaseException:
                logger.error(
                    "attachment source cleanup failed after stage failure",
                    extra={"attachment_id": attachment_id},
                    exc_info=True,
                )
            raise stage_error
        try:
            while not source_task.done():
                failure = _stream_interrupt(heartbeat, lease, shutdown)
                if failure is not None:
                    cleanup_error = await self._abort_staged_then_source(
                        staged,
                        source_task,
                    )
                    if cleanup_error is not None:
                        logger.error(
                            "attachment staged/source cleanup failed during interruption",
                            extra={
                                "attachment_id": attachment_id,
                                "cleanup_error_type": type(cleanup_error).__name__,
                            },
                        )
                    raise failure
                await asyncio.wait({source_task}, timeout=_STREAM_POLL_SECONDS)
        except asyncio.CancelledError:
            cleanup_error = await self._abort_staged_then_source(staged, source_task)
            if cleanup_error is not None:
                logger.error(
                    "attachment staged/source cleanup failed during cancellation",
                    extra={
                        "attachment_id": attachment_id,
                        "cleanup_error_type": type(cleanup_error).__name__,
                    },
                )
            raise
        _result, source_error, _source_cancellation = (
            await _observe_task_through_cancellation(source_task)
        )
        if source_error is not None:
            try:
                await self._abort_staged(staged)
            except BaseException:
                logger.error(
                    "attachment staged cleanup failed after source failure",
                    extra={"attachment_id": attachment_id},
                    exc_info=True,
                )
            raise source_error
        return staged

    async def _abort_staged_then_source(
        self,
        staged: Any,
        source_task: asyncio.Task[Any],
    ) -> BaseException | None:
        """Release owner exclusion, then always join arbitrary source cleanup."""

        cleanup_error: BaseException | None = None
        try:
            await self._abort_staged(staged)
        except BaseException as error:
            cleanup_error = error
        try:
            await _cancel_and_join_task(source_task)
        except BaseException as error:
            cleanup_error = cleanup_error or error
        return cleanup_error

    async def _stop_stage_then_source(
        self,
        stage_task: asyncio.Task[Any],
        source_task: asyncio.Task[Any],
    ) -> None:
        """Release Plane owner exclusion before joining arbitrary source cleanup."""

        stage_error: BaseException | None = None
        try:
            completed, _cancellation = await _cancel_and_join_task(stage_task)
            if completed is not None:
                await self._abort_staged(completed)
        except BaseException as error:
            stage_error = error
        source_error: BaseException | None = None
        try:
            await _cancel_and_join_task(source_task)
        except BaseException as error:
            source_error = error
        if stage_error is not None:
            raise stage_error
        if source_error is not None:
            raise source_error

    async def _abort_staged(self, staged: Any) -> None:
        await _join_task_through_cancellation(
            asyncio.create_task(self._run_policy(staged.abort))
        )

    async def _pump_source(
        self,
        *,
        chunks: AsyncIterable[bytes],
        queue: asyncio.Queue[bytes | _SourceTerminal],
        attachment_id: str,
    ) -> None:
        """Own source iteration/close independently from Plane's stage capability."""

        iterator: Any = None
        next_task: asyncio.Future[bytes] | None = None
        try:
            iterator = chunks.__aiter__()
            while True:
                next_task = asyncio.ensure_future(anext(iterator))
                if isinstance(next_task, asyncio.Task):
                    next_task.set_name(f"attachment-source-next:{attachment_id}")
                try:
                    chunk = await next_task
                    await queue.put(chunk)
                except StopAsyncIteration:
                    await queue.put(_SourceTerminal())
                    return
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        await _cancel_and_join_task(next_task)
                        raise
                    await queue.put(_SourceTerminal(asyncio.CancelledError()))
                    return
                except BaseException as error:
                    await queue.put(_SourceTerminal(error))
                    return
                finally:
                    next_task = None
        finally:
            if next_task is not None:
                await _cancel_and_join_task(next_task)
            close = getattr(iterator, "aclose", None) if iterator is not None else None
            if callable(close):
                await _join_task_through_cancellation(
                    asyncio.create_task(
                        close(),
                        name=f"attachment-source-close:{attachment_id}",
                    )
                )

    async def _queued_chunks(
        self,
        *,
        queue: asyncio.Queue[bytes | _SourceTerminal],
        heartbeat: asyncio.Task[None],
        lease: _LeaseState,
        shutdown: asyncio.Event,
        attachment_id: str,
    ) -> AsyncIterable[bytes]:
        """Feed Plane without making its owner lock depend on source cleanup."""

        while True:
            failure = _stream_interrupt(heartbeat, lease, shutdown)
            if failure is not None:
                raise failure
            item_task = asyncio.create_task(
                queue.get(),
                name=f"attachment-source-queue:{attachment_id}",
            )
            try:
                while not item_task.done():
                    failure = _stream_interrupt(heartbeat, lease, shutdown)
                    if failure is not None:
                        await _cancel_and_join_task(item_task)
                        raise failure
                    await asyncio.wait({item_task}, timeout=_STREAM_POLL_SECONDS)
                item = item_task.result()
            except asyncio.CancelledError:
                await _cancel_and_join_task(item_task)
                raise
            if isinstance(item, _SourceTerminal):
                if item.error is not None:
                    raise item.error
                return
            yield item

    async def _abegin_with_exact_replay(
        self,
        *,
        begin_values: dict[str, object],
        attachment_id: str,
    ) -> tuple[Any, asyncio.CancelledError | None]:
        original_error: BaseException | None = None
        cancellation: asyncio.CancelledError | None = None
        for attempt in range(_CONTROL_REPLAY_ATTEMPTS):
            result, error, observed_cancellation = (
                await _observe_task_through_cancellation(
                    asyncio.create_task(
                        self._coordinator.abegin_pending_materialization(**begin_values),
                        name=f"attachment-begin:{attachment_id}:{attempt}",
                    )
                )
            )
            cancellation = cancellation or observed_cancellation
            if error is None:
                return result, cancellation
            original_error = original_error or error
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                break
        if cancellation is not None:
            raise cancellation
        assert original_error is not None
        raise original_error

    def _begin_with_exact_replay(
        self,
        *,
        begin_values: dict[str, object],
    ) -> Any:
        original_error: BaseException | None = None
        for _attempt in range(_CONTROL_REPLAY_ATTEMPTS):
            try:
                return self._coordinator.begin_pending_materialization(**begin_values)
            except BaseException as error:
                original_error = original_error or error
                if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    break
        assert original_error is not None
        raise original_error

    async def _arenew_with_exact_replay(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> tuple[Any, asyncio.CancelledError | None]:
        original_error: BaseException | None = None
        cancellation: asyncio.CancelledError | None = None
        values = {
            "owner_id": owner_id,
            "attachment_id": attachment_id,
            "lease_id": lease_id,
            "expected_lease_version": expected_lease_version,
            "lease_seconds": self._lease_seconds,
        }
        for attempt in range(_CONTROL_REPLAY_ATTEMPTS):
            result, error, observed_cancellation = (
                await _observe_task_through_cancellation(
                    asyncio.create_task(
                        self._coordinator.arenew_pending_materialization(**values),
                        name=f"attachment-renew:{attachment_id}:{attempt}",
                    )
                )
            )
            cancellation = cancellation or observed_cancellation
            if error is None:
                return result, cancellation
            original_error = original_error or error
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                break
        if cancellation is not None:
            raise cancellation
        assert original_error is not None
        raise original_error

    async def _resolve_publish_ambiguity(
        self,
        *,
        begin_values: dict[str, object],
        original_error: BaseException,
        attachment_id: str,
    ) -> tuple[AttachmentRecord, asyncio.CancelledError | None]:
        try:
            replay, cancellation = await self._abegin_with_exact_replay(
                begin_values=begin_values,
                attachment_id=attachment_id,
            )
        except BaseException:
            raise original_error
        if replay.state is AttachmentMaterializationState.READY:
            assert replay.ready is not None
            return replay.ready, cancellation
        if cancellation is not None:
            raise cancellation
        raise original_error

    def _resolve_publish_ambiguity_sync(
        self,
        *,
        begin_values: dict[str, object],
        original_error: BaseException,
    ) -> AttachmentRecord:
        try:
            replay = self._begin_with_exact_replay(begin_values=begin_values)
        except BaseException:
            raise original_error
        if replay.state is AttachmentMaterializationState.READY:
            assert replay.ready is not None
            return replay.ready
        raise original_error

    async def _abandon_pending(self, **values: object) -> None:
        task = asyncio.create_task(
            self._purges.aabandon_pending_materialization(**values),
            name=f"attachment-abandon:{values.get('attachment_id', '<unknown>')}",
        )
        _result, cancellation = await _join_task_through_cancellation(task)
        if cancellation is not None:
            raise cancellation

    async def _schedule_committed_cancellation(
        self,
        *,
        owner_id: str,
        attachment_id: str,
    ) -> None:
        task = asyncio.create_task(
            self._purges.aschedule_attachment(
                owner_id=owner_id,
                attachment_id=attachment_id,
            ),
            name=f"attachment-cancel-purge:{attachment_id}",
        )
        await _join_task_through_cancellation(task)

    async def _run_policy(self, function: Callable[..., Any], /, **kwargs: object) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._policy_executor,
            lambda: function(**kwargs),
        )

    async def close(self) -> None:
        """Reject new uploads, interrupt active streams, and join policy workers."""

        loop = asyncio.get_running_loop()
        with self._lifecycle:
            task = self._close_task
            if task is None:
                if self._lifecycle_state == "closed":
                    return
                if self._lifecycle_state != "open":
                    raise RuntimeError("attachment materialization is closing")
                self._lifecycle_state = "closing"
                for signal in tuple(self._shutdown_signals):
                    loop.call_soon_threadsafe(signal.set)
                task = loop.create_task(
                    self._close_admitted(),
                    name="attachment-materialization-close",
                )
                self._close_task = task
        _result, cancellation = await _join_task_through_cancellation(task)
        if cancellation is not None:
            raise cancellation

    async def _close_admitted(self) -> None:
        try:
            while True:
                with self._lifecycle:
                    if self._active_operations == 0:
                        break
                await asyncio.sleep(0.01)
            self._policy_executor.shutdown(wait=False, cancel_futures=False)
        finally:
            with self._lifecycle:
                self._lifecycle_state = "closed"
                self._lifecycle.notify_all()

    def abort(self) -> None:
        """Close an unbound service during synchronous composition rollback."""

        with self._lifecycle:
            if self._lifecycle_state == "closed":
                return
            if self._lifecycle_state != "open" or self._active_operations:
                raise RuntimeError("active attachment materialization requires async close")
            self._lifecycle_state = "closing"
        self._policy_executor.shutdown(wait=True, cancel_futures=True)
        with self._lifecycle:
            self._lifecycle_state = "closed"
            self._lifecycle.notify_all()

    def _begin_operation(self, shutdown: asyncio.Event | None) -> None:
        with self._lifecycle:
            if self._lifecycle_state != "open":
                raise RuntimeError("attachment materialization is closing")
            self._active_operations += 1
            if shutdown is not None:
                self._shutdown_signals.add(shutdown)

    def _end_operation(self, shutdown: asyncio.Event | None) -> None:
        with self._lifecycle:
            if shutdown is not None:
                self._shutdown_signals.discard(shutdown)
            self._active_operations -= 1
            if self._active_operations < 0:
                raise RuntimeError("attachment materialization accounting underflow")
            if self._active_operations == 0:
                self._lifecycle.notify_all()

    def _observed_milliseconds(self, supplied: int | None) -> int:
        value = self._clock_milliseconds() if supplied is None else supplied
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("attachment creation time must be non-negative milliseconds")
        return value


async def _join_heartbeat(
    task: asyncio.Task[None],
    lease: _LeaseState,
) -> asyncio.CancelledError | None:
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError as exc:
            return exc
        return None
    _result, cancellation = await _join_task_through_cancellation(task)
    if lease.failure is not None:
        return cancellation
    return cancellation


def _resolve_staged_content_type(
    *,
    staged: Any,
    resolver: Callable[[bytes], str],
) -> str:
    prefix = staged.read_prefix(max_bytes=8192)
    content_type = resolver(prefix)
    if not isinstance(content_type, str) or not content_type:
        raise ValueError("attachment content-type resolver returned no type")
    return content_type


def _require_exact_ready_record(
    record: AttachmentRecord,
    *,
    owner_id: str,
    attachment_id: str,
    filename: str,
    category: str,
    extension: str,
    storage_locator: str,
    evidence: Any,
    content_type: str,
) -> AttachmentRecord:
    expected = (
        owner_id,
        attachment_id,
        filename,
        category,
        extension,
        storage_locator,
        evidence.size_bytes,
        evidence.sha256,
        content_type,
        None,
    )
    observed = (
        record.owner_id,
        record.attachment_id,
        record.filename,
        record.category,
        record.extension,
        record.storage_locator,
        record.size_bytes,
        record.sha256,
        record.content_type,
        record.deleted_at,
    )
    if observed != expected:
        raise RuntimeError("attachment publication returned mismatched READY metadata")
    return record


def _stream_interrupt(
    heartbeat: asyncio.Task[None],
    lease: _LeaseState,
    shutdown: asyncio.Event,
) -> BaseException | None:
    if lease.failure is not None:
        return lease.failure
    if shutdown.is_set():
        return RuntimeError("attachment materialization is closing")
    if not heartbeat.done():
        return None
    try:
        heartbeat.result()
    except asyncio.CancelledError as error:
        return error
    except BaseException as error:
        return error
    return RuntimeError("attachment materialization heartbeat stopped unexpectedly")


async def _cancel_and_join_task(
    task: asyncio.Future[Any],
) -> tuple[Any | None, asyncio.CancelledError | None]:
    if not task.done():
        task.cancel()
    result, error, cancellation = await _observe_task_through_cancellation(task)
    if error is not None and not isinstance(error, asyncio.CancelledError):
        raise error
    return result, cancellation


async def _observe_task_through_cancellation(
    task: asyncio.Future[Any],
) -> tuple[Any | None, BaseException | None, asyncio.CancelledError | None]:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.done():
                break
            cancellation = cancellation or error
        except BaseException:
            break
    try:
        return task.result(), None, cancellation
    except BaseException as error:
        return None, error, cancellation


async def _join_task_through_cancellation(
    task: asyncio.Task[Any],
) -> tuple[Any, asyncio.CancelledError | None]:
    result, error, cancellation = await _observe_task_through_cancellation(task)
    if error is not None:
        raise error
    return result, cancellation


def materialization_service_from_orchestrator(
    orchestrator: Any,
) -> AttachmentMaterializationService:
    """Resolve the single application service with explicit test injection."""

    injected = getattr(orchestrator, "attachment_materialization_service", None)
    if injected is not None:
        return injected
    composition = getattr(orchestrator, "runtime_composition", None)
    plane = getattr(composition, "plane", None)
    service = getattr(plane, "attachment_materializer", None)
    if service is None:
        raise RuntimeError("attachment materialization is not initialized")
    return service


__all__ = (
    "AttachmentContentTypeMismatchError",
    "AttachmentMaterializationService",
    "materialization_service_from_orchestrator",
)
