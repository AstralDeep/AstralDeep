"""Owner-isolated durable state for jobs executed on external remote machines, with
owner and poll-generation fences. Used by orchestrator/remote_jobs.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _required_id,
    _row_value,
    _single_returned,
)


@dataclass(frozen=True, slots=True)
class TrackedJobRecord:
    tracked_job_id: str
    owner_id: str = field(repr=False)
    machine_id: str
    scheduler_job_id: str
    conversation_id: str | None = field(default=None, repr=False)
    submit_marker: str | None = field(default=None, repr=False)
    output_path: str | None = field(default=None, repr=False)
    component_id: str | None = None
    job_name: str = ""
    state: str = "submitted"
    exit_code: str | None = None
    terminal: bool = False
    notify_on_finish: bool = False
    notified: bool = False
    fail_count: int = 0
    created_at: int = 0
    last_polled_at: int | None = None
    finished_at: int | None = None


class TrackedJobRepository:
    _FIELDS = (
        "tracked_job_id, owner_user_id, machine_id, chat_id, scheduler_job_id, "
        "submit_marker, output_path, component_id, job_name, state, exit_code, "
        "terminal, notify_on_finish, notified, fail_count, created_at, "
        "last_polled_at, finished_at"
    )

    def create(
        self,
        transaction: Transaction,
        record: TrackedJobRecord,
    ) -> TrackedJobRecord:
        job = _validated(record)
        result = transaction.execute(
            f"""
            INSERT INTO tracked_job (
                tracked_job_id, owner_user_id, machine_id, chat_id,
                scheduler_job_id, submit_marker, output_path, component_id,
                job_name, state, exit_code, terminal, notify_on_finish,
                notified, fail_count, created_at, last_polled_at, finished_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                job.tracked_job_id,
                job.owner_id,
                job.machine_id,
                job.conversation_id,
                job.scheduler_job_id,
                job.submit_marker,
                job.output_path,
                job.component_id,
                job.job_name,
                job.state,
                job.exit_code,
                job.terminal,
                job.notify_on_finish,
                job.notified,
                job.fail_count,
                job.created_at,
                job.last_polled_at,
                job.finished_at,
            ),
        )
        if getattr(result, "returned_records", ()):
            return _owned(_single_returned(result, "tracked_jobs.create"), job.owner_id)
        existing = self.get_by_scheduler_job(
            transaction,
            owner_id=job.owner_id,
            scheduler_job_id=job.scheduler_job_id,
            machine_id=job.machine_id,
        )
        if existing is None:
            raise RepositoryConflictError("tracked job identity is owned elsewhere")
        if existing != job:
            raise RepositoryConflictError("tracked job replay changed immutable state")
        return existing

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        tracked_job_id: str,
        lock: bool = False,
    ) -> TrackedJobRecord | None:
        owner = _required_id(owner_id, "owner_id")
        tracked = _required_id(tracked_job_id, "tracked_job_id")
        if type(lock) is not bool:
            raise RepositoryValidationError("lock must be a boolean")
        if lock:
            self._lock_owner(query, owner)
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM tracked_job "
            "WHERE tracked_job_id = %s AND owner_user_id = %s" + (" FOR UPDATE" if lock else ""),
            (tracked, owner),
        )
        return None if row is None else _owned(row, owner)

    def get_by_scheduler_job(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        scheduler_job_id: str,
        machine_id: str | None = None,
    ) -> TrackedJobRecord | None:
        owner = _required_id(owner_id, "owner_id")
        scheduled = _required_id(scheduler_job_id, "scheduler_job_id")
        machine = None if machine_id is None else _required_id(machine_id, "machine_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM tracked_job "
            "WHERE owner_user_id = %s AND scheduler_job_id = %s "
            "AND (%s IS NULL OR machine_id = %s)",
            (owner, scheduled, machine, machine),
        )
        return None if row is None else _owned(row, owner)

    def list_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        include_terminal: bool = True,
        limit: int = 200,
    ) -> tuple[TrackedJobRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        maximum = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"SELECT {self._FIELDS} FROM tracked_job "
            "WHERE owner_user_id = %s AND (%s OR terminal = FALSE) "
            "ORDER BY created_at DESC, tracked_job_id ASC LIMIT %s",
            (owner, bool(include_terminal), maximum),
        )
        return tuple(_owned(row, owner) for row in rows)

    def list_open_for_administration(
        self,
        query: QueryExecutor,
        *,
        limit: int = 200,
    ) -> tuple[TrackedJobRecord, ...]:
        maximum = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"SELECT {self._FIELDS} FROM tracked_job "
            "WHERE terminal = FALSE ORDER BY created_at ASC, tracked_job_id ASC LIMIT %s",
            (maximum,),
        )
        return tuple(_record(row) for row in rows)

    def apply_poll(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        tracked_job_id: str,
        expected_fail_count: int,
        expected_last_polled_at: int | None,
        state: str,
        exit_code: str | None,
        terminal: bool,
        fail_count: int,
        polled_at: int,
    ) -> TrackedJobRecord:
        owner = _required_id(owner_id, "owner_id")
        tracked = _required_id(tracked_job_id, "tracked_job_id")
        expected_failures = _non_negative_int(expected_fail_count, "expected_fail_count")
        expected_poll = _optional_time(expected_last_polled_at, "expected_last_polled_at")
        safe_state = _bounded_text(state, "state", maximum=128)
        safe_exit = (
            None
            if exit_code is None
            else _bounded_text(exit_code, "exit_code", maximum=512, allow_empty=True)
        )
        failures = _non_negative_int(fail_count, "fail_count")
        observed = _non_negative_int(polled_at, "polled_at")
        self._lock_owner(transaction, owner)
        result = transaction.execute(
            f"""
            UPDATE tracked_job
               SET state = %s, exit_code = %s, terminal = %s,
                   fail_count = %s, last_polled_at = %s,
                   finished_at = CASE WHEN %s THEN COALESCE(finished_at, %s)
                                      ELSE finished_at END
             WHERE tracked_job_id = %s AND owner_user_id = %s
               AND terminal = FALSE AND fail_count = %s
               AND last_polled_at IS NOT DISTINCT FROM %s
            RETURNING {self._FIELDS}
            """,
            (
                safe_state,
                safe_exit,
                bool(terminal),
                failures,
                observed,
                bool(terminal),
                observed,
                tracked,
                owner,
                expected_failures,
                expected_poll,
            ),
        )
        if getattr(result, "returned_records", ()):
            return _owned(_single_returned(result, "tracked_jobs.apply_poll"), owner)
        existing = self.get(transaction, owner_id=owner, tracked_job_id=tracked)
        if existing is None:
            raise RepositoryNotFoundError("owner-scoped tracked job was not found")
        raise RepositoryConflictError("tracked job poll compare-and-set fence is stale")

    @staticmethod
    def _lock_owner(transaction: Transaction, owner_id: str) -> None:
        transaction.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner_id,))
        retired = transaction.fetch_one(
            "SELECT state FROM astralplane_blob_owner_state WHERE owner_id=%s FOR UPDATE",
            (owner_id,),
        )
        if retired is not None and retired["state"] != "active":
            raise RepositoryConflictError(
                "tracked_job_owner_retired", code="tracked_job_owner_retired"
            )

    def mark_notified(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        tracked_job_id: str,
    ) -> bool:
        owner = _required_id(owner_id, "owner_id")
        tracked = _required_id(tracked_job_id, "tracked_job_id")
        result = transaction.execute(
            "UPDATE tracked_job SET notified = TRUE "
            "WHERE tracked_job_id = %s AND owner_user_id = %s "
            "AND terminal = TRUE AND notify_on_finish = TRUE AND notified = FALSE",
            (tracked, owner),
        )
        if result.rowcount not in (0, 1):
            raise RepositoryDataError("tracked job notification CAS was ambiguous")
        return result.rowcount == 1

    def delete_owner(self, transaction: Transaction, *, owner_id: str) -> int:
        owner = _required_id(owner_id, "owner_id")
        result = transaction.execute(
            "DELETE FROM tracked_job WHERE owner_user_id = %s",
            (owner,),
        )
        if result.rowcount < 0:
            raise RepositoryDataError("tracked-job owner deletion returned an invalid row count")
        return result.rowcount


def _optional_time(value: object, field: str) -> int | None:
    return None if value is None else _non_negative_int(value, field)


def _optional_text(value: object, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _validated(record: TrackedJobRecord) -> TrackedJobRecord:
    if not isinstance(record, TrackedJobRecord):
        raise RepositoryValidationError("record must be a TrackedJobRecord")
    tracked = _required_id(record.tracked_job_id, "tracked_job_id")
    owner = _required_id(record.owner_id, "owner_id")
    machine = _required_id(record.machine_id, "machine_id")
    scheduled = _required_id(record.scheduler_job_id, "scheduler_job_id")
    conversation = (
        None
        if record.conversation_id is None
        else _required_id(record.conversation_id, "conversation_id")
    )
    marker = _optional_text(record.submit_marker, "submit_marker", maximum=2048)
    path = _optional_text(record.output_path, "output_path", maximum=4096)
    component = (
        None
        if record.component_id is None
        else _required_id(record.component_id, "component_id")
    )
    name = _bounded_text(record.job_name, "job_name", maximum=1024, allow_empty=True)
    state = _bounded_text(record.state, "state", maximum=128)
    exit_code = _optional_text(record.exit_code, "exit_code", maximum=512)
    failures = _non_negative_int(record.fail_count, "fail_count")
    created = _non_negative_int(record.created_at, "created_at")
    polled = _optional_time(record.last_polled_at, "last_polled_at")
    finished = _optional_time(record.finished_at, "finished_at")
    if bool(record.terminal) != (finished is not None):
        raise RepositoryValidationError("tracked job terminal timestamp is inconsistent")
    if polled is not None and polled < created:
        raise RepositoryValidationError("last_polled_at cannot precede created_at")
    if finished is not None and finished < created:
        raise RepositoryValidationError("finished_at cannot precede created_at")
    return TrackedJobRecord(
        tracked_job_id=tracked,
        owner_id=owner,
        machine_id=machine,
        scheduler_job_id=scheduled,
        conversation_id=conversation,
        submit_marker=marker,
        output_path=path,
        component_id=component,
        job_name=name,
        state=state,
        exit_code=exit_code,
        terminal=bool(record.terminal),
        notify_on_finish=bool(record.notify_on_finish),
        notified=bool(record.notified),
        fail_count=failures,
        created_at=created,
        last_polled_at=polled,
        finished_at=finished,
    )


def _record(row: Mapping[str, Any]) -> TrackedJobRecord:
    try:
        return _validated(
            TrackedJobRecord(
                tracked_job_id=str(_row_value(row, "tracked_job_id")),
                owner_id=str(_row_value(row, "owner_user_id")),
                machine_id=str(_row_value(row, "machine_id")),
                scheduler_job_id=str(_row_value(row, "scheduler_job_id")),
                conversation_id=(
                    None if row.get("chat_id") is None else str(row["chat_id"])
                ),
                submit_marker=row.get("submit_marker"),
                output_path=row.get("output_path"),
                component_id=(
                    None if row.get("component_id") is None else str(row["component_id"])
                ),
                job_name=str(row.get("job_name") or ""),
                state=str(_row_value(row, "state")),
                exit_code=(
                    None if row.get("exit_code") is None else str(row["exit_code"])
                ),
                terminal=bool(_row_value(row, "terminal")),
                notify_on_finish=bool(_row_value(row, "notify_on_finish")),
                notified=bool(_row_value(row, "notified")),
                fail_count=_row_value(row, "fail_count"),
                created_at=_row_value(row, "created_at"),
                last_polled_at=row.get("last_polled_at"),
                finished_at=row.get("finished_at"),
            )
        )
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted tracked job is invalid") from exc


def _owned(row: Mapping[str, Any], owner_id: str) -> TrackedJobRecord:
    record = _record(row)
    if record.owner_id != owner_id:
        raise RepositoryDataError("tracked job query returned another owner's row")
    return record


__all__ = ("TrackedJobRecord", "TrackedJobRepository")
