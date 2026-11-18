
from __future__ import annotations

from uuid import uuid4

import pytest

from orchestrator import turn_guidance_authority as tga
from persistent_agents.models import AssignmentError
from shared.protocol import ProtocolValidationError, UIEvent


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


