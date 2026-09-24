"""Tests for private-notes ingress (orchestrator/human_request_authority.py,
projection_surfaces/guidance.py, personalization/explicit_note_service.py) through
the real bounded-socket admission and delivery path, not a fabricated caller.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import human_request_authority as human
from orchestrator import orchestrator as module
from orchestrator.credential_manager import CredentialManager
from orchestrator.projection_surfaces import guidance
from personalization import phi_gate
from personalization.explicit_note_service import ExplicitNoteService
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_guidance_surface_088 import save
from tests.test_human_socket_ingress_088 import (
    metadata as metadata, ingress as ingress, surface as surface, command as command,
    context as context, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, registered, cleaned,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def notes(metadata, runtime, monkeypatch, tmp_path):
    state, _, audits = metadata
    orch = state.orch
    orch.credential_manager = CredentialManager(plane_runtime=runtime, data_dir=str(tmp_path))
    orch.explicit_notes = ExplicitNoteService(orch)
    monkeypatch.setattr(phi_gate, "_GATE", phi_gate.PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))
    original = orch.handle_ui_message

    async def handle(ws, raw):
        await original(ws, raw)
        if json.loads(raw).get("type") == "register_ui":
            orch.ui_sessions[ws]["_client_capabilities"].append("guidance_notes_v1")

    orch.handle_ui_message = handle
    state.note_audits = audits
    yield state
    assert not getattr(orch, "_guidance_navigation", {})


def send(state, *, action="chrome_open", payload=None, generation=None, duplicate=True, transport_session=None):
    payload = deepcopy(payload if payload is not None else {"surface": "guidance", "params": {"mode": "list"}})
    generation = generation or str(uuid4())
    frame = {"type": "ui_event", "action": action, "payload": payload,
        "submission_id": str(uuid4()), "request_generation": generation,
        "connection_generation": state.connection_generation}
    if transport_session is not None:
        frame["session_id"] = transport_session
    if duplicate:
        payload.update({key: frame[key] for key in (
            "submission_id", "request_generation", "connection_generation")})
    if not hasattr(state, "note_submissions"):
        state.note_submissions = {}
    state.note_submissions[generation] = frame["submission_id"]
    state.socket.feed(json.dumps(frame))
    return generation


def outputs(state, generation=None):
    return [value for value in state.socket.payloads() if value.get("type") == "chrome_render"
            and value.get("surface_key") == "guidance"
            and (generation is None or value.get("request_generation") == generation)]


async def terminal(state, generation):
    return await state.arrived(lambda value: value.get("type") == "operation_status"
        and value.get("terminal") is True and value.get("request_generation") == generation)


async def seed(state):
    await registered(state)
    payload = save(value="Private owner note through actual ingress.")
    generation = send(state, action="chrome_note_save", payload=payload)
    assert (await terminal(state, generation))["state"] == "completed"
    assert len(outputs(state, generation)) == 1
    frame = next(frame for frame in state.frames if str(frame.request_generation) == generation)
    await cleaned(frame)
    assert frame.guidance_navigation is None
    return payload["note_id"]


async def test_actual_capture_token_and_current_caller_reach_notes_delivery(notes, monkeypatch, fixture):
    refreshes_before = len(fixture[-1])
    await seed(notes)
    calls = []
    original = guidance.deliver

    async def deliver(orch, websocket, owner, action, payload, generation, **kwargs):
        operation = module._CONNECTION_OPERATION_CONTEXT.get()
        navigation = kwargs["guidance_navigation"]
        caller = human.current_human_caller(expected_orchestrator=orch)
        assert type(navigation) is guidance.GuidanceNavigation
        assert navigation is operation["guidance_navigation"]
        assert navigation.pending is operation["human_request"] is caller._binding.socket_request
        assert caller.require_session().credential.session_id == fixture[2]
        calls.append((navigation, caller, owner))
        return await original(orch, websocket, owner, action, payload, generation, **kwargs)

    monkeypatch.setattr(guidance, "deliver", deliver)
    generation = send(notes, transport_session=str(uuid4()))
    assert (await terminal(notes, generation))["state"] == "completed"
    frame = next(frame for frame in notes.frames if str(frame.request_generation) == generation)
    await cleaned(frame)
    assert frame.read_only is True and frame.chat_id is None
    assert frame.guidance_navigation is None and calls[0][0].closed
    assert calls[0][2] == fixture[1] and len(outputs(notes, generation)) == 1
    assert "Private owner note through actual ingress." in outputs(notes, generation)[0]["html"]
    assert notes.orch._ws_active_chat == {} and notes.orch._chat_recorders == {}
    assert all(event["payload"] == {} and event["chat_id"] is None for event in notes.note_audits)
    assert len(fixture[-1]) == refreshes_before


async def test_preregistration_notes_never_adopt_later_login(notes):
    generation = send(notes)
    await notes.barrier()
    await registered(notes)
    await notes.barrier()
    assert not outputs(notes) and not any(str(frame.request_generation) == generation for frame in notes.frames)


@pytest.mark.parametrize("boundary", ["session_captured", "admission"])
async def test_original_issuance_is_selected_before_queued_wait(notes, runtime, fixture, monkeypatch, boundary):
    refreshes_before = len(fixture[-1])
    await seed(notes)
    entered, release = asyncio.Event(), asyncio.Event()
    if boundary == "session_captured":
        original = human._HumanSocketRequest.capture_session

        async def held(pending):
            await original(pending)
            if pending.message.get("action") == "chrome_open" and not entered.is_set():
                assert notes.orch._guidance_navigation[id(notes.socket)].pending is pending
                assert pending.observation is not None
                entered.set()
                await release.wait()

        monkeypatch.setattr(human._HumanSocketRequest, "capture_session", held)
    else:
        original = notes.orch._call_work_admission

        async def held(callback, *args, **kwargs):
            if getattr(callback, "__name__", "") == "_submit_connection_batch":
                pending = notes.orch._guidance_navigation[id(notes.socket)].pending
                assert pending.observation is not None
                entered.set()
                await release.wait()
            return await original(callback, *args, **kwargs)

        monkeypatch.setattr(notes.orch, "_call_work_admission", held)
    generation = send(notes)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        old = await asyncio.to_thread(get_session_record, runtime, fixture[2])
        replacement = await asyncio.to_thread(replace_session_record, runtime, old)
        assert replacement.incarnation_id != old.incarnation_id
        release.set()
        assert (await terminal(notes, generation))["state"] == "failed"
    finally:
        release.set()
    frame = next(frame for frame in notes.frames if str(frame.request_generation) == generation)
    await cleaned(frame)
    assert frame.guidance_navigation is None
    assert not outputs(notes, generation) and len(fixture[-1]) == refreshes_before


@pytest.mark.parametrize("retirement", ["close", "navigate", "register", "disconnect", "invalid"])
async def test_navigation_and_connection_retirement_prevent_late_private_frame(notes, monkeypatch, retirement):
    await seed(notes)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ExplicitNoteService.verify_snapshot
    target = str(uuid4())

    async def held(service, *, caller, notes):
        if caller._binding.socket_request.request_generation == target:
            entered.set()
            await release.wait()
        await original(service, caller=caller, notes=notes)

    monkeypatch.setattr(ExplicitNoteService, "verify_snapshot", held)
    send(notes, generation=target)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if retirement == "disconnect":
            notes.socket.closed = True
            notes.socket.disconnect()
        elif retirement == "register":
            await registered(notes)
        elif retirement == "invalid":
            send(notes, action="chrome_note_save", payload={"fields": {"value": "invalid private value"}})
            await notes.barrier()
        else:
            send(notes, action="chrome_close" if retirement == "close" else "chrome_open",
                 payload={} if retirement == "close" else {"surface": "work", "params": {"mode": "list"}})
            await notes.barrier()
        release.set()
        if retirement != "disconnect":
            assert (await terminal(notes, target))["state"] == "failed"
    finally:
        release.set()
    frame = next(frame for frame in notes.frames if str(frame.request_generation) == target)
    await cleaned(frame)
    assert frame.guidance_navigation is None and not outputs(notes, target)


async def test_newer_notes_navigation_wins_and_old_cleanup_cannot_close_it(notes, monkeypatch):
    await seed(notes)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ExplicitNoteService.verify_snapshot
    old_generation = str(uuid4())

    async def held(service, *, caller, notes):
        if caller._binding.socket_request.request_generation == old_generation:
            entered.set()
            await release.wait()
        await original(service, caller=caller, notes=notes)

    monkeypatch.setattr(ExplicitNoteService, "verify_snapshot", held)
    send(notes, generation=old_generation)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current = send(notes, payload={"surface": "guidance", "params": {"mode": "new"}})
        assert (await terminal(notes, current))["state"] == "completed"
        release.set()
        assert (await terminal(notes, old_generation))["state"] == "failed"
    finally:
        release.set()
    assert not outputs(notes, old_generation) and len(outputs(notes, current)) == 1


@pytest.mark.parametrize("action,payload", [
    ("chrome_note_unknown", {}),
    ("chrome_note_save", {"fields": {"value": "no identity"}}),
    ("chrome_note_toggle", {"note_id": str(uuid4()), "expected_revision": True, "enabled": False}),
])
async def test_closed_command_refusal_never_becomes_completed_or_private_output(notes, action, payload):
    await registered(notes)
    generation = send(notes, action=action, payload=payload)
    value = await notes.arrived(lambda event:
        (event.get("request_generation") == generation and event.get("type") == "operation_status"
         and event.get("terminal") is True)
        or (event.get("type") == "error" and event.get("submission_id") == notes.note_submissions[generation]))
    if value["type"] == "error":
        assert value["accepted"] is False and value["retryable"] is False
        assert value["code"] == "operation_failed" and "fields" not in value
    else:
        assert value["state"] == "failed"
    frame = next(frame for frame in notes.frames if str(frame.request_generation) == generation)
    await cleaned(frame)
    assert not outputs(notes) and frame.guidance_navigation is None
