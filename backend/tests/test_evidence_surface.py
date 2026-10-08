"""Checks fresh evidence inspection through signed metadata ingress and current host authority.
Navigation and revocation failures cannot publish stale source text or create durable source copies.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.evidence.evidence_agent import EvidenceAgent
from agents.evidence.mcp_server import TOOL_REGISTRY
from orchestrator import chrome_events, evidence_context, human_request_authority as human_authority
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.context_authority import current_context_authority
from orchestrator.history import HistoryManager
from orchestrator.orchestrator import GateRefusal, PreparedDispatch
from orchestrator.projection_surfaces import evidence
from orchestrator.work_admission import OperationOwner, OwnerScope
from personalization import phi_gate
from personalization.phi_gate import PHIGate
from persistent_agents.models import AssignmentError
from rote.capabilities import DeviceProfile, DeviceType
from shared.feature_flags import flags
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_human_socket_authority_088 import socket_request as socket_request
from tests.test_human_request_authority_088 import (
    bound as bound, fixture as fixture, human as human, runtime as runtime,
    service as service, signing_key as signing_key,
)
from tests.test_work_surface_ingress_088 import ingress as ingress, registered
from tests.test_work_surface_postgres_088 import surface as surface, command as command, context as context

pytestmark = pytest.mark.asyncio
TEXT = '<script>untrusted source</script> Full permitted source. ' * 110
SUMMARY = '<img src=x onerror=alert(1)> Generated summary stays untrusted.'


async def test_evidence_open_captures_a_fresh_read_caller_without_write_authority(human, socket_request):
    socket, context, message = socket_request
    message["action"] = "chrome_open"
    message["payload"] = {"surface": "evidence", "params": {"kind": "usage"}}
    pending = human_authority.capture_human_socket_request(human[1], websocket=socket,
        context=context, message=message)
    assert pending is not None
    try:
        caller = await pending.authenticate()
        assert type(caller) is human_authority.CurrentHumanCaller
        with pytest.raises(AssignmentError, match="human_write_required"):
            caller.require_write()
    finally:
        pending.close()


@pytest.fixture
async def inspection(ingress, fixture, runtime, service, tmp_path, monkeypatch):
    state, orch = ingress, ingress.orch
    orch.rote.get_profile = lambda _: replace(DeviceProfile.default(), device_type=DeviceType.IOS)
    for name in ("hook_system", "taint_tracking", "runtime_supervisor", "hitl_highrisk", "policy_engine",
                 "model_router", "user_skills", "slash_commands"):
        monkeypatch.setenv("FF_" + name.upper(), "false")
        monkeypatch.setitem(flags._flags, name, False)
    for name in ("observation_packing", "inprocess_agents"):
        monkeypatch.setenv("FF_" + name.upper(), "true")
        monkeypatch.setitem(flags._flags, name, True)
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "evidence-key.pem"))
    monkeypatch.setenv("POLICY_RULES", "[]")
    gate = PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: []))
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
    orch.audit_repo = service.audit
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(runtime=runtime, repositories=runtime.repositories))
    boundary = human_authority.HumanRequestBoundary(orch)
    orch.human_request_boundary = boundary
    orch.history = HistoryManager(str(tmp_path / "history"), plane_runtime=runtime)
    state.chat = str(uuid4())
    state.owner = fixture[1]
    await asyncio.to_thread(orch.history.create_chat, state.chat, state.owner)
    orch._ws_active_chat[id(state.socket)] = state.chat
    orch.cancelled_sessions = {}
    orch._hitl_pending_calls = {}
    orch._dispatch_context = {}
    orch._pending_cap_entries = {}
    orch._job_context = {}
    orch._policy_roles = lambda _: ["user"]
    orch.agent_cards["web-research-1"] = AgentCard("Source reader", "Reads one source", "web-research-1",
        skills=[AgentSkill("Read", "Reads one source", "fetch_page", scope="tools:read")])
    adapter = EvidenceAgent(orch, port=0)
    orch.local_agents["evidence-1"] = adapter
    orch.agent_cards["evidence-1"] = adapter.card
    orch.tool_permissions.register_tool_scopes("evidence-1", {name: tool["scope"] for name, tool in TOOL_REGISTRY.items()})
    orch.tool_permissions.set_agent_scopes(state.owner, "evidence-1", {"tools:read": True})
    state.evidence = evidence_context.EvidenceContext(orch)
    orch._evidence_context = state.evidence
    state.policy = tmp_path / "retention.json"
    state.policy.write_text(json.dumps([{"owner_id": state.owner, "conversation_id": state.chat,
        "audience_id": f"user:{state.owner}", "source_agent": "web-research-1", "source_tool": "fetch_page",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}]))
    monkeypatch.setenv("ASTRAL_OBSERVATION_POLICY", str(state.policy))
    grant = state.evidence._grant(state.owner, state.chat, "web-research-1", "fetch_page")
    observation = state.evidence.archive.capture(TEXT, grant=grant, operation_id=str(uuid4()),
        source_args={"query": "synthetic"}, outcome="success")
    state.reference = observation.reference
    state.evidence._sources[state.reference] = observation
    state.view = state.evidence.views.capture(SUMMARY, owner_id=state.owner, conversation_id=state.chat,
        audience_id=f"user:{state.owner}", dependencies=(state.evidence._dependency(observation),))
    state.calls, state.notices, state.last_response = [], [], None

    async def prepare(ws, agent, tool, arguments, chat, owner, **kwargs):
        assert ws is state.socket and chat == state.chat and owner == state.owner
        if not orch.tool_permissions.is_tool_allowed(owner, agent, tool):
            return GateRefusal(response=MCPResponse(error={"message": "Permission denied"}))
        return PreparedDispatch({**arguments, "user_id": owner, "session_id": chat}, {}, None, None)

    async def physical(ws, agent, tool, arguments, **kwargs):
        assert agent == "evidence-1"
        lease = current_context_authority(orchestrator=orch, websocket=ws, chat_id=state.chat)
        proof = await lease.verify(orchestrator=orch, websocket=ws, chat_id=state.chat)
        operation = (_CONNECTION_OPERATION_CONTEXT.get() or {})["operation"]
        assert len(proof.operation_records) == 1 and proof.operation_records[0].operation_id == operation.operation_id
        state.calls.append((tool, deepcopy(arguments), lease, proof))
        request_id = str(uuid4())
        with evidence_context.dispatch_requester(orch, None, None, state.owner, state.chat, ws):
            orch._dispatch_context[request_id] = {"agent_id": agent, **evidence_context.requester_binding(orch)}
        try:
            public = {key: value for key, value in arguments.items()
                      if not key.startswith("_") and key not in {"user_id", "session_id"}}
            result = await asyncio.create_task(state.evidence.tool(request_id, tool, public))
            state.last_response = result
            return result
        finally:
            orch._dispatch_context.pop(request_id, None)

    async def machine_guard(*_args):
        return None

    async def notice(*args, **kwargs):
        state.notices.append((args, kwargs))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Evidence read invoked a provider or conversation mutation")

    orch._authorize_and_prepare = prepare
    orch._execute_with_retry = physical
    orch._guard_machine_meta_tool = machine_guard
    orch._protected_dispatch_channel = lambda *_args, **_kwargs: "websocket"
    orch.send_ui_render = notice
    orch.llm_configured_for = forbidden
    orch._call_llm = forbidden
    orch._append_conversation_message = forbidden
    try:
        yield state
    finally:
        await state.evidence.close()
        boundary.close()
        assert not getattr(orch, "_evidence_navigation", {})


def request(state, params=None, *, action="chrome_open", **updates):
    generation = str(uuid4())
    value = {"type": "ui_event", "action": action, "submission_id": str(uuid4()),
        "request_generation": generation, "connection_generation": state.connection_generation,
        "payload": {"surface": "evidence", "params": params if params is not None else {"kind": "usage"}}}
    value.update(updates)
    state.socket.feed(json.dumps(value))
    return generation


async def terminal(state, generation):
    return await state.arrived(lambda value: value.get("type") == "operation_status"
        and value.get("request_generation") == generation and value.get("terminal") is True)


def surfaces(state):
    return [value for value in state.socket.payloads() if value.get("type") in {"chrome_render", "chrome_surface"}
        and value.get("surface_key") == "evidence"]


@pytest.mark.parametrize("device", [DeviceType.BROWSER, DeviceType.WINDOWS, DeviceType.ANDROID,
    DeviceType.IOS, DeviceType.MACOS])
@pytest.mark.parametrize("kind", ["source", "preview", "summary", "usage"])
async def test_real_ingress_uses_only_the_new_read_fence_and_requested_transient_modal(inspection, monkeypatch, kind, device):
    state = inspection
    state.orch.rote.get_profile = lambda _: replace(DeviceProfile.default(), device_type=device)
    await registered(state)
    params = ({"view_id": state.view.reference} if kind == "summary" else {"kind": "usage"} if kind == "usage"
        else {"kind": kind, "reference": state.reference, "offset": 0})
    generation = request(state, params)
    result = await terminal(state, generation)
    assert result["state"] == "completed"
    frame = surfaces(state)[-1]
    assert frame["request_generation"] == generation and frame["region"] == "modal"
    expected = "chrome_render" if device is DeviceType.BROWSER else "chrome_surface"
    assert frame["type"] == expected
    rendered = json.dumps(frame)
    if kind in {"source", "preview"}:
        assert ("Captured source" if kind == "source" else "Partial preview") in rendered
        if device is DeviceType.BROWSER:
            assert "<script>" not in frame["html"] and "&lt;script&gt;" in frame["html"]
    elif kind == "summary":
        assert "Generated summary" in rendered
        if device is DeviceType.BROWSER:
            assert "<img src=x" not in frame["html"] and "&lt;img" in frame["html"]
    else:
        assert "Whole conversation usage" in rendered
    admitted = next(frame for frame in state.frames if str(frame.request_generation) == generation)
    assert admitted.read_only is True and admitted.chat_id == state.chat
    assert len(state.calls) == 1
    with pytest.raises(AssignmentError):
        await state.calls[0][2].verify(orchestrator=state.orch, websocket=state.socket, chat_id=state.chat)
    assert not state.orch._chat_recorders and not state.orch._evidence_navigation
    assert (await asyncio.to_thread(state.orch.history.get_chat, state.chat, state.owner))["messages"] == []
    receipts, _ = await asyncio.to_thread(state.orch.audit_repo.list_for_user, state.owner, limit=200)
    persisted = "".join(receipt.model_dump_json() for receipt in receipts)
    assert TEXT not in persisted and SUMMARY not in persisted


@pytest.mark.parametrize("kind", ["source", "preview", "summary", "usage"])
async def test_watch_ingress_sends_only_verified_metadata_and_phone_desktop_handoff(inspection, kind):
    state = inspection
    state.orch.rote.get_profile = lambda _: replace(DeviceProfile.default(), device_type=DeviceType.WATCH)
    await registered(state)
    params = ({"view_id": state.view.reference} if kind == "summary" else {"kind": "usage"} if kind == "usage"
        else {"kind": kind, "reference": state.reference, "offset": 0})
    generation = request(state, params)
    assert (await terminal(state, generation))["state"] == "completed"
    frame = surfaces(state)[-1]
    assert frame["type"] == "chrome_surface" and frame["request_generation"] == generation
    rendered = json.dumps(frame)
    assert "phone or desktop" in rendered
    assert "untrusted source" not in rendered and "Generated summary stays untrusted" not in rendered
    assert "Permitted text" not in rendered and "chrome_open" not in rendered
    assert not any(component["type"] == "button" for component in frame["components"])
    if kind in {"source", "preview"}:
        assert state.reference in rendered and "Source SHA-256" in rendered
    elif kind == "summary":
        assert state.view.reference in rendered
    else:
        assert "Whole conversation usage" in rendered
    assert len(state.calls) == 1 and not state.orch._evidence_navigation
    admitted = next(frame for frame in state.frames if str(frame.request_generation) == generation)
    assert admitted.read_only is True and admitted.chat_id == state.chat
    with pytest.raises(AssignmentError):
        await state.calls[0][2].verify(orchestrator=state.orch, websocket=state.socket, chat_id=state.chat)
    assert (await asyncio.to_thread(state.orch.history.get_chat, state.chat, state.owner))["messages"] == []


@pytest.mark.parametrize("kind", ["source", "summary"])
async def test_watch_unknown_reference_keeps_the_uniform_denial_and_handoff(inspection, kind):
    state = inspection
    state.orch.rote.get_profile = lambda _: replace(DeviceProfile.default(), device_type=DeviceType.WATCH)
    await registered(state)
    identity = ("obs_" if kind == "source" else "view_") + "z" * 43
    params = {"kind": "source", "reference": identity} if kind == "source" else {"view_id": identity}
    generation = request(state, params)
    assert (await terminal(state, generation))["state"] == "completed"
    rendered = json.dumps(surfaces(state)[-1])
    assert "Recall blocked" in rendered and "phone or desktop" in rendered
    assert identity not in rendered and "chrome_open" not in rendered
    assert "untrusted source" not in rendered and "Generated summary stays untrusted" not in rendered


@pytest.mark.parametrize("kind", ["source", "preview", "summary", "usage"])
@pytest.mark.parametrize("loss", ["operation", "registration"])
async def test_watch_handoff_retains_final_cancellation_and_human_authority(inspection, monkeypatch, kind, loss):
    state = inspection
    state.orch.rote.get_profile = lambda _: replace(DeviceProfile.default(), device_type=DeviceType.WATCH)
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.evidence.verify_delivery
    checks = []

    async def held(response, **kwargs):
        checked = await original(response, **kwargs)
        checks.append(True)
        if len(checks) == 2:
            entered.set()
            await release.wait()
        return checked

    monkeypatch.setattr(state.evidence, "verify_delivery", held)
    params = ({"view_id": state.view.reference} if kind == "summary" else {"kind": "usage"} if kind == "usage"
        else {"kind": kind, "reference": state.reference, "offset": 0})
    generation = request(state, params)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if loss == "operation":
            record = state.calls[0][3].operation_records[0]
            await asyncio.to_thread(state.orch.work_admission.cancel,
                owner=OperationOwner(OwnerScope.CONNECTION, None, record.connection_scope_id),
                operation_id=record.operation_id, terminal_code="cancelled")
        else:
            state.orch.ui_sessions[state.socket] = deepcopy(state.orch.ui_sessions[state.socket])
        release.set()
        assert (await terminal(state, generation))["state"] in {"failed", "cancelled"}
    finally:
        release.set()
    assert not surfaces(state)


@pytest.mark.parametrize("params", [
    None, [], "source", {"kind": []}, {"kind": "delete"}, {"kind": "source"},
    {"kind": "source", "reference": "obs_short"}, {"kind": "source", "reference": "obs_" + "a" * 43, "offset": True},
    {"kind": "source", "reference": "obs_" + "a" * 43, "offset": -1},
    {"kind": "source", "reference": "obs_" + "a" * 43, "offset": 8 * 1024 * 1024 + 1},
    {"kind": "source", "reference": "obs_" + "a" * 43, "owner_id": "foreign"},
    {"view_id": "view_short"}, {"view_id": "view_" + "a" * 43, "write": True}, {"kind": "usage", "chat_id": str(uuid4())},
])
async def test_public_parameters_cannot_choose_authority_or_write_modes(params):
    with pytest.raises(AssignmentError, match="evidence_surface_unavailable"):
        evidence.validate_payload({"surface": "evidence", "params": params})


@pytest.mark.parametrize("change", ["chat", "missing_chat", "uppercase_chat", "params_owner", "params_delete", "payload_owner"])
async def test_invalid_ingress_never_admits_or_dispatches_evidence(inspection, change):
    state = inspection
    await registered(state)
    params = {"kind": "source", "reference": state.reference}
    updates = {}
    if change == "chat":
        updates["session_id"] = str(uuid4())
    elif change == "missing_chat":
        state.orch._ws_active_chat.clear()
    elif change == "uppercase_chat":
        state.orch._ws_active_chat[id(state.socket)] = state.chat.upper()
    elif change == "params_owner":
        params["owner_id"] = state.owner
    elif change == "params_delete":
        params = {"kind": "delete", "reference": state.reference}
    else:
        updates["payload"] = {"surface": "evidence", "params": params, "owner_id": state.owner}
    generation = request(state, params, **updates)
    await state.barrier()
    assert not state.calls and not surfaces(state)
    assert not any(str(frame.request_generation) == generation for frame in state.frames)


async def test_unregistered_request_cannot_adopt_a_later_login(inspection):
    state = inspection
    generation = request(state)
    await state.barrier()
    await registered(state)
    await state.barrier()
    assert not state.calls and not surfaces(state)
    assert not any(str(frame.request_generation) == generation for frame in state.frames)


@pytest.mark.parametrize("device", [DeviceType.BROWSER, DeviceType.IOS])
@pytest.mark.parametrize("action", ["chrome_close", "load_chat", "new_chat", "chrome_open", "register_ui"])
async def test_navigation_invalidates_an_awaiting_read_on_web_and_native(inspection, monkeypatch, device, action):
    state = inspection
    state.orch.rote.get_profile = lambda _: replace(DeviceProfile.default(), device_type=device)
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.evidence.verify_delivery
    calls = []

    async def held(response, **kwargs):
        checked = await original(response, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            entered.set()
            await release.wait()
        return checked

    monkeypatch.setattr(state.evidence, "verify_delivery", held)
    generation = request(state, {"kind": "source", "reference": state.reference})
    try:
        await asyncio.wait_for(entered.wait(), 5)
        actual = state.orch.handle_ui_message

        async def skip_navigation(ws, raw):
            frame = json.loads(raw)
            if frame.get("type") == "ui_event" and frame.get("action") == action:
                return
            await actual(ws, raw)

        if action == "register_ui":
            state.register()
            await asyncio.wait_for(state.registrations.get(), 5)
        else:
            monkeypatch.setattr(state.orch, "handle_ui_message", skip_navigation)
            params = {"surface": "llm", "params": {}} if action == "chrome_open" else {"surface": "evidence", "params": {}}
            request(state, action=action, payload=params)
        await state.barrier()
        release.set()
        assert (await terminal(state, generation))["state"] == "failed"
    finally:
        release.set()
    assert not surfaces(state)


@pytest.mark.parametrize("loss", ["active_chat", "registration", "token", "issuance", "consent", "operation", "provider_flags"])
async def test_fresh_metadata_scope_refuses_lifetime_changes_after_admission(inspection, fixture, runtime, monkeypatch, loss):
    state = inspection
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch._call_work_admission

    async def held(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_submit_connection_batch":
            entered.set()
            await release.wait()
        return await original(callback, *args, **kwargs)

    monkeypatch.setattr(state.orch, "_call_work_admission", held)
    generation = request(state, {"kind": "source", "reference": state.reference})
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if loss == "active_chat":
            state.orch._ws_active_chat[id(state.socket)] = str(uuid4())
        elif loss == "registration":
            state.orch.ui_sessions[state.socket] = deepcopy(state.orch.ui_sessions[state.socket])
        elif loss == "token":
            state.orch.ui_sessions[state.socket]["_raw_token"] = fixture[3](sub="different")
        elif loss == "issuance":
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        elif loss == "consent":
            await asyncio.to_thread(fixture[0].delete, fixture[2])
        elif loss == "operation":
            context = state.orch._connection_contexts[id(state.socket)]
            context.connection_generation = uuid4()
        else:
            monkeypatch.setenv("USE_MOCK_AUTH", "true")
        release.set()
        assert (await terminal(state, generation))["state"] == "failed"
    finally:
        release.set()
    assert not state.calls and not surfaces(state)


@pytest.mark.parametrize("loss", ["permission", "grant", "delete", "privacy", "operation", "response", "service"])
async def test_changes_after_latest_delivery_check_cannot_publish_source(inspection, monkeypatch, loss):
    state = inspection
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.evidence.verify_delivery
    checks = []

    async def held(response, **kwargs):
        checked = await original(response, **kwargs)
        checks.append(True)
        if len(checks) == 2:
            state.checked = checked
            entered.set()
            await release.wait()
        return checked

    monkeypatch.setattr(state.evidence, "verify_delivery", held)
    generation = request(state, {"kind": "source", "reference": state.reference})
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if loss == "permission":
            await asyncio.to_thread(state.orch.tool_permissions.set_tool_overrides,
                state.owner, "web-research-1", {"fetch_page": False})
        elif loss == "grant":
            state.policy.write_text("[]")
        elif loss == "delete":
            state.evidence.archive.delete(state.reference, owner_id=state.owner,
                conversation_id=state.chat, audience_id=f"user:{state.owner}")
        elif loss == "privacy":
            def unavailable(**_kwargs):
                raise RuntimeError("Privacy analyzer unavailable")
            gate = PHIGate(analyzer=SimpleNamespace(analyze=unavailable))
            monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
        elif loss == "operation":
            record = state.calls[0][3].operation_records[0]
            await asyncio.to_thread(state.orch.work_admission.cancel,
                owner=OperationOwner(OwnerScope.CONNECTION, None, record.connection_scope_id),
                operation_id=record.operation_id, terminal_code="cancelled")
        elif loss == "response":
            state.checked.result["text"] = "Forged replacement source"
        else:
            state.orch._evidence_context = evidence_context.EvidenceContext(state.orch)
        release.set()
        result = await terminal(state, generation)
        assert result["state"] in {"failed", "cancelled"}
    finally:
        release.set()
    assert not surfaces(state)


async def test_retained_reference_is_readable_with_both_new_flags_off(inspection, monkeypatch):
    state = inspection
    await registered(state)
    for name in ("observation_packing", "safe_compaction"):
        monkeypatch.setenv("FF_" + name.upper(), "false")
        monkeypatch.setitem(flags._flags, name, False)
    before = state.evidence.archive.retained_bytes
    generation = request(state, {"kind": "source", "reference": state.reference})
    assert (await terminal(state, generation))["state"] == "completed"
    assert "Captured source" in json.dumps(surfaces(state)[-1])
    assert state.evidence.archive.retained_bytes == before


@pytest.mark.parametrize("kind", ["source", "summary"])
async def test_unknown_reference_is_uniformly_blocked_without_existence_metadata(inspection, kind):
    state = inspection
    await registered(state)
    identity = ("obs_" if kind == "source" else "view_") + "z" * 43
    params = {"kind": "source", "reference": identity} if kind == "source" else {"view_id": identity}
    generation = request(state, params)
    assert (await terminal(state, generation))["state"] == "completed"
    rendered = json.dumps(surfaces(state)[-1])
    assert "Recall blocked" in rendered and identity not in rendered and TEXT not in rendered and SUMMARY not in rendered


async def test_old_closed_lease_cannot_be_reused_for_the_next_real_inspection(inspection):
    state = inspection
    await registered(state)
    first = request(state)
    assert (await terminal(state, first))["state"] == "completed"
    old = state.calls[0][2]
    with pytest.raises(AssignmentError):
        await old.verify(orchestrator=state.orch, websocket=state.socket, chat_id=state.chat)
    second = request(state)
    assert (await terminal(state, second))["state"] == "completed"
    assert state.calls[0][3].operation_records[0].operation_id != state.calls[1][3].operation_records[0].operation_id
    assert len(surfaces(state)) == 2


async def test_direct_surface_without_captured_metadata_authority_refuses(inspection):
    state = inspection
    await registered(state)
    with pytest.raises(AssignmentError):
        await evidence.deliver(state.orch, state.socket, state.owner,
            {"surface": "evidence", "params": {"kind": "usage"}}, str(uuid4()))
    with pytest.raises(AssignmentError):
        await chrome_events._render_surface(state.orch, state.socket, state.owner, ["user"], "evidence", {})
    assert not state.calls and not surfaces(state)


async def test_disabled_controls_without_a_cached_service_refuse_the_read(inspection, monkeypatch):
    state = inspection
    await registered(state)
    for name in ("observation_packing", "safe_compaction"):
        monkeypatch.setenv("FF_" + name.upper(), "false")
        monkeypatch.setitem(flags._flags, name, False)
    state.orch._evidence_context = None
    generation = request(state, {"kind": "source", "reference": state.reference})
    assert (await terminal(state, generation))["state"] == "failed"
    assert not state.calls and not surfaces(state)


async def test_failed_modal_send_cannot_be_acknowledged_as_a_completed_read(inspection, monkeypatch):
    state = inspection
    await registered(state)
    send = state.orch._safe_send
    refused = []

    async def refuse(ws, data):
        if json.loads(data).get("surface_key") == "evidence":
            refused.append(True)
            return False
        return await send(ws, data)

    monkeypatch.setattr(state.orch, "_safe_send", refuse)
    generation = request(state, {"kind": "source", "reference": state.reference})
    assert (await terminal(state, generation))["state"] == "failed"
    assert refused == [True] and not surfaces(state)


async def test_connection_close_scrubs_pending_navigation_without_source_delivery(inspection, monkeypatch):
    state = inspection
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.evidence.verify_delivery

    async def held(response, **kwargs):
        checked = await original(response, **kwargs)
        entered.set()
        await release.wait()
        return checked

    monkeypatch.setattr(state.evidence, "verify_delivery", held)
    request(state, {"kind": "source", "reference": state.reference})
    try:
        await asyncio.wait_for(entered.wait(), 5)
        state.socket.closed = True
        state.socket.disconnect()
        release.set()
        async with asyncio.timeout(5):
            while state.orch._evidence_navigation:
                await asyncio.sleep(0)
    finally:
        release.set()
    assert not surfaces(state)


@pytest.mark.parametrize("payload", [None, [], {}, {"surface": "llm"}, {"surface": "evidence", "write": True}])
async def test_surface_payload_has_no_unscoped_or_write_arity(payload):
    with pytest.raises(AssignmentError):
        evidence.validate_payload(payload)


@pytest.mark.parametrize("value", [None, 1, "chat", str(uuid4()).upper()])
async def test_active_chat_must_be_a_canonical_uuid(value):
    with pytest.raises(AssignmentError):
        evidence._chat(value)


@pytest.mark.parametrize("pending", [None, SimpleNamespace(owner_id="owner"), "captured read"])
async def test_navigation_cannot_be_minted_from_public_or_duck_typed_authority(pending):
    with pytest.raises(AssignmentError):
        evidence.capture_navigation(SimpleNamespace(), pending=pending, chat_id=str(uuid4()))


@pytest.mark.parametrize("change", ["chat", "write", "surface"])
async def test_even_typed_caller_cannot_rebind_navigation_to_another_chat_or_action(human, socket_request, change):
    socket, context, message = socket_request
    chat = str(uuid4())
    human[2]._ws_active_chat = {id(socket): chat}
    message["action"] = "chrome_open"
    message["payload"] = {"surface": "evidence", "params": {"kind": "usage"}}
    if change == "write":
        message["action"] = "chrome_user_skill_save"
    elif change == "surface":
        message["payload"]["surface"] = "guidance"
    pending = human_authority.capture_human_socket_request(human[1], websocket=socket, context=context, message=message)
    try:
        with pytest.raises(AssignmentError):
            evidence.capture_navigation(human[2], pending=pending, chat_id=str(uuid4()) if change == "chat" else chat)
    finally:
        pending.close()


def recalled_response(**changes):
    data = {"reference": "obs_" + "a" * 43, "digest": "b" * 64, "start": 0, "end": 4, "total": 4,
        "text": "text", "untrusted": True, "next_offset": None, "at_end": True, "partial": False,
        "outcome": "success"}
    data.update(changes)
    return MCPResponse(result=data, ui_components=[{"type": "badge", "label": "Captured source"}])


@pytest.mark.parametrize("change", [
    {"reference": "obs_" + "z" * 43}, {"untrusted": False}, {"text": 1}, {"text": "X" * 16385},
    {"start": True}, {"start": 1}, {"end": -1}, {"end": 5}, {"total": -1}, {"digest": "short"},
    {"digest": None},
])
async def test_unverified_page_shape_cannot_reach_a_renderer(change):
    with pytest.raises(AssignmentError):
        evidence._components(recalled_response(**change), {"reference": "obs_" + "a" * 43, "offset": 0}, "source")


@pytest.mark.parametrize("response", ["untyped", MCPResponse(result=None),
    MCPResponse(result={}, ui_components=None), MCPResponse(result={}, ui_components=["raw HTML"])])
async def test_non_contract_responses_cannot_publish_source(response):
    with pytest.raises(AssignmentError):
        evidence._components(response, {}, "usage")


@pytest.mark.parametrize("kind,data,args", [
    ("usage", {"usage": {}, "model_calls": True}, {}),
    ("usage", {"usage": [], "model_calls": 1}, {}),
    ("summary", {"view": "generated_summary", "view_id": "view_" + "a" * 43,
        "text": "summary", "untrusted": True, "source_refs": ["obs_short"]}, {"view_id": "view_" + "a" * 43}),
    ("summary", {"view": "generated_summary", "view_id": "view_" + "a" * 43,
        "text": "", "untrusted": True, "source_refs": []}, {"view_id": "view_" + "a" * 43}),
])
async def test_usage_and_summary_metadata_cannot_claim_a_verified_wrong_shape(kind, data, args):
    with pytest.raises(AssignmentError):
        evidence._components(MCPResponse(result=data, ui_components=[]), args, kind)


async def test_partial_preview_clamps_utf8_without_splitting_a_character():
    text = "🐍" * 400
    response = recalled_response(text=text, end=len(text.encode()), total=len(text.encode()))
    components = evidence._components(response, {"reference": "obs_" + "a" * 43, "offset": 0}, "preview")
    values = [item["value"] for component in components if component["type"] == "keyvalue"
              for item in component["items"] if item["key"] == "Permitted text"]
    assert values == ["🐍" * 256]


async def test_watch_preview_retains_only_the_exact_utf8_preview_extent():
    text = "🐍" * 400
    response = recalled_response(text=text, end=len(text.encode()), total=len(text.encode()))
    components = evidence._watch_components(response,
        {"reference": "obs_" + "a" * 43, "offset": 0}, "preview")
    rendered = json.dumps(components)
    assert "0\\u20131024 of 1600" in rendered and "phone or desktop" in rendered
    assert "Permitted text" not in rendered and "\\ud83d\\udc0d" not in rendered
    assert not any(component["type"] == "button" for component in components)
    assert response.result["text"] == text and response.result["end"] == 1600


@pytest.mark.parametrize("kind", ["source", "summary"])
async def test_watch_authorized_unavailable_view_has_no_invented_reference_or_inspection_action(kind):
    response = MCPResponse(result={"status": "unavailable", "reason": "deleted"},
        ui_components=[{"type": "badge", "label": "Source unavailable"}])
    components = evidence._watch_components(response, {"view_id": "view_" + "a" * 43}, kind)
    rendered = json.dumps(components)
    assert "Source unavailable" in rendered and "phone or desktop" in rendered
    assert "view_" not in rendered and "chrome_open" not in rendered


@pytest.mark.parametrize("response", [None, MCPResponse(error={"message": "private source details"})])
async def test_watch_failed_read_still_hands_off_without_private_error_content(response):
    components = evidence._watch_components(response, {}, "source")
    rendered = json.dumps(components)
    assert "Recall blocked" in rendered and "phone or desktop" in rendered
    assert "private source details" not in rendered and "chrome_open" not in rendered


async def test_blocked_and_authorized_unavailable_states_do_not_echo_response_bodies():
    components = evidence._components(MCPResponse(error={"message": "private"}), {}, "source")
    assert "private" not in json.dumps(components) and components[0]["label"] == "Recall blocked"
    unavailable = MCPResponse(result={"status": "unavailable", "reason": "deleted"},
        ui_components=[{"type": "badge", "label": "Source unavailable"}])
    assert evidence._components(unavailable, {}, "source") == unavailable.ui_components
