"""Tests for astralplane.database.migrations: populated upgrade of the
declarative-agents schema edge, preserving executable agent rows and refusing corrupt
or missing predecessors.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.agents import AgentRepository
from astralplane.repositories.drafts import DraftAgentRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_session_issuer_upgrade import load_liabilities, retained_rows


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "088.003"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "088.003"),
        current_schema_verifier_checksum="6db6549b6c1260fca5dd896a9bc8919aa9bd0e84e46e752c19f3c6bad00dd4d4",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="4c4cc5ac430c53c45dc13cbf7b969e2c2bd4c60c82eda44500a3265bdc89fe15",
    )
    assert registry.digest == m.PLANE_SCHEMA_088_003_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="088.003",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "088.003"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "088.003"
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


def seed_existing_agents(tx):
    repo = AgentRepository()
    owner = "legacy-agent-owner"
    repo.create_agent(
        tx, owner_id=owner, agent_id="existing", display_name="Executable", observed_at=1
    )
    good = repo.create_revision(
        tx,
        owner_id=owner,
        agent_id="existing",
        revision_id=str(uuid.uuid4()),
        revision_number=1,
        compatibility_state="compatible",
        state="prepared",
        artifact_digest="a" * 64,
        manifest={"contract": 3},
        artifact_relative_path="revisions/existing/one",
        runtime_contract_version=3,
        release_lock_digest="b" * 64,
        promotion_token=str(uuid.uuid4()),
    )
    repo.create_revision(
        tx,
        owner_id=owner,
        agent_id="existing",
        revision_id=str(uuid.uuid4()),
        revision_number=2,
        parent_revision_id=good.revision_id,
        compatibility_state="incompatible",
        state="failed",
        artifact_digest="c" * 64,
        manifest={"contract": 4},
        artifact_relative_path="revisions/existing/two",
        runtime_contract_version=4,
        release_lock_digest="d" * 64,
        promotion_token=str(uuid.uuid4()),
    )
    repo.create_agent(
        tx, owner_id="other-owner", agent_id="legacy", display_name="Unmaterialized", observed_at=2
    )
    repo.create_revision(
        tx,
        owner_id="other-owner",
        agent_id="legacy",
        revision_id=str(uuid.uuid4()),
        revision_number=0,
        compatibility_state="legacy_pending",
        state="legacy_pending",
    )
    now = datetime(2026, 9, 13, tzinfo=UTC)
    host = repo.create_host_session(
        tx,
        host_session_id=str(uuid.uuid4()),
        host_id=str(uuid.uuid4()),
        owner_id=owner,
        connection_scope_id=str(uuid.uuid4()),
        platform="windows",
        client_version="1.4.0",
        host_generation=1,
        supported_runtime_contract_versions=(3,),
        runtime_contract_version=3,
        release_lock_digest="b" * 64,
        eligible_since=now,
        accepted_at=now,
        last_seen_at=now,
    )
    runtime = repo.create_runtime_instance(
        tx,
        runtime_instance_id=str(uuid.uuid4()),
        agent_id="existing",
        owner_id=owner,
        host_id=host.host_id,
        host_session_id=host.host_session_id,
        delivery_id=str(uuid.uuid4()),
        revision_id=good.revision_id,
        lifecycle_generation=1,
        runtime_contract_version=3,
        operation_execution_generation=1,
    )
    repo.create_runtime_request(
        tx,
        request_id=str(uuid.uuid4()),
        request_generation=str(uuid.uuid4()),
        runtime_instance_id=runtime.runtime_instance_id,
        agent_id="existing",
        owner_id=owner,
        operation_execution_generation=1,
    )
    draft = DraftAgentRepository().create_draft(
        tx,
        draft_id="prior-draft",
        owner_id=owner,
        agent_name="Executable",
        agent_slug="existing",
        description="Existing artifact",
        observed_at=1,
        draft_uuid=str(uuid.uuid4()),
        target_agent_id="existing",
    )
    DraftAgentRepository().compare_and_set_draft(
        tx,
        owner_id=owner,
        draft_id=draft.draft_id,
        expected_revision=0,
        updates={"published_revision_id": good.revision_id},
        updated_at=2,
    )
    tx.execute(
        "INSERT INTO draft_artifact_publication(publication_id,draft_uuid,owner_user_id,"
        "source_state_revision,generation_claim_id,target_agent_id,target_revision_id,"
        "staging_relative_path,revision_relative_path,state) VALUES (%s,%s,%s,0,%s,'existing',%s,"
        "'staging/prior','revisions/existing/one','claimed')",
        (str(uuid.uuid4()), draft.draft_uuid, owner, str(uuid.uuid4()), good.revision_id),
    )
    return (
        "user_agent",
        "user_agent_revision",
        "agent_host_session",
        "agent_runtime_instance",
        "agent_runtime_request",
        "draft_agents",
        "draft_artifact_publication",
    )


def test_populated_upgrade_preserves_executable_lineage_and_authentic_liabilities(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = (*seed_existing_agents(tx), *load_liabilities(tx))
        before = retained_rows(tx, tables)
        assert before["agent_runtime_request"] and before["draft_artifact_publication"]
        assert before["draft_agents"][0]["published_revision_id"] is not None
        assert {r["state"] for r in before["persistent_assignment_action"]} == {
            "started",
            "uncertain",
        }
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).applied_steps == (
        "astralplane-088-declarative-agents",
        "astralplane-088-owner-guidance",
        "astralplane-088-selected-input",
        "astralplane-088-scheduler-policy",
        "astralplane-088-framework-credentials",
        "astralplane-089-typesafe-credentials",
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
        for row in after["user_agent"]:
            assert row.pop("agent_kind") == "executable"
            assert row.pop("selected_definition_revision_id") is None
        for row in after["user_agent_revision"]:
            assert row.pop("revision_kind") == "executable"
            for field in ("definition_version", "definition_json", "definition_digest"):
                assert row.pop(field) is None
        for table in ("agent_runtime_instance", "draft_artifact_publication"):
            for row in after[table]:
                assert row.pop("revision_kind") == "executable"
        for row in after["draft_agents"]:
            assert row.pop("published_revision_kind") == "executable"
        # Later migration adds nullable cols here; excluded from diff
        for row in after["user_offline_grant"]:
            assert row.pop("max_admissions") is None
            assert row.pop("consumed_admissions") is None
        assert after == before
        assert not tx.fetch_all("SELECT * FROM user_agent_command_receipt")
    assert current_runner(db).run(
        expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision
    ).already_current
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="088.003")
    with db.transaction() as tx:
        assert retained_rows(tx, ("persistent_assignment", "persistent_assignment_action")) == {
            key: before[key] for key in ("persistent_assignment", "persistent_assignment_action")
        }


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER TABLE user_agent ADD COLUMN agent_kind TEXT",
        "ALTER TABLE user_agent_revision DROP CONSTRAINT user_agent_revision_artifact_check",
        "CREATE TABLE user_agent_command_receipt (owner_user_id TEXT)",
    ],
)
def test_predecessor_corruption_refuses_without_partial_upgrade(predecessor, corruption):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute(corruption)
        before = retained_rows(tx, ("schema_meta",))
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
    with db.transaction() as tx:
        assert retained_rows(tx, ("schema_meta",)) == before
        assert (
            tx.fetch_one(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='user_agent' AND column_name='selected_definition_revision_id'"
            )
            is None
        )


def test_migration_failure_rolls_back_all_ddl_then_retries_exact_edge(predecessor):
    db = predecessor.database

    class AbortMigrationError(Exception):
        pass

    with pytest.raises(AbortMigrationError), db.transaction() as tx:
        m.PLANE_SCHEMA_088_004_MIGRATION.apply(tx)
        assert tx.fetch_one("SELECT to_regclass('user_agent_command_receipt') AS relation")[
            "relation"
        ]
        raise AbortMigrationError()
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT to_regclass('user_agent_command_receipt') AS relation")["relation"]
            is None
        )
    from tests.integration.test_guidance_storage_upgrade import prior_runner as at_088_004

    assert at_088_004(db).run(expected_revision="088.004").applied_steps == (
        "astralplane-088-declarative-agents",
    )
    assert at_088_004(db).run(expected_revision="088.004").already_current


@pytest.mark.parametrize(
    "corruption",
    [
        "ALTER TABLE agent_runtime_instance DROP CONSTRAINT agent_runtime_executable_revision_fk",
        "ALTER TABLE draft_artifact_publication DROP CONSTRAINT "
        "draft_publication_executable_revision_fk",
        "ALTER TABLE draft_agents DROP CONSTRAINT draft_agents_executable_revision_fk",
        "ALTER TABLE user_agent DROP CONSTRAINT user_agent_kind_state_check",
        "ALTER TABLE user_agent_command_receipt DROP CONSTRAINT "
        "user_agent_command_receipt_command_id_check",
    ],
)
def test_current_catalog_refuses_missing_declarative_guard(migrated_clone, corruption):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=m.CURRENT_DATA_PLANE_REVISION.schema_revision)
