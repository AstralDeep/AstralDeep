"""Tests that saving a reviewed research selection (real HTTP, skills/notes/agents
repositories, PostgreSQL) refuses when the underlying agent, skill, or note head
changed since review, while a completed result stays readable.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from astralplane.repositories.agent_models import DeclarativeAgentCommand
from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition

from persistent_agents.runtime_values import thaw
from persistent_agents.tests.test_selected_research_postgres_088 import (
    add_agent, add_skill, selected_body, selected_notes as selected_notes,
)
from tests.test_work_runtime_postgres_088 import (
    finished,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    integrated as integrated,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]
PRIVATE = ("Prefer exact attributed excerpts.", "Use concise paragraphs and quoted sources.",
           "Public release 088", "binding_key", "synthetic-provider-key", "Read one page.")


async def _completed(op, runner, client, body):
    accepted = await client.post("/api/work/v1/operations", json=body)
    assert accepted.status_code == 201, accepted.text
    identity = accepted.json()["id"]
    runner.notify(identity)
    final = await finished(client, identity)
    chat = str(uuid4())
    with op.runtime.transaction() as tx:
        op.runtime.repositories.history.conversations.create(tx, owner_id=op.owner,
            conversation_id=chat, title="Selected destination", agent_id=None, created_at=1)
    return SimpleNamespace(identity=identity, revision=final["revision"], chat=chat,
                           base=f"/api/work/v1/operations/{identity}/result/proposals")


def _proposal(value):
    return {"version": 1, "submission_id": str(uuid4()), "publication_id": str(uuid4()),
            "expected_revision": value.revision, "conversation_id": value.chat,
            "expected_workspace_revision": 0, "expected_workspace_publication_id": None}


def _approval(review):
    return {"version": 1, "submission_id": str(uuid4()), "expected_revision": review["revision"],
            "proposal_digest": review["proposal"]["proposal_digest"]}


async def _review(client, value):
    command = _proposal(value)
    proposed = await client.post(value.base, json=command)
    assert proposed.status_code == 200, proposed.text
    assert all(secret not in proposed.text for secret in PRIVATE if secret != "Public release 088")
    return command, proposed.json()


def _head(op, chat):
    with op.runtime.transaction() as tx:
        record = op.runtime.repositories.history.conversations.get(tx, owner_id=op.owner,
                                                                  conversation_id=chat)
    return record.render_revision, record.publication_id


async def _result_available(client, value):
    result = await client.get(f"/api/work/v1/operations/{value.identity}/result")
    assert result.status_code == 200, result.text
    assert all(secret not in result.text for secret in PRIVATE if secret != "Public release 088")
    return result.json()["result"]["available"]


def _skill_change(op, skill, change):
    definition = None
    if change == "replace":
        definition = SkillDefinition("Changed structure", "A new unsupported adoption.", ("always",))
    elif change == "disable":
        definition = SkillDefinition("Private structure", "Prefer exact attributed excerpts.",
                                     ("always",), enabled=False)
    command = SkillCommand(op.owner, skill.skill_id, str(uuid4()),
                           "delete" if change == "delete" else "replace", skill.revision,
                           definition=definition)
    with op.runtime.transaction() as tx:
        op.runtime.repositories.preferences.skills.apply_change(tx, command=command)


def _archive_agent(op, agent):
    with op.runtime.transaction() as tx:
        repo = op.runtime.repositories.agents
        head = repo.get_agent(tx, owner_id=op.owner, agent_id=agent["agent_id"])
        command = DeclarativeAgentCommand(op.owner, agent["agent_id"], str(uuid4()), "archive",
                                          expected_revision=head.state_revision)
        repo.apply_declarative_command(tx, preparation=repo.prepare_declarative_command(
            tx, command=command))


@pytest.mark.parametrize("when", ["after_review", "before_review"])
@pytest.mark.parametrize("head,change", [("skill", "replace"), ("skill", "delete"),
                                         ("skill", "disable"), ("agent", "archive")])
async def test_changed_skill_or_agent_head_refuses_save(integrated, monkeypatch, head, change, when):
    from shared.feature_flags import flags
    op, runner, client = integrated
    if head == "agent":
        monkeypatch.setitem(flags._flags, "byo_agents", True)
        agent = await asyncio.to_thread(add_agent, op, runner)
        value = await _completed(op, runner, client, selected_body(runner, agent=agent))
        mutate = lambda: _archive_agent(op, agent)  # noqa: E731
    else:
        skill = await asyncio.to_thread(add_skill, op)
        value = await _completed(op, runner, client, selected_body(runner, skill=skill))
        mutate = lambda: _skill_change(op, skill, change)  # noqa: E731
    assert await _result_available(client, value) is True
    if when == "before_review":
        await asyncio.to_thread(mutate)
        refused = await client.post(value.base, json=_proposal(value))
        assert refused.status_code == 409 and refused.json() == {"error": "assignment_guidance_changed"}
    else:
        command, review = await _review(client, value)
        await asyncio.to_thread(mutate)
        refused = await client.post(value.base + "/" + command["submission_id"] + "/save",
                                    json=_approval(review))
        assert refused.status_code == 409 and refused.json() == {"error": "assignment_guidance_changed"}
    assert _head(op, value.chat) == (0, None)
    assert await _result_available(client, value) is True
    rows, _ = runner.orch.audit_repo.list_for_user(op.owner)
    assert not any(row.action_type == "work.result.save" for row in rows)


@pytest.mark.parametrize("change", ["forget", "disable"])
async def test_forgotten_or_disabled_note_refuses_save(selected_notes, change):
    from personalization.explicit_note_service import ExplicitNoteCommand
    from tests.test_explicit_note_service_postgres_088 import apply as apply_note
    op, runner, client, state, note = selected_notes
    value = await _completed(op, runner, client, selected_body(runner, note=note))
    command, review = await _review(client, value)
    if change == "forget":
        await apply_note(state, ExplicitNoteCommand("forget", note.note_id, note.revision))
    else:
        await apply_note(state, ExplicitNoteCommand("set_enabled", note.note_id, note.revision,
                                                    enabled=False))
    refused = await client.post(value.base + "/" + command["submission_id"] + "/save",
                                json=_approval(review))
    assert refused.status_code == 409 and refused.json() == {"error": "assignment_guidance_changed"}
    assert _head(op, value.chat) == (0, None)
    assert await _result_available(client, value) is True
    rows, _ = runner.orch.audit_repo.list_for_user(op.owner)
    assert not any(row.action_type == "work.result.save" for row in rows)


async def test_exact_replay_saves_a_selected_result_once_with_id_only_audit(integrated):
    op, runner, client = integrated
    skill = await asyncio.to_thread(add_skill, op)
    value = await _completed(op, runner, client, selected_body(runner, skill=skill))
    command, review = await _review(client, value)
    target = value.base + "/" + command["submission_id"] + "/save"
    approval = _approval(review)
    accepted = await client.post(target, json=approval)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["applied"] is True and accepted.json()["render_revision"] == 1
    assert _head(op, value.chat) == (1, command["publication_id"])
    replay = await client.post(target, json=approval)
    assert replay.status_code == 200 and replay.json() == {**accepted.json(), "applied": False}
    assert _head(op, value.chat) == (1, command["publication_id"])
    await asyncio.to_thread(_skill_change, op, skill, "delete")
    replay = await client.post(target, json=approval)
    assert replay.status_code == 200 and replay.json()["applied"] is False
    assert await _result_available(client, value) is True
    with op.runtime.transaction() as tx:
        rows = op.runtime.repositories.workspaces.canvas.list_current(
            tx, owner_id=op.owner, conversation_id=value.chat)
    assert [row.component_id for row in rows] == [review["component"]["component_id"]]
    assert "Public release 088" in json.dumps(thaw(rows[0].payload))
    assert "Prefer exact attributed excerpts." not in json.dumps(thaw(rows[0].payload))
    audit, _ = runner.orch.audit_repo.list_for_user(op.owner)
    steps = [row for row in audit if row.action_type.startswith("work.result.")]
    assert sorted(row.action_type for row in steps) == ["work.result.propose", "work.result.save"]
    serialized = json.dumps([row.model_dump(mode="json") for row in steps])
    assert all(secret not in serialized for secret in PRIVATE)
    assert all(row.outputs_meta["assignment_id"] == value.identity
               and row.outputs_meta["publication_id"] == command["publication_id"]
               and row.outputs_meta["action_id"] == command["submission_id"] for row in steps)
    assert runner.orch.audit_repo.verify_chain(op.owner) is None
