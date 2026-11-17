"""Populated 089.004 to 089.005 notification-record upgrade, repeat, rollback, catalog
damage, and namesake refusal against real PostgreSQL. Existing completion, session, and
assignment records must remain byte-equivalent while only the new tables appear.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.database.baseline import BaselineMigrationRunner
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.completion_wake import CompletionWakeRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_mesh_enrollment_upgrade import populated
from tests.integration.test_session_issuer_upgrade import retained_rows


def at_089_004(database, registry=None):
    capped = (
        registry
        if registry is not None
        else m.MigrationRegistry(
            tuple(
                edge
                for edge in m.MIGRATION_REGISTRY.migrations
                if edge.target_revision <= "089.004"
            ),
            current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.004"),
            current_schema_verifier_checksum=m.PLANE_SCHEMA_089_004_SCHEMA_VERIFIER_CHECKSUM,
            predecessor_schema_verifier=m._verify_predecessor_plane_schema,
            predecessor_schema_verifier_checksum=(
                m.PLANE_SCHEMA_089_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
            ),
        )
    )
    assert capped.digest == m.PLANE_SCHEMA_089_004_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.004",
        migration_digest=capped.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.004"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.004"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=capped)


def current(database, registry=None):
    capped = (
        registry
        if registry is not None
        else m.MigrationRegistry(
            tuple(
                edge
                for edge in m.MIGRATION_REGISTRY.migrations
                if edge.target_revision <= "089.005"
            ),
            current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.005"),
            current_schema_verifier_checksum=m.PLANE_SCHEMA_089_005_SCHEMA_VERIFIER_CHECKSUM,
            predecessor_schema_verifier=m._verify_predecessor_plane_schema,
            predecessor_schema_verifier_checksum=m.PLANE_SCHEMA_089_005_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
        )
    )
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.005",
        migration_digest=capped.digest,
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
    return m.MigrationRunner(database, revision=revision, registry=capped)


@pytest.fixture
def predecessor(lineage_templates):
    with bound_clone(lineage_templates.template(at_089_004)) as clone:
        yield clone


@pytest.fixture
def notification_current(lineage_templates):
    with bound_clone(lineage_templates.template(current)) as clone:
        yield clone


def populated_predecessor(db):
    wake = CompletionWakeRepository()
    with db.transaction() as tx:
        tables = populated(tx)
        subscription = wake.register_subscription(
            tx,
            owner_id="notification-upgrade-owner",
            waiter_operation_id="22000000-0000-4000-8000-000000000001",
            waiter_owner_id="notification-upgrade-owner",
            source_operation_id="22000000-0000-4000-8000-000000000002",
            source_owner_id="notification-upgrade-owner",
            terminal_condition="completed",
            source_revision=1,
            current_revision_fence=2,
            created_at=10,
        )
        wake.accept_wake_receipt(
            tx,
            owner_id="notification-upgrade-owner",
            subscription_id=subscription.subscription_id,
            idempotency_key="notification-upgrade-wake",
            observed_terminal="completed",
            observed_revision=2,
            accepted_at=20,
        )
        tables = (*tables, "completion_subscription", "wake_receipt")
        before = retained_rows(tx, tables)
    return tables, before


def test_notification_populated_upgrade_preserves_rows_and_repeats(predecessor):
    db = predecessor.database
    tables, before = populated_predecessor(db)
    report = current(db).run(expected_revision="089.005")
    assert report.applied_steps == ("astralplane-089-notification-records",)
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        for table in (
            "notification_preference",
            "notification_inbox_entry",
            "notification_delivery_identity",
        ):
            assert tx.fetch_all(f"SELECT * FROM {table}") == ()
    assert current(db).run(expected_revision="089.005").already_current
    assert BaselineMigrationRunner(db, current(db)).run(expected_revision="089.005").already_current
    with pytest.raises(SchemaRevisionError):
        at_089_004(db).run(expected_revision="089.004")


def test_notification_partial_migration_rolls_back_and_can_retry(predecessor):
    db = predecessor.database
    tables, before = populated_predecessor(db)

    def interrupted(tx):
        m.PLANE_SCHEMA_089_005_MIGRATION.operation(tx)
        raise RuntimeError("synthetic notification migration interruption")

    registry = m.MigrationRegistry(
        (
            *tuple(
                edge
                for edge in m.MIGRATION_REGISTRY.migrations
                if edge.target_revision <= "089.004"
            ),
            replace(m.PLANE_SCHEMA_089_005_MIGRATION, operation=interrupted),
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.005"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_005_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PLANE_SCHEMA_089_005_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    with pytest.raises(RuntimeError, match="synthetic notification migration interruption"):
        current(db, registry).run(expected_revision="089.005")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert (
            tx.fetch_one("SELECT to_regclass('notification_preference') AS relation")["relation"]
            is None
        )
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.004"
        )
    assert current(db).run(expected_revision="089.005").applied_steps == (
        "astralplane-089-notification-records",
    )


def test_notification_current_catalog_damage_is_refused(notification_current):
    db = notification_current.database
    assert current(db).run(expected_revision="089.005").already_current
    with db.transaction() as tx:
        tx.execute(
            "ALTER TABLE notification_inbox_entry "
            "DROP CONSTRAINT notification_inbox_entry_deduplication"
        )
    with pytest.raises(SchemaRevisionError):
        current(db).run(expected_revision="089.005")


def test_notification_namesake_is_not_adopted(predecessor):
    db = predecessor.database
    populated_predecessor(db)
    with db.transaction() as tx:
        tx.execute("CREATE TABLE notification_preference (owner_id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current(db).run(expected_revision="089.005")
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.004"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('notification_inbox_entry') AS relation")["relation"]
            is None
        )
