"""Checks isolated completed-result clones against real PostgreSQL receipts and audit chains.
Exercises fresh reader authority, independent mutations and cleanup around the shared authenticated producer.
"""

from dataclasses import replace

import pytest

from tests.helpers.plane_template import client_connections, engine_clone, populated_engine_template


def test_populated_seed_closes_runtime_and_drops_database_when_producer_fails(plane_template):
    observed = {}

    def failed(runtime):
        observed["runtime"] = runtime
        with runtime.transaction() as tx:
            observed["database"] = tx.fetch_one("SELECT current_database() AS name")["name"]
        raise ValueError("synthetic producer failure")

    with pytest.raises(ValueError, match="producer failure"):
        with populated_engine_template(plane_template, failed):
            pytest.fail("failed producer exposed a clone")
    assert client_connections(plane_template.server_dsn, observed["database"]) == 0
    with pytest.raises(Exception):
        with observed["runtime"].transaction():
            pytest.fail("failed producer left an open runtime")
    from tests.helpers.plane_template import DatabaseCreationError
    with pytest.raises(DatabaseCreationError):
        with engine_clone(replace(plane_template, name=observed["database"])):
            pytest.fail("failed producer database was retained")
