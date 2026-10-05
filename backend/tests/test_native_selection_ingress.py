"""Runs the native Advanced picker through authenticated socket admission and delivery.
Selection confirmations remain owner-scoped, correlated and fenced across navigation changes.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from personalization.explicit_note_service import ExplicitNoteService
from rote.capabilities import DeviceProfile
from tests.test_guidance_ingress_088 import (
    notes as notes, send, terminal, metadata as metadata, runtime as runtime,
    fixture as fixture, service as service, signing_key as signing_key,
    ingress as ingress, surface as surface, command as command, context as context,
    registered, cleaned,
)


@pytest.fixture
async def native_picker(notes, monkeypatch):
    state = notes
    await registered(state)
    state.orch.ui_sessions[state.socket]["_client_capabilities"].append("guidance_selection_v1")
    state.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({
        "device_type": "ios", "console_contract": "console/v2",
        "supported_types": ["text", "alert", "badge", "card", "button", "param_picker"],
    })
    agent_id, revision_id = str(uuid4()), str(uuid4())

    async def offered(*, caller):
        state.catalog_owner = caller.owner_id
        return [SimpleNamespace(agent_id=agent_id, selected_definition_revision_id=revision_id,
                                display_name="Owner picker agent", status="active")]

    state.orch.declarative_agents = SimpleNamespace(list_heads=offered)
    state.selection = {"version": 1, "agent": {"agent_id": agent_id, "revision_id": revision_id},
                       "skills": [], "notes": []}
    return state


def frames(state, generation=None):
    return [value for value in state.socket.payloads()
            if value.get("type") == "chrome_surface" and value.get("surface_key") == "guidance"
            and (generation is None or value.get("request_generation") == generation)]


async def test_actual_native_advanced_open_delivers_the_shared_picker_without_a_chat(native_picker, fixture):
    generation = send(native_picker, payload={"surface": "guidance", "params": {"view": "selection"}})
    assert (await terminal(native_picker, generation))["state"] == "completed"
    frame, = frames(native_picker, generation)
    assert frame["title"] == "Use for this chat" and "selection" not in frame
    assert "Owner picker agent" in json.dumps(frame) and "Use this agent" in json.dumps(frame)
    assert native_picker.catalog_owner == fixture[1]
    assert native_picker.orch._ws_active_chat == {} and native_picker.orch._chat_recorders == {}


async def test_actual_native_ordinary_open_echoes_the_admitted_generation(native_picker, monkeypatch, fixture):
    from orchestrator.projection_surfaces import theme

    async def components(host, owner, roles, params):
        assert host is native_picker.orch and owner == fixture[1]
        return [{"type": "text", "content": "Current owner theme"}]

    monkeypatch.setattr(theme, "components", components)
    generation = send(native_picker, payload={"surface": "theme", "params": {}})
    assert (await terminal(native_picker, generation))["state"] == "completed"
    emitted, = [frame for frame in native_picker.socket.payloads()
                if frame.get("type") == "chrome_surface" and frame.get("surface_key") == "theme"]
    assert emitted["request_generation"] == generation
    assert emitted["components"][0]["content"] == "Current owner theme"
    assert native_picker.orch._ws_active_chat == {} and native_picker.orch._chat_recorders == {}


async def test_actual_native_selection_confirmation_keeps_its_admitted_generation(native_picker, fixture):
    generation = send(native_picker, action="chrome_turn_selection_set", payload=native_picker.selection,
                      transport_session=str(uuid4()))
    status = await terminal(native_picker, generation)
    assert status["state"] == "completed"
    frame, = frames(native_picker, generation)
    assert frame["selection"] == native_picker.selection
    assert frame["request_generation"] == generation and native_picker.catalog_owner == fixture[1]
    admitted = next(frame for frame in native_picker.frames if str(frame.request_generation) == generation)
    assert admitted.chat_id is None
    await cleaned(admitted)
    assert admitted.human_request is None and admitted.guidance_navigation is None
    assert native_picker.orch._ws_active_chat == {} and native_picker.orch._chat_recorders == {}


async def test_actual_native_selection_clear_confirms_an_empty_selection(native_picker):
    empty = {"version": 1, "agent": None, "skills": [], "notes": []}
    generation = send(native_picker, action="chrome_turn_selection_set", payload=empty)
    assert (await terminal(native_picker, generation))["state"] == "completed"
    assert frames(native_picker, generation)[0]["selection"] == empty


async def test_actual_native_picker_without_capability_refuses_without_disclosing_catalog(native_picker):
    native_picker.orch.ui_sessions[native_picker.socket]["_client_capabilities"].remove("guidance_selection_v1")
    generation = send(native_picker, payload={"surface": "guidance", "params": {"view": "selection"}})
    assert (await terminal(native_picker, generation))["state"] == "failed"
    assert not frames(native_picker, generation)
    assert not hasattr(native_picker, "catalog_owner")


async def test_actual_native_picker_cannot_deliver_after_navigation_changes(native_picker, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = ExplicitNoteService.verify_snapshot

    async def held(service, *, caller, notes):
        entered.set()
        await release.wait()
        await original(service, caller=caller, notes=notes)

    monkeypatch.setattr(ExplicitNoteService, "verify_snapshot", held)
    generation = send(native_picker, payload={"surface": "guidance", "params": {"view": "selection"}})
    try:
        await asyncio.wait_for(entered.wait(), 5)
        native_picker.socket.feed(json.dumps({"type": "ui_event", "action": "chrome_close",
            "submission_id": str(uuid4()), "request_generation": str(uuid4()),
            "connection_generation": native_picker.connection_generation, "payload": {}}))
        await native_picker.barrier()
        release.set()
        assert (await terminal(native_picker, generation))["state"] == "failed"
    finally:
        release.set()
    assert not frames(native_picker, generation)
    assert not getattr(native_picker.orch, "_guidance_navigation", {})
