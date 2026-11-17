"""Tests for src/astralplane/database/pool.py and transaction.py: detached command
results survive pool release, and cursor/fetch failures abort rather than commit.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import (
    CommandResult,
    DetachedRecord,
    PlaneDatabase,
    _column_names,
    _detach_row,
)
from astralplane.errors import TransactionStateError


class Column:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.closed = False
        self.description: list[Column] | None = None
        self.rowcount = -1
        self.statusmessage: str | None = None
        self._rows: list[Any] = []

    def execute(self, statement: str, parameters: object | None = None) -> None:
        self.connection.calls.append((statement, parameters))
        if statement == "RETURNING":
            self.description = [Column("id"), Column("payload")]
            self._rows = [(7, {"nested": ["value"]})]
            self.rowcount = 1
            self.statusmessage = "INSERT 0 1"
        elif statement == "MAPPING":
            self.description = [Column("id")]
            self._rows = [{"id": 9}]
            self.rowcount = 1
            self.statusmessage = "SELECT 1"
        elif statement == "FETCH_ONE_FAIL":
            self.description = [Column("id")]
            self._rows = [RuntimeError("fetch one failed")]
        elif statement == "FETCH_ALL_FAIL":
            self.description = [Column("id")]
            self._rows = [RuntimeError("fetch all failed")]
        elif statement == "FAIL":
            raise RuntimeError("driver failure")
        else:
            self.description = None
            self.rowcount = 3
            self.statusmessage = "UPDATE 3"

    def fetchone(self) -> Any:
        if self._rows and isinstance(self._rows[0], BaseException):
            raise self._rows[0]
        return None if not self._rows else self._rows[0]

    def fetchall(self) -> list[Any]:
        if self._rows and isinstance(self._rows[0], BaseException):
            raise self._rows[0]
        return list(self._rows)

    def close(self) -> None:
        self.closed = True
        if self.connection.fail_cursor_close:
            raise RuntimeError("cursor close failed")


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.cursors: list[FakeCursor] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = True
        self.fail_cursor_open = False
        self.fail_cursor_close = False

    def cursor(self) -> FakeCursor:
        if self.fail_cursor_open:
            raise RuntimeError("cursor open failed")
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeDriverPool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.returned: list[tuple[FakeConnection, bool]] = []
        self.closed = False

    def getconn(self) -> FakeConnection:
        return self.connection

    def putconn(self, connection: FakeConnection, *, close: bool = False) -> None:
        self.returned.append((connection, close))

    def closeall(self) -> None:
        self.closed = True


def _database() -> tuple[PlaneDatabase, FakeConnection, FakeDriverPool]:
    connection = FakeConnection()
    driver_pool = FakeDriverPool(connection)
    return PlaneDatabase(ConnectionPool(driver_pool)), connection, driver_pool


def test_execute_returns_only_detached_immutable_values_after_pool_release() -> None:
    database, connection, driver_pool = _database()

    with database.transaction() as transaction:
        result = transaction.execute("RETURNING", ("input",))
        cursor = connection.cursors[-1]
        assert cursor.closed
        assert driver_pool.returned == []

    assert isinstance(result, CommandResult)
    assert result.rowcount == 1
    assert result.status_message == "INSERT 0 1"
    assert result.returned_records[0]["id"] == 7
    assert result.returned_records[0]["payload"]["nested"] == ("value",)
    assert connection.commits == 1
    assert driver_pool.returned == [(connection, False)]
    assert all(cursor.closed for cursor in connection.cursors)
    assert not hasattr(result, "cursor")
    assert not hasattr(result, "connection")

    with pytest.raises(TypeError):
        result.returned_records[0]["payload"]["nested"][0] = "changed"
    with pytest.raises(TypeError):
        result.returned_records[0]["payload"]["new"] = "changed"
    with pytest.raises(AttributeError):
        result.returned_records[0]._values = {}  # type: ignore[attr-defined]
    with pytest.raises(FrozenInstanceError):
        result.rowcount = 2  # type: ignore[misc]


def test_execute_without_returning_detaches_command_metadata() -> None:
    database, connection, _ = _database()

    with database.transaction() as transaction:
        result = transaction.execute("UPDATE", {"owner_id": "owner-1"})

    assert result == CommandResult(rowcount=3, status_message="UPDATE 3")
    assert connection.calls == [("UPDATE", {"owner_id": "owner-1"})]


def test_fetch_methods_detach_mapping_and_sequence_rows() -> None:
    database, _, _ = _database()

    with database.transaction() as transaction:
        one = transaction.fetch_one("RETURNING")
        all_rows = transaction.fetch_all("MAPPING")

    assert one is not None and dict(one) == {"id": 7, "payload": {"nested": ("value",)}}
    assert tuple(dict(row) for row in all_rows) == ({"id": 9},)


def test_detached_record_freezes_sets_and_mutable_binary_values() -> None:
    record = DetachedRecord(
        {"tags": {"a", "b"}, "buffer": bytearray(b"abc"), "view": memoryview(b"xyz")}
    )

    assert record["tags"] == frozenset({"a", "b"})
    assert record["buffer"] == b"abc"
    assert record["view"] == b"xyz"
    assert len(record) == 3
    assert "DetachedRecord" in repr(record)


def test_driver_column_metadata_fallback_and_row_shape_failures() -> None:
    assert _column_names(None) == ()
    assert dict(_detach_row((1, "two"), [("id",), ("value",)])) == {
        "id": 1,
        "value": "two",
    }
    with pytest.raises(TransactionStateError, match="without column metadata"):
        _detach_row((1,), None)
    with pytest.raises(TransactionStateError, match="non-iterable"):
        _detach_row(1, [("id",)])
    with pytest.raises(TransactionStateError, match="width"):
        _detach_row((1, 2), [("id",)])
    with pytest.raises(TransactionStateError, match="column names"):
        DetachedRecord({1: "value"})  # type: ignore[dict-item]


@pytest.mark.parametrize("statement", ["FETCH_ONE_FAIL", "FETCH_ALL_FAIL"])
def test_fetch_failure_aborts_transaction(statement: str) -> None:
    database, connection, _ = _database()

    with pytest.raises(RuntimeError, match="fetch"), database.transaction() as transaction:
        if statement == "FETCH_ONE_FAIL":
            transaction.fetch_one(statement)
        else:
            transaction.fetch_all(statement)

    assert connection.commits == 0
    assert connection.rollbacks >= 1


@pytest.mark.parametrize("failure", ["open", "close"])
def test_cursor_lifecycle_failure_aborts_instead_of_committing(failure: str) -> None:
    database, connection, _ = _database()
    connection.fail_cursor_open = failure == "open"
    connection.fail_cursor_close = failure == "close"

    with (
        pytest.raises(RuntimeError, match=f"cursor {failure}"),
        database.transaction() as transaction,
    ):
        transaction.execute("UPDATE")

    assert connection.commits == 0


def test_cursor_close_failure_never_masks_the_primary_driver_failure() -> None:
    database, connection, _ = _database()
    connection.fail_cursor_close = True

    with pytest.raises(RuntimeError, match="driver failure"), database.transaction() as transaction:
        transaction.execute("FAIL")

    assert connection.commits == 0
