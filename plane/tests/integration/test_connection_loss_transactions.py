"""Real-PostgreSQL fault tests for database/pool.py and database/transaction.py: a
terminated backend is typed and discarded, while a lost commit acknowledgement remains
an uncertain outcome that is never retried and never reuses the connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import pytest

from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import (
    PoolCheckoutError,
    TransactionCommitOutcomeUnknownError,
    TransactionStatementError,
)
from tests.fixtures.migrated_template import MigratedDatabase


@pytest.fixture
def connection_loss_target(
    migrated_clone: MigratedDatabase,
) -> Iterator[tuple[MigratedDatabase, Any, str]]:
    administrator = psycopg2.connect(migrated_clone.dsn)
    administrator.autocommit = True
    schema = migrated_clone.schema
    with administrator.cursor() as cursor:
        cursor.execute(
            f'CREATE TABLE "{schema}".connection_loss_probe ('
            "id integer PRIMARY KEY, note text NOT NULL)"
        )
    try:
        yield migrated_clone, administrator, schema
    finally:
        administrator.close()


@contextmanager
def _real_database(dsn: str) -> Iterator[PlaneDatabase]:
    driver = create_postgres_driver_pool(
        dsn,
        minimum_connections=1,
        maximum_connections=1,
        acquire_timeout_seconds=3,
        connect_timeout_seconds=3,
        application_name="astralplane:connection-loss-test",
    )
    pool = ConnectionPool(driver)
    try:
        yield PlaneDatabase(pool)
    finally:
        pool.close()


def _terminate(administrator: Any, backend_pid: int) -> None:
    with administrator.cursor() as cursor:
        cursor.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))


def _backend_pid(transaction: Any) -> int:
    row = transaction.fetch_one("SELECT pg_backend_pid() AS pid")
    assert row is not None
    return int(row["pid"])


def _probe_rows(administrator: Any, schema: str) -> list[tuple[int, str]]:
    with administrator.cursor() as cursor:
        cursor.execute(f'SELECT id, note FROM "{schema}".connection_loss_probe ORDER BY id')
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def test_statement_interruption_is_typed_discarded_and_recovers(
    connection_loss_target: tuple[MigratedDatabase, Any, str],
) -> None:
    clone, administrator, schema = connection_loss_target

    with _real_database(clone.dsn) as database:
        with (
            pytest.raises(TransactionStatementError) as error,
            database.transaction() as transaction,
        ):
            _terminate(administrator, _backend_pid(transaction))
            transaction.execute(
                f'INSERT INTO "{schema}".connection_loss_probe VALUES(1, %s)',
                ("interrupted",),
            )

        assert error.value.code == "transaction_statement_failed"
        assert error.value.metadata == (("operation", "execute"),)
        assert isinstance(error.value.__cause__, psycopg2.Error)
        assert "server closed" not in str(error.value)
        assert database._pool.snapshot.borrowed == 0
        assert not database._pool._discarded

        with database.transaction() as transaction:
            transaction.execute(
                f'INSERT INTO "{schema}".connection_loss_probe VALUES(2, %s)',
                ("healthy",),
            )

    assert _probe_rows(administrator, schema) == [(2, "healthy")]


def test_commit_interruption_is_unknown_discarded_and_recovers(
    connection_loss_target: tuple[MigratedDatabase, Any, str],
) -> None:
    clone, administrator, schema = connection_loss_target

    with _real_database(clone.dsn) as database:
        with (
            pytest.raises(TransactionCommitOutcomeUnknownError) as error,
            database.transaction() as transaction,
        ):
            transaction.execute(
                f'INSERT INTO "{schema}".connection_loss_probe VALUES(10, %s)',
                ("unacknowledged",),
            )
            _terminate(administrator, _backend_pid(transaction))

        assert error.value.code == "transaction_commit_outcome_unknown"
        assert "failed" not in str(error.value)
        assert isinstance(error.value.__cause__, psycopg2.Error)
        assert database._pool.snapshot.borrowed == 0
        assert not database._pool._discarded

        with database.transaction() as transaction:
            transaction.execute(
                f'INSERT INTO "{schema}".connection_loss_probe VALUES(11, %s)',
                ("healthy",),
            )

    assert _probe_rows(administrator, schema) == [(11, "healthy")]


class _LostAckConnection:
    def __init__(self, connection: Any, *, uncertain: bool) -> None:
        self._connection = connection
        self._uncertain = uncertain
        self.commit_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def commit(self) -> None:
        self.commit_calls += 1
        self._connection.commit()
        if self._uncertain:
            self._uncertain = False
            raise psycopg2.OperationalError("synthetic lost commit acknowledgement")


class _LostAckDriverPool:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._current: _LostAckConnection | None = None
        self._first = True
        self.connections: list[_LostAckConnection] = []
        self.returned: list[tuple[_LostAckConnection, bool]] = []

    def getconn(self) -> _LostAckConnection:
        if self._current is None:
            connection = _LostAckConnection(
                psycopg2.connect(self._dsn),
                uncertain=self._first,
            )
            self._first = False
            self.connections.append(connection)
            self._current = connection
        connection = self._current
        self._current = None
        return connection

    def putconn(self, connection: _LostAckConnection, *, close: bool = False) -> None:
        self.returned.append((connection, close))
        self._current = None
        if close:
            connection.close()

    def closeall(self) -> None:
        for connection in self.connections:
            connection.close()


def test_lost_commit_acknowledgement_preserves_uncertainty_without_retry(
    connection_loss_target: tuple[MigratedDatabase, Any, str],
) -> None:
    clone, administrator, schema = connection_loss_target
    driver = _LostAckDriverPool(clone.dsn)
    pool = ConnectionPool(driver)
    database = PlaneDatabase(pool)
    try:
        with (
            pytest.raises(TransactionCommitOutcomeUnknownError) as error,
            database.transaction() as transaction,
        ):
            transaction.execute(
                f'INSERT INTO "{schema}".connection_loss_probe VALUES(20, %s)',
                ("acknowledgement-lost",),
            )

        assert error.value.code == "transaction_commit_outcome_unknown"
        assert driver.connections[0].commit_calls == 1
        assert driver.returned == [(driver.connections[0], True)]
        assert pool.snapshot.borrowed == 0
        assert not pool._discarded
        assert _probe_rows(administrator, schema) == [(20, "acknowledgement-lost")]

        with database.transaction() as transaction:
            transaction.execute(
                f'INSERT INTO "{schema}".connection_loss_probe VALUES(21, %s)',
                ("healthy",),
            )
    finally:
        pool.close()

    assert _probe_rows(administrator, schema) == [
        (20, "acknowledgement-lost"),
        (21, "healthy"),
    ]


def test_real_checkout_interruption_is_typed_and_releases_the_slot(
    connection_loss_target: tuple[MigratedDatabase, Any, str],
) -> None:
    clone, administrator, _schema = connection_loss_target
    broken = psycopg2.connect(clone.dsn)
    broken.commit()
    try:
        with broken.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid() AS pid")
            backend_pid = int(cursor.fetchone()[0])
        _terminate(administrator, backend_pid)
        with pytest.raises(psycopg2.OperationalError) as captured, broken.cursor() as cursor:
            cursor.execute("SELECT 1")
        checkout_fault = captured.value
    finally:
        broken.close()

    class CheckoutFaultPool:
        def __init__(self) -> None:
            self.calls = 0
            self.current: Any = None
            self.returned: list[tuple[Any, bool]] = []

        def getconn(self) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise checkout_fault
            self.current = psycopg2.connect(clone.dsn)
            return self.current

        def putconn(self, connection: Any, *, close: bool = False) -> None:
            self.returned.append((connection, close))
            self.current = None
            if close:
                connection.close()

        def closeall(self) -> None:
            if self.current is not None:
                self.current.close()

    driver = CheckoutFaultPool()
    pool = ConnectionPool(driver)
    try:
        with pytest.raises(PoolCheckoutError) as error, pool.connection():
            pytest.fail("a failed checkout must not yield a connection")

        assert error.value.code == "pool_checkout_failed"
        assert isinstance(error.value.__cause__, psycopg2.OperationalError)
        assert pool.snapshot.borrowed == 0

        with pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
    finally:
        pool.close()

    assert driver.returned == [(connection, False)]
