"""Lease-renewed PostgreSQL dispatch loop that materializes, claims, and runs due
scheduled occurrences through scheduler/store.py and work_admission.py, independently
renewing the occurrence claim and operation fence leases per handler.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Optional

from agentic_settings import SCHEDULER_TICK_SECONDS
from orchestrator.work_admission import (
    OperationState,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)

from .store import (
    OccurrenceClaim,
    ScheduledAdmissionRefusedError,
    ScheduledAttempt,
    StaleOccurrenceClaimError,
)


logger = logging.getLogger("scheduler.loop")


class ClaimLeaseKeeper:
    def __init__(self, store, claim: OccurrenceClaim, *, lease_seconds: int) -> None:
        if lease_seconds < 5 or lease_seconds > 60:
            raise ValueError("scheduled claim lease must be between 5 and 60 seconds")
        self.store = store
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.interval_seconds = lease_seconds / 3
        self.lost = asyncio.Event()
        self.successful_renewals = 0
        self.renewal_monotonic: list[float] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            task = self._task
            self._task = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set() and not self.lost.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                continue
            except asyncio.TimeoutError:
                pass
            try:
                renewed_until = await asyncio.to_thread(
                    self.store.renew_claim,
                    self.claim,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                logger.exception(
                    "scheduler claim renewal failed",
                    extra={"occurrence_id": str(self.claim.occurrence_id)},
                )
                self.lost.set()
                return
            if renewed_until is None:
                logger.warning(
                    "scheduler.stale_occurrence_claim",
                    extra={"occurrence_id": str(self.claim.occurrence_id)},
                )
                self.lost.set()
                return
            self.successful_renewals += 1
            self.renewal_monotonic.append(time.monotonic())


class OperationLeaseKeeper:
    _RENEWAL_DIVISOR = 4

    def __init__(self, coordinator: WorkAdmissionCoordinator, fence) -> None:
        lease_seconds = coordinator.slot_lease.total_seconds()
        if lease_seconds <= 0:  # pragma: no cover
            raise ValueError("operation execution lease must be positive")
        self.coordinator = coordinator
        self.fence = fence
        self.interval_seconds = lease_seconds / self._RENEWAL_DIVISOR
        self.lost = asyncio.Event()
        self.successful_renewals = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            task = self._task
            self._task = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop.is_set() and not self.lost.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds
                )
                continue
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(
                    self.coordinator.renew_execution_lease,
                    self.fence,
                )
            except asyncio.CancelledError:
                raise
            except StaleExecutionFenceError:
                logger.warning(
                    "scheduler.operation_execution_lease_lost",
                    extra={"operation_id": str(self.fence.operation_id)},
                )
                self.lost.set()
                return
            except Exception:
                logger.exception(
                    "scheduler operation execution lease renewal failed",
                    extra={"operation_id": str(self.fence.operation_id)},
                )
                self.lost.set()
                return
            self.successful_renewals += 1


class SchedulerLoop:
    _HANDLER_CANCEL_GRACE_SECONDS = 0.25

    def __init__(
        self,
        store,
        runner,
        task_manager=None,
        *,
        coordinator: WorkAdmissionCoordinator | None = None,
        instance_id: str | None = None,
        claim_lease_seconds: int | None = None,
        claim_limit: int = 20,
    ) -> None:
        self.store = store
        self.runner = runner
        self.task_manager = task_manager
        if coordinator is None and task_manager is not None:
            require = getattr(task_manager, "_require_coordinator", None)
            if callable(require):
                try:
                    coordinator = require()
                except Exception:
                    coordinator = None
        self.coordinator = coordinator
        if self.coordinator is not None:
            self.store.bind_coordinator(self.coordinator)
            bind = getattr(self.runner, "bind_execution_context", None)
            if callable(bind):
                bind(coordinator=self.coordinator, store=self.store)
        self.instance_id = instance_id or f"scheduler-{uuid.uuid4()}"
        if claim_lease_seconds is None:
            claim_lease_seconds = int(os.getenv("SCHEDULED_CLAIM_LEASE_SECONDS", "15"))
        if claim_lease_seconds < 5 or claim_lease_seconds > 60:
            raise ValueError("SCHEDULED_CLAIM_LEASE_SECONDS must be between 5 and 60")
        if claim_limit <= 0 or claim_limit > 1_000:
            raise ValueError("scheduler claim_limit must be between 1 and 1000")
        self.claim_lease_seconds = claim_lease_seconds
        self.claim_limit = claim_limit
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._handler_remainders: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is not None:
            return
        if self.coordinator is None:
            try:
                reconciled = self.store.reconcile_interrupted()
                if reconciled:
                    logger.info(
                        "scheduler reconciled %s interrupted legacy run(s)",
                        reconciled,
                    )
            except Exception:  # pragma: no cover
                logger.warning("scheduler reconcile failed", exc_info=True)
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        logger.info(
            "scheduler loop started",
            extra={
                "tick_seconds": SCHEDULER_TICK_SECONDS,
                "claim_lease_seconds": self.claim_lease_seconds,
                "durable_occurrences": self.coordinator is not None,
            },
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
        pending = tuple(self._dispatch_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._dispatch_tasks.clear()
        remainders = tuple(self._handler_remainders)
        for task in remainders:
            task.cancel()
        if remainders:
            _, still_running = await asyncio.wait(
                remainders,
                timeout=self._HANDLER_CANCEL_GRACE_SECONDS,
            )
            if still_running:
                logger.error(
                    "scheduler shutdown retained fenced handler remainders",
                    extra={"remainder_count": len(still_running)},
                )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:  # pragma: no cover
                logger.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=SCHEDULER_TICK_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        if self.coordinator is None:
            await self._tick_legacy()
            return
        claims = await asyncio.to_thread(
            self.store.materialize_and_claim_due,
            self.instance_id,
            limit=self.claim_limit,
            lease_seconds=self.claim_lease_seconds,
            eligibility=self.runner.assess_job,
        )
        for claim in claims:
            task = asyncio.create_task(self._dispatch_claim(claim))
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)

    async def _tick_legacy(self) -> None:
        now_ms = int(time.time() * 1000)
        due = self.store.list_due(now_ms)
        for job in due:
            try:
                await self.task_manager.submit(
                    job.get("target_chat_id") or f"job:{job['id']}",
                    job["user_id"],
                    self._job_coro,
                    job,
                )
            except Exception:  # pragma: no cover
                logger.exception("failed to dispatch legacy job %s", job.get("id"))

    async def _dispatch_claim(self, claim: OccurrenceClaim) -> None:
        keeper = ClaimLeaseKeeper(
            self.store, claim, lease_seconds=self.claim_lease_seconds
        )
        keeper.start()
        operation_keeper: OperationLeaseKeeper | None = None
        attempt: ScheduledAttempt | None = None
        run_task: asyncio.Task | None = None
        lost_task: asyncio.Task | None = None
        operation_lost_task: asyncio.Task | None = None
        try:
            attempt = await asyncio.to_thread(self.store.allocate_attempt, claim)
            while attempt.execution_fence is None:
                if keeper.lost.is_set() or self._stop.is_set():
                    await asyncio.to_thread(self.store.claim_attempt_execution, attempt)
                    return
                selected = await asyncio.to_thread(
                    self.store.claim_attempt_execution, attempt
                )
                if selected is not None:
                    attempt = selected
                    break
                try:
                    await asyncio.wait_for(
                        keeper.lost.wait(),
                        timeout=min(0.25, self.claim_lease_seconds / 12),
                    )
                except asyncio.TimeoutError:
                    pass
            if keeper.lost.is_set():
                await self._terminalize_attempt(
                    attempt,
                    state=OperationState.RETRYABLE,
                    code="claim_lost",
                    summary="Scheduled claim lost before start",
                )
                return

            attempt = await asyncio.to_thread(
                self.store.start_attempt,
                attempt,
                lease_seconds=self.claim_lease_seconds,
            )
            operation_keeper = OperationLeaseKeeper(
                self.coordinator, attempt.execution_fence
            )
            operation_keeper.start()
            run_task = asyncio.create_task(
                self.runner.run_occurrence(attempt, claim_lost=keeper.lost)
            )
            lost_task = asyncio.create_task(keeper.lost.wait())
            operation_lost_task = asyncio.create_task(
                operation_keeper.lost.wait()
            )
            done, _ = await asyncio.wait(
                {run_task, lost_task, operation_lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            claim_lost = lost_task in done and keeper.lost.is_set()
            operation_lost = (
                operation_lost_task in done
                and operation_keeper.lost.is_set()
            )
            if claim_lost or operation_lost:
                await self._cancel_handler_bounded(
                    run_task,
                    reason=(
                        "claim_lost" if claim_lost else "operation_lease_lost"
                    ),
                )
                loss_code = (
                    "claim_lost" if claim_lost else "operation_lease_lost"
                )
                try:
                    await asyncio.to_thread(
                        self.store.mark_claim_retryable,
                        attempt.claim,
                        error_code=loss_code,
                        retry_after_seconds=1,
                    )
                except StaleOccurrenceClaimError:
                    pass
                except Exception:
                    logger.exception(
                        "scheduler authority-loss recovery failed",
                        extra={
                            "occurrence_id": str(claim.occurrence_id),
                            "result_code": loss_code,
                        },
                    )
                await self._terminalize_attempt(
                    attempt,
                    state=OperationState.RETRYABLE,
                    code=loss_code,
                    summary=(
                        "Scheduled claim lost during execution"
                        if claim_lost
                        else "Scheduled operation execution lease lost"
                    ),
                )
                return
            lost_task.cancel()
            operation_lost_task.cancel()
            await asyncio.gather(
                lost_task,
                operation_lost_task,
                return_exceptions=True,
            )
            result = await run_task
            if keeper.lost.is_set():
                await self._terminalize_attempt(
                    attempt,
                    state=OperationState.RETRYABLE,
                    code="claim_lost",
                    summary="Scheduled result refused after claim loss",
                )
                return

            await asyncio.to_thread(
                self.store.finish_attempt,
                attempt,
                outcome=result.outcome,
                summary=result.summary,
                auth_ref=result.auth_ref,
                retryable=result.retryable,
                result_code=result.result_code,
                retry_after_seconds=result.retry_after_seconds,
            )
            terminal_state = (
                OperationState.RETRYABLE
                if result.retryable
                else (
                    OperationState.COMPLETED
                    if result.outcome == "success"
                    else OperationState.FAILED
                )
            )
            await self._terminalize_attempt(
                attempt,
                state=terminal_state,
                code=None
                if terminal_state is OperationState.COMPLETED
                else result.result_code,
                summary=result.summary,
            )
        except ScheduledAdmissionRefusedError:
            logger.warning(
                "scheduler operation admission refused",
                extra={"occurrence_id": str(claim.occurrence_id)},
            )
        except StaleOccurrenceClaimError:
            if attempt is not None:
                await self._terminalize_attempt(
                    attempt,
                    state=OperationState.RETRYABLE,
                    code="claim_lost",
                    summary="Scheduled claim became stale",
                )
        except StaleExecutionFenceError:
            # Lease-keeper race: this is retryable loss, never a failure
            if attempt is not None:
                try:
                    await asyncio.to_thread(
                        self.store.mark_claim_retryable,
                        attempt.claim,
                        error_code="operation_lease_lost",
                        retry_after_seconds=1,
                    )
                except StaleOccurrenceClaimError:
                    pass
                except Exception:
                    logger.exception(
                        "scheduler finish-fence recovery failed",
                        extra={
                            "occurrence_id": str(claim.occurrence_id),
                            "result_code": "operation_lease_lost",
                        },
                    )
                await self._terminalize_attempt(
                    attempt,
                    state=OperationState.RETRYABLE,
                    code="operation_lease_lost",
                    summary="Scheduled operation execution lease lost",
                )
        except asyncio.CancelledError:
            if run_task is not None and not run_task.done():
                await self._cancel_handler_bounded(
                    run_task,
                    reason="service_draining",
                )
            children = tuple(
                task
                for task in (lost_task, operation_lost_task)
                if task is not None and not task.done()
            )
            for child in children:
                child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            if attempt is not None:
                try:
                    await asyncio.to_thread(
                        self.store.mark_claim_retryable,
                        attempt.claim,
                        error_code="service_draining",
                        retry_after_seconds=1,
                    )
                except Exception:
                    pass
                await self._terminalize_attempt(
                    attempt,
                    state=OperationState.RETRYABLE,
                    code="service_draining",
                    summary="Scheduler service is draining",
                )
            raise
        except Exception:
            logger.exception(
                "scheduled occurrence failed",
                extra={"occurrence_id": str(claim.occurrence_id)},
            )
            if attempt is not None and attempt.run_id is not None:
                try:
                    await asyncio.to_thread(
                        self.store.finish_attempt,
                        attempt,
                        outcome="failure",
                        summary="Scheduled operation failed",
                        result_code="operation_failed",
                    )
                except Exception:
                    pass
            if attempt is not None:
                await self._terminalize_attempt(
                    attempt,
                    state=OperationState.FAILED,
                    code="operation_failed",
                    summary="Scheduled operation failed",
                )
        finally:
            if operation_keeper is not None:
                await operation_keeper.stop()
            await keeper.stop()

    async def _cancel_handler_bounded(
        self,
        task: asyncio.Task,
        *,
        reason: str,
    ) -> bool:
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return True
        task.cancel()
        done, _ = await asyncio.wait(
            {task},
            timeout=self._HANDLER_CANCEL_GRACE_SECONDS,
        )
        if task in done:
            await asyncio.gather(task, return_exceptions=True)
            return True
        self._handler_remainders.add(task)
        task.add_done_callback(self._handler_remainder_done)
        logger.error(
            "scheduler handler resisted cancellation and remains fenced",
            extra={"reason_code": reason},
        )
        return False

    def _handler_remainder_done(self, task: asyncio.Task) -> None:
        self._handler_remainders.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(
                "fenced scheduler handler remainder ended with an error",
                exc_info=True,
            )

    async def _terminalize_attempt(
        self,
        attempt: ScheduledAttempt,
        *,
        state: OperationState,
        code: str | None,
        summary: str | None,
    ) -> None:
        if self.coordinator is None or attempt.execution_fence is None:
            return
        try:
            await asyncio.to_thread(
                self.coordinator.terminalize,
                attempt.execution_fence,
                state=state,
                terminal_code=code,
                safe_summary=(summary or "Scheduled operation finished")[:512],
                retry_after_ms=0 if state is OperationState.RETRYABLE else None,
            )
        except StaleExecutionFenceError:
            pass

    async def _job_coro(self, virtual_ws, job):
        return await self.runner.run_job(job)
