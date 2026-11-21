"""The guarded computer-use edge preserves populated predecessor data and refuses partial
or foreign schemas. Registry recovery, repeat verification, and old-reader denial remain
authoritative.
"""

from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows

COMPUTER_TABLES = (
    "computer_session",
    "computer_task_grant",
    "computer_host_effect_intent",
    "computer_approval_fence",
)


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.017"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.017"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_017_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_017_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_017_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.017",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.017"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.017"
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


def test_populated_upgrade_repeat_and_old_reader_denial(predecessor):
    database = predecessor.database
    with database.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    report = current_runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        for table in COMPUTER_TABLES:
            assert tx.fetch_one(f"SELECT count(*) AS n FROM {table}")["n"] == 0
    assert current_runner(database).run(expected_revision="089.022").already_current
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.017")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_018_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "089.018" else edge
            for edge in m.MIGRATION_REGISTRY.migrations
        ),
        current_schema_verifier=m._verify_current_plane_schema,
        current_schema_verifier_checksum=m.CURRENT_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        current_runner_with(predecessor.database, registry).run(expected_revision="089.022")
    with predecessor.database.transaction() as tx:
        for table in COMPUTER_TABLES:
            assert tx.fetch_one(f"SELECT to_regclass('{table}') AS relation")["relation"] is None
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"]
            == "089.017"
        )
    assert current_runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
        "astralplane-089-computer-use",
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )


def current_runner_with(database, registry):
    return m.MigrationRunner(database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=registry)


def test_foreign_namesake_and_current_index_damage_fail_closed(predecessor, migrated_clone):
    with predecessor.database.transaction() as tx:
        tx.execute("CREATE TABLE computer_session(wrong TEXT)")
    with pytest.raises(SchemaRevisionError):
        current_runner(predecessor.database).run(expected_revision="089.022")
    with migrated_clone.database.transaction() as tx:
        tx.execute("DROP INDEX computer_host_effect_intent_owner")
    with pytest.raises(SchemaRevisionError):
        current_runner(migrated_clone.database).run(expected_revision="089.022")
