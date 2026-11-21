"""The guarded chat-read-receipt edge preserves populated predecessor data and refuses
partial or downgraded schemas. Normal registry recovery, repeat verification and parent
retention remain authoritative, and receipts mark and batch after the upgrade.
"""

from dataclasses import replace

import pytest

from astralplane import create_chat_read_receipt_repository
from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.history import ConversationRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.019"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.019"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_019_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_019_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_019_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.019",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "089.019"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.019"
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
    repo = create_chat_read_receipt_repository()
    with database.transaction() as tx:
        tables = seed_populated(tx)
        conversations = ConversationRepository()
        conversations.create(
            tx,
            owner_id="receipt-upgrade-owner",
            conversation_id="receipt-upgrade-chat",
            title="Synthetic upgrade chat",
            agent_id=None,
            created_at=10,
        )
        conversations.create(
            tx,
            owner_id="receipt-upgrade-owner",
            conversation_id="receipt-upgrade-second",
            title="Synthetic upgrade chat two",
            agent_id=None,
            created_at=11,
        )
        before = retained_rows(tx, tables)
    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_one("SELECT count(*) AS n FROM chat_read_receipt")["n"] == 0
        states = repo.list_receipts(tx, owner_id="receipt-upgrade-owner")
        assert [state.receipt for state in states] == [None, None]
        first = repo.mark_seen(
            tx,
            owner_id="receipt-upgrade-owner",
            chat_id="receipt-upgrade-chat",
            last_seen_position=4,
            expected_revision=0,
        )
        assert first == replace(first, revision=1) and first.revision == 1
        assert repo.mark_seen(
            tx,
            owner_id="receipt-upgrade-owner",
            chat_id="receipt-upgrade-chat",
            last_seen_position=2,
            expected_revision=first.revision,
        ) == first
        assert [
            (state.conversation_id, state.receipt)
            for state in repo.list_receipts(tx, owner_id="receipt-upgrade-owner")
        ] == [
            ("receipt-upgrade-second", None),
            ("receipt-upgrade-chat", first),
        ]
        assert ConversationRepository().delete(
            tx,
            owner_id="receipt-upgrade-owner",
            conversation_id="receipt-upgrade-chat",
        )
        conversations.create(
            tx,
            owner_id="receipt-upgrade-owner",
            conversation_id="receipt-upgrade-chat",
            title="Synthetic re-created chat",
            agent_id=None,
            created_at=12,
        )
        assert (
            repo.get(tx, owner_id="receipt-upgrade-owner", chat_id="receipt-upgrade-chat") is None
        )
    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.019")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_020_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "089.020" else edge
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
            tx.fetch_one("SELECT to_regclass('chat_read_receipt') AS relation")["relation"] is None
        )
    assert runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )


def test_foreign_namesake_and_current_structure_damage_fail_closed(predecessor, migrated_clone):
    with predecessor.database.transaction() as tx:
        tx.execute("CREATE TABLE chat_read_receipt(wrong TEXT)")
    with pytest.raises(SchemaRevisionError):
        runner(predecessor.database).run(expected_revision="089.022")
    with migrated_clone.database.transaction() as tx:
        tx.execute(
            "ALTER TABLE chat_read_receipt DROP CONSTRAINT chat_read_receipt_chat_identity"
        )
    with pytest.raises(SchemaRevisionError):
        runner(migrated_clone.database).run(expected_revision="089.022")
