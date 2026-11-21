
import threading
from contextlib import closing, nullcontext
from uuid import uuid4

import psycopg2
import pytest
from astralplane.database.revision import SCHEMA_REVISION

from tests.helpers.plane_template import (
    ENGINE_CLONE_PREFIX,
    DatabaseCreationError,
    TemplateDatabase,
    client_connections,
    cloned_database,
    database_dsn,
    engine_clone,
    engine_runtime,
)


def _database_exists(template, name):
    with closing(psycopg2.connect(template.server_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        return cursor.fetchone() is not None


def _state(runtime):
    with runtime.transaction() as tx:
        meta = {row["key"]: row["value"] for row in tx.fetch_all("SELECT key, value FROM schema_meta")}
        where = tx.fetch_one("SELECT current_database() AS database, current_schema() AS schema")
        marker = tx.fetch_one("SELECT state FROM astralplane_reconciliation_marker "
                              "WHERE schema_revision = %s AND hook_name = 'assignment-engine-test'",
                              (SCHEMA_REVISION,))
    return meta, where, marker


@pytest.mark.parametrize("failure", [False, True])
def test_engine_clone_closes_its_runtime_and_drops_its_clone_on_exit(plane_template, failure):
    refused = pytest.raises(RuntimeError, match="inside the clone") if failure else nullcontext()
    with refused, engine_clone(plane_template) as (runtime, name):
        assert runtime.health().ready and _database_exists(plane_template, name)
        if failure:
            raise RuntimeError("failure inside the clone")
    assert not runtime.health().ready
    assert not _database_exists(plane_template, name)


def test_a_clone_rechecked_under_its_own_identity_is_still_correct(plane_template):
    with cloned_database(plane_template, prefix=ENGINE_CLONE_PREFIX) as name:
        # Not engine_clone: booting under the clone's own identity forces the full verification it skips
        runtime = engine_runtime(plane_template.server_dsn, name)
        try:
            assert not runtime.health().ready
            report = runtime.initialize(expected_revision=SCHEMA_REVISION)
            assert runtime.health().ready and report.identity == name
            assert report.migration.already_current and report.migration.applied_steps == ()
            assert [hook.already_complete for hook in report.reconciliation.hooks] == [True]
        finally:
            runtime.close()


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
            with cloned_database(plane_template, prefix=ENGINE_CLONE_PREFIX) as name:
                created.append(_database_exists(plane_template, name))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=clone, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    assert errors == [] and created == [True, True]


def test_clone_is_dropped_even_with_an_open_connection(plane_template):
    with cloned_database(plane_template, prefix=ENGINE_CLONE_PREFIX) as name:
        leaked = psycopg2.connect(database_dsn(plane_template.server_dsn, name))
    try:
        assert not _database_exists(plane_template, name)
        with pytest.raises(psycopg2.OperationalError), leaked.cursor() as cursor:
            cursor.execute("SELECT 1")
    finally:
        leaked.close()


def test_missing_template_is_a_creation_error(plane_template):
    missing = TemplateDatabase(plane_template.server_dsn, "ad_test_plane_tpl_engine_" + uuid4().hex)
    with pytest.raises(DatabaseCreationError), cloned_database(missing, prefix=ENGINE_CLONE_PREFIX):
        pytest.fail("a clone of a missing template must not be created")
    with pytest.raises(DatabaseCreationError), engine_clone(missing):
        pytest.fail("an engine clone of a missing template must not boot")
