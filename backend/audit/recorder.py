"""
Async-friendly audit Recorder used by every authority boundary.

Design (research.md §R9):

* Best-effort synchronous: a successful call returns the inserted DTO so
  callers can immediately fan it out over WebSocket. The Recorder
  itself does not block its caller on transient DB errors — those are
  caught, the event is appended to a disk-backed retry queue, and the
  call returns ``None`` (with a warning logged). A background drain
  task replays the queue once the DB recovers.
* Never raises into the caller's hot path on a transient failure. The
  only time ``record(...)`` raises is when the caller hands it an
  invalid ``AuditEventCreate`` (e.g. payload-shaped data was inlined).
  That is a programmer error and should fail loudly.
* Single point of fan-out: after a successful insert, the recorder
  invokes the registered publisher (set by the orchestrator at
  startup) to deliver the new event over the user's WebSocket
  connection (FR-010).

The retry queue is a JSONL file under ``backend/audit/retry_queue/``;
one line per pending event. The drain task processes the file at
startup and on a 30-second timer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .repository import AuditRepository
from .schemas import AuditEventCreate, AuditEventDTO

logger = logging.getLogger("Audit.Recorder")

PublisherFn = Callable[[AuditEventDTO, str], Awaitable[None]]


def _retry_queue_path() -> Path:
    base = Path(os.path.dirname(os.path.abspath(__file__))) / "retry_queue"
    base.mkdir(parents=True, exist_ok=True)
    return base / "pending.jsonl"


class Recorder:
    """Public façade over the audit repository.

    Construction is synchronous (no I/O). Optionally attach a publisher
    via :meth:`set_publisher` so each successful insert fans out over
    the user's WebSocket. The drain task is started lazily on the first
    record (so unit tests that don't need it pay nothing).
    """

    def __init__(self, repository: AuditRepository, *, retry_queue: Optional[Path] = None):
        self._repo = repository
        self._publisher: Optional[PublisherFn] = None
        self._retry_path = retry_queue or _retry_queue_path()
        self._retry_lock = threading.Lock()
        self._drain_task: Optional[asyncio.Task] = None
        self._drain_stop: Optional[asyncio.Event] = None
        self._close_task: Optional[asyncio.Task] = None
        self._operation_condition = threading.Condition()
        self._active_operations = 0
        self._closing = False
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_publisher(self, publisher: Optional[PublisherFn]) -> None:
        """Register a coroutine that delivers the new event over WebSocket.

        Called once by the orchestrator after startup. ``None`` clears
        the publisher (used in tests).
        """
        self._publisher = publisher

    async def record(self, event: AuditEventCreate) -> Optional[AuditEventDTO]:
        """Persist an audit event and (best-effort) fan it out.

        Returns the inserted DTO on success, or ``None`` if the insert
        failed and was queued for retry. Never raises on transient DB
        errors. Raises ``pydantic.ValidationError`` if ``event`` itself
        is malformed (callers should construct a valid model).
        """
        self._begin_operation()
        try:
            try:
                # Lazy-start the drain loop so plain unit tests pay nothing.
                self._ensure_drain_task()
                dto = await asyncio.to_thread(self._repo.insert, event)
            except Exception as exc:
                logger.warning(
                    "Audit insert failed (%s) — queuing for retry. event=%s/%s user=%s",
                    exc.__class__.__name__, event.event_class, event.action_type,
                    event.actor_user_id,
                )
                self._enqueue_retry(event)
                return None

            if self._publisher is not None:
                try:
                    await self._publisher(dto, event.actor_user_id)
                except Exception as exc:  # pragma: no cover — never block
                    logger.warning("Audit publisher failed: %s", exc)
            return dto
        finally:
            self._end_operation()

    def record_blocking(self, event: AuditEventCreate) -> Optional[AuditEventDTO]:
        """Synchronous variant for callers that aren't on the event loop.

        Used by the auth-lifecycle hook and other handlers that do not
        run inside ``asyncio``. Skips publisher fan-out (the caller is
        outside the event loop, so we cannot safely schedule a coroutine).
        """
        self._begin_operation()
        try:
            try:
                return self._repo.insert(event)
            except Exception as exc:
                logger.warning(
                    "Audit insert failed (%s) — queuing for retry. event=%s/%s user=%s",
                    exc.__class__.__name__, event.event_class, event.action_type,
                    event.actor_user_id,
                )
                self._enqueue_retry(event)
                return None
        finally:
            self._end_operation()

    async def close(self) -> None:
        """Stop retry work and join every in-flight repository operation."""

        task = self._close_task
        if task is None:
            if self._closed:
                return
            task = asyncio.create_task(
                self._close_once(),
                name="audit-recorder-close",
            )
            self._close_task = task
        await _join_task_through_cancellation(task)

    async def _close_once(self) -> None:
        with self._operation_condition:
            self._closing = True
        stop = self._drain_stop
        if stop is not None:
            stop.set()
        drain = self._drain_task
        if drain is not None:
            await drain
        await asyncio.to_thread(self._wait_for_active_operations)
        self._publisher = None
        self._closed = True

    def _begin_operation(self) -> None:
        with self._operation_condition:
            if self._closing or self._closed:
                raise RuntimeError("audit recorder is closing")
            self._active_operations += 1

    def _end_operation(self) -> None:
        with self._operation_condition:
            self._active_operations -= 1
            if self._active_operations == 0:
                self._operation_condition.notify_all()

    def _wait_for_active_operations(self) -> None:
        with self._operation_condition:
            while self._active_operations:
                self._operation_condition.wait()

    # ------------------------------------------------------------------
    # Retry queue (disk-backed, JSONL)
    # ------------------------------------------------------------------

    def _enqueue_retry(self, event: AuditEventCreate) -> None:
        line = event.model_dump_json() + "\n"
        with self._retry_lock:
            try:
                with self._retry_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception as exc:  # pragma: no cover — disk full etc
                logger.error("Audit retry-queue write failed: %s — event lost", exc)

    def _ensure_drain_task(self) -> None:
        if self._closing or self._closed:
            return
        if self._drain_task is not None and not self._drain_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._drain_stop = asyncio.Event()
        self._drain_task = loop.create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        stop = self._drain_stop
        if stop is None:
            return
        while not stop.is_set():
            try:
                await self._drain_once()
            except Exception as exc:  # pragma: no cover
                logger.warning("Audit drain loop iteration failed: %s", exc)
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass

    async def _drain_once(self) -> None:
        """Replay the retry queue. Lossy on parse errors (logged + skipped)."""
        with self._retry_lock:
            if not self._retry_path.exists():
                return
            try:
                lines = self._retry_path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:  # pragma: no cover
                logger.warning("Audit retry-queue read failed: %s", exc)
                return
            # Truncate immediately; we'll re-append anything that still fails.
            self._retry_path.write_text("", encoding="utf-8")
        if not lines:
            return
        survivors: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                event = AuditEventCreate.model_validate(payload)
            except Exception as exc:
                logger.error("Dropping unparseable retry-queue entry: %s", exc)
                continue
            try:
                dto = await asyncio.to_thread(self._repo.insert, event)
            except Exception as exc:
                logger.debug("Audit retry still failing (%s): %s", exc.__class__.__name__, exc)
                survivors.append(line)
                continue
            if self._publisher is not None:
                try:
                    await self._publisher(dto, event.actor_user_id)
                except Exception as exc:  # pragma: no cover
                    logger.debug("Audit retry publisher failed: %s", exc)
        if survivors:
            with self._retry_lock:
                with self._retry_path.open("a", encoding="utf-8") as fh:
                    for s in survivors:
                        fh.write(s + "\n")


# ---------------------------------------------------------------------------
# Module-level singleton, initialised by the orchestrator
# ---------------------------------------------------------------------------

_RECORDER: Optional[Recorder] = None


def get_recorder() -> Optional[Recorder]:
    """Return the process-wide Recorder instance, or ``None`` if not yet wired."""
    return _RECORDER


def set_recorder(recorder: Optional[Recorder]) -> None:
    global _RECORDER
    _RECORDER = recorder


async def _join_task_through_cancellation(task: asyncio.Task) -> None:
    """Observe one shared close task before propagating cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    task.result()
    if cancellation is not None:
        raise cancellation


# ---------------------------------------------------------------------------
# Convenience helpers used by the orchestrator integration code
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    """Return the current UTC time. Used as ``started_at`` / ``completed_at``."""
    return datetime.now(timezone.utc)


def make_correlation_id() -> str:
    """Return a new correlation id for paired in_progress→terminal entries."""
    import uuid
    return str(uuid.uuid4())
