"""Exercises populated project upgrades, rollback, and catalog refusal on PostgreSQL.
Historical observation rows and chat identities survive the guarded registry edge.
"""

from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_observations_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows
from tests.test_projects import create


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.012"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.012"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_012_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PLANE_SCHEMA_089_012_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    assert registry.digest == m.PLANE_SCHEMA_089_012_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.012",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            x for x in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if x < "089.012"
        ),
        accepted_predecessor_digests=tuple(
            x
            for x in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if x[0] < "089.012"
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


def test_populated_upgrade_repeat_and_old_binary_refusal(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    assert current_runner(db).run(expected_revision="089.022").applied_steps == (
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
        assert tx.fetch_all("SELECT * FROM chat_project_membership") == ()
        create(tx)
    assert current_runner(db).run(expected_revision="089.022").already_current
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="089.012")


def test_interrupted_upgrade_rolls_back_and_retries(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    with pytest.raises(RuntimeError), db.transaction() as tx:
        m.PLANE_SCHEMA_089_013_MIGRATION.apply(tx)
        raise RuntimeError("synthetic interruption")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_one("SELECT to_regclass('chat_project') AS relation")["relation"] is None
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "089.012"
        )
    assert current_runner(db).run(expected_revision="089.022").applied_steps == (
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


@pytest.mark.parametrize(
    "corruption",
    [
        "CREATE TABLE chat_project (owner_id TEXT PRIMARY KEY)",
        "ALTER TABLE chats DROP CONSTRAINT chats_pkey CASCADE",
    ],
)
def test_namesake_or_damaged_predecessor_refused(predecessor, corruption):
    with predecessor.database.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(predecessor.database).run(expected_revision="089.022")


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX chat_project_order",
        "DROP INDEX chat_project_membership_order",
        "ALTER TABLE chat_project DROP CONSTRAINT chat_project_revision_check",
        "ALTER TABLE chat_project_membership DROP CONSTRAINT chat_project_membership_project",
        "ALTER TABLE chat_project_membership DROP CONSTRAINT chat_project_membership_chat",
        "ALTER TABLE chats DROP CONSTRAINT chats_owner_identity CASCADE",
    ],
)
def test_current_catalog_damage_refused(migrated_clone, corruption):
    with migrated_clone.database.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(migrated_clone.database).run(expected_revision="089.022")
