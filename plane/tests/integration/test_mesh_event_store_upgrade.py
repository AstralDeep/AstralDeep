"""The guarded mesh event store edge preserves populated predecessor data, adds the event
link columns to chats and messages without rewriting rows, and refuses partial, same-name or
downgraded schemas. Normal registry recovery and repeat verification remain authoritative,
and events, rosters and derived chats work after the upgrade.
"""

from dataclasses import replace

import pytest

from astralplane import (
    create_derived_chat_repository,
    create_event_log_repository,
    create_event_roster_repository,
)
from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.history import ConversationRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows
from tests.repositories._mesh_support import append_chain, ref


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.020"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.020"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_020_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_020_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_020_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.020",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "089.020"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.020"
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
        ConversationRepository().create(
            tx,
            owner_id="event-upgrade-owner",
            conversation_id="event-upgrade-chat",
            title="Synthetic upgrade chat",
            agent_id=None,
            created_at=10,
        )
        before = retained_rows(tx, tables)
    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        for table in (
            "event_mesh",
            "event_feed",
            "event_entry",
            "event_fork_evidence",
            "event_body",
            "event_roster_entry",
            "event_checkpoint",
            "event_checkpoint_countersignature",
        ):
            assert tx.fetch_one(f"SELECT count(*) AS n FROM {table}")["n"] == 0
        assert tx.fetch_one("SELECT count(*) AS n FROM chats WHERE event_id IS NOT NULL")["n"] == 0
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM messages WHERE event_id IS NOT NULL")["n"] == 0
        )
        mesh = (
            create_event_roster_repository()
            .create_mesh(
                tx,
                owner_id="event-upgrade-owner",
                kind="genesis",
                signed_bytes=b"upgrade-genesis",
                recorded_at=1,
            )
            .mesh_id
        )
        results = append_chain(tx, mesh, 2, owner="event-upgrade-owner")
        assert [r.event.seq for r in results] == [1, 2]
        derived = create_derived_chat_repository()
        derived.upsert_chat(
            tx,
            owner_id="event-upgrade-owner",
            event_id=ref(b"upgrade-chat"),
            chat_id=ref(b"upgrade-chat"),
            title="Derived after upgrade",
            agent_id=None,
            created_at=20,
        )
        assert (
            create_event_log_repository()
            .list_feed_heads(tx, owner_id="event-upgrade-owner", mesh_id=mesh)[0]
            .head_seq
            == 2
        )
    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.020")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_021_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "089.021" else edge
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
        assert tx.fetch_one("SELECT to_regclass('event_entry') AS relation")["relation"] is None
        assert (
            tx.fetch_one(
                "SELECT count(*) AS n FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'messages' "
                "AND column_name = 'event_id'"
            )["n"]
            == 0
        )
    assert runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )


@pytest.mark.parametrize(
    "namesake",
    (
        "CREATE TABLE event_feed(wrong TEXT)",
        "ALTER TABLE messages ADD COLUMN event_id TEXT",
        "ALTER TABLE chats ADD COLUMN event_id TEXT",
    ),
)
def test_foreign_namesakes_fail_closed(predecessor, namesake):
    with predecessor.database.transaction() as tx:
        tx.execute(namesake)
    with pytest.raises(SchemaRevisionError):
        runner(predecessor.database).run(expected_revision="089.022")


@pytest.mark.parametrize(
    "damage",
    (
        "ALTER TABLE event_entry DROP CONSTRAINT event_entry_chain_shape",
        "ALTER TABLE messages DROP CONSTRAINT messages_event_id_unique",
        "DROP INDEX idx_event_entry_scope_clock",
    ),
)
def test_current_structure_damage_fails_closed(migrated_clone, damage):
    with migrated_clone.database.transaction() as tx:
        tx.execute(damage)
    with pytest.raises(SchemaRevisionError):
        runner(migrated_clone.database).run(expected_revision="089.022")
