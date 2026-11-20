"""Exercises the guarded 089.009-to-089.010 harness-binding edge on populated PostgreSQL
data. Legacy operations remain unbound until an owner/fenced write, and interrupted DDL,
namesake objects, and current-catalog damage stay fail-closed.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from astralplane.repositories.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationOwner,
    OperationRequest,
    OwnerScope,
    WorkAdmissionRepository,
)
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_atlas_pages_upgrade import seed_populated
from tests.integration.test_session_issuer_upgrade import retained_rows

OWNER = "harness-upgrade-owner"
DIGEST = "e" * 64


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.009"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "089.009"),
        current_schema_verifier_checksum=m.PLANE_SCHEMA_089_009_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            m.PLANE_SCHEMA_089_009_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    )
    assert registry.digest == m.PLANE_SCHEMA_089_009_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="089.009",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            value
            for value in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from
            if value < "089.009"
        ),
        accepted_predecessor_digests=tuple(
            pair
            for pair in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if pair[0] < "089.009"
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


def _configs() -> tuple[AdmissionClassConfig, ...]:
    return (
        AdmissionClassConfig(AdmissionClass.GLOBAL, None, 4, 0, 0, "harness-upgrade"),
        AdmissionClassConfig(
            AdmissionClass.BACKGROUND, AdmissionClass.GLOBAL, 4, 0, 0, "harness-upgrade"
        ),
    )


def _seed_running_operation(tx):
    work = WorkAdmissionRepository()
    work.configure(tx, _configs())
    work.bind_configs(_configs())
    owner = OperationOwner(OwnerScope.USER, OWNER, None)
    accepted = work.submit(
        tx,
        OperationRequest(
            operation_kind="harness_turn",
            admission_class=AdmissionClass.BACKGROUND,
            owner=owner,
            submission_id=uuid.uuid4(),
            idempotency_namespace=None,
            idempotency_key=None,
            normalized_input_digest=None,
            chat_id=None,
            parent_operation_id=None,
            connection_generation=None,
            request_generation=None,
        ),
        now=None,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=10),
    )
    claim = work.claim_operation(
        tx,
        AdmissionClass.BACKGROUND,
        accepted.operation_id,
        now=None,
        retention=timedelta(days=1),
        slot_lease=timedelta(minutes=10),
    )
    assert claim is not None
    return owner, claim.fence, accepted.operation_id


def test_populated_089_010_upgrade_preserves_rows_and_binds_only_on_request(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        owner, fence, operation_id = _seed_running_operation(tx)
        before = retained_rows(tx, tables)
    report = current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    assert report.applied_steps == (
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
        assert retained_rows(tx, tables) == before
        assert tx.fetch_all("SELECT * FROM operation_harness_binding") == ()
        work = WorkAdmissionRepository()
        assert work.get_harness_profile_binding(tx, owner, operation_id) is None
        identity = HarnessProfileIdentity("standard", "1.0.0", DIGEST)
        binding = work.bind_harness_profile(tx, owner, fence, identity)
        assert binding.operation_id == operation_id
        assert binding.identity == identity
        assert work.get_harness_profile_binding(tx, owner, operation_id) == binding
    assert (
        current_runner(db)
        .run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
        .already_current
    )
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="089.009")


def test_interrupted_harness_binding_edge_rolls_back_and_retries(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    with (
        pytest.raises(RuntimeError, match="injected harness binding failure"),
        db.transaction() as tx,
    ):
        m.PLANE_SCHEMA_089_010_MIGRATION.apply(tx)
        raise RuntimeError("injected harness binding failure")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert (
            tx.fetch_one("SELECT to_regclass('operation_harness_binding') AS relation")["relation"]
            is None
        )
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.009"
        )
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
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


def test_harness_binding_namesake_predecessor_is_refused(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute("CREATE TABLE operation_harness_binding (operation_id UUID PRIMARY KEY)")
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == (
            "089.009"
        )
        assert (
            tx.fetch_one("SELECT to_regclass('operation_harness_binding') AS relation")["relation"]
            is not None
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX operation_harness_binding_profile",
        "DROP TRIGGER operation_harness_binding_immutable ON operation_harness_binding",
        "ALTER TABLE operation_harness_binding "
        "DROP CONSTRAINT operation_harness_binding_artifact_digest_check",
        "ALTER TABLE operation_harness_binding "
        "DROP CONSTRAINT operation_harness_binding_execution_generation_check",
        "ALTER TABLE operation_harness_binding "
        "DROP CONSTRAINT operation_harness_binding_operation_id_fkey",
        "ALTER TABLE operation_harness_binding ALTER COLUMN profile DROP NOT NULL",
    ],
)
def test_current_harness_binding_catalog_damage_is_refused(migrated_clone, corruption):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
