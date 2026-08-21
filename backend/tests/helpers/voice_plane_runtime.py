"""Isolated PostgreSQL + AstralPlane composition for voice integration tests."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.pool import ThreadedConnectionPool

from astralplane import create_repository_catalog
from astralplane.database import ConnectionPool, PlaneDatabase
from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_REGISTRY,
    MigrationRunner,
)

_DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def build_test_database_url() -> str:
    """Resolve the ordinary Deep PostgreSQL test target without legacy code."""

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
    """Adapt legacy test-seed placeholders to Plane's native psycopg contract."""

    return statement.replace("?", "%s")


class VoicePlaneTestRuntime:
    """Current Plane runtime/catalog plus bounded test-only seed operations."""

    def __init__(self, database_url: str) -> None:
        # The five-owner capacity proof intentionally launches 15 contenders.
        # Keep a small fixed margin without approaching the host RAM guard.
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
        """Seed or perturb an integration fixture in one committed transaction."""

        with self.transaction() as transaction:
            return transaction.execute(_native_statement(statement), parameters)

    def fetch_one(
        self,
        statement: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> Any:
        """Read one detached integration-fixture record."""

        with self.transaction() as transaction:
            return transaction.fetch_one(_native_statement(statement), parameters)

    def fetch_all(
        self,
        statement: str,
        parameters: Sequence[object] | Mapping[str, object] = (),
    ) -> tuple[Any, ...]:
        """Read detached integration-fixture records."""

        with self.transaction() as transaction:
            return transaction.fetch_all(_native_statement(statement), parameters)

    def close(self) -> None:
        self._pool.close()


@contextmanager
def isolated_voice_plane_runtime(prefix: str) -> Iterator[VoicePlaneTestRuntime]:
    """Create, migrate, and safely remove one isolated PostgreSQL database."""

    base_params = psycopg2.extensions.parse_dsn(build_test_database_url())
    admin_params = dict(base_params)
    admin_params["dbname"] = "postgres"
    database_name = f"{prefix}_{uuid.uuid4().hex}"
    if not _DATABASE_NAME.fullmatch(database_name):
        raise ValueError("isolated voice database name is outside the safe contract")

    created = False
    runtime: VoicePlaneTestRuntime | None = None
    try:
        try:
            admin = psycopg2.connect(**admin_params)
            admin.autocommit = True
            try:
                with admin.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                            sql.Identifier(database_name)
                        )
                    )
            finally:
                admin.close()
            created = True
        except psycopg2.Error as exc:  # pragma: no cover - environment gate
            pytest.skip(
                "cannot create isolated PostgreSQL database: "
                f"{type(exc).__name__}"
            )

        fixture_params = dict(base_params)
        fixture_params["dbname"] = database_name
        runtime = VoicePlaneTestRuntime(
            psycopg2.extensions.make_dsn(**fixture_params)
        )
        yield runtime
    finally:
        if runtime is not None:
            runtime.close()
        if created:
            admin = psycopg2.connect(**admin_params)
            admin.autocommit = True
            try:
                with admin.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (database_name,),
                    )
                    cursor.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(
                            sql.Identifier(database_name)
                        )
                    )
            finally:
                admin.close()


def ensure_voice_plane_runtime(runtime: VoicePlaneTestRuntime) -> VoicePlaneTestRuntime:
    """Validate and return the already-composed application Plane runtime."""

    if not hasattr(runtime.repositories, "voice"):
        raise TypeError("voice repository is missing from the Plane catalog")
    return runtime


def voice_session_repository(runtime: VoicePlaneTestRuntime, **kwargs: object) -> Any:
    """Construct Deep's coordinator over the test-owned Plane runtime."""

    from orchestrator.voice_sessions import VoiceSessionRepository

    runtime = ensure_voice_plane_runtime(runtime)
    return VoiceSessionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        **kwargs,
    )


def plane_work_admission_repository(runtime: VoicePlaneTestRuntime) -> Any:
    """Construct Deep work-admission policy over the same Plane transaction seam."""

    from orchestrator.work_admission import PlaneWorkAdmissionRepository

    runtime = ensure_voice_plane_runtime(runtime)
    return PlaneWorkAdmissionRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def history_manager(runtime: VoicePlaneTestRuntime) -> Any:
    """Build HistoryManager over the current application Plane seam."""

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
    "voice_session_repository",
)
