"""Tests for the composer's Advanced picker in
orchestrator/projection_surfaces/guidance.py: the selection request shape is
accepted, a selection narrows to what still exists at the current revision, and
work_submit.py consumes the stamped payload.
"""

from copy import deepcopy
from uuid import uuid4

import pytest

from persistent_agents.models import AssignmentError

AGENT_ID = str(uuid4())
REVISION_ID = str(uuid4())
SKILL_ID = str(uuid4())
NOTE_ID = str(uuid4())


def selection(agent=None, skills=(), notes=()):
    return {"version": 1, "agent": agent, "skills": list(skills), "notes": list(notes)}


def offered():
    return (
        ({"agent_id": AGENT_ID, "revision_id": REVISION_ID, "display_name": "Paper triage"},),
        ({"skill_id": SKILL_ID, "revision": 2, "name": "House style",
          "command": "style", "enabled": True},),
        ({"note_id": NOTE_ID, "revision": 1, "category": "preference", "enabled": True},),
    )


def test_the_advanced_button_request_is_accepted():
    from orchestrator.projection_surfaces.guidance import _request
    payload = {"surface": "guidance", "params": {"view": "selection"}}
    expected = deepcopy(payload)
    frozen = _request("chrome_open", payload)
    payload.clear()
    assert frozen == expected


@pytest.mark.parametrize("params", [
    {"view": "selection", "mode": "list"},
    {"view": "selection", "search": "anything"},
    {"view": "skills"},
    {"view": "selection", "selection": None},
    {"view": "selection", "selection": selection()},
])
def test_a_request_carrying_anything_but_the_view_is_refused(params):
    from orchestrator.projection_surfaces.guidance import _request
    with pytest.raises(AssignmentError, match="explicit_note_request_invalid"):
        _request("chrome_open", {"surface": "guidance", "params": params})


def test_the_notes_list_is_unaffected_by_the_new_view():
    from orchestrator.projection_surfaces.guidance import _request
    payload = {"surface": "guidance", "params": {"mode": "list", "search": "short"}}
    assert _request("chrome_open", payload) == payload


@pytest.mark.parametrize("bad", [
    {"version": 2, "agent": None, "skills": [], "notes": []},
    {"agent": None, "skills": [], "notes": []},
    selection(agent={"agent_id": AGENT_ID}),
    selection(skills=[{"skill_id": SKILL_ID}]),
    selection(skills=[{"skill_id": "nope", "revision": 1}]),
    selection(notes=[{"note_id": NOTE_ID, "revision": 0}]),
    selection(skills=[{"skill_id": SKILL_ID, "revision": 1},
                      {"skill_id": SKILL_ID, "revision": 1}]),
])
def test_a_malformed_selection_command_is_refused_without_echoing_it(bad):
    from orchestrator.projection_surfaces.guidance import _request
    with pytest.raises(AssignmentError, match="explicit_note_request_invalid"):
        _request("chrome_turn_selection_set", bad)


def test_setting_a_selection_is_its_own_action():
    from orchestrator.projection_surfaces.guidance import _is_selection, _request
    payload = selection(agent={"agent_id": AGENT_ID, "revision_id": REVISION_ID})
    assert _request("chrome_turn_selection_set", payload) == payload
    assert _is_selection("chrome_turn_selection_set", payload)
    assert _is_selection("chrome_open", {"params": {"view": "selection"}})
    assert not _is_selection("chrome_open", {"params": {"mode": "list"}})


def test_a_selection_keeps_only_what_is_still_offered_at_that_revision():
    from orchestrator.projection_surfaces.guidance import _narrow_selection
    agents, skills, notes = offered()
    chosen = _narrow_selection(selection(
        agent={"agent_id": AGENT_ID, "revision_id": REVISION_ID},
        skills=[{"skill_id": SKILL_ID, "revision": 2}],
        notes=[{"note_id": NOTE_ID, "revision": 1}]), agents, skills, notes)
    assert chosen == {
        "agent": {"agent_id": AGENT_ID, "revision_id": REVISION_ID},
        "skills": [{"skill_id": SKILL_ID, "revision": 2}],
        "notes": [{"note_id": NOTE_ID, "revision": 1}],
    }


@pytest.mark.parametrize("stale", [
    selection(skills=[{"skill_id": SKILL_ID, "revision": 1}]),
    selection(notes=[{"note_id": str(uuid4()), "revision": 1}]),
    selection(agent={"agent_id": AGENT_ID, "revision_id": str(uuid4())}),
])
def test_anything_changed_since_is_simply_no_longer_selected(stale):
    from orchestrator.projection_surfaces.guidance import _narrow_selection
    agents, skills, notes = offered()
    chosen = _narrow_selection(stale, agents, skills, notes)
    assert chosen == {"agent": None, "skills": [], "notes": []}


def test_no_incoming_selection_means_nothing_selected():
    from orchestrator.projection_surfaces.guidance import _narrow_selection
    agents, skills, notes = offered()
    for value in (None, {}, "nonsense", []):
        assert _narrow_selection(value, agents, skills, notes) == {
            "agent": None, "skills": [], "notes": []}


def test_the_view_builds_from_the_state_this_module_produces():
    from astralprojection.chrome import render_html
    from astralprojection.chrome.guidance import build_guidance_view
    from orchestrator.projection_surfaces.guidance import SELECTION_VIEW
    agents, skills, notes = offered()
    view = build_guidance_view({
        "view": SELECTION_VIEW, "status": "ready",
        "agents": agents, "skills": skills, "notes": notes,
        "selected": {"agent": None, "skills": [], "notes": []},
    })
    html = render_html(view)
    assert view.title == "Use for this chat"
    assert "Selections are unavailable" not in html
    assert "Paper triage" in html and "House style" in html
    assert "chrome_turn_selection_set" in html


def test_the_payload_stamped_for_the_composer_is_the_shape_work_accepts():
    from orchestrator.projection_surfaces.guidance import _selection_payload
    payload = _selection_payload({
        "agent": {"agent_id": AGENT_ID, "revision_id": REVISION_ID},
        "skills": [{"skill_id": SKILL_ID, "revision": 2}],
        "notes": [],
    })
    assert payload == {
        "version": 1,
        "agent": {"agent_id": AGENT_ID, "revision_id": REVISION_ID},
        "skills": [{"skill_id": SKILL_ID, "revision": 2}],
        "notes": [],
    }
    from orchestrator.work_submit import _selected_ids
    _selected_ids(payload)
