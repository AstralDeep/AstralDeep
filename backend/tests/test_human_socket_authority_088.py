"""Tests for orchestrator/human_request_authority.py: registered-socket capture binds
the original caller through JWT waits, cancellation and reset, and boundary close
ordering against chrome_events and Plane.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import time
from uuid import uuid4

import pytest

from orchestrator import auth, human_request_authority as module
from orchestrator.orchestrator import ConnectionContext
from persistent_agents.models import AssignmentError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_human_request_authority_088 import (
    bound as bound, fixture as fixture, human as human, runtime as runtime,
    service as service, signing_key as signing_key,
)
from tests.test_work_control_authority_088 import incoming

pytestmark = pytest.mark.asyncio


@dataclass(eq=False)
class Socket:
    scope: dict
    closed: bool = False


@pytest.fixture
async def socket_request(human, bound, fixture):
    orch = human[2]
    request = incoming(bound, fixture)
    socket = Socket({**request.scope, "type": "websocket"})
    socket.scope.pop("method", None)
    token = fixture[3]()
    claims = await auth.verify_user(await auth.verify_production_token(token))
    claims["_raw_token"] = token
    orch.ui_sessions = {socket: claims}
    context = ConnectionContext(socket, uuid4(), time.monotonic() + 30,
        registered=True, connection_generation=uuid4())
    orch._connection_contexts = {id(socket): context}
    message = {"type": "ui_event", "action": "chrome_user_skill_save",
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "connection_generation": str(context.connection_generation),
        "payload": {"fields": {"name": "Private authored instructions"}}}
    yield socket, context, message


def capture(human, socket_request):
    socket, context, message = socket_request
    return module.capture_human_socket_request(human[1], websocket=socket,
        context=context, message=message)


async def test_registered_socket_yields_exact_current_human_without_refresh(
    human, socket_request, fixture, runtime,
):
    pending = capture(human, socket_request)
    try:
        await pending.capture_session()
        caller = await pending.authenticate()
        assert type(caller) is module.CurrentHumanCaller
        assert caller.owner_id == fixture[1] and caller.runtime is runtime
        assert caller.require_write() == "WS_WRITE"
        assert caller.require_session().credential.session_id == fixture[2]
        assert await caller.transaction(lambda tx, repos: repos is runtime.repositories,
            expected_orchestrator=human[2])
        await caller.verify_delivery()
        assert fixture[-1] == []
        for private in (fixture[3](), "Private authored instructions", fixture[1]):
            assert private not in repr(pending) and private not in repr(caller)
    finally:
        pending.close()


@pytest.mark.parametrize("action", ["chrome_user_skill_edit", "chrome_author_list", "chrome_open"])
async def test_original_read_cannot_upgrade_after_message_mutation(human, socket_request, action):
    socket, context, message = socket_request
    message["action"] = action
    if action == "chrome_open":
        message["payload"] = {"surface": "agent_authoring"}
    pending = capture(human, socket_request)
    try:
        caller = await pending.authenticate()
        with pytest.raises(AssignmentError, match="human_write_required"):
            caller.require_write()
        message["action"] = "chrome_user_skill_save"
        with pytest.raises(AssignmentError):
            await caller.transaction(lambda tx, _: True, expected_orchestrator=human[2])
    finally:
        pending.close()


@pytest.mark.parametrize("loss", ["same_owner_registration", "owner", "token", "context",
    "generation", "pending_registration", "closed", "closing", "unregistered", "message"])
async def test_socket_lifetime_changes_refuse_before_callback(human, socket_request, loss):
    socket, context, message = socket_request
    pending = capture(human, socket_request)
    try:
        caller = await pending.authenticate()
        if loss == "same_owner_registration":
            human[2].ui_sessions[socket] = deepcopy(human[2].ui_sessions[socket])
        elif loss in {"owner", "token"}:
            human[2].ui_sessions[socket]["sub" if loss == "owner" else "_raw_token"] = "changed"
        elif loss == "context":
            human[2]._connection_contexts[id(socket)] = object()
        elif loss == "generation":
            context.connection_generation = uuid4()
        elif loss == "pending_registration":
            context.work_registrations_pending = 1
        elif loss == "closed":
            socket.closed = True
        elif loss == "closing":
            context.closing = True
        elif loss == "unregistered":
            context.registered = False
        else:
            message["payload"]["fields"]["name"] = "Replaced intent"
        effects = []
        with pytest.raises(AssignmentError):
            await caller.transaction(lambda tx, _: effects.append(True), expected_orchestrator=human[2])
        assert effects == []
        with pytest.raises(AssignmentError):
            await caller.verify_delivery()
    finally:
        pending.close()


async def test_captured_issuance_is_not_replaced_during_normal_jwt_wait(
    human, socket_request, fixture, runtime, monkeypatch,
):
    pending = capture(human, socket_request)
    await pending.capture_session()
    entered, release = asyncio.Event(), asyncio.Event()
    verify = auth.verify_production_token

    async def held(token):
        entered.set()
        await release.wait()
        return await verify(token)

    monkeypatch.setattr(auth, "verify_production_token", held)
    task = asyncio.create_task(pending.authenticate())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        old = get_session_record(runtime, fixture[2])
        replacement = await asyncio.to_thread(replace_session_record, runtime, old)
        assert replacement.incarnation_id != old.incarnation_id
        release.set()
        with pytest.raises(AssignmentError):
            await task
        assert fixture[-1] == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        pending.close()


async def test_closed_capture_retires_caller_and_private_context(human, socket_request):
    pending = capture(human, socket_request)
    caller = await pending.authenticate()
    assert module.current_human_caller() is None
    with module.bind_human_caller(caller):
        assert module.current_human_caller() is caller
        pending.close()
        with pytest.raises(AssignmentError):
            module.current_human_caller()
    assert module.current_human_caller() is None
    with pytest.raises(AssignmentError):
        await caller.verify_delivery()


async def test_unknown_action_is_not_a_metadata_write(human, socket_request):
    socket_request[2]["action"] = "chrome_not_registered"
    assert capture(human, socket_request) is None


async def test_payload_does_not_choose_authority_or_method(human, socket_request):
    socket_request[2]["action"] = "chrome_user_skill_edit"
    socket_request[2]["payload"].update(method="POST", write=True, owner_id="other-owner")
    pending = capture(human, socket_request)
    try:
        caller = await pending.authenticate()
        assert caller.owner_id == human[2].ui_sessions[socket_request[0]]["sub"]
        with pytest.raises(AssignmentError, match="human_write_required"):
            caller.require_write()
        assert json.loads(caller.context._claims_json)["sub"] == caller.owner_id
    finally:
        pending.close()


async def test_wire_identity_locations_remain_compatible_and_conflicts_refuse(human, socket_request):
    message = socket_request[2]
    for key in ("submission_id", "request_generation", "connection_generation"):
        message["payload"][key] = message.pop(key)
    pending = capture(human, socket_request)
    try:
        assert (await pending.authenticate()).require_write() == "WS_WRITE"
    finally:
        pending.close()
    message["submission_id"] = str(uuid4())
    with pytest.raises(AssignmentError):
        capture(human, socket_request)


async def test_invalid_original_transport_and_metadata_are_closed(human, socket_request):
    socket, _, message = socket_request
    original_headers = list(socket.scope["headers"])
    original_message = deepcopy(message)
    malformed = [
        [(b"cookie", b"astral_session=forged")],
        [(b"cookie", b"astral_session=")],
        [(b"origin", b"https://hostile.invalid")],
        [(b"origin", b"not-an-origin")],
    ]
    for headers in malformed:
        socket.scope["headers"] = [*original_headers, *headers]
        with pytest.raises(AssignmentError):
            capture(human, socket_request)
    socket.scope["headers"] = [(k, v) for k, v in original_headers if k != b"origin"]
    with pytest.raises(AssignmentError):
        capture(human, socket_request)
    socket.scope["headers"] = original_headers
    for query in (b"token=private", b"%74oken=private", b"x" * 65537, b"\xff"):
        socket.scope["query_string"] = query
        with pytest.raises(AssignmentError) as caught:
            capture(human, socket_request)
        assert "private" not in str(caught.value)
    socket.scope["query_string"] = b""
    for patch in ({"submission_id": "wrong"}, {"request_generation": None},
                  {"connection_generation": str(uuid4())},
                  {"payload": {"text": "x" * (128 * 1024)}},
                  {"payload": {"value": float("nan")}}, {"payload": {"value": "\ud800"}}):
        message.clear()
        message.update(deepcopy(original_message))
        message.update(patch)
        with pytest.raises(AssignmentError):
            capture(human, socket_request)
    for value in (None, [], {}, {"type": "ping"}, {"type": "ui_event", "action": []}):
        assert module.capture_human_socket_request(human[1], websocket=socket,
            context=socket_request[1], message=value) is None


async def test_bare_registered_native_human_does_not_borrow_owner_latest(human, socket_request, fixture):
    socket = socket_request[0]
    socket.scope["headers"] = [(k, v) for k, v in socket.scope["headers"] if k not in {b"cookie", b"origin"}]
    pending = capture(human, socket_request)
    try:
        caller = await pending.authenticate()
        assert caller.require_write() == "WS_WRITE" and caller.caller is None
        with pytest.raises(AssignmentError):
            caller.require_session()
        assert not fixture[-1]
    finally:
        pending.close()


async def test_original_token_failure_never_uses_replacement_registration(human, socket_request, monkeypatch):
    pending = capture(human, socket_request)
    seen = []
    async def fail(token):
        seen.append(token)
        human[2].ui_sessions[socket_request[0]]["_raw_token"] = "replacement-secret"
        raise RuntimeError("private external failure detail")
    monkeypatch.setattr(auth, "verify_production_token", fail)
    try:
        with pytest.raises(AssignmentError) as caught:
            await pending.authenticate()
        assert len(seen) == 1 and seen[0] != "replacement-secret"
        assert "private external" not in str(caught.value)
        with pytest.raises(AssignmentError):
            await pending.authenticate()
        assert len(seen) == 1
    finally:
        pending.close()


async def test_missing_original_issuance_is_closed_before_auth(human, socket_request, fixture, monkeypatch):
    pending = capture(human, socket_request)
    fixture[0].delete(fixture[2])
    async def forbidden(*_):
        raise AssertionError("missing issuance reached IAM")
    monkeypatch.setattr(auth, "verify_production_token", forbidden)
    try:
        with pytest.raises(AssignmentError):
            await pending.authenticate()
    finally:
        pending.close()


async def test_capture_cancellation_cannot_reselect_and_context_resets(human, socket_request, monkeypatch):
    pending = capture(human, socket_request)
    entered, release = asyncio.Event(), asyncio.Event()
    original = human[1].adapter.run_in_transaction
    async def held(callback):
        entered.set()
        await release.wait()
        return await original(callback)
    monkeypatch.setattr(human[1].adapter, "run_in_transaction", held)
    task = asyncio.create_task(pending.capture_session())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        with pytest.raises(AssignmentError):
            await pending.authenticate()
        assert module.current_human_caller() is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        pending.close()


async def test_capture_repository_failures_are_data_free(human, socket_request, monkeypatch):
    from astralplane.repositories import RepositoryConflictError
    for error in (RepositoryConflictError("changed"), RuntimeError("private database detail")):
        pending = capture(human, socket_request)
        async def fail(callback):
            raise error
        with monkeypatch.context() as patch:
            patch.setattr(human[1].adapter, "run_in_transaction", fail)
            with pytest.raises(AssignmentError) as caught:
                await pending.capture_session()
            assert caught.value.status_code in {401, 503}
            assert "private" not in str(caught.value)
        pending.close()


async def test_authentication_wait_uses_original_deadline(human, socket_request, monkeypatch):
    pending = capture(human, socket_request)
    pending.deadline = time.monotonic() + 0.1
    entered = asyncio.Event()
    async def held(*_):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(auth, "verify_production_token", held)
    try:
        with pytest.raises(AssignmentError) as caught:
            await pending.authenticate()
        assert entered.is_set() and caught.value.status_code == 408
    finally:
        pending.close()


async def test_invalid_boundary_registration_token_and_context_binding_are_closed(human, socket_request):
    socket, context, message = socket_request
    for boundary in (None, object()):
        with pytest.raises(AssignmentError):
            module.capture_human_socket_request(boundary, websocket=socket, context=context, message=message)
    registration = human[2].ui_sessions[socket]
    for value in (None, {}, {**registration, "_raw_token": ""}, {**registration, "act": {"sub": "agent"}}):
        human[2].ui_sessions[socket] = value
        with pytest.raises(AssignmentError):
            capture(human, socket_request)
    human[2].ui_sessions[socket] = registration
    with pytest.raises(AssignmentError):
        with module.bind_human_caller(object()):
            raise AssertionError("forged caller context")
    with pytest.raises(AssignmentError):
        module.capture_human_socket_request(human[1], websocket=socket, context=context,
            message=message, purpose="write_from_payload")


async def test_lookup_helpers_refuse_wrong_purpose_or_missing_ingress(human, socket_request):
    with pytest.raises(AssignmentError, match="human_skill_lookup_unavailable"):
        await module.current_socket_human_read(expected_orchestrator=human[2], websocket=socket_request[0])
    pending = capture(human, socket_request)
    try:
        caller = await pending.authenticate()
        with pytest.raises(AssignmentError):
            module.retire_socket_human_read(caller)
        with pytest.raises(AssignmentError):
            module.retire_socket_human_read(None)
        assert module.capture_human_socket_request(human[1], websocket=socket_request[0],
            context=socket_request[1], message=socket_request[2], purpose="skill_lookup") is None
    finally:
        pending.close()


async def test_application_close_retires_boundary_before_audit_and_plane(human, monkeypatch):
    from types import SimpleNamespace
    from orchestrator import orchestrator as host
    from tests.test_runtime_composition_074 import _StartAsyncTasks
    events = []
    boundary = human[1]
    owner = host.Orchestrator.__new__(host.Orchestrator)
    owner.human_request_boundary = boundary
    owner.async_task_manager = _StartAsyncTasks(events)
    async def close_audit():
        assert boundary.closed
        events.append("audit")
    async def close_plane():
        assert boundary.closed
        events.append("plane")
    owner._owned_audit_recorder = SimpleNamespace(close=close_audit)
    owner.runtime_composition = SimpleNamespace(close=close_plane)
    await owner._close_started_services()
    assert events[-2:] == ["audit", "plane"]
    assert boundary.adapter._closed


async def test_failed_construction_retires_boundary_and_still_aborts_graph(human):
    from types import SimpleNamespace
    from orchestrator import orchestrator as host
    boundary = human[1]
    events = []
    @host._transactional_runtime_construction
    def construct(owner):
        owner.human_request_boundary = boundary
        owner.runtime_composition = SimpleNamespace(abort=lambda: events.append(boundary.closed))
        raise RuntimeError("synthetic constructor failure")
    with pytest.raises(RuntimeError, match="synthetic constructor failure"):
        construct(SimpleNamespace())
    assert events == [True] and boundary.closed


async def test_boundary_close_failure_does_not_prevent_runtime_cleanup(human, monkeypatch):
    from types import SimpleNamespace
    from orchestrator import orchestrator as host
    from tests.test_runtime_composition_074 import _StartAsyncTasks
    events = []
    original_close = human[1].close
    def fail():
        events.append("boundary")
        raise RuntimeError("synthetic close failure")
    monkeypatch.setattr(human[1], "close", fail)
    owner = host.Orchestrator.__new__(host.Orchestrator)
    owner.human_request_boundary = human[1]
    owner.async_task_manager = _StartAsyncTasks(events)
    async def close_plane():
        events.append("plane")
    owner.runtime_composition = SimpleNamespace(close=close_plane)
    try:
        with pytest.raises(BaseExceptionGroup):
            await owner._close_started_services()
        assert events[-1] == "plane" and events.count("boundary") == 2
    finally:
        monkeypatch.setattr(human[1], "close", original_close)


async def test_wrong_delivery_socket_cannot_use_private_caller(human, socket_request):
    from orchestrator import chrome_events
    pending = capture(human, socket_request)
    try:
        caller = await pending.authenticate()
        with module.bind_human_caller(caller), pytest.raises(AssignmentError):
            await chrome_events._push_modal(human[2], object(), "Never delivered")
    finally:
        pending.close()


async def test_chrome_deadline_cancels_handler_and_resets_private_context(human, socket_request, monkeypatch):
    from orchestrator import chrome_events, orchestrator as host
    pending = capture(human, socket_request)
    entered = asyncio.Event()
    cancelled = []
    async def held(*_args, **_kwargs):
        assert type(module.current_human_caller()) is module.CurrentHumanCaller
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    monkeypatch.setattr(chrome_events, "_handle_chrome_event", held)
    token = host._CONNECTION_OPERATION_CONTEXT.set({"human_request": pending})
    try:
        pending.deadline = time.monotonic() + 0.15
        with pytest.raises(AssignmentError) as caught:
            await chrome_events.handle_chrome_event(human[2], socket_request[0],
                socket_request[2]["action"], socket_request[2]["payload"], "ignored-owner")
        assert entered.is_set() and cancelled == [True] and caught.value.status_code == 408
        assert module.current_human_caller() is None
    finally:
        host._CONNECTION_OPERATION_CONTEXT.reset(token)
        pending.close()


async def test_threaded_operation_context_survives_a_reset_context_var(human, socket_request):
    from orchestrator.orchestrator import _CONNECTION_OPERATION_CONTEXT

    socket, context, _message = socket_request
    chat_frame = {"type": "ui_event", "action": "chat_message",
                  "submission_id": str(uuid4()), "request_generation": str(uuid4()),
                  "connection_generation": str(context.connection_generation),
                  "payload": {"message": "hello"}}
    pending = module.capture_human_socket_request(
        human[1], websocket=socket, context=context, message=chat_frame,
        purpose="skill_lookup")
    assert pending is not None and pending.method == "WS_READ"
    try:
        await pending.capture_session()
        operation_context = {"human_request": pending}

        token = _CONNECTION_OPERATION_CONTEXT.set(None)
        try:
            with pytest.raises(AssignmentError, match="human_skill_lookup_unavailable"):
                await module.current_socket_human_read(
                    expected_orchestrator=human[2], websocket=socket)

            caller = await module.current_socket_human_read(
                expected_orchestrator=human[2], websocket=socket,
                operation_context=operation_context)
            assert type(caller).__name__ == "CurrentHumanCaller"
        finally:
            _CONNECTION_OPERATION_CONTEXT.reset(token)
    finally:
        pending.close()
