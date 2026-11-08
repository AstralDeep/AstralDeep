"""Tests for orchestrator/human_request_authority.py and chrome_events.py: metadata
dispatch through a real guarded repository read, capture lifecycle and slot claims
across admission waits, and refusal of unregistered or mismatched metadata actions.
"""

import asyncio
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import chrome_events, human_request_authority as human
from orchestrator import orchestrator as module
from orchestrator.work_admission import OperationState
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


async def admission_refusal(state, frame=None):
    value = await state.arrived(lambda item: item.get("type") == "error"
                                and item.get("code") == "harness_owner_invalid"
                                and isinstance(item.get("submission_id"), str)
                                and (frame is None or item.get("submission_id") == str(frame.submission_id)))
    assert value["accepted"] is False
    assert value["code"] == "harness_owner_invalid"
    return value


async def cleaned(frame):
    async with asyncio.timeout(5):
        while frame.human_request is not None:
            await asyncio.sleep(0.01)


async def test_actual_metadata_dispatch_uses_verified_caller_and_scrubs_outer_audit(metadata, fixture):
    state, calls, audits = metadata
    await registered(state)
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
        if change in {"registration", "context"}:
            await admission_refusal(state, frame)
        else:
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
    captured, waiting = asyncio.Event(), asyncio.Event()
    captures, running = [], []
    original = human._HumanSocketRequest.capture_session
    phase = state.orch._emit_long_running_operation_phase

    async def capture(self):
        await original(self)
        self.deadline = time.monotonic() + 0.2 if finish == "timeout" else self.deadline
        captures.append(self)
        captured.set()

    async def lane_wait(host_context, work):
        # Created just before the operation parks on its predecessor, so it runs once parked
        running.append(work.task)
        waiting.set()
        return await phase(host_context, work)

    monkeypatch.setattr(human._HumanSocketRequest, "capture_session", capture)
    monkeypatch.setattr(state.orch, "_emit_long_running_operation_phase", lane_wait)
    send(state, "chrome_user_skill_edit", attempt_write=False)
    try:
        await asyncio.wait_for(captured.wait(), 5)
        await waiting.wait()
        assert len(running) == 1 and not running[0].done()
        if finish == "disconnect":
            state.socket.closed = True
            state.socket.disconnect()
        elif finish == "cancel":
            running[0].cancel()
        if finish != "disconnect":
            result = await terminal(state)
            assert result["state"] in {"retryable", "cancelled"}
        await asyncio.wait(running)
        assert captures[0].closed
        assert not predecessor.cancelled() and not calls
        assert human.current_human_caller() is None
    finally:
        if not predecessor.done():
            predecessor.set_result(None)


async def test_operation_waiting_behind_a_turn_keeps_renewing_its_lease(metadata, monkeypatch):
    state, calls, _ = metadata
    await registered(state)
    context = state.orch._connection_contexts[id(state.socket)]
    predecessor = asyncio.get_running_loop().create_future()
    context.mutation_tail = predecessor
    monkeypatch.setattr(module, "CONNECTION_LEASE_RENEW_SECONDS", 0.01)
    renewed = asyncio.Event()
    original = state.orch._call_work_admission

    async def observed(callback, *args, **kwargs):
        value = await original(callback, *args, **kwargs)
        if getattr(callback, "__name__", "") == "renew_execution_lease":
            renewed.set()
        return value

    monkeypatch.setattr(state.orch, "_call_work_admission", observed)
    send(state, "chrome_user_skill_edit", attempt_write=False)
    try:
        await asyncio.wait_for(renewed.wait(), 5)
        assert not predecessor.done() and not calls
        predecessor.set_result(None)
        assert (await terminal(state))["state"] == "completed"
        assert len(calls) == 1
    finally:
        if not predecessor.done():
            predecessor.set_result(None)


def _waiting_operation(state, generation):
    context = state.orch._connection_contexts[id(state.socket)]
    return next(work for work in context.operations.values() if str(work.frame.request_generation) == generation)


def _parked_behind(state):
    predecessor = asyncio.get_running_loop().create_future()
    state.orch._connection_contexts[id(state.socket)].mutation_tail = predecessor
    return predecessor


async def test_cancel_during_the_early_claim_keeps_the_fence(metadata, monkeypatch):
    state, calls, _ = metadata
    await registered(state)
    predecessor = _parked_behind(state)
    entered, release, landed = threading.Event(), threading.Event(), threading.Event()
    original = state.orch._call_work_admission

    async def gated(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") != "claim_operation":
            return await original(callback, *args, **kwargs)

        def claim():
            entered.set()
            release.wait(5)
            try:
                return callback(*args, **kwargs)
            finally:
                landed.set()

        return await asyncio.to_thread(claim)

    monkeypatch.setattr(state.orch, "_call_work_admission", gated)
    generation = send(state, "chrome_user_skill_edit", attempt_write=False)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        work = _waiting_operation(state, generation)
        assert work.preselected
        work.task.cancel()
        release.set()
        # Holding the loop lets the thread rotate the slot lease before the task sees its cancellation.
        assert landed.wait(5)
        assert (await terminal(state))["state"] == "cancelled"
        await asyncio.wait_for(asyncio.wait({work.task}), 5)
        projection = await asyncio.to_thread(state.orch.work_admission.query_operation,
                                             owner=work.owner, operation_id=work.operation_id)
        assert projection.state is OperationState.CANCELLED
        assert not predecessor.cancelled() and not calls
    finally:
        release.set()
        if not predecessor.done():
            predecessor.set_result(None)


async def _chat_behind_a_turn(state, monkeypatch, fixture):
    orch = state.orch
    orch.cancelled_sessions = {}
    chat_id = str(uuid4())
    monkeypatch.setattr(orch.history, "get_chat", lambda *_args, **_kwargs: {"id": chat_id})
    used = []

    async def lookup(socket, *_args, **_kwargs):
        caller = await human.current_socket_human_read(expected_orchestrator=orch, websocket=socket)
        try:
            with pytest.raises(AssignmentError, match="human_write_required"):
                caller.require_write()
            used.append(await caller.transaction(
                lambda tx, repos: repos.history.sessions.get_execution_state(
                    tx, owner_id=caller.owner_id, session_id=fixture[2]), expected_orchestrator=orch))
        finally:
            human.retire_socket_human_read(caller)

    monkeypatch.setattr(orch, "_serialized_chat", lookup, raising=False)
    await registered(state)
    context = orch._connection_contexts[id(state.socket)]
    predecessor = asyncio.get_running_loop().create_future()
    context.mutation_tail = predecessor
    parked = asyncio.Event()
    phase = orch._emit_long_running_operation_phase

    async def lane_wait(host_context, work):
        parked.set()
        return await phase(host_context, work)

    monkeypatch.setattr(orch, "_emit_long_running_operation_phase", lane_wait)
    generation = send(state, "chat_message", message="queued request", chat_id=chat_id)
    await asyncio.wait_for(parked.wait(), 5)
    frame = next(f for f in state.frames if str(f.request_generation) == generation)
    pending = frame.human_request
    pending.deadline = time.monotonic() - 1
    pending.until = human.datetime.now(human.timezone.utc) - human.timedelta(seconds=1)
    return frame, pending, predecessor, used


async def test_chat_message_queued_past_its_window_runs_on_a_full_recheck(metadata, fixture, monkeypatch):
    state, _, _ = metadata
    frame, pending, predecessor, used = await _chat_behind_a_turn(state, monkeypatch, fixture)
    predecessor.set_result(None)
    assert (await terminal(state))["state"] == "completed"
    assert len(used) == 1 and pending.closed
    await cleaned(frame)


@pytest.mark.parametrize("change", ["token", "issuance", "message", "policy"])
async def test_queued_chat_message_refused_when_anything_changed(
    metadata, runtime, fixture, monkeypatch, change,
):
    state, _, _ = metadata
    frame, pending, predecessor, used = await _chat_behind_a_turn(state, monkeypatch, fixture)
    if change == "token":
        state.orch.ui_sessions[state.socket]["_raw_token"] = "private replacement token"
    elif change == "issuance":
        await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
    elif change == "message":
        frame.parsed["payload"]["message"] = "changed after capture"
    else:
        monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "still-allows-primary-but-changed-policy")
    predecessor.set_result(None)
    assert (await terminal(state))["state"] == "failed"
    assert not used
    await cleaned(frame)


async def test_recheck_refuses_a_replaced_registration_and_a_closed_request(metadata, fixture, monkeypatch):
    state, _, _ = metadata
    frame, pending, predecessor, _ = await _chat_behind_a_turn(state, monkeypatch, fixture)
    state.orch.ui_sessions[state.socket] = deepcopy(state.orch.ui_sessions[state.socket])
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await human.recheck_queued_skill_read(pending)
    pending.close()
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await human.recheck_queued_skill_read(pending)
    predecessor.set_result(None)
    await terminal(state)
    await cleaned(frame)


async def test_recheck_refuses_a_message_that_no_longer_captures(metadata, fixture, monkeypatch):
    state, _, _ = metadata
    frame, pending, predecessor, used = await _chat_behind_a_turn(state, monkeypatch, fixture)
    pending.message = {**pending.message, "action": "not_a_guidance_read"}
    with pytest.raises(AssignmentError, match="human_authentication_required"):
        await human.recheck_queued_skill_read(pending)
    assert not pending.closed
    pending.close()
    predecessor.set_result(None)
    assert (await terminal(state))["state"] == "failed"
    assert not used
    await cleaned(frame)


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


@pytest.mark.parametrize("initial", ["registered", "preregistration"])
async def test_real_serialized_foreground_turn_keeps_original_guidance_authority(
    metadata, fixture, monkeypatch, initial,
):
    from orchestrator import turn_guidance_authority as guidance

    state, _, _ = metadata
    orch = state.orch
    orch.cancelled_sessions = {}
    chat_id = str(uuid4())
    monkeypatch.setattr(orch.history, "get_chat", lambda *_args, **_kwargs: {"id": chat_id})
    monkeypatch.setattr("orchestrator.user_skills.enabled", lambda: True)
    readers = []

    async def execute(socket, _message, chat, *_args, **_kwargs):
        reader = await guidance.acquire_turn_guidance_reader(
            expected_orchestrator=orch, websocket=socket, chat_id=chat)
        try:
            found = await reader.transaction(lambda tx, repos: repos.history.sessions.get_execution_state(
                tx, owner_id=reader.owner_id, session_id=fixture[2]), expected_orchestrator=orch)
            await reader.verify_delivery()
            with pytest.raises(AssignmentError, match="human_write_required"):
                reader.require_write()
            readers.append(found.credential.incarnation_id)
        finally:
            reader.close()

    monkeypatch.setattr(orch, "_handle_chat_message_with_guidance", execute)
    if initial == "registered":
        await registered(state)
    send(state, "chat_message", message="synthetic foreground turn", chat_id=chat_id)
    if initial == "preregistration":
        await state.barrier()
        await registered(state)
    result = await terminal(state)
    assert result["state"] == ("completed" if initial == "registered" else "failed"), state.socket.payloads()
    assert len(readers) == (1 if initial == "registered" else 0)
