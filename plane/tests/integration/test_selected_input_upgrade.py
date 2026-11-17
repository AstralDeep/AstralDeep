"""Tests for astralplane.database.migrations: the selected-input schema upgrade retains
history and unsettled work without inferring a selection envelope, and refuses a
wrong predecessor before mutation.
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from astralplane.database import migrations as m
from astralplane.errors import SchemaRevisionError
from astralplane.repositories.agent_models import definition_snapshot
from astralplane.repositories.assignments import AssignmentRepository, canonical, digest
from astralplane.repositories.guidance import ExplicitNotesRepository, SkillsRepository
from astralplane.repositories.guidance_models import (
    ExplicitNoteRecord,
    SkillCommand,
    SkillDefinition,
)
from tests.fixtures.migrated_template import bound_clone
from tests.integration.test_declarative_agents_upgrade import seed_existing_agents
from tests.integration.test_session_issuer_upgrade import load_liabilities, retained_rows


def prior_runner(database):
    registry = m.MigrationRegistry(
        tuple(e for e in m.MIGRATION_REGISTRY.migrations if e.target_revision <= "088.005"),
        current_schema_verifier=lambda tx: m._verify_predecessor_plane_schema(tx, "088.005"),
        current_schema_verifier_checksum="d510ca0efc4afb85fed560ce7bb0fe4a92c0627fb8aaf85b99b350d6fbef3cf6",
        predecessor_schema_verifier=m._verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="43c6a705e324d4246fffa478ace12e850b002e490e6e93c0ac8e846b2c124d6d",
    )
    assert registry.digest == m.PLANE_SCHEMA_088_005_REGISTRY_DIGEST
    revision = replace(
        m.CURRENT_DATA_PLANE_REVISION,
        schema_revision="088.005",
        migration_digest=registry.digest,
        read_compatible_from=tuple(
            r for r in m.CURRENT_DATA_PLANE_REVISION.read_compatible_from if r < "088.005"
        ),
        accepted_predecessor_digests=tuple(
            p
            for p in m.CURRENT_DATA_PLANE_REVISION.accepted_predecessor_digests
            if p[0] < "088.005"
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


def populated(tx):
    tables = (*seed_existing_agents(tx), *load_liabilities(tx))
    assignments = tx.fetch_all("SELECT * FROM persistent_assignment ORDER BY id")
    owner = assignments[0]["owner_user_id"]
    skill = SkillsRepository().apply_change(
        tx,
        command=SkillCommand(
            owner,
            str(uuid4()),
            str(uuid4()),
            "create",
            0,
            slug="upgrade",
            definition=SkillDefinition("Upgrade", "Exact pre-upgrade skill instructions."),
        ),
    )
    notes = ExplicitNotesRepository()
    prep = notes.prepare_explicit_note(
        tx, owner_id=owner, note_id=str(uuid4()), expected_revision=0
    )
    note = notes.put_explicit_note(
        tx,
        preparation=prep,
        record=ExplicitNoteRecord(
            owner,
            prep.note_id,
            1,
            "context",
            True,
            prep.observed_at_ms,
            prep.observed_at_ms,
            b"exact-pre-upgrade-opaque-ciphertext",
        ),
    )
    refs = sorted([("skill", skill.head.skill_id, 1), ("note", note.note_id, 1)])
    for index, assignment in enumerate(assignments):
        selected = refs if index == 0 else []
        tx.execute(
            "INSERT INTO assignment_guidance_selection(owner_id,assignment_id,"
            "instruction_revision,reference_digest,created_at) VALUES(%s,%s,1,%s,0)",
            (assignment["owner_user_id"], str(assignment["id"]), digest(selected)),
        )
        for kind, identity, revision in selected:
            tx.execute(
                "INSERT INTO assignment_guidance_reference(owner_id,assignment_id,"
                "instruction_revision,kind,resource_id,revision) VALUES(%s,%s,1,%s,%s,%s)",
                (assignment["owner_user_id"], str(assignment["id"]), kind, identity, revision),
            )
    revision_id = str(uuid4())
    definition, definition_digest = definition_snapshot({"version": 1, "purpose": "Existing"})
    tx.execute(
        "INSERT INTO user_agent(agent_id,owner_user_id,display_name,status,agent_kind,"
        "created_at,updated_at) VALUES('prior-definition',%s,'Prior','draft','declarative',0,0)",
        (owner,),
    )
    tx.execute(
        "INSERT INTO user_agent_revision(revision_id,agent_id,owner_user_id,revision_number,"
        "revision_kind,compatibility_state,state,definition_version,"
        "definition_json,definition_digest) "
        "VALUES(%s,'prior-definition',%s,1,'declarative','declarative','definition',1,%s::jsonb,%s)",
        (revision_id, owner, canonical(definition), definition_digest),
    )
    tx.execute(
        "UPDATE user_agent SET status='active',selected_definition_revision_id=%s "
        "WHERE agent_id='prior-definition'",
        (revision_id,),
    )
    return tuple(
        dict.fromkeys(
            (
                *tables,
                "owner_skill_head",
                "owner_skill_revision",
                "explicit_note_current",
                "assignment_guidance_selection",
                "assignment_guidance_reference",
            )
        )
    )


def test_populated005_upgrade_keeps_exact_rows_and_does_not_infer_envelope(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = populated(tx)
        before = retained_rows(tx, tables)
    report = current_runner(db).run(expected_revision=HEAD_REVISION)
    assert "astralplane-088-selected-input" in report.applied_steps
    with db.transaction() as tx:
        after = retained_rows(tx, tables)
        for row in after["assignment_guidance_selection"]:
            assert row.pop("selected_input") is None
        # Later migration adds nullable cols here; excluded from diff
        for row in after["user_offline_grant"]:
            assert row.pop("max_admissions") is None
            assert row.pop("consumed_admissions") is None
        assert after == before
        assert tx.fetch_all("SELECT * FROM assignment_selected_agent") == ()
        for assignment in before["persistent_assignment"]:
            selected = AssignmentRepository().get_selected_input(
                tx, owner_id=assignment["owner_user_id"], assignment_id=assignment["id"]
            )
            assert selected is not None and selected.envelope is None
        assert {r["state"] for r in after["persistent_assignment_action"]} == {
            "started",
            "uncertain",
        }
    assert current_runner(db).run(expected_revision=HEAD_REVISION).already_current
    with pytest.raises(SchemaRevisionError):
        prior_runner(db).run(expected_revision="088.005")


def test_006_interruption_rolls_back_populated005_and_recovery_repeats(predecessor):
    db = predecessor.database
    with db.transaction() as tx:
        tables = populated(tx)
        before = retained_rows(tx, tables)
    with pytest.raises(RuntimeError, match="after full DDL"), db.transaction() as tx:
        m.PLANE_SCHEMA_088_006_MIGRATION.apply(tx)
        raise RuntimeError("after full DDL")
    with db.transaction() as tx:
        assert retained_rows(tx, tables) == before
        m._verify_predecessor_plane_schema(tx, "088.005")
        assert tx.fetch_one("SELECT to_regclass('assignment_selected_agent') AS t")["t"] is None
    from tests.integration.test_scheduler_policy_upgrade import prior_runner as at_088_006

    assert at_088_006(db).run(expected_revision="088.006").applied_steps == (
        "astralplane-088-selected-input",
    )
    assert at_088_006(db).run(expected_revision="088.006").already_current


@pytest.mark.parametrize(
    "corrupt",
    [
        "CREATE TABLE assignment_selected_agent (owner_id TEXT)",
        "ALTER TABLE assignment_guidance_selection DROP CONSTRAINT "
        "assignment_guidance_selection_pkey CASCADE",
    ],
)
def test_wrong005_predecessor_refuses_before_mutation(predecessor, corrupt):
    db = predecessor.database
    with db.transaction() as tx:
        tx.execute(corrupt)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=HEAD_REVISION)
    with db.transaction() as tx:
        assert (
            tx.fetch_one("SELECT value FROM schema_meta WHERE key='revision'")["value"] == "088.005"
        )


@pytest.mark.parametrize(
    "corrupt",
    [
        "DROP INDEX assignment_selected_agent_active",
        "DROP TRIGGER assignment_selected_agent_immutable ON assignment_selected_agent",
        "ALTER TABLE assignment_guidance_selection DROP CONSTRAINT "
        "assignment_guidance_selection_selected_input_check",
        "ALTER FUNCTION valid_assignment_selected_input(jsonb) RESET search_path",
        "ALTER TABLE assignment_selected_agent ADD COLUMN private_expansion TEXT",
    ],
)
def test_current_selected_catalog_refuses_removed_guards_or_new_text(migrated_clone, corrupt):
    db = migrated_clone.database
    with db.transaction() as tx:
        tx.execute(corrupt)
    with pytest.raises(SchemaRevisionError):
        current_runner(db).run(expected_revision=HEAD_REVISION)
