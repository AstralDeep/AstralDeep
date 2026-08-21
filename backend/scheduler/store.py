"""Durable scheduled jobs, occurrence claims, attempts, and effect fencing.

Feature 060 keeps the feature-025 job APIs as compatibility methods while
making PostgreSQL ``scheduled_occurrence`` and ``effect_ledger`` rows the only
execution authority.  New scheduler workers never dispatch from ``list_due``;
they materialize, advance, and claim in one transaction and carry both the
occurrence and accepted-operation fences through every mutation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Callable, Dict, List, Optional

from astralplane.errors import PlaneError
from astralplane.repositories.scheduler import (
    OccurrenceState as PlaneOccurrenceState,
    ScheduledJob as PlaneScheduledJob,
    StagedChatLayout as PlaneStagedChatLayout,
    StagedChatMessage as PlaneStagedChatMessage,
    StagedChatPublication as PlaneStagedChatPublication,
)
from orchestrator.work_admission import (
    AdmissionClass,
    ExecutionFence,
    OperationNotFoundError,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    RefusedAdmission,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from orchestrator.scheduled_publication import ScheduledHistoryBatch

from .cron import compute_next_run_ms


logger = logging.getLogger("scheduler.store")

ACTIVE_STATUSES = ("active",)
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class StaleOccurrenceClaimError(RuntimeError):
    """The supplied occurrence token/generation no longer owns the row."""

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        terminal_code: str | None = None,
    ) -> None:
        super().__init__(code if message is None else message)
        self.code = code
        self.terminal_code = terminal_code


class ScheduleActionError(RuntimeError):
    """Safe, owner-scoped refusal from a scheduler definition action."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EffectIdempotencyConflictError(RuntimeError):
    """One effect key was reused with different normalized payload bytes."""


class ScheduledAdmissionRefusedError(RuntimeError):
    """The scheduled operation could not enter its finite admission queue."""

    def __init__(self, refusal: RefusedAdmission) -> None:
        super().__init__(refusal.code)
        self.code = refusal.code
        self.retryable = refusal.retryable
        self.retry_after_ms = refusal.retry_after_ms


@dataclass(frozen=True)
class OccurrenceClaim:
    """One current PostgreSQL claim for a stable scheduled occurrence."""

    occurrence_id: uuid.UUID
    job: Dict[str, Any]
    scheduled_for: datetime
    claim_generation: int
    lease_token: uuid.UUID
    lease_owner: str
    lease_expires_at: datetime
    attempt_number: int
    parent_operation_id: uuid.UUID | None


@dataclass(frozen=True)
class ScheduledAttempt:
    """Attempt-scoped accepted operation attached to an occurrence claim."""

    claim: OccurrenceClaim
    operation_id: uuid.UUID
    operation_state: OperationState
    execution_fence: ExecutionFence | None
    parent_operation_id: uuid.UUID | None
    run_id: uuid.UUID | None = None
    request_generation: uuid.UUID | None = None

    @property
    def job(self) -> Dict[str, Any]:
        return self.claim.job


@dataclass(frozen=True)
class EffectReservation:
    """Safe effect-ledger reconciliation result."""

    state: str
    created: bool
    ambiguous: bool


@dataclass(frozen=True)
class RunNowMaterialization:
    """Canonical result of one owner-scoped run-now submission."""

    occurrence_id: uuid.UUID
    job_id: uuid.UUID
    owner_user_id: str
    scheduled_for: datetime
    state: str
    created: bool


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _plane_terminal_code(error: PlaneError) -> str | None:
    """Read Plane's immutable metadata pairs without assuming a mapping API."""

    value = dict(error.metadata).get("terminal_code")
    return None if value in {None, "None"} else value


def _stale_plane_error(
    error: PlaneError, *, message: str | None = None
) -> StaleOccurrenceClaimError:
    safe_message = message or {
        "effect_reservation_not_found": "effect was not reserved by this attempt",
    }.get(error.code, str(error))
    return StaleOccurrenceClaimError(
        error.code,
        message=safe_message,
        terminal_code=_plane_terminal_code(error),
    )


class ScheduledJobStore:
    def __init__(
        self,
        *,
        coordinator: WorkAdmissionCoordinator | None = None,
        plane_runtime: Any,
        plane_repositories: Any | None = None,
    ) -> None:
        self._coordinator = coordinator
        repository, runtime = repository_from(
            "scheduler",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        self._plane = PlaneRepositoryContext(
            repository=repository,
            plane_runtime=runtime,
        )

    def bind_coordinator(self, coordinator: WorkAdmissionCoordinator) -> None:
        """Bind the shared production operation authority exactly once."""

        if self._coordinator is not None and self._coordinator is not coordinator:
            raise RuntimeError("cannot replace the scheduler operation coordinator")
        self._coordinator = coordinator

    def _require_coordinator(self) -> WorkAdmissionCoordinator:
        if self._coordinator is None:
            raise RuntimeError(
                "durable scheduler execution requires a WorkAdmissionCoordinator"
            )
        return self._coordinator

    @staticmethod
    def _validate_claim_settings(
        instance_id: str, *, limit: int, lease_seconds: int
    ) -> None:
        if not _INSTANCE_RE.fullmatch(instance_id):
            raise ValueError("instance_id must be a bounded non-sensitive identifier")
        if limit <= 0 or limit > 1_000:
            raise ValueError("claim limit must be between 1 and 1000")
        if lease_seconds < 5 or lease_seconds > 60:
            raise ValueError("scheduled claim lease must be between 5 and 60 seconds")

    @staticmethod
    def _claim_matches(row: Dict[str, Any], claim: OccurrenceClaim) -> bool:
        return (
            _as_uuid(row.get("occurrence_id")) == claim.occurrence_id
            and int(row.get("claim_generation") or 0) == claim.claim_generation
            and _as_uuid(row.get("lease_token")) == claim.lease_token
            and row.get("lease_owner") == claim.lease_owner
        )

    @staticmethod
    def _owner(claim: OccurrenceClaim) -> OperationOwner:
        return OperationOwner(
            owner_scope=OwnerScope.SCHEDULE,
            owner_user_id=str(claim.job["user_id"]),
            connection_scope_id=None,
        )

    @staticmethod
    def _job_dict(job: PlaneScheduledJob) -> Dict[str, Any]:
        return {
            "id": job.job_id,
            "user_id": job.owner_id,
            "agent_id": job.agent_id,
            "name": job.name,
            "instruction": job.instruction,
            "schedule_kind": job.schedule_kind,
            "schedule_expr": job.schedule_expression,
            "timezone": job.timezone,
            "consented_scopes": list(job.consented_scopes),
            "delivery": job.delivery,
            "status": job.status,
            "target_chat_id": job.target_chat_id,
            "next_run_at": job.next_run_at,
            "last_run_at": job.last_run_at,
            "offline_grant_id": job.offline_grant_id,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    @staticmethod
    def _run_dict(run: Any) -> Dict[str, Any]:
        return {
            "id": run.run_id,
            "job_id": run.job_id,
            "user_id": run.owner_id,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "outcome": run.outcome,
            "auth_ref": run.auth_ref,
            "correlation_id": run.correlation_id,
            "summary": run.summary,
            "occurrence_id": run.occurrence_id,
            "attempt_number": run.attempt_number,
            "operation_id": run.operation_id,
            "operation_execution_generation": run.operation_execution_generation,
            "occurrence_claim_generation": run.occurrence_claim_generation,
        }

    # ── Jobs ─────────────────────────────────────────────────────────────

    def count_active(self, user_id: str) -> int:
        return self._plane.call(
            self._plane.repository.count_active_jobs,
            owner_id=user_id,
        )

    def create_job(
        self,
        user_id: str,
        *,
        name: str,
        instruction: str,
        schedule_kind: str,
        schedule_expr: str,
        timezone: str,
        consented_scopes: List[str],
        agent_id: Optional[str],
        target_chat_id: Optional[str],
        next_run_at: Optional[int],
        offline_grant_id: Optional[str],
    ) -> Dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = _now_ms()
        job = self._plane.call(
            self._plane.repository.create_job_definition,
            job=PlaneScheduledJob(
                job_id=job_id,
                owner_id=user_id,
                name=name,
                instruction=instruction,
                schedule_kind=schedule_kind,
                schedule_expression=schedule_expr,
                timezone=timezone,
                status="active",
                next_run_at=next_run_at,
                created_at=now,
                updated_at=now,
                agent_id=agent_id,
                consented_scopes=tuple(consented_scopes),
                target_chat_id=target_chat_id,
                offline_grant_id=offline_grant_id,
            ),
        )
        return self._job_dict(job)

    def get_job(self, user_id: str, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            job = self._plane.call(
                self._plane.repository.get_job,
                owner_id=user_id,
                job_id=job_id,
            )
        except ValueError:
            return None
        return None if job is None else self._job_dict(job)

    def list_jobs(self, user_id: str) -> List[Dict[str, Any]]:
        jobs = self._plane.call(
            self._plane.repository.list_jobs,
            owner_id=user_id,
            limit=1000,
        )
        return [self._job_dict(job) for job in jobs]

    def set_offline_grant(
        self, user_id: str, job_id: str, grant_id: Optional[str]
    ) -> bool:
        """Attach (or clear) the captured offline-grant id on a job (030 FR-003 / 025 T042).

        Written by the WS consent-capture flow after ``OfflineGrantStore.capture``
        so the runner can mint a fresh token per run. Until set, ``offline_grant_id``
        is NULL and the runner refuses to execute (``skipped_auth``)."""
        return self._plane.call(
            self._plane.repository.set_job_offline_grant,
            owner_id=user_id,
            job_id=job_id,
            grant_id=grant_id,
            updated_at=_now_ms(),
        )

    def set_status(self, user_id: str, job_id: str, status: str) -> bool:
        try:
            return self._plane.call(
                self._plane.repository.set_job_status,
                owner_id=user_id,
                job_id=job_id,
                status=status,
                updated_at=_now_ms(),
            )
        except ValueError:
            return False

    def materialize_run_now(
        self,
        *,
        user_id: str,
        job_id: str,
        submission_id: uuid.UUID,
        eligibility: Callable[[Dict[str, Any]], Any] | None = None,
    ) -> RunNowMaterialization:
        """Create or reconcile one manual firing without changing cadence."""

        try:
            job_identity = uuid.UUID(str(job_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ScheduleActionError("job_not_found") from exc
        try:
            submission_identity = uuid.UUID(str(submission_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ScheduleActionError("invalid_submission_id") from exc
        if submission_identity.version != 4:
            raise ScheduleActionError("invalid_submission_id")

        try:
            with self._plane.transaction() as transaction:
                job_record = self._plane.repository.get_job(
                    transaction,
                    owner_id=user_id,
                    job_id=str(job_identity),
                    for_update=True,
                )
                if job_record is None:
                    raise ScheduleActionError("job_not_found")
                job = self._job_dict(job_record)
                if eligibility is not None:
                    decision = eligibility(job)
                    if not bool(getattr(decision, "eligible", decision)):
                        code = str(
                            getattr(decision, "code", None)
                            or "handler_not_idempotent"
                        )
                        if code not in {
                            "handler_not_idempotent",
                            "handler_downstream_idempotency_unreviewed",
                        }:
                            code = "handler_not_idempotent"
                        raise ScheduleActionError(code)
                result = self._plane.repository.materialize_run_now(
                    transaction,
                    owner_id=user_id,
                    job_id=str(job_identity),
                    submission_id=str(submission_identity),
                )
        except PlaneError as exc:
            code = {
                "scheduled_job_not_found": "job_not_found",
                "scheduled_job_not_active": "job_not_active",
                "scheduled_run_now_idempotency_conflict": "idempotency_conflict",
                "scheduled_run_now_timestamp_conflict": "run_now_timestamp_conflict",
            }.get(exc.code, exc.code)
            raise ScheduleActionError(code) from exc
        return RunNowMaterialization(
            occurrence_id=uuid.UUID(result.occurrence_id),
            job_id=uuid.UUID(result.job_id),
            owner_user_id=result.owner_id,
            scheduled_for=_utc(result.scheduled_for),
            state=result.state.value,
            created=result.created,
        )

    def set_status_and_cancel_unstarted(
        self,
        *,
        user_id: str,
        job_id: str,
        status: str,
        terminal_code: str,
    ) -> bool:
        """Transition a job and atomically cancel every unstarted occurrence."""

        expected_codes = {
            "paused": "cancelled_job_paused",
            "disabled": "cancelled_job_deleted",
        }
        if expected_codes.get(status) != terminal_code:
            raise ValueError("status and scheduler cancellation code disagree")
        try:
            job_identity = uuid.UUID(str(job_id))
        except (TypeError, ValueError, AttributeError):
            return False

        coordinator = self._require_coordinator()
        owner = OperationOwner(
            owner_scope=OwnerScope.SCHEDULE,
            owner_user_id=user_id,
            connection_scope_id=None,
        )
        with self._plane.transaction() as transaction:
            candidates = self._plane.repository.transition_job_and_list_unstarted(
                transaction,
                owner_id=user_id,
                job_id=str(job_identity),
                status=status,
            )
            if candidates is None:
                return False
            for candidate in candidates:
                occurrence_id = uuid.UUID(candidate.occurrence_id)
                operation_id = (
                    None
                    if candidate.operation_id is None
                    else uuid.UUID(candidate.operation_id)
                )
                operation = None
                if operation_id is not None:
                    try:
                        operation = coordinator.cancel(
                            owner=owner,
                            operation_id=operation_id,
                            terminal_code=terminal_code,
                            request_running=False,
                            transaction=transaction,
                        )
                    except OperationNotFoundError:
                        operation = None
                if operation_id is not None and operation is None:
                    raise RuntimeError(
                        "scheduled occurrence references a missing operation"
                    )
                if operation is not None and operation.state is OperationState.RUNNING:
                    if operation.execution_lease_token is None:
                        raise RuntimeError("running operation has no execution fence")
                    coordinator.terminalize(
                        ExecutionFence(
                            operation_id=operation.operation_id,
                            execution_generation=operation.execution_generation,
                            execution_lease_token=operation.execution_lease_token,
                        ),
                        state=OperationState.CANCELLED,
                        terminal_code=terminal_code,
                        safe_summary="Scheduled job cancelled before start",
                        retry_after_ms=None,
                        transaction=transaction,
                    )
                self._plane.repository.cancel_unstarted_occurrence(
                    transaction,
                    owner_id=user_id,
                    occurrence_id=str(occurrence_id),
                    expected_operation_id=(
                        None if operation_id is None else str(operation_id)
                    ),
                    terminal_code=terminal_code,
                )
        return True

    def update_after_run(
        self,
        job_id: str,
        *,
        last_run_at: int,
        next_run_at: Optional[int],
        completed: bool,
    ) -> None:
        self._plane.call(
            self._plane.repository.update_job_after_run_for_administration,
            job_id=job_id,
            last_run_at=last_run_at,
            next_run_at=next_run_at,
            completed=completed,
            updated_at=_now_ms(),
        )

    # ── Scheduler-internal (cross-user) ──────────────────────────────────

    def list_due(self, now_ms: int) -> List[Dict[str, Any]]:
        """Legacy read-only due list; feature-060 execution does not use it."""

        jobs = self._plane.call(
            self._plane.repository.list_due_jobs_for_administration,
            due_at_ms=now_ms,
            limit=1000,
        )
        return [self._job_dict(job) for job in jobs]

    # ── Feature 060 occurrence authority ────────────────────────────────

    def materialize_and_claim_due(
        self,
        instance_id: str,
        *,
        limit: int = 20,
        lease_seconds: int = 15,
        eligibility: Callable[[Dict[str, Any]], Any] | None = None,
    ) -> tuple[OccurrenceClaim, ...]:
        """Materialize due jobs, advance them, and claim occurrences atomically.

        ``eligibility`` is a pure pre-materialization handler declaration
        check.  A false decision leaves the job untouched; no occurrence or
        accepted operation is fabricated for an ineligible handler.
        """

        self._validate_claim_settings(
            instance_id, limit=limit, lease_seconds=lease_seconds
        )
        def is_eligible(job_record: PlaneScheduledJob) -> bool:
            job = self._job_dict(job_record)
            decision = eligibility(job) if eligibility is not None else None
            return decision is None or bool(getattr(decision, "eligible", decision))

        def next_cadence(job_record: PlaneScheduledJob, scheduled_ms: int) -> int | None:
            return compute_next_run_ms(
                job_record.schedule_kind,
                job_record.schedule_expression,
                job_record.timezone,
                scheduled_ms,
            )

        with self._plane.transaction() as transaction:
            batch = self._plane.repository.materialize_and_claim_due_for_administration(
                transaction,
                instance_id=instance_id,
                limit=limit,
                lease_seconds=lease_seconds,
                eligible=is_eligible,
                next_run=next_cadence,
            )
        for job_id in batch.ineligible_job_ids:
            logger.warning(
                "scheduler.handler_ineligible",
                extra={"job_id": job_id, "code": "handler_not_idempotent"},
            )
        for prior in batch.recovered_attempts:
            self._terminalize_recovered_attempt(
                operation_id=uuid.UUID(prior.operation_id),
            )
        return tuple(
            OccurrenceClaim(
                occurrence_id=uuid.UUID(claim.occurrence.occurrence_id),
                job=self._job_dict(claim.job),
                scheduled_for=_utc(claim.occurrence.scheduled_for),
                claim_generation=claim.occurrence.claim_generation,
                lease_token=uuid.UUID(str(claim.occurrence.lease_token)),
                lease_owner=str(claim.occurrence.lease_owner),
                lease_expires_at=_utc(claim.occurrence.lease_expires_at),
                attempt_number=claim.occurrence.attempt_count,
                parent_operation_id=(
                    None
                    if claim.parent_operation_id is None
                    else uuid.UUID(claim.parent_operation_id)
                ),
            )
            for claim in batch.claims
        )

    def _terminalize_recovered_attempt(
        self,
        *,
        operation_id: uuid.UUID,
        execution_generation: int | None = None,
        execution_lease_token: uuid.UUID | None = None,
        state: str | None = None,
    ) -> None:
        """Settle the prior operation before its replacement is allocated."""

        coordinator = self._coordinator
        if coordinator is None:
            return
        supplied = (execution_generation, execution_lease_token, state)
        if all(value is None for value in supplied):
            operation = coordinator.repository.get_operation_for_administration(
                operation_id
            )
            if operation is None:
                return
            state = operation.state.value
            execution_generation = operation.execution_generation
            execution_lease_token = operation.execution_lease_token
        elif any(value is None for value in supplied):
            raise ValueError("recovered operation authority must be supplied together")
        if (
            state == "running"
            and execution_generation > 0
            and execution_lease_token is not None
        ):
            try:
                coordinator.terminalize(
                    ExecutionFence(
                        operation_id=operation_id,
                        execution_generation=execution_generation,
                        execution_lease_token=execution_lease_token,
                    ),
                    state=OperationState.RETRYABLE,
                    terminal_code="claim_lost",
                    safe_summary="Scheduled claim expired before completion",
                    retry_after_ms=0,
                )
                return
            except StaleExecutionFenceError:
                pass

        if state in {"queued", "running"}:
            coordinator.terminalize_unselected(
                operation_id,
                terminal_code="claim_lost",
                safe_summary="Scheduled claim expired before start",
                retry_after_ms=0,
            )

    def renew_claim(
        self, claim: OccurrenceClaim, *, lease_seconds: int = 15
    ) -> datetime | None:
        """Renew one unexpired current claim using PostgreSQL time."""

        self._validate_claim_settings(
            claim.lease_owner, limit=1, lease_seconds=lease_seconds
        )
        renewed = self._plane.call(
            self._plane.repository.renew_occurrence_claim,
            owner_id=str(claim.job["user_id"]),
            occurrence_id=str(claim.occurrence_id),
            claim_generation=claim.claim_generation,
            lease_token=str(claim.lease_token),
            lease_owner=claim.lease_owner,
            lease_seconds=lease_seconds,
        )
        return None if renewed is None else _utc(renewed)

    def _current_claim_row(
        self,
        cursor: Any,
        claim: OccurrenceClaim,
        *,
        states: tuple[str, ...] = ("claimed", "running"),
    ) -> Dict[str, Any]:
        try:
            record = self._plane.repository.assert_current_claim(
                cursor,
                owner_id=str(claim.job["user_id"]),
                occurrence_id=str(claim.occurrence_id),
                claim_generation=claim.claim_generation,
                lease_token=str(claim.lease_token),
                lease_owner=claim.lease_owner,
                states=tuple(PlaneOccurrenceState(state) for state in states),
            )
        except PlaneError as exc:
            raise _stale_plane_error(exc) from exc
        return {
            "occurrence_id": record.occurrence_id,
            "job_id": record.job_id,
            "owner_user_id": record.owner_id,
            "scheduled_for": record.scheduled_for,
            "state": record.state.value,
            "claim_generation": record.claim_generation,
            "lease_token": record.lease_token,
            "lease_owner": record.lease_owner,
            "lease_expires_at": record.lease_expires_at,
            "attempt_count": record.attempt_count,
            "current_operation_id": record.operation_id,
            "operation_execution_generation": record.operation_execution_generation,
            "terminal_at": record.terminal_at,
            "next_attempt_at": record.next_attempt_at,
            "result_code": record.result_code,
            "last_error_code": record.last_error_code,
        }

    def _lock_claim_job(self, cursor: Any, claim: OccurrenceClaim) -> None:
        """Serialize attempt allocation with pause/delete definition changes."""

        try:
            self._plane.repository.assert_claim_job_active(
                cursor,
                owner_id=str(claim.job["user_id"]),
                job_id=str(claim.job["id"]),
            )
        except PlaneError as exc:
            raise _stale_plane_error(exc) from exc

    def allocate_attempt(self, claim: OccurrenceClaim) -> ScheduledAttempt:
        """Create/resolve and attach one attempt-scoped scheduled operation."""

        coordinator = self._require_coordinator()
        with self._plane.transaction() as cursor:
            self._lock_claim_job(cursor, claim)
            self._current_claim_row(cursor, claim, states=("claimed",))

        attempt_key = f"{claim.occurrence_id}:{claim.attempt_number}"
        normalized_identity = "|".join(
            (
                attempt_key,
                str(claim.job["id"]),
                claim.scheduled_for.isoformat(),
            )
        )
        request = OperationRequest(
            operation_kind="scheduled_occurrence",
            admission_class=AdmissionClass.SCHEDULED,
            owner=self._owner(claim),
            submission_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"astraldeep:scheduled:{attempt_key}"
            ),
            idempotency_namespace="scheduled_occurrence_attempt",
            idempotency_key=attempt_key,
            normalized_input_digest=hashlib.sha256(
                normalized_identity.encode("utf-8")
            ).hexdigest(),
            chat_id=str(claim.job.get("target_chat_id") or claim.job["id"]),
            parent_operation_id=claim.parent_operation_id,
            connection_generation=None,
            request_generation=uuid.uuid4(),
        )
        admitted = coordinator.submit(request)
        if not admitted.accepted:
            self.mark_claim_retryable(
                claim,
                error_code=admitted.code,
                retry_after_seconds=max(1, (admitted.retry_after_ms or 1_000) // 1_000),
            )
            raise ScheduledAdmissionRefusedError(admitted)

        try:
            with self._plane.transaction() as cursor:
                self._lock_claim_job(cursor, claim)
                self._current_claim_row(cursor, claim, states=("claimed",))
                self._plane.repository.attach_operation_to_claim(
                    cursor,
                    owner_id=str(claim.job["user_id"]),
                    occurrence_id=str(claim.occurrence_id),
                    claim_generation=claim.claim_generation,
                    lease_token=str(claim.lease_token),
                    operation_id=str(admitted.operation_id),
                )
        except PlaneError as exc:
            self._settle_unstarted_operation(
                claim,
                admitted.operation_id,
                admitted.state,
                terminal_code=_plane_terminal_code(exc),
            )
            raise _stale_plane_error(exc) from exc
        except StaleOccurrenceClaimError as exc:
            self._settle_unstarted_operation(
                claim,
                admitted.operation_id,
                admitted.state,
                terminal_code=exc.terminal_code,
            )
            raise

        projection = coordinator.query_operation(
            owner=self._owner(claim), operation_id=admitted.operation_id
        )
        attempt = ScheduledAttempt(
            claim=claim,
            operation_id=admitted.operation_id,
            operation_state=admitted.state,
            execution_fence=None,
            parent_operation_id=claim.parent_operation_id,
            request_generation=projection.request_generation,
        )
        selected = self.claim_attempt_execution(attempt)
        return selected or attempt

    def _settle_unstarted_operation(
        self,
        claim: OccurrenceClaim,
        operation_id: uuid.UUID,
        state: OperationState,
        *,
        terminal_code: str | None = None,
    ) -> None:
        coordinator = self._require_coordinator()
        if state in {OperationState.QUEUED, OperationState.RUNNING}:
            if terminal_code in {
                "cancelled_job_paused",
                "cancelled_job_deleted",
            }:
                coordinator.cancel(
                    owner=self._owner(claim),
                    operation_id=operation_id,
                    terminal_code=terminal_code,
                )
                return
            coordinator.terminalize_unselected(
                operation_id,
                terminal_code="claim_lost",
                safe_summary="Scheduled claim lost before start",
                retry_after_ms=0,
            )

    def claim_attempt_execution(
        self, attempt: ScheduledAttempt
    ) -> ScheduledAttempt | None:
        """Select the exact queued attempt only while its claim is current."""

        if attempt.execution_fence is not None:
            return attempt
        coordinator = self._require_coordinator()
        try:
            with self._plane.transaction() as cursor:
                self._lock_claim_job(cursor, attempt.claim)
                self._current_claim_row(cursor, attempt.claim, states=("claimed",))
        except StaleOccurrenceClaimError as exc:
            self._settle_unstarted_operation(
                attempt.claim,
                attempt.operation_id,
                attempt.operation_state,
                terminal_code=exc.terminal_code,
            )
            return None
        selected = coordinator.claim_operation(
            AdmissionClass.SCHEDULED, attempt.operation_id
        )
        if selected is None:
            return None
        try:
            with self._plane.transaction() as cursor:
                self._lock_claim_job(cursor, attempt.claim)
                self._current_claim_row(cursor, attempt.claim, states=("claimed",))
        except StaleOccurrenceClaimError as exc:
            try:
                coordinator.terminalize(
                    selected.fence,
                    state=(
                        OperationState.CANCELLED
                        if exc.terminal_code
                        else OperationState.RETRYABLE
                    ),
                    terminal_code=exc.terminal_code or "claim_lost",
                    safe_summary=(
                        "Scheduled job cancelled before start"
                        if exc.terminal_code
                        else "Scheduled claim lost before start"
                    ),
                    retry_after_ms=None if exc.terminal_code else 0,
                )
            except StaleExecutionFenceError:
                pass
            return None
        return replace(
            attempt,
            operation_state=selected.operation.state,
            execution_fence=selected.fence,
        )

    def start_attempt(
        self, attempt: ScheduledAttempt, *, lease_seconds: int = 15
    ) -> ScheduledAttempt:
        """Fenced claimed→running transition and unique ``job_run`` insert."""

        self._validate_claim_settings(
            attempt.claim.lease_owner, limit=1, lease_seconds=lease_seconds
        )
        if attempt.execution_fence is None:
            raise StaleOccurrenceClaimError("attempt has no selected execution")
        coordinator = self._require_coordinator()
        run_id = uuid.uuid4()
        correlation_id = uuid.uuid4()
        with coordinator.fenced_transaction(attempt.execution_fence) as cursor:
            self._current_claim_row(cursor, attempt.claim, states=("claimed",))
            try:
                record = self._plane.repository.start_claim_attempt(
                    cursor,
                    owner_id=str(attempt.claim.job["user_id"]),
                    job_id=str(attempt.claim.job["id"]),
                    occurrence_id=str(attempt.claim.occurrence_id),
                    attempt_number=attempt.claim.attempt_number,
                    claim_generation=attempt.claim.claim_generation,
                    lease_token=str(attempt.claim.lease_token),
                    operation_id=str(attempt.operation_id),
                    operation_execution_generation=(
                        attempt.execution_fence.execution_generation
                    ),
                    run_id=str(run_id),
                    correlation_id=str(correlation_id),
                    lease_seconds=lease_seconds,
                )
            except PlaneError as exc:
                message = (
                    "job_run fence conflict"
                    if exc.code == "scheduled_run_fence_conflict"
                    else None
                )
                raise _stale_plane_error(exc, message=message) from exc
            run_id = uuid.UUID(record.run_id)
        return replace(
            attempt,
            operation_state=OperationState.RUNNING,
            run_id=run_id,
        )

    def mark_claim_retryable(
        self,
        claim: OccurrenceClaim,
        *,
        error_code: str,
        retry_after_seconds: int = 1,
    ) -> None:
        """Release a current claim for a later attempt without an effect."""

        if not _SAFE_NAME_RE.fullmatch(error_code):
            raise ValueError("error_code must be bounded snake_case")
        if retry_after_seconds < 0 or retry_after_seconds > 86_400:
            raise ValueError("retry_after_seconds is out of range")
        with self._plane.transaction() as cursor:
            current = self._current_claim_row(
                cursor, claim, states=("claimed", "running")
            )
            operation_id = _as_uuid(current.get("current_operation_id"))
            execution_generation = (
                None
                if current.get("operation_execution_generation") is None
                else int(current["operation_execution_generation"])
            )
            try:
                self._plane.repository.mark_claim_retryable(
                    cursor,
                    owner_id=str(claim.job["user_id"]),
                    occurrence_id=str(claim.occurrence_id),
                    attempt_number=claim.attempt_number,
                    claim_generation=claim.claim_generation,
                    lease_token=str(claim.lease_token),
                    lease_owner=claim.lease_owner,
                    operation_id=(
                        None if operation_id is None else str(operation_id)
                    ),
                    operation_execution_generation=execution_generation,
                    error_code=error_code,
                    retry_after_seconds=retry_after_seconds,
                )
            except PlaneError as exc:
                raise _stale_plane_error(exc) from exc

    @staticmethod
    def _validate_effect_identity(
        *, effect_kind: str, effect_key: str, payload_digest: str
    ) -> None:
        if not _SAFE_NAME_RE.fullmatch(effect_kind):
            raise ValueError("effect_kind must be bounded snake_case")
        if not (1 <= len(effect_key) <= 256):
            raise ValueError("effect_key must be 1..256 characters")
        if not _SHA256_RE.fullmatch(payload_digest):
            raise ValueError("payload_digest must be lowercase SHA-256")

    def _assert_effect_authority(
        self, cursor: Any, attempt: ScheduledAttempt
    ) -> Dict[str, Any]:
        if attempt.execution_fence is None or attempt.run_id is None:
            raise StaleOccurrenceClaimError("attempt has not started")
        row = self._current_claim_row(cursor, attempt.claim, states=("running",))
        if (
            _as_uuid(row.get("current_operation_id")) != attempt.operation_id
            or int(row.get("operation_execution_generation") or 0)
            != attempt.execution_fence.execution_generation
        ):
            raise StaleOccurrenceClaimError("stale_occurrence_claim")
        return row

    @staticmethod
    def _effect_attempt_arguments(attempt: ScheduledAttempt) -> Dict[str, Any]:
        if attempt.execution_fence is None:
            raise StaleOccurrenceClaimError("attempt has no execution fence")
        return {
            "owner_id": str(attempt.claim.job["user_id"]),
            "occurrence_id": str(attempt.claim.occurrence_id),
            "claim_generation": attempt.claim.claim_generation,
            "lease_token": str(attempt.claim.lease_token),
            "lease_owner": attempt.claim.lease_owner,
            "operation_id": str(attempt.operation_id),
            "operation_execution_generation": (
                attempt.execution_fence.execution_generation
            ),
        }

    def reserve_effect(
        self,
        attempt: ScheduledAttempt,
        *,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
    ) -> EffectReservation:
        """Reserve or reconcile one stable AstralDeep-controlled effect."""

        self._validate_effect_identity(
            effect_kind=effect_kind,
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        if attempt.execution_fence is None:
            raise StaleOccurrenceClaimError("attempt has no execution fence")
        coordinator = self._require_coordinator()
        with coordinator.fenced_transaction(attempt.execution_fence) as cursor:
            self._assert_effect_authority(cursor, attempt)
            try:
                result = self._plane.repository.reserve_effect_for_attempt(
                    cursor,
                    **self._effect_attempt_arguments(attempt),
                    effect_kind=effect_kind,
                    effect_key=effect_key,
                    payload_digest=payload_digest,
                )
            except PlaneError as exc:
                if exc.code == "effect_idempotency_conflict":
                    raise EffectIdempotencyConflictError(exc.code) from exc
                raise _stale_plane_error(exc) from exc
            return EffectReservation(
                state=result.state.value,
                created=result.created,
                ambiguous=result.ambiguous,
            )

    def reserve_atomic_chat_effect(
        self,
        attempt: ScheduledAttempt,
        *,
        effect_key: str,
        payload_digest: str,
    ) -> EffectReservation:
        """Reserve a database-only chat effect with crash-safe reassignment.

        A ``reserved`` row from an older attempt is recoverable here because
        scheduled chat messages exist only in memory until the same PostgreSQL
        transaction inserts them and marks this row ``published``.  Therefore
        a committed ``reserved`` state proves that no target message escaped.
        """

        effect_kind = "chat_history"
        self._validate_effect_identity(
            effect_kind=effect_kind,
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        if attempt.execution_fence is None:
            raise StaleOccurrenceClaimError("attempt has no execution fence")
        coordinator = self._require_coordinator()
        with coordinator.fenced_transaction(attempt.execution_fence) as cursor:
            self._assert_effect_authority(cursor, attempt)
            try:
                result = self._plane.repository.reserve_effect_for_attempt(
                    cursor,
                    **self._effect_attempt_arguments(attempt),
                    effect_kind=effect_kind,
                    effect_key=effect_key,
                    payload_digest=payload_digest,
                    recover_reserved=True,
                )
            except PlaneError as exc:
                if exc.code == "effect_idempotency_conflict":
                    raise EffectIdempotencyConflictError(exc.code) from exc
                raise _stale_plane_error(exc) from exc
            return EffectReservation(
                state=result.state.value,
                created=result.created,
                ambiguous=result.ambiguous,
            )
    def publish_staged_chat_effect(
        self,
        attempt: ScheduledAttempt,
        batch: ScheduledHistoryBatch,
        *,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
    ) -> EffectReservation:
        """Atomically publish one conversation revision and its effect row."""

        if effect_kind != "chat_history":
            raise ValueError("staged chat publication requires chat_history")
        self._validate_effect_identity(
            effect_kind=effect_kind,
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        if batch.chat_id != effect_key:
            raise ValueError("chat effect key must equal the staged chat identity")
        if str(attempt.job["user_id"]) != batch.user_id:
            raise ValueError("staged chat owner differs from the occurrence owner")
        if not batch.messages:
            raise ValueError("scheduled chat produced no history messages")
        if attempt.execution_fence is None:
            raise StaleOccurrenceClaimError("attempt has no execution fence")
        commit_id = _as_uuid(batch.conversation_commit_id)
        request_generation = _as_uuid(batch.request_generation)
        if (
            commit_id is None
            or commit_id.version != 4
            or request_generation is None
            or request_generation.version != 4
            or attempt.request_generation != request_generation
            or isinstance(batch.base_render_revision, bool)
            or not isinstance(batch.base_render_revision, int)
            or isinstance(batch.committed_render_revision, bool)
            or not isinstance(batch.committed_render_revision, int)
            or batch.base_render_revision < 0
            or batch.committed_render_revision != batch.base_render_revision + 1
        ):
            raise ValueError("scheduled conversation commit metadata is invalid")
        validated_layouts = []
        seen_layouts = set()
        for layout in batch.canvas_layouts:
            if not isinstance(layout, dict):
                raise ValueError("scheduled canvas layout is invalid")
            key = layout.get("layout_key")
            position = layout.get("position")
            tree = layout.get("layout")
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 512
                or key in seen_layouts
                or isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
                or not isinstance(tree, list)
            ):
                raise ValueError("scheduled canvas layout is invalid")
            try:
                encoded_tree = json.dumps(
                    tree,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("scheduled canvas layout is invalid") from exc
            seen_layouts.add(key)
            validated_layouts.append((key, position, encoded_tree))

        coordinator = self._require_coordinator()
        plane_publication = PlaneStagedChatPublication(
            conversation_id=batch.chat_id,
            owner_id=batch.user_id,
            create_conversation_if_missing=batch.create_chat_if_missing,
            agent_id=batch.agent_id,
            requested_title=batch.requested_title,
            messages=tuple(
                PlaneStagedChatMessage(
                    role=message.role,
                    content=message.content,
                    title_source=message.title_source,
                    timestamp_ms=message.timestamp_ms,
                )
                for message in batch.messages
            ),
            publication_id=str(commit_id),
            request_generation=str(request_generation),
            base_render_revision=batch.base_render_revision,
            committed_render_revision=batch.committed_render_revision,
            layouts=tuple(
                PlaneStagedChatLayout(
                    layout_key=layout_key,
                    position=position,
                    tree=tuple(json.loads(encoded_tree)),
                )
                for layout_key, position, encoded_tree in validated_layouts
            ),
        )
        with coordinator.fenced_transaction(attempt.execution_fence) as cursor:
            self._assert_effect_authority(cursor, attempt)
            try:
                result = self._plane.repository.publish_staged_chat_effect(
                    cursor,
                    **self._effect_attempt_arguments(attempt),
                    effect_key=effect_key,
                    payload_digest=payload_digest,
                    publication=plane_publication,
                )
            except PlaneError as exc:
                if exc.code == "effect_idempotency_conflict":
                    raise EffectIdempotencyConflictError(exc.code) from exc
                raise _stale_plane_error(exc) from exc
            return EffectReservation(
                result.state.value, result.created, result.ambiguous
            )
    def publish_effect(
        self,
        attempt: ScheduledAttempt,
        *,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
        downstream_receipt_digest: str | None = None,
    ) -> EffectReservation:
        """Publish a reservation only under the exact creating fences."""

        self._validate_effect_identity(
            effect_kind=effect_kind,
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        if downstream_receipt_digest is not None and not _SHA256_RE.fullmatch(
            downstream_receipt_digest
        ):
            raise ValueError("downstream_receipt_digest must be lowercase SHA-256")
        if attempt.execution_fence is None:
            raise StaleOccurrenceClaimError("attempt has no execution fence")
        coordinator = self._require_coordinator()
        with coordinator.fenced_transaction(attempt.execution_fence) as cursor:
            self._assert_effect_authority(cursor, attempt)
            try:
                result = self._plane.repository.publish_reserved_effect(
                    cursor,
                    **self._effect_attempt_arguments(attempt),
                    effect_kind=effect_kind,
                    effect_key=effect_key,
                    payload_digest=payload_digest,
                    downstream_receipt_digest=downstream_receipt_digest,
                )
            except PlaneError as exc:
                if exc.code == "effect_idempotency_conflict":
                    raise EffectIdempotencyConflictError(exc.code) from exc
                raise _stale_plane_error(exc) from exc
            return EffectReservation(
                result.state.value, result.created, result.ambiguous
            )

    def fail_effect(
        self,
        attempt: ScheduledAttempt,
        *,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
        failure_code: str,
    ) -> EffectReservation:
        """Mark a reservation failed only when no visible effect occurred."""

        self._validate_effect_identity(
            effect_kind=effect_kind,
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        if not _SAFE_NAME_RE.fullmatch(failure_code):
            raise ValueError("failure_code must be bounded snake_case")
        if attempt.execution_fence is None:
            raise StaleOccurrenceClaimError("attempt has no execution fence")
        coordinator = self._require_coordinator()
        with coordinator.fenced_transaction(attempt.execution_fence) as cursor:
            self._assert_effect_authority(cursor, attempt)
            try:
                result = self._plane.repository.fail_reserved_effect(
                    cursor,
                    **self._effect_attempt_arguments(attempt),
                    effect_kind=effect_kind,
                    effect_key=effect_key,
                    payload_digest=payload_digest,
                    failure_code=failure_code,
                )
            except PlaneError as exc:
                if exc.code == "effect_idempotency_conflict":
                    raise EffectIdempotencyConflictError(exc.code) from exc
                raise _stale_plane_error(exc) from exc
            return EffectReservation(
                result.state.value, result.created, result.ambiguous
            )

    def finish_attempt(
        self,
        attempt: ScheduledAttempt,
        *,
        outcome: str,
        summary: str | None = None,
        auth_ref: str | None = None,
        retryable: bool = False,
        result_code: str | None = None,
        retry_after_seconds: int = 1,
    ) -> Dict[str, Any]:
        """Commit one fenced job-run and occurrence terminal/retry state."""

        if attempt.execution_fence is None or attempt.run_id is None:
            raise StaleOccurrenceClaimError("attempt has not started")
        if outcome not in {"success", "failure", "interrupted", "skipped_auth"}:
            raise ValueError("unsupported job_run outcome")
        if summary is not None and len(summary) > 2_000:
            summary = summary[:2_000]
        if result_code is not None and not _SAFE_NAME_RE.fullmatch(result_code):
            raise ValueError("result_code must be bounded snake_case")
        if retry_after_seconds < 0 or retry_after_seconds > 86_400:
            raise ValueError("retry_after_seconds is out of range")

        if retryable:
            occurrence_state = "retryable"
            job_outcome = "failure" if outcome == "success" else outcome
            safe_code = result_code or "operation_failed"
        elif outcome == "success":
            occurrence_state = "completed"
            job_outcome = "success"
            safe_code = result_code or "success"
        else:
            occurrence_state = "failed"
            job_outcome = outcome
            safe_code = result_code or (
                "authorization_unavailable"
                if outcome == "skipped_auth"
                else "operation_failed"
            )

        coordinator = self._require_coordinator()
        with coordinator.fenced_transaction(attempt.execution_fence) as cursor:
            self._assert_effect_authority(cursor, attempt)
            try:
                record = self._plane.repository.finish_claim_attempt(
                    cursor,
                    **self._effect_attempt_arguments(attempt),
                    job_id=str(attempt.claim.job["id"]),
                    attempt_number=attempt.claim.attempt_number,
                    run_id=str(attempt.run_id),
                    job_outcome=job_outcome,
                    occurrence_state=PlaneOccurrenceState(occurrence_state),
                    safe_code=safe_code,
                    summary=summary,
                    auth_ref=auth_ref,
                    retry_after_seconds=retry_after_seconds,
                )
            except PlaneError as exc:
                message = (
                    "job_run is no longer running"
                    if exc.code == "scheduled_run_fence_conflict"
                    else None
                )
                raise _stale_plane_error(exc, message=message) from exc
            return {
                "occurrence_id": record.occurrence_id,
                "job_id": record.job_id,
                "owner_user_id": record.owner_id,
                "scheduled_for": record.scheduled_for,
                "state": record.state.value,
                "claim_generation": record.claim_generation,
                "lease_token": record.lease_token,
                "lease_owner": record.lease_owner,
                "lease_expires_at": record.lease_expires_at,
                "attempt_count": record.attempt_count,
                "current_operation_id": record.operation_id,
                "operation_execution_generation": (
                    record.operation_execution_generation
                ),
                "terminal_at": record.terminal_at,
                "next_attempt_at": record.next_attempt_at,
                "result_code": record.result_code,
                "last_error_code": record.last_error_code,
            }

    # ── Runs ─────────────────────────────────────────────────────────────

    def start_run(self, job_id: str, user_id: str, correlation_id: str) -> str:
        run_id = str(uuid.uuid4())
        self._plane.call(
            self._plane.repository.start_legacy_run,
            run_id=run_id,
            job_id=job_id,
            owner_id=user_id,
            correlation_id=correlation_id,
            started_at=_now_ms(),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        summary: Optional[str] = None,
        auth_ref: Optional[str] = None,
    ) -> None:
        self._plane.call(
            self._plane.repository.finish_run_for_administration,
            run_id=run_id,
            outcome=outcome,
            summary=summary,
            auth_ref=auth_ref,
            ended_at=_now_ms(),
        )

    def list_runs(
        self, user_id: str, job_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        runs = self._plane.call(
            self._plane.repository.list_runs,
            owner_id=user_id,
            job_id=job_id,
            limit=limit,
        )
        return [self._run_dict(run) for run in runs]

    def reconcile_interrupted(self) -> int:
        """On startup, mark any run left 'running' (by a crash/restart) as interrupted."""
        return self._plane.call(
            self._plane.repository.reconcile_interrupted_for_administration,
            ended_at=_now_ms(),
        )
