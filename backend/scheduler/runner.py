"""Executes one due scheduled job under fresh, scope-bounded delegated authority: mints
a token, intersects consented and current scopes, runs the instruction as a chat
turn, and reschedules; called by scheduler/loop.py against store.py.
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
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Optional

from orchestrator.work_admission import WorkAdmissionCoordinator
from orchestrator.tool_permissions import VALID_SCOPES

from .cron import compute_next_run_ms
from .store import (
    EffectIdempotencyConflictError,
    ScheduleActionError,
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

_SKIP_SUMMARY = {
    "missing_consent": "no durable authorization on record",
    "revoked_or_expired": "authorization revoked or expired",
    "mint_failed": "could not refresh authorization",
    "token_endpoint_unconfigured": "identity provider token endpoint not configured",
    "empty_scopes": "consented scopes no longer granted",
}
_SKIP_BODY = {
    "missing_consent": (
        "It has no durable authorization to run while you are "
        "signed out. Re-confirm the schedule to grant it."
    ),
    "revoked_or_expired": (
        "Its authorization expired or was revoked. Re-confirm to resume."
    ),
    "mint_failed": "Could not refresh its authorization. Re-confirm to resume.",
    "token_endpoint_unconfigured": (
        "The server cannot reach its identity provider because the token "
        "endpoint is not configured (KEYCLOAK_AUTHORITY). An administrator "
        "must fix the server configuration; re-confirming will not help. "
        "Resume the job once that is done."
    ),
    "empty_scopes": (
        "The permissions it was granted are no longer enabled for "
        "that agent. Re-enable them (or re-confirm) to resume."
    ),
}


def default_monitoring_dispatcher(transaction: Any, job: Dict[str, Any],
                                   prior_assignment_id: str | None) -> str:
    if not prior_assignment_id:
        raise ScheduleActionError("monitoring_assignment_unbound")
    return prior_assignment_id


_MONITORING_TERMINAL_REASONS = frozenset(
    {"allowance_exhausted", "terminal_stop", "monitoring_assignment_unbound"}
)
_MONITORING_PAUSE_BODY = {
    "allowance_exhausted": (
        "It has used its full run allowance and has been paused. Raise its "
        "run limit (or remove it) from your schedules to let it continue."
    ),
    "terminal_stop": "It was stopped and cannot run again.",
    "monitoring_assignment_unbound": (
        "It has no ongoing agent bound to check yet, so it has been paused. "
        "Bind one before resuming it."
    ),
}


DEFAULT_MAX_ATTEMPTS = 3
CLAIM_LOOP_MULTIPLIER = 10
DEFAULT_STALE_GRACE_SECONDS = 2 * 60 * 60
_MAX_BACKLOG_ESTIMATE = 10_000


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


def stale_grace_seconds() -> int:
    return _env_int(
        "SCHEDULER_STALE_GRACE_SECONDS", DEFAULT_STALE_GRACE_SECONDS, minimum=0
    )


def claim_loop_ceiling() -> int:
    return max_attempts() * CLAIM_LOOP_MULTIPLIER


def occurrence_age_seconds(
    scheduled_for: datetime, *, now: datetime | None = None
) -> float:
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current - scheduled_for).total_seconds()


def occurrence_is_stale(scheduled_for: datetime, *, now: datetime | None = None) -> bool:
    grace = stale_grace_seconds()
    if grace <= 0:
        return False
    return occurrence_age_seconds(scheduled_for, now=now) > grace


def estimate_missed_runs(job: Dict[str, Any], scheduled_for: datetime, now_ms: int) -> int:
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    cursor = int(scheduled_for.timestamp() * 1000)
    skipped = 0
    try:
        while skipped < _MAX_BACKLOG_ESTIMATE:
            following = compute_next_run_ms(
                str(job.get("schedule_kind") or "interval"),
                str(job.get("schedule_expr") or ""),
                str(job.get("timezone") or "UTC"),
                cursor,
            )
            if following is None or following > now_ms:
                break
            skipped += 1
            cursor = following
    except Exception:
        logger.debug("scheduler backlog estimate failed", exc_info=True)
    return max(skipped, 1)


def _intersect_scopes(
    consented: List[str], current_enabled: Dict[str, bool]
) -> List[str]:
    return [s for s in consented if s in VALID_SCOPES and current_enabled.get(s, False)]


class JobRunner:
    def __init__(
        self,
        orchestrator,
        store,
        offline_grants,
        *,
        handler_declarations: Dict[str, ScheduledHandlerDeclaration] | None = None,
        monitoring_dispatcher: Callable[[Any, Dict[str, Any], str | None], str] | None = None,
    ) -> None:
        self.orch = orchestrator
        self.store = store
        self.grants = offline_grants
        self._coordinator: WorkAdmissionCoordinator | None = None
        self._monitoring_dispatcher = monitoring_dispatcher or default_monitoring_dispatcher
        self._handler_declarations = dict(
            _DEFAULT_HANDLER_DECLARATIONS
            if handler_declarations is None
            else handler_declarations
        )
        self._skip_notified: set = set()
        self._failures: Dict[str, int] = {}
        self._stale_notified: set = set()

    _FAILURE_MEMO_LIMIT = 10_000

    def _record_failure(self, attempt: ScheduledAttempt) -> int:
        key = str(attempt.claim.occurrence_id)
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        while len(self._failures) > self._FAILURE_MEMO_LIMIT:
            self._failures.pop(next(iter(self._failures)))
        return count

    def _forget_failures(self, attempt: ScheduledAttempt) -> None:
        self._failures.pop(str(attempt.claim.occurrence_id), None)

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

    def _current_next_run_at(self, job: Dict[str, Any]) -> int | None:
        row = self._fresh_job(job)
        value = (row if row is not None else job).get("next_run_at")
        return None if value is None else int(value)

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

    async def _exhaust_attempts(
        self,
        attempt: ScheduledAttempt,
        *,
        last_failure: str,
        last_code: str,
        failures: int,
    ) -> OccurrenceRunResult:
        job = attempt.job
        limit = max_attempts()
        logger.warning(
            "scheduler.attempts_exhausted",
            extra={
                "job_id": str(job["id"]),
                "occurrence_id": str(attempt.claim.occurrence_id),
                "attempt_number": attempt.claim.attempt_number,
                "failures": failures,
                "max_attempts": limit,
                "last_code": last_code,
            },
        )
        self._forget_failures(attempt)
        await self._pause_and_notify_once(
            attempt,
            title=f"Scheduled job paused: {job['name']}",
            body=(
                f"It failed {failures} time{'s' if failures != 1 else ''} in a row "
                f"({last_failure}) and has been paused so it does not keep "
                "retrying. Resume it from your schedules once the cause is fixed."
            ),
            result_code="attempts_exhausted",
            notify=job.get("agent_id") != "__dreaming__",
        )
        summary = (
            f"Gave up after {failures} failed attempts "
            f"(limit {limit}); last failure: {last_failure}"
        )
        return OccurrenceRunResult(
            "failure",
            summary,
            str(attempt.operation_id),
            False,
            "attempts_exhausted",
        )

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
        self._forget_failures(attempt)
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

    async def _admit_monitoring_episode(self, attempt: ScheduledAttempt) -> bool:
        job = attempt.job
        get_job_policy = getattr(self.store, "get_job_policy", None)
        if get_job_policy is None:
            return False
        user_id, job_id = str(job["user_id"]), str(job["id"])
        policy = await asyncio.to_thread(get_job_policy, user_id, job_id)
        if policy is None:
            return False

        def admit() -> Any:
            with self.store.transaction() as transaction:
                assignment_id = self._monitoring_dispatcher(
                    transaction, job, policy.get("last_assignment_id")
                )
                return self.store.admit_episode(
                    attempt.claim,
                    assignment_id=assignment_id,
                    spend=1,
                    transaction=transaction,
                )

        result = await asyncio.to_thread(admit)
        if not result.admitted:
            raise ScheduleActionError(result.reason)
        return True

    async def _refuse_monitoring_admission(
        self, attempt: ScheduledAttempt, *, code: str
    ) -> OccurrenceRunResult:
        job = attempt.job
        if code in _MONITORING_TERMINAL_REASONS:
            self._forget_failures(attempt)
            await self._pause_and_notify_once(
                attempt,
                title=f"Scheduled job paused: {job['name']}",
                body=_MONITORING_PAUSE_BODY.get(
                    code, "It could not be admitted for its next run."
                ),
                result_code=code,
            )
            return OccurrenceRunResult(
                "failure",
                _MONITORING_PAUSE_BODY.get(code, "Not admitted"),
                str(attempt.operation_id),
                False,
                code,
            )
        self._observe_scheduler("terminal", job, result_code=code)
        return OccurrenceRunResult(
            "failure",
            "Its ongoing agent could not be reached for this run; it will retry.",
            str(attempt.operation_id),
            True,
            code,
        )

    def _should_skip_stale(self, job: Dict[str, Any], scheduled_for: datetime) -> bool:
        if job.get("schedule_kind") == "one_shot":
            return False
        now = datetime.now(UTC)
        if not occurrence_is_stale(scheduled_for, now=now):
            return False
        next_run_at = self._current_next_run_at(job)
        if next_run_at is None:
            return False
        return next_run_at <= int(now.timestamp() * 1000)

    async def _notify_stale_backlog_once(self, attempt: ScheduledAttempt) -> None:
        job = attempt.job
        job_id = str(job["id"])
        if job_id in self._stale_notified:
            return
        self._stale_notified.add(job_id)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        missed = estimate_missed_runs(job, attempt.claim.scheduled_for, now_ms)
        await self._notify_occurrence(
            attempt,
            level="warning",
            title=f"Missed runs skipped: {job['name']}",
            body=(
                f"Skipped {missed} missed run{'s' if missed != 1 else ''} while "
                "the service was unavailable. The most recent one will run "
                "normally and the schedule continues as usual."
            ),
        )

    async def run_occurrence(
        self,
        attempt: ScheduledAttempt,
        *,
        claim_lost: asyncio.Event,
    ) -> OccurrenceRunResult:
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

        try:
            await self._admit_monitoring_episode(attempt)
        except ScheduleActionError as exc:
            return await self._refuse_monitoring_admission(attempt, code=exc.code)

        job = attempt.job
        user_id = str(job["user_id"])
        job_id = str(job["id"])

        # Catch-up guard: one-shot occurrences are never skipped
        scheduled_for = attempt.claim.scheduled_for
        skip_stale = await asyncio.to_thread(self._should_skip_stale, job, scheduled_for)
        if not skip_stale:
            self._stale_notified.discard(job_id)
        if skip_stale:
            age_s = int(occurrence_age_seconds(scheduled_for))
            logger.info(
                "scheduler.skipped_stale",
                extra={
                    "job_id": job_id,
                    "occurrence_id": str(attempt.claim.occurrence_id),
                    "age_seconds": age_s,
                    "grace_seconds": stale_grace_seconds(),
                },
            )
            self._forget_failures(attempt)
            await self._notify_stale_backlog_once(attempt)
            self._observe_scheduler("terminal", job, result_code="skipped_stale")
            return OccurrenceRunResult(
                "failure",
                (
                    f"Skipped: scheduled for {scheduled_for.isoformat()} "
                    f"({age_s // 3600}h {age_s % 3600 // 60}m ago), older than the "
                    f"{stale_grace_seconds()}s stale-grace window with more "
                    "backlog behind it; no turn was run"
                ),
                str(attempt.operation_id),
                False,
                "skipped_stale",
            )

        from orchestrator.chain_authority import AuthoritySkip, MachineTurnAuthority

        authority = await MachineTurnAuthority(self.orch, self.grants).derive(
            user_id=user_id,
            agent_id=job.get("agent_id"),
            consented_scopes=list(job.get("consented_scopes") or []),
            grant_id=job.get("offline_grant_id"),
            turn_class="scheduled_job",
        )
        if isinstance(authority, AuthoritySkip):
            await self._pause_and_notify_once(
                attempt,
                title=f"Scheduled job paused: {job['name']}",
                body=_SKIP_BODY.get(
                    authority.reason,
                    "Its authorization is no longer valid. Re-confirm to resume.",
                ),
                result_code="authorization_unavailable",
            )
            return OccurrenceRunResult(
                "skipped_auth",
                _SKIP_SUMMARY.get(authority.reason, authority.reason),
                str(attempt.operation_id),
                False,
                "authorization_unavailable",
            )

        effect_key = str(job.get("target_chat_id") or job["id"])
        digest = self._effect_digest(job=job, effect_kind="chat_history")
        try:
            reservation = await asyncio.to_thread(
                self.store.reserve_atomic_chat_effect,
                attempt,
                effect_key=effect_key,
                payload_digest=digest,
            )
        except EffectIdempotencyConflictError:
            self._observe_effect(
                "conflict",
                effect_kind="chat_history",
                result_code="effect_idempotency_conflict",
            )
            self._observe_scheduler(
                "terminal", job, result_code="effect_idempotency_conflict"
            )
            raise
        self._observe_effect(
            "reserved" if reservation.state == "reserved" else "deduplicated",
            effect_kind="chat_history",
        )
        if reservation.state == "published":
            self._observe_scheduler(
                "terminal", job, result_code="success"
            )
            return OccurrenceRunResult(
                "success",
                "Scheduled output was already published",
                str(attempt.operation_id),
                False,
                "success",
            )
        if reservation.ambiguous:
            self._observe_effect(
                "deduplicated",
                effect_kind="chat_history",
                result_code="effect_outcome_ambiguous",
            )
            self._observe_scheduler(
                "terminal", job, result_code="effect_outcome_ambiguous"
            )
            return OccurrenceRunResult(
                "failure",
                "A prior output may already be visible; the handler was not repeated",
                str(attempt.operation_id),
                False,
                "effect_outcome_ambiguous",
            )
        try:
            summary = await self.orch.run_scheduled_turn(
                user_id=user_id,
                chat_id=job.get("target_chat_id"),
                instruction=str(job["instruction"]),
                agent_id=job.get("agent_id"),
                access_token=authority.access_token,
                allowed_scopes=authority.allowed_scopes,
                correlation_id=str(attempt.claim.occurrence_id),
                authority=authority,
                scheduled_attempt=attempt,
                scheduled_store=self.store,
                effect_kind="chat_history",
                effect_key=effect_key,
                payload_digest=digest,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                from llm_config import LLMUnavailable

                llm_unavailable = isinstance(exc, LLMUnavailable)
            except Exception:  # pragma: no cover
                llm_unavailable = False
            logger.exception("scheduled job execution failed", extra={"job_id": job_id})
            result_code = "llm_unavailable" if llm_unavailable else "operation_failed"
            failure_summary = (
                "System AI was unavailable"
                if llm_unavailable
                else "Scheduled turn failed"
            )
            failures = self._record_failure(attempt)
            if failures >= max_attempts():
                return await self._exhaust_attempts(
                    attempt,
                    last_failure=failure_summary,
                    last_code=result_code,
                    failures=failures,
                )
            self._observe_scheduler("terminal", job, result_code=result_code)
            return OccurrenceRunResult(
                "failure",
                failure_summary,
                str(attempt.operation_id),
                True,
                result_code,
            )

        if claim_lost.is_set():
            self._observe_scheduler(
                "claim_lost", job, result_code="claim_lost"
            )
            raise StaleOccurrenceClaimError("stale_occurrence_claim")
        self._observe_effect("published", effect_kind="chat_history")
        self._forget_failures(attempt)
        self._skip_notified.discard(job_id)
        self._stale_notified.discard(job_id)
        await self._notify_occurrence(
            attempt,
            level="success",
            title=f"{job['name']} is ready",
            body=(summary or "Your scheduled task finished.")[:200],
        )
        self._observe_scheduler("terminal", job, result_code="success")
        return OccurrenceRunResult(
            "success", summary, str(attempt.operation_id), False, "success"
        )

    async def run_job(self, job: Dict[str, Any]) -> str:
        user_id = job["user_id"]
        job_id = job["id"]
        correlation_id = str(uuid.uuid4())

        if job.get("agent_id") == "__dreaming__":
            return await self._run_dreaming(job, correlation_id)

        run_id = self.store.start_run(job_id, user_id, correlation_id)

        agent_id = job.get("agent_id")
        from orchestrator.chain_authority import AuthoritySkip, MachineTurnAuthority

        authority = await MachineTurnAuthority(self.orch, self.grants).derive(
            user_id=user_id,
            agent_id=agent_id,
            consented_scopes=list(job.get("consented_scopes") or []),
            grant_id=job.get("offline_grant_id"),
            turn_class="scheduled_job",
        )
        if isinstance(authority, AuthoritySkip):
            already_notified = job_id in self._skip_notified
            self.store.finish_run(
                run_id,
                outcome="skipped_auth",
                summary=_SKIP_SUMMARY.get(authority.reason, authority.reason),
            )
            self.store.set_status(user_id, job_id, "paused")
            logger.warning(
                "scheduler.authority_skip",
                extra={
                    "job_id": job_id,
                    "user_id": user_id,
                    "reason": authority.reason,
                    "notified": not already_notified,
                },
            )
            if not already_notified:
                self._skip_notified.add(job_id)
                await self._notify(
                    user_id,
                    level="warning",
                    title=f"Scheduled job paused: {job['name']}",
                    body=_SKIP_BODY.get(
                        authority.reason,
                        "Its authorization is no longer valid. Re-confirm to resume.",
                    ),
                    job_id=job_id,
                    chat_id=job.get("target_chat_id"),
                )
            return "skipped_auth"

        access_token = authority.access_token
        allowed_scopes = authority.allowed_scopes

        outcome = "success"
        summary = None
        llm_unavailable = False
        try:
            summary = await self.orch.run_scheduled_turn(
                user_id=user_id,
                chat_id=job.get("target_chat_id"),
                instruction=job["instruction"],
                agent_id=agent_id,
                access_token=access_token,
                allowed_scopes=allowed_scopes,
                correlation_id=correlation_id,
                authority=authority,
            )
        except Exception as exc:
            try:
                from llm_config import LLMUnavailable

                llm_unavailable = isinstance(exc, LLMUnavailable)
            except Exception:  # pragma: no cover
                llm_unavailable = False
            if llm_unavailable:
                logger.warning(
                    "scheduled job skipped: system_llm_unconfigured",
                    extra={"job_id": job_id},
                )
                outcome = "failure"
                summary = "llm_unavailable: no system AI credential configured"
            else:
                logger.exception(
                    "scheduled job execution failed", extra={"job_id": job_id}
                )
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
        completed = job["schedule_kind"] == "one_shot" or next_run is None
        self.store.update_after_run(
            job_id, last_run_at=now_ms, next_run_at=next_run, completed=completed
        )

        logger.info(
            "scheduler.run_finished",
            extra={
                "job_id": job_id,
                "user_id": user_id,
                "outcome": outcome,
                "correlation_id": correlation_id,
                "next_run_at": next_run,
            },
        )

        if outcome == "success":
            self._skip_notified.discard(job_id)
            await self._notify(
                user_id,
                level="success",
                title=f"{job['name']} is ready",
                body=(summary or "Your scheduled task finished.")[:200],
                job_id=job_id,
                chat_id=job.get("target_chat_id"),
            )
        elif llm_unavailable:
            await self._notify(
                user_id,
                level="error",
                title=f"Scheduled job failed: {job['name']}",
                body=(
                    "The AI was unavailable — the task did not run. "
                    "An admin needs to configure the System LLM "
                    "in settings."
                ),
                job_id=job_id,
                chat_id=job.get("target_chat_id"),
            )
        return outcome
