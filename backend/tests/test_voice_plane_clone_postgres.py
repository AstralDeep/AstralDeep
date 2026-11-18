"""Proves tests/helpers/voice_plane_runtime.py's isolated runtimes stay isolated and
faithful while cloning its process-wide public-schema template from plane_template.py:
clones hold independent data at the current Plane revision and vanish on exit, and the
sealed template keeps no client connections.
"""

from contextlib import closing, nullcontext

import psycopg2
import pytest

from tests.helpers.plane_template import (
    client_connections,
    database_dsn,
)
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


