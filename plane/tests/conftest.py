"""Session-wide PostgreSQL fixtures: one lineage-template cache per pytest process, from which the
current-revision template and every predecessor lineage are cloned per test or per module.
Built on tests/fixtures/migrated_template.py; skipped when ASTRALPLANE_TEST_POSTGRES_DSN is unset.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.fixtures.migrated_template import (
    DatabaseTemplate,
    LineageTemplates,
    MigratedDatabase,
    bound_clone,
    session_lineages,
)
from tests.fixtures.pre_split.loader import TEST_DATABASE_ENV


@pytest.fixture(autouse=True)
def backend_specific_assertions(request):
    if (
        os.environ.get(TEST_DATABASE_ENV, "").startswith("sqlite:")
        and request.node.get_closest_marker("postgresql_schema_catalog") is not None
    ):
        pytest.skip("PostgreSQL schema/catalog assertion; SQLite has a fresh guarded baseline")
    if (
        os.environ.get(TEST_DATABASE_ENV, "").startswith("sqlite:")
        and request.node.get_closest_marker("postgresql_lock_catalog") is not None
    ):
        pytest.skip("PostgreSQL wait catalogs; SQLite locking is verified in test_sqlite.py")


@pytest.fixture(scope="session", autouse=True)
def _session_lineage_templates() -> Iterator[LineageTemplates | None]:
    dsn = os.environ.get(TEST_DATABASE_ENV)
    if not dsn:
        yield None
        return
    with session_lineages(dsn) as lineages:
        yield lineages


@pytest.fixture(scope="session")
def lineage_templates(
    _session_lineage_templates: LineageTemplates | None,
) -> LineageTemplates:
    if _session_lineage_templates is None:
        pytest.skip(f"{TEST_DATABASE_ENV} is required for PostgreSQL integration tests")
    return _session_lineage_templates


@pytest.fixture(scope="session")
def postgres_administrator_dsn(lineage_templates: LineageTemplates) -> str:
    return lineage_templates.administrator_dsn


@pytest.fixture(scope="session")
def migrated_template(lineage_templates: LineageTemplates) -> DatabaseTemplate:
    return lineage_templates.current()


@pytest.fixture
def migrated_clone(migrated_template: DatabaseTemplate) -> Iterator[MigratedDatabase]:
    with bound_clone(migrated_template) as clone:
        yield clone
