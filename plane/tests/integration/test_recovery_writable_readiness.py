"""Real-PostgreSQL acceptance cases for the writable-readiness contract: exact current
schema/digest repeat as a verified no-op, while wrong metadata or catalog damage is refused
without relabeling. Each case starts from a fresh clone of the current migrated template.
"""

from __future__ import annotations

import pytest

from astralplane.database.migrations import (
    CURRENT_DATA_PLANE_REVISION,
    MIGRATION_DIGEST,
    MIGRATION_REGISTRY,
    MigrationRunner,
)
from astralplane.database.revision import SCHEMA_REVISION
from astralplane.errors import SchemaRevisionError


def _runner(database):
    return MigrationRunner(
        database,
        revision=CURRENT_DATA_PLANE_REVISION,
        registry=MIGRATION_REGISTRY,
    )


def _metadata(database):
    with database.transaction() as transaction:
        records = transaction.fetch_all(
            "SELECT key,value FROM schema_meta ORDER BY key"
        )
    return {record["key"]: record["value"] for record in records}


def test_current_catalog_repeat_is_a_verified_noop(migrated_clone):
    report = _runner(migrated_clone.database).run(expected_revision=SCHEMA_REVISION)

    assert report.source_revision == SCHEMA_REVISION
    assert report.target_revision == SCHEMA_REVISION
    assert report.already_current is True
    assert report.applied_steps == ()
    assert report.migration_digest == MIGRATION_DIGEST
    assert report.migration_digest == CURRENT_DATA_PLANE_REVISION.migration_digest
    assert _metadata(migrated_clone.database) == {
        "astralplane_migration_digest": MIGRATION_DIGEST,
        "revision": SCHEMA_REVISION,
    }


def test_wrong_current_digest_is_refused_without_relabelling(migrated_clone):
    damaged_digest = "0" * 64
    with migrated_clone.database.transaction() as transaction:
        transaction.execute(
            "UPDATE schema_meta SET value=%s "
            "WHERE key='astralplane_migration_digest'",
            (damaged_digest,),
        )

    with pytest.raises(SchemaRevisionError, match="different migration-set digest"):
        _runner(migrated_clone.database).run(expected_revision=SCHEMA_REVISION)

    assert _metadata(migrated_clone.database) == {
        "astralplane_migration_digest": damaged_digest,
        "revision": SCHEMA_REVISION,
    }


def test_damaged_catalog_is_refused_without_relabelling(migrated_clone):
    before = _metadata(migrated_clone.database)
    with migrated_clone.database.transaction() as transaction:
        transaction.execute(
            "ALTER TABLE web_session ADD COLUMN unexpected_recovery_field TEXT"
        )

    with pytest.raises(SchemaRevisionError, match="current schema canonical structure"):
        _runner(migrated_clone.database).run(expected_revision=SCHEMA_REVISION)

    assert _metadata(migrated_clone.database) == before