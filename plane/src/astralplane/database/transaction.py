"""Caller-owned transactions (Transaction, PlaneDatabase) over database/pool.py
connections that detach every result into immutable DetachedRecord/CommandResult
values before the cursor closes. A lost statement connection is typed and discarded,
while a commit whose acknowledgement was lost is reported as an unknown outcome and
is never retried as a success.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from astralplane.contracts import IsolationLevel, Parameters, Statement
from astralplane.database.dialect import POSTGRES, Dialect
from astralplane.database.pool import ConnectionPool
from astralplane.database.sql import execute_native
from astralplane.errors import (
    PlaneError,
    TransactionCleanupError,
    TransactionCommitOutcomeUnknownError,
    TransactionRollbackError,
    TransactionStateError,
    TransactionStatementError,
)

_SAVEPOINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_TRANSACTION_STATUS_UNKNOWN = 4


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


class DetachedRecord(Mapping[str, Any]):
    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        if any(not isinstance(key, str) for key in values):
            raise TransactionStateError("driver record column names must be strings")
        object.__setattr__(
            self,
            "_values",
            MappingProxyType({key: _freeze(value) for key, value in values.items()}),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("DetachedRecord is immutable")

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"DetachedRecord({dict(self._values)!r})"


@dataclass(frozen=True, slots=True)
class CommandResult:
    rowcount: int
    status_message: str | None
    returned_records: tuple[DetachedRecord, ...] = ()


def _column_names(description: Any) -> tuple[str, ...]:
    if description is None:
        return ()
    names: list[str] = []
    for column in description:
        name = getattr(column, "name", None)
        if name is None:
            name = column[0]
        names.append(str(name))
    return tuple(names)


def _detach_row(row: Any, description: Any) -> DetachedRecord:
    if isinstance(row, Mapping):
        return DetachedRecord(row)
    names = _column_names(description)
    if not names:
        raise TransactionStateError("driver returned a row without column metadata")
    try:
        values = tuple(row)
    except TypeError as exc:
        raise TransactionStateError("driver returned a non-iterable row") from exc
    if len(values) != len(names):
        raise TransactionStateError("driver row width does not match column metadata")
    return DetachedRecord(dict(zip(names, values, strict=True)))


class Transaction:
    def __init__(
        self,
        connection: Any,
        *,
        isolation: IsolationLevel | None = None,
    ) -> None:
        self._connection = connection
        self._isolation = isolation
        self._active = False
        self._failed = False
        self._prior_autocommit: bool | None = None
        self._discard_required = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def discard_required(self) -> bool:
        return self._discard_required

    @property
    def dialect(self) -> Dialect:
        found = getattr(self._connection, "dialect", None)
        return found if isinstance(found, Dialect) else POSTGRES

    @property
    def isolation(self) -> IsolationLevel | None:
        return self._isolation

    def _connection_interrupted(self) -> bool:
        try:
            if getattr(self._connection, "closed", 0):
                return True
            info = getattr(self._connection, "info", None)
            return getattr(info, "transaction_status", 0) == _TRANSACTION_STATUS_UNKNOWN
        except Exception:
            return True

    def _raise_if_interrupted(self, exc: BaseException, operation: str) -> None:
        if not isinstance(exc, Exception) or isinstance(exc, PlaneError):
            return
        if self._connection_interrupted():
            self._discard_required = True
            raise TransactionStatementError(
                "transaction statement connection was interrupted",
                metadata={"operation": operation},
            ) from exc

    def _begin(self) -> None:
        if self._active:
            raise TransactionStateError("transaction is already active")
        if hasattr(self._connection, "autocommit"):
            try:
                self._prior_autocommit = bool(self._connection.autocommit)
                if self._prior_autocommit:
                    self._connection.autocommit = False
            except BaseException as exc:
                self._prior_autocommit = None
                self._discard_required = True
                if not isinstance(exc, Exception):
                    raise
                raise TransactionCleanupError("transaction connection setup failed") from exc
        self._active = True
        if self._isolation is not None:
            try:
                self._control(f"BEGIN ISOLATION LEVEL {self._isolation.value}")
            except BaseException as exc:
                self._active = False
                try:
                    self._restore_autocommit()
                except BaseException:
                    self._discard_required = True
                self._raise_if_interrupted(exc, "begin")
                raise

    def _restore_autocommit(self) -> None:
        if self._prior_autocommit is not None:
            prior = self._prior_autocommit
            self._prior_autocommit = None
            self._connection.autocommit = prior

    def _ensure_usable(self) -> None:
        if not self._active:
            raise TransactionStateError("transaction is not active")
        if self._failed:
            raise TransactionStateError("transaction is aborted and must roll back")

    def _cursor(self) -> Any:
        self._ensure_usable()
        try:
            return self._connection.cursor()
        except BaseException as exc:
            self._failed = True
            self._raise_if_interrupted(exc, "cursor")
            raise

    def _close_cursor(self, cursor: Any, *, preserve_active_error: bool) -> None:
        try:
            cursor.close()
        except BaseException as exc:
            self._failed = True
            if not preserve_active_error:
                self._raise_if_interrupted(exc, "cursor_close")
                raise

    def _control(self, statement: str) -> None:
        cursor = self._connection.cursor()
        try:
            cursor.execute(statement)
        except BaseException as exc:
            self._raise_if_interrupted(exc, "control")
            raise
        finally:
            cursor.close()

    def execute(self, statement: Statement, parameters: Parameters = ()) -> CommandResult:
        cursor = self._cursor()
        operation_failed = False
        try:
            execute_native(cursor, statement, parameters)
            description = getattr(cursor, "description", None)
            returned_records = (
                tuple(_detach_row(row, description) for row in cursor.fetchall())
                if description is not None
                else ()
            )
            return CommandResult(
                rowcount=int(getattr(cursor, "rowcount", -1)),
                status_message=(
                    None
                    if getattr(cursor, "statusmessage", None) is None
                    else str(cursor.statusmessage)
                ),
                returned_records=returned_records,
            )
        except BaseException as exc:
            operation_failed = True
            self._failed = True
            self._raise_if_interrupted(exc, "execute")
            raise
        finally:
            self._close_cursor(cursor, preserve_active_error=operation_failed)

    def fetch_one(self, statement: Statement, parameters: Parameters = ()) -> DetachedRecord | None:
        cursor = self._cursor()
        operation_failed = False
        try:
            execute_native(cursor, statement, parameters)
            row = cursor.fetchone()
            return None if row is None else _detach_row(row, cursor.description)
        except BaseException as exc:
            operation_failed = True
            self._failed = True
            self._raise_if_interrupted(exc, "fetch_one")
            raise
        finally:
            self._close_cursor(cursor, preserve_active_error=operation_failed)

    def fetch_all(
        self, statement: Statement, parameters: Parameters = ()
    ) -> tuple[DetachedRecord, ...]:
        cursor = self._cursor()
        operation_failed = False
        try:
            execute_native(cursor, statement, parameters)
            return tuple(_detach_row(row, cursor.description) for row in cursor.fetchall())
        except BaseException as exc:
            operation_failed = True
            self._failed = True
            self._raise_if_interrupted(exc, "fetch_all")
            raise
        finally:
            self._close_cursor(cursor, preserve_active_error=operation_failed)

    @contextmanager
    def savepoint(self, name: str) -> Iterator[Transaction]:
        self._ensure_usable()
        if _SAVEPOINT_NAME.fullmatch(name) is None:
            raise TransactionStateError("savepoint name is not a safe PostgreSQL identifier")
        quoted_name = f'"{name}"'
        try:
            self._control(f"SAVEPOINT {quoted_name}")
        except BaseException as exc:
            if not (
                self.dialect.is_sqlite
                and getattr(exc, "pgcode", None) == "55P03"
                and not self._connection.raw.in_transaction
            ):
                self._failed = True
            raise
        failed_before = self._failed
        try:
            yield self
        except BaseException:
            try:
                self._failed = False
                self._control(f"ROLLBACK TO SAVEPOINT {quoted_name}")
                self._control(f"RELEASE SAVEPOINT {quoted_name}")
                self._failed = failed_before
            except BaseException:
                self._failed = True
            raise
        else:
            try:
                self._control(f"RELEASE SAVEPOINT {quoted_name}")
            except BaseException:
                self._failed = True
                raise

    def _finish(self, *, failed: bool) -> None:
        if not self._active:
            raise TransactionStateError("transaction is not active")
        should_rollback = failed or self._failed
        primary_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            if should_rollback:
                try:
                    self._connection.rollback()
                except BaseException as exc:
                    self._discard_required = True
                    primary_error = exc
            else:
                try:
                    self._connection.commit()
                except BaseException as exc:
                    self._discard_required = True
                    with suppress(BaseException):
                        self._connection.rollback()
                    primary_error = exc
        finally:
            self._active = False
            try:
                self._restore_autocommit()
            except BaseException as exc:
                self._discard_required = True
                cleanup_error = exc
        if primary_error is not None:
            if not isinstance(primary_error, Exception):
                raise primary_error
            if should_rollback:
                raise TransactionRollbackError("transaction rollback failed") from primary_error
            raise TransactionCommitOutcomeUnknownError(
                "transaction commit outcome is unknown"
            ) from primary_error
        if cleanup_error is not None:
            if not isinstance(cleanup_error, Exception):
                raise cleanup_error
            raise TransactionCleanupError(
                "transaction connection cleanup failed"
            ) from cleanup_error
        if self._failed and not failed:
            raise TransactionStateError("transaction rolled back after a handled command failure")


class PlaneDatabase:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @contextmanager
    def transaction(
        self, *, isolation: IsolationLevel | None = None, read_only: bool = False
    ) -> Iterator[Transaction]:
        with self._pool.connection() as connection:
            transaction = Transaction(connection, isolation=isolation)
            if read_only and transaction.dialect.is_sqlite:
                connection.configure_read_only(True)
            try:
                transaction._begin()
                if read_only and transaction.dialect.is_postgres:
                    transaction._control("SET TRANSACTION READ ONLY")
                try:
                    yield transaction
                except BaseException:
                    with suppress(BaseException):
                        transaction._finish(failed=True)
                    raise
                else:
                    transaction._finish(failed=False)
            except BaseException:
                if transaction.discard_required:
                    self._pool._discard_on_return(connection)
                raise


__all__ = ("CommandResult", "DetachedRecord", "PlaneDatabase", "Transaction")
