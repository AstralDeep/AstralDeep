"""The guarded capture edge preserves populated predecessor data and refuses partial or
downgraded schemas.
Normal registry recovery, repeat verification and parent retention remain authoritative.
"""

from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_harness_bindings_upgrade import _seed_running_operation
from tests.integration.test_session_issuer_upgrade import retained_rows


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.013"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.013"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_013_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=m.PLANE_SCHEMA_089_013_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    )
    assert registry.digest == m.PLANE_SCHEMA_089_013_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.013",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            v for v in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if v < "089.013"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.013"
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
        _seed_running_operation(tx)
        before = retained_rows(tx, tables)
    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
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
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_harness_capture")["n"] == 0
    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.013")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_014_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt) if edge.target_revision == "089.014" else edge
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
            tx.fetch_one("SELECT to_regclass('operation_harness_capture') AS relation")["relation"]
            is None
        )
    assert runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
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


def test_foreign_namesake_and_current_trigger_damage_fail_closed(predecessor, migrated_clone):
    with predecessor.database.transaction() as tx:
        tx.execute("CREATE TABLE operation_harness_capture(wrong TEXT)")
    with pytest.raises(SchemaRevisionError):
        runner(predecessor.database).run(expected_revision="089.022")
    with migrated_clone.database.transaction() as tx:
        tx.execute("DROP TRIGGER assignment_harness_capture_immutable ON persistent_assignment")
    with pytest.raises(SchemaRevisionError):
        runner(migrated_clone.database).run(expected_revision="089.022")
