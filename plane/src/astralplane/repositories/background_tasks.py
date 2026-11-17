"""Owner-scoped compatibility projection of WorkAdmission operations into legacy
background-task status rows. Read by AstralDeep's orchestrator/async_tasks.py and
orchestrator.py; advances only forward through queued/running/terminal states.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _positive_int,
    _required_id,
    _row_value,
    _single_returned,
)


class BackgroundTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYABLE = "retryable"


_TRANSITIONS = {
    BackgroundTaskStatus.QUEUED: frozenset(
        {
            BackgroundTaskStatus.RUNNING,
            BackgroundTaskStatus.FAILED,
            BackgroundTaskStatus.CANCELLED,
            BackgroundTaskStatus.RETRYABLE,
        }
    ),
    BackgroundTaskStatus.RUNNING: frozenset(
        {
            BackgroundTaskStatus.COMPLETED,
            BackgroundTaskStatus.FAILED,
            BackgroundTaskStatus.CANCELLED,
            BackgroundTaskStatus.RETRYABLE,
        }
    ),
    BackgroundTaskStatus.RETRYABLE: frozenset(
        {
            BackgroundTaskStatus.QUEUED,
            BackgroundTaskStatus.RUNNING,
            BackgroundTaskStatus.FAILED,
            BackgroundTaskStatus.CANCELLED,
        }
    ),
    BackgroundTaskStatus.COMPLETED: frozenset(),
    BackgroundTaskStatus.FAILED: frozenset(),
    BackgroundTaskStatus.CANCELLED: frozenset(),
}

_TERMINAL_STATUSES = frozenset(
    {
        BackgroundTaskStatus.COMPLETED,
        BackgroundTaskStatus.FAILED,
        BackgroundTaskStatus.CANCELLED,
        BackgroundTaskStatus.RETRYABLE,
    }
)

_LEGACY_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_LEGACY_SHORT_ID_PATTERN = r"^[0-9a-f]{8}$"


@dataclass(frozen=True, slots=True)
class BackgroundTaskRecord:
    task_id: str
    owner_id: str = field(repr=False)
    conversation_id: str = field(repr=False)
    kind: str
    status: BackgroundTaskStatus
    title: str
    summary: str | None = field(default=None, repr=False)
    created_at: datetime | None = None
    completed_at: datetime | None = None
    notified: bool = False
    operation_id: str | None = None
    operation_execution_generation: int | None = None


class BackgroundTaskRepository:
    _FIELDS = (
        "task_id, user_id, chat_id, kind, status, title, summary, created_at, "
        "completed_at, notified, operation_id, operation_execution_generation"
    )

    def create(
        self,
        transaction: Transaction,
        record: BackgroundTaskRecord,
    ) -> BackgroundTaskRecord:
        task = _validated(record)
        result = transaction.execute(
            f"""
            INSERT INTO background_task (
                task_id, user_id, chat_id, kind, status, title, summary,
                created_at, completed_at, notified, operation_id,
                operation_execution_generation
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (task_id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                task.task_id,
                task.owner_id,
                task.conversation_id,
                task.kind,
                task.status.value,
                task.title,
                task.summary,
                task.created_at,
                task.completed_at,
                task.notified,
                task.operation_id,
                task.operation_execution_generation,
            ),
        )
        if getattr(result, "returned_records", ()):
            return _owned(
                _single_returned(result, "background_tasks.create"), task.owner_id
            )
        existing = self.get(
            transaction,
            owner_id=task.owner_id,
            task_id=task.task_id,
        )
        if existing is None:
            raise RepositoryConflictError("background task identity is owned elsewhere")
        if existing != task:
            raise RepositoryConflictError("background task replay changed immutable state")
        return existing

    def apply_operation_projection(
        self,
        transaction: Transaction,
        record: BackgroundTaskRecord,
    ) -> BackgroundTaskRecord:
        task = _validated(record)
        result = transaction.execute(
            f"""
            INSERT INTO background_task (
                task_id, user_id, chat_id, kind, status, title, summary,
                created_at, completed_at, notified, operation_id,
                operation_execution_generation
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (task_id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            _parameters(task),
        )
        if getattr(result, "returned_records", ()):
            return _owned(
                _single_returned(result, "background_tasks.apply_operation_projection"),
                task.owner_id,
            )

        row = transaction.fetch_one(
            f"SELECT {self._FIELDS} FROM background_task "
            "WHERE task_id = %s FOR UPDATE",
            (task.task_id,),
        )
        if row is None:  # pragma: no cover
            raise RepositoryConflictError("background task projection identity disappeared")
        existing = _record(row)
        if existing.owner_id != task.owner_id:
            raise RepositoryConflictError("background task identity is owned elsewhere")
        immutable_existing = (
            existing.conversation_id,
            existing.kind,
            existing.title,
            existing.created_at,
            existing.operation_id,
        )
        immutable_projected = (
            task.conversation_id,
            task.kind,
            task.title,
            task.created_at,
            task.operation_id,
        )
        if immutable_existing != immutable_projected:
            raise RepositoryConflictError(
                "background task projection changed immutable identity"
            )
        _validate_projection_advance(existing, task)

        summary = _enriched_value(existing.summary, task.summary, "summary")
        completed_at = _enriched_value(
            existing.completed_at,
            task.completed_at,
            "completed_at",
        )
        notified = existing.notified or task.notified
        generation = _projected_generation(existing, task)
        merged = BackgroundTaskRecord(
            task_id=existing.task_id,
            owner_id=existing.owner_id,
            conversation_id=existing.conversation_id,
            kind=existing.kind,
            status=task.status,
            title=existing.title,
            summary=summary,
            created_at=existing.created_at,
            completed_at=completed_at,
            notified=notified,
            operation_id=existing.operation_id,
            operation_execution_generation=generation,
        )
        if merged == existing:
            return existing
        updated = transaction.execute(
            f"""
            UPDATE background_task
               SET status = %s, summary = %s, completed_at = %s,
                   notified = %s, operation_execution_generation = %s
             WHERE task_id = %s AND user_id = %s AND status = %s
               AND operation_execution_generation IS NOT DISTINCT FROM %s
               AND summary IS NOT DISTINCT FROM %s
               AND completed_at IS NOT DISTINCT FROM %s
               AND notified = %s
            RETURNING {self._FIELDS}
            """,
            (
                merged.status.value,
                merged.summary,
                merged.completed_at,
                merged.notified,
                merged.operation_execution_generation,
                existing.task_id,
                existing.owner_id,
                existing.status.value,
                existing.operation_execution_generation,
                existing.summary,
                existing.completed_at,
                existing.notified,
            ),
        )
        if not getattr(updated, "returned_records", ()):
            raise RepositoryConflictError(
                "background task operation projection compare-and-set fence is stale"
            )
        return _owned(
            _single_returned(updated, "background_tasks.apply_operation_projection"),
            task.owner_id,
        )

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        task_id: str,
    ) -> BackgroundTaskRecord | None:
        owner = _required_id(owner_id, "owner_id")
        task = _required_id(task_id, "task_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM background_task "
            "WHERE task_id = %s AND user_id = %s",
            (task, owner),
        )
        return None if row is None else _owned(row, owner)

    def list_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        status: BackgroundTaskStatus | str | None = None,
        limit: int = 100,
    ) -> tuple[BackgroundTaskRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        maximum = _bounded_limit(limit, maximum=1000)
        state = None if status is None else _status(status)
        rows = query.fetch_all(
            f"SELECT {self._FIELDS} FROM background_task "
            "WHERE user_id = %s AND (%s IS NULL OR status = %s) "
            "ORDER BY created_at DESC, task_id ASC LIMIT %s",
            (
                owner,
                None if state is None else state.value,
                None if state is None else state.value,
                maximum,
            ),
        )
        return tuple(_owned(row, owner) for row in rows)

    def transition(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        task_id: str,
        expected_status: BackgroundTaskStatus | str,
        status: BackgroundTaskStatus | str,
        completed_at: datetime | None = None,
        summary: str | None = None,
        expected_operation_execution_generation: int | None = None,
    ) -> BackgroundTaskRecord:
        owner = _required_id(owner_id, "owner_id")
        task = _required_id(task_id, "task_id")
        expected = _status(expected_status)
        target = _status(status)
        if target not in _TRANSITIONS[expected]:
            raise RepositoryValidationError(
                "background task state transition is unsupported"
            )
        terminal = target in {
            BackgroundTaskStatus.COMPLETED,
            BackgroundTaskStatus.FAILED,
            BackgroundTaskStatus.CANCELLED,
        }
        if terminal != (completed_at is not None):
            raise RepositoryValidationError(
                "terminal background task transitions require completed_at"
            )
        safe_summary = (
            None
            if summary is None
            else _bounded_text(summary, "summary", maximum=16_384, allow_empty=True)
        )
        generation = (
            None
            if expected_operation_execution_generation is None
            else _positive_int(
                expected_operation_execution_generation,
                "expected_operation_execution_generation",
            )
        )
        result = transaction.execute(
            f"""
            UPDATE background_task
               SET status = %s, completed_at = %s,
                   summary = CASE WHEN %s IS NULL THEN summary ELSE %s END
             WHERE task_id = %s AND user_id = %s AND status = %s
               AND operation_execution_generation IS NOT DISTINCT FROM %s
            RETURNING {self._FIELDS}
            """,
            (
                target.value,
                completed_at,
                safe_summary,
                safe_summary,
                task,
                owner,
                expected.value,
                generation,
            ),
        )
        if getattr(result, "returned_records", ()):
            return _owned(
                _single_returned(result, "background_tasks.transition"), owner
            )
        existing = self.get(transaction, owner_id=owner, task_id=task)
        if existing is None:
            raise RepositoryNotFoundError("owner-scoped background task was not found")
        raise RepositoryConflictError("background task compare-and-set fence is stale")

    def mark_notified(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        task_id: str,
    ) -> bool:
        owner = _required_id(owner_id, "owner_id")
        task = _required_id(task_id, "task_id")
        result = transaction.execute(
            "UPDATE background_task SET notified = TRUE "
            "WHERE task_id = %s AND user_id = %s AND notified = FALSE",
            (task, owner),
        )
        if result.rowcount not in (0, 1):
            raise RepositoryDataError("background task notification CAS was ambiguous")
        return result.rowcount == 1

    def oldest_overdue_for_administration(
        self,
        query: QueryExecutor,
        *,
        cutoff_at: datetime,
    ) -> datetime | None:
        cutoff = _aware_time(cutoff_at, "cutoff_at")
        row = query.fetch_one(
            f"""
            SELECT COALESCE(completed_at, created_at) AS retained_at
            FROM background_task
            WHERE operation_id IS NULL
              AND COALESCE(completed_at, created_at) < %s
              AND (
                operation_execution_generation IS NOT NULL
                OR task_id ~* '{_LEGACY_UUID_PATTERN}'
                OR (
                    task_id ~* '{_LEGACY_SHORT_ID_PATTERN}'
                    AND status IN ('completed', 'failed', 'cancelled', 'retryable')
                )
              )
            ORDER BY COALESCE(completed_at, created_at), task_id
            LIMIT 1
            """,
            (cutoff,),
        )
        if row is None:
            return None
        return _aware_time(_row_value(row, "retained_at"), "retained_at")

    def purge_overdue_for_administration(
        self,
        transaction: Transaction,
        *,
        cutoff_at: datetime,
        limit: int = 1000,
    ) -> tuple[str, ...]:
        cutoff = _aware_time(cutoff_at, "cutoff_at")
        bounded = _bounded_limit(limit, maximum=5000)
        result = transaction.execute(
            f"""
            WITH candidates AS (
                SELECT task_id
                FROM background_task
                WHERE operation_id IS NULL
                  AND COALESCE(completed_at, created_at) < %s
                  AND (
                    operation_execution_generation IS NOT NULL
                    OR task_id ~* '{_LEGACY_UUID_PATTERN}'
                    OR (
                        task_id ~* '{_LEGACY_SHORT_ID_PATTERN}'
                        AND status IN ('completed', 'failed', 'cancelled', 'retryable')
                    )
                  )
                ORDER BY COALESCE(completed_at, created_at), task_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            DELETE FROM background_task AS task
            USING candidates
            WHERE task.task_id = candidates.task_id
            RETURNING task.task_id
            """,
            (cutoff, bounded),
        )
        rows = tuple(getattr(result, "returned_records", ()))
        task_ids = tuple(
            _required_id(_row_value(row, "task_id"), "persisted task_id")
            for row in rows
        )
        if len(task_ids) > bounded or len(set(task_ids)) != len(task_ids):
            raise RepositoryDataError("background task purge returned invalid identities")
        return task_ids


def _status(value: BackgroundTaskStatus | str) -> BackgroundTaskStatus:
    try:
        return BackgroundTaskStatus(value)
    except (TypeError, ValueError) as exc:
        raise RepositoryValidationError("background task status is unsupported") from exc


def _aware_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryValidationError(f"{field} must be a timezone-aware datetime")
    return value


def _parameters(task: BackgroundTaskRecord) -> tuple[object, ...]:
    return (
        task.task_id,
        task.owner_id,
        task.conversation_id,
        task.kind,
        task.status.value,
        task.title,
        task.summary,
        task.created_at,
        task.completed_at,
        task.notified,
        task.operation_id,
        task.operation_execution_generation,
    )


def _validate_projection_advance(
    existing: BackgroundTaskRecord,
    projected: BackgroundTaskRecord,
) -> None:
    if existing.status in _TERMINAL_STATUSES:
        if projected.status is not existing.status:
            raise RepositoryConflictError(
                "background task terminal projection cannot be overwritten"
            )
        return
    if existing.status is BackgroundTaskStatus.QUEUED:
        allowed = {
            BackgroundTaskStatus.QUEUED,
            BackgroundTaskStatus.RUNNING,
            *_TERMINAL_STATUSES,
        }
    elif existing.status is BackgroundTaskStatus.RUNNING:
        allowed = {BackgroundTaskStatus.RUNNING, *_TERMINAL_STATUSES}
    else:  # pragma: no cover
        allowed = {existing.status}
    if projected.status not in allowed:
        raise RepositoryConflictError("background task operation projection moved backwards")


def _enriched_value(existing: Any, projected: Any, field: str) -> Any:
    if projected is None or projected == existing:
        return existing
    if existing is None:
        return projected
    raise RepositoryConflictError(
        f"background task projection changed established {field}"
    )


def _projected_generation(
    existing: BackgroundTaskRecord,
    projected: BackgroundTaskRecord,
) -> int | None:
    current = existing.operation_execution_generation
    incoming = projected.operation_execution_generation
    if incoming is None:
        return current
    if current is not None and incoming < current:
        raise RepositoryConflictError(
            "background task operation generation moved backwards"
        )
    return incoming


def _validated(record: BackgroundTaskRecord) -> BackgroundTaskRecord:
    if not isinstance(record, BackgroundTaskRecord):
        raise RepositoryValidationError("record must be a BackgroundTaskRecord")
    task = _required_id(record.task_id, "task_id")
    owner = _required_id(record.owner_id, "owner_id")
    conversation = _required_id(record.conversation_id, "conversation_id")
    kind = _bounded_text(record.kind, "kind", maximum=128)
    title = _bounded_text(record.title, "title", maximum=1024, allow_empty=True)
    summary = (
        None
        if record.summary is None
        else _bounded_text(record.summary, "summary", maximum=16_384, allow_empty=True)
    )
    status = _status(record.status)
    operation = (
        None
        if record.operation_id is None
        else _required_id(record.operation_id, "operation_id")
    )
    generation = (
        None
        if record.operation_execution_generation is None
        else _positive_int(
            record.operation_execution_generation, "operation_execution_generation"
        )
    )
    if operation is None and generation is not None:
        raise RepositoryValidationError(
            "operation generation requires an operation identity"
        )
    created_at = (
        None if record.created_at is None else _aware_time(record.created_at, "created_at")
    )
    completed_at = (
        None
        if record.completed_at is None
        else _aware_time(record.completed_at, "completed_at")
    )
    terminal = status in _TERMINAL_STATUSES
    if terminal != (completed_at is not None):
        raise RepositoryValidationError(
            "background task terminal timestamp is inconsistent"
        )
    return BackgroundTaskRecord(
        task_id=task,
        owner_id=owner,
        conversation_id=conversation,
        kind=kind,
        status=status,
        title=title,
        summary=summary,
        created_at=created_at,
        completed_at=completed_at,
        notified=bool(record.notified),
        operation_id=operation,
        operation_execution_generation=generation,
    )


def _record(row: Mapping[str, Any]) -> BackgroundTaskRecord:
    try:
        return _validated(
            BackgroundTaskRecord(
                task_id=str(_row_value(row, "task_id")),
                owner_id=str(_row_value(row, "user_id")),
                conversation_id=str(_row_value(row, "chat_id")),
                kind=str(_row_value(row, "kind")),
                status=_status(_row_value(row, "status")),
                title=str(row.get("title") or ""),
                summary=None if row.get("summary") is None else str(row["summary"]),
                created_at=row.get("created_at"),
                completed_at=row.get("completed_at"),
                notified=bool(_row_value(row, "notified")),
                operation_id=(
                    None if row.get("operation_id") is None else str(row["operation_id"])
                ),
                operation_execution_generation=row.get(
                    "operation_execution_generation"
                ),
            )
        )
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted background task is invalid") from exc


def _owned(row: Mapping[str, Any], owner_id: str) -> BackgroundTaskRecord:
    record = _record(row)
    if record.owner_id != owner_id:
        raise RepositoryDataError("background task query returned another owner's row")
    return record


__all__ = (
    "BackgroundTaskRecord",
    "BackgroundTaskRepository",
    "BackgroundTaskStatus",
)
