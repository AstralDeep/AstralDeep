"""Proves test_engine_postgres.py's `plane` fixture keeps per-test isolation and fidelity
while cloning tests/helpers/plane_template.py's session template: clones hold independent
data, start ready at SCHEMA_REVISION without replaying migrations, and the sealed template
keeps no client connections.
"""

import threading
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

import psycopg2
import pytest
from astralplane.database.revision import SCHEMA_REVISION

from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.helpers.plane_template import (
    ENGINE_SCHEMA,
    DatabaseCreationError,
    TemplateDatabase,
    client_connections,
    cloned_database,
    database_dsn,
    engine_runtime,
)


def _database_exists(template, name):
    with closing(psycopg2.connect(template.server_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        return cursor.fetchone() is not None


def _grant(runtime):
    grant_id = str(uuid4())
    now = int(datetime.now(UTC).timestamp() * 1000)
    with runtime.transaction() as tx:
        runtime.repositories.offline_grants.create_grant(
            tx, grant_id=grant_id, owner_id="owner", agent_id=None,
            encrypted_refresh_token=b"clone-isolation-token", issued_at=now, expires_at=now + 60000)
    return grant_id


def _has_grant(runtime, grant_id):
    with runtime.transaction() as tx:
        return runtime.repositories.offline_grants.get_grant(
            tx, owner_id="owner", grant_id=grant_id) is not None


def test_clone_is_ready_at_schema_revision_without_replaying_migrations(plane):
    report = plane.initialize(expected_revision=SCHEMA_REVISION)
    assert plane.health().ready
    assert report.migration.target_revision == SCHEMA_REVISION
    assert report.migration.already_current and report.migration.applied_steps == ()
    assert [hook.already_complete for hook in report.reconciliation.hooks] == [True]
    with plane.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key = 'revision'")["value"] == SCHEMA_REVISION
        assert tx.fetch_one("SELECT current_schema() AS name")["name"] == ENGINE_SCHEMA


def test_two_clones_hold_independent_data(plane, plane_template):
    with cloned_database(plane_template, prefix="ad_test_plane_clone_engine") as name:
        other = engine_runtime(plane_template.server_dsn, name)
        try:
            other.initialize(expected_revision=SCHEMA_REVISION)
            assert other.health().ready
            mine, theirs = _grant(plane), _grant(other)
            assert _has_grant(plane, mine) and not _has_grant(plane, theirs)
            assert _has_grant(other, theirs) and not _has_grant(other, mine)
        finally:
            other.close()


def test_template_is_sealed_without_client_connections(plane_template):
    assert client_connections(plane_template.server_dsn, plane_template.name) == 0
    with pytest.raises(psycopg2.OperationalError, match="not currently accepting connections"):
        psycopg2.connect(database_dsn(plane_template.server_dsn, plane_template.name))


def test_concurrent_clones_of_the_sealed_template_succeed(plane_template):
    start = threading.Barrier(2)
    created, errors = [], []

    def clone():
        try:
            start.wait(timeout=10)
            with cloned_database(plane_template, prefix="ad_test_plane_clone_engine") as name:
                created.append(_database_exists(plane_template, name))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=clone) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    assert errors == [] and created == [True, True]


def test_clone_is_dropped_even_with_an_open_connection(plane_template):
    with cloned_database(plane_template, prefix="ad_test_plane_clone_engine") as name:
        leaked = psycopg2.connect(database_dsn(plane_template.server_dsn, name))
    try:
        assert not _database_exists(plane_template, name)
        with pytest.raises(psycopg2.OperationalError), leaked.cursor() as cursor:
            cursor.execute("SELECT 1")
    finally:
        leaked.close()


def test_missing_template_is_a_creation_error(plane_template):
    missing = TemplateDatabase(plane_template.server_dsn, "ad_test_plane_tpl_engine_" + uuid4().hex)
    with pytest.raises(DatabaseCreationError), cloned_database(missing, prefix="ad_test_plane_clone_engine"):
        pytest.fail("a clone of a missing template must not be created")
