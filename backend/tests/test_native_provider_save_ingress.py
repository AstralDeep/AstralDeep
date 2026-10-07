"""Exercises provider Save through authenticated socket admission and the encrypted Plane store.
Persistence acknowledges and unlocks setup before testing, with warnings that cannot undo an accepted save.
"""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from llm_config.data_sharing import DataSharingStore
from llm_config.typesafe_store import TypeSafeCredentialStore
from llm_config.user_store import UserLLMConfigStore
from orchestrator import chrome_events, llm_gate
from orchestrator.projection_surfaces import llm as llm_surface
from rote.capabilities import DeviceProfile
from tests.test_native_selection_ingress import (
    native_picker as native_picker, notes as notes, metadata as metadata, runtime as runtime,
    fixture as fixture, service as service, signing_key as signing_key, ingress as ingress,
    surface as surface, command as command, context as context, send, terminal,
)


KEY = "sk-native-provider-secret-1234567890"
FIELDS = {"provider": "openai", "api_key": KEY, "model": "gpt-4o-mini",
          "data_sharing_acknowledged": True}


@pytest.fixture
async def provider(native_picker, runtime, monkeypatch, fixture):
    state = native_picker
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    state.orch._llm_store = UserLLMConfigStore(plane_runtime=runtime)
    state.orch._data_sharing_store = DataSharingStore(plane_runtime=runtime)
    state.orch._ws_llm_gated = {}
    state.orch._ws_welcome = {}
    state.orch._ff_llm_first_run = True
    state.audit_events = []

    async def record(event):
        state.audit_events.append(event)

    state.orch.audit_recorder = SimpleNamespace(record=record)

    async def configured(owner):
        return await state.orch._llm_store.get(owner) is not None

    monkeypatch.setattr(state.orch, "llm_configured_for", configured)
    monkeypatch.setattr(llm_gate, "_send_welcome", AsyncMock())
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", AsyncMock(return_value=(True, None, None)))
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {
        action: ("llm", handler) for action, handler in llm_surface.HANDLERS.items()})
    return state


def request(state, fields=None):
    return send(state, action="chrome_llm_save", payload={
        "surface": "llm_settings", "fields": dict(FIELDS if fields is None else fields)})


async def encrypted(runtime, owner):
    def read():
        with runtime.transaction() as transaction:
            return runtime.repositories.encrypted_llm_config.get_user(transaction, owner_id=owner)
    return await asyncio.to_thread(read)


@pytest.mark.parametrize("device", ["ios", "macos", "android", "windows", "browser"])
async def test_actual_save_commits_and_unblocks_first_run_before_failed_probe(provider, fixture, runtime, monkeypatch, device):
    state, owner = provider, fixture[1]
    state.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": device,
        "supported_types": ["text", "alert", "button", "param_picker", "container"]})
    await llm_gate.push_setup_dialog(state.orch, state.socket, owner)
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(**kwargs):
        assert (await state.orch._llm_store.get(owner)).api_key == KEY
        assert not llm_gate.is_gated(state.orch, state.socket)
        assert any(frame.get("type") == "llm_config_ack" and frame.get("ok") is True
                   for frame in state.socket.payloads())
        entered.set()
        await release.wait()
        return False, "transport_error", "private upstream " + KEY

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    generation = request(state)
    try:
        result = await terminal(state, generation)
        assert result["state"] == "completed" and result["label"] == "Provider settings saved"
        await asyncio.wait_for(entered.wait(), 5)
        if device != "browser":
            closed, = [frame for frame in state.socket.payloads()
                       if frame.get("type") == "chrome_surface" and frame.get("surface_key") == ""]
            assert closed["components"] == [] and "request_generation" not in closed
        record = await encrypted(runtime, owner)
        assert record.api_key_ciphertext and KEY not in record.api_key_ciphertext
        assert record.owner_id == owner and record.scope == "user"
        release.set()
        warning = await state.arrived(lambda frame: frame.get("type") == "notification")
        assert warning["title"] == "Provider settings saved" and warning["level"] == "warning"
        assert "Connection test failed" in warning["body"] and KEY not in json.dumps(warning)
        assert await state.orch.llm_configured_for(owner) is True
        assert not any(frame.get("code") == "llm_config_invalid" for frame in state.socket.payloads())
        assert [event.inputs_meta.get("action") for event in state.audit_events
                if event.event_class == "llm_config_change"] == ["created", "tested"]
        assert KEY not in json.dumps([event.model_dump(mode="json") for event in state.audit_events])
    finally:
        release.set()


async def test_actual_invalid_save_leaves_mandatory_setup_and_skips_storage_probe(provider, fixture, monkeypatch):
    owner = fixture[1]
    await llm_gate.push_setup_dialog(provider.orch, provider.socket, owner)
    probe = AsyncMock()
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    generation = request(provider, dict(FIELDS, model=""))
    assert (await terminal(provider, generation))["state"] == "failed"
    assert await provider.orch._llm_store.get(owner) is None
    assert llm_gate.is_gated(provider.orch, provider.socket)
    assert not any(frame.get("type") == "llm_config_ack" for frame in provider.socket.payloads())
    probe.assert_not_awaited()


async def test_actual_unacknowledged_save_still_fails_before_persistence(provider, fixture, monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    generation = request(provider, dict(FIELDS, data_sharing_acknowledged=False))
    assert (await terminal(provider, generation))["state"] == "failed"
    assert await provider.orch._llm_store.get(fixture[1]) is None
    probe.assert_not_awaited()


async def test_actual_legacy_save_acks_and_retains_a_failed_connection_check(provider, fixture, monkeypatch):
    await provider.orch._data_sharing_store.acknowledge(fixture[1])
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", AsyncMock(
        return_value=(False, "auth_failed", "private authentication response")))
    generation = str(uuid4())
    provider.socket.feed(json.dumps({"type": "llm_config_set", "config": FIELDS,
        "submission_id": str(uuid4()), "request_generation": generation,
        "connection_generation": provider.connection_generation}))
    assert (await terminal(provider, generation))["state"] == "completed"
    warning = await provider.arrived(lambda frame: frame.get("type") == "notification")
    assert warning["level"] == "warning"
    assert (await provider.orch._llm_store.get(fixture[1])).api_key == KEY


async def test_actual_saved_key_cannot_be_forwarded_to_a_changed_endpoint(provider, fixture, monkeypatch):
    await provider.orch._llm_store.set(fixture[1], provider="custom", api_key=KEY,
        base_url="https://old.example/v1", model="old-model")
    probe = AsyncMock()
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    generation = request(provider, dict(FIELDS, provider="custom", api_key="",
        base_url="https://changed.example/v1"))
    result = await terminal(provider, generation)
    assert result["state"] == "failed" and "enter the API key again" in result["error"]["message"]
    saved = await provider.orch._llm_store.get(fixture[1])
    assert saved.base_url == "https://old.example/v1" and saved.api_key == KEY
    probe.assert_not_awaited()


async def test_actual_settings_save_uses_authenticated_owner_despite_forged_owner(provider, fixture, runtime):
    foreign = str(uuid4())
    generation = send(provider, action="chrome_llm_save", payload={
        "surface": "llm_settings", "user_id": foreign, "fields": FIELDS})
    assert (await terminal(provider, generation))["state"] == "completed"
    await provider.arrived(lambda frame: frame.get("type") == "llm_config_ack")
    assert (await encrypted(runtime, fixture[1])).owner_id == fixture[1]
    assert await encrypted(runtime, foreign) is None
    assert foreign not in json.dumps(provider.socket.payloads())


async def test_actual_slow_save_audit_cannot_hide_completed_acknowledgement(provider, fixture):
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.orch.audit_recorder.record

    async def record(event):
        if event.action_type == "llm_config.created":
            entered.set()
            await release.wait()
        await original(event)

    provider.orch.audit_recorder.record = record
    generation = request(provider)
    try:
        assert (await terminal(provider, generation))["state"] == "completed"
        await asyncio.wait_for(entered.wait(), 5)
        assert any(frame.get("type") == "llm_config_ack" and frame.get("ok") is True
                   for frame in provider.socket.payloads())
        assert await provider.orch._llm_store.get(fixture[1]) is not None
    finally:
        release.set()


async def test_registered_legacy_handler_unlocks_before_its_advisory_probe(provider, fixture, monkeypatch):
    owner = fixture[1]
    await provider.orch._data_sharing_store.acknowledge(owner)
    provider.orch._ws_llm_gated[id(provider.socket)] = True

    async def probe(**kwargs):
        assert not llm_gate.is_gated(provider.orch, provider.socket)
        assert (await provider.orch._llm_store.get(owner)).api_key == KEY
        assert any(frame.get("type") == "llm_config_ack" for frame in provider.socket.payloads())
        return False, "transport_error", "private upstream"

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    await provider.orch.handle_ui_message(provider.socket,
        json.dumps({"type": "llm_config_set", "config": FIELDS}))
    warning, = [frame for frame in provider.socket.payloads() if frame.get("type") == "notification"]
    assert warning["level"] == "warning"
    assert await provider.orch.llm_configured_for(owner) is True


@pytest.mark.parametrize("path", ["worker", "legacy"])
@pytest.mark.parametrize("field", ["provider", "model", "api_key", "base_url"])
@pytest.mark.parametrize("value", [True, 7, {}, []], ids=["boolean", "integer", "object", "array"])
async def test_actual_save_rejects_nontext_before_saved_key_resolution(
    provider, fixture, runtime, monkeypatch, path, field, value,
):
    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="custom", api_key=KEY,
        base_url="https://saved.example/v1", model="saved-model")
    before = await encrypted(runtime, owner)
    resolve = AsyncMock(wraps=llm_surface._resolve_api_key)
    probe = AsyncMock(return_value=(False, "transport_error", "private response"))
    unlock = AsyncMock(return_value=False)
    monkeypatch.setattr(llm_surface, "_resolve_api_key", resolve)
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    monkeypatch.setattr(llm_gate, "unlock_after_save", unlock)
    fields = dict(FIELDS, provider="custom", api_key="", base_url="https://saved.example/v1")
    fields[field] = value
    if path == "worker":
        result = await terminal(provider, request(provider, fields))
        assert result["state"] == "failed" and result["error"]["code"] == "validation_failed"
    else:
        await provider.orch.handle_ui_message(provider.socket, json.dumps({
            "type": "ui_event", "action": "chrome_llm_save", "payload": {
                "surface": "llm_settings", "fields": fields}}))
    resolve.assert_not_awaited()
    probe.assert_not_awaited()
    unlock.assert_not_awaited()
    assert await encrypted(runtime, owner) == before
    assert not provider.audit_events
    frames = provider.socket.payloads()
    assert not any(frame.get("type") in {"llm_config_ack", "notification"} for frame in frames)
    assert not any(frame.get("type") == "chrome_close" for frame in frames)
    assert "must be text" in json.dumps(frames) and KEY not in json.dumps(frames)


@pytest.mark.parametrize("field,value", [("model", 7), ("api_key", True)])
async def test_actual_nontext_first_run_save_cannot_acknowledge_or_unlock(
    provider, fixture, monkeypatch, field, value,
):
    owner = fixture[1]
    await llm_gate.push_setup_dialog(provider.orch, provider.socket, owner)
    probe = AsyncMock()
    unlock = AsyncMock()
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    monkeypatch.setattr(llm_gate, "unlock_after_save", unlock)
    fields = dict(FIELDS)
    fields[field] = value
    result = await terminal(provider, request(provider, fields))
    assert result["state"] == "failed" and result["error"]["code"] == "validation_failed"
    assert await provider.orch._llm_store.get(owner) is None
    assert llm_gate.is_gated(provider.orch, provider.socket)
    assert not any(frame.get("type") == "llm_config_ack" for frame in provider.socket.payloads())
    probe.assert_not_awaited()
    unlock.assert_not_awaited()


@pytest.mark.parametrize("path", ["worker", "legacy"])
@pytest.mark.parametrize("key_state", ["omitted", "blank"])
async def test_actual_save_preserves_legitimate_saved_key_reuse(provider, fixture, monkeypatch, path, key_state):
    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="custom", api_key=KEY,
        base_url="https://saved.example/v1", model="saved-model")
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", AsyncMock(
        return_value=(False, "transport_error", "private response")))
    fields = dict(FIELDS, provider="custom", api_key="", base_url="https://saved.example/v1")
    if key_state == "omitted":
        fields.pop("api_key")
    if path == "worker":
        assert (await terminal(provider, request(provider, fields)))["state"] == "completed"
    else:
        await provider.orch.handle_ui_message(provider.socket, json.dumps({
            "type": "ui_event", "action": "chrome_llm_save", "payload": {
                "surface": "llm_settings", "fields": fields}}))
    await provider.arrived(lambda frame: frame.get("type") == "notification")
    saved = await provider.orch._llm_store.get(owner)
    assert saved.api_key == KEY and saved.model == FIELDS["model"]
    assert any(frame.get("type") == "llm_config_ack" and frame.get("ok") is True
               for frame in provider.socket.payloads())


@pytest.mark.parametrize("path", ["worker", "legacy", "llm_config_set"])
@pytest.mark.parametrize("url", [
    "https://provider.example:not-a-port/v1",
    "https://[malformed-ipv6]/v1",
    "https://provider.example:65536/v1",
    "https:///v1",
    "https://inline-user:inline-password@provider.example/v1",
])
async def test_actual_save_rejects_malformed_or_credential_bearing_endpoint(
    provider, fixture, runtime, monkeypatch, path, url,
):
    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="custom", api_key=KEY,
        base_url="https://saved.example/v1", model="saved-model")
    await provider.orch._data_sharing_store.acknowledge(owner)
    before = await encrypted(runtime, owner)
    probe = AsyncMock()
    unlock = AsyncMock()
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    monkeypatch.setattr(llm_gate, "unlock_after_save", unlock)
    fields = dict(FIELDS, provider="custom", base_url=url)
    if path == "worker":
        result = await terminal(provider, request(provider, fields))
        assert result["state"] == "failed" and result["error"]["code"] == "validation_failed"
    elif path == "legacy":
        await provider.orch.handle_ui_message(provider.socket, json.dumps({
            "type": "ui_event", "action": "chrome_llm_save", "payload": {
                "surface": "llm_settings", "fields": fields}}))
    else:
        generation = str(uuid4())
        provider.socket.feed(json.dumps({"type": "llm_config_set", "config": fields,
            "submission_id": str(uuid4()), "request_generation": generation,
            "connection_generation": provider.connection_generation}))
        result = await terminal(provider, generation)
        assert result["state"] == "failed" and result["error"]["code"] == "validation_failed"
    assert await encrypted(runtime, owner) == before
    probe.assert_not_awaited()
    unlock.assert_not_awaited()
    assert not provider.audit_events
    frames = provider.socket.payloads()
    assert not any(frame.get("type") in {"llm_config_ack", "notification"} for frame in frames)
    assert not any(frame.get("type") == "chrome_close" for frame in frames)
    assert "inline-user" not in json.dumps(frames) and "inline-password" not in json.dumps(frames)


@pytest.mark.parametrize("destination", ["theme", "llm"])
async def test_actual_delayed_save_cannot_close_queued_new_settings_generation(
    provider, fixture, monkeypatch, destination,
):
    from orchestrator.projection_surfaces import theme as theme_surface

    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="openai", api_key=KEY,
        base_url="https://api.openai.com/v1", model="gpt-4o-mini")
    await provider.orch._data_sharing_store.acknowledge(owner)

    async def components(host, actor, roles, params):
        assert actor == owner and host is provider.orch
        return [{"type": "text", "content": "Current settings draft"}]

    monkeypatch.setattr(llm_surface, "components", components)
    monkeypatch.setattr(theme_surface, "components", components)
    opened = send(provider, payload={"surface": "llm", "params": {}})
    assert (await terminal(provider, opened))["state"] == "completed"
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.orch._llm_store.set_fenced

    async def save(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(provider.orch._llm_store, "set_fenced", save)
    saving = request(provider)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current = send(provider, payload={"surface": destination, "params": {}})
        await provider.barrier()
        release.set()
        assert (await terminal(provider, saving))["state"] == "completed"
        assert (await terminal(provider, current))["state"] == "completed"
    finally:
        release.set()
    frames = provider.socket.payloads()
    assert not any(frame.get("type") == "chrome_surface" and frame.get("surface_key") == ""
                   for frame in frames)
    assert any(frame.get("type") == "chrome_surface" and frame.get("surface_key") == destination
               and frame.get("request_generation") == current for frame in frames)
    assert chrome_events.open_surface_for(provider.orch, provider.socket) == destination
    assert (await provider.orch._llm_store.get(owner)).api_key == KEY


@pytest.mark.parametrize("device", ["ios", "macos", "android", "windows"])
@pytest.mark.parametrize("registration", ["fresh", "renewed"])
async def test_actual_preserved_form_save_reclaims_registration_owner_and_closes_before_probe(
    provider, fixture, monkeypatch, device, registration,
):
    from tests.test_work_surface_ingress_088 import registered

    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="openai", api_key=KEY,
        base_url="https://api.openai.com/v1", model="saved-model")
    await provider.orch._data_sharing_store.acknowledge(owner)
    provider.orch.rote.get_profile = lambda _: DeviceProfile.from_dict({
        "device_type": device, "supported_types": ["text", "alert", "button", "param_picker"],
    })
    if registration == "renewed":
        opened = send(provider, payload={"surface": "llm", "params": {}})
        assert (await terminal(provider, opened))["state"] == "completed"
        assert chrome_events.open_surface_for(provider.orch, provider.socket) == "llm"
        await registered(provider)
    assert chrome_events.open_surface_for(provider.orch, provider.socket) == ""
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(**fields):
        assert fields["api_key"] == KEY
        entered.set()
        await release.wait()
        return False, "auth_failed", "private provider rejection"

    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    generation = request(provider, dict(FIELDS, api_key=""))
    try:
        result = await terminal(provider, generation)
        assert result["state"] == "completed" and result["label"] == "Provider settings saved"
        await asyncio.wait_for(entered.wait(), 5)
        frames = provider.socket.payloads()
        closed = [frame for frame in frames if frame.get("type") == "chrome_surface"
                  and frame.get("surface_key") == "" and frame.get("request_generation") == generation]
        assert len(closed) == 1 and closed[0]["components"] == []
        assert chrome_events.open_surface_for(provider.orch, provider.socket) == ""
        ack = next(frame for frame in frames if frame.get("type") == "llm_config_ack")
        assert frames.index(result) < frames.index(closed[0]) < frames.index(ack)
        assert not any(frame.get("type") == "notification" for frame in frames)
        saved = await provider.orch._llm_store.get(owner)
        assert saved.api_key == KEY and saved.model == FIELDS["model"]
        release.set()
        warning = await provider.arrived(lambda frame: frame.get("type") == "notification")
        assert warning["level"] == "warning" and warning["title"] == "Provider settings saved"
        assert frames.index(closed[0]) < len(provider.socket.payloads()) - 1
    finally:
        release.set()


@pytest.mark.parametrize("phase", ["claim", "close"])
@pytest.mark.parametrize("change", [
    "register", "owner", "context", "generation", "registration", "closed",
    "pending_registration", "new_llm", "new_theme",
])
async def test_actual_saved_form_claim_and_close_refuse_changed_socket_authority_after_wait(
    provider, fixture, monkeypatch, phase, change,
):
    from tests.test_work_surface_ingress_088 import registered

    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="openai", api_key=KEY,
        base_url="https://api.openai.com/v1", model="saved-model")
    await provider.orch._data_sharing_store.acknowledge(owner)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = chrome_events._verify_human_delivery
    deliveries = 0

    async def held(host, socket):
        nonlocal deliveries
        deliveries += 1
        if deliveries == (1 if phase == "claim" else 2):
            entered.set()
            await release.wait()
        await original(host, socket)

    async def probe(**fields):
        assert fields["api_key"] == KEY
        finished.set()
        return True, None, None

    monkeypatch.setattr(chrome_events, "_verify_human_delivery", held)
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    generation = request(provider, dict(FIELDS, api_key=""))
    context = provider.orch._connection_contexts[id(provider.socket)]
    newer = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await provider.orch._llm_store.get(owner)).model == FIELDS["model"]
        if change in {"register", "owner"}:
            if change == "owner":
                provider.token = fixture[3](sub=str(uuid4()))
            await registered(provider)
            assert chrome_events.open_surface_for(provider.orch, provider.socket) == ""
            assert not chrome_events.surface_request_current(provider.orch, provider.socket, "llm", generation)
        elif change == "context":
            provider.orch._connection_contexts[id(provider.socket)] = object()
        elif change == "generation":
            context.connection_generation = uuid4()
        elif change == "registration":
            provider.orch.ui_sessions[provider.socket] = deepcopy(provider.orch.ui_sessions[provider.socket])
        elif change == "closed":
            provider.socket.closed = True
        elif change == "pending_registration":
            context.work_registrations_pending = 1
        else:
            surface_key = "llm" if change == "new_llm" else "theme"
            newer = send(provider, payload={"surface": surface_key, "params": {}})
            await provider.barrier()
            assert chrome_events.surface_request_current(provider.orch, provider.socket, surface_key, newer)
        release.set()
        await asyncio.wait_for(finished.wait(), 5)
        if newer is not None:
            assert (await terminal(provider, newer))["state"] == "completed"
            assert chrome_events.open_surface_for(provider.orch, provider.socket) == surface_key
        assert not any(frame.get("type") == "chrome_surface" and frame.get("surface_key") == ""
                       for frame in provider.socket.payloads())
        assert any(frame.get("type") == "llm_config_ack" and frame.get("ok") is True
                   for frame in provider.socket.payloads())
        assert (await provider.orch._llm_store.get(owner)).api_key == KEY
    finally:
        context.work_registrations_pending = 0
        provider.socket.closed = False
        release.set()


async def test_actual_native_request_tracking_is_cleared_when_connection_drains(provider, monkeypatch):
    from orchestrator.projection_surfaces import theme as theme_surface

    async def configured(owner):
        return True

    provider.orch.llm_configured_for = configured

    async def components(host, owner, roles, params):
        return [{"type": "text", "content": "Current settings"}]

    monkeypatch.setattr(theme_surface, "components", components)
    generation = send(provider, payload={"surface": "theme", "params": {}})
    assert (await terminal(provider, generation))["state"] == "completed"
    assert chrome_events.surface_request_current(provider.orch, provider.socket, "theme", generation)
    context = provider.orch._connection_contexts[id(provider.socket)]
    await provider.orch._drain_connection_context(context)
    assert provider.orch._ordinary_chrome_requests == {}
    assert chrome_events.open_surface_for(provider.orch, provider.socket) == ""


@pytest.mark.parametrize("action", ["chrome_llm_save", "chrome_typesafe_save"])
@pytest.mark.parametrize("requested_surface", ["theme", "guidance"])
@pytest.mark.parametrize("location", ["payload", "frame"])
async def test_actual_credential_save_rejects_foreign_surface_before_handler_effects(
    provider, fixture, runtime, monkeypatch, action, requested_surface, location,
):
    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="openai", api_key=KEY,
        base_url="https://api.openai.com/v1", model="saved-model")
    provider.orch._typesafe_store = TypeSafeCredentialStore(plane_runtime=runtime)
    await provider.orch._typesafe_store.save(owner, "typesafe-saved-fixture-key")
    before = await encrypted(runtime, owner)
    before_typesafe = await provider.orch._typesafe_store.get_key(owner)
    acknowledgment = AsyncMock(wraps=llm_surface._require_acknowledgment)
    resolve = AsyncMock(wraps=llm_surface._resolve_api_key)
    persist = AsyncMock(wraps=provider.orch._llm_store.set_fenced)
    typesafe_persist = AsyncMock(wraps=provider.orch._typesafe_store.save)
    probe = AsyncMock(return_value=(False, "transport_error", "private fixture response"))
    typesafe_probe = AsyncMock()
    unlock = AsyncMock(return_value=False)
    monkeypatch.setattr(llm_surface, "_require_acknowledgment", acknowledgment)
    monkeypatch.setattr(llm_surface, "_resolve_api_key", resolve)
    monkeypatch.setattr(provider.orch._llm_store, "set_fenced", persist)
    monkeypatch.setattr(provider.orch._typesafe_store, "save", typesafe_persist)
    monkeypatch.setattr("llm_config.ws_handlers.probe_chat_completion", probe)
    monkeypatch.setattr("llm_config.typesafe_handlers.probe_key", typesafe_probe)
    monkeypatch.setattr(llm_gate, "unlock_after_save", unlock)
    generation = str(uuid4())
    submission = str(uuid4())
    payload = {"fields": dict(FIELDS, typesafe_api_key="typesafe-new-fixture-key")}
    frame = {"type": "ui_event", "action": action, "payload": payload,
        "submission_id": submission, "request_generation": generation,
        "connection_generation": provider.connection_generation}
    (payload if location == "payload" else frame)["surface"] = requested_surface
    provider.socket.feed(json.dumps(frame))
    result = await terminal(provider, generation)
    assert result["state"] == "failed" and result["error"]["code"] == "validation_failed"
    assert result["request_generation"] == generation
    admitted = next(frame for frame in provider.frames if str(frame.request_generation) == generation)
    assert str(admitted.submission_id) == submission
    assert result["surface"] == requested_surface
    acknowledgment.assert_not_awaited()
    resolve.assert_not_awaited()
    persist.assert_not_awaited()
    typesafe_persist.assert_not_awaited()
    probe.assert_not_awaited()
    typesafe_probe.assert_not_awaited()
    unlock.assert_not_awaited()
    assert await encrypted(runtime, owner) == before
    assert await provider.orch._typesafe_store.get_key(owner) == before_typesafe
    assert not provider.audit_events
    frames = provider.socket.payloads()
    assert not any(frame.get("type") in {"llm_config_ack", "notification"} for frame in frames)
    assert not any(frame.get("type") == "chrome_surface" for frame in frames)
    assert KEY not in json.dumps(frames) and "typesafe-new-fixture-key" not in json.dumps(frames)


@pytest.mark.parametrize("action,requested_surface,location", [
    ("chrome_llm_save", None, "payload"),
    ("chrome_llm_save", "llm", "payload"),
    ("chrome_llm_save", "llm_settings", "payload"),
    ("chrome_typesafe_save", None, "payload"),
    ("chrome_typesafe_save", "llm", "payload"),
    ("chrome_typesafe_save", "llm_settings", "frame"),
])
async def test_actual_credential_save_keeps_existing_owner_aliases_and_omitted_surface(
    provider, fixture, runtime, monkeypatch, action, requested_surface, location,
):
    owner = fixture[1]
    await provider.orch._llm_store.set(owner, provider="openai", api_key=KEY,
        base_url="https://api.openai.com/v1", model="saved-model")
    provider.orch._typesafe_store = TypeSafeCredentialStore(plane_runtime=runtime)
    monkeypatch.setattr("llm_config.typesafe_handlers.probe_key", AsyncMock())
    generation = str(uuid4())
    payload = {"fields": dict(FIELDS, typesafe_api_key="typesafe-new-fixture-key")}
    frame = {"type": "ui_event", "action": action, "payload": payload,
        "submission_id": str(uuid4()), "request_generation": generation,
        "connection_generation": provider.connection_generation}
    if requested_surface is not None:
        (payload if location == "payload" else frame)["surface"] = requested_surface
    provider.socket.feed(json.dumps(frame))
    assert (await terminal(provider, generation))["state"] == "completed"
    if action == "chrome_llm_save":
        await provider.arrived(lambda frame: frame.get("type") == "llm_config_ack")
        assert (await provider.orch._llm_store.get(owner)).model == FIELDS["model"]
    else:
        assert (await provider.orch._typesafe_store.get_key(owner)).api_key == "typesafe-new-fixture-key"
