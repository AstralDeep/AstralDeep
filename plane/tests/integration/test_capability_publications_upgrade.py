"""Populated predecessor upgrades preserve representative Plane records while adding
neutral capability-publication storage. Failed, repeat, damaged and namesake upgrades
remain closed and retryable against the exact guarded registry."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.capability_publications import (
    CapabilityPublicationRepository,
)
from astralplane.repositories.mesh_enrollment import MeshEnrollmentRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_mesh_enrollment_upgrade import populated
from tests.integration.test_session_issuer_upgrade import retained_rows

AT = datetime(2026, 10, 10, tzinfo=UTC)


def at_089_008(database, registry=None):
    capped = (
        registry
        if registry is not None
        else m.MigrationRegistry(
            tuple(
                edge
                for edge in m.MIGRATION_REGISTRY.migrations
                if edge.target_revision <= "089.008"
            ),
            current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.008"),
            current_schema_verifier_checksum=m.PLANE_SCHEMA_089_008_SCHEMA_VERIFIER_CHECKSUM,
            predecessor_schema_verifier=m._verify_predecessor_plane_schema,
            predecessor_schema_verifier_checksum=(
                m.PLANE_SCHEMA_089_008_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
            ),
        )
    )
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.008",
        migration_digest=capped.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.008"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.008"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=capped)


def current(database, registry=m.MIGRATION_REGISTRY):
    return m.MigrationRunner(
        database,
        revision=m.CURRENT_DATA_PLANE_REVISION,
        registry=registry,
    )


@pytest.fixture
def predecessor(lineage_templates):
    with bound_clone(lineage_templates.template(at_089_008)) as clone:
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


def test_populated_upgrade_preserves_rows_and_repeats(predecessor):
    db = predecessor.database
    tables, before = populated_predecessor(db)
    report = current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
        "astralplane-089-capability-publications",
        "astralplane-089-operation-harness-bindings",
        "astralplane-089-push-deliveries",
        "astralplane-089-observation-records",
        "astralplane-089-projects",
        "astralplane-089-harness-captures",
        "astralplane-089-audit-conversation-index",
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_all("SELECT * FROM capability_publication") == ()
    assert (
        current(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with pytest.raises(SchemaRevisionError):
        at_089_008(db).run(expected_revision="089.008")


def test_committed_publication_survives_repeat_and_restart(predecessor):
    db = predecessor.database
    populated_predecessor(db)
    current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    repo = CapabilityPublicationRepository()
    with db.transaction() as tx:
        published = repo.publish(
            tx,
            owner_id="upgrade-owner",
            capability_id="capability-upgrade",
            audience_id="audience-upgrade",
            capability_revision="revision-upgrade",
            metadata_ciphertext="synthetic opaque ciphertext",
            metadata_digest="a" * 64,
            published_at=AT,
            expires_at=AT + timedelta(hours=1),
            expected_revision=0,
        )
    assert (
        current(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with db.transaction() as tx:
        fresh = CapabilityPublicationRepository()
        assert (
            fresh.get(
                tx,
                owner_id="upgrade-owner",
                capability_id="capability-upgrade",
                audience_id="audience-upgrade",
            )
            == published
        )


def test_partial_migration_rolls_back_and_retries(predecessor):
    db = predecessor.database
    tables, before = populated_predecessor(db)

    def interrupted(tx):
        m.PLANE_SCHEMA_089_009_MIGRATION.operation(tx)
        raise RuntimeError("synthetic migration interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupted) if edge.target_revision == "089.009" else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        current(db, registry).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert (
            tx.fetch_one("SELECT to_regclass('capability_publication') AS relation")["relation"]
            is None
        )
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.008"
        )
    assert current(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
        "astralplane-089-capability-publications",
        "astralplane-089-operation-harness-bindings",
        "astralplane-089-push-deliveries",
        "astralplane-089-observation-records",
        "astralplane-089-projects",
        "astralplane-089-harness-captures",
        "astralplane-089-audit-conversation-index",
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )


def test_current_catalog_damage_is_refused(predecessor):
    db = predecessor.database
    current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        tx.execute(
            "ALTER TABLE capability_publication DROP CONSTRAINT capability_publication_state_shape"
        )
    with pytest.raises(SchemaRevisionError):
        current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)


def test_namesake_is_not_adopted(predecessor):
    db = predecessor.database
    populated_predecessor(db)
    with db.transaction() as tx:
        tx.execute("CREATE TABLE capability_publication (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.008"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('capability_publication_owner_live') AS relation")[
                "relation"
            ]
            is None
        )
