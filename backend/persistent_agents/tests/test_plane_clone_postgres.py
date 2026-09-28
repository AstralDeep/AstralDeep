"""Proves tests/helpers/plane_template.py's engine_clone, behind test_engine_postgres.py's
`plane` fixture, keeps per-test isolation and fidelity while booting each fresh clone of the
session template under the template's verified boot identity: clones are ready at
SCHEMA_REVISION on their own databases, hold independent data, survive other clones closing
or booting concurrently, close and drop with their context, and still verify correctly when
re-checked from scratch.
"""

import threading
from contextlib import closing, nullcontext
from datetime import UTC, datetime
from uuid import uuid4

import psycopg2
import pytest
from astralplane.database.migrations import MIGRATION_REGISTRY
from astralplane.database.revision import SCHEMA_REVISION
from astralplane.errors import InitializationError

from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.helpers.plane_template import (
    ENGINE_CLONE_PREFIX,
    ENGINE_SCHEMA,
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


def _state(runtime):
    with runtime.transaction() as tx:
        meta = {row["key"]: row["value"] for row in tx.fetch_all("SELECT key, value FROM schema_meta")}
        where = tx.fetch_one("SELECT current_database() AS database, current_schema() AS schema")
        marker = tx.fetch_one("SELECT state FROM astralplane_reconciliation_marker "
                              "WHERE schema_revision = %s AND hook_name = 'assignment-engine-test'",
                              (SCHEMA_REVISION,))
    return meta, where, marker


def test_clone_is_ready_on_its_own_database_with_the_templates_verified_boot(plane, plane_template):
    report = plane.initialize(expected_revision=SCHEMA_REVISION)
    assert plane.health().ready
    assert report.identity == plane_template.name
    assert report.expected_revision == SCHEMA_REVISION
    assert report.migration.source_revision is None, (
        "canary: Plane stopped reusing the boot state it caches per identity, "
        "so each engine clone now re-verifies the template's schema")
    assert report.migration.target_revision == SCHEMA_REVISION
    meta, where, marker = _state(plane)
    assert meta["revision"] == SCHEMA_REVISION
    assert meta["astralplane_migration_digest"] == MIGRATION_REGISTRY.digest
    assert where["schema"] == ENGINE_SCHEMA
    assert where["database"].startswith(f"{ENGINE_CLONE_PREFIX}_") and where["database"] != plane_template.name
    assert marker["state"] == "completed"


def test_writes_through_one_clone_are_invisible_through_another(plane, plane_template):
    with engine_clone(plane_template) as (other, _name):
        assert other.health().ready
        mine, theirs = _grant(plane), _grant(other)
        assert _has_grant(plane, mine) and not _has_grant(plane, theirs)
        assert _has_grant(other, theirs) and not _has_grant(other, mine)


def test_closing_a_clone_runtime_does_not_break_a_later_clone(plane_template):
    with engine_clone(plane_template) as (first, _first_name):
        earlier = _grant(first)
    assert not first.health().ready
    with pytest.raises(InitializationError), first.transaction():
        pytest.fail("a closed clone runtime must not open a transaction")
    with engine_clone(plane_template) as (second, second_name):
        assert second.health().ready
        assert not _has_grant(second, earlier)
        assert _has_grant(second, _grant(second))
        assert _state(second)[1]["database"] == second_name


@pytest.mark.parametrize("failure", [False, True])
def test_engine_clone_closes_its_runtime_and_drops_its_clone_on_exit(plane_template, failure):
    refused = pytest.raises(RuntimeError, match="inside the clone") if failure else nullcontext()
    with refused, engine_clone(plane_template) as (runtime, name):
        assert runtime.health().ready and _database_exists(plane_template, name)
        if failure:
            raise RuntimeError("failure inside the clone")
    assert not runtime.health().ready
    assert not _database_exists(plane_template, name)


def test_concurrent_clone_runtimes_boot_ready_and_stay_isolated(plane_template):
    workers_count = 4
    written = threading.Barrier(workers_count)
    grants, errors, observations = {}, [], []

    def run(index):
        try:
            with engine_clone(plane_template) as (runtime, name):
                grants[index] = _grant(runtime)
                written.wait(timeout=30)
                observations.append((index, runtime.health().ready, _state(runtime)[1]["database"] == name,
                                     {other: _has_grant(runtime, grant) for other, grant in grants.items()}))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index,), daemon=True) for index in range(workers_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == [] and len(observations) == workers_count
    for index, ready, own_database, seen in observations:
        assert ready and own_database
        assert seen == {other: other == index for other in range(workers_count)}


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
