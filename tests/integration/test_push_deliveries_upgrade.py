"""Populated 089.010 to 089.011 push-destination upgrade, repeat, interruption, namesake,
and current-catalog damage checks against real PostgreSQL. Existing notification, session,
assignment, and Atlas records must remain unchanged while only the new tables appear.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.atlas_pages import (
    AtlasPageCreateCommand,
    AtlasPageRepository,
)
from astralplane.repositories.notifications import NotificationRepository
from astralplane.repositories.push_deliveries import PushDeliveryRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_mesh_enrollment_upgrade import populated
from tests.integration.test_session_issuer_upgrade import retained_rows

OWNER = "push-upgrade-owner"
ENTRY_ID = "50000000-0000-4000-8000-000000000001"
DESTINATION_ID = "50000000-0000-4000-8000-000000000002"


def at_089_010(database, registry=None):
    capped = (
        registry
        if registry is not None
        else m.MigrationRegistry(
            tuple(
                edge
                for edge in m.MIGRATION_REGISTRY.migrations
                if edge.target_revision <= "089.010"
            ),
            current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.010"),
            current_schema_verifier_checksum=m.PLANE_SCHEMA_089_010_SCHEMA_VERIFIER_CHECKSUM,
            predecessor_schema_verifier=m._verify_predecessor_plane_schema,
            predecessor_schema_verifier_checksum=(
                m.PLANE_SCHEMA_089_010_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
            ),
        )
    )
    assert capped.digest == m.PLANE_SCHEMA_089_010_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.010",
        migration_digest=capped.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.010"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.010"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=capped)


def current(database):
    return m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=m.MIGRATION_REGISTRY
    )


@pytest.fixture
def predecessor(lineage_templates):
    with bound_clone(lineage_templates.template(at_089_010)) as clone:
        yield clone


def populated_predecessor(db):
    notifications = NotificationRepository()
    atlas = AtlasPageRepository()
    with db.transaction() as tx:
        tables = (*populated(tx), "notification_inbox_entry", "notification_delivery_identity")
        body = b"synthetic-upgrade-push-notification"
        entry = notifications.create_entry(
            tx,
            owner_id=OWNER,
            event_kind="completion",
            source_id="synthetic-upgrade-source",
            deduplication_key="synthetic-upgrade-push",
            content_format="astral.notification/v1",
            content_ciphertext=body,
            content_sha256=hashlib.sha256(body).hexdigest(),
            created_at=10,
            retention_expires_at=100,
            entry_id=ENTRY_ID,
        )
        notifications.register_delivery_identity(
            tx,
            owner_id=OWNER,
            entry_id=entry.entry_id,
            delivery_key="synthetic-upgrade-delivery",
            created_at=11,
        )
        atlas.create_page(
            tx,
            command=AtlasPageCreateCommand(
                owner_id=OWNER,
                command_id="50000000-0000-4000-8000-000000000003",
                page_id="50000000-0000-4000-8000-000000000004",
                project_id="upgrade-project",
                slug="upgrade-page",
                sources=("synthetic/upgrade",),
                content=b"synthetic-upgrade-atlas-content",
                created_at=12,
            ),
        )
        tables = (*tables, "atlas_page", "atlas_page_command")
        before = retained_rows(tx, tables)
    return tables, before, entry


def test_populated_push_upgrade_preserves_rows_and_supports_claim_acknowledgment(
    predecessor,
):
    db = predecessor.database
    tables, before, entry = populated_predecessor(db)
    report = current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
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
    repo = PushDeliveryRepository()
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_all("SELECT * FROM notification_device_destination") == ()
        assert tx.fetch_all("SELECT * FROM notification_push_delivery") == ()
        token = b"synthetic-upgrade-push-token"
        destination = repo.register_destination(
            tx,
            owner_id=OWNER,
            destination_key="upgrade-device",
            token_format="astral.push-token/v1",
            token_ciphertext=token,
            token_sha256=hashlib.sha256(token).hexdigest(),
            created_at=20,
            destination_id=DESTINATION_ID,
        )
        delivery = repo.enqueue_delivery(
            tx,
            owner_id=OWNER,
            entry_id=entry.entry_id,
            destination_id=destination.destination_id,
            delivery_key="upgrade-push-delivery",
            expected_destination_revision=destination.revision,
            expected_token_version=destination.token_version,
            max_attempts=2,
            available_at=21,
            created_at=21,
        )
        claimed = repo.claim_deliveries(
            tx,
            owner_id=OWNER,
            worker_id="upgrade-worker",
            now=22,
            lease_expires_at=60,
            limit=1,
        )[0]
        assert claimed.delivery.delivery_id == delivery.delivery_id
        assert claimed.token_ciphertext == token
        assert (
            repo.acknowledge_delivery(
                tx,
                owner_id=OWNER,
                delivery_id=delivery.delivery_id,
                claim_id=claimed.delivery.claim_id,
                acknowledged_at=23,
            ).status
            == "delivered"
        )
    assert (
        current(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    assert (
        BaselineMigrationRunner(db, current(db))
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with pytest.raises(SchemaRevisionError):
        at_089_010(db).run(expected_revision="089.010")


def test_interrupted_push_edge_rolls_back_and_can_retry(predecessor):
    db = predecessor.database
    tables, before, _ = populated_predecessor(db)

    def interrupted(tx):
        m.PLANE_SCHEMA_089_011_MIGRATION.operation(tx)
        raise RuntimeError("synthetic push migration interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupted) if edge.target_revision == "089.011" else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    with pytest.raises(RuntimeError, match="synthetic push migration interruption"):
        m.MigrationRunner(db, revision=m.CURRENT_DATA_PLANE_REVISION, registry=registry).run(
            expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
        )
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert (
            tx.fetch_one("SELECT to_regclass('notification_device_destination') AS relation")[
                "relation"
            ]
            is None
        )
        assert (
            tx.fetch_one("SELECT to_regclass('notification_push_delivery') AS relation")["relation"]
            is None
        )
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.010"
        )
    assert current(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
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


def test_push_namesake_is_refused_before_adoption(predecessor):
    db = predecessor.database
    populated_predecessor(db)
    with db.transaction() as tx:
        tx.execute("CREATE TABLE notification_device_destination (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.010"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('notification_push_delivery') AS relation")["relation"]
            is None
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX notification_device_destination_owner_live",
        "DROP INDEX notification_push_delivery_owner_claimable",
        "ALTER TABLE notification_push_delivery "
        "DROP CONSTRAINT notification_push_delivery_attempts",
        "ALTER TABLE notification_device_destination "
        "DROP CONSTRAINT notification_device_destination_token_shape",
    ],
)
def test_current_push_catalog_damage_is_refused(migrated_clone, corruption):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
