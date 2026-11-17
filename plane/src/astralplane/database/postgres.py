"""Constructs the bounded psycopg2 driver pool behind database/pool.py's ConnectionPool
for embedded Plane runtimes, without exposing driver mechanics to callers.
"""

from __future__ import annotations

import importlib
import math
import threading
from types import ModuleType
from typing import Any, Final

from astralplane.database.pool import DriverPool
from astralplane.errors import InitializationError

_DRIVER_MODULE: Final = "psycopg2.pool"
_EXTRAS_MODULE: Final = "psycopg2.extras"


class _BoundedDriverPool:
    def __init__(self, pool: Any, *, maximum_connections: int, timeout_seconds: float) -> None:
        self._pool = pool
        self._semaphore = threading.BoundedSemaphore(maximum_connections)
        self._timeout_seconds = timeout_seconds

    def getconn(self) -> Any:
        if not self._semaphore.acquire(timeout=self._timeout_seconds):
            raise InitializationError(
                "PostgreSQL connection checkout exceeded its bounded deadline",
                code="pool_acquire_timeout",
            )
        try:
            return self._pool.getconn()
        except BaseException:
            self._semaphore.release()
            raise

    def putconn(self, connection: Any, *, close: bool = False) -> None:
        try:
            self._pool.putconn(connection, close=close)
        finally:
            self._semaphore.release()

    def closeall(self) -> None:
        self._pool.closeall()


def _load_driver_modules() -> tuple[ModuleType, ModuleType]:
    try:
        pool_module = importlib.import_module(_DRIVER_MODULE)
        extras_module = importlib.import_module(_EXTRAS_MODULE)
    except ImportError:
        raise InitializationError(
            "the composed runtime does not provide the required psycopg2 driver",
            code="postgres_driver_unavailable",
        ) from None
    return pool_module, extras_module


def _retaining_pool_type(pool_module: ModuleType) -> type[Any]:
    threaded_pool = pool_module.ThreadedConnectionPool

    class RetainingThreadedConnectionPool(threaded_pool):  # type: ignore[misc, valid-type]

        def _putconn(self, connection: Any, key: object = None, close: bool = False) -> Any:
            real_minimum = self.minconn
            self.minconn = self.maxconn
            try:
                return super()._putconn(connection, key=key, close=close)
            finally:
                self.minconn = real_minimum

    return RetainingThreadedConnectionPool


def create_postgres_driver_pool(
    database_url: str,
    *,
    minimum_connections: int = 2,
    maximum_connections: int = 10,
    acquire_timeout_seconds: float = 30.0,
    connect_timeout_seconds: int = 10,
    application_name: str = "astralplane",
) -> DriverPool:
    if not isinstance(database_url, str) or not database_url.strip():
        raise InitializationError("database_url must be a non-empty PostgreSQL DSN")
    if (
        not isinstance(minimum_connections, int)
        or isinstance(minimum_connections, bool)
        or minimum_connections < 1
    ):
        raise InitializationError("minimum_connections must be a positive integer")
    if (
        not isinstance(maximum_connections, int)
        or isinstance(maximum_connections, bool)
        or maximum_connections < minimum_connections
    ):
        raise InitializationError(
            "maximum_connections must be an integer no smaller than minimum_connections"
        )
    if (
        isinstance(acquire_timeout_seconds, bool)
        or not isinstance(acquire_timeout_seconds, (int, float))
        or not math.isfinite(float(acquire_timeout_seconds))
        or acquire_timeout_seconds <= 0
    ):
        raise InitializationError("acquire_timeout_seconds must be finite and positive")
    if (
        not isinstance(connect_timeout_seconds, int)
        or isinstance(connect_timeout_seconds, bool)
        or connect_timeout_seconds < 1
    ):
        raise InitializationError("connect_timeout_seconds must be a positive integer")
    if (
        not isinstance(application_name, str)
        or not application_name
        or len(application_name) > 128
        or any(ord(character) < 0x20 for character in application_name)
    ):
        raise InitializationError("application_name must be bounded printable text")

    pool_module, extras_module = _load_driver_modules()
    retaining_pool = _retaining_pool_type(pool_module)
    try:
        driver_pool = retaining_pool(
            minimum_connections,
            maximum_connections,
            dsn=database_url,
            connect_timeout=connect_timeout_seconds,
            application_name=application_name,
            cursor_factory=extras_module.RealDictCursor,
        )
    except Exception:
        raise InitializationError(
            "PostgreSQL connection pool construction failed",
            code="postgres_pool_construction_failed",
        ) from None
    return _BoundedDriverPool(
        driver_pool,
        maximum_connections=maximum_connections,
        timeout_seconds=float(acquire_timeout_seconds),
    )


__all__ = ("create_postgres_driver_pool",)
