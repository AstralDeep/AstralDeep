"""Populated predecessor upgrades preserve mesh, audit and existing liabilities
while adding neutral stop storage. Migration failures and damaged catalogs roll
back and remain closed until the exact registry succeeds."""

from __future__ import annotations

from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.mesh_enrollment import MeshEnrollmentRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_mesh_enrollment_upgrade import populated
from tests.integration.test_session_issuer_upgrade import retained_rows


def predecessor(database):
    registry = m.MigrationRegistry(
        tuple(
            edge
            for edge in m.MIGRATION_REGISTRY.migrations
            if edge.target_revision <= "089.002"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.002"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_002_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PLANE_SCHEMA_089_002_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    assert registry.digest == m.PLANE_SCHEMA_089_002_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.002",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.002"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.002"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def current(database, registry=m.MIGRATION_REGISTRY):
    return m.MigrationRunner(database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=registry)


def at_089_003(database, registry=None):
    capped = (
        registry
        if registry is not None
        else m.MigrationRegistry(
            tuple(
                edge
                for edge in m.MIGRATION_REGISTRY.migrations
                if edge.target_revision <= "089.003"
            ),
            current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.003"),
            current_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
            predecessor_schema_verifier=m._verify_predecessor_plane_schema,
            predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
        )
    )
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.003",
        migration_digest=capped.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.003"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.003"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=capped)


@pytest.fixture
def stop_predecessor(lineage_templates):
    with bound_clone(lineage_templates.template(predecessor)) as clone:
        yield clone


@pytest.fixture
def stop_current(lineage_templates):
    with bound_clone(lineage_templates.template(at_089_003)) as clone:
        yield clone


def populated_predecessor(db):
    with db.transaction() as tx:
        tables = populated(tx)
        MeshEnrollmentRepository().bootstrap_mesh(
            tx,
            owner_id="upgrade-owner",
            mesh_id="mesh-upgrade",
            display_name="synthetic",
            bootstrap_member_id="owner-device",
            bootstrap_member_kind="device",
        )
        tables = (*tables, "mesh_record", "mesh_member")
        before = retained_rows(tx, tables)
    return tables, before


def test_stop_populated_upgrade_preserves_rows_and_repeats(stop_predecessor):
    db = stop_predecessor.database
    tables, before = populated_predecessor(db)
    report = at_089_003(db).run(expected_revision="089.003")
    assert report.applied_steps == ("astralplane-owner-stop-epochs",)
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_all("SELECT * FROM owner_stop_epoch") == ()
        assert tx.fetch_all("SELECT * FROM peer_stop_acknowledgment") == ()
        assert tx.fetch_all("SELECT * FROM owner_stop_operation_epoch") == ()
    assert at_089_003(db).run(expected_revision="089.003").already_current
    runner_089_003 = BaselineMigrationRunner(db, at_089_003(db))
    assert runner_089_003.run(expected_revision="089.003").already_current
    with pytest.raises(SchemaRevisionError):
        predecessor(db).run(expected_revision="089.002")


def test_stop_partial_migration_rolls_back_and_can_retry(stop_predecessor):
    db = stop_predecessor.database
    tables, before = populated_predecessor(db)

    def interrupted(tx):
        m.PLANE_SCHEMA_089_003_MIGRATION.operation(tx)
        raise RuntimeError("synthetic migration interruption")

    registry = m.MigrationRegistry(
        (
            *tuple(
                edge
                for edge in m.MIGRATION_REGISTRY.migrations
                if edge.target_revision <= "089.002"
            ),
            replace(m.PLANE_SCHEMA_089_003_MIGRATION, operation=interrupted),
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        at_089_003(db, registry).run(expected_revision="089.003")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert (
            tx.fetch_one("SELECT to_regclass('owner_stop_epoch') AS relation")["relation"] is None
        )
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.002"
        )
    assert at_089_003(db).run(expected_revision="089.003").applied_steps == (
        "astralplane-owner-stop-epochs",
    )


def test_stop_current_catalog_damage_is_refused(stop_current):
    db = stop_current.database
    assert at_089_003(db).run(expected_revision="089.003").already_current
    with db.transaction() as tx:
        tx.execute("ALTER TABLE owner_stop_epoch DROP CONSTRAINT owner_stop_epoch_state")
    with pytest.raises(SchemaRevisionError):
        at_089_003(db).run(expected_revision="089.003")


def test_stop_namesake_is_not_adopted(stop_predecessor):
    db = stop_predecessor.database
    _, _ = populated_predecessor(db)
    with db.transaction() as tx:
        tx.execute("CREATE TABLE owner_stop_epoch (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        at_089_003(db).run(expected_revision="089.003")
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.002"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('peer_stop_acknowledgment') AS relation")["relation"]
            is None
        )
