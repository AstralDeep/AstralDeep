"""Proves tests/helpers/voice_plane_runtime.py's isolated runtimes stay isolated and
faithful while cloning its process-wide public-schema template from plane_template.py:
clones hold independent data at the current Plane revision and vanish on exit, and the
sealed template keeps no client connections.
"""

from contextlib import closing, nullcontext
from datetime import UTC, datetime
from uuid import uuid4

import psycopg2
import pytest
from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION, MIGRATION_REGISTRY
from astralplane.errors import SchemaRevisionError

from tests.helpers import voice_plane_runtime
from tests.helpers.plane_template import (
    TemplateDatabase,
    client_connections,
    cloned_database,
    database_dsn,
    drop_database,
)
from tests.helpers.voice_plane_runtime import (
    VoicePlaneTestRuntime,
    isolated_voice_plane_runtime,
    voice_plane_template_or_skip,
)


def _identity(runtime):
    return runtime.fetch_one("SELECT current_database() AS database, current_schema() AS schema")


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


def test_isolated_runtimes_are_independent_clones_at_the_current_revision():
    with (isolated_voice_plane_runtime("voice_clone_a") as first,
          isolated_voice_plane_runtime("voice_clone_b") as second):
        one, two = _identity(first), _identity(second)
        assert one["database"].startswith("voice_clone_a_") and two["database"].startswith("voice_clone_b_")
        assert one["schema"] == two["schema"] == "public"
        for runtime in (first, second):
            meta = {row["key"]: row["value"] for row in runtime.fetch_all("SELECT key, value FROM schema_meta")}
            assert meta["revision"] == CURRENT_DATA_PLANE_REVISION.schema_revision
            assert meta["astralplane_migration_digest"] == MIGRATION_REGISTRY.digest
        mine, theirs = _grant(first), _grant(second)
        assert _has_grant(first, mine) and not _has_grant(first, theirs)
        assert _has_grant(second, theirs) and not _has_grant(second, mine)


@pytest.mark.parametrize("failure", [False, True])
def test_isolated_runtime_database_is_dropped_on_exit(failure):
    template = voice_plane_template_or_skip()
    refused = pytest.raises(RuntimeError, match="inside the clone") if failure else nullcontext()
    with refused, isolated_voice_plane_runtime("voice_clone_drop") as runtime:
        name = _identity(runtime)["database"]
        assert _database_exists(template, name)
        if failure:
            raise RuntimeError("failure inside the clone")
    assert runtime._driver_pool.closed
    assert not _database_exists(template, name)
    assert voice_plane_template_or_skip() is template


def test_voice_template_is_sealed_without_client_connections():
    template = voice_plane_template_or_skip()
    assert client_connections(template.server_dsn, template.name) == 0
    with pytest.raises(psycopg2.OperationalError, match="not currently accepting connections"):
        psycopg2.connect(database_dsn(template.server_dsn, template.name))


def test_fresh_template_verifies_once_and_clones_keep_the_verified_schema(monkeypatch):
    voice_plane_template_or_skip()
    monkeypatch.setattr(voice_plane_runtime, "_TEMPLATES", {})
    original = voice_plane_runtime.BaselineMigrationRunner.run
    reports = []

    def verified(runner, **kwargs):
        report = original(runner, **kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(voice_plane_runtime.BaselineMigrationRunner, "run", verified)
    template = voice_plane_template_or_skip()
    try:
        assert len(reports) == 1
        assert client_connections(template.server_dsn, template.name) == 0
        with pytest.raises(psycopg2.OperationalError, match="not currently accepting connections"):
            psycopg2.connect(database_dsn(template.server_dsn, template.name))
        for prefix in ("voice_verified_first", "voice_verified_second"):
            with isolated_voice_plane_runtime(prefix) as runtime:
                assert runtime.fetch_one("SELECT value FROM schema_meta WHERE key = 'revision'")["value"] == (
                    CURRENT_DATA_PLANE_REVISION.schema_revision
                )
                assert _has_grant(runtime, _grant(runtime))
            assert runtime._driver_pool.closed
        assert len(reports) == 1
    finally:
        drop_database(template.server_dsn, template.name)


def test_direct_constructor_migrates_empty_database_and_rejects_schema_drift(monkeypatch):
    template = voice_plane_template_or_skip()
    original = voice_plane_runtime.ThreadedConnectionPool
    pools = []

    def connected(*args, **kwargs):
        pool = original(*args, **kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr(voice_plane_runtime, "ThreadedConnectionPool", connected)
    with cloned_database(TemplateDatabase(template.server_dsn, "template0"), prefix="voice_direct") as name:
        dsn = database_dsn(template.server_dsn, name)
        with closing(VoicePlaneTestRuntime(dsn)) as runtime:
            assert _has_grant(runtime, _grant(runtime))
            assert runtime.fetch_one("SELECT value FROM schema_meta WHERE key = 'revision'")["value"] == (
                CURRENT_DATA_PLANE_REVISION.schema_revision
            )
        assert pools[-1].closed
        with closing(VoicePlaneTestRuntime(dsn)) as runtime:
            runtime.execute("UPDATE schema_meta SET value = %s WHERE key = 'astralplane_migration_digest'",
                            ("invalid-digest",))
        assert pools[-1].closed
        with pytest.raises(SchemaRevisionError):
            VoicePlaneTestRuntime(dsn)
        assert len(pools) == 3 and all(pool.closed for pool in pools)
    assert not _database_exists(template, name)
