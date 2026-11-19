"""Runs one due scheduled job: dreaming maintenance executes under the scheduler's claim,
idempotency and notification gates, and every other job is refused with turn_engine_retired.
Called by scheduler/loop.py against store.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Optional

from orchestrator.work_admission import WorkAdmissionCoordinator
from orchestrator.tool_permissions import VALID_SCOPES

from .cron import compute_next_run_ms
from .store import (
    ScheduledAttempt,
    StaleOccurrenceClaimError,
)

logger = logging.getLogger("scheduler.runner")

_EFFECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REVIEWED_EFFECT_KINDS = frozenset(
    {
        "audit_record",
        "chat_history",
        "chat_message",
        "downstream_request",
        "history_publish",
        "maintenance_output",
        "notification",
    }
)


class HandlerIdempotencyBoundary(str, Enum):
    ASTRALDEEP_TRANSACTION = "astraldeep_transaction"
    DOWNSTREAM_IDEMPOTENCY_KEY = "downstream_idempotency_key"


@dataclass(frozen=True)
class ScheduledHandlerDeclaration:
    supports_unattended: bool
    idempotency_boundary: HandlerIdempotencyBoundary | None
    effect_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        boundary = self.idempotency_boundary
        if boundary is not None and not isinstance(
            boundary, HandlerIdempotencyBoundary
        ):
            raise ValueError("idempotency_boundary must be a reviewed boundary")
        if not self.effect_kinds:
            raise ValueError("effect_kinds must contain a reviewed effect_kind")
        if len(set(self.effect_kinds)) != len(self.effect_kinds):
            raise ValueError("duplicate effect_kind is not allowed")
        for effect_kind in self.effect_kinds:
            if (
                not _EFFECT_KIND_RE.fullmatch(effect_kind)
                or effect_kind not in _REVIEWED_EFFECT_KINDS
            ):
                raise ValueError("effect_kind is not a reviewed safe name")


@dataclass(frozen=True)
class HandlerEligibilityDecision:
    eligible: bool
    code: str | None
    retryable: bool


def assess_unattended_handler(
    declaration: ScheduledHandlerDeclaration | None,
) -> HandlerEligibilityDecision:
    if (
        declaration is None
        or not declaration.supports_unattended
        or declaration.idempotency_boundary is None
    ):
        return HandlerEligibilityDecision(False, "handler_not_idempotent", False)
    return HandlerEligibilityDecision(True, None, False)


@dataclass(frozen=True)
class OccurrenceRunResult:
    outcome: str
    summary: str | None
    auth_ref: str | None
    retryable: bool
    result_code: str | None
    retry_after_seconds: int = 1


_DEFAULT_HANDLER_DECLARATIONS = MappingProxyType(
    {
        "scheduled_chat": ScheduledHandlerDeclaration(
            supports_unattended=True,
            idempotency_boundary=HandlerIdempotencyBoundary.ASTRALDEEP_TRANSACTION,
            effect_kinds=("chat_history", "notification", "audit_record"),
        ),
        "dreaming": ScheduledHandlerDeclaration(
            supports_unattended=True,
            idempotency_boundary=HandlerIdempotencyBoundary.ASTRALDEEP_TRANSACTION,
            effect_kinds=("maintenance_output", "audit_record"),
        ),
    }
)
_UNREVIEWED_MUTATING_SCOPES = frozenset({"tools:write", "tools:execute"})

_TURN_ENGINE_RETIRED_SUMMARY = (
    "Scheduled turns are retired. Turns run on the Kernel engine."
)

DEFAULT_MAX_ATTEMPTS = 3
CLAIM_LOOP_MULTIPLIER = 10


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("scheduler.bad_setting", extra={"setting": name})
        return default
    return max(value, minimum)


def max_attempts() -> int:
    return _env_int("SCHEDULER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, minimum=1)


def claim_loop_ceiling() -> int:
    return max_attempts() * CLAIM_LOOP_MULTIPLIER


def _intersect_scopes(
    consented: List[str], current_enabled: Dict[str, bool]
) -> List[str]:
    return [s for s in consented if s in VALID_SCOPES and current_enabled.get(s, False)]


class JobRunner:
    def __init__(
        self,
        orchestrator,
        store,
        *,
        handler_declarations: Dict[str, ScheduledHandlerDeclaration] | None = None,
    ) -> None:
        self.orch = orchestrator
        self.store = store
        self._coordinator: WorkAdmissionCoordinator | None = None
        self._handler_declarations = dict(
            _DEFAULT_HANDLER_DECLARATIONS
            if handler_declarations is None
            else handler_declarations
        )
        self._skip_notified: set = set()

    def _fresh_job(self, job: Dict[str, Any]) -> Dict[str, Any] | None:
        get_job = getattr(self.store, "get_job", None)
        if get_job is None:
            return None
        try:
            row = get_job(str(job["user_id"]), str(job["id"]))
        except Exception:
            logger.debug("scheduler could not re-read the job row", exc_info=True)
            return None
        return row or None

    def _current_job_status(self, job: Dict[str, Any]) -> str | None:
        row = self._fresh_job(job)
        if row is None or row.get("status") is None:
            return None
        return str(row["status"])

    def bind_execution_context(
        self,
        *,
        coordinator: WorkAdmissionCoordinator,
        store,
    ) -> None:
        if self._coordinator is not None and self._coordinator is not coordinator:
            raise RuntimeError("cannot replace the scheduler operation coordinator")
        if store is not self.store:
            raise RuntimeError("scheduler runner/store binding mismatch")
        self._coordinator = coordinator

    def assess_job(self, job: Dict[str, Any]) -> HandlerEligibilityDecision:
        handler_kind = job.get("handler_kind")
        if handler_kind is None:
            handler_kind = (
                "dreaming"
                if job.get("agent_id") == "__dreaming__"
                else "scheduled_chat"
            )
        declaration = self._handler_declarations.get(str(handler_kind))
        decision = assess_unattended_handler(declaration)
        if not decision.eligible:
            return decision
        consented_scopes = {
            str(scope) for scope in (job.get("consented_scopes") or [])
        }
        if (
            str(handler_kind) == "scheduled_chat"
            and consented_scopes.intersection(_UNREVIEWED_MUTATING_SCOPES)
        ):
            return HandlerEligibilityDecision(
                False,
                "handler_downstream_idempotency_unreviewed",
                False,
            )
        return decision

    @staticmethod
    def _job_type(job: Dict[str, Any]) -> str:
        return (
            "dreaming"
            if job.get("agent_id") == "__dreaming__"
            else "scheduled_chat"
        )

    def _observe_scheduler(
        self,
        event: str,
        job: Dict[str, Any],
        *,
        result_code: str | None = None,
    ) -> None:
        observability = getattr(self.orch, "runtime_observability", None)
        if observability is None:
            return
        try:
            observability.record_scheduler(
                event,
                job_type=self._job_type(job),
                result_code=result_code,
            )
        except Exception:
            logger.debug("scheduler observability rejected an event", exc_info=True)

    def _observe_effect(
        self,
        event: str,
        *,
        effect_kind: str,
        result_code: str | None = None,
    ) -> None:
        observability = getattr(self.orch, "runtime_observability", None)
        if observability is None:
            return
        try:
            observability.record_effect(
                event,
                effect_kind=effect_kind,
                result_code=result_code,
            )
        except Exception:
            logger.debug("scheduler effect observability rejected an event", exc_info=True)

    async def _notify(
        self,
        user_id: str,
        *,
        level: str,
        title: str,
        body: str,
        job_id: Optional[str],
        chat_id: Optional[str],
    ) -> None:
        try:
            await self.orch.notify_user(
                user_id,
                {
                    "type": "notification",
                    "level": level,
                    "source": "schedule",
                    "job_id": job_id,
                    "chat_id": chat_id,
                    "title": title,
                    "body": body,
                },
            )
        except Exception:  # pragma: no cover
            logger.debug("scheduler notify failed (non-fatal)", exc_info=True)

    async def _run_dreaming(self, job: Dict[str, Any], correlation_id: str) -> str:
        user_id = job["user_id"]
        job_id = job["id"]
        run_id = self.store.start_run(job_id, user_id, correlation_id)
        outcome = "success"
        summary = None
        try:
            from personalization.phi_gate import get_phi_gate

            from dreaming.consolidation import run_sweep

            repo = self.orch.personalization_service.repo
            profile = repo.get_profile(user_id) or {}
            if not bool(profile.get("dreaming_enabled", True)):
                self.store.finish_run(
                    run_id, outcome="skipped", summary="dreaming disabled"
                )
                self.store.set_status(user_id, job_id, "paused")
                return "skipped"
            sweep = run_sweep(repo, get_phi_gate(), user_id, trigger="scheduled")
            summary = (
                f"Consolidated {sweep.get('promoted_count', 0)} of "
                f"{sweep.get('candidates_considered', 0)} signals."
            )
        except Exception as exc:
            logger.exception("dreaming sweep failed", extra={"job_id": job_id})
            outcome = "failure"
            summary = f"error: {exc}"

        self.store.finish_run(
            run_id, outcome=outcome, summary=summary, auth_ref=correlation_id
        )

        import time

        now_ms = int(time.time() * 1000)
        next_run = compute_next_run_ms(
            job["schedule_kind"],
            job["schedule_expr"],
            job.get("timezone", "UTC"),
            now_ms,
        )
        completed = next_run is None
        self.store.update_after_run(
            job_id, last_run_at=now_ms, next_run_at=next_run, completed=completed
        )
        return outcome

    @staticmethod
    def _effect_digest(*, job: Dict[str, Any], effect_kind: str) -> str:
        instruction_digest = hashlib.sha256(
            str(job.get("instruction") or "").encode("utf-8")
        ).hexdigest()
        normalized = json.dumps(
            {
                "effect_kind": effect_kind,
                "job_id": str(job["id"]),
                "target_chat_id": job.get("target_chat_id"),
                "instruction_sha256": instruction_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def _notify_occurrence(
        self,
        attempt: ScheduledAttempt,
        *,
        level: str,
        title: str,
        body: str,
    ) -> None:
        digest = hashlib.sha256(
            json.dumps(
                {"level": level, "title": title, "body": body},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reservation = await asyncio.to_thread(
            self.store.reserve_effect,
            attempt,
            effect_kind="notification",
            effect_key="completion",
            payload_digest=digest,
        )
        self._observe_effect(
            "reserved" if reservation.state == "reserved" else "deduplicated",
            effect_kind="notification",
        )
        if reservation.state == "published" or reservation.ambiguous:
            return
        await self._notify(
            str(attempt.job["user_id"]),
            level=level,
            title=title,
            body=body,
            job_id=str(attempt.job["id"]),
            chat_id=attempt.job.get("target_chat_id"),
        )
        await asyncio.to_thread(
            self.store.publish_effect,
            attempt,
            effect_kind="notification",
            effect_key="completion",
            payload_digest=digest,
        )
        self._observe_effect("published", effect_kind="notification")

    async def _pause_and_notify_once(
        self,
        attempt: ScheduledAttempt,
        *,
        title: str,
        body: str,
        result_code: str,
        notify: bool = True,
    ) -> None:
        job = attempt.job
        user_id = str(job["user_id"])
        job_id = str(job["id"])
        if job_id in self._skip_notified and self._current_job_status(job) == "active":
            self._skip_notified.discard(job_id)
        already_notified = job_id in self._skip_notified
        self.store.set_status(user_id, job_id, "paused")
        if notify and not already_notified:
            self._skip_notified.add(job_id)
            await self._notify_occurrence(
                attempt, level="warning", title=title, body=body
            )
        self._observe_scheduler("terminal", job, result_code=result_code)

    async def _exhaust_claim_loop(
        self, attempt: ScheduledAttempt
    ) -> OccurrenceRunResult:
        job = attempt.job
        ceiling = claim_loop_ceiling()
        logger.warning(
            "scheduler.claim_loop_exhausted",
            extra={
                "job_id": str(job["id"]),
                "occurrence_id": str(attempt.claim.occurrence_id),
                "attempt_number": attempt.claim.attempt_number,
                "ceiling": ceiling,
            },
        )
        await self._pause_and_notify_once(
            attempt,
            title=f"Scheduled job paused: {job['name']}",
            body=(
                "The scheduler could not get one of its runs to settle after "
                f"{attempt.claim.attempt_number} attempts (the service kept "
                "losing or refusing the run; the job itself did not fail). It "
                "has been paused so it does not loop. Resume it from your "
                "schedules; if it pauses again, an administrator should check "
                "the scheduler."
            ),
            result_code="claim_loop_exhausted",
            notify=job.get("agent_id") != "__dreaming__",
        )
        return OccurrenceRunResult(
            "failure",
            (
                f"Gave up after {attempt.claim.attempt_number} claims "
                f"(ceiling {ceiling}); the run never settled"
            ),
            str(attempt.operation_id),
            False,
            "claim_loop_exhausted",
        )

    async def _run_dreaming_occurrence(
        self, attempt: ScheduledAttempt
    ) -> OccurrenceRunResult:
        job = attempt.job
        digest = self._effect_digest(job=job, effect_kind="maintenance_output")
        reservation = await asyncio.to_thread(
            self.store.reserve_effect,
            attempt,
            effect_kind="maintenance_output",
            effect_key="consolidation",
            payload_digest=digest,
        )
        self._observe_effect(
            "reserved" if reservation.state == "reserved" else "deduplicated",
            effect_kind="maintenance_output",
        )
        if reservation.state == "published":
            self._observe_scheduler(
                "terminal", job, result_code="success"
            )
            return OccurrenceRunResult(
                "success",
                "Dreaming output already published",
                str(attempt.operation_id),
                False,
                "success",
            )
        if reservation.ambiguous:
            self._observe_scheduler(
                "terminal", job, result_code="effect_outcome_ambiguous"
            )
            return OccurrenceRunResult(
                "failure",
                "Prior dreaming effect outcome is ambiguous; it was not repeated",
                str(attempt.operation_id),
                False,
                "effect_outcome_ambiguous",
            )
        try:
            from personalization.phi_gate import get_phi_gate

            from dreaming.consolidation import run_sweep

            repo = self.orch.personalization_service.repo
            profile = repo.get_profile(str(job["user_id"])) or {}
            if not bool(profile.get("dreaming_enabled", True)):
                await asyncio.to_thread(
                    self.store.fail_effect,
                    attempt,
                    effect_kind="maintenance_output",
                    effect_key="consolidation",
                    payload_digest=digest,
                    failure_code="dreaming_disabled",
                )
                self._observe_effect(
                    "failed",
                    effect_kind="maintenance_output",
                    result_code="dreaming_disabled",
                )
                self.store.set_status(str(job["user_id"]), str(job["id"]), "paused")
                self._observe_scheduler(
                    "terminal", job, result_code="dreaming_disabled"
                )
                return OccurrenceRunResult(
                    "failure",
                    "Dreaming is disabled",
                    str(attempt.operation_id),
                    False,
                    "dreaming_disabled",
                )
            sweep = await asyncio.to_thread(
                run_sweep,
                repo,
                get_phi_gate(),
                str(job["user_id"]),
                trigger="scheduled",
            )
            summary = (
                f"Consolidated {sweep.get('promoted_count', 0)} of "
                f"{sweep.get('candidates_considered', 0)} signals."
            )
        except Exception:
            logger.exception("dreaming sweep failed", extra={"job_id": str(job["id"])})
            self._observe_scheduler(
                "terminal", job, result_code="operation_failed"
            )
            return OccurrenceRunResult(
                "failure",
                "Dreaming sweep failed",
                str(attempt.operation_id),
                False,
                "operation_failed",
            )
        await asyncio.to_thread(
            self.store.publish_effect,
            attempt,
            effect_kind="maintenance_output",
            effect_key="consolidation",
            payload_digest=digest,
        )
        self._observe_effect("published", effect_kind="maintenance_output")
        self._observe_scheduler("terminal", job, result_code="success")
        return OccurrenceRunResult(
            "success", summary, str(attempt.operation_id), False, "success"
        )

    async def run_occurrence(
        self,
        attempt: ScheduledAttempt,
        *,
        claim_lost: asyncio.Event,
    ) -> OccurrenceRunResult:
        from orchestrator.emergency_stop import EmergencyStopRefused

        stop = getattr(self.orch, "emergency_stop", None)
        if stop is not None:
            try:
                async with stop.effect(str(attempt.job["user_id"])):
                    return await self._run_occurrence(attempt, claim_lost=claim_lost)
            except EmergencyStopRefused:
                return OccurrenceRunResult("failure", "Owner emergency stop is active",
                    str(attempt.operation_id), False, "emergency_stop_active")
        return await self._run_occurrence(attempt, claim_lost=claim_lost)

    async def _run_occurrence(self, attempt: ScheduledAttempt, *, claim_lost: asyncio.Event) -> OccurrenceRunResult:
        decision = self.assess_job(attempt.job)
        if not decision.eligible:
            self._observe_scheduler(
                "terminal",
                attempt.job,
                result_code=decision.code or "handler_not_idempotent",
            )
            return OccurrenceRunResult(
                "failure",
                "Scheduled handler does not provide an idempotency boundary",
                str(attempt.operation_id),
                False,
                decision.code,
            )
        if claim_lost.is_set():
            self._observe_scheduler(
                "claim_lost", attempt.job, result_code="claim_lost"
            )
            raise StaleOccurrenceClaimError("stale_occurrence_claim")
        if attempt.claim.attempt_number > 1:
            self._observe_scheduler(
                "claim_recovered", attempt.job, result_code="claim_recovered"
            )
        # attempt_number counts every re-claim, not just failures
        if attempt.claim.attempt_number > claim_loop_ceiling():
            return await self._exhaust_claim_loop(attempt)
        if attempt.job.get("agent_id") == "__dreaming__":
            return await self._run_dreaming_occurrence(attempt)

        self._observe_scheduler(
            "terminal", attempt.job, result_code="turn_engine_retired"
        )
        return OccurrenceRunResult(
            "failure",
            _TURN_ENGINE_RETIRED_SUMMARY,
            str(attempt.operation_id),
            False,
            "turn_engine_retired",
        )

    async def run_job(self, job: Dict[str, Any]) -> str:
        stop = getattr(self.orch, "emergency_stop", None)
        if stop is not None:
            async with stop.effect(str(job["user_id"])):
                return await self._run_job(job)
        return await self._run_job(job)

    async def _run_job(self, job: Dict[str, Any]) -> str:
        if job.get("agent_id") == "__dreaming__":
            return await self._run_dreaming(job, str(uuid.uuid4()))
        self._observe_scheduler("terminal", job, result_code="turn_engine_retired")
        return "failure"
