"""Tests for src/astralplane/database/pool.py and transaction.py: connection
checkout/reset/release failure handling, commit/rollback ownership, and savepoint
recovery.
"""

from __future__ import annotations

import threading
import time

import pytest

from astralplane.contracts import IsolationLevel
from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import (
    ConnectionResetError,
    InitializationError,
    PlaneError,
    PoolCheckoutError,
    PoolClosedError,
    PoolInUseError,
    PoolReleaseError,
    PoolShutdownError,
    TransactionCleanupError,
    TransactionCommitError,
    TransactionCommitOutcomeUnknownError,
    TransactionRollbackError,
    TransactionStateError,
    TransactionStatementError,
)


class Cursor:
    description = None
    rowcount = 0
    statusmessage = "OK"

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.closed = False

    def execute(self, statement: str, parameters: object | None = None) -> None:
        self.connection.statements.append((statement, parameters))
        if statement == "INTERRUPT":
            self.connection.closed = 2
            raise RuntimeError("connection interrupted")
        if statement == "BROKEN" or (statement.startswith("BEGIN") and self.connection.fail_begin):
            raise ValueError("broken statement")
        if statement.startswith("SAVEPOINT") and self.connection.fail_savepoint:
            raise RuntimeError("savepoint start failed")
        if statement.startswith("RELEASE SAVEPOINT") and self.connection.fail_release_savepoint:
            raise RuntimeError("savepoint release failed")
        if (
            statement.startswith("ROLLBACK TO SAVEPOINT")
            and self.connection.fail_rollback_savepoint
        ):
            raise RuntimeError("savepoint rollback failed")

    def close(self) -> None:
        self.closed = True


class Connection:
    def __init__(self) -> None:
        self._autocommit = True
        self.statements: list[tuple[str, object | None]] = []
        self.commits = 0
        self.rollbacks = 0
        self._closed = 0
        self.fail_state_inspection = False
        self.fail_commit = False
        self.commit_error: BaseException | None = None
        self.fail_rollback = False
        self.fail_autocommit = False
        self.fail_autocommit_on_restore = False
        self.fail_begin = False
        self.fail_savepoint = False
        self.fail_release_savepoint = False
        self.fail_rollback_savepoint = False
        self.autocommit_error: BaseException | None = None

    @property
    def closed(self) -> int:
        if self.fail_state_inspection:
            raise RuntimeError("connection state unavailable")
        return self._closed

    @closed.setter
    def closed(self, value: int) -> None:
        self._closed = value

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        if self.autocommit_error is not None:
            raise self.autocommit_error
        if self.fail_autocommit or (self.fail_autocommit_on_restore and value):
            raise RuntimeError("autocommit failed")
        self._autocommit = value

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error
        if self.fail_commit:
            raise RuntimeError("commit failed")

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError("rollback failed")

    def close(self) -> None:
        self.closed += 1


class DriverPool:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.returned: list[tuple[Connection, bool]] = []
        self.close_calls = 0
        self.fail_release = False
        self.fail_borrow = False
        self.borrow_error: BaseException | None = None
        self.fail_closeall = False
        self.interrupt_closeall = False
        self.close_gate: threading.Event | None = None
        self.close_entered = threading.Event()
        self.max_concurrent_close_calls = 0
        self._open_close_calls = 0
        self._close_accounting_lock = threading.Lock()

    def getconn(self) -> Connection:
        if self.borrow_error is not None:
            raise self.borrow_error
        if self.fail_borrow:
            raise RuntimeError("borrow failed")
        return self.connection

    def putconn(self, connection: Connection, *, close: bool = False) -> None:
        if self.fail_release:
            raise RuntimeError("release failed")
        self.returned.append((connection, close))

    def closeall(self) -> None:
        with self._close_accounting_lock:
            self._open_close_calls += 1
            self.close_calls += 1
            self.max_concurrent_close_calls = max(
                self.max_concurrent_close_calls, self._open_close_calls
            )
        try:
            self.close_entered.set()
            if self.interrupt_closeall:
                raise KeyboardInterrupt
            if self.fail_closeall:
                raise RuntimeError("closeall failed")
            if self.close_gate is not None and not self.close_gate.wait(timeout=10.0):
                raise AssertionError("close gate was never released")
        finally:
            with self._close_accounting_lock:
                self._open_close_calls -= 1


def test_connection_scope_rolls_back_implicit_work_before_return() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    pool = ConnectionPool(driver)

    with pool.connection() as borrowed:
        assert borrowed is connection
        assert pool.snapshot.borrowed == 1

    assert connection.rollbacks == 1
    assert driver.returned == [(connection, False)]
    assert pool.snapshot.borrowed == 0


def test_failed_driver_checkout_releases_the_reserved_borrow_slot() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.fail_borrow = True
    pool = ConnectionPool(driver)

    with pytest.raises(PoolCheckoutError) as error, pool.connection():
        pass

    assert error.value.code == "pool_checkout_failed"
    assert error.value.metadata == ()
    assert isinstance(error.value.__cause__, RuntimeError)
    assert "borrow failed" not in str(error.value)
    assert pool.snapshot.borrowed == 0

    driver.fail_borrow = False
    with pool.connection() as borrowed:
        assert borrowed is connection
    assert pool.snapshot.borrowed == 0


def test_pool_close_is_idempotent_and_refuses_active_borrowers() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    pool = ConnectionPool(driver)

    with pool.connection():
        with pytest.raises(PoolInUseError) as error:
            pool.close()
        assert error.value.metadata == (("borrowed", "1"),)

    pool.close()
    pool.close()
    assert driver.close_calls == 1
    assert pool.snapshot.closed
    with pytest.raises(PoolClosedError), pool.connection():
        pass


def test_failed_close_keeps_admission_closed_until_an_explicit_close_succeeds() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.fail_closeall = True
    pool = ConnectionPool(driver)

    with pytest.raises(PoolShutdownError):
        pool.close()
    assert driver.close_calls == 1
    assert pool.snapshot.closed
    assert not pool.snapshot.cleanup_complete
    assert pool.snapshot.cleanup_failed
    assert pool.snapshot.cleanup_attempts == 1
    with pytest.raises(PoolClosedError), pool.connection():
        pass

    driver.fail_closeall = False
    pool.close()
    assert driver.close_calls == 2
    snapshot = pool.snapshot
    assert snapshot.closed
    assert snapshot.cleanup_complete
    assert not snapshot.cleanup_failed
    assert snapshot.cleanup_attempts == 2

    pool.close()
    assert driver.close_calls == 2
    with pytest.raises(PoolClosedError), pool.connection():
        pass


def test_failed_close_raises_a_typed_non_sensitive_shutdown_failure() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.fail_closeall = True
    pool = ConnectionPool(driver)

    with pytest.raises(PoolShutdownError) as error:
        pool.close()

    assert error.value.code == "pool_shutdown_failed"
    assert error.value.metadata == (("attempts", "1"),)
    assert "closeall failed" not in str(error.value)
    assert isinstance(error.value.__cause__, RuntimeError)


def test_base_exception_interruptions_record_failed_cleanup_without_masking() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.interrupt_closeall = True
    pool = ConnectionPool(driver)

    with pytest.raises(KeyboardInterrupt):
        pool.close()
    assert pool.snapshot.closed
    assert pool.snapshot.cleanup_failed
    assert not pool.snapshot.cleanup_complete

    driver.interrupt_closeall = False
    pool.close()
    assert pool.snapshot.cleanup_complete
    assert driver.close_calls == 2


def test_borrowed_refusal_survives_a_failed_cleanup_attempt() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.fail_closeall = True
    pool = ConnectionPool(driver)

    with pool.connection():
        with pytest.raises(PoolInUseError) as error:
            pool.close()
        assert error.value.metadata == (("borrowed", "1"),)
        assert not pool.snapshot.closed

    with pytest.raises(PoolShutdownError):
        pool.close()
    assert pool.snapshot.cleanup_failed
    with pytest.raises(PoolClosedError), pool.connection():
        pass

    driver.fail_closeall = False
    pool.close()
    assert pool.snapshot.cleanup_complete


def test_concurrent_close_callers_share_one_cleanup_and_never_report_early_success() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.close_gate = threading.Event()
    pool = ConnectionPool(driver)
    outcomes: list[BaseException | None] = []

    def close() -> None:
        try:
            pool.close()
            outcomes.append(None)
        except BaseException as exc:
            outcomes.append(exc)

    first = threading.Thread(target=close)
    first.start()
    assert driver.close_entered.wait(timeout=10.0)
    second = threading.Thread(target=close)
    second.start()
    deadline = time.monotonic() + 10.0
    while not second.is_alive() and time.monotonic() < deadline:
        second.join(timeout=0.05)
    assert second.is_alive()
    second.join(timeout=0.5)
    assert second.is_alive(), "concurrent close finished while cleanup was still in flight"
    assert driver.close_calls == 1

    driver.close_gate.set()
    first.join(timeout=10.0)
    second.join(timeout=10.0)
    assert not first.is_alive() and not second.is_alive()
    assert len(outcomes) == 2 and all(outcome is None for outcome in outcomes)
    assert driver.close_calls == 1
    assert driver.max_concurrent_close_calls == 1
    assert pool.snapshot.cleanup_complete
    assert pool.snapshot.cleanup_attempts == 1


def test_concurrent_close_callers_serialize_failed_attempts_and_each_reports_the_truth() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.fail_closeall = True
    pool = ConnectionPool(driver)
    failures: list[PoolShutdownError] = []

    def close() -> None:
        try:
            pool.close()
        except PoolShutdownError as exc:
            failures.append(exc)

    closers = [threading.Thread(target=close) for _ in range(3)]
    for closer in closers:
        closer.start()
    for closer in closers:
        closer.join(timeout=10.0)
        assert not closer.is_alive()

    assert len(failures) == 3
    assert {failure.metadata for failure in failures} == {
        (("attempts", "1"),),
        (("attempts", "2"),),
        (("attempts", "3"),),
    }
    assert driver.max_concurrent_close_calls == 1
    assert driver.close_calls == 3
    assert pool.snapshot.closed
    assert pool.snapshot.cleanup_failed
    assert not pool.snapshot.cleanup_complete
    assert pool.snapshot.cleanup_attempts == 3

    driver.fail_closeall = False
    pool.close()
    assert pool.snapshot.cleanup_complete
    assert driver.close_calls == 4


def test_reset_failure_discards_connection_and_is_visible() -> None:
    connection = Connection()
    connection.fail_rollback = True
    driver = DriverPool(connection)
    pool = ConnectionPool(driver)

    with pytest.raises(ConnectionResetError), pool.connection():
        pass

    assert driver.returned == [(connection, True)]


def test_pool_release_failure_closes_connection_and_is_visible() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.fail_release = True
    pool = ConnectionPool(driver)

    with pytest.raises(PoolReleaseError), pool.connection():
        pass

    assert connection.closed == 1
    assert pool.snapshot.borrowed == 0


def test_body_failure_is_not_masked_by_reset_or_release_failures() -> None:
    connection = Connection()
    connection.fail_rollback = True
    driver = DriverPool(connection)
    driver.fail_release = True
    pool = ConnectionPool(driver)

    with pytest.raises(LookupError, match="caller failure"), pool.connection():
        raise LookupError("caller failure")

    assert connection.closed == 1


@pytest.mark.parametrize(
    "failure",
    [InitializationError("checkout deadline", code="pool_acquire_timeout"), KeyboardInterrupt()],
)
def test_checkout_preserves_typed_and_base_exceptions_while_releasing_the_slot(
    failure: BaseException,
) -> None:
    connection = Connection()
    driver = DriverPool(connection)
    driver.borrow_error = failure
    pool = ConnectionPool(driver)

    with pytest.raises(type(failure)) as error, pool.connection():
        pytest.fail("a failed checkout must not yield a connection")

    assert error.value is failure
    assert pool.snapshot.borrowed == 0


def test_transaction_state_rejects_double_begin_and_finish_after_close() -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with database.transaction() as transaction:
        assert transaction.active
        assert not transaction.discard_required
        with pytest.raises(TransactionStateError, match="already active"):
            transaction._begin()

    assert not transaction.active
    with pytest.raises(TransactionStateError, match="not active"):
        transaction._finish(failed=False)


def test_plane_error_statement_failure_is_not_rewrapped() -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with (
        pytest.raises(PlaneError, match="statement must be a string"),
        database.transaction() as transaction,
    ):
        transaction.execute(7)  # type: ignore[arg-type]


def test_savepoint_start_failure_aborts_without_commit() -> None:
    connection = Connection()
    connection.fail_savepoint = True
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with (
        pytest.raises(RuntimeError, match="savepoint start"),
        database.transaction() as transaction,
        transaction.savepoint("safe"),
    ):
        pytest.fail("an unusable savepoint must not run its body")

    assert connection.commits == 0


def test_autocommit_setup_base_exception_is_preserved_and_discarded() -> None:
    connection = Connection()
    connection.autocommit_error = KeyboardInterrupt()
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with pytest.raises(KeyboardInterrupt), database.transaction():
        pytest.fail("an interrupted setup must not run the body")

    assert driver.returned == [(connection, True)]


def test_autocommit_restore_base_exception_is_preserved_and_discarded() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with pytest.raises(KeyboardInterrupt), database.transaction() as transaction:
        transaction.execute("UPDATE")
        connection.autocommit_error = KeyboardInterrupt()

    assert connection.commits == 1
    assert driver.returned == [(connection, True)]


def test_transaction_commits_only_when_outer_caller_scope_succeeds() -> None:
    connection = Connection()
    pool = ConnectionPool(DriverPool(connection))
    database = PlaneDatabase(pool)

    with database.transaction(isolation=IsolationLevel.SERIALIZABLE) as transaction:
        transaction.execute("UPDATE item SET value = %s", (3,))
        assert connection.autocommit is False

    assert connection.statements == [
        ("BEGIN ISOLATION LEVEL SERIALIZABLE", None),
        ("UPDATE item SET value = %s", (3,)),
    ]
    assert connection.commits == 1
    assert connection.autocommit is True


def test_transaction_rolls_back_and_preserves_body_exception() -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with pytest.raises(KeyError, match="caller"), database.transaction() as transaction:
        transaction.execute("UPDATE")
        raise KeyError("caller")

    assert connection.commits == 0
    assert connection.rollbacks >= 1


def test_handled_driver_failure_cannot_commit_fake_success() -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with (
        pytest.raises(TransactionStateError, match="rolled back"),
        database.transaction() as transaction,
    ):
        with pytest.raises(ValueError, match="broken"):
            transaction.execute("BROKEN")
        with pytest.raises(TransactionStateError, match="aborted"):
            transaction.execute("UPDATE")

    assert connection.commits == 0


def test_commit_failure_is_typed_and_rolls_back() -> None:
    connection = Connection()
    connection.fail_commit = True
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with (
        pytest.raises(TransactionCommitOutcomeUnknownError) as error,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE")

    assert isinstance(error.value, TransactionCommitError)
    assert error.value.code == "transaction_commit_outcome_unknown"
    assert "failed" not in str(error.value)
    assert isinstance(error.value.__cause__, RuntimeError)
    assert connection.commits == 1
    assert connection.rollbacks >= 1
    assert driver.returned == [(connection, True)]


def test_commit_uncertainty_survives_failed_rollback_release_and_cleanup() -> None:
    connection = Connection()
    connection.fail_commit = True
    connection.fail_rollback = True
    driver = DriverPool(connection)
    driver.fail_release = True
    database = PlaneDatabase(ConnectionPool(driver))

    with (
        pytest.raises(TransactionCommitOutcomeUnknownError) as error,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE")

    assert error.value.__cause__ is not None
    assert connection.commits == 1
    assert connection.closed == 1
    assert driver.returned == []


def test_commit_base_exception_is_preserved_and_connection_is_discarded() -> None:
    connection = Connection()
    connection.commit_error = KeyboardInterrupt()
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with pytest.raises(KeyboardInterrupt), database.transaction() as transaction:
        transaction.execute("UPDATE")

    assert connection.commits == 1
    assert driver.returned == [(connection, True)]


def test_interrupted_statement_is_typed_and_discards_the_connection() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with (
        pytest.raises(TransactionStatementError) as error,
        database.transaction() as transaction,
    ):
        transaction.execute("INTERRUPT")

    assert error.value.code == "transaction_statement_failed"
    assert error.value.metadata == (("operation", "execute"),)
    assert isinstance(error.value.__cause__, RuntimeError)
    assert connection.commits == 0
    assert driver.returned == [(connection, True)]


def test_connection_state_inspection_failure_fails_closed() -> None:
    connection = Connection()
    connection.fail_state_inspection = True
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with (
        pytest.raises(TransactionStatementError) as error,
        database.transaction() as transaction,
    ):
        transaction.execute("BROKEN")

    assert error.value.metadata == (("operation", "execute"),)
    assert isinstance(error.value.__cause__, ValueError)
    assert driver.returned == [(connection, True)]


def test_isolation_begin_failure_with_failed_restore_discards_connection() -> None:
    connection = Connection()
    connection.fail_begin = True
    connection.fail_autocommit_on_restore = True
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with (
        pytest.raises(ValueError, match="broken"),
        database.transaction(isolation=IsolationLevel.REPEATABLE_READ),
    ):
        pytest.fail("a failed begin must not run the body")

    assert driver.returned == [(connection, True)]


def test_ordinary_statement_failure_keeps_the_connection_reusable() -> None:
    connection = Connection()
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with pytest.raises(ValueError, match="broken"), database.transaction() as transaction:
        transaction.execute("BROKEN")

    assert driver.returned == [(connection, False)]
    with database.transaction() as transaction:
        transaction.execute("UPDATE")

    assert connection.commits == 1
    assert driver.returned[-1] == (connection, False)


def test_rollback_failure_marks_the_connection_unsafe_without_masking_body_error() -> None:
    connection = Connection()
    connection.fail_rollback = True
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with pytest.raises(KeyError, match="caller"), database.transaction() as transaction:
        transaction.execute("UPDATE")
        raise KeyError("caller")

    assert connection.commits == 0
    assert driver.returned == [(connection, True)]


def test_autocommit_restore_failure_after_commit_is_typed_and_discards() -> None:
    connection = Connection()
    connection.fail_autocommit_on_restore = True
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with (
        pytest.raises(TransactionCleanupError) as error,
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE")

    assert error.value.code == "transaction_cleanup_failed"
    assert connection.commits == 1
    assert driver.returned == [(connection, True)]


def test_autocommit_setup_failure_is_typed_and_discards_the_connection() -> None:
    connection = Connection()
    connection.fail_autocommit = True
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with pytest.raises(TransactionCleanupError) as error, database.transaction():
        pytest.fail("an unusable connection must not start a transaction")

    assert error.value.code == "transaction_cleanup_failed"
    assert isinstance(error.value.__cause__, RuntimeError)
    assert driver.returned == [(connection, True)]


def test_failed_rollback_after_a_handled_failure_is_typed_and_discarded() -> None:
    connection = Connection()
    connection.fail_rollback = True
    driver = DriverPool(connection)
    database = PlaneDatabase(ConnectionPool(driver))

    with (
        pytest.raises(TransactionRollbackError) as error,
        database.transaction() as transaction,
        pytest.raises(ValueError, match="broken"),
    ):
        transaction.execute("BROKEN")

    assert error.value.code == "transaction_rollback_failed"
    assert isinstance(error.value.__cause__, RuntimeError)
    assert driver.returned == [(connection, True)]


def test_savepoint_recovers_a_statement_failure_without_nested_commit() -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with database.transaction() as transaction:
        with pytest.raises(ValueError, match="broken"), transaction.savepoint("bounded_1"):
            transaction.execute("BROKEN")
        transaction.execute("UPDATE")

    statements = [statement for statement, _ in connection.statements]
    assert statements == [
        'SAVEPOINT "bounded_1"',
        "BROKEN",
        'ROLLBACK TO SAVEPOINT "bounded_1"',
        'RELEASE SAVEPOINT "bounded_1"',
        "UPDATE",
    ]
    assert connection.commits == 1


def test_successful_savepoint_releases_without_taking_commit_ownership() -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with database.transaction() as transaction, transaction.savepoint("safe"):
        transaction.execute("UPDATE")

    assert [statement for statement, _ in connection.statements] == [
        'SAVEPOINT "safe"',
        "UPDATE",
        'RELEASE SAVEPOINT "safe"',
    ]
    assert connection.commits == 1


def test_savepoint_control_failure_aborts_the_outer_transaction() -> None:
    connection = Connection()
    connection.fail_release_savepoint = True
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with (
        pytest.raises(RuntimeError, match="savepoint release"),
        database.transaction() as transaction,
        transaction.savepoint("safe"),
    ):
        transaction.execute("UPDATE")

    assert connection.commits == 0


def test_savepoint_rollback_failure_preserves_original_statement_error() -> None:
    connection = Connection()
    connection.fail_rollback_savepoint = True
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with (
        pytest.raises(ValueError, match="broken"),
        database.transaction() as transaction,
        transaction.savepoint("safe"),
    ):
        transaction.execute("BROKEN")

    assert connection.commits == 0


@pytest.mark.parametrize("name", ["", "has-hyphen", "1starts_wrong", "x" * 64])
def test_savepoint_rejects_unsafe_identifiers(name: str) -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with (
        database.transaction() as transaction,
        pytest.raises(TransactionStateError),
        transaction.savepoint(name),
    ):
        pass


def test_transaction_methods_fail_outside_their_scope() -> None:
    connection = Connection()
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with database.transaction() as transaction:
        pass

    with pytest.raises(TransactionStateError, match="not active"):
        transaction.execute("UPDATE")


def test_isolation_begin_failure_restores_connection_state() -> None:
    connection = Connection()
    connection.fail_begin = True
    database = PlaneDatabase(ConnectionPool(DriverPool(connection)))

    with (
        pytest.raises(ValueError, match="broken"),
        database.transaction(isolation=IsolationLevel.REPEATABLE_READ),
    ):
        pass

    assert connection.autocommit is True
    assert connection.commits == 0


@pytest.mark.parametrize("operation", ["statement", "commit"])
@pytest.mark.parametrize("rollback_fails", [False, True])
@pytest.mark.parametrize("release_fails", [False, True])
def test_discarded_transaction_releases_its_marker_and_preserves_primary_error(
    operation: str, rollback_fails: bool, release_fails: bool
) -> None:
    connection = Connection()
    connection.fail_rollback = rollback_fails
    primary_error = RuntimeError("commit acknowledgement interrupted")
    if operation == "commit":
        connection.commit_error = primary_error
    driver = DriverPool(connection)
    driver.fail_release = release_fails
    pool = ConnectionPool(driver)
    database = PlaneDatabase(pool)
    expected_error = (
        TransactionStatementError if operation == "statement"
        else TransactionCommitOutcomeUnknownError
    )

    with pytest.raises(expected_error) as error, database.transaction() as transaction:
        transaction.execute("INTERRUPT" if operation == "statement" else "UPDATE")

    if operation == "commit":
        assert error.value.__cause__ is primary_error
        assert connection.commits == 1
    else:
        assert isinstance(error.value.__cause__, RuntimeError)
        assert str(error.value.__cause__) == "connection interrupted"
        assert connection.commits == 0
    assert connection.rollbacks == 2
    assert driver.returned == ([] if release_fails else [(connection, True)])
    if release_fails:
        assert connection.closed == (3 if operation == "statement" else 1)
    assert pool.snapshot.borrowed == 0
    assert not pool._discarded
