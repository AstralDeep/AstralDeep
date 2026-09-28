"""Proves tests/helpers/voice_plane_runtime.py's isolated runtimes stay isolated and
faithful while cloning its process-wide public-schema template from plane_template.py:
clones hold independent data at the current Plane revision and vanish on exit, and the
sealed template keeps no client connections.
"""

from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

import psycopg2
import pytest
from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION, MIGRATION_REGISTRY

from tests.helpers.plane_template import client_connections, database_dsn
from tests.helpers.voice_plane_runtime import (
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


def test_isolated_runtime_database_is_dropped_on_exit():
    template = voice_plane_template_or_skip()
    with isolated_voice_plane_runtime("voice_clone_drop") as runtime:
        name = _identity(runtime)["database"]
        assert _database_exists(template, name)
    assert not _database_exists(template, name)
    assert voice_plane_template_or_skip() is template


def test_voice_template_is_sealed_without_client_connections():
    template = voice_plane_template_or_skip()
    assert client_connections(template.server_dsn, template.name) == 0
    with pytest.raises(psycopg2.OperationalError, match="not currently accepting connections"):
        psycopg2.connect(database_dsn(template.server_dsn, template.name))
