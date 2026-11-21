"""Creates isolated SQLite copies of the current guarded schema for the repository contract suite.
The PostgreSQL template harness selects these fixtures when its database URL uses SQLite.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from typing import Any

from astralplane.database.migrations import MigrationRunner
from astralplane.database.pool import ConnectionPool
from astralplane.database.sqlite import create_sqlite_driver_pool
from astralplane.database.sqlite_migrations import (
    SQLITE_DATA_PLANE_REVISION,
    SQLITE_MIGRATION_REGISTRY,
    SQLiteBootRunner,
)
from astralplane.database.transaction import PlaneDatabase


class ObservedWriterGate:
    def __init__(self):
        self._lock = Lock()
        self.observe = False
        self.waiting = Event()

    def acquire(self, *, timeout=-1):
        if self.observe:
            if self._lock.acquire(blocking=False):
                return True
            self.waiting.set()
        return self._lock.acquire(timeout=timeout)

    def release(self):
        self._lock.release()


class SQLiteTemplate:
    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="plane_sqlite_template_")
        self._path = Path(self._directory.name) / "template.db"
        pool = ConnectionPool(create_sqlite_driver_pool(f"sqlite://{self._path}"))
        try:
            database = PlaneDatabase(pool)
            SQLiteBootRunner(
                database,
                MigrationRunner(
                    database,
                    revision=SQLITE_DATA_PLANE_REVISION,
                    registry=SQLITE_MIGRATION_REGISTRY,
                ),
            ).run(expected_revision=SQLITE_DATA_PLANE_REVISION.schema_revision)
        finally:
            pool.close()

    @contextmanager
    def clone(self):
        from tests.fixtures.migrated_template import MigratedDatabase

        with tempfile.TemporaryDirectory(prefix="plane_sqlite_clone_") as directory:
            path = Path(directory) / "plane.db"
            shutil.copyfile(self._path, path)
            url = f"sqlite://{path}"
            driver = create_sqlite_driver_pool(url)
            connection = driver.getconn()
            pool = ConnectionPool(driver)
            try:
                yield MigratedDatabase(
                    name=path.name,
                    dsn=url,
                    schema="main",
                    connection=connection,
                    pool=pool,
                    database=PlaneDatabase(pool),
                )
            finally:
                driver.putconn(connection)
                pool.close()

    def drop(self) -> None:
        self._directory.cleanup()


class SQLiteTemplates:
    def __init__(self) -> None:
        self._template: SQLiteTemplate | None = None

    def current(self) -> SQLiteTemplate:
        if self._template is None:
            self._template = SQLiteTemplate()
        return self._template

    def close(self) -> None:
        if self._template is not None:
            self._template.drop()

    def template(self, runner: Any) -> SQLiteTemplate:
        raise ValueError("SQLite contract fixtures support only their current baseline")
