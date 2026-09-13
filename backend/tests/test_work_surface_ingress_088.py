"""Work reads through real finite socket ingress and actual PostgreSQL admission.

The registration boundary verifies the fixture's signed JWT, then installs its
normal private claims without launching dashboard/voice startup. All subsequent
UI frame parsing, bounded enqueue/admission/lane dispatch, Work handler, read
policy/rendering, and connection drain are production code. Only transport and
external institutional replies are synthetic; no provider or chat is created.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import auth, chrome_availability
from orchestrator import orchestrator as module
from orchestrator.projection_surfaces import work
from orchestrator.work_admission import WorkAdmissionCoordinator
from orchestrator.work_surface_authority import WorkSurfaceRead, _transport_headers
from persistent_agents.models import AssignmentError
from starlette.datastructures import Headers
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.chrome.test_watch_workspace_disposition_088 import register_chrome_branch as register_chrome_branch
from tests.perf.test_runtime_reliability_060 import _MessageProbe, _orchestrator, _socket
from tests.test_work_surface_postgres_088 import (
    surface as surface, command as command, context as context,
    fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


@dataclass
class Ingress:
    orch: object
    socket: object
    token: str
    identity: str
    connection_generation: str
    registrations: asyncio.Queue
    frames: list
    arrivals: asyncio.Queue
    finished: asyncio.Queue

    def register(self):
        self.socket.feed(json.dumps({"type": "register_ui", "token": self.token,
            "connection_generation": self.connection_generation}))

    def request(self, *, mode="list", action="chrome_open", generation=None, submission=None):
        generation = generation or str(uuid4())
        params = {"mode": mode}
        if mode != "list":
            params["operation_id"] = self.identity
        value = {"type": "ui_event", "action": action,
            "submission_id": submission or str(uuid4()), "request_generation": generation,
            "connection_generation": self.connection_generation,
            "payload": {"surface": "work", "params": params}}
        self.socket.feed(json.dumps(value))
        return generation

    async def arrived(self, predicate):
        async with asyncio.timeout(5):
            while True:
                value = await self.arrivals.get()
                if predicate(value):
                    return value

    async def barrier(self):
        # A pong proves the preceding navigation frame passed _route_ui_frame.
        self.socket.feed(json.dumps({"type": "ping"}))
        await self.arrived(lambda value: value.get("type") == "pong")


@pytest.fixture
async def ingress(surface, runtime, monkeypatch):
    previous, old_socket, identity = surface
    original_claims = deepcopy(previous.ui_sessions[old_socket])
    orch = _orchestrator(module, _MessageProbe())
    orch.__dict__.update(vars(previous))
    orch.persistent_assignments.orch = orch
    orch.ui_sessions = {}
    socket = _socket(module)
    socket.scope = deepcopy(old_socket.scope)
    socket.closed = False
    orch.ui_clients = [socket]
    orch._registered_events[id(socket)] = asyncio.Event()
    orch.rote = SimpleNamespace(get_profile=previous.rote.get_profile, cleanup=lambda _: None)
    orch.work_admission = await asyncio.to_thread(WorkAdmissionCoordinator.from_plane,
        plane_runtime=runtime, slot_lease=timedelta(seconds=60))
    calls, arrivals, finished, registrations = [], asyncio.Queue(), asyncio.Queue(), asyncio.Queue()
    state = Ingress(orch, socket, original_claims["_raw_token"], identity, str(uuid4()),
                    registrations, calls, arrivals, finished)
    ordinary_frame = orch._connection_frame

    def frame(*args, **kwargs):
        value = ordinary_frame(*args, **kwargs)
        if value is not None:
            calls.append(value)
        return value

    orch._connection_frame = frame

    async def send(ws, data):
        assert ws is socket
        value = json.loads(data)
        socket.sent.append(data)
        arrivals.put_nowait(value)
        return True

    orch._safe_send = send

    async def handle(ws, raw):
        parsed = json.loads(raw)
        if parsed["type"] == "register_ui":
            claims = await auth.verify_user(await auth.verify_production_token(parsed["token"]))
            claims.update(_raw_token=parsed["token"], _client_capabilities=["work_read_v1"])
            orch.ui_sessions[ws] = claims
            orch._registered_events[id(ws)].set()
            registrations.put_nowait(claims)
            return
        try:
            await module.Orchestrator.handle_ui_message(orch, ws, raw)
        finally:
            finished.put_nowait(parsed)

    orch.handle_ui_message = handle

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Work read reached provider or conversation mutation")

    orch._call_llm = forbidden
    orch.history.create_chat = forbidden
    monkeypatch.setattr("audit.hooks.record_ws_action", forbidden_audit)
    serve = asyncio.create_task(orch._serve_ui_frames(socket, socket.receive_text))
    try:
        yield state
    finally:
        socket.closed = True
        socket.disconnect()
        try:
            await asyncio.wait_for(serve, 8)
        except module.WebSocketDisconnect:
            pass
        finally:
            if not serve.done():
                serve.cancel()
                await asyncio.gather(serve, return_exceptions=True)
        assert not getattr(orch, "_work_surface_reads", {})


async def forbidden_audit(**_kwargs):
    # Audit record content/privacy has separate qualification. Do not create
    # untracked background global-recorder work in this ingress routing fixture.
    return None


async def registered(state):
    state.register()
    await asyncio.wait_for(state.registrations.get(), 5)
    await state.barrier()


def work_frames(state):
    return [value for value in state.socket.payloads() if value.get("type") == "chrome_render"
            and value.get("surface_key") == "work"]


async def test_actual_serve_reaches_work_read_lane_with_original_generation(ingress):
    state = ingress
    await registered(state)
    generation = state.request(mode="detail")
    response = await state.arrived(lambda value: value.get("surface_key") == "work")
    assert response["request_generation"] == generation
    assert "Owner release work" in response["html"]
    admitted = next(value for value in state.frames if str(value.request_generation) == generation)
    assert admitted.read_only is True
    assert admitted.chat_id is None
    assert state.orch._ws_active_chat == {} and state.orch._chat_recorders == {}
    assert len(work_frames(state)) == 1


async def test_preregistration_work_read_is_not_replayed_after_later_login(ingress):
    state = ingress
    generation = state.request()
    await state.barrier()
    await registered(state)
    await state.barrier()
    assert not any(str(frame.request_generation) == generation for frame in state.frames)
    assert not work_frames(state)


@pytest.mark.parametrize("change", ["registration", "claims", "connection"])
async def test_original_ingress_lifetime_cannot_be_adopted_after_admission_wait(ingress, monkeypatch, change):
    state = ingress
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch._call_work_admission

    async def hold(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_submit_connection_batch":
            entered.set()
            await release.wait()
        return await original(callback, *args, **kwargs)

    monkeypatch.setattr(state.orch, "_call_work_admission", hold)
    generation = state.request()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == "registration":
            state.register()
            await asyncio.wait_for(state.registrations.get(), 5)
        elif change == "claims":
            state.orch.ui_sessions[state.socket]["private_test_marker"] = "changed"
        else:
            state.orch._connection_contexts[id(state.socket)].connection_generation = uuid4()
        release.set()
        await state.arrived(lambda value: value.get("type") in {"operation_terminal", "operation_rejected"}
                            or value.get("state") in {"completed", "failed", "cancelled"})
    finally:
        release.set()
    assert not work_frames(state)
    assert any(str(frame.request_generation) == generation for frame in state.frames)


@pytest.mark.parametrize("action", ["chrome_close", "load_chat", "new_chat"])
async def test_navigation_invalidates_active_work_before_its_queued_handler(ingress, monkeypatch, action):
    state = ingress
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = work._state

    async def hold(read, params):
        value = await original(read, params)
        entered.set()
        await release.wait()
        return value

    monkeypatch.setattr(work, "_state", hold)
    state.request()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Let the real ingress invalidate navigation immediately, while replacing
        # only its later non-Work application handler so no chat can be created.
        actual = state.orch.handle_ui_message
        async def handle(ws, raw):
            frame = json.loads(raw)
            if frame.get("action") == action:
                state.finished.put_nowait(frame)
                return
            await actual(ws, raw)
        monkeypatch.setattr(state.orch, "handle_ui_message", handle)
        state.request(action=action)
        await state.barrier()
        release.set()
        await state.arrived(lambda value: value.get("type") == "operation_status" and value.get("terminal") is True)
    finally:
        release.set()
    assert not work_frames(state)


async def test_newer_work_read_can_supersede_active_read_through_real_lane(ingress, monkeypatch):
    state = ingress
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = work._state
    count = 0

    async def hold(read, params):
        nonlocal count
        count += 1
        value = await original(read, params)
        if count == 1:
            entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(work, "_state", hold)
    old = state.request()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        latest = state.request(mode="detail")
        response = await state.arrived(lambda value: value.get("surface_key") == "work")
        assert response["request_generation"] == latest and latest != old
        release.set()
        await state.arrived(lambda value: value.get("type") == "operation_status" and value.get("terminal") is True)
    finally:
        release.set()
    assert [value["request_generation"] for value in work_frames(state)] == [latest]


async def test_work_arriving_during_reregistration_does_not_borrow_prior_login(ingress, monkeypatch):
    state = ingress
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    actual = state.orch.handle_ui_message

    async def held(ws, raw):
        if json.loads(raw).get("type") == "register_ui":
            entered.set()
            await release.wait()
        await actual(ws, raw)

    monkeypatch.setattr(state.orch, "handle_ui_message", held)
    state.register()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        state.request()
        await state.barrier()
        assert not work_frames(state)
        assert not state.orch._connection_contexts[id(state.socket)].operations
        release.set()
        await asyncio.wait_for(state.registrations.get(), 5)
        state.request()
        await state.arrived(lambda value: value.get("surface_key") == "work")
    finally:
        release.set()


@pytest.mark.parametrize("headers", ["asgi", "request_headers", "request.headers"])
async def test_original_cookie_issuance_is_fixed_before_admission_wait(
    ingress, runtime, fixture, monkeypatch, headers,
):
    state = ingress
    await registered(state)
    if headers != "asgi":
        frozen = state.socket.scope["headers"]
        del state.socket.scope
        if headers == "request_headers":
            state.socket.request_headers = Headers(raw=frozen)
        else:
            state.socket.request = SimpleNamespace(headers=Headers(raw=frozen))
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch._call_work_admission

    async def held(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_submit_connection_batch":
            entered.set()
            await release.wait()
        return await original(callback, *args, **kwargs)

    monkeypatch.setattr(state.orch, "_call_work_admission", held)
    generation = state.request()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        old = await asyncio.to_thread(get_session_record, runtime, fixture[2])
        replacement = await asyncio.to_thread(replace_session_record, runtime, old)
        assert replacement.incarnation_id != old.incarnation_id
        release.set()
        terminal = await state.arrived(lambda value: value.get("terminal") is True
                                       and value.get("request_generation") == generation)
        assert terminal["state"] == "failed"
    finally:
        release.set()
    assert not work_frames(state)


async def test_missing_cookie_issuance_refuses_before_durable_admission(ingress, runtime, fixture):
    state = ingress
    await registered(state)
    def retire():
        repository = runtime.repositories.history.sessions
        with runtime.transaction() as tx:
            row = repository.get(tx, owner_id=fixture[1], session_id=fixture[2])
            assert repository.delete(tx, owner_id=row.owner_id, session_id=row.session_id,
                                     expected_incarnation_id=row.incarnation_id)
    await asyncio.to_thread(retire)
    state.request()
    await state.barrier()
    assert not state.orch._connection_contexts[id(state.socket)].operations
    assert not state.orch._work_surface_reads
    assert not work_frames(state)


@pytest.mark.parametrize("malformed", ["extra", "params", "generation", "submission"])
async def test_malformed_work_is_refused_before_read_or_admission(ingress, malformed):
    state = ingress
    await registered(state)
    frame = {"type": "ui_event", "action": "chrome_open", "submission_id": str(uuid4()),
             "request_generation": str(uuid4()), "payload": {"surface": "work", "params": {}}}
    if malformed == "extra":
        frame["payload"]["owner_id"] = "another-owner"
    elif malformed == "params":
        frame["payload"]["params"] = {"mode": "resume"}
    else:
        frame.pop("request_generation" if malformed == "generation" else "submission_id")
    state.socket.feed(json.dumps(frame))
    await state.barrier()
    assert not state.orch._connection_contexts[id(state.socket)].operations
    assert not work_frames(state)


@pytest.mark.parametrize("malformed", ["conflicting", "missing_payload_surface"])
async def test_work_surface_envelope_is_refused_before_capture_or_admission(ingress, malformed):
    state = ingress
    await registered(state)
    frame = {
        "type": "ui_event", "action": "chrome_open",
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "surface": "settings",
        "payload": {"surface": "work", "params": {"mode": "list"}},
    }
    if malformed == "missing_payload_surface":
        frame["surface"] = "work"
        frame["payload"].pop("surface")
    state.socket.feed(json.dumps(frame))
    await state.barrier()
    assert not state.frames
    assert not state.orch._connection_contexts[id(state.socket)].operations
    assert not getattr(state.orch, "_work_surface_reads", {})
    assert not work_frames(state)


async def test_cancelled_cookie_capture_never_reselects_a_replacement(surface, runtime, fixture, monkeypatch):
    orch, ws, _ = surface
    read = WorkSurfaceRead(orch, ws, fixture[1])
    original = orch.web_sessions.capture_execution_reference
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def held(**kwargs):
        calls.append(kwargs)
        reference = original(**kwargs)
        entered.set()
        try:
            assert release.wait(5)
            return reference
        finally:
            finished.set()
    monkeypatch.setattr(orch.web_sessions, "capture_execution_reference", held)
    capture = asyncio.create_task(read.capture_session())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        capture.cancel()
        with pytest.raises(asyncio.CancelledError):
            await capture
        old = await asyncio.to_thread(get_session_record, runtime, fixture[2])
        await asyncio.to_thread(replace_session_record, runtime, old)
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        with pytest.raises(AssignmentError, match="work_authentication_required"):
            await read.capture_session()
        assert len(calls) == 1 and read.delivery.cookie_session is None
    finally:
        release.set()
        read.close()
        await asyncio.gather(capture, return_exceptions=True)


async def test_work_attempt_deadline_also_bounds_predecessor_lane_wait(ingress, monkeypatch):
    state = ingress
    await registered(state)
    context = state.orch._connection_contexts[id(state.socket)]
    predecessor = asyncio.get_running_loop().create_future()
    context.mutation_tail = predecessor
    original = state.orch._call_work_admission
    async def short_deadline(callback, *args, **kwargs):
        value = await original(callback, *args, **kwargs)
        if getattr(callback, "__name__", "") == "_submit_connection_batch":
            for frame, *_ in value:
                frame.work_read.deadline = time.monotonic() + 0.1
        return value
    monkeypatch.setattr(state.orch, "_call_work_admission", short_deadline)
    try:
        generation = state.request()
        terminal = await state.arrived(lambda value: value.get("terminal") is True
                                       and value.get("request_generation") == generation)
        assert terminal["state"] == "retryable" and terminal["error"]["code"] == "deadline_exceeded"
        assert terminal["error"]["message"] == "The Work view could not be loaded in time. Try again."
        assert not predecessor.done() and not work_frames(state)
    finally:
        if not predecessor.done():
            predecessor.set_result(None)


@pytest.mark.parametrize("headers", [None, [(1, b"x")], [(b"x", object())], [(b"x", "\ud800")],
    [(b"x", b"x" * 65537)], [(b"x", b"y")] * 257, [(b"x", b"y" * 1000)] * 66])
async def test_malformed_transport_headers_are_bounded_closed_refusals(headers):
    with pytest.raises(AssignmentError, match="work_authentication_required"):
        _transport_headers(SimpleNamespace(scope={"headers": headers}))


async def test_legacy_headers_preserve_duplicate_cookies_and_plain_bearer_absence():
    class RawHeaders:
        def raw_items(self):
            return [("Cookie", "astral_session=one"), ("cookie", "astral_session=two")]
    assert _transport_headers(SimpleNamespace(request_headers=RawHeaders())) == [
        (b"cookie", b"astral_session=one"), (b"cookie", b"astral_session=two")]
    assert _transport_headers(SimpleNamespace(request=SimpleNamespace(headers={"Cookie": "x=y"}))) == [
        (b"cookie", b"x=y")]
    assert _transport_headers(SimpleNamespace()) == []
    with pytest.raises(AssignmentError):
        _transport_headers(SimpleNamespace(request_headers=object()))


@pytest.mark.parametrize("generation", ["", "invalid", str(uuid4()).upper(), None])
async def test_ingress_guard_requires_exact_uuid4(generation):
    with pytest.raises(AssignmentError, match="work_query_invalid"):
        WorkSurfaceRead(None, None, "owner", context=object(), request_generation=generation)


@pytest.mark.parametrize("device", ["watch", "ios", "android", "macos", "windows"])
@pytest.mark.parametrize("capable,enabled", [(False, True), (True, False), (True, True)])
async def test_actual_registration_menu_negotiates_only_shared_work(
    monkeypatch, register_chrome_branch, device, capable, enabled,
):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(chrome_availability, "projection_chrome_availability", lambda: {
        "work_enabled": enabled, "export_enabled": True, "share_enabled": True,
    })
    host, ws = SimpleNamespace(_safe_send=AsyncMock()), object()
    await register_chrome_branch(device, host, ws, {"realm_access": {"roles": ["user"]},
        "_client_capabilities": ["work_read_v1"] if capable else []})
    if device == "watch" and not capable:
        host._safe_send.assert_not_awaited()
        return
    host._safe_send.assert_awaited_once()
    model = json.loads(host._safe_send.await_args.args[1])["model"]
    keys = [control["key"] for control in model["topbar"]]
    assert ("work" in keys) is (capable and enabled)
    if device == "watch":
        assert keys == (["work"] if capable and enabled else [])
        assert model["menu"] == [] and model["signout"] == {}
    else:
        assert "brand" in keys and "settings" in keys


async def test_failed_work_transport_terminalizes_failed_and_releases_private_read(ingress, monkeypatch):
    state = ingress
    await registered(state)
    actual = state.orch._safe_send
    attempted = []
    async def send(ws, raw):
        value = json.loads(raw)
        if value.get("surface_key") == "work":
            attempted.append(value)
            return False
        return await actual(ws, raw)
    monkeypatch.setattr(state.orch, "_safe_send", send)
    generation = state.request()
    terminal = await state.arrived(lambda value: value.get("terminal") is True
                                   and value.get("request_generation") == generation)
    assert terminal["state"] == "failed"
    assert terminal["error"] == {"code": "operation_failed", "message": "The operation could not be completed."}
    assert len(attempted) == 1 and not work_frames(state)
    await state.barrier()
    assert not state.orch._work_surface_reads
    assert state.token not in json.dumps(state.socket.payloads())


@pytest.mark.parametrize("failure", ["repository", "handoff"])
async def test_admission_failure_discards_original_private_read(ingress, monkeypatch, failure):
    state = ingress
    await registered(state)
    finished = asyncio.Event()
    if failure == "repository":
        def refuse(*_args, **_kwargs):
            raise TimeoutError("synthetic closed repository failure")
        monkeypatch.setattr(state.orch.work_admission, "submit", refuse)
    actual = state.orch._call_work_admission
    async def submit(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") != "_submit_connection_batch":
            return await actual(callback, *args, **kwargs)
        try:
            if failure == "handoff":
                raise TimeoutError("synthetic closed handoff failure")
            return await actual(callback, *args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(state.orch, "_call_work_admission", submit)
    state.request()
    await asyncio.wait_for(finished.wait(), 5)
    task = state.orch._connection_contexts[id(state.socket)].admission_task
    if failure == "handoff":
        with pytest.raises(TimeoutError):
            await task
    else:
        await task
    assert not state.orch._work_surface_reads
    assert all(frame.work_read is None for frame in state.frames)
    assert not work_frames(state)


async def test_private_guard_cannot_be_reused_for_another_request(ingress):
    state = ingress
    await registered(state)
    context = state.orch._connection_contexts[id(state.socket)]
    owner = state.orch.ui_sessions[state.socket]["sub"]
    generation = str(uuid4())
    read = WorkSurfaceRead(state.orch, state.socket, owner,
                           request_generation=generation, context=context)
    try:
        await read.capture_session()
        read.assert_request(state.orch, state.socket, owner, generation)
        for args in [(object(), state.socket, owner, generation),
                     (state.orch, object(), owner, generation),
                     (state.orch, state.socket, "other-owner", generation),
                     (state.orch, state.socket, owner, str(uuid4()))]:
            with pytest.raises(AssignmentError, match="work_authentication_required"):
                read.assert_request(*args)
    finally:
        read.close()
    assert not work_frames(state)
