"""Exercises the guarded 089.005-to-089.006 Atlas page edge on populated PostgreSQL data.
The predecessor digest, repeat no-op, injected rollback, and namespace collision stay
fail-closed while existing stop and completion records remain byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.atlas_pages import (
    AtlasPageCreateCommand,
    AtlasPageRepository,
)
from astralplane.repositories.completion_wake import CompletionWakeRepository
from astralplane.repositories.notifications import NotificationRepository
from astralplane.repositories.stop_epochs import StopEpochRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_mesh_enrollment_upgrade import populated
from tests.integration.test_session_issuer_upgrade import retained_rows

OWNER = "atlas-upgrade-owner"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.005"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.005"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_005_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_005_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_005_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.005",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.005"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.005"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def atlas_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.006"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.006"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_006_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_006_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_006_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.006",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.006"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.006"
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
    tables = (
        *populated(tx),
        "owner_stop_epoch",
        "completion_subscription",
        "notification_preference",
        "notification_inbox_entry",
        "notification_delivery_identity",
    )
    notifications = NotificationRepository()
    for owner in (OWNER, "atlas-upgrade-foreign"):
        notifications.put_preferences(
            tx,
            owner_id=owner,
            expected_revision=0,
            updated_at=10,
            **{
                f"{channel}_{event}": True
                for channel in ("in_app", "inbox", "push")
                for event in ("completion", "failure", "required_action")
            },
        )
        content = b"synthetic-upgrade-notification-ciphertext"
        entry = notifications.create_entry(
            tx,
            owner_id=owner,
            event_kind="completion",
            source_id="synthetic-source",
            deduplication_key="synthetic-key",
            content_format="synthetic-sealed",
            content_ciphertext=content,
            content_sha256=hashlib.sha256(content).hexdigest(),
            created_at=10,
            retention_expires_at=100,
        )
        notifications.register_delivery_identity(
            tx,
            owner_id=owner,
            entry_id=entry.entry_id,
            delivery_key="synthetic-delivery",
            created_at=11,
        )
    StopEpochRepository().engage(
        tx,
        owner_id=OWNER,
        expected_revision=0,
        reason="synthetic atlas upgrade",
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
    return tables


def page_command(**changes):
    values = dict(
        owner_id=OWNER,
        command_id=str(uuid.uuid4()),
        page_id="40000000-0000-4000-8000-000000000001",
        project_id="upgrade-project",
        slug="upgrade-page",
        sources=("synthetic/upgrade",),
        content=b"synthetic-opaque-upgrade-ciphertext",
        created_at=30,
    )
    values.update(changes)
    return AtlasPageCreateCommand(**values)


def test_populated_089_006_upgrade_preserves_rows_adds_only_atlas_tables(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    report = current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
        "astralplane-089-atlas-pages",
        "astralplane-089-atlas-revisions",
        "astralplane-089-atlas-links",
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
        assert tx.fetch_all("SELECT * FROM atlas_page") == ()
        assert tx.fetch_all("SELECT * FROM atlas_page_command") == ()
        assert tx.fetch_all("SELECT * FROM atlas_page_revision") == ()
        assert tx.fetch_all("SELECT * FROM atlas_page_link") == ()
        assert tx.fetch_all("SELECT * FROM atlas_page_link_head") == ()
        assert tx.fetch_all("SELECT * FROM atlas_page_link_command") == ()
        created = AtlasPageRepository().create_page(tx, command=page_command())
        assert created.page.content == b"synthetic-opaque-upgrade-ciphertext"
    assert (
        current_runner(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with pytest.raises(SchemaRevisionError):
        atlas_runner(db).run(expected_revision="089.006")
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="089.005")


def test_interrupted_atlas_edge_rolls_back_and_retries(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    with pytest.raises(RuntimeError, match="injected atlas failure"), db.transaction() as tx:
        m.PLANE_SCHEMA_089_006_MIGRATION.apply(tx)
        raise RuntimeError("injected atlas failure")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_one("SELECT to_regclass('atlas_page') AS relation")["relation"] is None
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_command') AS relation")["relation"] is None
        )
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.005"
        )
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
        "astralplane-089-atlas-pages",
        "astralplane-089-atlas-revisions",
        "astralplane-089-atlas-links",
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


def test_atlas_namesake_or_damaged_predecessor_is_refused(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute("CREATE TABLE atlas_page (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.005"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('atlas_page_command') AS relation")["relation"] is None
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX atlas_page_live_slug",
        "DROP INDEX atlas_page_live_listing",
        "DROP TRIGGER atlas_page_lifecycle ON atlas_page",
        "DROP TRIGGER atlas_page_command_immutable ON atlas_page_command",
        "ALTER TABLE atlas_page DROP CONSTRAINT atlas_page_lifecycle_shape",
        "DROP INDEX atlas_page_revision_history",
        "DROP TRIGGER atlas_page_revision_immutable ON atlas_page_revision",
        "DROP TRIGGER atlas_page_head_revision ON atlas_page",
        "DROP INDEX atlas_page_link_outbound",
        "DROP TRIGGER atlas_page_link_immutable ON atlas_page_link",
        "DROP TRIGGER atlas_page_link_command_immutable ON atlas_page_link_command",
    ],
)
def test_current_atlas_catalog_damage_is_refused(migrated_clone, corruption):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX notification_inbox_entry_owner_live",
        "ALTER TABLE notification_inbox_entry "
        "DROP CONSTRAINT notification_inbox_entry_deduplication",
    ],
)
def test_damaged_notification_predecessor_is_refused_without_atlas_adoption(
    predecessor, corruption
):
    db = predecessor.database
    with db.transaction() as tx:
        seed_populated(tx)
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision="089.007")
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.005"
        )
        assert tx.fetch_one("SELECT to_regclass('atlas_page') AS relation")["relation"] is None
        assert tx.fetch_one("SELECT count(*) AS count FROM notification_inbox_entry")["count"] == 2
