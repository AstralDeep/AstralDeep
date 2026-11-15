"""Builds PostgreSQL template databases by running real Plane migrations once, then hands each
consumer a fresh CREATE DATABASE ... TEMPLATE copy that keeps the template's fixed schema name.
LineageTemplates caches one template per declared migration lineage for the whole pytest process,
building each by applying the remaining edges to a clone of its nearest accepted predecessor.
A child pytest process adopts the current template its parent names in the environment.
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

import psycopg2
import pytest
from psycopg2.extensions import make_dsn

from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from tests.fixtures.pre_split.loader import TEST_DATABASE_ENV

TEMPLATE_SCHEMA: Final = "astralplane_fixture_" + "0" * 32
SHARED_CURRENT_TEMPLATE_ENV: Final = "ASTRALPLANE_TEST_CURRENT_TEMPLATE"
_IDENTIFIER: Final = re.compile(r"^(?:plane_tpl|plane_clone|astralplane_fixture)_[0-9a-f]{32}$")
_CLONE_LOCK: Final = threading.Lock()
_SESSION_LOCK: Final = threading.Lock()


class DedicatedDriverPool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.borrowed = False

    def getconn(self) -> Any:
        if self.borrowed:
            raise RuntimeError("template database connection is already borrowed")
        self.borrowed = True
        return self.connection

    def putconn(self, connection: Any, *, close: bool = False) -> None:
        if connection is not self.connection or not self.borrowed or close:
            raise RuntimeError("template database connection was returned in an invalid state")
        self.borrowed = False

    def closeall(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class MigratedDatabase:
    name: str
    dsn: str
    schema: str
    connection: Any
    pool: ConnectionPool
    database: PlaneDatabase


def current_runner(database: PlaneDatabase) -> MigrationRunner:
    return MigrationRunner(
        database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )


def migrate_to_current(database: PlaneDatabase) -> None:
    BaselineMigrationRunner(database, current_runner(database)).run(
        expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision
    )


def _quoted(identifier: str) -> str:
    if _IDENTIFIER.fullmatch(identifier) is None:
        raise ValueError(f"refusing to quote an unexpected identifier: {identifier!r}")
    return f'"{identifier}"'


@contextmanager
def _connected(name: str, dsn: str, *, create_schema: bool) -> Iterator[MigratedDatabase]:
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            if create_schema:
                cursor.execute(f"CREATE SCHEMA {_quoted(TEMPLATE_SCHEMA)}")
            cursor.execute(f"SET search_path TO {_quoted(TEMPLATE_SCHEMA)}, pg_catalog")
        connection.commit()
        pool = ConnectionPool(DedicatedDriverPool(connection))
        try:
            yield MigratedDatabase(
                name=name,
                dsn=dsn,
                schema=TEMPLATE_SCHEMA,
                connection=connection,
                pool=pool,
                database=PlaneDatabase(pool),
            )
        finally:
            pool.close()
    finally:
        connection.close()


class DatabaseTemplate:
    def __init__(self, administrator_dsn: str, name: str, *, owned: bool = True) -> None:
        _quoted(name)
        self.administrator_dsn = administrator_dsn
        self.name = name
        self.owned = owned

    @classmethod
    def build(
        cls,
        administrator_dsn: str,
        migrate: Callable[[PlaneDatabase], object],
        *,
        source: DatabaseTemplate | None = None,
    ) -> DatabaseTemplate:
        return cls.prepare(
            administrator_dsn,
            lambda built: migrate(built.database),
            source=source,
            create_schema=source is None,
        )

    @classmethod
    def prepare(
        cls,
        administrator_dsn: str,
        prepare: Callable[[MigratedDatabase], object],
        *,
        source: DatabaseTemplate | None = None,
        create_schema: bool,
    ) -> DatabaseTemplate:
        template = cls(administrator_dsn, f"plane_tpl_{uuid.uuid4().hex}")
        origin = "template0" if source is None else _quoted(source.name)
        with _CLONE_LOCK:
            template._administer(f"CREATE DATABASE {_quoted(template.name)} TEMPLATE {origin}")
        try:
            with _connected(
                template.name,
                make_dsn(administrator_dsn, dbname=template.name),
                create_schema=create_schema,
            ) as built:
                prepare(built)
            # CREATE DATABASE ... TEMPLATE fails while any session is connected to the template.
            template._administer(
                f"ALTER DATABASE {_quoted(template.name)} WITH ALLOW_CONNECTIONS false"
            )
        except BaseException:
            template.drop()
            raise
        return template

    @classmethod
    def adopt(cls, administrator_dsn: str, name: str) -> DatabaseTemplate:
        return cls(administrator_dsn, name, owned=False)

    @contextmanager
    def clone(self) -> Iterator[MigratedDatabase]:
        name = f"plane_clone_{uuid.uuid4().hex}"
        with _CLONE_LOCK:
            self._administer(f"CREATE DATABASE {_quoted(name)} TEMPLATE {_quoted(self.name)}")
        try:
            with _connected(
                name,
                make_dsn(self.administrator_dsn, dbname=name),
                create_schema=False,
            ) as clone:
                yield clone
        finally:
            self._administer(f"DROP DATABASE IF EXISTS {_quoted(name)} WITH (FORCE)")

    def drop(self) -> None:
        if self.owned:
            self._administer(f"DROP DATABASE IF EXISTS {_quoted(self.name)} WITH (FORCE)")

    def __enter__(self) -> DatabaseTemplate:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.drop()

    def _administer(self, statement: str) -> None:
        connection = psycopg2.connect(self.administrator_dsn)
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(statement)
        finally:
            connection.close()


class _UnopenedDatabase:
    def transaction(self) -> Any:
        raise RuntimeError("a lineage probe runner must not touch a database")


@dataclass(frozen=True, slots=True)
class _Lineage:
    revision: str
    digest: str


class LineageTemplates:
    def __init__(self, administrator_dsn: str) -> None:
        self.administrator_dsn = administrator_dsn
        self._templates: dict[_Lineage, DatabaseTemplate] = {}
        self._lock = threading.RLock()

    @staticmethod
    def lineage_of(runner: Callable[[Any], MigrationRunner]) -> _Lineage:
        declared = runner(_UnopenedDatabase())
        return _Lineage(declared.revision.schema_revision, declared.registry.digest)

    def template(self, runner: Callable[[Any], MigrationRunner]) -> DatabaseTemplate:
        lineage = self.lineage_of(runner)
        with self._lock:
            cached = self._templates.get(lineage)
            if cached is None:
                cached = self._build(lineage, runner)
                self._templates[lineage] = cached
            return cached

    def current(self) -> DatabaseTemplate:
        return self.template(current_runner)

    def adopt_current(self, name: str) -> None:
        with self._lock:
            lineage = self.lineage_of(current_runner)
            if lineage in self._templates:
                raise RuntimeError("the current lineage template is already registered")
            self._templates[lineage] = DatabaseTemplate.adopt(self.administrator_dsn, name)

    def source_for(self, runner: Callable[[Any], MigrationRunner]) -> DatabaseTemplate | None:
        declared = runner(_UnopenedDatabase()).revision
        with self._lock:
            for lineage in sorted(
                self._templates, key=lambda item: (item.revision, item.digest), reverse=True
            ):
                if (
                    lineage.revision < declared.schema_revision
                    and declared.predecessor_digest_for(lineage.revision) == lineage.digest
                ):
                    return self._templates[lineage]
        return None

    def close(self) -> None:
        with self._lock:
            templates, self._templates = tuple(self._templates.values()), {}
        for template in templates:
            template.drop()

    def _build(
        self, lineage: _Lineage, runner: Callable[[Any], MigrationRunner]
    ) -> DatabaseTemplate:
        source = self.source_for(runner)
        if source is None:
            return DatabaseTemplate.build(
                self.administrator_dsn,
                lambda database: BaselineMigrationRunner(database, runner(database)).run(
                    expected_revision=lineage.revision
                ),
            )
        return DatabaseTemplate.build(
            self.administrator_dsn,
            lambda database: runner(database).run(expected_revision=lineage.revision),
            source=source,
        )


_SESSION: dict[str, LineageTemplates] = {}


def process_lineages(administrator_dsn: str) -> LineageTemplates:
    if administrator_dsn.startswith("sqlite:"):
        from tests.fixtures.sqlite_template import SQLiteTemplates

        return SQLiteTemplates()
    lineages = LineageTemplates(administrator_dsn)
    shared = os.environ.get(SHARED_CURRENT_TEMPLATE_ENV)
    if shared:
        lineages.adopt_current(shared)
    return lineages


@contextmanager
def session_lineages(administrator_dsn: str) -> Iterator[LineageTemplates]:
    with _SESSION_LOCK:
        if _SESSION:
            raise RuntimeError("session lineage templates are already open")
        lineages = _SESSION["lineages"] = process_lineages(administrator_dsn)
    try:
        yield lineages
    finally:
        with _SESSION_LOCK:
            _SESSION.clear()
        lineages.close()


def current_lineages() -> LineageTemplates:
    with _SESSION_LOCK:
        lineages = _SESSION.get("lineages")
    if lineages is None:
        raise RuntimeError("session lineage templates are not open")
    return lineages


@contextmanager
def bound_clone(template: DatabaseTemplate) -> Iterator[MigratedDatabase]:
    with template.clone() as clone, pytest.MonkeyPatch.context() as patch:
        patch.setenv(TEST_DATABASE_ENV, clone.dsn)
        yield clone
