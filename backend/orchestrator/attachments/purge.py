"""Deep policy around Plane-owned durable attachment purge mechanics.

Plane owns the tombstone repository, caller-owned transactions, streaming
blob deletion, absence verification, and fenced retry transitions.  Deep owns
when a user-visible attachment or account namespace is logically deleted,
whether the resulting physical purge is complete, and the lifecycle of the
bounded recovery loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from astralplane import (
    DurablePurgeExecutor,
    PostgresPurgeStore,
    PurgeAttemptResult,
    PurgeAttemptState,
    PurgeScheduleResult,
    create_durable_purge_executor,
)
from astralplane.errors import PlaneError, SQLContractError
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)

logger = logging.getLogger("Orchestrator.AttachmentPurge")

_DEFAULT_RETRY_DELAY = timedelta(seconds=30)
_DEFAULT_RECONCILE_INTERVAL_SECONDS = 30.0
_DEFAULT_RECONCILE_LIMIT = 100
_DEFAULT_STARTUP_RECONCILE_MAX_ITEMS = 1000
_DEFAULT_STARTUP_RECONCILE_TIMEOUT_SECONDS = 30.0
_DEFAULT_SCHEDULE_WORKERS = 4


class AttachmentPurgeReadinessError(RuntimeError):
    """The composed purge boundary cannot currently prove physical convergence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AccountRetirementNeedsReconciliation(RuntimeError):
    """Assignments were fenced; unresolved effects prevent account erasure."""

    def __init__(self, unresolved_action_count: int) -> None:
        self.unresolved_action_count = unresolved_action_count
        super().__init__("account_retirement_reconciliation_required")


@dataclass(frozen=True, slots=True)
class AttachmentPurgeOutcome:
    """One committed logical deletion and its post-commit physical result."""

    schedule: PurgeScheduleResult
    attempt: PurgeAttemptResult

    @property
    def completed(self) -> bool:
        return self.attempt.state in {
            PurgeAttemptState.PURGED,
            PurgeAttemptState.ALREADY_PURGED,
        }

    @property
    def metadata_rows_soft_deleted(self) -> int:
        return self.schedule.metadata_rows_soft_deleted


@dataclass(frozen=True, slots=True)
class AttachmentPurgeAcceptance:
    """A logical deletion whose physical cleanup is durably pending.

    Request paths return this value after the owner-scoped metadata mutation
    and purge tombstone commit.  They deliberately do not wait for filesystem
    exclusion held by a concurrent upload, and therefore must not describe the
    attachment as physically absent yet.
    """

    schedule: PurgeScheduleResult

    @property
    def cleanup_id(self) -> str:
        """Opaque, replay-stable identifier safe to return to the owner."""

        return str(self.schedule.tombstone.tombstone_id)

    @property
    def metadata_rows_soft_deleted(self) -> int:
        return self.schedule.metadata_rows_soft_deleted


@dataclass(frozen=True, slots=True)
class AttachmentPurgeStatus:
    """Owner-safe view of one durable cleanup tombstone."""

    cleanup_id: str
    status: str
    requested_at: datetime
    attempt_count: int
    verified_absent_at: datetime | None
    last_error_code: str | None


class AttachmentPurgeCoordinator:
    """Compose atomic logical deletion with bounded physical reconciliation."""

    def __init__(
        self,
        *,
        plane_runtime: Any,
        purge_repository: PostgresPurgeStore,
        blobs: Any,
        executor: DurablePurgeExecutor | None = None,
        clock: Callable[[], datetime] | None = None,
        retry_delay: timedelta = _DEFAULT_RETRY_DELAY,
        reconcile_interval_seconds: float = _DEFAULT_RECONCILE_INTERVAL_SECONDS,
        reconcile_limit: int = _DEFAULT_RECONCILE_LIMIT,
        startup_reconcile_max_items: int = _DEFAULT_STARTUP_RECONCILE_MAX_ITEMS,
        startup_reconcile_timeout_seconds: float = (
            _DEFAULT_STARTUP_RECONCILE_TIMEOUT_SECONDS
        ),
        schedule_workers: int = _DEFAULT_SCHEDULE_WORKERS,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if plane_runtime is None:
            raise TypeError("attachment purge requires the application Plane runtime")
        if purge_repository is None:
            raise TypeError("attachment purge requires the Plane purge repository")
        if blobs is None:
            raise TypeError("attachment purge requires the application Plane blob store")
        if not isinstance(retry_delay, timedelta) or not (
            timedelta(seconds=1) <= retry_delay <= timedelta(hours=1)
        ):
            raise ValueError("attachment purge retry delay must be between 1 and 3600 seconds")
        if not 1.0 <= reconcile_interval_seconds <= 3600.0:
            raise ValueError(
                "attachment purge reconciliation interval must be between 1 and 3600 seconds"
            )
        if (
            isinstance(reconcile_limit, bool)
            or not isinstance(reconcile_limit, int)
            or not 1 <= reconcile_limit <= 1000
        ):
            raise ValueError("attachment purge reconciliation limit must be between 1 and 1000")
        if (
            isinstance(startup_reconcile_max_items, bool)
            or not isinstance(startup_reconcile_max_items, int)
            or not 1 <= startup_reconcile_max_items <= 100_000
        ):
            raise ValueError(
                "attachment purge startup item budget must be between 1 and 100000"
            )
        if not 0.1 <= startup_reconcile_timeout_seconds <= 600.0:
            raise ValueError(
                "attachment purge startup time budget must be between 0.1 and 600 seconds"
            )
        if (
            isinstance(schedule_workers, bool)
            or not isinstance(schedule_workers, int)
            or not 1 <= schedule_workers <= 16
        ):
            raise ValueError("attachment purge scheduling workers must be between 1 and 16")

        self._runtime = plane_runtime
        self._repository = purge_repository
        self._executor = executor or create_durable_purge_executor(
            database=plane_runtime,
            purge_store=purge_repository,
            blobs=blobs,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._retry_delay = retry_delay
        self._reconcile_interval_seconds = float(reconcile_interval_seconds)
        self._reconcile_limit = reconcile_limit
        self._startup_reconcile_max_items = startup_reconcile_max_items
        self._startup_reconcile_timeout_seconds = float(
            startup_reconcile_timeout_seconds
        )
        self._monotonic = monotonic_clock or time.monotonic
        self._state_lock = threading.Lock()
        self._state_epoch = 0
        self._ready = False
        self._readiness_code = "purge_reconciliation_not_run"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Request-time PostgreSQL scheduling and potentially blocking physical
        # purge must never share the event loop's default executor with staged
        # blob writes.  Otherwise enough delete waiters can starve the upload
        # worker that owns the filesystem lock they are waiting to acquire.
        self._schedule_executor = ThreadPoolExecutor(
            max_workers=schedule_workers,
            thread_name_prefix="attachment-purge-schedule",
        )
        self._reconcile_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="attachment-purge-reconcile",
        )
        self._lifecycle = threading.Condition()
        self._lifecycle_state = "open"
        self._active_operations = 0
        self._close_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        with self._state_lock:
            return self._ready

    @property
    def readiness_code(self) -> str | None:
        with self._state_lock:
            return self._readiness_code

    @property
    def started(self) -> bool:
        return self._task is not None

    def assert_ready(self) -> None:
        """Fail closed when any non-purged tombstone or recovery fault remains."""

        with self._state_lock:
            if self._ready:
                return
            code = self._readiness_code or "purge_reconciliation_incomplete"
        raise AttachmentPurgeReadinessError(code)

    def assert_globally_ready(self, transaction: Any) -> None:
        """Revalidate process-local readiness against Plane's durable aggregate.

        A different process can commit a purge tombstone without changing this
        coordinator's in-memory epoch.  Readiness probes therefore supply their
        existing short Plane transaction here and must not rely on the cached
        state alone.
        """

        with self._state_lock:
            if not self._ready:
                code = self._readiness_code or "purge_reconciliation_incomplete"
                raise AttachmentPurgeReadinessError(code)
            epoch = self._state_epoch
        try:
            incomplete = self._repository.has_incomplete_for_administration(
                transaction
            )
        except BaseException:
            self._mark_incomplete("purge_reconciliation_failed")
            raise
        if incomplete:
            self._mark_incomplete("purge_reconciliation_incomplete")
            raise AttachmentPurgeReadinessError(
                "purge_reconciliation_incomplete"
            )
        with self._state_lock:
            if self._ready and self._state_epoch == epoch:
                return
            code = self._readiness_code or "purge_reconciliation_incomplete"
        raise AttachmentPurgeReadinessError(code)

    def schedule_attachment(
        self,
        *,
        owner_id: str,
        attachment_id: str,
    ) -> AttachmentPurgeOutcome:
        """Atomically hide one attachment and durably schedule its prefix purge."""

        with self._admitted_operation():
            scheduled, observed_at = self._schedule_attachment_only(
                owner_id=owner_id,
                attachment_id=attachment_id,
            )
            return self._execute_scheduled(scheduled, observed_at=observed_at)

    async def aschedule_attachment(
        self,
        *,
        owner_id: str,
        attachment_id: str,
    ) -> AttachmentPurgeAcceptance:
        """Commit logical deletion on a dedicated lane and wake reconciliation.

        This is the user-visible request contract.  It returns after the
        tombstone commit, never after waiting for an upload's filesystem owner
        lock.  The single bounded reconciler owns physical deletion and absence
        proof, while readiness stays red until that proof converges.
        """

        self._begin_operation()
        try:
            loop = asyncio.get_running_loop()
            worker = loop.run_in_executor(
                self._schedule_executor,
                partial(
                    self._schedule_attachment_only,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                ),
            )
            try:
                scheduled, _observed_at = await _join_worker_through_cancellation(worker)
            except BaseException as exc:
                # Typed validation/not-found failures prove rollback and should
                # not let random-ID spam thrash the physical reconciler.
                # Cancellation and transaction-exit failures may hide a
                # committed tombstone, so they still wake fail-closed recovery.
                if not _is_definite_schedule_rollback(exc):
                    self._wake.set()
                raise
            else:
                # Event semantics make a wake set immediately before the
                # reconciler waits edge-safe rather than lossy.
                self._wake.set()
            return AttachmentPurgeAcceptance(schedule=scheduled)
        finally:
            self._end_operation()

    def _schedule_attachment_only(
        self,
        *,
        owner_id: str,
        attachment_id: str,
    ) -> tuple[PurgeScheduleResult, datetime]:
        """Commit only metadata+tombstone; never touch the filesystem."""

        observed_at = self._now()
        degraded_epoch: int | None = None
        try:
            with self._state_lock:
                # Degrade before the commit-uncertain transaction begins.  If
                # PostgreSQL commits but the connection drops before __exit__ can
                # confirm it, the durable tombstone must never coexist with a
                # stale green readiness state.  Release the local lock before
                # borrowing a Plane connection: readiness probes already own a
                # connection when they inspect this state, so holding both in
                # the inverse order can deadlock a bounded pool.
                self._mark_incomplete_locked("purge_reconciliation_incomplete")
                degraded_epoch = self._state_epoch
            with self._runtime.transaction() as transaction:
                scheduled = self._repository.schedule_attachment_prefix(
                    transaction,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    requested_at=observed_at,
                    deleted_at=_epoch_milliseconds(observed_at),
                )
        except BaseException as exc:
            if degraded_epoch is not None and _is_definite_schedule_rollback(exc):
                self._restore_after_definite_rollback(degraded_epoch)
            raise
        return scheduled, observed_at

    def schedule_owner(
        self,
        *,
        owner_id: str,
    ) -> AttachmentPurgeOutcome:
        """Atomically hide all owner metadata and schedule namespace deletion.

        The authenticated account-retirement endpoint is its only product
        caller.  Logout and identity-provider session events never invoke it.
        """

        with self._admitted_operation():
            scheduled, observed_at = self._schedule_owner_only(owner_id=owner_id)
            return self._execute_scheduled(scheduled, observed_at=observed_at)

    async def aschedule_owner(
        self,
        *,
        owner_id: str,
    ) -> AttachmentPurgeAcceptance:
        """Durably accept one owner cleanup without request-time filesystem work.

        This supports authenticated retirement and reserved verification
        teardown.  It is never reachable from logout or an IAM session event.
        """

        self._begin_operation()
        try:
            loop = asyncio.get_running_loop()
            worker = loop.run_in_executor(
                self._schedule_executor,
                partial(self._schedule_owner_only, owner_id=owner_id),
            )
            try:
                scheduled, _observed_at = await _join_worker_through_cancellation(worker)
            except BaseException as exc:
                if not _is_definite_schedule_rollback(exc):
                    self._wake.set()
                raise
            else:
                self._wake.set()
            return AttachmentPurgeAcceptance(schedule=scheduled)
        finally:
            self._end_operation()

    def owner_cleanup_status(
        self,
        *,
        owner_id: str,
        cleanup_id: str,
    ) -> AttachmentPurgeStatus | None:
        """Read one owner-bound cleanup without exposing blob locators."""

        with self._admitted_operation():
            with self._runtime.transaction() as transaction:
                tombstone = self._repository.load(
                    transaction,
                    owner_id=owner_id,
                    tombstone_id=cleanup_id,
                )
        if tombstone is None:
            return None
        return AttachmentPurgeStatus(
            cleanup_id=str(tombstone.tombstone_id),
            status=str(tombstone.status),
            requested_at=tombstone.requested_at,
            attempt_count=tombstone.attempt_count,
            verified_absent_at=tombstone.verified_absent_at,
            last_error_code=tombstone.last_error_code,
        )

    async def aowner_cleanup_status(
        self,
        *,
        owner_id: str,
        cleanup_id: str,
    ) -> AttachmentPurgeStatus | None:
        """Read cleanup state off the request event loop."""

        loop = asyncio.get_running_loop()
        worker = loop.run_in_executor(
            self._schedule_executor,
            partial(
                self.owner_cleanup_status,
                owner_id=owner_id,
                cleanup_id=cleanup_id,
            ),
        )
        return await _join_worker_through_cancellation(worker)

    def _schedule_owner_only(
        self,
        *,
        owner_id: str,
    ) -> tuple[PurgeScheduleResult, datetime]:
        """Fence owner assignments before scheduling physical namespace cleanup.

        An unresolved external effect is retained for explicit reconciliation.
        Its stop commits even though this call cannot yet accept physical purge.
        """

        observed_at = self._now()
        degraded_epoch: int | None = None
        unresolved_action_count = 0
        scheduled = None
        try:
            with self._runtime.transaction() as transaction:
                catalog = getattr(self._runtime, "repositories", None)
                assignments = getattr(catalog, "assignments", None)
                if assignments is not None:
                    retirement = assignments.retire_owner(transaction, owner_id=owner_id)
                    unresolved_action_count = len(retirement.unresolved_action_ids)
                if not unresolved_action_count:
                    with self._state_lock:
                        self._mark_incomplete_locked("purge_reconciliation_incomplete")
                        degraded_epoch = self._state_epoch
                    scheduled = self._repository.schedule_owner_namespace(
                        transaction,
                        owner_id=owner_id,
                        requested_at=observed_at,
                        deleted_at=_epoch_milliseconds(observed_at),
                    )
        except BaseException as exc:
            if degraded_epoch is not None and _is_definite_schedule_rollback(exc):
                self._restore_after_definite_rollback(degraded_epoch)
            raise
        if unresolved_action_count:
            # Raise only after transaction exit: rolling back would revive the
            # very work the owner has asked to retire.
            raise AccountRetirementNeedsReconciliation(unresolved_action_count)
        return scheduled, observed_at

    def abandon_pending_materialization(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> AttachmentPurgeOutcome:
        """Fence a failed upload intent and durably purge its hidden prefix.

        Callers must first stop and join any staging/publication worker and
        abort the staged blob session.  ``BlobStagedWrite`` holds Plane's
        per-owner exclusion until that abort completes, so attempting physical
        purge before then would self-deadlock.
        """

        with self._admitted_operation():
            scheduled, observed_at = self._abandon_pending_only(
                owner_id=owner_id,
                attachment_id=attachment_id,
                lease_id=lease_id,
                expected_lease_version=expected_lease_version,
            )
            return self._execute_scheduled(scheduled, observed_at=observed_at)

    async def aabandon_pending_materialization(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> AttachmentPurgeAcceptance:
        """Durably abandon one failed upload without waiting for physical purge."""

        self._begin_operation()
        try:
            loop = asyncio.get_running_loop()
            worker = loop.run_in_executor(
                self._schedule_executor,
                partial(
                    self._abandon_pending_only,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=expected_lease_version,
                ),
            )
            try:
                scheduled, _observed_at = await _join_worker_through_cancellation(worker)
            except BaseException as exc:
                if not _is_definite_schedule_rollback(exc):
                    self._wake.set()
                raise
            else:
                self._wake.set()
            return AttachmentPurgeAcceptance(schedule=scheduled)
        finally:
            self._end_operation()

    def _abandon_pending_only(
        self,
        *,
        owner_id: str,
        attachment_id: str,
        lease_id: str,
        expected_lease_version: int,
    ) -> tuple[PurgeScheduleResult, datetime]:
        """Commit a pending-row tombstone without touching the filesystem."""

        observed_at = self._now()
        degraded_epoch: int | None = None
        try:
            with self._state_lock:
                # Abandonment has the same commit-uncertainty boundary as a
                # user-requested deletion.  Degrade before opening the
                # transaction, then restore only after a typed failure proves
                # rollback and the durable global aggregate is clean.
                self._mark_incomplete_locked("purge_reconciliation_incomplete")
                degraded_epoch = self._state_epoch
            with self._runtime.transaction() as transaction:
                scheduled = self._repository.abandon_pending_materialization(
                    transaction,
                    owner_id=owner_id,
                    attachment_id=attachment_id,
                    lease_id=lease_id,
                    expected_lease_version=expected_lease_version,
                    deleted_at=_epoch_milliseconds(observed_at),
                )
        except BaseException as exc:
            if degraded_epoch is not None and _is_definite_schedule_rollback(exc):
                self._restore_after_definite_rollback(degraded_epoch)
            raise
        return scheduled, observed_at

    def reconcile_once(
        self,
        *,
        fail_on_incomplete: bool = False,
        limit: int | None = None,
    ) -> tuple[PurgeAttemptResult, ...]:
        """Reconcile one bounded batch and prove no non-purged work is hidden."""

        with self._admitted_operation():
            return self._reconcile_once_admitted(
                fail_on_incomplete=fail_on_incomplete,
                limit=limit,
            )

    async def areconcile_once(
        self,
        *,
        fail_on_incomplete: bool = False,
        limit: int | None = None,
    ) -> tuple[PurgeAttemptResult, ...]:
        """Run one physical reconciliation pass only on the dedicated lane."""

        self._begin_operation()
        try:
            loop = asyncio.get_running_loop()
            worker = loop.run_in_executor(
                self._reconcile_executor,
                partial(
                    self._reconcile_once_admitted,
                    fail_on_incomplete=fail_on_incomplete,
                    limit=limit,
                ),
            )
            return await _join_worker_through_cancellation(worker)
        finally:
            self._end_operation()

    def _reconcile_once_admitted(
        self,
        *,
        fail_on_incomplete: bool = False,
        limit: int | None = None,
    ) -> tuple[PurgeAttemptResult, ...]:
        """Implementation for one already-admitted reconciliation pass."""

        batch_limit = self._reconcile_limit if limit is None else limit
        if (
            isinstance(batch_limit, bool)
            or not isinstance(batch_limit, int)
            or not 1 <= batch_limit <= self._reconcile_limit
        ):
            raise ValueError(
                "attachment purge batch limit must be within the configured limit"
            )
        observed_at = self._now()
        try:
            # Convert expired hidden upload intents to deterministic prefix
            # tombstones before discovering ready purge work.  Mark local
            # readiness incomplete while still inside the commit-uncertain
            # caller transaction, exactly like user-visible delete scheduling.
            with self._runtime.transaction() as transaction:
                recovered_materializations = (
                    self._repository
                    .schedule_expired_pending_materializations_for_administration(
                        transaction,
                        limit=batch_limit,
                    )
                )
                # Take the local lock only after the Plane transaction already
                # owns its connection, matching readiness-probe lock order.  It
                # still happens before transaction exit so a lost commit
                # response cannot leave process readiness green.
                with self._state_lock:
                    if recovered_materializations:
                        self._mark_incomplete_locked(
                            "purge_reconciliation_incomplete"
                        )
                    epoch = self._state_epoch
            results = self._executor.reconcile_ready_for_administration(
                observed_at=observed_at,
                retry_at=observed_at + self._retry_delay,
                limit=batch_limit,
            )
            with self._runtime.transaction() as transaction:
                incomplete = self._repository.has_incomplete_for_administration(
                    transaction
                )
        except BaseException:
            self._mark_incomplete("purge_reconciliation_failed")
            raise

        failed = any(result.state is PurgeAttemptState.FAILED for result in results)
        if incomplete or failed:
            self._mark_incomplete("purge_reconciliation_incomplete")
            if fail_on_incomplete:
                raise AttachmentPurgeReadinessError(
                    "purge_reconciliation_incomplete"
                )
        else:
            self._set_ready_if_unchanged(epoch)
        return tuple(results)

    def reconcile_startup(self) -> tuple[PurgeAttemptResult, ...]:
        """Drain ready work within budgets while keeping readiness fail-closed.

        Delayed, manual-review, failing, or over-budget work intentionally
        leaves this coordinator unready.  Startup still returns so the single
        bounded background loop can continue making progress; actual executor
        or database exceptions still escape and abort composition.
        """

        deadline = self._monotonic() + self._startup_reconcile_timeout_seconds
        remaining = self._startup_reconcile_max_items
        observed: list[PurgeAttemptResult] = []
        while remaining > 0 and self._monotonic() < deadline:
            batch_limit = min(self._reconcile_limit, remaining)
            batch = self.reconcile_once(limit=batch_limit)
            observed.extend(batch)
            remaining -= len(batch)
            if self.ready or not batch:
                break
            if any(result.state is PurgeAttemptState.FAILED for result in batch):
                break
        return tuple(observed)

    def start(self) -> asyncio.Task[None]:
        """Start the single bounded recovery loop on the active event loop."""

        with self._lifecycle:
            if self._lifecycle_state != "open":
                raise RuntimeError("attachment purge coordinator is closing")
            if self._task is not None:
                return self._task
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run_forever(),
                name="attachment-purge-reconciler",
            )
            return self._task

    async def stop(self) -> None:
        """Wake and join the recovery loop exactly once."""

        self._stop.set()
        self._wake.set()
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
            if self._task is task:
                self._task = None

    async def close(self) -> None:
        """Atomically reject new work, join admitted work, and release lanes."""

        loop = asyncio.get_running_loop()
        with self._lifecycle:
            task = self._close_task
            if task is None:
                if self._lifecycle_state == "closed":
                    return
                if self._lifecycle_state != "open":
                    raise RuntimeError("attachment purge coordinator is closing")
                self._lifecycle_state = "closing"
                # Reject new admissions before the first await and wake any
                # sleeping recovery pass.  Work admitted before this point is
                # tracked and joined below.
                self._stop.set()
                self._wake.set()
                task = loop.create_task(
                    self._close_admitted_operations(),
                    name="attachment-purge-close",
                )
                self._close_task = task
        await _join_worker_through_cancellation(task)

    async def _close_admitted_operations(self) -> None:
        try:
            task = self._task
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
                if self._task is task:
                    self._task = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._schedule_executor,
                self._wait_for_admitted_operations,
            )
            self._reconcile_executor.shutdown(wait=True, cancel_futures=False)
            self._schedule_executor.shutdown(wait=True, cancel_futures=False)
        finally:
            with self._lifecycle:
                self._lifecycle_state = "closed"
                self._lifecycle.notify_all()

    def abort(self) -> None:
        """Release an unstarted coordinator during composition rollback."""

        with self._lifecycle:
            if self._lifecycle_state == "closed":
                return
            if self._lifecycle_state != "open" or self.started:
                raise RuntimeError("started attachment purge coordinator requires async close")
            if self._active_operations:
                raise RuntimeError("active attachment purge operations require async close")
            self._lifecycle_state = "closing"
        self._schedule_executor.shutdown(wait=True, cancel_futures=True)
        self._reconcile_executor.shutdown(wait=True, cancel_futures=True)
        with self._lifecycle:
            self._lifecycle_state = "closed"
            self._lifecycle.notify_all()

    async def _run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await loop.run_in_executor(
                    self._reconcile_executor,
                    self.reconcile_once,
                )
            except Exception as exc:
                logger.warning(
                    "attachment_purge_reconciliation_failed",
                    extra={"reason": getattr(exc, "code", type(exc).__name__)},
                )
            if self._stop.is_set():
                break
            if self._wake.is_set():
                continue
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._reconcile_interval_seconds,
                )
            except TimeoutError:
                pass

    def _execute_scheduled(
        self,
        scheduled: PurgeScheduleResult,
        *,
        observed_at: datetime,
    ) -> AttachmentPurgeOutcome:
        try:
            attempt = self._executor.execute(
                owner_id=scheduled.tombstone.owner_id,
                tombstone_id=scheduled.tombstone.tombstone_id,
                now=observed_at,
                retry_at=observed_at + self._retry_delay,
            )
        except BaseException:
            self._mark_incomplete("purge_reconciliation_failed")
            raise
        outcome = AttachmentPurgeOutcome(schedule=scheduled, attempt=attempt)
        if not outcome.completed:
            self._mark_incomplete("purge_reconciliation_incomplete")
        else:
            self._refresh_readiness()
        return outcome

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("attachment purge clock must return an aware datetime")
        return value.astimezone(UTC)

    def _begin_operation(self) -> None:
        with self._lifecycle:
            if self._lifecycle_state != "open":
                raise RuntimeError("attachment purge coordinator is closing")
            self._active_operations += 1

    def _end_operation(self) -> None:
        with self._lifecycle:
            if self._active_operations <= 0:
                raise RuntimeError("attachment purge operation accounting underflow")
            self._active_operations -= 1
            if self._active_operations == 0:
                self._lifecycle.notify_all()

    @contextmanager
    def _admitted_operation(self) -> Iterator[None]:
        self._begin_operation()
        try:
            yield
        finally:
            self._end_operation()

    def _wait_for_admitted_operations(self) -> None:
        with self._lifecycle:
            while self._active_operations:
                self._lifecycle.wait()

    def _readiness_epoch(self) -> int:
        with self._state_lock:
            return self._state_epoch

    def _mark_incomplete(self, code: str) -> None:
        with self._state_lock:
            self._mark_incomplete_locked(code)

    def _mark_incomplete_locked(self, code: str) -> None:
        self._state_epoch += 1
        self._ready = False
        self._readiness_code = code

    def _set_ready_if_unchanged(self, epoch: int) -> None:
        with self._state_lock:
            if self._state_epoch != epoch:
                return
            self._ready = True
            self._readiness_code = None

    def _refresh_readiness(self) -> None:
        epoch = self._readiness_epoch()
        try:
            with self._runtime.transaction() as transaction:
                incomplete = self._repository.has_incomplete_for_administration(
                    transaction
                )
        except BaseException:
            self._mark_incomplete("purge_reconciliation_failed")
            raise
        if incomplete:
            self._mark_incomplete("purge_reconciliation_incomplete")
        else:
            self._set_ready_if_unchanged(epoch)

    def _restore_after_definite_rollback(self, degraded_epoch: int) -> None:
        """Undo conservative degradation only after a durable absence proof.

        Typed validation/not-found failures occur before a purge tombstone can
        be committed and the transaction manager has already completed its
        rollback when they escape the context.  Re-query the global aggregate
        so random or foreign attachment IDs cannot pin process readiness red.
        The original degradation epoch prevents that proof from overriding a
        different thread's newly scheduled work.
        """

        try:
            with self._runtime.transaction() as transaction:
                incomplete = self._repository.has_incomplete_for_administration(
                    transaction
                )
        except BaseException:
            self._mark_incomplete("purge_reconciliation_failed")
            return
        if incomplete:
            self._mark_incomplete("purge_reconciliation_incomplete")
        else:
            self._set_ready_if_unchanged(degraded_epoch)


def purge_coordinator_from_orchestrator(orchestrator: Any) -> AttachmentPurgeCoordinator:
    """Resolve the one composed coordinator, with explicit test injection."""

    injected = getattr(orchestrator, "attachment_purge_coordinator", None)
    if injected is not None:
        return injected
    composition = getattr(orchestrator, "runtime_composition", None)
    plane = getattr(composition, "plane", None)
    coordinator = getattr(plane, "attachment_purges", None)
    if coordinator is None:
        raise RuntimeError("the application attachment purge coordinator is not initialized")
    return coordinator


def _epoch_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _is_definite_schedule_rollback(exc: BaseException) -> bool:
    """Return whether a typed schedule failure proves no commit occurred."""

    if isinstance(
        exc,
        (
            SQLContractError,
            RepositoryConflictError,
            RepositoryValidationError,
            RepositoryNotFoundError,
        ),
    ):
        return True
    return isinstance(exc, PlaneError) and exc.code == "purge_object_not_found"


async def _join_worker_through_cancellation(future: asyncio.Future[Any]) -> Any:
    """Observe one executor call before propagating even repeated cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = future.result()
    if cancellation is not None:
        raise cancellation
    return result


__all__ = (
    "AccountRetirementNeedsReconciliation",
    "AttachmentPurgeAcceptance",
    "AttachmentPurgeCoordinator",
    "AttachmentPurgeOutcome",
    "AttachmentPurgeReadinessError",
    "AttachmentPurgeStatus",
    "purge_coordinator_from_orchestrator",
)
