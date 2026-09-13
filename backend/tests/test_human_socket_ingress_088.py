"""Actual finite ingress, normal registered JWT, and current-human Plane calls.

The controlled metadata handler performs an actual guarded repository read;
this verifies shared dispatch authority, not a fabricated authoring mutation.
"""
import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import chrome_events, human_request_authority as human
from orchestrator import orchestrator as module
from persistent_agents.models import AssignmentError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_work_surface_ingress_088 import (
    ingress as ingress, surface as surface, command as command, context as context,
    fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
    registered,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def metadata(ingress, runtime, service, monkeypatch):
    state = ingress
    orch = state.orch
    orch.audit_repo = service.audit
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=runtime, repositories=runtime.repositories))
    boundary = human.HumanRequestBoundary(orch)
    orch.human_request_boundary = boundary
    state.socket.scope["headers"].append((b"origin", b"https://app.invalid"))
    calls, audits = [], []

    async def handler(host, socket, owner, roles, payload):
        caller = human.current_human_caller(expected_orchestrator=host)
        assert type(caller) is human.CurrentHumanCaller
        assert owner == caller.owner_id
        if payload.get("attempt_write", True):
            caller.require_write()
        found = await caller.transaction(lambda tx, repos: repos.history.sessions.get_execution_state(
            tx, owner_id=owner, session_id=caller.require_session().credential.session_id),
            expected_orchestrator=host)
        calls.append((caller, found, roles))
        if payload.get("native"):
            await chrome_events._push_surface(host, socket, "agent_authoring", "Metadata", False,
                [{"type": "text", "text": "Current metadata"}])
        else:
            await chrome_events._push_modal(host, socket, "Current metadata")

    async def configured(*_):
        return True

    async def audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(orch, "llm_configured_for", configured)
    monkeypatch.setattr(chrome_events, "_handlers", lambda: {
        "chrome_user_skill_save": ("agent_authoring", handler),
        "chrome_user_skill_edit": ("agent_authoring", handler),
        # Even a mistakenly registered unknown namespace action gets no caller.
        "chrome_user_skill_unguarded": ("agent_authoring", handler),
    })
    monkeypatch.setattr("audit.hooks.record_ws_action", audit)
    try:
        yield state, calls, audits
    finally:
        boundary.close()


def send(state, action="chrome_user_skill_save", **payload):
    request = str(uuid4())
    frame = {"type": "ui_event", "action": action,
        "submission_id": str(uuid4()), "request_generation": request,
        "connection_generation": state.connection_generation,
        "payload": {"fields": {"name": "Private instructions"}, **payload}}
    state.socket.feed(json.dumps(frame))
    return request


async def terminal(state):
    return await state.arrived(lambda v: v.get("type") == "operation_status" and v.get("terminal") is True)


async def cleaned(frame):
    async with asyncio.timeout(5):
        while frame.human_request is not None:
            await asyncio.sleep(0.01)


async def test_actual_metadata_dispatch_uses_verified_caller_and_scrubs_outer_audit(metadata, fixture):
    state, calls, audits = metadata
    await registered(state)
    # Registration-derived role hints cannot grant roles absent from verified IAM.
    state.orch.ui_sessions[state.socket]["realm_access"] = {"roles": ["injected-role"]}
    generation = send(state, url="private-untrusted-url", agent_id="private-text")
    result = await terminal(state)
    assert result["state"] == "completed"
    assert len(calls) == 1 and calls[0][1].credential.session_id == fixture[2]
    assert "injected-role" not in calls[0][2]
    frame = next(f for f in state.frames if str(f.request_generation) == generation)
    await cleaned(frame)
    assert "Private instructions" not in repr(frame) and "private-untrusted-url" not in repr(frame)
    assert frame.human_request is None
    assert len(audits) == 1 and audits[0]["payload"] == {} and audits[0]["chat_id"] is None
    assert "_raw_token" not in audits[0]["claims"]
    assert human.current_human_caller() is None and module._CONNECTION_OPERATION_CONTEXT.get() is None
    with pytest.raises(AssignmentError):
        calls[0][0].require_write()


@pytest.mark.parametrize("action", ["chrome_user_skill_edit", "chrome_user_skill_unguarded"])
async def test_read_or_unknown_metadata_action_cannot_call_mutation(metadata, action):
    state, calls, _ = metadata
    await registered(state)
    send(state, action)
    assert (await terminal(state))["state"] == "failed"
    assert calls == []
    assert not any(v.get("type") == "chrome_render" for v in state.socket.payloads())


async def test_metadata_before_registration_cannot_adopt_later_identity(metadata):
    state, calls, _ = metadata
    generation = send(state)
    await state.barrier()
    await registered(state)
    await state.barrier()
    assert not calls and not any(str(f.request_generation) == generation for f in state.frames)


@pytest.mark.parametrize("change", ["registration", "token", "issuance", "message", "context", "policy"])
async def test_original_capture_survives_real_admission_wait_without_adoption(
    metadata, runtime, fixture, monkeypatch, change,
):
    state, calls, _ = metadata
    refreshes_before = len(fixture[-1])
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch._call_work_admission

    async def held(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_submit_connection_batch":
            entered.set()
            await release.wait()
        return await original(callback, *args, **kwargs)

    monkeypatch.setattr(state.orch, "_call_work_admission", held)
    generation = send(state)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        frame = next(f for f in state.frames if str(f.request_generation) == generation)
        pending = frame.human_request
        assert pending.observation.credential.session_id == fixture[2]
        if change == "registration":
            state.register()
            await asyncio.wait_for(state.registrations.get(), 5)
        elif change == "token":
            state.orch.ui_sessions[state.socket]["_raw_token"] = "private replacement token"
        elif change == "issuance":
            old = get_session_record(runtime, fixture[2])
            newer = await asyncio.to_thread(replace_session_record, runtime, old)
            assert newer.incarnation_id != pending.observation.credential.incarnation_id
        elif change == "message":
            frame.parsed["payload"]["fields"]["name"] = "changed after capture"
        elif change == "context":
            state.orch._connection_contexts[id(state.socket)].connection_generation = uuid4()
        else:
            monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "still-allows-primary-but-changed-policy")
        release.set()
        assert (await terminal(state))["state"] == "failed"
        await cleaned(frame)
        assert not calls and frame.human_request is None and pending.closed
        assert len(fixture[-1]) == refreshes_before
    finally:
        release.set()


@pytest.mark.parametrize("finish", ["timeout", "disconnect", "cancel"])
async def test_waiting_lane_retirement_closes_capture_without_cancelling_predecessor(metadata, monkeypatch, finish):
    state, calls, _ = metadata
    await registered(state)
    context = state.orch._connection_contexts[id(state.socket)]
    predecessor = asyncio.get_running_loop().create_future()
    context.mutation_tail = predecessor
    captured = asyncio.Event()
    captures = []
    original = human._HumanSocketRequest.capture_session

    async def capture(self):
        await original(self)
        self.deadline = time.monotonic() + 0.2 if finish == "timeout" else self.deadline
        captures.append(self)
        captured.set()

    monkeypatch.setattr(human._HumanSocketRequest, "capture_session", capture)
    send(state, "chrome_user_skill_edit", attempt_write=False)
    try:
        await asyncio.wait_for(captured.wait(), 5)
        # Admission/worker establishment is observed, not simulated success.
        async with asyncio.timeout(5):
            while not context.operations:
                await asyncio.sleep(0.01)
        if finish == "disconnect":
            state.socket.closed = True
            state.socket.disconnect()
        elif finish == "cancel":
            for work in tuple(context.operations.values()):
                work.task.cancel()
        if finish != "disconnect":
            result = await terminal(state)
            assert result["state"] in {"retryable", "cancelled"}
        async with asyncio.timeout(5):
            while not captures[0].closed:
                await asyncio.sleep(0.01)
        assert not predecessor.cancelled() and not calls
        assert human.current_human_caller() is None
    finally:
        if not predecessor.done():
            predecessor.set_result(None)


async def test_delivery_rechecks_original_caller_after_render_wait(metadata, monkeypatch):
    state, calls, _ = metadata
    await registered(state)
    original = chrome_events._verify_human_delivery

    async def replaced(orch, socket):
        state.orch.ui_sessions[state.socket] = deepcopy(state.orch.ui_sessions[state.socket])
        await original(orch, socket)

    monkeypatch.setattr(chrome_events, "_verify_human_delivery", replaced)
    send(state)
    assert (await terminal(state))["state"] == "failed"
    assert len(calls) == 1
    assert not any(v.get("type") == "chrome_render" for v in state.socket.payloads())


async def test_native_metadata_delivery_and_cached_authentication_remain_current(metadata):
    state, calls, _ = metadata
    await registered(state)
    send(state, native=True)
    assert (await terminal(state))["state"] == "completed"
    assert len(calls) == 1
    views = [v for v in state.socket.payloads() if v.get("type") == "chrome_surface"]
    assert len(views) == 1 and views[0]["surface_key"] == "agent_authoring"


async def test_missing_capture_cannot_invoke_registered_metadata_handler(metadata):
    state, calls, _ = metadata
    await registered(state)
    with pytest.raises(AssignmentError):
        await chrome_events.handle_chrome_event(state.orch, state.socket,
            "chrome_user_skill_save", {}, "caller-supplied-owner")
    assert not calls


async def test_capture_failure_at_enqueue_is_explicit_and_scrubbed(metadata, monkeypatch):
    state, calls, _ = metadata
    await registered(state)
    captured = []
    original = human.capture_human_socket_request
    def capture(*args, **kwargs):
        pending = original(*args, **kwargs)
        if pending is not None:
            captured.append(pending)
            async def fail():
                raise AssignmentError("human_authentication_required", 401)
            pending.capture_session = fail
        return pending
    monkeypatch.setattr(human, "capture_human_socket_request", capture)
    send(state)
    response = await state.arrived(lambda v: v.get("type") == "error" and v.get("accepted") is False)
    assert response["code"] == "operation_failed" and "operation_id" not in response
    assert captured[0].closed and not calls


async def test_cancellation_during_capture_retires_before_queue(metadata, monkeypatch):
    state, calls, _ = metadata
    await registered(state)
    entered = asyncio.Event()
    captures = []
    async def held(self):
        captures.append(self)
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(human._HumanSocketRequest, "capture_session", held)
    context = state.orch._connection_contexts[id(state.socket)]
    message = {"type": "ui_event", "action": "chrome_user_skill_save", "payload": {},
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "connection_generation": state.connection_generation}
    task = asyncio.create_task(state.orch._enqueue_connection_frame(context, json.dumps(message), message))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert captures[0].closed and not calls and not context.ingress
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("initial", ["registered", "preregistration", "retired_session"])
async def test_chat_skill_read_uses_original_capture_without_extending_to_chat_execution(
    metadata, runtime, fixture, monkeypatch, initial,
):
    state, _, _ = metadata
    orch = state.orch
    orch.cancelled_sessions = {}
    chat_id = str(uuid4())
    monkeypatch.setattr(orch.history, "get_chat", lambda *_args, **_kwargs: {"id": chat_id})
    reached, used = [], []
    held, release = asyncio.Event(), asyncio.Event()

    async def lookup(socket, *_args, **_kwargs):
        reached.append(True)
        caller = await human.current_socket_human_read(expected_orchestrator=orch, websocket=socket)
        try:
            with pytest.raises(AssignmentError, match="human_write_required"):
                caller.require_write()
            result = await caller.transaction(lambda tx, repos: repos.history.sessions.get_execution_state(
                tx, owner_id=caller.owner_id, session_id=fixture[2]), expected_orchestrator=orch)
            await caller.verify_delivery()
        finally:
            human.retire_socket_human_read(caller)
        used.append(result.credential.incarnation_id)
        with pytest.raises(AssignmentError):
            await caller.verify_delivery()
        # A later chat/model phase is outside the metadata lifetime, not cancelled
        # merely because the completed private lookup has been retired.
        await asyncio.sleep(0.25)

    monkeypatch.setattr(orch, "_serialized_chat", lookup, raising=False)
    if initial != "preregistration":
        await registered(state)
    original = orch._call_work_admission
    async def admission(callback, *args, **kwargs):
        if initial == "retired_session" and getattr(callback, "__name__", "") == "_submit_connection_batch":
            held.set()
            await release.wait()
        return await original(callback, *args, **kwargs)
    monkeypatch.setattr(orch, "_call_work_admission", admission)
    generation = send(state, "chat_message", message="/private-skill request", chat_id=chat_id)
    try:
        if initial == "preregistration":
            await state.barrier()
            await registered(state)
        elif initial == "retired_session":
            await asyncio.wait_for(held.wait(), 5)
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
            release.set()
        result = await terminal(state)
        assert reached == [True]
        assert result["state"] == ("completed" if initial == "registered" else "failed")
        assert len(used) == (1 if initial == "registered" else 0)
        frame = next(f for f in state.frames if str(f.request_generation) == generation)
        assert frame.read_only is False
        await cleaned(frame)
    finally:
        release.set()
