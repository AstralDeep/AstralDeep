"""Owner-isolated, append-only histories of a mesh's long-running work: executor leases with
their handoffs and goal state transitions. Each entry is derived from one event, is
idempotent by that event id, and extends its run's history contiguously; the host decides
which events are authorized and this module keeps the one-executor and finished-run rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.database.dialect import dialect_of
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _required_id,
    _row_value,
)
from astralplane.repositories._mesh_values import (
    bounded_integer,
    hash_value,
    returned_row,
    stored_integer,
)

_LOCK_SEED: Final = 105_041
_MAX_PAGE: Final = 1000
_TERMINAL_LEASE: Final = frozenset({"completed", "failed", "cancelled"})
_TERMINAL_GOAL: Final = frozenset({"achieved", "abandoned"})
_LEASE_KINDS: Final = frozenset({"take", "transfer", "takeover", "state"})
_LEASE_STATES: Final = frozenset({"running", "paused", "completed", "failed", "cancelled"})
_GOAL_STATES: Final = frozenset({"active", "paused", "stalled", "achieved", "abandoned"})

_LEASE_FIELDS: Final = (
    "owner_id, mesh_id, run_id, epoch, kind, executor_feed_id, state, event_id, recorded_at"
)
_GOAL_FIELDS: Final = (
    "owner_id, mesh_id, run_id, revision, goal, state, rounds, stalls, event_id, recorded_at"
)

_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))"

_LEASE_BY_EVENT_SQL: Final = f"""
SELECT {_LEASE_FIELDS} FROM run_lease_entry
WHERE owner_id = %s AND mesh_id = %s AND event_id = %s
""".strip()

_LEASE_LATEST_SQL: Final = f"""
SELECT {_LEASE_FIELDS} FROM run_lease_entry
WHERE owner_id = %s AND mesh_id = %s AND run_id = %s
ORDER BY epoch DESC LIMIT 1
""".strip()

_LEASE_INSERT_SQL: Final = f"""
INSERT INTO run_lease_entry (
    owner_id, mesh_id, run_id, epoch, kind, executor_feed_id, state, event_id, recorded_at
)
SELECT mesh.owner_id, mesh.mesh_id, %s, %s, %s, %s, %s, %s, %s
FROM event_mesh AS mesh
WHERE mesh.owner_id = %s AND mesh.mesh_id = %s
RETURNING {_LEASE_FIELDS}
""".strip()

_LEASE_HISTORY_SQL: Final = f"""
SELECT {_LEASE_FIELDS} FROM run_lease_entry
WHERE owner_id = %s AND mesh_id = %s AND run_id = %s
ORDER BY epoch ASC LIMIT %s
""".strip()

_EXECUTOR_RUNS_SQL: Final = f"""
SELECT {_LEASE_FIELDS} FROM (
    SELECT DISTINCT ON (run_id) {_LEASE_FIELDS} FROM run_lease_entry
    WHERE owner_id = %s AND mesh_id = %s
    ORDER BY run_id ASC, epoch DESC
) AS latest
WHERE latest.executor_feed_id = %s AND latest.state IN ('running', 'paused')
ORDER BY latest.run_id ASC LIMIT %s
""".strip()

_GOAL_BY_EVENT_SQL: Final = f"""
SELECT {_GOAL_FIELDS} FROM run_goal_transition
WHERE owner_id = %s AND mesh_id = %s AND event_id = %s
""".strip()

_EXECUTOR_RUNS_SQLITE_SQL: Final = f"""
SELECT {_LEASE_FIELDS} FROM (
    SELECT {_LEASE_FIELDS}, row_number() OVER (PARTITION BY run_id ORDER BY epoch DESC) AS rank
    FROM run_lease_entry WHERE owner_id = %s AND mesh_id = %s
) AS latest
WHERE latest.rank = 1 AND latest.executor_feed_id = %s AND latest.state IN ('running', 'paused')
ORDER BY latest.run_id ASC LIMIT %s
""".strip()

_GOAL_LATEST_SQL: Final = f"""
SELECT {_GOAL_FIELDS} FROM run_goal_transition
WHERE owner_id = %s AND mesh_id = %s AND run_id = %s
ORDER BY revision DESC LIMIT 1
""".strip()

_GOAL_INSERT_SQL: Final = f"""
INSERT INTO run_goal_transition (
    owner_id, mesh_id, run_id, revision, goal, state, rounds, stalls, event_id, recorded_at
)
SELECT mesh.owner_id, mesh.mesh_id, %s, %s, %s, %s, %s, %s, %s, %s
FROM event_mesh AS mesh
WHERE mesh.owner_id = %s AND mesh.mesh_id = %s
RETURNING {_GOAL_FIELDS}
""".strip()

_GOAL_HISTORY_SQL: Final = f"""
SELECT {_GOAL_FIELDS} FROM run_goal_transition
WHERE owner_id = %s AND mesh_id = %s AND run_id = %s
ORDER BY revision ASC LIMIT %s
""".strip()


@dataclass(frozen=True, slots=True)
class RunLeaseRecord:
    owner_id: str
    mesh_id: str
    run_id: str
    epoch: int
    kind: str
    executor_feed_id: str
    state: str
    event_id: str
    recorded_at: int


@dataclass(frozen=True, slots=True)
class RunGoalRecord:
    owner_id: str
    mesh_id: str
    run_id: str
    revision: int
    goal: str
    state: str
    rounds: int
    stalls: int
    event_id: str
    recorded_at: int


def _lease(row: Mapping[str, object]) -> RunLeaseRecord:
    return RunLeaseRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        run_id=str(_row_value(row, "run_id")),
        epoch=stored_integer(row, "epoch"),
        kind=str(_row_value(row, "kind")),
        executor_feed_id=str(_row_value(row, "executor_feed_id")),
        state=str(_row_value(row, "state")),
        event_id=str(_row_value(row, "event_id")),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _goal(row: Mapping[str, object]) -> RunGoalRecord:
    return RunGoalRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        run_id=str(_row_value(row, "run_id")),
        revision=stored_integer(row, "revision"),
        goal=str(_row_value(row, "goal")),
        state=str(_row_value(row, "state")),
        rounds=stored_integer(row, "rounds"),
        stalls=stored_integer(row, "stalls"),
        event_id=str(_row_value(row, "event_id")),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _choice(value: object, field: str, allowed: frozenset[str]) -> str:
    text = _bounded_text(value, field, maximum=32)
    if text not in allowed:
        raise RepositoryValidationError(
            f"{field} is not a supported value", metadata={"field": field}
        )
    return text


def _lock(transaction: Transaction, owner: str, mesh: str, run: str) -> None:
    transaction.fetch_one(_LOCK_SQL, (f"{owner}\x1f{mesh}\x1f{run}", _LOCK_SEED))


class RunRepository:
    def record_lease(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        run_id: str,
        kind: str,
        executor_feed_id: str,
        state: str,
        event_id: str,
        recorded_at: int,
    ) -> RunLeaseRecord:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        run = _required_id(run_id, "run_id", maximum=256)
        kind_value = _choice(kind, "kind", _LEASE_KINDS)
        executor = _required_id(executor_feed_id, "executor_feed_id", maximum=128)
        state_value = _choice(state, "state", _LEASE_STATES)
        event = hash_value(event_id, "event_id")
        recorded = bounded_integer(recorded_at, "recorded_at")
        _lock(transaction, owner, mesh, run)
        existing = transaction.fetch_one(_LEASE_BY_EVENT_SQL, (owner, mesh, event))
        if existing is not None:
            record = _lease(existing)
            if (record.run_id, record.kind, record.executor_feed_id, record.state) != (
                run,
                kind_value,
                executor,
                state_value,
            ):
                raise RepositoryConflictError(
                    "lease event identity was reused with different semantics",
                    metadata={"operation": "run.lease"},
                )
            return record
        latest_row = transaction.fetch_one(_LEASE_LATEST_SQL, (owner, mesh, run))
        latest = None if latest_row is None else _lease(latest_row)
        self._require_lease_step(latest, kind_value, executor)
        epoch = 1 if latest is None else latest.epoch + 1
        row = returned_row(
            transaction.execute(
                _LEASE_INSERT_SQL,
                (run, epoch, kind_value, executor, state_value, event, recorded, owner, mesh),
            ),
            "run.lease",
        )
        if row is None:
            raise RepositoryNotFoundError("mesh was not found", metadata={"operation": "run.lease"})
        return _lease(row)

    @staticmethod
    def _require_lease_step(latest: RunLeaseRecord | None, kind: str, executor: str) -> None:
        if latest is None:
            if kind != "take":
                raise RepositoryConflictError(
                    "a run starts with its executor taking the lease",
                    metadata={"reason": "lease_not_taken"},
                )
            return
        if latest.state in _TERMINAL_LEASE:
            raise RepositoryConflictError("run is finished", metadata={"reason": "run_finished"})
        if kind == "take":
            raise RepositoryConflictError(
                "run already has an executor", metadata={"reason": "lease_held"}
            )
        if kind == "state" and executor != latest.executor_feed_id:
            raise RepositoryConflictError(
                "only the executor changes the run state",
                metadata={"reason": "not_the_executor"},
            )
        if kind in {"transfer", "takeover"} and executor == latest.executor_feed_id:
            raise RepositoryConflictError(
                "a handoff must name a different executor",
                metadata={"reason": "same_executor"},
            )

    def get_lease(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, run_id: str
    ) -> RunLeaseRecord | None:
        row = query.fetch_one(
            _LEASE_LATEST_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                _required_id(run_id, "run_id", maximum=256),
            ),
        )
        return None if row is None else _lease(row)

    def list_lease_history(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, run_id: str, limit: int = 200
    ) -> tuple[RunLeaseRecord, ...]:
        rows = query.fetch_all(
            _LEASE_HISTORY_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                _required_id(run_id, "run_id", maximum=256),
                _bounded_limit(limit, maximum=_MAX_PAGE),
            ),
        )
        return tuple(_lease(row) for row in rows)

    def list_executor_runs(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        mesh_id: str,
        executor_feed_id: str,
        limit: int = 200,
    ) -> tuple[RunLeaseRecord, ...]:
        rows = query.fetch_all(
            dialect_of(query).choose(postgres=_EXECUTOR_RUNS_SQL, sqlite=_EXECUTOR_RUNS_SQLITE_SQL),
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                _required_id(executor_feed_id, "executor_feed_id", maximum=128),
                _bounded_limit(limit, maximum=_MAX_PAGE),
            ),
        )
        return tuple(_lease(row) for row in rows)

    def record_goal_transition(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        run_id: str,
        goal: str,
        state: str,
        rounds: int,
        stalls: int,
        event_id: str,
        recorded_at: int,
    ) -> RunGoalRecord:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        run = _required_id(run_id, "run_id", maximum=256)
        goal_text = _bounded_text(goal, "goal", maximum=8192)
        state_value = _choice(state, "state", _GOAL_STATES)
        round_count = bounded_integer(rounds, "rounds")
        stall_count = bounded_integer(stalls, "stalls")
        event = hash_value(event_id, "event_id")
        recorded = bounded_integer(recorded_at, "recorded_at")
        _lock(transaction, owner, mesh, run)
        existing = transaction.fetch_one(_GOAL_BY_EVENT_SQL, (owner, mesh, event))
        if existing is not None:
            record = _goal(existing)
            if (record.run_id, record.goal, record.state, record.rounds, record.stalls) != (
                run,
                goal_text,
                state_value,
                round_count,
                stall_count,
            ):
                raise RepositoryConflictError(
                    "goal event identity was reused with different semantics",
                    metadata={"operation": "run.goal"},
                )
            return record
        latest_row = transaction.fetch_one(_GOAL_LATEST_SQL, (owner, mesh, run))
        latest = None if latest_row is None else _goal(latest_row)
        if latest is None:
            if state_value != "active":
                raise RepositoryConflictError(
                    "a goal starts active", metadata={"reason": "goal_not_started"}
                )
        else:
            if latest.state in _TERMINAL_GOAL:
                raise RepositoryConflictError(
                    "goal is finished", metadata={"reason": "goal_finished"}
                )
            if goal_text != latest.goal:
                raise RepositoryConflictError(
                    "a goal cannot change its text", metadata={"reason": "goal_changed"}
                )
            if round_count < latest.rounds:
                raise RepositoryConflictError(
                    "goal rounds cannot decrease", metadata={"reason": "rounds_decreased"}
                )
        revision = 1 if latest is None else latest.revision + 1
        row = returned_row(
            transaction.execute(
                _GOAL_INSERT_SQL,
                (
                    run,
                    revision,
                    goal_text,
                    state_value,
                    round_count,
                    stall_count,
                    event,
                    recorded,
                    owner,
                    mesh,
                ),
            ),
            "run.goal",
        )
        if row is None:
            raise RepositoryNotFoundError("mesh was not found", metadata={"operation": "run.goal"})
        return _goal(row)

    def get_goal(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, run_id: str
    ) -> RunGoalRecord | None:
        row = query.fetch_one(
            _GOAL_LATEST_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                _required_id(run_id, "run_id", maximum=256),
            ),
        )
        return None if row is None else _goal(row)

    def list_goal_history(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, run_id: str, limit: int = 200
    ) -> tuple[RunGoalRecord, ...]:
        rows = query.fetch_all(
            _GOAL_HISTORY_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                _required_id(run_id, "run_id", maximum=256),
                _bounded_limit(limit, maximum=_MAX_PAGE),
            ),
        )
        return tuple(_goal(row) for row in rows)


__all__ = ("RunGoalRecord", "RunLeaseRecord", "RunRepository")
