"""Tests for src/astralplane/reconciliation_store.py: the PostgreSQL marker-store
coordinator's session-locked lifecycle, driver-row validation, and unlock/commit
failure handling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

import astralplane.reconciliation_store as store_module
from astralplane.contracts import (
    ReconciliationCoordinator,
    ReconciliationHookIdentity,
    ReconciliationMarkerState,
    ReconciliationSession,
)
from astralplane.database.pool import ConnectionPool
from astralplane.errors import ReconciliationError, SchemaRevisionError
from astralplane.reconciliation import RECONCILIATION_ADVISORY_LOCK, ReconciliationRunner
from astralplane.reconciliation_store import (
    PostgresReconciliationCoordinator,
    _marker_from_row,
)


def test_failed_marker_retry_sql_uses_the_same_bounded_identity_regex() -> None:
    assert "{0,127}" in store_module._MARK_STARTED_SQL
    assert "{0, 127}" not in store_module._MARK_STARTED_SQL

REVISION = "067.001"
PLAN_DIGEST = "a" * 64
RESULT_DIGEST = "b" * 64
HOOK = ReconciliationHookIdentity("deep-seeds", "1.0.0")
MarkerKey = tuple[str, str, str, str]


def _row(
    *,
    state: str = "started",
    attempt: object = 1,
    result_digest: object = None,
    error_type: object = None,
) -> dict[str, object]:
    return {
        "schema_revision": REVISION,
        "plan_digest": PLAN_DIGEST,
        "hook_name": HOOK.name,
        "hook_version": HOOK.version,
        "state": state,
        "attempt": attempt,
        "result_digest": result_digest,
        "error_type": error_type,
    }


@dataclass
class MemoryPostgres:
    markers: dict[MarkerKey, dict[str, object]] = field(default_factory=dict)
    lock_acquisitions: int = 0
    lock_releases: int = 0


class Cursor:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.description: object = None
        self._result: object = None
        self.closed = False

    @staticmethod
    def _description(names: tuple[str, ...]) -> tuple[tuple[str], ...]:
        return tuple((name,) for name in names)

    def _marker_result(self, marker: dict[str, object] | None) -> None:
        names = tuple(_row())
        self.description = self._description(names)
        if marker is None:
            self._result = None
        elif self.connection.tuple_rows:
            self._result = tuple(marker[name] for name in names)
        else:
            self._result = dict(marker)

    def execute(self, statement: str, parameters: object | None = None) -> None:
        self.connection.statements.append((statement, parameters))
        if self.connection.next_execute_error is not None:
            error = self.connection.next_execute_error
            self.connection.next_execute_error = None
            raise error
        exact = tuple(parameters or ())  # type: ignore[arg-type]
        if "pg_advisory_lock" in statement and "unlock" not in statement:
            self.connection.database.lock_acquisitions += 1
            self.connection.has_lock = True
            self.description = self._description(("pg_advisory_lock",))
            self._result = None if self.connection.drop_lock_row else (None,)
            return
        if "pg_advisory_unlock" in statement:
            unlocked = self.connection.has_lock and not self.connection.refuse_unlock
            if unlocked:
                self.connection.has_lock = False
                self.connection.database.lock_releases += 1
            self.description = self._description(("unlocked",))
            self._result = (
                {"unlocked": unlocked} if self.connection.mapping_unlock_row else (unlocked,)
            )
            return
        if statement.startswith("SELECT schema_revision"):
            key = exact  # type: ignore[assignment]
            marker = self.connection.read_override
            if marker is None:
                marker = self.connection.database.markers.get(key)  # type: ignore[arg-type]
            self._marker_result(marker)
            return
        if statement.startswith("INSERT INTO astralplane_reconciliation_marker"):
            key = exact  # type: ignore[assignment]
            existing = self.connection.database.markers.get(key)  # type: ignore[arg-type]
            if existing is not None:
                valid_started = (
                    existing["state"] == "started"
                    and existing["result_digest"] is None
                    and existing["error_type"] is None
                )
                valid_failed = (
                    existing["state"] == "failed"
                    and existing["result_digest"] is None
                    and isinstance(existing["error_type"], str)
                    and existing["error_type"]
                    .replace(".", "")
                    .replace("_", "")
                    .replace("-", "")
                    .isalnum()
                )
                if not (valid_started or valid_failed):
                    self._marker_result(None)
                    return
            marker = _row(
                state="started",
                attempt=1 if existing is None else int(existing["attempt"]) + 1,
            )
            marker.update(
                {
                    "schema_revision": key[0],
                    "plan_digest": key[1],
                    "hook_name": key[2],
                    "hook_version": key[3],
                }
            )
            self.connection.pending = (key, marker)  # type: ignore[assignment]
            self._marker_result(marker)
            return
        if statement.startswith("UPDATE astralplane_reconciliation_marker"):
            value, *key_parts = exact
            key = tuple(key_parts)  # type: ignore[assignment]
            existing = self.connection.database.markers.get(key)  # type: ignore[arg-type]
            valid = (
                existing is not None
                and existing["state"] == "started"
                and existing["result_digest"] is None
                and existing["error_type"] is None
            )
            if not valid:
                self._marker_result(None)
                return
            if "state = 'completed'" in statement:
                marker = {**existing, "state": "completed", "result_digest": value}
            else:
                marker = {**existing, "state": "failed", "error_type": value}
            self.connection.pending = (key, marker)  # type: ignore[assignment]
            self._marker_result(marker)
            return
        raise AssertionError(f"unexpected SQL: {statement}")

    def fetchone(self) -> object:
        return self._result

    def close(self) -> None:
        self.closed = True


class Connection:
    def __init__(self, database: MemoryPostgres) -> None:
        self.database = database
        self.pending: tuple[MarkerKey, dict[str, object]] | None = None
        self.statements: list[tuple[str, object | None]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.has_lock = False
        self.tuple_rows = False
        self.mapping_unlock_row = False
        self.refuse_unlock = False
        self.drop_lock_row = False
        self.next_commit_error: BaseException | None = None
        self.next_rollback_error: BaseException | None = None
        self.next_execute_error: BaseException | None = None
        self.read_override: dict[str, object] | None = None

    def cursor(self) -> Cursor:
        if self.closed:
            raise RuntimeError("connection is closed")
        return Cursor(self)

    def commit(self) -> None:
        if self.closed:
            raise RuntimeError("connection is closed")
        if self.next_commit_error is not None:
            error = self.next_commit_error
            self.next_commit_error = None
            raise error
        if self.pending is not None:
            key, marker = self.pending
            self.database.markers[key] = marker
            self.pending = None
        self.commits += 1

    def rollback(self) -> None:
        if self.closed:
            raise RuntimeError("connection is closed")
        if self.next_rollback_error is not None:
            error = self.next_rollback_error
            self.next_rollback_error = None
            raise error
        self.pending = None
        self.rollbacks += 1

    def close(self) -> None:
        if self.has_lock:
            self.has_lock = False
            self.database.lock_releases += 1
        self.closed = True


class DriverPool:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.returned: list[tuple[Connection, bool]] = []

    def getconn(self) -> Connection:
        return self.connection

    def putconn(self, connection: Connection, *, close: bool = False) -> None:
        self.returned.append((connection, close))

    def closeall(self) -> None:
        pass


def _coordinator() -> tuple[PostgresReconciliationCoordinator, Connection, DriverPool]:
    connection = Connection(MemoryPostgres())
    driver = DriverPool(connection)
    coordinator = PostgresReconciliationCoordinator(ConnectionPool(driver))
    return coordinator, connection, driver


class Hook:
    name = HOOK.name
    version = HOOK.version

    def __init__(self) -> None:
        self.calls = 0

    def reconcile(self, context: Mapping[str, object]) -> Mapping[str, object]:
        self.calls += 1
        return {"owner": context["owner"]}


def test_marker_lifecycle_commits_each_transition_under_exact_session_lock() -> None:
    coordinator, connection, driver = _coordinator()
    assert isinstance(coordinator, ReconciliationCoordinator)

    with coordinator.coordinate(
        advisory_lock=RECONCILIATION_ADVISORY_LOCK,
        schema_revision=REVISION,
        plan_digest=PLAN_DIGEST,
    ) as session:
        assert isinstance(session, ReconciliationSession)
        assert session.get_marker(HOOK) is None
        started = session.mark_started(HOOK)
        assert started.state is ReconciliationMarkerState.STARTED
        assert started.attempt == 1
        assert next(iter(connection.database.markers.values()))["state"] == "started"
        completed = session.mark_completed(HOOK, result_digest=RESULT_DIGEST)
        assert completed.state is ReconciliationMarkerState.COMPLETED
        assert completed.attempt == started.attempt
        assert completed.result_digest == RESULT_DIGEST
        assert session.get_marker(HOOK) == completed

    assert connection.database.lock_acquisitions == 1
    assert connection.database.lock_releases == 1
    assert connection.commits == 4
    assert driver.returned == [(connection, False)]
    with pytest.raises(ReconciliationError, match="no longer active"):
        session.get_marker(HOOK)


def test_concrete_coordinator_runs_and_replays_the_public_runner_contract() -> None:
    coordinator, connection, _ = _coordinator()
    hook = Hook()
    runner = ReconciliationRunner(coordinator, (hook,))

    first = runner.run(schema_revision=REVISION, context={"owner": "system"})
    second = runner.run(schema_revision=REVISION, context={"owner": "system"})

    assert first.durably_complete
    assert not first.hooks[0].already_complete
    assert second.hooks[0].already_complete
    assert hook.calls == 1
    assert connection.database.lock_acquisitions == 2
    assert connection.database.lock_releases == 2


def test_failed_attempt_is_durable_and_retry_increments_monotonically() -> None:
    coordinator, connection, _ = _coordinator()

    with coordinator.coordinate(
        advisory_lock=RECONCILIATION_ADVISORY_LOCK,
        schema_revision=REVISION,
        plan_digest=PLAN_DIGEST,
    ) as session:
        first = session.mark_started(HOOK)
        failed = session.mark_failed(HOOK, error_type="RuntimeError")
        assert failed.attempt == first.attempt
        assert failed.error_type == "RuntimeError"
        second = session.mark_started(HOOK)
        assert second.attempt == 2
        assert second.result_digest is None
        assert second.error_type is None

    assert next(iter(connection.database.markers.values()))["attempt"] == 2


def test_completed_or_absent_attempts_cannot_be_rebound() -> None:
    coordinator, _, _ = _coordinator()

    with coordinator.coordinate(
        advisory_lock=RECONCILIATION_ADVISORY_LOCK,
        schema_revision=REVISION,
        plan_digest=PLAN_DIGEST,
    ) as session:
        with pytest.raises(ReconciliationError, match="transition to completed"):
            session.mark_completed(HOOK, result_digest=RESULT_DIGEST)
        with pytest.raises(ReconciliationError, match="transition to failed"):
            session.mark_failed(HOOK, error_type="RuntimeError")
        session.mark_started(HOOK)
        session.mark_completed(HOOK, result_digest=RESULT_DIGEST)
        with pytest.raises(ReconciliationError, match="transition to started"):
            session.mark_started(HOOK)
        with pytest.raises(ReconciliationError, match="transition to failed"):
            session.mark_failed(HOOK, error_type="LateFailure")


def test_tuple_driver_rows_are_detached_and_validated() -> None:
    coordinator, connection, _ = _coordinator()
    connection.tuple_rows = True

    with coordinator.coordinate(
        advisory_lock=RECONCILIATION_ADVISORY_LOCK,
        schema_revision=REVISION,
        plan_digest=PLAN_DIGEST,
    ) as session:
        assert session.mark_started(HOOK).hook == HOOK
        assert session.get_marker(HOOK).attempt == 1  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ({**_row(), "extra": "value"}, "columns"),
        ({**_row(), "schema_revision": 4}, "schema revision"),
        ({**_row(), "schema_revision": "bad"}, "revision"),
        ({**_row(), "plan_digest": "bad"}, "plan digest"),
        ({**_row(), "hook_name": None}, "hook identity"),
        ({**_row(), "state": "unknown"}, "state"),
        ({**_row(), "attempt": True}, "attempt"),
        ({**_row(), "result_digest": 4}, "result digest"),
        ({**_row(), "error_type": 4}, "error type"),
        ({**_row(), "result_digest": RESULT_DIGEST}, "started"),
        ({**_row(state="completed")}, "completed"),
        (
            _row(state="completed", result_digest=RESULT_DIGEST, error_type="Wrong"),
            "carries a failure",
        ),
        ({**_row(state="failed")}, "failed"),
        (_row(state="failed", error_type="bad/type"), "failed"),
    ],
)
def test_malformed_durable_marker_rows_fail_closed(marker: dict[str, object], message: str) -> None:
    with pytest.raises((ReconciliationError, SchemaRevisionError), match=message):
        _marker_from_row(marker, None)


@pytest.mark.parametrize(
    ("row", "description", "message"),
    [
        (object(), None, "non-record"),
        ((REVISION,), (("schema_revision",), ("plan_digest",)), "width"),
        ((REVISION,), (("schema_revision",),), "columns"),
    ],
)
def test_non_mapping_driver_rows_require_exact_description(
    row: object, description: object, message: str
) -> None:
    with pytest.raises(ReconciliationError, match=message):
        _marker_from_row(row, description)


@pytest.mark.parametrize(
    ("method", "value", "message"),
    [
        ("start", object(), "hook identity"),
        ("start", ReconciliationHookIdentity("bad/name", "1"), "bounded"),
        ("complete", "not-a-digest", "lowercase SHA-256"),
        ("fail", "bad/type", "bounded identifier"),
    ],
)
def test_marker_inputs_are_validated_before_sql(method: str, value: object, message: str) -> None:
    coordinator, _, _ = _coordinator()
    with (
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ) as session,
        pytest.raises(ReconciliationError, match=message),
    ):
        if method == "start":
            session.mark_started(value)  # type: ignore[arg-type]
        elif method == "complete":
            session.mark_completed(HOOK, result_digest=value)  # type: ignore[arg-type]
        else:
            session.mark_failed(HOOK, error_type=value)  # type: ignore[arg-type]


def test_corrupt_read_identity_is_rejected_and_transaction_is_rolled_back() -> None:
    coordinator, connection, _ = _coordinator()
    connection.read_override = {**_row(), "plan_digest": "c" * 64}

    with (
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ) as session,
        pytest.raises(ReconciliationError, match="exact identity"),
    ):
        session.get_marker(HOOK)

    assert connection.rollbacks >= 2


def test_marker_commit_failure_is_visible_and_discards_uncertain_connection() -> None:
    coordinator, connection, driver = _coordinator()

    with (
        pytest.raises(ReconciliationError, match="committed durably"),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ) as session,
    ):
        connection.next_commit_error = RuntimeError("commit failed")
        session.mark_started(HOOK)

    assert connection.closed
    assert connection.database.markers == {}
    assert driver.returned == [(connection, True)]


@pytest.mark.parametrize("termination", [KeyboardInterrupt(), SystemExit(11)])
def test_marker_commit_interruption_is_outcome_uncertain_and_discards_connection(
    termination: BaseException,
) -> None:
    coordinator, connection, driver = _coordinator()

    with (
        pytest.raises(type(termination)),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ) as session,
    ):
        connection.next_commit_error = termination
        session.mark_started(HOOK)

    assert connection.closed
    assert driver.returned == [(connection, True)]


def test_read_transaction_reset_failure_discards_connection() -> None:
    coordinator, connection, driver = _coordinator()

    with (
        pytest.raises(ReconciliationError, match="read could not be closed"),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ) as session,
    ):
        connection.next_rollback_error = RuntimeError("rollback failed")
        session.get_marker(HOOK)

    assert connection.closed
    assert driver.returned == [(connection, True)]


@pytest.mark.parametrize("termination", [KeyboardInterrupt(), SystemExit(9)])
def test_marker_driver_interruptions_propagate_and_still_release_lock(
    termination: BaseException,
) -> None:
    coordinator, connection, _ = _coordinator()

    with (
        pytest.raises(type(termination)),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ) as session,
    ):
        connection.next_execute_error = termination
        session.mark_started(HOOK)

    assert connection.database.lock_releases == 1


def test_body_failure_is_preserved_after_successful_unlock() -> None:
    coordinator, connection, _ = _coordinator()
    connection.mapping_unlock_row = True

    with (
        pytest.raises(LookupError, match="hook failed"),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ),
    ):
        raise LookupError("hook failed")

    assert connection.database.lock_releases == 1


def test_unlock_refusal_is_visible_and_connection_is_discarded() -> None:
    coordinator, connection, driver = _coordinator()
    connection.refuse_unlock = True

    with (
        pytest.raises(ReconciliationError, match="did not release"),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ),
    ):
        pass

    assert connection.closed
    assert driver.returned == [(connection, True)]


def test_unlock_failure_does_not_mask_active_body_failure() -> None:
    coordinator, connection, driver = _coordinator()
    connection.refuse_unlock = True

    with (
        pytest.raises(LookupError, match="original"),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ),
    ):
        raise LookupError("original")

    assert connection.closed
    assert driver.returned == [(connection, True)]


def test_missing_lock_row_and_driver_failure_fail_closed() -> None:
    coordinator, connection, driver = _coordinator()
    connection.drop_lock_row = True
    with (
        pytest.raises(ReconciliationError, match="confirm"),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ),
    ):
        pass
    assert connection.closed
    assert driver.returned == [(connection, True)]

    coordinator, connection, _ = _coordinator()
    connection.next_execute_error = RuntimeError("driver unavailable")
    with (
        pytest.raises(ReconciliationError, match="acquisition failed"),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ),
    ):
        pass


def test_session_construction_interruption_still_releases_acquired_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, connection, _ = _coordinator()

    def interrupted_session(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(store_module, "PostgresReconciliationSession", interrupted_session)
    with (
        pytest.raises(KeyboardInterrupt),
        coordinator.coordinate(
            advisory_lock=RECONCILIATION_ADVISORY_LOCK,
            schema_revision=REVISION,
            plan_digest=PLAN_DIGEST,
        ),
    ):
        pass

    assert connection.database.lock_releases == 1


@pytest.mark.parametrize(
    ("lock", "revision", "digest", "message"),
    [
        ((1, 2), REVISION, PLAN_DIGEST, "canonical advisory lock"),
        (RECONCILIATION_ADVISORY_LOCK, "bad", PLAN_DIGEST, "revision"),
        (RECONCILIATION_ADVISORY_LOCK, REVISION, "BAD", "lowercase SHA-256"),
    ],
)
def test_coordinate_rejects_noncanonical_scope(
    lock: tuple[int, int], revision: str, digest: str, message: str
) -> None:
    coordinator, connection, _ = _coordinator()
    with (
        pytest.raises((ReconciliationError, SchemaRevisionError), match=message),
        coordinator.coordinate(
            advisory_lock=lock,
            schema_revision=revision,
            plan_digest=digest,
        ),
    ):
        pass
    assert connection.database.lock_acquisitions == 0


def test_constructor_requires_the_kernel_connection_pool() -> None:
    with pytest.raises(TypeError, match="ConnectionPool"):
        PostgresReconciliationCoordinator(object())  # type: ignore[arg-type]
