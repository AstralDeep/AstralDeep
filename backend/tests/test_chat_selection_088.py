"""Tests for composer selection (backend/shared/protocol.py,
orchestrator/turn_guidance_authority.py): the chat_message wire shape and
bind_turn_selection's checks against a caller's active agents, skills, and notes.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from orchestrator import turn_guidance_authority as tga
from persistent_agents.models import AssignmentError
from shared.protocol import ProtocolValidationError, UIEvent
from tests.test_declarative_agent_lifecycle_postgres_088 import (
    api as api, body as body, caller as caller, declarations as declarations,
    fixture as fixture, next_body as next_body, plane as plane, research_definition as research_definition,
    research_service as research_service, runtime as runtime, service as service,
    signing_key as signing_key, source_service as source_service,
)


def selection(**changes):
    return {"version": 1, "agent": None, "skills": [], "notes": [], **changes}


def test_absent_selection_leaves_the_payload_untouched():
    event = UIEvent(action="chat_message", payload={"message": "hi"})
    assert "selection" not in event.payload


def test_valid_selection_shape_is_accepted_on_chat_message():
    payload = {"message": "hi", "selection": selection(
        skills=[{"skill_id": str(uuid4()), "revision": 1}])}
    UIEvent(action="chat_message", payload=payload)


@pytest.mark.parametrize("bad", [
    selection(),
    {**selection(), "version": 2},
    {**selection(), "agent": {"agent_id": "a"}},
    {**selection(), "skills": [{"skill_id": "not-a-uuid", "revision": 1}]},
    {**selection(), "skills": [{"skill_id": str(uuid4()), "revision": 0}]},
    {**selection(), "notes": [{"note_id": str(uuid4()), "revision": 1}] * 9},
    {**selection(), "skills": [{"skill_id": str(uuid4())}]},
    {**selection(), "agent": {"agent_id": "", "revision_id": str(uuid4())}},
])
def test_malformed_selection_is_refused_at_the_socket_boundary(bad):
    with pytest.raises(ProtocolValidationError):
        UIEvent(action="chat_message", payload={"message": "hi", "selection": bad})


def test_duplicate_skill_identity_is_refused():
    dup = str(uuid4())
    payload = selection(skills=[{"skill_id": dup, "revision": 1}, {"skill_id": dup, "revision": 2}])
    with pytest.raises(ProtocolValidationError):
        UIEvent(action="chat_message", payload={"message": "hi", "selection": payload})


def test_selection_is_refused_on_every_other_action():
    payload = selection(skills=[{"skill_id": str(uuid4()), "revision": 1}])
    with pytest.raises(ProtocolValidationError):
        UIEvent(action="chrome_open", payload={"surface": "guidance", "selection": payload})


def test_identifiers_are_bounded_sorted_and_hashable():
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    value = selection(
        skills=[{"skill_id": a, "revision": 1}, {"skill_id": b, "revision": 3}],
        notes=[{"note_id": c, "revision": 2}])
    agent, skills, notes = tga._selection_identifiers(value)
    assert agent is None
    assert skills == tuple(sorted([(a, 1), (b, 3)]))
    assert notes == ((c, 2),)


def test_identifiers_refuse_the_all_empty_shape():
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        tga._selection_identifiers(selection())


def test_identifiers_carry_the_agent_tuple():
    agent_id, revision_id = "user-agent", str(uuid4())
    value = selection(agent={"agent_id": agent_id, "revision_id": revision_id})
    agent, skills, notes = tga._selection_identifiers(value)
    assert agent == (agent_id, revision_id) and skills == () and notes == ()


async def build_binding(state, current):
    origin = await tga.capture_turn_guidance_from_human(current, expected_orchestrator=state.api.orch)
    return tga.bind_http_guidance(origin, expected_orchestrator=state.api.orch,
                                  websocket=object(), chat_id="chat-1")


async def test_bind_turn_selection_accepts_a_current_active_agent(declarations):
    state = declarations
    created = await state.service.command(caller=await caller(state),
        body=body(display_name="Selectable agent", definition=research_definition(state)))
    activated = await state.service.command(caller=await caller(state), body=next_body(
        created, "activate", revision_id=created.revision.revision_id))

    binding = await build_binding(state, await caller(state))
    assert binding.selection is None
    value = selection(agent={"agent_id": activated.agent.agent_id,
                             "revision_id": activated.revision.revision_id})
    bound = await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)
    assert bound is not binding and binding.selection is None
    assert bound.selection.agent == (activated.agent.agent_id, activated.revision.revision_id)
    assert bound.selection.skills == () and bound.selection.notes == ()
    with state.api.runtime.transaction() as tx:
        bound.selection.recheck(tx, state.api.runtime.repositories, activated.agent.owner_id)


async def test_bind_turn_selection_refuses_a_draft_not_yet_active(declarations):
    state = declarations
    result = await state.service.command(caller=await caller(state), body=body(display_name="Still draft"))
    binding = await build_binding(state, await caller(state))
    value = selection(agent={"agent_id": result.agent.agent_id, "revision_id": result.revision.revision_id})
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


async def test_bind_turn_selection_refuses_a_foreign_owners_agent(declarations):
    state = declarations
    created = await state.service.command(caller=await caller(state),
        body=body(display_name="Owner-only", definition=research_definition(state)))
    activated = await state.service.command(caller=await caller(state), body=next_body(
        created, "activate", revision_id=created.revision.revision_id))
    other = await caller(state, cookie=False, owner="another-owner")
    binding = await build_binding(state, other)
    value = selection(agent={"agent_id": activated.agent.agent_id,
                             "revision_id": activated.revision.revision_id})
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


async def test_bind_turn_selection_refuses_an_archived_agent(declarations):
    state = declarations
    created = await state.service.command(caller=await caller(state),
        body=body(display_name="Will archive", definition=research_definition(state)))
    activated = await state.service.command(caller=await caller(state), body=next_body(
        created, "activate", revision_id=created.revision.revision_id))
    await state.service.command(caller=await caller(state), body=next_body(activated, "archive"))
    binding = await build_binding(state, await caller(state))
    value = selection(agent={"agent_id": activated.agent.agent_id,
                             "revision_id": activated.revision.revision_id})
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


async def test_bind_turn_selection_type_checks_its_binding(declarations):
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(object(), expected_orchestrator=declarations.api.orch,
                                      selection=selection())


def make_skill(state, owner_id, *, enabled=True):
    from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition
    with state.api.runtime.transaction() as tx:
        result = state.api.runtime.repositories.preferences.skills.apply_change(tx, command=SkillCommand(
            owner_id, str(uuid4()), str(uuid4()), "create", 0, slug="chat-selection-" + uuid4().hex[:12],
            definition=SkillDefinition("Selection skill", "Prefer exact attributed excerpts.",
                                       ("always",), enabled=enabled)))
    return result.head


def bump_skill(state, owner_id, head):
    from astralplane.repositories.guidance_models import SkillCommand, SkillDefinition
    with state.api.runtime.transaction() as tx:
        result = state.api.runtime.repositories.preferences.skills.apply_change(tx, command=SkillCommand(
            owner_id, head.skill_id, str(uuid4()), "replace", head.revision,
            definition=SkillDefinition("Selection skill", "Updated attribution instructions.", ("always",))))
    return result.head


def delete_skill(state, owner_id, head):
    from astralplane.repositories.guidance_models import SkillCommand
    with state.api.runtime.transaction() as tx:
        result = state.api.runtime.repositories.preferences.skills.apply_change(
            tx, command=SkillCommand(owner_id, head.skill_id, str(uuid4()), "delete", head.revision))
    return result.head


async def test_bind_turn_selection_refuses_an_unknown_skill(declarations):
    state = declarations
    current = await caller(state)
    binding = await build_binding(state, current)
    value = selection(skills=[{"skill_id": str(uuid4()), "revision": 1}])
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


async def test_bind_turn_selection_refuses_a_disabled_skill(declarations):
    state = declarations
    current = await caller(state)
    head = make_skill(state, current.owner_id, enabled=False)
    binding = await build_binding(state, current)
    value = selection(skills=[{"skill_id": head.skill_id, "revision": head.revision}])
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


async def test_bind_turn_selection_refuses_a_stale_skill_revision(declarations):
    state = declarations
    current = await caller(state)
    head = make_skill(state, current.owner_id)
    bump_skill(state, current.owner_id, head)
    binding = await build_binding(state, current)
    value = selection(skills=[{"skill_id": head.skill_id, "revision": head.revision}])
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


async def test_bind_turn_selection_refuses_a_deleted_skill(declarations):
    state = declarations
    current = await caller(state)
    head = make_skill(state, current.owner_id)
    deleted = delete_skill(state, current.owner_id, head)
    binding = await build_binding(state, current)
    value = selection(skills=[{"skill_id": deleted.skill_id, "revision": deleted.revision}])
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


@pytest.fixture
def notes_service(declarations, tmp_path):
    from orchestrator.credential_manager import CredentialManager
    from personalization.explicit_note_service import ExplicitNoteService
    orch = declarations.api.orch
    orch.credential_manager = CredentialManager(plane_runtime=declarations.api.runtime,
                                                data_dir=str(tmp_path))
    orch.explicit_notes = ExplicitNoteService(orch)
    return declarations


async def make_note(state, current):
    from personalization.explicit_note_service import ExplicitNoteCommand
    note_id = str(uuid4())
    metadata = await state.api.orch.explicit_notes.command(caller=current, body=ExplicitNoteCommand(
        command="save", note_id=note_id, expected_revision=0, category="context",
        value="Selection note.", enabled=True, expires_at=None))
    return metadata


async def test_bind_turn_selection_refuses_an_unknown_note(notes_service):
    state = notes_service
    current = await caller(state)
    binding = await build_binding(state, current)
    value = selection(notes=[{"note_id": str(uuid4()), "revision": 1}])
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)


async def test_bind_turn_selection_refuses_a_stale_note_revision(notes_service):
    state = notes_service
    current = await caller(state)
    metadata = await make_note(state, current)
    binding = await build_binding(state, current)
    value = selection(notes=[{"note_id": metadata.note_id, "revision": metadata.revision + 1}])
    with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
        await tga.bind_turn_selection(binding, expected_orchestrator=state.api.orch, selection=value)
