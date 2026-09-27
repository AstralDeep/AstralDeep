"""Isolated PostgreSQL-backed AstralPlane runtime builder for voice, history, and
work-admission integration tests: clones a throwaway database from a public-schema
template that plane_template.py migrates once per process, and exposes seed/query helpers.
"""

from __future__ import annotations

import atexit
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from typing import Any

import psycopg2
import pytest
from psycopg2.pool import ThreadedConnectionPool

from astralplane import create_repository_catalog
from astralplane.database import ConnectionPool, PlaneDatabase
from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from tests.helpers.plane_template import (
    DatabaseCreationError,
    TemplateDatabase,
    cloned_database,
    create_template,
    database_dsn,
    drop_database,
)

_TEMPLATES: dict[str, TemplateDatabase] = {}
_TEMPLATES_LOCK = threading.Lock()


def build_test_database_url() -> str:
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured
    host = os.getenv("DB_HOST", "localhost")
    if host.strip().lower() == "localhost":
        host = "127.0.0.1"
    return psycopg2.extensions.make_dsn(
        host=host,
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "astraldeep"),
        user=os.getenv("DB_USER", "astral"),
        password=os.getenv("DB_PASSWORD", "astral_dev"),
    )


def _native_statement(statement: str) -> str:
    return statement.replace("?", "%s")


class VoicePlaneTestRuntime:
    def __init__(self, database_url: str) -> None:
        # Pool margin above the 15-contender capacity test's peak
        self._driver_pool = ThreadedConnectionPool(1, 20, database_url)
        self._pool = ConnectionPool(self._driver_pool)
        self._database = PlaneDatabase(self._pool)
        self.repositories = create_repository_catalog()
        self.plane_runtime = self
        self.plane_repositories = self.repositories
        try:
            BaselineMigrationRunner(
                self._database,
                MigrationRunner(
                    self._database,
                    revision=CURRENT_DATA_PLANE_REVISION,
                    registry=MIGRATION_REGISTRY,
                ),
            ).run(expected_revision=CURRENT_DATA_PLANE_REVISION.schema_revision)
        except BaseException:
            self._pool.close()
            raise

    @contextmanager
    def transaction(self, *, isolation: Any = None) -> Iterator[Any]:
        with self._database.transaction(isolation=isolation) as transaction:
            yield transaction

    def execute(
        self,
        statement: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> Any:
        with self.transaction() as transaction:
            return transaction.execute(_native_statement(statement), parameters)

    def fetch_one(
        self,
        statement: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> Any:
        with self.transaction() as transaction:
            return transaction.fetch_one(_native_statement(statement), parameters)

    def fetch_all(
        self,
        statement: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> tuple[Any, ...]:
        with self.transaction() as transaction:
            return transaction.fetch_all(_native_statement(statement), parameters)

    def close(self) -> None:
        self._pool.close()


def _migrate_template(template: TemplateDatabase) -> None:
    VoicePlaneTestRuntime(database_dsn(template.server_dsn, template.name)).close()


def voice_plane_template() -> TemplateDatabase:
    admin_params = psycopg2.extensions.parse_dsn(build_test_database_url())
    admin_params["dbname"] = "postgres"
    admin_dsn = psycopg2.extensions.make_dsn(**admin_params)
    with _TEMPLATES_LOCK:
        template = _TEMPLATES.get(admin_dsn)
        if template is None:
            template = create_template(
                admin_dsn, prefix="voice_template", migrate=_migrate_template
            )
            # Callers are plain context managers, so no pytest session teardown can drop it
            atexit.register(drop_database, admin_dsn, template.name)
            _TEMPLATES[admin_dsn] = template
        return template


@contextmanager
def isolated_voice_plane_runtime(prefix: str) -> Iterator[VoicePlaneTestRuntime]:
    with ExitStack() as cleanup:
        try:
            template = voice_plane_template()
            database = cleanup.enter_context(cloned_database(template, prefix=prefix))
        except DatabaseCreationError as exc:  # pragma: no cover
            pytest.skip(f"cannot create isolated PostgreSQL database: {exc}")
        runtime = VoicePlaneTestRuntime(database_dsn(template.server_dsn, database))
        cleanup.callback(runtime.close)
        yield runtime


def ensure_voice_plane_runtime(runtime: VoicePlaneTestRuntime) -> VoicePlaneTestRuntime:
    if not hasattr(runtime.repositories, "voice"):
        raise TypeError("voice repository is missing from the Plane catalog")
    return runtime


def voice_session_repository(runtime: VoicePlaneTestRuntime, **kwargs: object) -> Any:
    from orchestrator.voice_sessions import VoiceSessionRepository

    runtime = ensure_voice_plane_runtime(runtime)
    return VoiceSessionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        **kwargs,
    )


def plane_work_admission_repository(runtime: VoicePlaneTestRuntime) -> Any:
    from orchestrator.work_admission import PlaneWorkAdmissionRepository

    runtime = ensure_voice_plane_runtime(runtime)
    return PlaneWorkAdmissionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def history_manager(runtime: VoicePlaneTestRuntime) -> Any:
    from orchestrator.history import HistoryManager

    runtime = ensure_voice_plane_runtime(runtime)
    return HistoryManager(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


PlaneTestRuntime = VoicePlaneTestRuntime
isolated_plane_runtime = isolated_voice_plane_runtime


__all__ = (
    "PlaneTestRuntime",
    "VoicePlaneTestRuntime",
    "build_test_database_url",
    "ensure_voice_plane_runtime",
    "history_manager",
    "isolated_plane_runtime",
    "isolated_voice_plane_runtime",
    "plane_work_admission_repository",
    "voice_plane_template",
    "voice_session_repository",
)
