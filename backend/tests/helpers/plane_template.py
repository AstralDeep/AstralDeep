"""Migrates a throwaway AstralPlane PostgreSQL database once, seals it against connections,
and hands each test its own CREATE DATABASE … TEMPLATE clone instead of a full migration
replay; engine clone runtimes share the template's verified boot identity, so their
initialize() does not re-verify the byte-identical schema. The session `plane_template`
fixture backs test_engine_postgres.py's `plane`, and voice_plane_runtime.py seals its own.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from astralplane.api import PlaneRuntime, create_postgres_runtime
from astralplane.database.revision import SCHEMA_REVISION

ENGINE_SCHEMA = "assignment_engine"
_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_DRAIN_SECONDS = 10.0


class DatabaseCreationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TemplateDatabase:
    server_dsn: str = field(repr=False)
    name: str


class EngineReconciler:
    name = "assignment-engine-test"
    version = "1"

    def reconcile(self, context: object) -> dict[str, str]:
        return {"fixture": "isolated-assignment-engine"}


def database_dsn(server_dsn: str, database: str) -> str:
    from psycopg2.extensions import make_dsn

    return make_dsn(server_dsn, dbname=database)


def engine_runtime(server_dsn: str, database: str, *, identity: str) -> PlaneRuntime:
    from psycopg2.extensions import make_dsn

    return create_postgres_runtime(
        make_dsn(server_dsn, dbname=database,
                 options=f"-csearch_path={ENGINE_SCHEMA},pg_catalog"),
        identity=identity,
        reconcilers=(EngineReconciler(),),
        maximum_connections=8,
    )


def engine_clone_runtime(template: TemplateDatabase, database: str) -> PlaneRuntime:
    # Plane keeps boot state per identity; every clone is a byte copy of the sealed, verified template
    return engine_runtime(template.server_dsn, database, identity=template.name)


@contextmanager
def _autocommit_cursor(dsn: str) -> Iterator[Any]:
    import psycopg2

    with closing(psycopg2.connect(dsn)) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            yield cursor


def _unique_name(prefix: str) -> str:
    name = f"{prefix}_{uuid.uuid4().hex}"
    if _DATABASE_NAME.fullmatch(name) is None:
        raise ValueError("test database name is outside the safe identifier contract")
    return name


def _create_database(server_dsn: str, database: str, template: str) -> None:
    import psycopg2
    from psycopg2 import sql

    try:
        with _autocommit_cursor(server_dsn) as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(database), sql.Identifier(template)))
    except psycopg2.Error as exc:
        raise DatabaseCreationError(type(exc).__name__) from exc


def drop_database(server_dsn: str, database: str) -> None:
    from psycopg2 import sql

    with _autocommit_cursor(server_dsn) as cursor:
        cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(database)))


def client_connections(server_dsn: str, database: str) -> int:
    with _autocommit_cursor(server_dsn) as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = %s AND backend_type = 'client backend'",
            (database,),
        )
        return cursor.fetchone()[0]


def _seal(template: TemplateDatabase) -> None:
    from psycopg2 import sql

    with _autocommit_cursor(template.server_dsn) as cursor:
        cursor.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
            sql.Identifier(template.name)))
    deadline = time.monotonic() + _DRAIN_SECONDS
    while client_connections(template.server_dsn, template.name):
        if time.monotonic() >= deadline:
            raise RuntimeError("template database kept client connections after migration")
        time.sleep(0.05)


def create_template(
    server_dsn: str,
    *,
    prefix: str,
    migrate: Callable[[TemplateDatabase], None],
) -> TemplateDatabase:
    template = TemplateDatabase(server_dsn, _unique_name(prefix))
    _create_database(server_dsn, template.name, "template0")
    try:
        migrate(template)
        _seal(template)
    except BaseException:
        drop_database(server_dsn, template.name)
        raise
    return template


@contextmanager
def cloned_database(template: TemplateDatabase, *, prefix: str) -> Iterator[str]:
    clone = _unique_name(prefix)
    _create_database(template.server_dsn, clone, template.name)
    try:
        yield clone
    finally:
        drop_database(template.server_dsn, clone)


def _migrate_engine(template: TemplateDatabase) -> None:
    from psycopg2 import sql

    with _autocommit_cursor(database_dsn(template.server_dsn, template.name)) as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(ENGINE_SCHEMA)))
    runtime = engine_runtime(template.server_dsn, template.name, identity=template.name)
    try:
        runtime.initialize(expected_revision=SCHEMA_REVISION)
        if not runtime.health().ready:
            raise RuntimeError("engine template runtime is not ready after migration")
    finally:
        runtime.close()


@pytest.fixture(scope="session")
def plane_template() -> Iterator[TemplateDatabase]:
    server_dsn = os.environ.get("ASTRALPLANE_TEST_POSTGRES_DSN")
    if not server_dsn:
        pytest.skip("isolated ASTRALPLANE_TEST_POSTGRES_DSN required")
    template = create_template(server_dsn, prefix="ad_test_plane_tpl_engine", migrate=_migrate_engine)
    try:
        yield template
    finally:
        drop_database(server_dsn, template.name)


__all__ = (
    "ENGINE_SCHEMA",
    "DatabaseCreationError",
    "EngineReconciler",
    "TemplateDatabase",
    "client_connections",
    "cloned_database",
    "create_template",
    "database_dsn",
    "drop_database",
    "engine_clone_runtime",
    "engine_runtime",
    "plane_template",
)
