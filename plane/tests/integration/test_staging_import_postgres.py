"""Tests for scripts/migrate_qualification_database.py: an isolated import upgrades
through the normal migration registry, refuses a missing schema, and never replaces
existing public data.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import make_dsn

from astralplane import MIGRATION_DIGEST, SCHEMA_REVISION
from scripts import migrate_qualification_database as migration_driver
from tests.fixtures.pre_split.loader import fixture_digest, verify_blob_fixture
from tests.integration.test_pre_split_upgrade import _representative_snapshot
from tests.test_staging_import import driver


@pytest.fixture
def isolated_database() -> Iterator[tuple[str, str]]:
    administrator_dsn = os.environ.get("ASTRALPLANE_TEST_POSTGRES_DSN")
    if not administrator_dsn:
        pytest.skip("ASTRALPLANE_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")
    qualification_id = uuid.uuid4().hex
    database = f"astralplane_qualification_{qualification_id}"
    administrator = psycopg2.connect(administrator_dsn)
    administrator.autocommit = True
    try:
        with administrator.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{database}" TEMPLATE template0')
        yield qualification_id, make_dsn(administrator_dsn, dbname=database)
    finally:
        with administrator.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        administrator.close()


def test_import_upgrades_through_normal_registry_and_preserves_records(
    isolated_database: tuple[str, str], tmp_path: Path
) -> None:
    qualification_id, database_url = isolated_database
    blob_root = tmp_path / "imported-blobs"
    report = driver.import_fixture(
        database_url=database_url,
        qualification_id=qualification_id,
        expected_fixture_sha256=fixture_digest(),
        blob_root=blob_root,
    )
    configured_dsn = make_dsn(database_url, options=report["database_options"])
    connection = psycopg2.connect(configured_dsn)
    try:
        before = _representative_snapshot(connection)
        blobs = verify_blob_fixture(blob_root)
        for attempt in range(2):
            migrated = migration_driver.migrate(
                database_url=database_url,
                qualification_id=qualification_id,
                expected_revision=SCHEMA_REVISION,
                expected_digest=MIGRATION_DIGEST,
            )
            assert migrated["already_current"] is bool(attempt)
            assert migrated["migration_digest"] == MIGRATION_DIGEST
            assert migrated["target_revision"] == SCHEMA_REVISION
            assert migrated["product_reconciliation_completed"] is False
            assert _representative_snapshot(connection) == before
            assert verify_blob_fixture(blob_root) == blobs
        with pytest.raises(driver.QualificationImportError, match="private schemas"):
            driver.import_fixture(
                database_url=database_url,
                qualification_id=qualification_id,
                expected_fixture_sha256=fixture_digest(),
                blob_root=tmp_path / "must-not-exist",
            )
        assert not (tmp_path / "must-not-exist").exists()
    finally:
        connection.close()


def test_migration_refuses_missing_schema(isolated_database: tuple[str, str]) -> None:
    qualification_id, database_url = isolated_database
    with pytest.raises(ValueError, match="database/schema"):
        migration_driver.migrate(
            database_url=database_url,
            qualification_id=qualification_id,
            expected_revision=SCHEMA_REVISION,
            expected_digest=MIGRATION_DIGEST,
        )


def test_existing_public_data_is_not_replaced(
    isolated_database: tuple[str, str], tmp_path: Path
) -> None:
    qualification_id, database_url = isolated_database
    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE existing_data (value text)")
            cursor.execute("INSERT INTO existing_data VALUES ('retained')")
        connection.commit()
        with pytest.raises(driver.QualificationImportError, match="not empty"):
            driver.import_fixture(
                database_url=database_url,
                qualification_id=qualification_id,
                expected_fixture_sha256=fixture_digest(),
                blob_root=tmp_path / "must-not-exist",
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT value FROM existing_data")
            assert cursor.fetchall() == [("retained",)]
        assert not (tmp_path / "must-not-exist").exists()
    finally:
        connection.close()
