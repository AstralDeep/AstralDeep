"""The guarded run and learning ledger edge preserves populated predecessor data, installs
empty history tables beside the mesh event store, and refuses partial, same-name or downgraded
schemas. Normal registry recovery and repeat verification remain authoritative, and leases,
goals and ledger entries work after the upgrade.
"""

from dataclasses import replace

import pytest

from astralplane import (
    create_event_roster_repository,
    create_learning_ledger_repository,
    create_run_repository,
)
from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows
from tests.repositories._mesh_support import ref


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.021"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.021"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_021_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_021_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_021_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.021",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "089.021"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.021"
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
        mesh = (
            create_event_roster_repository()
            .create_mesh(
                tx,
                owner_id="run-upgrade-owner",
                kind="genesis",
                signed_bytes=b"upgrade-genesis",
                recorded_at=1,
            )
            .mesh_id
        )
        before = retained_rows(tx, tables)
    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == ("astralplane-089-run-and-learning-ledger",)
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        for table in ("run_lease_entry", "run_goal_transition", "learning_ledger_entry"):
            assert tx.fetch_one(f"SELECT count(*) AS n FROM {table}")["n"] == 0
        lease = create_run_repository().record_lease(
            tx,
            owner_id="run-upgrade-owner",
            mesh_id=mesh,
            run_id="run-1",
            kind="take",
            executor_feed_id="phone",
            state="running",
            event_id=ref(b"lease"),
            recorded_at=2,
        )
        assert lease.epoch == 1
        proposal = create_learning_ledger_repository().record_proposal(
            tx,
            owner_id="run-upgrade-owner",
            mesh_id=mesh,
            event_id=ref(b"proposal"),
            item_kind="skill",
            content_hash=ref(b"content"),
            scope="project",
            evidence=[ref(b"turn")],
            recorded_at=3,
        )
        assert proposal.kind == "proposal"
    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.021")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_022_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "089.022" else edge
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
        assert tx.fetch_one("SELECT to_regclass('run_lease_entry') AS relation")["relation"] is None
    assert runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
        "astralplane-089-run-and-learning-ledger",
    )


@pytest.mark.parametrize(
    "namesake",
    (
        "CREATE TABLE run_lease_entry(wrong TEXT)",
        "CREATE TABLE learning_ledger_entry(wrong TEXT)",
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
        "ALTER TABLE run_lease_entry DROP CONSTRAINT run_lease_entry_first_take",
        "DROP INDEX uq_learning_ledger_entry_adoption",
        "ALTER TABLE learning_ledger_entry DROP CONSTRAINT learning_ledger_entry_review_shape",
    ),
)
def test_current_structure_damage_fails_closed(migrated_clone, damage):
    with migrated_clone.database.transaction() as tx:
        tx.execute(damage)
    with pytest.raises(SchemaRevisionError):
        runner(migrated_clone.database).run(expected_revision="089.022")
