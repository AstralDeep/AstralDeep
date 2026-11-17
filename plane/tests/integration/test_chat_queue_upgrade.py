"""The guarded chat-queue edge preserves populated predecessor data and refuses partial
or downgraded schemas. Normal registry recovery, repeat verification and parent retention
remain authoritative, and queued follow-ups dispatch after the upgrade.
"""

from dataclasses import replace

import pytest

from astralplane import create_chat_queue_repository
from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows

ITEM_1 = "10000000-0000-4000-8000-0000000000a1"
ITEM_2 = "10000000-0000-4000-8000-0000000000a2"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.015"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.015"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_015_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PLANE_SCHEMA_089_015_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    assert registry.digest == m.PLANE_SCHEMA_089_015_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.015",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "089.015"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.015"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


@pytest.fixture(scope="module")
def predecessor_template(lineage_templates):
    return lineage_templates.template(prior_runner)


@pytest.fixture
def predecessor(predecessor_template):
    with bound_clone(predecessor_template) as clone:
        yield clone


def runner(database, **changes):
    return m.MigrationRunner(
        database,
        revision=m.CURRENT_DATA_PLANE_REVISION,
        registry=changes.get("registry", m.MIGRATION_REGISTRY),
    )


def test_populated_upgrade_repeat_and_old_reader_denial(predecessor):
    database = predecessor.database
    with database.transaction() as tx:
        tables = seed_populated(tx)
        tx.execute(
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("queue-upgrade-chat", "queue-upgrade-owner", "Synthetic upgrade chat", 10, 10),
        )
        before = retained_rows(tx, tables)
    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_one("SELECT count(*) AS n FROM chat_queue_item")["n"] == 0
        repo = create_chat_queue_repository()
        first = repo.enqueue(
            tx,
            owner_id="queue-upgrade-owner",
            chat_id="queue-upgrade-chat",
            text="survives the upgrade",
            created_at=20,
            item_id=ITEM_1,
        )
        repo.enqueue(
            tx,
            owner_id="queue-upgrade-owner",
            chat_id="queue-upgrade-chat",
            text="runs in order",
            created_at=21,
            item_id=ITEM_2,
        )
        assert repo.claim_next(
            tx, owner_id="queue-upgrade-owner", chat_id="queue-upgrade-chat"
        ) == first
    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.015")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_016_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "089.016" else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        runner(predecessor.database, registry=registry).run(expected_revision="089.022")
    with predecessor.database.transaction() as tx:
        assert (
            tx.fetch_one("SELECT to_regclass('chat_queue_item') AS relation")["relation"] is None
        )
    assert runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
        "astralplane-089-chat-queue",
        "astralplane-089-conversation-branches",
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )


def test_foreign_namesake_and_current_structure_damage_fail_closed(predecessor, migrated_clone):
    with predecessor.database.transaction() as tx:
        tx.execute("CREATE TABLE chat_queue_item(wrong TEXT)")
    with pytest.raises(SchemaRevisionError):
        runner(predecessor.database).run(expected_revision="089.022")
    with migrated_clone.database.transaction() as tx:
        tx.execute("ALTER TABLE chat_queue_item DROP CONSTRAINT chat_queue_item_chat_order")
    with pytest.raises(SchemaRevisionError):
        runner(migrated_clone.database).run(expected_revision="089.022")
