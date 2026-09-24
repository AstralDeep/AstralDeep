"""Tests for the notes adapter (projection_surfaces/guidance.py,
personalization/explicit_note_service.py) over real IAM, encryption, SQL, and audit:
create/edit/expiry/search, navigation-scoped delivery, and bounded-wait correlation.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import chrome_events, human_request_authority as human_module
from orchestrator.credential_manager import CredentialManager
from orchestrator.projection_surfaces import guidance
from personalization import phi_gate
from personalization.explicit_note_service import ExplicitNoteCommand, ExplicitNoteService
from persistent_agents.models import AssignmentError
from rote.capabilities import DeviceProfile
from tests.test_guidance_surface_088 import save
from tests.test_human_socket_authority_088 import (
    human as human, bound as bound, fixture as fixture, runtime as runtime,
    service as service, signing_key as signing_key, socket_request as socket_request,
)
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_work_control_authority_088 import incoming

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def notes(human, socket_request, monkeypatch, tmp_path):
    orch = human[2]
    socket, context, _ = socket_request
    orch.ui_sessions[socket]["_client_capabilities"] = ["guidance_notes_v1"]
    orch.credential_manager = CredentialManager(plane_runtime=human[1].plane_runtime, data_dir=str(tmp_path))
    orch.explicit_notes = ExplicitNoteService(orch)
    orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.default())
    monkeypatch.setattr(phi_gate, "_GATE", phi_gate.PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))
    sent = []
    async def send(ws, frame):
        assert ws is socket
        sent.append(json.loads(frame))
        return True
    orch._safe_send = send
    state = SimpleNamespace(orch=orch, socket=socket, context=context, boundary=human[1],
                            sent=sent, pending=[])
    yield state
    guidance.invalidate_navigation(orch, socket)
    for pending in state.pending:
        pending.close()


def capture(state, action="chrome_open", payload=None, *, duplicate_envelope=False):
    if payload is None:
        payload = {"surface": "guidance", "params": {"mode": "list"}}
    message = {"type": "ui_event", "action": action, "payload": payload,
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "connection_generation": str(state.context.connection_generation)}
    if duplicate_envelope:
        payload.update({key: message[key] for key in (
            "submission_id", "request_generation", "connection_generation")})
    pending = human_module.capture_human_socket_request(state.boundary, websocket=state.socket,
        context=state.context, message=message)
    state.pending.append(pending)
    token = guidance.capture_navigation(state.orch, pending=pending)
    return pending, token


async def invoke(state, action="chrome_open", payload=None, *, complete=False):
    pending, token = capture(state, action, payload)
    caller = await pending.authenticate()
    if complete:
        with human_module.bind_human_caller(caller):
            return await guidance.deliver(state.orch, state.socket, caller.owner_id, action,
                pending.message["payload"], pending.request_generation, guidance_navigation=token)
    return await guidance._state(state.orch.explicit_notes, caller, action,
                                  guidance._request(action, pending.message["payload"]))


async def test_actual_create_edit_keep_expiry_toggle_search_and_forget(notes, runtime, fixture):
    payload = save(expiry="Set a date", expiry_date="2030-12-31T23:59:00.123Z")
    state, opened = await invoke(notes, "chrome_note_save", payload)
    identity = payload["note_id"]
    assert state["notice"] == "saved" and len(opened) == 1
    assert opened[0].metadata.revision == 1 and opened[0].metadata.expires_at == 1924991940123
    edit = {"surface": "guidance", "params": {"mode": "edit", "note_id": identity, "expected_revision": 1}}
    state, exact = await invoke(notes, payload=edit)
    assert state["note"]["value"] == payload["fields"]["value"] and exact == opened
    payload["expected_revision"] = 1
    payload["fields"].update(expiry="Keep current expiry", expiry_date="", value="A corrected context.")
    state, revised = await invoke(notes, "chrome_note_save", payload)
    assert revised[0].metadata.revision == 2
    assert revised[0].metadata.expires_at == opened[0].metadata.expires_at
    await invoke(notes, "chrome_note_toggle", {"note_id": identity, "expected_revision": 2, "enabled": False})
    state, disabled = await invoke(notes, "chrome_note_search", {"fields": {"search": "corrected"}})
    assert state["search"] == "corrected" and disabled[0].metadata.enabled is False
    await invoke(notes, "chrome_note_toggle", {"note_id": identity, "expected_revision": 3, "enabled": True})
    state, current = await invoke(notes, payload={"surface": "guidance", "params": {
        "mode": "forget", "note_id": identity, "expected_revision": 4}})
    assert state["mode"] == "forget" and current[0].metadata.revision == 4
    state, erased = await invoke(notes, "chrome_note_forget", {"note_id": identity, "expected_revision": 4})
    assert state["notice"] == "forgotten" and erased == ()
    with runtime.transaction() as tx:
        row = tx.fetch_one("SELECT * FROM explicit_note_current WHERE note_id=%s", (identity,))
        events = tx.fetch_all("SELECT * FROM audit_events WHERE action_type LIKE 'explicit_note_%'")
    assert row["ciphertext"] is None and row["revision"] == 5
    assert len(events) == 5 and all(payload["fields"]["value"] not in str(event) for event in events)
    assert fixture[-1] == [] and not notes.sent


async def test_keep_unrepresentable_expiry_is_exact_and_new_view_is_ephemeral(notes):
    pending, _ = capture(notes, "chrome_note_save", save())
    caller = await pending.authenticate()
    identity = str(uuid4())
    await notes.orch.explicit_notes.command(caller=caller, body=ExplicitNoteCommand(command="save",
        note_id=identity, expected_revision=0, category="context", value="Exact expiry.",
        enabled=True, expires_at=2**53-1))
    payload = save(expiry="Keep current expiry", expiry_date="")
    payload.update(note_id=identity, expected_revision=1)
    _, opened = await invoke(notes, "chrome_note_save", payload)
    assert opened[0].metadata.expires_at == 2**53-1
    state, snapshots = await invoke(notes, payload={"surface": "guidance", "params": {"mode": "new"}})
    assert state["mode"] == "new" and state["note_id"] != identity and snapshots == ()


@pytest.mark.parametrize("mode", ["edit", "forget", "keep"])
async def test_old_revision_cannot_display_or_relabel_current_value(notes, mode):
    payload = save()
    await invoke(notes, "chrome_note_save", payload)
    payload["expected_revision"] = 1
    await invoke(notes, "chrome_note_save", payload)
    if mode == "keep":
        payload["fields"]["expiry"] = "Keep current expiry"
        action = "chrome_note_save"
    else:
        action, payload = "chrome_open", {"surface": "guidance", "params": {
            "mode": mode, "note_id": payload["note_id"], "expected_revision": 1}}
    with pytest.raises(AssignmentError, match="explicit_note_changed"):
        await invoke(notes, action, payload)


async def test_invalid_new_request_retires_previous_and_old_cleanup_preserves_successor(notes):
    first, old = capture(notes)
    caller = await first.authenticate()
    second, current = capture(notes)
    old.close()
    assert notes.orch._guidance_navigation[id(notes.socket)] is current
    with pytest.raises(AssignmentError):
        old.assert_current(notes.orch, notes.socket, caller, first.request_generation)
    with pytest.raises(AssignmentError):
        capture(notes, "chrome_note_search", {"fields": {"search": "", "value": "private"}})
    assert current.closed and not notes.orch._guidance_navigation
    assert repr(current) == "<GuidanceNavigation private>"


async def test_navigation_requires_exact_original_caller_not_same_owner_replacement(notes):
    first, _ = capture(notes)
    old = await first.authenticate()
    second, token = capture(notes)
    with pytest.raises(AssignmentError):
        token.assert_current(notes.orch, notes.socket, old, second.request_generation)
    selected = await second.authenticate()
    token.assert_current(notes.orch, notes.socket, selected, second.request_generation)
    with pytest.raises(AssignmentError):
        token.assert_current(notes.orch, notes.socket, selected, str(uuid4()))
    with pytest.raises(AssignmentError):
        guidance.capture_navigation(notes.orch, pending=object())


@pytest.mark.parametrize("device", ["browser", "ios", "watch"])
async def test_actual_built_snapshot_delivery_is_correlated_and_no_setup_gate(notes, monkeypatch, device):
    await invoke(notes, "chrome_note_save", save())
    notes.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": device})
    async def forbidden(*args, **kwargs):
        raise AssertionError("inert notes must not require a model")
    monkeypatch.setattr(chrome_events, "_llm_gate_refusal", forbidden)
    pending, token = capture(notes)
    caller = await pending.authenticate()
    with human_module.bind_human_caller(caller):
        assert await chrome_events._handle_chrome_event(notes.orch, notes.socket, "chrome_open",
            pending.message["payload"], caller.owner_id, request_generation=pending.request_generation,
            guidance_navigation=token)
    assert notes.sent[-1]["surface_key"] == "guidance"
    assert notes.sent[-1]["request_generation"] == pending.request_generation
    assert "Prefer short paragraphs." in json.dumps(notes.sent[-1])
    assert token.closed and not notes.orch._guidance_navigation


@pytest.mark.parametrize("change", ["correct", "forget", "expire", "session", "navigation", "service", "socket"])
async def test_final_snapshot_or_authority_change_delivers_no_private_frame(notes, monkeypatch, fixture, bound, runtime, change):
    payload = save()
    _, opened = await invoke(notes, "chrome_note_save", payload)
    pending, token = capture(notes)
    caller = await pending.authenticate()
    write = await human_module.authenticate_current_human_request(incoming(bound, fixture), boundary=notes.boundary)
    original = ExplicitNoteService.verify_snapshot
    async def during_render(service, *, caller, notes):
        if change == "correct":
            command = ExplicitNoteCommand(command="save", note_id=payload["note_id"], expected_revision=1,
                category="context", value="Replacement private value.", enabled=True)
            await service.command(caller=write, body=command)
        elif change == "forget":
            await service.command(caller=write, body=ExplicitNoteCommand(
                command="forget", note_id=payload["note_id"], expected_revision=1))
        elif change == "expire":
            monkeypatch.setattr("personalization.explicit_note_service._now", lambda: 2**53-1)
        elif change == "session":
            old = await asyncio.to_thread(get_session_record, runtime, fixture[2])
            replacement = await asyncio.to_thread(replace_session_record, runtime, old)
            assert replacement.incarnation_id != old.incarnation_id
        elif change == "navigation":
            guidance.invalidate_navigation(state.orch, state.socket)
        elif change == "service":
            state.orch.explicit_notes = object()
        else:
            state.socket.closed = True
        await original(service, caller=caller, notes=notes)
    state = notes
    if change == "expire":
        pending.close()
        payload["expected_revision"] = 1
        payload["fields"].update(expiry="Set a date", expiry_date="2030-12-31T23:59:00Z")
        await invoke(notes, "chrome_note_save", payload)
        pending, token = capture(notes)
        caller = await pending.authenticate()
    monkeypatch.setattr(ExplicitNoteService, "verify_snapshot", during_render)
    with human_module.bind_human_caller(caller), pytest.raises(AssignmentError):
        await guidance.deliver(notes.orch, notes.socket, caller.owner_id, "chrome_open",
            pending.message["payload"], pending.request_generation, guidance_navigation=token)
    assert not notes.sent


@pytest.mark.parametrize("loss", ["capability", "service", "token", "owner", "payload", "send"])
async def test_unavailable_context_never_silently_claims_delivery(notes, loss):
    if loss == "capability":
        notes.orch.ui_sessions[notes.socket]["_client_capabilities"] = []
    pending, token = capture(notes)
    caller = await pending.authenticate()
    payload = pending.message["payload"]
    if loss == "service":
        notes.orch.explicit_notes = object()
    elif loss == "token":
        token = None
    elif loss == "payload":
        payload = {"surface": "guidance", "params": {"mode": "new"}}
    elif loss == "send":
        async def decline(*_):
            return False
        notes.orch._safe_send = decline
    with human_module.bind_human_caller(caller), pytest.raises(AssignmentError):
        await guidance.deliver(notes.orch, notes.socket, "another-owner" if loss == "owner" else caller.owner_id,
            "chrome_open", payload, pending.request_generation, guidance_navigation=token)
    assert not notes.sent


async def test_generic_render_and_unknown_note_action_cannot_bypass_private_lifetime(notes):
    with pytest.raises(AssignmentError):
        await chrome_events._render_surface(notes.orch, notes.socket, "owner", [], "guidance", {})
    with pytest.raises(AssignmentError):
        await chrome_events.handle_chrome_event(notes.orch, notes.socket, "chrome_note_unknown", {}, "owner")
    assert not notes.sent


async def test_transport_wait_is_bounded_by_original_request_deadline(notes):
    pending, token = capture(notes)
    pending.deadline = time.monotonic() + 1
    caller = await pending.authenticate()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def stalled(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    notes.orch._safe_send = stalled
    with human_module.bind_human_caller(caller):
        with pytest.raises(AssignmentError, match="explicit_note_delivery_timeout"):
            async with asyncio.timeout(3):
                await guidance.deliver(notes.orch, notes.socket, caller.owner_id, "chrome_open",
                    pending.message["payload"], pending.request_generation, guidance_navigation=token)
    assert entered.is_set() and cancelled.is_set() and token.closed and not notes.sent


async def test_authenticated_chrome_wrapper_passes_only_its_original_pending_token(notes):
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT
    pending, token = capture(notes)
    context = _CONNECTION_OPERATION_CONTEXT.set({"human_request": pending})
    try:
        assert await chrome_events.handle_chrome_event(notes.orch, notes.socket, "chrome_open",
            pending.message["payload"], "untrusted-handler-owner", request_generation=pending.request_generation,
            guidance_navigation=token)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(context)
    assert len(notes.sent) == 1 and notes.sent[0]["request_generation"] == pending.request_generation


async def test_actual_wire_duplicates_are_validated_then_removed_only_from_note_fields(notes):
    payload = save()
    pending, token = capture(notes, "chrome_note_save", payload, duplicate_envelope=True)
    caller = await pending.authenticate()
    with human_module.bind_human_caller(caller):
        assert await guidance.deliver(notes.orch, notes.socket, caller.owner_id, "chrome_note_save",
            pending.message["payload"], pending.request_generation, guidance_navigation=token)
    assert notes.sent[0]["request_generation"] == pending.request_generation
    assert "Prefer short paragraphs." in notes.sent[0]["html"]
    assert "Note saved." in notes.sent[0]["html"]
    with pytest.raises(AssignmentError):
        guidance._request("chrome_note_save", payload)


@pytest.mark.parametrize("key", ["submission_id", "request_generation", "connection_generation"])
async def test_conflicting_wire_duplicates_never_produce_an_admitted_notes_token(notes, key):
    payload = {"surface": "guidance", key: str(uuid4())}
    with pytest.raises(AssignmentError):
        capture(notes, payload=payload)
    assert not getattr(notes.orch, "_guidance_navigation", {}) and not notes.sent


@pytest.mark.parametrize("mode", ["list", "new", "edit"])
async def test_watch_delivery_retains_complete_note_navigation_and_form(notes, mode):
    payload = save()
    await invoke(notes, "chrome_note_save", payload)
    notes.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": "watch"})
    params = {"mode": mode}
    if mode == "edit":
        params.update(note_id=payload["note_id"], expected_revision=1)
    await invoke(notes, payload={"surface": "guidance", "params": params}, complete=True)
    frame = notes.sent[-1]
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == "guidance"
    nodes = list(frame["components"])
    nodes.extend(child for node in tuple(nodes) if node["type"] == "card" for child in node["content"])
    labels = {node["label"] for node in nodes if node["type"] == "button"}
    if mode == "list":
        assert {"Add note", "Edit", "Disable", "Forget", "Refresh"} <= labels
    else:
        assert "Back to notes" in labels
        forms = [node for node in nodes if node["type"] == "param_picker"]
        assert len(forms) == 1 and forms[0]["submit_action"] == "chrome_note_save"
        assert {field["name"] for field in forms[0]["fields"]} == {
            "category", "value", "enabled", "expiry", "expiry_date"}
        assert forms[0]["submit_payload"]["expected_revision"] == (1 if mode == "edit" else 0)


@pytest.mark.parametrize("restriction", ["type", "interactivity", "actions"])
async def test_native_host_limit_refuses_entire_private_notes_view(notes, restriction):
    await invoke(notes, "chrome_note_save", save())
    profile = DeviceProfile.from_dict({"device_type": "watch"})
    if restriction == "type":
        profile.supported_types = frozenset({"text", "card", "badge", "button", "alert"})
    elif restriction == "interactivity":
        profile.supports_interactivity = False
    else:
        profile.max_actions = 1
    notes.orch.rote.get_profile = lambda _: profile
    with pytest.raises(ValueError, match="guidance_surface_unavailable"):
        await invoke(notes, complete=True)
    assert notes.sent == [] and not notes.orch._guidance_navigation
