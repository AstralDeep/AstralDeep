"""Exercises the guarded 089.011-to-089.012 tool observation edge on populated
PostgreSQL data. The predecessor digest, repeat no-op, injected rollback, and namespace
collision stay fail-closed while existing atlas, stop, and completion records remain
byte-for-byte unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.atlas_pages import AtlasPageCreateCommand, AtlasPageRepository
from astralplane.repositories.completion_wake import CompletionWakeRepository
from astralplane.repositories.observations import ObservationCaptureCommand, ObservationRepository
from astralplane.repositories.stop_epochs import StopEpochRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_mesh_enrollment_upgrade import populated
from tests.integration.test_session_issuer_upgrade import retained_rows

OWNER = "observation-upgrade-owner"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.011"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.011"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_011_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_011_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_011_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.011",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.011"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.011"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def current_runner(database):
    return m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=m.MIGRATION_REGISTRY
    )


@pytest.fixture(scope="module")
def predecessor_template(lineage_templates):
    return lineage_templates.template(prior_runner)


@pytest.fixture
def predecessor(predecessor_template):
    with bound_clone(predecessor_template) as clone:
        yield clone


def seed_populated(tx):
    tables = (*populated(tx), "owner_stop_epoch", "completion_subscription", "atlas_page")
    StopEpochRepository().engage(
        tx,
        owner_id=OWNER,
        expected_revision=0,
        reason="synthetic observation upgrade",
        actor_id=OWNER,
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    CompletionWakeRepository().register_subscription(
        tx,
        owner_id=OWNER,
        waiter_operation_id="30000000-0000-4000-8000-000000000001",
        waiter_owner_id=OWNER,
        source_operation_id="30000000-0000-4000-8000-000000000002",
        source_owner_id=OWNER,
        terminal_condition="completed",
        source_revision=1,
        current_revision_fence=1,
        created_at=10,
        subscription_id="30000000-0000-4000-8000-000000000003",
    )
    AtlasPageRepository().create_page(
        tx,
        command=AtlasPageCreateCommand(
            owner_id=OWNER,
            command_id="40000000-0000-4000-8000-000000000001",
            page_id="40000000-0000-4000-8000-000000000002",
            project_id="upgrade-project",
            slug="upgrade-page",
            sources=("synthetic/upgrade",),
            content=b"synthetic-opaque-atlas-ciphertext",
            created_at=20,
        ),
    )
    return tables


def capture_command(**changes):
    values = dict(
        owner_id=OWNER,
        conversation_id="upgrade-conversation",
        observation_id="50000000-0000-4000-8000-000000000001",
        operation_id="upgrade-operation",
        source_refs=("synthetic/upgrade-observation",),
        seal="upgrade-seal-v1",
        content=b"synthetic-opaque-observation-ciphertext",
        created_at=30,
        expires_at=40,
    )
    values.update(changes)
    return ObservationCaptureCommand(**values)


def test_populated_089_012_upgrade_preserves_rows_adds_only_observation_tables(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    report = current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
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
        assert tx.fetch_all("SELECT * FROM tool_observation") == ()
        assert tx.fetch_all("SELECT * FROM tool_observation_page") == ()
        captured = ObservationRepository().capture(tx, command=capture_command())
        assert not captured.replayed
        assert captured.observation.observation_octets == len(
            b"synthetic-opaque-observation-ciphertext"
        )
    assert (
        current_runner(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="089.011")


def test_interrupted_observation_edge_rolls_back_and_retries(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    with pytest.raises(RuntimeError, match="injected observation failure"), db.transaction() as tx:
        m.PLANE_SCHEMA_089_012_MIGRATION.apply(tx)
        raise RuntimeError("injected observation failure")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert (
            tx.fetch_one("SELECT to_regclass('tool_observation') AS relation")["relation"] is None
        )
        assert (
            tx.fetch_one("SELECT to_regclass('tool_observation_page') AS relation")["relation"]
            is None
        )
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.011"
        )
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == ("astralplane-089-observation-records", "astralplane-089-projects",
                            "astralplane-089-harness-captures",
                            "astralplane-089-audit-conversation-index",
                            "astralplane-089-chat-queue",
                            "astralplane-089-conversation-branches",
                            "astralplane-089-computer-use",
                            "astralplane-089-verification-credentials",
                            "astralplane-089-chat-read-receipts",
                            "astralplane-089-mesh-event-store",
                            "astralplane-089-run-and-learning-ledger")


def test_observation_namesake_or_damaged_predecessor_is_refused(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute("CREATE TABLE tool_observation (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.011"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('tool_observation_page') AS relation")["relation"]
            is None
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX tool_observation_recall",
        "DROP INDEX tool_observation_expiry",
        "DROP TRIGGER tool_observation_lifecycle ON tool_observation",
        "DROP TRIGGER tool_observation_delete ON tool_observation",
        "DROP TRIGGER tool_observation_page_immutable ON tool_observation_page",
        "DROP TRIGGER tool_observation_page_purge ON tool_observation_page",
        "ALTER TABLE tool_observation DROP CONSTRAINT tool_observation_lifecycle_shape",
        "ALTER TABLE tool_observation DROP CONSTRAINT tool_observation_retention_window",
    ],
)
def test_current_observation_catalog_damage_is_refused(migrated_clone, corruption):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
