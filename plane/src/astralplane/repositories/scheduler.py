"""Durable scheduling: job definitions, due occurrences, run attempts, and the effect
ledger, including the atomic staged-chat publish. Neutral transitions only;
work-admission locks live in work_admission.py; used by scheduler/store.py.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from astralplane.contracts import Record, Transaction
from astralplane.database.dialect import dialect_of
from astralplane.errors import PlaneError
from astralplane.repositories.scheduler_models import (
    MAX_SPEND,
    EpisodeAdmission,
    JobStopOutcome,
    ScheduledJobPolicy,
)

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class OccurrenceState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYABLE = "retryable"
    CANCELLED = "cancelled"


class EffectState(StrEnum):
    RESERVED = "reserved"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    job_id: str
    owner_id: str
    name: str
    instruction: str
    schedule_kind: str
    schedule_expression: str
    timezone: str
    status: str
    next_run_at: int | None
    created_at: int
    updated_at: int
    agent_id: str | None = None
    consented_scopes: tuple[str, ...] = ()
    delivery: str = "in_app"
    target_chat_id: str | None = None
    last_run_at: int | None = None
    offline_grant_id: str | None = None

    def __post_init__(self) -> None:
        _uuid("job_id", self.job_id, version=4)
        _required("owner_id", self.owner_id)
        _required("name", self.name, maximum=256)
        _required("instruction", self.instruction, maximum=65_536)
        if self.schedule_kind not in {"one_shot", "interval", "cron"}:
            raise ValueError("schedule_kind is not supported")
        _required("schedule_expression", self.schedule_expression, maximum=512)
        _required("timezone", self.timezone, maximum=128)
        if self.status not in {"active", "paused", "expired", "completed", "disabled"}:
            raise ValueError("scheduled job status is not supported")
        if self.created_at < 0 or self.updated_at < self.created_at:
            raise ValueError("scheduled job timestamps are invalid")
        if self.agent_id is not None:
            _required("agent_id", self.agent_id, maximum=256)
        if len(self.consented_scopes) > 64 or any(
            not isinstance(scope, str)
            or not scope
            or len(scope) > 128
            for scope in self.consented_scopes
        ):
            raise ValueError("consented_scopes must contain at most 64 bounded strings")
        if len(set(self.consented_scopes)) != len(self.consented_scopes):
            raise ValueError("consented_scopes must not contain duplicates")
        if self.delivery != "in_app":
            raise ValueError("only in_app scheduled delivery is supported")
        if self.target_chat_id is not None:
            _required("target_chat_id", self.target_chat_id, maximum=256)
        if self.last_run_at is not None and self.last_run_at < 0:
            raise ValueError("last_run_at cannot be negative")
        if self.offline_grant_id is not None:
            _uuid("offline_grant_id", self.offline_grant_id)


@dataclass(frozen=True, slots=True)
class RunNowMaterialization:
    occurrence_id: str
    job_id: str
    owner_id: str
    scheduled_for: datetime
    state: OccurrenceState
    created: bool


@dataclass(frozen=True, slots=True)
class JobRunRecord:
    run_id: str
    job_id: str
    owner_id: str
    started_at: int
    ended_at: int | None
    outcome: str
    auth_ref: str | None
    correlation_id: str
    summary: str | None
    occurrence_id: str | None
    attempt_number: int | None
    operation_id: str | None
    operation_execution_generation: int | None
    occurrence_claim_generation: int | None


@dataclass(frozen=True, slots=True)
class RecoveredAttemptRecord:
    operation_id: str


@dataclass(frozen=True, slots=True)
class ClaimedOccurrenceRecord:
    occurrence: ScheduledOccurrence
    job: ScheduledJob
    parent_operation_id: str | None


@dataclass(frozen=True, slots=True)
class DueScanContinuation:
    definition: tuple[int, str] | None = None
    occurrence: tuple[datetime, str] | None = None
    definition_owner: str | None = None
    occurrence_owner: str | None = None

    def __post_init__(self) -> None:
        for name in ("definition", "occurrence"):
            position = getattr(self, name)
            if position is None:
                continue
            if not isinstance(position, tuple) or len(position) != 2:
                raise ValueError("scan position must contain a timestamp and UUID")
            if name == "definition":
                _millisecond("definition scan timestamp", position[0])
                if position[0] > 2**63 - 1:
                    raise ValueError("definition scan timestamp exceeds storage range")
            else:
                if not isinstance(position[0], datetime):
                    raise ValueError("occurrence scan timestamp must be aware")
                _aware("occurrence scan timestamp", position[0])
            _uuid("scan identifier", position[1], version=4)
        for owner in (self.definition_owner, self.occurrence_owner):
            if owner is not None:
                _required("scan owner", owner)


@dataclass(frozen=True, slots=True)
class DueClaimBatch:
    claims: tuple[ClaimedOccurrenceRecord, ...]
    recovered_attempts: tuple[RecoveredAttemptRecord, ...]
    ineligible_job_ids: tuple[str, ...]
    continuation: DueScanContinuation = DueScanContinuation()


@dataclass(frozen=True, slots=True)
class EffectReservationOutcome:
    state: EffectState
    created: bool
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class StagedChatMessage:
    role: str
    content: str
    title_source: str
    timestamp_ms: int


@dataclass(frozen=True, slots=True)
class StagedChatLayout:
    layout_key: str
    position: int
    tree: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class StagedChatPublication:
    conversation_id: str
    owner_id: str
    create_conversation_if_missing: bool
    agent_id: str | None
    requested_title: str | None
    messages: tuple[StagedChatMessage, ...]
    publication_id: str
    request_generation: str
    base_render_revision: int
    committed_render_revision: int
    layouts: tuple[StagedChatLayout, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduledOccurrence:
    occurrence_id: str
    job_id: str
    owner_id: str
    scheduled_for: datetime
    state: OccurrenceState
    claim_generation: int
    lease_token: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempt_count: int
    operation_id: str | None
    operation_execution_generation: int | None
    terminal_at: datetime | None = None
    next_attempt_at: datetime | None = None
    result_code: str | None = None
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class EffectRecord:
    occurrence_id: str
    effect_kind: str
    effect_key: str
    payload_digest: str
    state: EffectState
    operation_id: str | None
    operation_execution_generation: int
    occurrence_claim_generation: int
    downstream_receipt_digest: str | None
    published_at: datetime | None


class SchedulerRepository:
    def count_active_jobs(self, transaction: Transaction, *, owner_id: str) -> int:
        _required("owner_id", owner_id)
        row = transaction.fetch_one(
            "SELECT COUNT(*) AS n FROM scheduled_job "
            "WHERE user_id = %s AND status = 'active'",
            (owner_id,),
        )
        return 0 if row is None else int(row["n"])

    def create_job_definition(
        self,
        transaction: Transaction,
        job: ScheduledJob,
    ) -> ScheduledJob:
        row = transaction.fetch_one(
            """
            INSERT INTO scheduled_job (
                id, user_id, agent_id, name, instruction, schedule_kind,
                schedule_expr, timezone, consented_scopes, delivery, status,
                target_chat_id, next_run_at, last_run_at, offline_grant_id,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            (
                job.job_id,
                job.owner_id,
                job.agent_id,
                job.name,
                job.instruction,
                job.schedule_kind,
                job.schedule_expression,
                job.timezone,
                json.dumps(list(job.consented_scopes), separators=(",", ":")),
                job.delivery,
                job.status,
                job.target_chat_id,
                job.next_run_at,
                job.last_run_at,
                job.offline_grant_id,
                job.created_at,
                job.updated_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM scheduled_job WHERE id = %s AND user_id = %s FOR UPDATE",
                (job.job_id, job.owner_id),
            )
        if row is None or _job(row) != job:
            raise PlaneError(
                "scheduled job identity has conflicting semantics",
                code="scheduled_job_conflict",
                metadata={"owner_id": job.owner_id},
            )
        return job

    def list_jobs(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        limit: int = 1000,
    ) -> tuple[ScheduledJob, ...]:
        _required("owner_id", owner_id)
        _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM scheduled_job
            WHERE user_id = %s
            ORDER BY created_at DESC, id
            LIMIT %s
            """,
            (owner_id, limit),
        )
        return tuple(_job(row) for row in rows)

    def set_job_offline_grant(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        grant_id: str | None,
        updated_at: int,
    ) -> bool:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        if grant_id is not None:
            _uuid("grant_id", grant_id)
        _millisecond("updated_at", updated_at)
        result = transaction.execute(
            """
            UPDATE scheduled_job
            SET offline_grant_id = %s, updated_at = %s
            WHERE id = %s AND user_id = %s
            """,
            (grant_id, updated_at, job_id, owner_id),
        )
        _zero_or_one(result.rowcount, "scheduled job offline-grant update")
        return result.rowcount == 1

    def set_job_status(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        status: str,
        updated_at: int,
    ) -> bool:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        if status not in {"active", "paused", "expired", "completed", "disabled"}:
            raise ValueError("scheduled job status is not supported")
        _millisecond("updated_at", updated_at)
        result = transaction.execute(
            """
            UPDATE scheduled_job SET status = %s, updated_at = %s
            WHERE id = %s AND user_id = %s
            """,
            (status, updated_at, job_id, owner_id),
        )
        _zero_or_one(result.rowcount, "scheduled job status update")
        return result.rowcount == 1

    def transition_job_and_list_unstarted(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        status: str,
    ) -> tuple[ScheduledOccurrence, ...] | None:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        if status not in {"paused", "disabled"}:
            raise ValueError("cancelling unstarted occurrences requires paused or disabled")
        job = transaction.fetch_one(
            """
            SELECT id FROM scheduled_job
            WHERE id = %s AND user_id = %s
            FOR UPDATE
            """,
            (job_id, owner_id),
        )
        if job is None:
            return None
        result = transaction.execute(
            """
            UPDATE scheduled_job
            SET status = %s,
                updated_at = (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT
            WHERE id = %s AND user_id = %s
            """,
            (status, job_id, owner_id),
        )
        if result.rowcount != 1:
            raise PlaneError(
                "scheduled definition status fence was lost",
                code="scheduled_job_status_conflict",
                metadata={"owner_id": owner_id},
            )
        rows = transaction.fetch_all(
            """
            SELECT occurrence.* FROM scheduled_occurrence AS occurrence
            JOIN scheduled_job AS job ON job.id = occurrence.job_id
            WHERE occurrence.job_id = %s
              AND occurrence.owner_user_id = %s
              AND job.user_id = %s
              AND occurrence.state IN ('pending', 'retryable', 'claimed')
            ORDER BY occurrence.occurrence_id
            FOR UPDATE OF occurrence
            """,
            (job_id, owner_id, owner_id),
        )
        return tuple(_occurrence(row) for row in rows)

    def cancel_unstarted_occurrence(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        expected_operation_id: str | None,
        terminal_code: str,
    ) -> bool:
        _required("owner_id", owner_id)
        _uuid("occurrence_id", occurrence_id, version=4)
        if expected_operation_id is not None:
            _uuid("expected_operation_id", expected_operation_id, version=4)
        _bounded_code("terminal_code", terminal_code)
        row = transaction.fetch_one(
            """
            UPDATE scheduled_occurrence
            SET state = 'cancelled', lease_token = NULL, lease_owner = NULL,
                lease_expires_at = NULL, next_attempt_at = NULL,
                terminal_at = clock_timestamp(), result_code = %s,
                last_error_code = %s, updated_at = clock_timestamp()
            WHERE occurrence_id = %s AND owner_user_id = %s
              AND current_operation_id IS NOT DISTINCT FROM %s
              AND state IN ('pending', 'retryable', 'claimed')
            RETURNING *
            """,
            (
                terminal_code,
                terminal_code,
                occurrence_id,
                owner_id,
                expected_operation_id,
            ),
        )
        if row is not None:
            return True
        current = transaction.fetch_one(
            """
            SELECT state, current_operation_id
            FROM scheduled_occurrence
            WHERE occurrence_id = %s AND owner_user_id = %s
            FOR UPDATE
            """,
            (occurrence_id, owner_id),
        )
        if current is None or str(current["state"]) not in {
            "pending",
            "retryable",
            "claimed",
        }:
            return False
        observed_operation = (
            None
            if current.get("current_operation_id") is None
            else str(current["current_operation_id"])
        )
        if observed_operation != expected_operation_id:
            raise PlaneError(
                "scheduled occurrence operation changed during cancellation",
                code="scheduled_occurrence_operation_conflict",
                metadata={"owner_id": owner_id},
            )
        raise PlaneError(
            "scheduled occurrence cancellation lost its state fence",
            code="stale_occurrence_fence",
            metadata={"owner_id": owner_id},
        )

    def update_job_after_run_for_administration(
        self,
        transaction: Transaction,
        *,
        job_id: str,
        last_run_at: int,
        next_run_at: int | None,
        completed: bool,
        updated_at: int,
    ) -> bool:
        _uuid("job_id", job_id, version=4)
        _millisecond("last_run_at", last_run_at)
        if next_run_at is not None:
            _millisecond("next_run_at", next_run_at)
        if not isinstance(completed, bool):
            raise ValueError("completed must be a boolean")
        _millisecond("updated_at", updated_at)
        result = transaction.execute(
            """
            UPDATE scheduled_job
            SET status = CASE WHEN %s THEN 'completed' ELSE status END,
                last_run_at = %s, next_run_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (completed, last_run_at, next_run_at, updated_at, job_id),
        )
        _zero_or_one(result.rowcount, "scheduled job cadence projection")
        return result.rowcount == 1

    def list_due_jobs_for_administration(
        self,
        query: Transaction,
        *,
        due_at_ms: int,
        limit: int = 1000,
    ) -> tuple[ScheduledJob, ...]:
        _millisecond("due_at_ms", due_at_ms)
        _limit(limit)
        rows = query.fetch_all(
            """
            SELECT * FROM scheduled_job
            WHERE status = 'active' AND next_run_at IS NOT NULL
              AND next_run_at <= %s
            ORDER BY next_run_at, id
            LIMIT %s
            """,
            (due_at_ms, limit),
        )
        return tuple(_job(row) for row in rows)

    def materialize_and_claim_due_for_administration(
        self,
        transaction: Transaction,
        *,
        instance_id: str,
        limit: int,
        lease_seconds: int,
        eligible: Callable[[ScheduledJob], bool],
        next_run: Callable[[ScheduledJob, int], int | None],
        continuation: DueScanContinuation | None = None,
        scan_limit: int | None = None,
    ) -> DueClaimBatch:
        _instance_id(instance_id)
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        _limit(limit)
        scan_limit = min(1000, max(32, limit * 4)) if scan_limit is None else scan_limit
        if not isinstance(scan_limit, int) or isinstance(scan_limit, bool):
            raise ValueError("scan_limit must be an integer")
        _limit(scan_limit)
        if continuation is None:
            continuation = DueScanContinuation()
        if not isinstance(continuation, DueScanContinuation):
            raise ValueError("continuation must be a scheduler scan hint")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool):
            raise ValueError("lease_seconds must be an integer")
        if not 5 <= lease_seconds <= 60:
            raise ValueError("lease_seconds must be between 5 and 60")
        if not callable(eligible) or not callable(next_run):
            raise ValueError("scheduler policy callbacks must be callable")
        clock = transaction.fetch_one("SELECT clock_timestamp() AS now")
        if clock is None:
            raise PlaneError("database clock was unavailable", code="scheduler_clock_missing")
        observed_at = clock["now"]
        _aware("observed_at", observed_at)
        observed_at = observed_at.astimezone(UTC)
        observed_ms = int(observed_at.timestamp() * 1000)

        due_rows = _scan_rows(
            transaction,
            """
            SELECT * FROM scheduled_job
            WHERE status = 'active' AND next_run_at IS NOT NULL
              AND next_run_at <= %s
              {position}
            ORDER BY next_run_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (observed_ms,),
            columns="(next_run_at, id)",
            position=continuation.definition,
            limit=scan_limit,
        )
        ineligible_ids: list[str] = []
        due_candidates: list[tuple[ScheduledJob, Record]] = []
        for row in due_rows:
            job = _job(row)
            if not bool(eligible(job)):
                ineligible_ids.append(job.job_id)
                continue
            due_candidates.append((job, row))
        selected_due, definition_owner = _rotate_owners(
            due_candidates, limit=limit, last_owner=continuation.definition_owner,
        )
        for job, _ in selected_due:
            if job.next_run_at is None:  # pragma: no cover
                raise PlaneError(
                    "locked due definition has no cadence timestamp",
                    code="scheduler_record_invalid",
                    metadata={"owner_id": job.owner_id},
                )
            scheduled_ms = job.next_run_at
            scheduled_for = datetime.fromtimestamp(scheduled_ms / 1000, tz=UTC)
            transaction.execute(
                """
                INSERT INTO scheduled_occurrence (
                    occurrence_id, job_id, owner_user_id, scheduled_for,
                    state, first_eligible_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s)
                ON CONFLICT (job_id, scheduled_for) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    job.job_id,
                    job.owner_id,
                    scheduled_for,
                    observed_at,
                    observed_at,
                    observed_at,
                ),
            )
            following = next_run(job, scheduled_ms)
            if following is not None:
                _millisecond("next_run callback result", following)
                if following <= scheduled_ms:
                    raise ValueError("next_run callback must advance the cadence")
            completed = job.schedule_kind == "one_shot" or following is None
            # App clock may lead DB's; never regress the mod timestamp
            advanced = transaction.execute(
                """
                UPDATE scheduled_job
                SET next_run_at = %s,
                    status = CASE WHEN %s THEN 'completed' ELSE status END,
                    updated_at = GREATEST(updated_at, %s)
                WHERE id = %s AND user_id = %s AND next_run_at = %s
                """,
                (
                    following,
                    completed,
                    observed_ms,
                    job.job_id,
                    job.owner_id,
                    scheduled_ms,
                ),
            )
            if advanced.rowcount != 1:
                raise PlaneError(
                    "scheduled cadence advancement lost its definition fence",
                    code="scheduled_job_cadence_conflict",
                    metadata={"owner_id": job.owner_id},
                )

        candidates = _scan_rows(
            transaction,
            """
            SELECT occurrence.*, to_jsonb(job) AS job_record
            FROM scheduled_occurrence AS occurrence
            JOIN scheduled_job AS job ON job.id = occurrence.job_id
            WHERE (
                job.status = 'active'
                OR (job.status = 'completed' AND job.schedule_kind = 'one_shot')
            ) AND (
                (occurrence.state = 'pending' AND occurrence.scheduled_for <= %s)
                OR (occurrence.state = 'retryable'
                    AND (occurrence.next_attempt_at IS NULL
                         OR occurrence.next_attempt_at <= %s))
                OR (occurrence.state IN ('claimed', 'running')
                    AND occurrence.lease_expires_at <= %s)
            )
            {position}
            ORDER BY occurrence.scheduled_for, occurrence.occurrence_id
            FOR UPDATE OF occurrence SKIP LOCKED
            LIMIT %s
            """,
            (observed_at, observed_at, observed_at),
            columns="(occurrence.scheduled_for, occurrence.occurrence_id)",
            position=continuation.occurrence,
            limit=scan_limit,
        )
        claims: list[ClaimedOccurrenceRecord] = []
        recovered: list[RecoveredAttemptRecord] = []
        eligible_candidates: list[tuple[ScheduledJob, Record]] = []
        for row in candidates:
            raw_job = row.get("job_record")
            if not isinstance(raw_job, Mapping):
                raise PlaneError(
                    "claim candidate has an invalid job projection",
                    code="scheduler_record_invalid",
                )
            job = _job(raw_job)
            if not bool(eligible(job)):
                if job.job_id not in ineligible_ids:
                    ineligible_ids.append(job.job_id)
                continue
            eligible_candidates.append((job, row))
        selected_occurrences, occurrence_owner = _rotate_owners(
            eligible_candidates, limit=limit, last_owner=continuation.occurrence_owner,
        )
        for job, row in selected_occurrences:
            occurrence = _occurrence(row)
            parent_operation_id = occurrence.operation_id
            if parent_operation_id is not None:
                recovered.append(RecoveredAttemptRecord(parent_operation_id))
            lease_token = str(uuid.uuid4())
            claimed = transaction.fetch_one(
                """
                UPDATE scheduled_occurrence
                SET state = 'claimed', lease_token = %s,
                    claim_generation = claim_generation + 1,
                    lease_owner = %s,
                    lease_expires_at = %s + (%s * INTERVAL '1 second'),
                    attempt_count = attempt_count + 1,
                    current_operation_id = NULL,
                    operation_execution_generation = NULL,
                    started_at = NULL, terminal_at = NULL,
                    next_attempt_at = NULL, result_code = NULL,
                    updated_at = %s
                WHERE occurrence_id = %s AND owner_user_id = %s
                  AND claim_generation = %s AND state = %s
                RETURNING *
                """,
                (
                    lease_token,
                    instance_id,
                    observed_at,
                    lease_seconds,
                    observed_at,
                    occurrence.occurrence_id,
                    occurrence.owner_id,
                    occurrence.claim_generation,
                    occurrence.state.value,
                ),
            )
            if claimed is None:
                raise PlaneError(
                    "scheduled occurrence claim lost its locked state fence",
                    code="stale_occurrence_fence",
                    metadata={"owner_id": occurrence.owner_id},
                )
            claims.append(
                ClaimedOccurrenceRecord(
                    occurrence=_occurrence(claimed),
                    job=job,
                    parent_operation_id=parent_operation_id,
                )
            )
        return DueClaimBatch(
            claims=tuple(claims),
            recovered_attempts=tuple(recovered),
            ineligible_job_ids=tuple(ineligible_ids),
            continuation=DueScanContinuation(
                definition=(
                    (int(due_rows[-1]["next_run_at"]), str(due_rows[-1]["id"]))
                    if due_rows else continuation.definition
                ),
                occurrence=(
                    (candidates[-1]["scheduled_for"], str(candidates[-1]["occurrence_id"]))
                    if candidates else continuation.occurrence
                ),
                definition_owner=definition_owner,
                occurrence_owner=occurrence_owner,
            ),
        )

    def start_legacy_run(
        self,
        transaction: Transaction,
        *,
        run_id: str,
        job_id: str,
        owner_id: str,
        correlation_id: str,
        started_at: int,
    ) -> JobRunRecord:
        _uuid("run_id", run_id, version=4)
        _uuid("job_id", job_id, version=4)
        _required("owner_id", owner_id)
        _uuid("correlation_id", correlation_id)
        _millisecond("started_at", started_at)
        row = transaction.fetch_one(
            """
            INSERT INTO job_run (
                id, job_id, user_id, started_at, outcome, correlation_id
            )
            SELECT %s, job.id, job.user_id, %s, 'running', %s
            FROM scheduled_job AS job
            WHERE job.id = %s AND job.user_id = %s
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            (run_id, started_at, correlation_id, job_id, owner_id),
        )
        if row is None:
            existing = transaction.fetch_one(
                "SELECT * FROM job_run WHERE id = %s AND job_id = %s AND user_id = %s",
                (run_id, job_id, owner_id),
            )
            if existing is None:
                raise PlaneError(
                    "scheduled job was not found for run creation",
                    code="scheduled_job_not_found",
                    metadata={"owner_id": owner_id},
                )
            row = existing
        record = _job_run(row)
        if (
            record.job_id != job_id
            or record.owner_id != owner_id
            or record.started_at != started_at
            or record.correlation_id != correlation_id
        ):
            raise PlaneError(
                "scheduled run identity has conflicting semantics",
                code="scheduled_run_conflict",
                metadata={"owner_id": owner_id},
            )
        return record

    def finish_run_for_administration(
        self,
        transaction: Transaction,
        *,
        run_id: str,
        outcome: str,
        summary: str | None,
        auth_ref: str | None,
        ended_at: int,
    ) -> bool:
        _uuid("run_id", run_id, version=4)
        if outcome not in {"success", "failure", "interrupted", "skipped_auth"}:
            raise ValueError("unsupported scheduled run outcome")
        if summary is not None and len(summary) > 2000:
            raise ValueError("summary exceeds 2000 characters")
        if auth_ref is not None:
            _required("auth_ref", auth_ref, maximum=512)
        _millisecond("ended_at", ended_at)
        result = transaction.execute(
            """
            UPDATE job_run
            SET ended_at = %s, outcome = %s, summary = %s, auth_ref = %s
            WHERE id = %s
            """,
            (ended_at, outcome, summary, auth_ref, run_id),
        )
        _zero_or_one(result.rowcount, "scheduled run completion")
        return result.rowcount == 1

    def list_runs(
        self,
        query: Transaction,
        *,
        owner_id: str,
        job_id: str,
        limit: int = 20,
    ) -> tuple[JobRunRecord, ...]:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        _limit(limit)
        rows = query.fetch_all(
            """
            SELECT * FROM job_run
            WHERE job_id = %s AND user_id = %s
            ORDER BY started_at DESC, id
            LIMIT %s
            """,
            (job_id, owner_id, limit),
        )
        return tuple(_job_run(row) for row in rows)

    def reconcile_interrupted_for_administration(
        self,
        transaction: Transaction,
        *,
        ended_at: int,
    ) -> int:
        _millisecond("ended_at", ended_at)
        result = transaction.execute(
            """
            UPDATE job_run SET outcome = 'interrupted', ended_at = %s
            WHERE outcome = 'running'
            """,
            (ended_at,),
        )
        if result.rowcount < 0:
            raise PlaneError(
                "scheduled run reconciliation returned an invalid row count",
                code="scheduler_rowcount_integrity_error",
            )
        return result.rowcount

    def materialize_run_now(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        submission_id: str,
    ) -> RunNowMaterialization:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        _uuid("submission_id", submission_id, version=4)
        job = transaction.fetch_one(
            """
            SELECT * FROM scheduled_job
            WHERE id = %s AND user_id = %s
            FOR UPDATE
            """,
            (job_id, owner_id),
        )
        if job is None:
            raise PlaneError(
                "scheduled job was not found",
                code="scheduled_job_not_found",
                metadata={"owner_id": owner_id},
            )
        existing = transaction.fetch_one(
            """
            SELECT * FROM scheduled_occurrence
            WHERE owner_user_id = %s AND run_now_submission_id = %s
            FOR UPDATE
            """,
            (owner_id, submission_id),
        )
        if existing is not None:
            if str(existing["job_id"]) != job_id:
                raise PlaneError(
                    "run-now submission identity conflicts with another job",
                    code="scheduled_run_now_idempotency_conflict",
                    metadata={"owner_id": owner_id},
                )
            return _run_now(existing, created=False)
        if str(job["status"]) != "active":
            raise PlaneError(
                "scheduled job is not active",
                code="scheduled_job_not_active",
                metadata={"owner_id": owner_id},
            )
        clock = transaction.fetch_one("SELECT clock_timestamp() AS now")
        if clock is None:
            raise PlaneError("database clock was unavailable", code="scheduler_clock_missing")
        observed_at = clock["now"]
        _aware("observed_at", observed_at)
        occurrence_id = str(uuid.uuid4())
        scheduled_for = observed_at
        for collision in range(32):
            inserted = transaction.fetch_one(
                """
                INSERT INTO scheduled_occurrence (
                    occurrence_id, job_id, owner_user_id, scheduled_for,
                    run_now_submission_id, state, first_eligible_at,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    occurrence_id,
                    job_id,
                    owner_id,
                    scheduled_for,
                    submission_id,
                    observed_at,
                    observed_at,
                    observed_at,
                ),
            )
            if inserted is not None:
                return _run_now(inserted, created=True)
            existing = transaction.fetch_one(
                """
                SELECT * FROM scheduled_occurrence
                WHERE owner_user_id = %s AND run_now_submission_id = %s
                FOR UPDATE
                """,
                (owner_id, submission_id),
            )
            if existing is not None:
                if str(existing["job_id"]) != job_id:
                    raise PlaneError(
                        "run-now submission identity conflicts with another job",
                        code="scheduled_run_now_idempotency_conflict",
                        metadata={"owner_id": owner_id},
                    )
                return _run_now(existing, created=False)
            scheduled_for = observed_at - timedelta(microseconds=collision + 1)
        raise PlaneError(
            "run-now timestamp collision budget was exhausted",
            code="scheduled_run_now_timestamp_conflict",
            metadata={"owner_id": owner_id},
        )

    def put_job(self, transaction: Transaction, job: ScheduledJob) -> ScheduledJob:
        row = transaction.fetch_one(
            """
            INSERT INTO scheduled_job (
                id, user_id, name, instruction, schedule_kind, schedule_expr,
                timezone, status, next_run_at, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            (
                job.job_id,
                job.owner_id,
                job.name,
                job.instruction,
                job.schedule_kind,
                job.schedule_expression,
                job.timezone,
                job.status,
                job.next_run_at,
                job.created_at,
                job.updated_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM scheduled_job WHERE id = %s AND user_id = %s",
                (job.job_id, job.owner_id),
            )
        if row is None or _job(row) != job:
            raise PlaneError(
                "scheduled job identity has conflicting semantics",
                code="scheduled_job_conflict",
                metadata={"owner_id": job.owner_id},
            )
        return job

    def get_job(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        for_update: bool = False,
    ) -> ScheduledJob | None:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        if not isinstance(for_update, bool):
            raise ValueError("for_update must be a boolean")
        lock = " FOR UPDATE" if for_update else ""
        row = transaction.fetch_one(
            "SELECT * FROM scheduled_job WHERE id = %s AND user_id = %s" + lock,
            (job_id, owner_id),
        )
        return None if row is None else _job(row)

    def list_due_jobs(
        self, transaction: Transaction, *, owner_id: str, due_at_ms: int, limit: int
    ) -> tuple[ScheduledJob, ...]:
        _required("owner_id", owner_id)
        _limit(limit)
        rows = transaction.fetch_all(
            """
            SELECT * FROM scheduled_job
            WHERE user_id = %s AND status = 'active' AND next_run_at <= %s
            ORDER BY next_run_at, id LIMIT %s
            """,
            (owner_id, due_at_ms, limit),
        )
        return tuple(_job(row) for row in rows)

    def create_occurrence(
        self,
        transaction: Transaction,
        *,
        occurrence_id: str,
        job_id: str,
        owner_id: str,
        scheduled_for: datetime,
    ) -> ScheduledOccurrence:
        _uuid("occurrence_id", occurrence_id, version=4)
        _uuid("job_id", job_id, version=4)
        _required("owner_id", owner_id)
        _aware("scheduled_for", scheduled_for)
        row = transaction.fetch_one(
            """
            INSERT INTO scheduled_occurrence (
                occurrence_id, job_id, owner_user_id, scheduled_for, state,
                first_eligible_at
            ) SELECT %s, id, user_id, %s, 'pending', %s
              FROM scheduled_job WHERE id = %s AND user_id = %s
            ON CONFLICT (job_id, scheduled_for) DO NOTHING
            RETURNING *
            """,
            (occurrence_id, scheduled_for, scheduled_for, job_id, owner_id),
        )
        if row is None:
            row = transaction.fetch_one(
                """
                SELECT * FROM scheduled_occurrence
                WHERE job_id = %s AND owner_user_id = %s AND scheduled_for = %s
                """,
                (job_id, owner_id, scheduled_for),
            )
        if row is None:
            raise PlaneError(
                "scheduled occurrence owner or job was not found",
                code="scheduled_occurrence_not_found",
                metadata={"owner_id": owner_id},
            )
        return _occurrence(row)

    def claim_occurrence(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> ScheduledOccurrence:
        _required("owner_id", owner_id)
        _uuid("occurrence_id", occurrence_id, version=4)
        _required("worker_id", worker_id)
        _uuid("lease_token", lease_token, version=4)
        _aware("now", now)
        _aware("lease_expires_at", lease_expires_at)
        if lease_expires_at <= now:
            raise ValueError("occurrence lease must expire after now")
        row = transaction.fetch_one(
            """
            UPDATE scheduled_occurrence
            SET state = 'claimed', lease_token = %s, lease_owner = %s,
                lease_expires_at = %s, claim_generation = claim_generation + 1,
                attempt_count = attempt_count + 1,
                current_operation_id = NULL,
                operation_execution_generation = NULL,
                started_at = NULL, terminal_at = NULL,
                next_attempt_at = NULL, result_code = NULL,
                updated_at = %s
            WHERE occurrence_id = %s AND owner_user_id = %s
              AND (
                    (
                        state IN ('pending', 'retryable')
                        AND scheduled_for <= %s
                        AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                    )
                    OR (
                        state IN ('claimed', 'running')
                        AND lease_expires_at <= %s
                    )
              )
            RETURNING *
            """,
            (
                lease_token,
                worker_id,
                lease_expires_at,
                now,
                occurrence_id,
                owner_id,
                now,
                now,
                now,
            ),
        )
        return _required_occurrence(row, "occurrence claim was denied", owner_id)

    def renew_occurrence_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> datetime | None:
        _claim_identity(
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
        )
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool):
            raise ValueError("lease_seconds must be an integer")
        if not 5 <= lease_seconds <= 60:
            raise ValueError("lease_seconds must be between 5 and 60")
        row = transaction.fetch_one(
            """
            UPDATE scheduled_occurrence
            SET lease_expires_at = clock_timestamp()
                    + (%s * INTERVAL '1 second'),
                updated_at = clock_timestamp()
            WHERE occurrence_id = %s AND owner_user_id = %s
              AND claim_generation = %s AND lease_token = %s
              AND lease_owner = %s AND state IN ('claimed', 'running')
              AND lease_expires_at > clock_timestamp()
            RETURNING lease_expires_at
            """,
            (
                lease_seconds,
                occurrence_id,
                owner_id,
                claim_generation,
                lease_token,
                lease_owner,
            ),
        )
        if row is None:
            return None
        expires_at = row["lease_expires_at"]
        _aware("lease_expires_at", expires_at)
        return expires_at.astimezone(UTC)

    def assert_current_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        states: tuple[OccurrenceState, ...],
    ) -> ScheduledOccurrence:
        _claim_identity(
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
        )
        if (
            not isinstance(states, tuple)
            or not states
            or len(states) > 2
            or any(
                state not in {OccurrenceState.CLAIMED, OccurrenceState.RUNNING}
                for state in states
            )
        ):
            raise ValueError("claim states must contain claimed and/or running")
        row = transaction.fetch_one(
            """
            SELECT *, clock_timestamp() AS database_now
            FROM scheduled_occurrence
            WHERE occurrence_id = %s AND owner_user_id = %s
            FOR UPDATE
            """,
            (occurrence_id, owner_id),
        )
        terminal_code = (
            None
            if row is None
            or str(row.get("state")) != "cancelled"
            or row.get("result_code") is None
            else str(row["result_code"])
        )
        if (
            row is None
            or int(row.get("claim_generation") or 0) != claim_generation
            or str(row.get("lease_token")) != lease_token
            or str(row.get("lease_owner")) != lease_owner
            or str(row.get("state")) not in {state.value for state in states}
            or row.get("lease_expires_at") is None
            or row["lease_expires_at"] <= row["database_now"]
        ):
            raise PlaneError(
                "scheduled occurrence claim is stale",
                code="stale_occurrence_claim",
                metadata={"owner_id": owner_id, "terminal_code": terminal_code},
            )
        return _occurrence(row)

    def assert_claim_job_active(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
    ) -> ScheduledJob:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        row = transaction.fetch_one(
            """
            SELECT * FROM scheduled_job
            WHERE id = %s AND user_id = %s
            FOR UPDATE
            """,
            (job_id, owner_id),
        )
        status = "missing" if row is None else str(row["status"])
        if row is None or status not in {"active", "completed"}:
            terminal_code = {
                "paused": "cancelled_job_paused",
                "disabled": "cancelled_job_deleted",
            }.get(status)
            raise PlaneError(
                "scheduled claim definition is not executable",
                code="stale_occurrence_claim",
                metadata={"owner_id": owner_id, "terminal_code": terminal_code},
            )
        return _job(row)

    def attach_operation_to_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        operation_id: str,
    ) -> ScheduledOccurrence:
        _required("owner_id", owner_id)
        _uuid("occurrence_id", occurrence_id, version=4)
        _uuid("lease_token", lease_token, version=4)
        _uuid("operation_id", operation_id, version=4)
        if claim_generation <= 0:
            raise ValueError("claim_generation must be positive")
        row = transaction.fetch_one(
            """
            UPDATE scheduled_occurrence
            SET current_operation_id = %s, updated_at = clock_timestamp()
            WHERE occurrence_id = %s AND owner_user_id = %s
              AND claim_generation = %s AND lease_token = %s
              AND state = 'claimed' AND lease_expires_at > clock_timestamp()
              AND (current_operation_id IS NULL OR current_operation_id = %s)
            RETURNING *
            """,
            (
                operation_id,
                occurrence_id,
                owner_id,
                claim_generation,
                lease_token,
                operation_id,
            ),
        )
        return _required_occurrence(
            row, "scheduled occurrence operation attachment lost its claim fence", owner_id
        )

    def start_claim_attempt(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        occurrence_id: str,
        attempt_number: int,
        claim_generation: int,
        lease_token: str,
        operation_id: str,
        operation_execution_generation: int,
        run_id: str,
        correlation_id: str,
        lease_seconds: int,
    ) -> JobRunRecord:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        _uuid("occurrence_id", occurrence_id, version=4)
        _uuid("lease_token", lease_token, version=4)
        _uuid("operation_id", operation_id, version=4)
        _uuid("run_id", run_id, version=4)
        _uuid("correlation_id", correlation_id, version=4)
        if min(attempt_number, claim_generation, operation_execution_generation) <= 0:
            raise ValueError("attempt and execution generations must be positive")
        if not 5 <= lease_seconds <= 60:
            raise ValueError("lease_seconds must be between 5 and 60")
        occurrence = transaction.fetch_one(
            """
            UPDATE scheduled_occurrence
            SET state = 'running', started_at = COALESCE(started_at, clock_timestamp()),
                operation_execution_generation = %s,
                lease_expires_at = clock_timestamp() + (%s * INTERVAL '1 second'),
                updated_at = clock_timestamp()
            WHERE occurrence_id = %s AND owner_user_id = %s
              AND claim_generation = %s AND lease_token = %s
              AND current_operation_id = %s AND state = 'claimed'
              AND lease_expires_at > clock_timestamp()
            RETURNING occurrence_id
            """,
            (
                operation_execution_generation,
                lease_seconds,
                occurrence_id,
                owner_id,
                claim_generation,
                lease_token,
                operation_id,
            ),
        )
        if occurrence is None:
            raise PlaneError(
                "scheduled occurrence start lost its claim fence",
                code="stale_occurrence_claim",
                metadata={"owner_id": owner_id},
            )
        inserted = transaction.fetch_one(
            """
            INSERT INTO job_run (
                id, job_id, user_id, started_at, outcome, correlation_id,
                occurrence_id, attempt_number, operation_id,
                operation_execution_generation, occurrence_claim_generation
            ) VALUES (
                %s, %s, %s,
                (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT,
                'running', %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (occurrence_id, attempt_number)
                WHERE occurrence_id IS NOT NULL DO NOTHING
            RETURNING *
            """,
            (
                run_id,
                job_id,
                owner_id,
                correlation_id,
                occurrence_id,
                attempt_number,
                operation_id,
                operation_execution_generation,
                claim_generation,
            ),
        )
        row = inserted or transaction.fetch_one(
            """
            SELECT * FROM job_run
            WHERE occurrence_id = %s AND attempt_number = %s
            """,
            (occurrence_id, attempt_number),
        )
        if row is None:
            raise PlaneError(
                "scheduled occurrence run row is missing",
                code="scheduler_record_invalid",
                metadata={"owner_id": owner_id},
            )
        record = _job_run(row)
        if (
            record.owner_id != owner_id
            or record.job_id != job_id
            or record.operation_id != operation_id
            or record.operation_execution_generation != operation_execution_generation
            or record.occurrence_claim_generation != claim_generation
        ):
            raise PlaneError(
                "scheduled run replay conflicts with the execution fence",
                code="scheduled_run_fence_conflict",
                metadata={"owner_id": owner_id},
            )
        return record

    def mark_claim_retryable(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        attempt_number: int,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        operation_id: str | None,
        operation_execution_generation: int | None,
        error_code: str,
        retry_after_seconds: int,
    ) -> ScheduledOccurrence:
        _required("owner_id", owner_id)
        _uuid("occurrence_id", occurrence_id, version=4)
        _uuid("lease_token", lease_token, version=4)
        if min(attempt_number, claim_generation) <= 0:
            raise ValueError("attempt_number and claim_generation must be positive")
        if (operation_id is None) != (operation_execution_generation is None):
            raise ValueError("operation identity and generation are all-or-none")
        if operation_id is not None:
            _uuid("operation_id", operation_id, version=4)
            if operation_execution_generation is None or operation_execution_generation <= 0:
                raise ValueError("operation_execution_generation must be positive")
        _bounded_code("error_code", error_code, maximum=64)
        if not 0 <= retry_after_seconds <= 86_400:
            raise ValueError("retry_after_seconds is out of range")
        current = self.assert_current_claim(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            states=(OccurrenceState.CLAIMED, OccurrenceState.RUNNING),
        )
        if current.state is OccurrenceState.RUNNING:
            if (
                operation_id is None
                or operation_execution_generation is None
                or current.operation_id != operation_id
                or current.operation_execution_generation != operation_execution_generation
            ):
                raise PlaneError(
                    "running occurrence has a conflicting operation identity",
                    code="scheduled_run_fence_conflict",
                    metadata={"owner_id": owner_id},
                )
            interrupted = transaction.fetch_one(
                """
                UPDATE job_run
                SET outcome = 'interrupted',
                    ended_at = (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT
                WHERE occurrence_id = %s AND attempt_number = %s
                  AND operation_id = %s AND operation_execution_generation = %s
                  AND occurrence_claim_generation = %s AND outcome = 'running'
                RETURNING id
                """,
                (
                    occurrence_id,
                    attempt_number,
                    operation_id,
                    operation_execution_generation,
                    claim_generation,
                ),
            )
            if interrupted is None:
                raise PlaneError(
                    "running occurrence has no current job run",
                    code="scheduler_record_invalid",
                    metadata={"owner_id": owner_id},
                )
        row = transaction.fetch_one(
            """
            UPDATE scheduled_occurrence
            SET state = 'retryable', lease_token = NULL, lease_owner = NULL,
                lease_expires_at = NULL,
                next_attempt_at = clock_timestamp() + (%s * INTERVAL '1 second'),
                last_error_code = %s, updated_at = clock_timestamp()
            WHERE occurrence_id = %s AND owner_user_id = %s
              AND claim_generation = %s AND lease_token = %s
            RETURNING *
            """,
            (
                retry_after_seconds,
                error_code,
                occurrence_id,
                owner_id,
                claim_generation,
                lease_token,
            ),
        )
        return _required_occurrence(
            row, "scheduled retry transition lost its claim fence", owner_id
        )
    def reserve_effect_for_attempt(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        operation_id: str,
        operation_execution_generation: int,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
        recover_reserved: bool = False,
    ) -> EffectReservationOutcome:
        _claim_identity(
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
        )
        _uuid("operation_id", operation_id, version=4)
        if operation_execution_generation <= 0:
            raise ValueError("operation_execution_generation must be positive")
        _bounded_code("effect_kind", effect_kind, maximum=64)
        _required("effect_key", effect_key, maximum=256)
        _digest("payload_digest", payload_digest)
        if not isinstance(recover_reserved, bool):
            raise ValueError("recover_reserved must be a boolean")
        current = self.assert_current_claim(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            states=(OccurrenceState.RUNNING,),
        )
        if (
            current.operation_id != operation_id
            or current.operation_execution_generation != operation_execution_generation
        ):
            raise PlaneError(
                "effect authority differs from the running occurrence",
                code="stale_occurrence_claim",
                metadata={"owner_id": owner_id},
            )
        row = transaction.fetch_one(
            """
            SELECT * FROM effect_ledger
            WHERE occurrence_id = %s AND effect_kind = %s AND effect_key = %s
            FOR UPDATE
            """,
            (occurrence_id, effect_kind, effect_key),
        )
        if row is None:
            inserted = transaction.fetch_one(
                """
                INSERT INTO effect_ledger (
                    occurrence_id, effect_kind, effect_key, payload_digest,
                    state, operation_id, operation_execution_generation,
                    occurrence_claim_generation, reserved_at
                ) VALUES (%s, %s, %s, %s, 'reserved', %s, %s, %s,
                          clock_timestamp())
                RETURNING *
                """,
                (
                    occurrence_id,
                    effect_kind,
                    effect_key,
                    payload_digest,
                    operation_id,
                    operation_execution_generation,
                    claim_generation,
                ),
            )
            if inserted is None:  # pragma: no cover
                raise PlaneError(
                    "effect reservation insert returned no row",
                    code="scheduler_record_invalid",
                    metadata={"owner_id": owner_id},
                )
            return EffectReservationOutcome(
                state=EffectState.RESERVED, created=True, ambiguous=False
            )
        existing = _effect(row)
        if existing.payload_digest != payload_digest:
            raise PlaneError(
                "effect key was reused with another payload",
                code="effect_idempotency_conflict",
                metadata={"owner_id": owner_id},
            )
        same_attempt = (
            existing.operation_id == operation_id
            and existing.operation_execution_generation
            == operation_execution_generation
            and existing.occurrence_claim_generation == claim_generation
        )
        recoverable = existing.state is EffectState.FAILED or (
            recover_reserved and existing.state is EffectState.RESERVED
        )
        if recoverable:
            updated = transaction.fetch_one(
                """
                UPDATE effect_ledger
                SET state = 'reserved', operation_id = %s,
                    operation_execution_generation = %s,
                    occurrence_claim_generation = %s,
                    reserved_at = clock_timestamp(), published_at = NULL,
                    failed_at = NULL, failure_code = NULL,
                    downstream_receipt_digest = NULL
                WHERE occurrence_id = %s AND effect_kind = %s AND effect_key = %s
                  AND state IN ('reserved', 'failed')
                RETURNING *
                """,
                (
                    operation_id,
                    operation_execution_generation,
                    claim_generation,
                    occurrence_id,
                    effect_kind,
                    effect_key,
                ),
            )
            if updated is None:
                raise PlaneError(
                    "effect recovery lost its locked state fence",
                    code="effect_reservation_conflict",
                    metadata={"owner_id": owner_id},
                )
            return EffectReservationOutcome(
                state=EffectState.RESERVED,
                created=existing.state is EffectState.FAILED,
                ambiguous=False,
            )
        return EffectReservationOutcome(
            state=existing.state,
            created=False,
            ambiguous=existing.state is EffectState.RESERVED and not same_attempt,
        )

    def publish_reserved_effect(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        operation_id: str,
        operation_execution_generation: int,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
        downstream_receipt_digest: str | None,
    ) -> EffectReservationOutcome:
        self._validate_effect_attempt(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            operation_id=operation_id,
            operation_execution_generation=operation_execution_generation,
            effect_kind=effect_kind,
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        if downstream_receipt_digest is not None:
            _digest("downstream_receipt_digest", downstream_receipt_digest)
        existing_row = transaction.fetch_one(
            """
            SELECT * FROM effect_ledger
            WHERE occurrence_id = %s AND effect_kind = %s AND effect_key = %s
            FOR UPDATE
            """,
            (occurrence_id, effect_kind, effect_key),
        )
        if existing_row is None:
            raise PlaneError(
                "effect reservation was not found",
                code="effect_reservation_not_found",
                metadata={"owner_id": owner_id},
            )
        existing = _effect(existing_row)
        if existing.payload_digest != payload_digest:
            raise PlaneError(
                "effect key was reused with another payload",
                code="effect_idempotency_conflict",
                metadata={"owner_id": owner_id},
            )
        if existing.state is EffectState.PUBLISHED:
            return EffectReservationOutcome(
                state=EffectState.PUBLISHED, created=False, ambiguous=False
            )
        if (
            existing.state is not EffectState.RESERVED
            or existing.operation_id != operation_id
            or existing.operation_execution_generation
            != operation_execution_generation
            or existing.occurrence_claim_generation != claim_generation
        ):
            raise PlaneError(
                "effect reservation belongs to another attempt",
                code="effect_reservation_conflict",
                metadata={"owner_id": owner_id},
            )
        updated = transaction.fetch_one(
            """
            UPDATE effect_ledger
            SET state = 'published', published_at = clock_timestamp(),
                failed_at = NULL, failure_code = NULL,
                downstream_receipt_digest = %s
            WHERE occurrence_id = %s AND effect_kind = %s AND effect_key = %s
              AND payload_digest = %s AND state = 'reserved'
              AND operation_id = %s AND operation_execution_generation = %s
              AND occurrence_claim_generation = %s
            RETURNING *
            """,
            (
                downstream_receipt_digest,
                occurrence_id,
                effect_kind,
                effect_key,
                payload_digest,
                operation_id,
                operation_execution_generation,
                claim_generation,
            ),
        )
        if updated is None:
            raise PlaneError(
                "effect publication lost its reservation fence",
                code="effect_reservation_conflict",
                metadata={"owner_id": owner_id},
            )
        return EffectReservationOutcome(
            state=EffectState.PUBLISHED, created=False, ambiguous=False
        )

    def fail_reserved_effect(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        operation_id: str,
        operation_execution_generation: int,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
        failure_code: str,
    ) -> EffectReservationOutcome:
        self._validate_effect_attempt(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            operation_id=operation_id,
            operation_execution_generation=operation_execution_generation,
            effect_kind=effect_kind,
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        _bounded_code("failure_code", failure_code, maximum=64)
        row = transaction.fetch_one(
            """
            UPDATE effect_ledger
            SET state = 'failed', failed_at = clock_timestamp(),
                published_at = NULL, failure_code = %s
            WHERE occurrence_id = %s AND effect_kind = %s AND effect_key = %s
              AND payload_digest = %s AND state = 'reserved'
              AND operation_id = %s AND operation_execution_generation = %s
              AND occurrence_claim_generation = %s
            RETURNING *
            """,
            (
                failure_code,
                occurrence_id,
                effect_kind,
                effect_key,
                payload_digest,
                operation_id,
                operation_execution_generation,
                claim_generation,
            ),
        )
        if row is None:
            raise PlaneError(
                "effect failure transition lost its reservation fence",
                code="effect_reservation_conflict",
                metadata={"owner_id": owner_id},
            )
        return EffectReservationOutcome(
            state=EffectState.FAILED, created=False, ambiguous=False
        )

    def _validate_effect_attempt(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        operation_id: str,
        operation_execution_generation: int,
        effect_kind: str,
        effect_key: str,
        payload_digest: str,
    ) -> ScheduledOccurrence:
        _claim_identity(
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
        )
        _uuid("operation_id", operation_id, version=4)
        if operation_execution_generation <= 0:
            raise ValueError("operation_execution_generation must be positive")
        _bounded_code("effect_kind", effect_kind, maximum=64)
        _required("effect_key", effect_key, maximum=256)
        _digest("payload_digest", payload_digest)
        current = self.assert_current_claim(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            states=(OccurrenceState.RUNNING,),
        )
        if (
            current.operation_id != operation_id
            or current.operation_execution_generation != operation_execution_generation
        ):
            raise PlaneError(
                "effect authority differs from the running occurrence",
                code="stale_occurrence_claim",
                metadata={"owner_id": owner_id},
            )
        return current

    def finish_claim_attempt(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        occurrence_id: str,
        attempt_number: int,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        operation_id: str,
        operation_execution_generation: int,
        run_id: str,
        job_outcome: str,
        occurrence_state: OccurrenceState,
        safe_code: str,
        summary: str | None,
        auth_ref: str | None,
        retry_after_seconds: int,
    ) -> ScheduledOccurrence:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        _uuid("occurrence_id", occurrence_id, version=4)
        _uuid("lease_token", lease_token, version=4)
        _instance_id(lease_owner)
        _uuid("operation_id", operation_id, version=4)
        _uuid("run_id", run_id, version=4)
        if min(attempt_number, claim_generation, operation_execution_generation) <= 0:
            raise ValueError("attempt and execution generations must be positive")
        if job_outcome not in {"success", "failure", "interrupted", "skipped_auth"}:
            raise ValueError("unsupported job run outcome")
        if occurrence_state not in {
            OccurrenceState.COMPLETED,
            OccurrenceState.FAILED,
            OccurrenceState.RETRYABLE,
        }:
            raise ValueError("unsupported occurrence settlement state")
        _bounded_code("safe_code", safe_code, maximum=64)
        if summary is not None and len(summary) > 2000:
            raise ValueError("summary exceeds 2000 characters")
        if auth_ref is not None:
            _required("auth_ref", auth_ref, maximum=512)
        if not 0 <= retry_after_seconds <= 86_400:
            raise ValueError("retry_after_seconds is out of range")
        current = self.assert_current_claim(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            states=(OccurrenceState.RUNNING,),
        )
        if (
            current.job_id != job_id
            or current.operation_id != operation_id
            or current.operation_execution_generation != operation_execution_generation
        ):
            raise PlaneError(
                "scheduled settlement authority differs from the running occurrence",
                code="stale_occurrence_claim",
                metadata={"owner_id": owner_id},
            )
        run = transaction.fetch_one(
            """
            UPDATE job_run
            SET ended_at = (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT,
                outcome = %s, summary = %s, auth_ref = %s
            WHERE id = %s AND job_id = %s AND user_id = %s
              AND occurrence_id = %s AND attempt_number = %s
              AND operation_id = %s AND operation_execution_generation = %s
              AND occurrence_claim_generation = %s AND outcome = 'running'
            RETURNING id
            """,
            (
                job_outcome,
                summary,
                auth_ref,
                run_id,
                job_id,
                owner_id,
                occurrence_id,
                attempt_number,
                operation_id,
                operation_execution_generation,
                claim_generation,
            ),
        )
        if run is None:
            raise PlaneError(
                "scheduled job run is no longer running",
                code="scheduled_run_fence_conflict",
                metadata={"owner_id": owner_id},
            )
        terminal = occurrence_state in {
            OccurrenceState.COMPLETED,
            OccurrenceState.FAILED,
        }
        retryable = occurrence_state is OccurrenceState.RETRYABLE
        row = transaction.fetch_one(
            """
            UPDATE scheduled_occurrence
            SET state = %s, lease_token = NULL, lease_owner = NULL,
                lease_expires_at = NULL,
                terminal_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                next_attempt_at = CASE WHEN %s THEN
                    clock_timestamp() + (%s * INTERVAL '1 second') ELSE NULL END,
                result_code = CASE WHEN %s THEN %s ELSE NULL END,
                last_error_code = CASE WHEN %s THEN %s ELSE NULL END,
                updated_at = clock_timestamp()
            WHERE occurrence_id = %s AND owner_user_id = %s
              AND claim_generation = %s AND lease_token = %s
              AND current_operation_id = %s
              AND operation_execution_generation = %s AND state = 'running'
            RETURNING *
            """,
            (
                occurrence_state.value,
                terminal,
                retryable,
                retry_after_seconds,
                occurrence_state is OccurrenceState.COMPLETED,
                safe_code,
                occurrence_state is not OccurrenceState.COMPLETED,
                safe_code,
                occurrence_id,
                owner_id,
                claim_generation,
                lease_token,
                operation_id,
                operation_execution_generation,
            ),
        )
        return _required_occurrence(
            row, "scheduled occurrence settlement lost its claim fence", owner_id
        )

    def publish_staged_chat_effect(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        operation_id: str,
        operation_execution_generation: int,
        effect_key: str,
        payload_digest: str,
        publication: StagedChatPublication,
    ) -> EffectReservationOutcome:
        if not isinstance(publication, StagedChatPublication):
            raise ValueError("publication must be a StagedChatPublication")
        if publication.owner_id != owner_id:
            raise ValueError("staged chat owner differs from the occurrence owner")
        if publication.conversation_id != effect_key:
            raise ValueError("chat effect key must equal the staged conversation identity")
        _required("conversation_id", publication.conversation_id, maximum=256)
        _required("owner_id", publication.owner_id)
        _uuid("publication_id", publication.publication_id, version=4)
        _uuid("request_generation", publication.request_generation, version=4)
        if not isinstance(publication.create_conversation_if_missing, bool):
            raise ValueError("create_conversation_if_missing must be a boolean")
        if publication.agent_id is not None:
            _required("agent_id", publication.agent_id, maximum=256)
        if publication.requested_title is not None:
            _required("requested_title", publication.requested_title, maximum=512)
        if not publication.messages or len(publication.messages) > 10_000:
            raise ValueError("staged chat must contain 1..10000 messages")
        if (
            isinstance(publication.base_render_revision, bool)
            or not isinstance(publication.base_render_revision, int)
            or publication.base_render_revision < 0
            or publication.committed_render_revision
            != publication.base_render_revision + 1
        ):
            raise ValueError("scheduled conversation revisions are invalid")
        for message in publication.messages:
            if not isinstance(message, StagedChatMessage):
                raise ValueError("scheduled history message is invalid")
            _required("message.role", message.role, maximum=64)
            if not isinstance(message.content, str) or len(message.content) > 1_000_000:
                raise ValueError("scheduled message content is invalid")
            if not isinstance(message.title_source, str):
                raise ValueError("scheduled message title source is invalid")
            _millisecond("message.timestamp_ms", message.timestamp_ms)
        validated_layouts: list[tuple[str, int, str]] = []
        seen_layouts: set[str] = set()
        if len(publication.layouts) > 10_000:
            raise ValueError("scheduled layout count exceeds 10000")
        for layout in publication.layouts:
            if not isinstance(layout, StagedChatLayout):
                raise ValueError("scheduled canvas layout is invalid")
            _required("layout_key", layout.layout_key, maximum=512)
            if layout.layout_key in seen_layouts:
                raise ValueError("scheduled canvas layout identity is duplicated")
            if (
                isinstance(layout.position, bool)
                or not isinstance(layout.position, int)
                or layout.position < 0
            ):
                raise ValueError("scheduled canvas layout position is invalid")
            try:
                encoded_tree = json.dumps(
                    list(layout.tree),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("scheduled canvas layout tree is invalid") from exc
            seen_layouts.add(layout.layout_key)
            validated_layouts.append(
                (layout.layout_key, layout.position, encoded_tree)
            )

        self._validate_effect_attempt(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            operation_id=operation_id,
            operation_execution_generation=operation_execution_generation,
            effect_kind="chat_history",
            effect_key=effect_key,
            payload_digest=payload_digest,
        )
        effect_row = transaction.fetch_one(
            """
            SELECT * FROM effect_ledger
            WHERE occurrence_id = %s AND effect_kind = 'chat_history'
              AND effect_key = %s
            FOR UPDATE
            """,
            (occurrence_id, effect_key),
        )
        if effect_row is None:
            raise PlaneError(
                "chat effect was not reserved",
                code="effect_reservation_not_found",
                metadata={"owner_id": owner_id},
            )
        effect = _effect(effect_row)
        if effect.payload_digest != payload_digest:
            raise PlaneError(
                "effect key was reused with another payload",
                code="effect_idempotency_conflict",
                metadata={"owner_id": owner_id},
            )
        if effect.state is EffectState.PUBLISHED:
            return EffectReservationOutcome(
                state=EffectState.PUBLISHED, created=False, ambiguous=False
            )
        if (
            effect.state is not EffectState.RESERVED
            or effect.operation_id != operation_id
            or effect.operation_execution_generation
            != operation_execution_generation
            or effect.occurrence_claim_generation != claim_generation
        ):
            raise PlaneError(
                "chat effect reservation belongs to another attempt",
                code="effect_reservation_conflict",
                metadata={"owner_id": owner_id},
            )

        chat = transaction.fetch_one(
            "SELECT * FROM chats WHERE id = %s FOR UPDATE",
            (publication.conversation_id,),
        )
        if chat is None and publication.create_conversation_if_missing:
            created_at = publication.messages[0].timestamp_ms
            transaction.execute(
                """
                INSERT INTO chats (
                    id, user_id, title, agent_id, created_at, updated_at
                ) VALUES (%s, %s, 'New Chat', %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    publication.conversation_id,
                    owner_id,
                    publication.agent_id,
                    created_at,
                    created_at,
                ),
            )
            chat = transaction.fetch_one(
                "SELECT * FROM chats WHERE id = %s FOR UPDATE",
                (publication.conversation_id,),
            )
        if chat is None or str(chat["user_id"]) != owner_id:
            raise PlaneError(
                "scheduled target conversation is missing or owner-mismatched",
                code="scheduled_conversation_not_found",
                metadata={"owner_id": owner_id},
            )
        staged = transaction.fetch_one(
            "SELECT * FROM conversation_commit WHERE commit_id = %s FOR UPDATE",
            (publication.publication_id,),
        )
        if (
            staged is None
            or str(staged["chat_id"]) != publication.conversation_id
            or str(staged["owner_user_id"]) != owner_id
            or str(staged["request_generation"]) != publication.request_generation
            or str(staged["state"]) != "staged"
            or int(staged["base_render_revision"])
            != publication.base_render_revision
            or str(staged.get("operation_id")) != operation_id
            or int(staged["operation_execution_generation"])
            != operation_execution_generation
            or int(chat.get("render_revision") or 0)
            != publication.base_render_revision
        ):
            raise PlaneError(
                "scheduled conversation publication fence changed",
                code="scheduled_publication_conflict",
                metadata={"owner_id": owner_id},
            )
        component_counts = transaction.fetch_one(
            """
            SELECT COUNT(*) AS count,
                   COUNT(*) FILTER (WHERE committed_render_revision = %s) AS valid
            FROM saved_components WHERE conversation_commit_id = %s
            """,
            (publication.committed_render_revision, publication.publication_id),
        )
        if component_counts is None:  # pragma: no cover
            raise PlaneError(
                "scheduled component count was unavailable",
                code="scheduler_record_invalid",
            )
        if int(component_counts["count"]) != int(component_counts["valid"]):
            raise PlaneError(
                "scheduled canvas stage is incomplete",
                code="scheduled_publication_conflict",
                metadata={"owner_id": owner_id},
            )
        staged_component_count = int(component_counts["count"])
        message_count = transaction.fetch_one(
            """
            SELECT COUNT(*) AS count FROM messages
            WHERE chat_id = %s AND user_id = %s
            """,
            (publication.conversation_id, owner_id),
        )
        if message_count is None:  # pragma: no cover
            raise PlaneError(
                "scheduled message count was unavailable",
                code="scheduler_record_invalid",
            )
        existing_message_count = int(message_count["count"])
        for position, message in enumerate(publication.messages):
            transaction.execute(
                """
                INSERT INTO messages (
                    chat_id, user_id, role, content, timestamp,
                    conversation_commit_id, commit_position,
                    committed_render_revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    publication.conversation_id,
                    owner_id,
                    message.role,
                    message.content,
                    message.timestamp_ms,
                    publication.publication_id,
                    position,
                    publication.committed_render_revision,
                ),
            )
        transaction.execute(
            """
            DELETE FROM saved_components
            WHERE chat_id = %s AND user_id = %s
              AND conversation_commit_id IS DISTINCT FROM %s
            """,
            (publication.conversation_id, owner_id, publication.publication_id),
        )
        transaction.execute(
            "DELETE FROM workspace_layout WHERE chat_id = %s AND user_id = %s",
            (publication.conversation_id, owner_id),
        )
        updated_ms = publication.messages[-1].timestamp_ms
        for layout_key, position, encoded_tree in validated_layouts:
            transaction.execute(
                """
                INSERT INTO workspace_layout (
                    chat_id, user_id, layout_key, position, layout,
                    created_at, updated_at, conversation_commit_id,
                    committed_render_revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    publication.conversation_id,
                    owner_id,
                    layout_key,
                    position,
                    encoded_tree,
                    updated_ms,
                    updated_ms,
                    publication.publication_id,
                    publication.committed_render_revision,
                ),
            )
        title = publication.requested_title
        if title is None and existing_message_count == 0:
            first_user = next(
                (
                    message.title_source
                    for message in publication.messages
                    if message.role == "user"
                ),
                None,
            )
            if first_user is not None:
                title = first_user[:30] + "..." if len(first_user) > 30 else first_user
        clock = transaction.fetch_one("SELECT clock_timestamp() AS current_time")
        if clock is None:
            raise PlaneError("database clock was unavailable", code="scheduler_clock_missing")
        committed_at = clock["current_time"]
        committed = transaction.fetch_one(
            """
            UPDATE conversation_commit
            SET state = 'committed', committed_render_revision = %s,
                committed_at = %s
            WHERE commit_id = %s AND chat_id = %s AND owner_user_id = %s
              AND state = 'staged' AND base_render_revision = %s
            RETURNING commit_id
            """,
            (
                publication.committed_render_revision,
                committed_at,
                publication.publication_id,
                publication.conversation_id,
                owner_id,
                publication.base_render_revision,
            ),
        )
        if committed is None:
            raise PlaneError(
                "scheduled conversation publication lost its CAS",
                code="scheduled_publication_conflict",
                metadata={"owner_id": owner_id},
            )
        chat_result = transaction.execute(
            """
            UPDATE chats
            SET title = COALESCE(%s, title), updated_at = %s,
                render_revision = %s, snapshot_committed_at = %s,
                conversation_commit_id = %s, has_saved_components = %s
            WHERE id = %s AND user_id = %s AND render_revision = %s
            """,
            (
                title,
                updated_ms,
                publication.committed_render_revision,
                committed_at,
                publication.publication_id,
                bool(staged_component_count),
                publication.conversation_id,
                owner_id,
                publication.base_render_revision,
            ),
        )
        if chat_result.rowcount != 1:
            raise PlaneError(
                "scheduled conversation revision CAS is stale",
                code="scheduled_publication_conflict",
                metadata={"owner_id": owner_id},
            )
        return self.publish_reserved_effect(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            operation_id=operation_id,
            operation_execution_generation=operation_execution_generation,
            effect_kind="chat_history",
            effect_key=effect_key,
            payload_digest=payload_digest,
            downstream_receipt_digest=None,
        )

    def get_job_policy(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
    ) -> ScheduledJobPolicy | None:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        row = transaction.fetch_one(
            "SELECT * FROM scheduled_job_policy WHERE job_id = %s AND owner_id = %s",
            (job_id, owner_id),
        )
        return None if row is None else _policy(row)

    def put_job_policy(
        self,
        transaction: Transaction,
        *,
        policy: ScheduledJobPolicy,
        expected_version: int,
    ) -> ScheduledJobPolicy:
        if not isinstance(policy, ScheduledJobPolicy):
            raise ValueError("policy must be a ScheduledJobPolicy")
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or not 0 <= expected_version < 9007199254740991
        ):
            raise ValueError("expected_version must be a non-negative integer")
        if policy.version != expected_version + 1:
            raise ValueError("policy version must advance the expected version by one")
        if expected_version == 0 and (
            policy.admitted_runs != 0 or policy.terminal_stop or policy.last_assignment_id
        ):
            raise ValueError("scheduler-owned policy fields must start unset")
        self._lock_job(transaction, owner_id=policy.owner_id, job_id=policy.job_id)
        current = self._lock_policy(transaction, owner_id=policy.owner_id, job_id=policy.job_id)
        if expected_version == 0:
            if current is not None:
                raise _policy_version_conflict(policy.owner_id, current.version)
            row = transaction.fetch_one(
                """
                INSERT INTO scheduled_job_policy (
                    job_id, owner_id, version, max_runs, admitted_runs,
                    per_episode_limits, max_outstanding_episodes, monitor_changes,
                    definition_revision, terminal_stop, last_assignment_id, updated_at
                ) VALUES (%s, %s, %s, %s, 0, %s::jsonb, %s, %s, %s, FALSE, NULL, %s)
                ON CONFLICT (job_id) DO NOTHING
                RETURNING *
                """,
                (
                    policy.job_id,
                    policy.owner_id,
                    policy.version,
                    policy.max_runs,
                    _limits_json(policy),
                    policy.max_outstanding_episodes,
                    policy.monitor_changes,
                    policy.definition_revision,
                    policy.updated_at,
                ),
            )
            if row is None:
                raise _policy_version_conflict(policy.owner_id, None)
            return _policy(row)
        if current is None:
            raise PlaneError(
                "scheduled job policy is missing",
                code="scheduled_job_policy_missing",
                metadata={"owner_id": policy.owner_id},
            )
        if current.version != expected_version:
            raise _policy_version_conflict(policy.owner_id, current.version)
        if (
            policy.admitted_runs != current.admitted_runs
            or policy.terminal_stop != current.terminal_stop
            or policy.last_assignment_id != current.last_assignment_id
        ):
            raise PlaneError(
                "scheduler-owned policy fields cannot be rewritten",
                code="scheduled_job_policy_charge_rewrite",
                metadata={"owner_id": policy.owner_id},
            )
        row = transaction.fetch_one(
            """
            UPDATE scheduled_job_policy
            SET version = %s, max_runs = %s, per_episode_limits = %s::jsonb,
                max_outstanding_episodes = %s, monitor_changes = %s,
                definition_revision = %s, updated_at = %s
            WHERE job_id = %s AND owner_id = %s AND version = %s
            RETURNING *
            """,
            (
                policy.version,
                policy.max_runs,
                _limits_json(policy),
                policy.max_outstanding_episodes,
                policy.monitor_changes,
                policy.definition_revision,
                policy.updated_at,
                policy.job_id,
                policy.owner_id,
                expected_version,
            ),
        )
        if row is None:
            raise _policy_version_conflict(policy.owner_id, None)
        return _policy(row)

    def list_outstanding_episodes(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
    ) -> tuple[str, ...]:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        return self._outstanding(transaction, owner_id=owner_id, job_id=job_id)

    def admit_assignment_episode(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        occurrence_id: str,
        claim_generation: int,
        lease_token: str,
        lease_owner: str,
        assignment_id: str,
        admitted_at: int,
        spend: int = 1,
    ) -> EpisodeAdmission:
        _claim_identity(
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
        )
        _uuid("job_id", job_id, version=4)
        _uuid("assignment_id", assignment_id, version=4)
        _millisecond("admitted_at", admitted_at)
        if not isinstance(spend, int) or isinstance(spend, bool) or not 0 <= spend <= MAX_SPEND:
            raise ValueError("spend must be an integer between 0 and the run allowance limit")
        self.assert_claim_job_active(transaction, owner_id=owner_id, job_id=job_id)
        policy = self._lock_policy(transaction, owner_id=owner_id, job_id=job_id)
        if policy is None:
            return _refusal(
                owner_id, job_id, occurrence_id, assignment_id, spend, "policy_missing", None
            )
        occurrence = self.assert_current_claim(
            transaction,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            claim_generation=claim_generation,
            lease_token=lease_token,
            lease_owner=lease_owner,
            states=(OccurrenceState.CLAIMED, OccurrenceState.RUNNING),
        )
        if occurrence.job_id != job_id:
            raise PlaneError(
                "scheduled occurrence belongs to a different definition",
                code="scheduled_occurrence_job_mismatch",
                metadata={"owner_id": owner_id},
            )
        if policy.terminal_stop:
            return _refusal(
                owner_id, job_id, occurrence_id, assignment_id, spend, "terminal_stop", policy
            )
        existing = transaction.fetch_one(
            """
            SELECT assignment_id, spend FROM scheduled_occurrence_assignment
            WHERE occurrence_id = %s AND owner_id = %s
            FOR UPDATE
            """,
            (occurrence_id, owner_id),
        )
        if existing is not None:
            if str(existing["assignment_id"]) != assignment_id:
                raise PlaneError(
                    "scheduled occurrence is already bound to another episode",
                    code="scheduled_occurrence_binding_conflict",
                    metadata={"owner_id": owner_id},
                )
            return EpisodeAdmission(
                job_id=job_id,
                owner_id=owner_id,
                occurrence_id=occurrence_id,
                assignment_id=assignment_id,
                admitted=True,
                created=False,
                reason="replayed",
                spend=int(existing["spend"]),
                policy=policy,
            )
        assignment = transaction.fetch_one(
            "SELECT lifecycle FROM persistent_assignment WHERE id = %s AND owner_user_id = %s",
            (assignment_id, owner_id),
        )
        if assignment is None:
            raise PlaneError(
                "episode assignment is missing or foreign",
                code="scheduled_episode_assignment_missing",
                metadata={"owner_id": owner_id},
            )
        if str(assignment["lifecycle"]) not in {"active", "paused"}:
            raise PlaneError(
                "episode assignment is already resolved",
                code="scheduled_episode_assignment_resolved",
                metadata={"owner_id": owner_id},
            )
        outstanding = self._outstanding(transaction, owner_id=owner_id, job_id=job_id)
        if len(outstanding) >= policy.max_outstanding_episodes:
            return _refusal(
                owner_id, job_id, occurrence_id, assignment_id, spend, "episode_outstanding", policy
            )
        if policy.max_runs is not None and policy.admitted_runs + spend > policy.max_runs:
            return _refusal(
                owner_id, job_id, occurrence_id, assignment_id, spend, "allowance_exhausted", policy
            )
        bound = transaction.fetch_one(
            """
            INSERT INTO scheduled_occurrence_assignment (
                occurrence_id, owner_id, job_id, assignment_id, spend, admitted_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (occurrence_id, owner_id) DO NOTHING
            RETURNING occurrence_id
            """,
            (occurrence_id, owner_id, job_id, assignment_id, spend, admitted_at),
        )
        if bound is None:
            raise PlaneError(
                "scheduled occurrence binding lost its lock fence",
                code="scheduled_occurrence_binding_conflict",
                metadata={"owner_id": owner_id},
            )
        charged = transaction.fetch_one(
            """
            UPDATE scheduled_job_policy
            SET admitted_runs = admitted_runs + %s, last_assignment_id = %s,
                version = version + 1, updated_at = %s
            WHERE job_id = %s AND owner_id = %s AND version = %s
              AND terminal_stop = FALSE
              AND (max_runs IS NULL OR admitted_runs + %s <= max_runs)
            RETURNING *
            """,
            (spend, assignment_id, admitted_at, job_id, owner_id, policy.version, spend),
        )
        if charged is None:
            raise PlaneError(
                "scheduled job policy lost its lock fence during admission",
                code="scheduled_job_policy_version_conflict",
                metadata={"owner_id": owner_id},
            )
        return EpisodeAdmission(
            job_id=job_id,
            owner_id=owner_id,
            occurrence_id=occurrence_id,
            assignment_id=assignment_id,
            admitted=True,
            created=True,
            reason="admitted",
            spend=spend,
            policy=_policy(charged),
        )

    def stop_assignment_job(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
        expected_version: int,
        stopped_at: int,
    ) -> JobStopOutcome:
        _required("owner_id", owner_id)
        _uuid("job_id", job_id, version=4)
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or not 1 <= expected_version <= 9007199254740991
        ):
            raise ValueError("expected_version must be a positive integer")
        _millisecond("stopped_at", stopped_at)
        self._lock_job(transaction, owner_id=owner_id, job_id=job_id)
        current = self._lock_policy(transaction, owner_id=owner_id, job_id=job_id)
        if current is None:
            raise PlaneError(
                "scheduled job policy is missing",
                code="scheduled_job_policy_missing",
                metadata={"owner_id": owner_id},
            )
        if current.version != expected_version:
            raise _policy_version_conflict(owner_id, current.version)
        if current.terminal_stop:
            return JobStopOutcome(
                job_id=job_id,
                owner_id=owner_id,
                stopped=False,
                policy=current,
                cancelled_occurrence_ids=(),
                cancelled_operation_ids=(),
                outstanding_assignment_ids=self._outstanding(
                    transaction, owner_id=owner_id, job_id=job_id
                ),
            )
        row = transaction.fetch_one(
            """
            UPDATE scheduled_job_policy
            SET terminal_stop = TRUE, version = version + 1, updated_at = %s
            WHERE job_id = %s AND owner_id = %s AND version = %s AND terminal_stop = FALSE
            RETURNING *
            """,
            (stopped_at, job_id, owner_id, expected_version),
        )
        if row is None:
            raise _policy_version_conflict(owner_id, None)
        result = transaction.execute(
            """
            UPDATE scheduled_job
            SET status = 'completed', next_run_at = NULL, updated_at = %s
            WHERE id = %s AND user_id = %s
            """,
            (stopped_at, job_id, owner_id),
        )
        if result.rowcount != 1:
            raise PlaneError(
                "scheduled definition status fence was lost",
                code="scheduled_job_status_conflict",
                metadata={"owner_id": owner_id},
            )
        unstarted = transaction.fetch_all(
            """
            SELECT occurrence.* FROM scheduled_occurrence AS occurrence
            JOIN scheduled_job AS job ON job.id = occurrence.job_id
            WHERE occurrence.job_id = %s
              AND occurrence.owner_user_id = %s
              AND job.user_id = %s
              AND occurrence.state IN ('pending', 'retryable', 'claimed')
            ORDER BY occurrence.occurrence_id
            FOR UPDATE OF occurrence
            """,
            (job_id, owner_id, owner_id),
        )
        cancelled: list[str] = []
        operations: list[str] = []
        for occurrence in (_occurrence(item) for item in unstarted):
            if not self.cancel_unstarted_occurrence(
                transaction,
                owner_id=owner_id,
                occurrence_id=occurrence.occurrence_id,
                expected_operation_id=occurrence.operation_id,
                terminal_code="cancelled_job_stopped",
            ):
                raise PlaneError(
                    "scheduled occurrence cancellation lost its state fence",
                    code="stale_occurrence_fence",
                    metadata={"owner_id": owner_id},
                )
            cancelled.append(occurrence.occurrence_id)
            if occurrence.operation_id is not None and occurrence.operation_id not in operations:
                operations.append(occurrence.operation_id)
        return JobStopOutcome(
            job_id=job_id,
            owner_id=owner_id,
            stopped=True,
            policy=_policy(row),
            cancelled_occurrence_ids=tuple(cancelled),
            cancelled_operation_ids=tuple(operations),
            outstanding_assignment_ids=self._outstanding(
                transaction, owner_id=owner_id, job_id=job_id
            ),
        )

    def _lock_job(self, transaction: Transaction, *, owner_id: str, job_id: str) -> None:
        row = transaction.fetch_one(
            "SELECT id FROM scheduled_job WHERE id = %s AND user_id = %s FOR UPDATE",
            (job_id, owner_id),
        )
        if row is None:
            raise PlaneError(
                "scheduled job is missing or foreign",
                code="scheduled_job_missing",
                metadata={"owner_id": owner_id},
            )

    def _lock_policy(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
    ) -> ScheduledJobPolicy | None:
        row = transaction.fetch_one(
            "SELECT * FROM scheduled_job_policy WHERE job_id = %s AND owner_id = %s FOR UPDATE",
            (job_id, owner_id),
        )
        return None if row is None else _policy(row)

    def _outstanding(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        job_id: str,
    ) -> tuple[str, ...]:
        rows = transaction.fetch_all(
            """
            SELECT binding.assignment_id FROM scheduled_occurrence_assignment AS binding
            JOIN persistent_assignment AS assignment
              ON assignment.id = binding.assignment_id
             AND assignment.owner_user_id = binding.owner_id
            WHERE binding.job_id = %s AND binding.owner_id = %s
              AND assignment.lifecycle IN ('active', 'paused')
            ORDER BY binding.admitted_at, binding.assignment_id
            """,
            (job_id, owner_id),
        )
        return tuple(dict.fromkeys(str(row["assignment_id"]) for row in rows))


def _scan_rows(
    transaction: Transaction,
    query: str,
    parameters: tuple[object, ...],
    *,
    columns: str,
    position: tuple[object, str] | None,
    limit: int,
) -> list[Record]:
    sqlite_job_projection = (
        dialect_of(transaction).is_sqlite and "to_jsonb(job) AS job_record" in query
    )
    if sqlite_job_projection:
        query = query.replace("to_jsonb(job) AS job_record", "job.id AS sqlite_job_id")
    predicate = "" if position is None else f"AND {columns} > (%s, %s)"
    values = parameters if position is None else (*parameters, *position)
    rows = list(transaction.fetch_all(query.format(position=predicate), (*values, limit)))
    if position is not None and len(rows) < limit:
        rows.extend(
            transaction.fetch_all(
                query.format(position=f"AND {columns} <= (%s, %s)"),
                (*parameters, *position, limit - len(rows)),
            )
        )
    if sqlite_job_projection:
        rows = [
            {
                **row,
                "job_record": transaction.fetch_one(
                    "SELECT * FROM scheduled_job WHERE id=%s AND user_id=%s",
                    (row["sqlite_job_id"], row["owner_user_id"]),
                ),
            }
            for row in rows
        ]
    return rows


def _rotate_owners(
    candidates: list[tuple[ScheduledJob, Record]],
    *,
    limit: int,
    last_owner: str | None,
) -> tuple[list[tuple[ScheduledJob, Record]], str | None]:
    by_owner: dict[str, deque[tuple[ScheduledJob, Record]]] = {}
    for candidate in candidates:
        by_owner.setdefault(candidate[0].owner_id, deque()).append(candidate)
    owners = sorted(by_owner)
    if last_owner is not None:
        owners = [owner for owner in owners if owner > last_owner] + [
            owner for owner in owners if owner <= last_owner
        ]
    ring = deque(owners)
    selected: list[tuple[ScheduledJob, Record]] = []
    while ring and len(selected) < limit:
        owner = ring.popleft()
        selected.append(by_owner[owner].popleft())
        last_owner = owner
        if by_owner[owner]:
            ring.append(owner)
    return selected, last_owner


def _required(name: str, value: str, *, maximum: int = 512) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")


def _bounded_code(name: str, value: str, *, maximum: int = 128) -> None:
    if len(value) > maximum or _CODE.fullmatch(value) is None:
        raise ValueError(f"{name} must be bounded snake_case")


def _uuid(name: str, value: str, *, version: int | None = None) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a canonical UUID") from exc
    if (
        parsed.int == 0
        or str(parsed) != value
        or (version is not None and parsed.version != version)
    ):
        suffix = f"v{version}" if version is not None else "UUID"
        raise ValueError(f"{name} must be a canonical non-nil {suffix}")
    return value


def _digest(name: str, value: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _limit(limit: int) -> None:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")


def _millisecond(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer millisecond timestamp")


def _instance_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or re.fullmatch(r"[A-Za-z0-9_.:-]+", value) is None
    ):
        raise ValueError("instance_id must be a bounded non-sensitive identifier")


def _claim_identity(
    *,
    owner_id: str,
    occurrence_id: str,
    claim_generation: int,
    lease_token: str,
    lease_owner: str,
) -> None:
    _required("owner_id", owner_id)
    _uuid("occurrence_id", occurrence_id, version=4)
    _uuid("lease_token", lease_token, version=4)
    _instance_id(lease_owner)
    if not isinstance(claim_generation, int) or isinstance(claim_generation, bool):
        raise ValueError("claim_generation must be a positive integer")
    if claim_generation <= 0:
        raise ValueError("claim_generation must be a positive integer")


def _zero_or_one(rowcount: int, operation: str) -> None:
    if rowcount not in {0, 1}:
        raise PlaneError(
            f"{operation} affected an impossible number of rows",
            code="scheduler_rowcount_integrity_error",
        )


def _job(row: Record) -> ScheduledJob:
    job_id = str(row["id"])
    _uuid("job_id", job_id, version=4)
    raw_scopes = row.get("consented_scopes")
    if raw_scopes is None:
        scopes: tuple[str, ...] = ()
    elif isinstance(raw_scopes, str):
        decoded = json.loads(raw_scopes)
        if not isinstance(decoded, list):
            raise PlaneError(
                "scheduled consented scopes are not a JSON array",
                code="scheduler_record_invalid",
            )
        scopes = tuple(str(scope) for scope in decoded)
    else:
        scopes = tuple(str(scope) for scope in raw_scopes)
    return ScheduledJob(
        job_id=job_id,
        owner_id=str(row["user_id"]),
        name=str(row["name"]),
        instruction=str(row["instruction"]),
        schedule_kind=str(row["schedule_kind"]),
        schedule_expression=str(row["schedule_expr"]),
        timezone=str(row["timezone"]),
        status=str(row["status"]),
        next_run_at=None if row.get("next_run_at") is None else int(row["next_run_at"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        agent_id=None if row.get("agent_id") is None else str(row["agent_id"]),
        consented_scopes=scopes,
        delivery=str(row.get("delivery") or "in_app"),
        target_chat_id=(
            None if row.get("target_chat_id") is None else str(row["target_chat_id"])
        ),
        last_run_at=(
            None if row.get("last_run_at") is None else int(row["last_run_at"])
        ),
        offline_grant_id=(
            None
            if row.get("offline_grant_id") is None
            else str(row["offline_grant_id"])
        ),
    )


def _policy_version_conflict(owner_id: str, observed_version: int | None) -> PlaneError:
    return PlaneError(
        "scheduled job policy version fence was lost",
        code="scheduled_job_policy_version_conflict",
        metadata={
            "owner_id": owner_id,
            "observed_version": "<none>" if observed_version is None else observed_version,
        },
    )


def _limits_json(policy: ScheduledJobPolicy) -> str:
    return json.dumps(dict(policy.per_episode_limits), separators=(",", ":"), sort_keys=True)


def _refusal(
    owner_id: str,
    job_id: str,
    occurrence_id: str,
    assignment_id: str,
    spend: int,
    reason: str,
    policy: ScheduledJobPolicy | None,
) -> EpisodeAdmission:
    return EpisodeAdmission(
        job_id=job_id,
        owner_id=owner_id,
        occurrence_id=occurrence_id,
        assignment_id=assignment_id,
        admitted=False,
        created=False,
        reason=reason,
        spend=spend,
        policy=policy,
    )


def _policy(row: Record) -> ScheduledJobPolicy:
    raw_limits = row.get("per_episode_limits")
    if raw_limits is None:
        limits: object = {}
    elif isinstance(raw_limits, str):
        limits = json.loads(raw_limits)
    else:
        limits = raw_limits
    if not isinstance(limits, Mapping):
        raise PlaneError(
            "scheduled job policy limits are not a JSON object",
            code="scheduler_record_invalid",
        )
    try:
        return ScheduledJobPolicy(
            job_id=str(row["job_id"]),
            owner_id=str(row["owner_id"]),
            version=int(row["version"]),
            max_runs=None if row.get("max_runs") is None else int(row["max_runs"]),
            admitted_runs=int(row["admitted_runs"]),
            per_episode_limits=tuple(limits.items()),
            max_outstanding_episodes=int(row["max_outstanding_episodes"]),
            monitor_changes=bool(row["monitor_changes"]),
            definition_revision=int(row["definition_revision"]),
            terminal_stop=bool(row["terminal_stop"]),
            last_assignment_id=(
                None if row.get("last_assignment_id") is None else str(row["last_assignment_id"])
            ),
            updated_at=int(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlaneError(
            "scheduled job policy record is invalid",
            code="scheduler_record_invalid",
        ) from exc


def _run_now(row: Record, *, created: bool) -> RunNowMaterialization:
    occurrence_id = str(row["occurrence_id"])
    job_id = str(row["job_id"])
    _uuid("occurrence_id", occurrence_id, version=4)
    _uuid("job_id", job_id, version=4)
    scheduled_for = row["scheduled_for"]
    _aware("scheduled_for", scheduled_for)
    return RunNowMaterialization(
        occurrence_id=occurrence_id,
        job_id=job_id,
        owner_id=str(row["owner_user_id"]),
        scheduled_for=scheduled_for.astimezone(UTC),
        state=OccurrenceState(str(row["state"])),
        created=created,
    )


def _job_run(row: Record) -> JobRunRecord:
    run_id = str(row["id"])
    job_id = str(row["job_id"])
    correlation_id = str(row["correlation_id"])
    _uuid("run_id", run_id, version=4)
    _uuid("job_id", job_id, version=4)
    _uuid("correlation_id", correlation_id)
    occurrence_id = (
        None if row.get("occurrence_id") is None else str(row["occurrence_id"])
    )
    operation_id = (
        None if row.get("operation_id") is None else str(row["operation_id"])
    )
    if occurrence_id is not None:
        _uuid("occurrence_id", occurrence_id, version=4)
    if operation_id is not None:
        _uuid("operation_id", operation_id, version=4)
    return JobRunRecord(
        run_id=run_id,
        job_id=job_id,
        owner_id=str(row["user_id"]),
        started_at=int(row["started_at"]),
        ended_at=None if row.get("ended_at") is None else int(row["ended_at"]),
        outcome=str(row["outcome"]),
        auth_ref=None if row.get("auth_ref") is None else str(row["auth_ref"]),
        correlation_id=correlation_id,
        summary=None if row.get("summary") is None else str(row["summary"]),
        occurrence_id=occurrence_id,
        attempt_number=(
            None if row.get("attempt_number") is None else int(row["attempt_number"])
        ),
        operation_id=operation_id,
        operation_execution_generation=(
            None
            if row.get("operation_execution_generation") is None
            else int(row["operation_execution_generation"])
        ),
        occurrence_claim_generation=(
            None
            if row.get("occurrence_claim_generation") is None
            else int(row["occurrence_claim_generation"])
        ),
    )


def _occurrence(row: Record) -> ScheduledOccurrence:
    occurrence_id = str(row["occurrence_id"])
    job_id = str(row["job_id"])
    _uuid("occurrence_id", occurrence_id, version=4)
    _uuid("job_id", job_id, version=4)
    lease_token = None if row.get("lease_token") is None else str(row["lease_token"])
    if lease_token is not None:
        _uuid("lease_token", lease_token, version=4)
    operation_id = (
        None if row.get("current_operation_id") is None else str(row["current_operation_id"])
    )
    if operation_id is not None:
        _uuid("current_operation_id", operation_id, version=4)
    terminal_at = row.get("terminal_at")
    next_attempt_at = row.get("next_attempt_at")
    if terminal_at is not None:
        _aware("terminal_at", terminal_at)
    if next_attempt_at is not None:
        _aware("next_attempt_at", next_attempt_at)
    return ScheduledOccurrence(
        occurrence_id=occurrence_id,
        job_id=job_id,
        owner_id=str(row["owner_user_id"]),
        scheduled_for=row["scheduled_for"],
        state=OccurrenceState(str(row["state"])),
        claim_generation=int(row["claim_generation"]),
        lease_token=lease_token,
        lease_owner=None if row.get("lease_owner") is None else str(row["lease_owner"]),
        lease_expires_at=row.get("lease_expires_at"),
        attempt_count=int(row["attempt_count"]),
        operation_id=operation_id,
        operation_execution_generation=(
            None
            if row.get("operation_execution_generation") is None
            else int(row["operation_execution_generation"])
        ),
        terminal_at=terminal_at,
        next_attempt_at=next_attempt_at,
        result_code=None if row.get("result_code") is None else str(row["result_code"]),
        last_error_code=(
            None
            if row.get("last_error_code") is None
            else str(row["last_error_code"])
        ),
    )


def _required_occurrence(row: Record | None, message: str, owner_id: str) -> ScheduledOccurrence:
    if row is None:
        raise PlaneError(message, code="stale_occurrence_fence", metadata={"owner_id": owner_id})
    return _occurrence(row)


def _effect(row: Record) -> EffectRecord:
    occurrence_id = str(row["occurrence_id"])
    _uuid("occurrence_id", occurrence_id, version=4)
    operation_id = None if row.get("operation_id") is None else str(row["operation_id"])
    if operation_id is not None:
        _uuid("operation_id", operation_id, version=4)
    published_at = row.get("published_at")
    if published_at is not None:
        _aware("published_at", published_at)
    return EffectRecord(
        occurrence_id=occurrence_id,
        effect_kind=str(row["effect_kind"]),
        effect_key=str(row["effect_key"]),
        payload_digest=str(row["payload_digest"]),
        state=EffectState(str(row["state"])),
        operation_id=operation_id,
        operation_execution_generation=int(row["operation_execution_generation"]),
        occurrence_claim_generation=int(row["occurrence_claim_generation"]),
        downstream_receipt_digest=(
            None
            if row.get("downstream_receipt_digest") is None
            else str(row["downstream_receipt_digest"])
        ),
        published_at=published_at,
    )


__all__ = (
    "ClaimedOccurrenceRecord",
    "DueClaimBatch",
    "EffectRecord",
    "EffectReservationOutcome",
    "EffectState",
    "JobRunRecord",
    "OccurrenceState",
    "RecoveredAttemptRecord",
    "RunNowMaterialization",
    "ScheduledJob",
    "ScheduledOccurrence",
    "SchedulerRepository",
    "StagedChatLayout",
    "StagedChatMessage",
    "StagedChatPublication",
)
