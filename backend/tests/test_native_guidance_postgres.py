"""Exercises native selection confirmation through real Plane-backed human authority.
Catalog snapshots stay owner scoped and stale navigation cannot deliver selection metadata.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import human_request_authority as human_module, user_skills
from orchestrator.projection_surfaces import guidance
from personalization.explicit_note_service import ExplicitNoteService
from persistent_agents.models import AssignmentError
from rote.capabilities import DeviceProfile
from tests.test_guidance_surface_088 import save
from tests.test_guidance_surface_postgres_088 import (
    bound as bound, capture, fixture as fixture, human as human, invoke, notes as notes,
    runtime as runtime, service as service, signing_key as signing_key,
    socket_request as socket_request,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def picker(notes, monkeypatch):
    notes.orch.ui_sessions[notes.socket]["_client_capabilities"] = [
        "guidance_notes_v1", "guidance_selection_v1",
    ]
    notes.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({
        "device_type": "ios", "console_contract": "console/v2",
        "supported_types": ["text", "alert", "badge", "card", "button", "param_picker"],
    })
    note_command = save()
    await invoke(notes, "chrome_note_save", note_command)
    agent_id, revision_id, skill_id = (str(uuid4()) for _ in range(3))
    lookups = []

    async def agents(*, caller):
        lookups.append(caller.owner_id)
        return [SimpleNamespace(agent_id=agent_id, selected_definition_revision_id=revision_id,
                                display_name="Selected agent", status="active")]

    async def skills(*, caller):
        lookups.append(caller.owner_id)
        return [SimpleNamespace(skill_id=skill_id, revision=1, name="Short responses",
                                command="brief", enabled=True)]

    notes.orch.declarative_agents = SimpleNamespace(list_heads=agents)
    monkeypatch.setattr(user_skills, "store_for", lambda _: SimpleNamespace(list=skills))
    notes.chosen = {"version": 1, "agent": {"agent_id": agent_id, "revision_id": revision_id},
                    "skills": [{"skill_id": skill_id, "revision": 1}],
                    "notes": [{"note_id": note_command["note_id"], "revision": 1}]}
    notes.lookups = lookups
    notes.secret_note = note_command["fields"]["value"]
    return notes


async def deliver(state, action="chrome_turn_selection_set", payload=None, *, owner=None):
    pending, token = capture(state, action, state.chosen if payload is None else payload)
    caller = await pending.authenticate()
    with human_module.bind_human_caller(caller):
        await guidance.deliver(state.orch, state.socket, caller.owner_id if owner is None else owner,
            action, pending.message["payload"], pending.request_generation, guidance_navigation=token)
    return pending


async def test_real_native_selection_confirmation_is_owner_scoped_and_data_free(picker, fixture):
    pending = await deliver(picker)
    frame, = picker.sent
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == "guidance"
    assert frame["selection"] == picker.chosen
    assert frame["request_generation"] == pending.request_generation
    assert picker.secret_note not in json.dumps(frame)
    assert all(owner == fixture[1] for owner in picker.lookups)
    assert "Selection saved." in json.dumps(frame)


async def test_picker_open_has_shared_controls_without_replacing_client_selection(picker):
    await deliver(picker, "chrome_open", {"surface": "guidance", "params": {"view": "selection"}})
    frame, = picker.sent
    assert "selection" not in frame
    assert "Use this agent" in json.dumps(frame)
    assert "Add skill" in json.dumps(frame) and "Add note" in json.dumps(frame)
    assert picker.secret_note not in json.dumps(frame)


async def test_clear_confirmation_carries_explicit_empty_selection(picker):
    clear = {"version": 1, "agent": None, "skills": [], "notes": []}
    await deliver(picker, payload=clear)
    assert picker.sent[-1]["selection"] == clear
    assert "Selection cleared." in json.dumps(picker.sent[-1])


@pytest.mark.parametrize("change", ["foreign", "stale"])
async def test_foreign_or_stale_references_are_not_confirmed(picker, change):
    chosen = json.loads(json.dumps(picker.chosen))
    chosen["agent"]["revision_id"] = str(uuid4())
    for kind, identity in (("skills", "skill_id"), ("notes", "note_id")):
        if change == "foreign":
            chosen[kind][0][identity] = str(uuid4())
        else:
            chosen[kind][0]["revision"] += 1
    await deliver(picker, payload=chosen)
    assert picker.sent[-1]["selection"] == {"version": 1, "agent": None, "skills": [], "notes": []}
    assert chosen["agent"]["revision_id"] not in json.dumps(picker.sent[-1])


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 2}, {"owner_id": "foreign"},
    {"skills": [{"skill_id": str(uuid4()), "revision": True}]},
])
async def test_malformed_selection_never_admits_navigation(picker, changes):
    with pytest.raises(AssignmentError, match="explicit_note_request_invalid"):
        await deliver(picker, payload={**picker.chosen, **changes})
    assert picker.sent == []


@pytest.mark.parametrize("loss", ["owner", "capability", "navigation", "connection"])
async def test_owner_capability_and_stale_request_denials_emit_no_selection(picker, monkeypatch, loss):
    if loss == "owner":
        with pytest.raises(AssignmentError):
            await deliver(picker, owner="foreign-owner")
    elif loss == "capability":
        picker.orch.ui_sessions[picker.socket]["_client_capabilities"] = ["guidance_notes_v1"]
        with pytest.raises(AssignmentError):
            await deliver(picker)
    else:
        original = ExplicitNoteService.verify_snapshot

        async def retire(service, *, caller, notes):
            await original(service, caller=caller, notes=notes)
            if loss == "navigation":
                guidance.invalidate_navigation(picker.orch, picker.socket)
            else:
                picker.context.connection_generation = uuid4()

        monkeypatch.setattr(ExplicitNoteService, "verify_snapshot", retire)
        with pytest.raises(AssignmentError):
            await deliver(picker)
    assert picker.sent == []


@pytest.mark.parametrize("device", ["windows", "watch", "ios"])
async def test_unnegotiated_client_receives_no_selection_metadata(picker, device):
    picker.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": device})
    await deliver(picker)
    frame, = picker.sent
    assert "selection" not in frame
    assert [node["type"] for node in frame["components"]] == ["alert"]


@pytest.mark.parametrize("surface", ["notes", "selection"])
async def test_negotiated_watch_uses_only_the_scoped_guidance_capability(picker, surface):
    profile = DeviceProfile.from_dict({"device_type": "watch", "console_contract": "console/v2",
        "supported_types": ["text", "alert", "badge", "card", "button"]})
    picker.orch.rote.get_profile = lambda _: profile
    if surface == "selection":
        await deliver(picker)
        assert picker.sent[-1]["selection"] == picker.chosen
    else:
        await deliver(picker, "chrome_open", {"surface": "guidance", "params": {"mode": "new"}})
        assert "chrome_note_save" in json.dumps(picker.sent[-1])
        assert "param_picker" in json.dumps(picker.sent[-1])
    assert "param_picker" not in profile.supported_types
