"""Tests for astralplane.database.migrations: the mesh-enrollment schema upgrade adds
only its own neutral tables, preserves representative predecessor rows exactly, and
refuses replay or predecessor digests that no longer qualify.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.history import SessionRecord, SessionRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_session_issuer_upgrade import load_liabilities, retained_rows

OWNER = "mesh-enrollment-upgrade-owner"


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(e for e in m.MIGRATION_REGISTRY.migrations if e.target_revision <= "089.001"),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.001"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_001_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_001_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_001_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.001",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "089.001"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "089.001"
        ),
    )
    return m.MigrationRunner(database, revision=revision, registry=registry)


def current_runner(database):
    return m.MigrationRunner(
        database, revision=m.CURRENT_DATA_PLANE_REVISION, registry=m.MIGRATION_REGISTRY
    )


def populated(tx):
    tables = load_liabilities(tx)
    now = int(tx.fetch_one("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now")["now"])
    session = SessionRepository().put(
        tx,
        SessionRecord(
            f"{uuid.uuid4()}",
            OWNER,
            "synthetic-encrypted-access",
            "synthetic-encrypted-refresh",
            now,
            now + 3600,
            now,
            False,
            now,
        ),
    )
    assert session.owner_id == OWNER
    return tuple(dict.fromkeys((*tables, "web_session")))


@pytest.fixture
def predecessor(lineage_templates):
    with bound_clone(lineage_templates.template(prior_runner)) as clone:
        yield clone


def test_populated_089_001_upgrade_keeps_rows_and_adds_only_mesh_tables(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = populated(tx)
        before = retained_rows(tx, tables)
    report = current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    )
    assert report.applied_steps == (
"astralplane-089-mesh-enrollment-records",
        "astralplane-owner-stop-epochs",
        "astralplane-089-completion-wake",
        "astralplane-089-notification-records",
        "astralplane-089-atlas-pages",
        "astralplane-089-atlas-revisions",
        "astralplane-089-atlas-links",
        "astralplane-089-capability-publications",
        "astralplane-089-operation-harness-bindings",
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
    with db.transaction() as tx:
        after = retained_rows(tx, tables)
        assert after == before
        for table in (
            "mesh_record",
            "mesh_member",
            "mesh_public_identity",
            "mesh_enrollment_challenge",
            "mesh_enrollment_invitation",
            "mesh_member_revocation",
        ):
            assert tx.fetch_all(f"SELECT * FROM {table}") == ()
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).already_current
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="089.001")
