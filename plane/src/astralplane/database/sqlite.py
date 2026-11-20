"""SQLite driver for Plane: a bounded pool of standard-library connections with write-ahead
logging, one writer at a time, 0600 database files and a PostgreSQL-flavoured cursor that
database/transaction.py drives unchanged. Statements pass through database/sqlite_sql.py.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from astralplane.database.dialect import SQLITE, Dialect
from astralplane.database.pool import DriverPool
from astralplane.database.sqlite_functions import (
    bind_value,
    install_functions,
    register_adapters,
    timeout_seconds,
)
from astralplane.database.sqlite_sql import (
    classify_control,
    is_try_lock,
    statement_access,
    translate,
)
from astralplane.errors import InitializationError, LockContentionError

MINIMUM_SQLITE_VERSION: Final = (3, 39, 0)
SQLITE_URL_SCHEME: Final = "sqlite"
LOCK_NOT_AVAILABLE: Final = "55P03"


class SQLiteLockTimeoutError(LockContentionError):
    pgcode = LOCK_NOT_AVAILABLE

    def __init__(self, message: str = "SQLite write lock was not available in time") -> None:
        super().__init__(message)


def is_sqlite_url(database_url: str) -> bool:
    return isinstance(database_url, str) and database_url.lower().startswith(
        f"{SQLITE_URL_SCHEME}:"
    )


def sqlite_path_from_url(database_url: str) -> Path:
    parts = urlsplit(database_url)
    if parts.scheme.lower() != SQLITE_URL_SCHEME or parts.netloc or parts.query or parts.fragment:
        raise InitializationError(
            "a SQLite database URL must look like sqlite:///absolute/path.db",
            code="sqlite_url_invalid",
        )
    path = unquote(parts.path)
    if not path.startswith("/") or path in {"/", "/:memory:"} or "\x00" in path:
        raise InitializationError(
            "a SQLite database URL needs an absolute file path", code="sqlite_url_invalid"
        )
    return Path(path)


def _wrap_lock_error(exc: sqlite3.Error) -> BaseException:
    native_code = getattr(exc, "sqlite_errorcode", None)
    if (
        isinstance(exc, sqlite3.OperationalError)
        and type(native_code) is int
        and native_code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    ):
        error = SQLiteLockTimeoutError()
        error.__cause__ = exc
        return error
    return exc


class _Description:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class SQLiteCursor:
    def __init__(self, connection: SQLiteConnection) -> None:
        self._connection = connection
        self._raw: sqlite3.Cursor | None = None
        self._closed = False
        self._deadline: float | None = None
        self.description: list[_Description] | None = None
        self.rowcount = -1
        self.statusmessage: str | None = None

    def execute(self, statement: str, parameters: Any = None) -> SQLiteCursor:
        if self._closed:
            raise sqlite3.ProgrammingError("cursor is closed")
        self._clear()
        self.description = None
        self.rowcount = -1
        self.statusmessage = None
        self._connection.note_statement()
        control = classify_control(statement)
        if control is not None:
            if control.startswith("setting:"):
                _, name, value = control.split(":", 2)
                self._connection.set_local_setting(name, value)
            elif control != "begin":
                self._connection.begin_if_needed()
            if control == "audit_purge":
                self._connection.set_local_setting("audit.allow_purge", "true")
            return self
        translated = translate(statement, parameterized=bool(parameters))
        if parameters is None:
            bound: Sequence[object] | dict[str, object] = ()
        elif isinstance(parameters, dict):
            bound = {key: bind_value(value) for key, value in parameters.items()}
        else:
            bound = tuple(bind_value(value) for value in parameters)
        write, scalar, locking_select, no_wait = statement_access(statement)
        if locking_select and self._connection._holds_gate:
            locking_select = False
        deadline = self._connection.statement_deadline()
        self._deadline = deadline
        if locking_select and not self._connection.raw.in_transaction:
            probe = self._connection.raw.cursor()
            try:
                self._execute_raw(probe, translated, bound, deadline)
                found = self._fetch(raw=probe, all_rows=False) is not None
            except BaseException:
                probe.close()
                raise
            if not found:
                self._raw = probe
                self.description = [_Description(column[0]) for column in probe.description]
                self.rowcount = probe.rowcount
                return self
            probe.close()
            self._connection.begin_if_needed(write=True, no_wait=no_wait)
            locking_select = False
        if not scalar:
            self._connection.begin_if_needed(
                write=(write and not locking_select) or not self._connection.raw.in_transaction,
                defer_writer=is_try_lock(statement),
            )
        raw = self._connection.raw.cursor()
        self._raw = raw
        self._execute_raw(raw, translated, bound, deadline)
        if locking_select and self._fetch(raw=raw, all_rows=False) is not None:
            self._connection.reserve_writer(no_wait=no_wait)
            raw.close()
            raw = self._connection.raw.cursor()
            self._raw = raw
            self._execute_raw(raw, translated, bound, deadline)
        columns = raw.description
        self.description = None if columns is None else [_Description(c[0]) for c in columns]
        self.rowcount = raw.rowcount
        self._connection.record_control(translated)
        return self

    def _execute_raw(self, raw, statement, bound, deadline) -> None:
        try:
            self._watch_progress(deadline)
            raw.execute(statement, bound)
            self._check_deadline(deadline)
        except BaseException as exc:
            self._clear_progress()
            raw.close()
            if self._raw is raw:
                self._raw = None
            wrapped = _wrap_lock_error(exc) if isinstance(exc, sqlite3.Error) else exc
            if wrapped is exc:
                raise
            raise wrapped from exc
        finally:
            self._stop_progress()
        if raw.description is None:
            self._clear_progress()

    def _watch_progress(self, deadline) -> None:
        self._check_deadline(deadline)
        if deadline is None:
            self._stop_progress()
        else:
            self._connection.raw.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)

    def _check_deadline(self, deadline) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("SQLite statement deadline exceeded")

    def _stop_progress(self) -> None:
        if not self._connection.closed:
            self._connection.raw.set_progress_handler(None, 0)

    def _clear_progress(self) -> None:
        self._deadline = None
        self._stop_progress()

    def _fetch(self, *, all_rows, raw=None):
        raw = self._raw if raw is None else raw
        try:
            self._watch_progress(self._deadline)
            result = raw.fetchall() if all_rows else raw.fetchone()
            self._check_deadline(self._deadline)
        except BaseException as exc:
            self._clear_progress()
            raw.close()
            if self._raw is raw:
                self._raw = None
            wrapped = _wrap_lock_error(exc) if isinstance(exc, sqlite3.Error) else exc
            if wrapped is exc:
                raise
            raise wrapped from exc
        finally:
            self._stop_progress()
        if all_rows or result is None:
            self._clear_progress()
        return result

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self._raw is None or self.description is None:
            raise sqlite3.ProgrammingError("the statement did not produce records")
        rows = self._fetch(all_rows=True)
        self.rowcount = self._raw.rowcount if self.description is None else len(rows)
        return rows

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._raw is None or self.description is None:
            raise sqlite3.ProgrammingError("the statement did not produce records")
        return self._fetch(all_rows=False)

    def _clear(self) -> None:
        self._clear_progress()
        if self._raw is not None:
            self._raw.close()
            self._raw = None

    def close(self) -> None:
        self._closed = True
        self._clear()

    def __enter__(self) -> SQLiteCursor:
        return self

    def __exit__(self, *error: object) -> None:
        self.close()


class SQLiteConnection:
    dialect: Dialect = SQLITE

    def __init__(
        self,
        raw: sqlite3.Connection,
        *,
        writer_gate: threading.Lock,
        gate_timeout_seconds: float,
        settings: dict[str, str],
        session_settings: dict[str, str],
        transaction_started: list[datetime | None],
        statement_started: list[datetime | None],
        clock: Callable[[], datetime],
    ) -> None:
        self.raw = raw
        self._gate = writer_gate
        self._gate_timeout = gate_timeout_seconds
        self._holds_gate = False
        self._settings = settings
        self._session_settings = session_settings
        self._prior_session_settings: dict[str, str] | None = None
        self._transaction_started = transaction_started
        self._statement_started = statement_started
        self._clock = clock
        self._savepoint_settings: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.autocommit = False
        self.closed = 0
        self.begin_mode = "IMMEDIATE"

    def cursor(self) -> SQLiteCursor:
        if self.closed:
            raise sqlite3.ProgrammingError("connection is closed")
        return SQLiteCursor(self)

    def begin_if_needed(
        self, *, write: bool = True, no_wait: bool = False, defer_writer: bool = False
    ) -> None:
        if self.autocommit:
            return
        if self.raw.in_transaction:
            if write:
                self.reserve_writer(no_wait=no_wait)
            return
        immediate = write and self.begin_mode == "IMMEDIATE" and not defer_writer
        timeout = (
            0
            if no_wait
            else (
                timeout_seconds(
                    self._settings.get(
                        "lock_timeout", self._session_settings.get("lock_timeout", "0")
                    )
                )
                or self._gate_timeout
            )
        )
        self.raw.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        if immediate:
            if not self._gate.acquire(timeout=timeout):
                raise SQLiteLockTimeoutError()
            self._holds_gate = True
        try:
            self.raw.execute("BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED")
            if self.begin_mode == "DEFERRED":
                self.raw.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            self._release_gate()
            wrapped = _wrap_lock_error(exc)
            if wrapped is exc:
                raise
            raise wrapped from exc

    def reserve_writer(self, *, try_only: bool = False, no_wait: bool = False) -> bool:
        if self._holds_gate:
            return True
        if not self.raw.in_transaction:
            if try_only:
                return False
            raise sqlite3.ProgrammingError("writer reservation requires an active transaction")
        if self.begin_mode == "DEFERRED":
            if try_only:
                return False
            raise sqlite3.OperationalError(
                "a read-only transaction cannot reserve the SQLite writer"
            )
        timeout = (
            0
            if try_only or no_wait
            else (
                timeout_seconds(
                    self._settings.get(
                        "lock_timeout", self._session_settings.get("lock_timeout", "0")
                    )
                )
                or self._gate_timeout
            )
        )
        if not self._gate.acquire(timeout=timeout):
            if try_only:
                return False
            raise SQLiteLockTimeoutError()
        self._holds_gate = True
        try:
            self.raw.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
            self.raw.execute("UPDATE schema_meta SET value=value WHERE 0")
            return True
        except sqlite3.Error as exc:
            self._release_gate()
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY_SNAPSHOT:
                if try_only:
                    return False
                raise SQLiteLockTimeoutError(
                    "another writer committed after this transaction began reading, "
                    "so it can no longer take the SQLite writer: retry the transaction"
                ) from exc
            wrapped = _wrap_lock_error(exc)
            if try_only and isinstance(wrapped, SQLiteLockTimeoutError):
                return False
            if wrapped is exc:
                raise
            raise wrapped from exc

    def _release_gate(self) -> None:
        if self._holds_gate:
            self._holds_gate = False
            self._gate.release()

    def _finish(self, verb: str) -> None:
        try:
            if self.raw.in_transaction:
                self.raw.execute(verb)
        finally:
            try:
                if self.begin_mode != "IMMEDIATE":
                    self.raw.execute("PRAGMA query_only = OFF")
            finally:
                if verb == "ROLLBACK" and self._prior_session_settings is not None:
                    self._session_settings.clear()
                    self._session_settings.update(self._prior_session_settings)
                self._prior_session_settings = None
                self._settings.clear()
                self._savepoint_settings.clear()
                self._transaction_started[0] = None
                self._statement_started[0] = None
                self._release_gate()

    def set_local_setting(self, name: str, value: str) -> None:
        if name in {"lock_timeout", "statement_timeout"}:
            timeout_seconds(value)
        self._settings[name] = value

    def statement_deadline(self) -> float | None:
        seconds = timeout_seconds(
            self._settings.get(
                "statement_timeout", self._session_settings.get("statement_timeout", "0")
            )
        )
        return time.monotonic() + seconds if seconds else None

    def note_statement(self) -> None:
        if self._prior_session_settings is None:
            self._prior_session_settings = dict(self._session_settings)
        self._statement_started[0] = self._clock()
        if self._transaction_started[0] is None:
            self._transaction_started[0] = self._statement_started[0]

    def record_control(self, statement: str) -> None:
        match = re.fullmatch(
            r'\s*(SAVEPOINT|ROLLBACK TO SAVEPOINT|RELEASE SAVEPOINT)\s+"(\w+)"\s*',
            statement,
            re.IGNORECASE,
        )
        if match is None:
            return
        operation, name = match[1].upper(), match[2]
        if operation == "SAVEPOINT":
            self._savepoint_settings.append(
                (name, dict(self._settings), dict(self._session_settings))
            )
            return
        index = next(
            i
            for i in range(len(self._savepoint_settings) - 1, -1, -1)
            if self._savepoint_settings[i][0] == name
        )
        if operation == "ROLLBACK TO SAVEPOINT":
            self._settings.clear()
            self._settings.update(self._savepoint_settings[index][1])
            self._session_settings.clear()
            self._session_settings.update(self._savepoint_settings[index][2])
            del self._savepoint_settings[index + 1 :]
        else:
            del self._savepoint_settings[index:]

    def commit(self) -> None:
        self._finish("COMMIT")

    def rollback(self) -> None:
        self._finish("ROLLBACK")

    def configure_read_only(self, read_only: bool) -> None:
        self.begin_mode = "DEFERRED" if read_only else "IMMEDIATE"

    def close(self) -> None:
        if self.closed:
            return
        self.closed = 1
        try:
            if self.raw.in_transaction:
                self.raw.execute("ROLLBACK")
        finally:
            self._release_gate()
            self.raw.close()


class SQLiteDriverPool:
    @property
    def database_path(self) -> Path:
        return self._path

    @property
    def busy_timeout_seconds(self) -> float:
        return self._busy_timeout_ms / 1000

    def __init__(
        self,
        path: Path,
        *,
        maximum_connections: int,
        acquire_timeout_seconds: float,
        busy_timeout_seconds: float,
        clock: Callable[[], datetime],
    ) -> None:
        self._path = path
        self._maximum = maximum_connections
        self._acquire_timeout = acquire_timeout_seconds
        self._busy_timeout_ms = int(busy_timeout_seconds * 1000)
        self._clock = clock
        self._semaphore = threading.BoundedSemaphore(maximum_connections)
        self._writer_gate = threading.Lock()
        self._lock = threading.Lock()
        self._idle: list[SQLiteConnection] = []
        self._all: list[SQLiteConnection] = []
        self._closed = False
        self._borrowed: set[SQLiteConnection] = set()

    def _open(self) -> SQLiteConnection:
        descriptor = os.open(
            self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise InitializationError("SQLite requires a regular database file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        raw = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
        )
        settings: dict[str, str] = {}
        session_settings: dict[str, str] = {}
        started: list[datetime | None] = [None]
        statement_started: list[datetime | None] = [None]
        opened: list[SQLiteConnection] = []

        def transaction_clock() -> datetime:
            return started[0] or self._clock()

        try:
            raw.execute("PRAGMA journal_mode = WAL")
            raw.execute("PRAGMA synchronous = FULL")
            raw.execute("PRAGMA foreign_keys = ON")
            raw.execute("PRAGMA trusted_schema = ON")
            raw.execute("PRAGMA case_sensitive_like = ON")
            raw.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            install_functions(
                raw,
                clock=self._clock,
                transaction_clock=transaction_clock,
                settings=settings,
                statement_clock=lambda: statement_started[0] or self._clock(),
                session_settings=session_settings,
                reserve_writer=lambda: opened[0].reserve_writer(try_only=True),
            )
        except BaseException:
            raw.close()
            raise
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self._path}{suffix}")
            if sidecar.exists():
                os.chmod(sidecar, stat.S_IRUSR | stat.S_IWUSR)
        connection = SQLiteConnection(
            raw,
            writer_gate=self._writer_gate,
            gate_timeout_seconds=self._busy_timeout_ms / 1000,
            settings=settings,
            session_settings=session_settings,
            transaction_started=started,
            statement_started=statement_started,
            clock=self._clock,
        )
        opened.append(connection)
        return connection

    def getconn(self) -> SQLiteConnection:
        if not self._semaphore.acquire(timeout=self._acquire_timeout):
            raise InitializationError(
                "SQLite connection checkout exceeded its bounded deadline",
                code="pool_acquire_timeout",
            )
        try:
            with self._lock:
                if self._closed:
                    raise InitializationError("SQLite pool is closed")
                connection = self._idle.pop() if self._idle else None
                if connection is None:
                    connection = self._open()
                    self._all.append(connection)
                self._borrowed.add(connection)
            connection.configure_read_only(False)
            return connection
        except BaseException:
            self._semaphore.release()
            raise

    def putconn(self, connection: SQLiteConnection, *, close: bool = False) -> None:
        with self._lock:
            if connection not in self._borrowed:
                raise InitializationError("SQLite connection was not checked out from this pool")
            self._borrowed.remove(connection)
        try:
            if close or connection.closed or self._closed:
                with self._lock:
                    if connection in self._all:
                        self._all.remove(connection)
                connection.close()
            else:
                with self._lock:
                    self._idle.append(connection)
        finally:
            self._semaphore.release()

    def closeall(self) -> None:
        with self._lock:
            self._closed = True
            connections = list(self._all)
            self._idle.clear()
        failure: BaseException | None = None
        for connection in connections:
            try:
                connection.close()
                with self._lock:
                    self._all.remove(connection)
            except BaseException as exc:
                failure = failure or exc
        if failure is not None:
            raise failure


def create_sqlite_driver_pool(
    database_url: str,
    *,
    maximum_connections: int = 8,
    acquire_timeout_seconds: float = 30.0,
    busy_timeout_seconds: float = 10.0,
    clock: Callable[[], datetime] | None = None,
) -> DriverPool:
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE_VERSION:
        raise InitializationError(
            "the SQLite library is older than the 3.39 Plane requires",
            code="sqlite_version_unsupported",
        )
    path = sqlite_path_from_url(database_url)
    if not path.parent.is_dir():
        raise InitializationError(
            "the SQLite database directory does not exist", code="sqlite_directory_missing"
        )
    for name, value in (
        ("maximum_connections", maximum_connections),
        ("acquire_timeout_seconds", acquire_timeout_seconds),
        ("busy_timeout_seconds", busy_timeout_seconds),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise InitializationError(f"{name} must be positive")
    if not isinstance(maximum_connections, int):
        raise InitializationError("maximum_connections must be an integer")
    register_adapters()
    return SQLiteDriverPool(
        path,
        maximum_connections=int(maximum_connections),
        acquire_timeout_seconds=float(acquire_timeout_seconds),
        busy_timeout_seconds=float(busy_timeout_seconds),
        clock=clock or (lambda: datetime.now(UTC)),
    )


__all__ = (
    "LOCK_NOT_AVAILABLE",
    "SQLiteConnection",
    "SQLiteCursor",
    "SQLiteDriverPool",
    "SQLiteLockTimeoutError",
    "create_sqlite_driver_pool",
    "is_sqlite_url",
    "sqlite_path_from_url",
)
