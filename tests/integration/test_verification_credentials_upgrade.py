"""The guarded verification-credential edge preserves populated predecessor data and refuses
partial or downgraded schemas. Normal registry recovery, repeat verification, bounded
enrollment, and single-use challenge consumption work immediately after the upgrade.
"""

from dataclasses import replace

import pytest

from astralplane import create_verification_credential_repository
from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows

OWNER = "verification-upgrade-owner"
CREDENTIAL = "10000000-0000-4000-8000-0000000000c1"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.018"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.018"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_018_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_018_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_018_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.018",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.018"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.018"
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
        before = retained_rows(tx, tables)
    report = runner(database).run(expected_revision="089.022")
    assert report.applied_steps == (
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
    with database.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_one("SELECT count(*) AS n FROM verification_credential")["n"] == 0
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM verification_challenge_consumption")["n"] == 0
        )
        repo = create_verification_credential_repository()
        enrolled = repo.enroll_credential(
            tx,
            owner_id=OWNER,
            credential_id=CREDENTIAL,
            device_id="synthetic-mac-device",
            algorithm="es256",
            key_format="spki",
            public_key="0" * 64,
        )
        assert enrolled.credential_state == "active"
        consumed = repo.consume_challenge(
            tx, owner_id=OWNER, challenge_digest="a" * 64, consumption_id="upgrade-consumption"
        )
        assert consumed.challenge_digest == "a" * 64
    assert runner(database).run(expected_revision="089.022").applied_steps == ()
    with pytest.raises(SchemaRevisionError):
        prior_runner(database).run(expected_revision="089.018")


def test_interrupted_edge_rolls_back_then_normal_runner_recovers(predecessor):
    def interrupt(tx):
        m.PLANE_SCHEMA_089_019_MIGRATION.operation(tx)
        raise RuntimeError("synthetic interruption")

    registry = m.MigrationRegistry(
        tuple(
            replace(edge, operation=interrupt)
            if edge.target_revision == "089.019"
            else edge
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
            tx.fetch_one("SELECT to_regclass('verification_credential') AS relation")["relation"]
            is None
        )
        assert (
            tx.fetch_one(
                "SELECT to_regclass('verification_challenge_consumption') AS relation"
            )["relation"]
            is None
        )
    assert runner(predecessor.database).run(expected_revision="089.022").applied_steps == (
        "astralplane-089-verification-credentials",
        "astralplane-089-chat-read-receipts",
        "astralplane-089-mesh-event-store",
        "astralplane-089-run-and-learning-ledger",
    )
