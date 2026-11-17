"""Tests for astralplane.database.migrations: the owner-guidance schema upgrade
preserves real history, runtime notes, and issued liabilities, and refuses a wrong
predecessor without partial writes.
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.agent_models import definition_snapshot
from astralplane.repositories.agents import DeclarativeAgentCommand
from astralplane.repositories.assignments import canonical
from astralplane.repositories.guidance import SkillsRepository
from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition
from astralplane.repositories.preferences import MemoryRecord, PreferencesRepository
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_declarative_agents_upgrade import seed_existing_agents
from tests.integration.test_session_issuer_upgrade import load_liabilities, retained_rows


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(
            edge for edge in m.MIGRATION_REGISTRY.migrations if edge.target_revision <= "088.004"
        ),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "088.004"),
        current_schema_verifier_checksum="1169d81190322100a04c41c37dfafd291399fef6687106b057ff52d3443570c4",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="46b4e2dce4a6b15c38e42292617185ba3a8442aa663ce771eb35d7c37584672d",
    )
    assert registry.digest == m.PLANE_SCHEMA_088_004_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="088.004",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "088.004"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "088.004"
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


HEAD_REVISION = m.CURRENT_DATA_PLANE_REVISION.schema_revision


def seed_populated(tx):
    tables = (*seed_existing_agents(tx), *load_liabilities(tx))
    prefs = PreferencesRepository()
    prefs.personalization.create_memory(
        tx,
        MemoryRecord(
            str(uuid4()),
            "legacy-memory-owner",
            "context",
            "Existing plaintext automatic memory remains exact.",
            "promoted",
            0.5,
            100,
            100,
            None,
            None,
            "existing",
            "original-signature",
            90,
            None,
            100,
            2,
            101,
            None,
        ),
    )
    prefs.set_chat_phi_notice_enabled(tx, owner_id="legacy-memory-owner", enabled=False)
    command = DeclarativeAgentCommand(
        owner_id="declaration-owner",
        agent_id="prior-declaration",
        command_id=str(uuid4()),
        command="create",
        revision_id=str(uuid4()),
        display_name="Stored draft",
        definition={"version": 1, "purpose": "Exact prior definition"},
    )
    definition, definition_digest = definition_snapshot(command.definition)
    tx.execute(
        "INSERT INTO user_agent(agent_id,owner_user_id,display_name,status,agent_kind,"
        "created_at,updated_at) VALUES(%s,%s,%s,'draft','declarative',0,0)",
        (command.agent_id, command.owner_id, command.display_name),
    )
    tx.execute(
        "INSERT INTO user_agent_revision(revision_id,agent_id,owner_user_id,revision_number,"
        "revision_kind,compatibility_state,state,definition_version,"
        "definition_json,definition_digest) "
        "VALUES(%s,%s,%s,1,'declarative','declarative','definition',1,%s::jsonb,%s)",
        (
            command.revision_id,
            command.agent_id,
            command.owner_id,
            canonical(definition),
            definition_digest,
        ),
    )
    selected = DeclarativeAgentCommand(
        owner_id=command.owner_id,
        agent_id=command.agent_id,
        command_id=str(uuid4()),
        command="activate",
        expected_revision=0,
        revision_id=command.revision_id,
    )
    tx.execute(
        "UPDATE user_agent SET status='active',selected_definition_revision_id=%s,"
        "state_revision=1 WHERE agent_id=%s",
        (command.revision_id, command.agent_id),
    )
    for state, cmd in enumerate((command, selected)):
        tx.execute(
            "INSERT INTO user_agent_command_receipt(owner_user_id,agent_id,command_id,"
            "command_version,command,request_digest,result_state_revision,"
            "result_definition_revision_id) "
            "VALUES(%s,%s,%s,1,%s,%s,%s,%s)",
            (
                cmd.owner_id,
                cmd.agent_id,
                cmd.command_id,
                cmd.command,
                cmd.request_digest,
                state,
                command.revision_id,
            ),
        )
    return tuple(
        dict.fromkeys((*tables, "memory_item", "user_preferences", "user_agent_command_receipt"))
    )


def test_populated_upgrade_preserves_real_history_runtime_notes_and_issued_liabilities(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
        assert {r["state"] for r in before["persistent_assignment_action"]} == {
            "started",
            "uncertain",
        }
        assert (
            before["memory_item"]
            and before["user_agent_command_receipt"]
            and before["user_offline_grant"]
        )
        assert any(r["selected_definition_revision_id"] for r in before["user_agent"])
    upgrade_steps = current_runner(db).run(expected_revision=HEAD_REVISION).applied_steps
    assert "astralplane-088-owner-guidance" in upgrade_steps
    assert "astralplane-088-selected-input" in upgrade_steps
    with db.transaction() as tx:
        after = retained_rows(tx, tables)
        # Later migration adds nullable cols here; excluded from diff
        for row in after["user_offline_grant"]:
            assert row.pop("max_admissions") is None
            assert row.pop("consumed_admissions") is None
        assert after == before
        for table in (
            "owner_skill_head",
            "owner_skill_revision",
            "owner_skill_catalog",
            "explicit_note_current",
            "assignment_guidance_selection",
            "assignment_guidance_reference",
        ):
            assert tx.fetch_all("SELECT * FROM " + table) == ()
        result = SkillsRepository().apply_change(
            tx,
            command=SkillCommand(
                "new-owner",
                str(uuid4()),
                str(uuid4()),
                "create",
                0,
                slug="new",
                definition=SkillDefinition("New skill", "New current skill instructions."),
            ),
        )
        assert result.head.revision == 1
    assert current_runner(db).run(expected_revision=HEAD_REVISION).already_current
    with db.transaction() as tx:
        still = retained_rows(tx, tables)
        for row in still["user_offline_grant"]:
            assert row.pop("max_admissions") is None
            assert row.pop("consumed_admissions") is None
        assert still == before
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="088.004")


@pytest.mark.parametrize(
    "corruption",
    [
        "CREATE TABLE owner_skill_head (owner_id TEXT)",
        "ALTER TABLE user_agent_revision DROP CONSTRAINT user_agent_revision_artifact_check",
    ],
)
def test_wrong_predecessor_refuses_without_partial_guidance_schema(predecessor, corruption):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=HEAD_REVISION)
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "088.004"
        )
        assert tx.fetch_one("SELECT to_regclass('explicit_note_current') AS t")["t"] is None


def test_new_edge_rollback_preserves_populated_predecessor_and_can_repeat(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = seed_populated(tx)
        before = retained_rows(tx, tables)
    with pytest.raises(RuntimeError, match="injected final failure"), db.transaction() as tx:
        m.PLANE_SCHEMA_088_005_MIGRATION.apply(tx)
        raise RuntimeError("injected final failure")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        assert tx.fetch_one("SELECT to_regclass('explicit_note_current') AS t")["t"] is None
    from tests.integration.test_selected_input_upgrade import prior_runner as at_088_005

    assert at_088_005(db).run(expected_revision="088.005").applied_steps == (
        "astralplane-088-owner-guidance",
    )
    assert at_088_005(db).run(expected_revision="088.005").already_current
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "DROP INDEX owner_skill_live_alias",
        "DROP TRIGGER owner_skill_revision_immutable ON owner_skill_revision",
        "ALTER TABLE assignment_guidance_reference DROP CONSTRAINT "
        "assignment_guidance_reference_assignment_id_owner_id_fkey",
        "ALTER TABLE explicit_note_current ADD COLUMN prior_ciphertext BYTEA",
    ],
)
def test_current_schema_refuses_missing_guards_or_extra_note_history(migrated_clone, corruption):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corruption)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=HEAD_REVISION)
