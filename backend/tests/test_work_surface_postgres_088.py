"""Tests for registered Work presentation
(backend/orchestrator/work_surface_authority.py, work_service.py,
projection_surfaces/work.py): public-row rendering, WS propose/save parity, and
refusal of stale or foreign requests.
"""

import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.websockets import WebSocket, WebSocketState

from orchestrator import auth, chrome_events, web_auth
from orchestrator.projection_surfaces import work
from orchestrator.work_service import WorkService
from orchestrator.work_surface_authority import WorkSurfaceRead, invalidate
from persistent_agents.models import AssignmentError
from rote.capabilities import DeviceProfile
from shared.protocol import ChromeRender, ChromeSurface, ProtocolValidationError
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_work_submit_postgres_088 import (
    command, context, fixture as fixture, runtime as runtime,
    service as service, signing_key as signing_key,
)
from tests.test_work_save_postgres_088 import (
    approval_for, completed as completed, gate_orchestrator as gate_orchestrator,
    operation as operation, path as save_operation_path, plane as plane,
    post as save_post, proposal_body, research as research, saved as saved,
)


@pytest.fixture
async def surface(service, fixture, runtime):
    original_adapter = service.assignments.store.async_runtime
    accepted = await service.submit(await context(fixture, runtime), command(name="Owner release work"))
    orch = service.assignments.orch
    orch.persistent_assignments = service.assignments
    orch.web_sessions = fixture[0]
    orch.sent = []
    raw = fixture[3]()
    claims = await auth.verify_user(await auth.verify_production_token(raw))
    claims.update(_raw_token=raw, _client_capabilities=["work_read_v1"])
    async def unused(*_):
        raise AssertionError("fixture transport is handled by captured safe_send")
    socket = WebSocket({"type": "websocket", "path": "/ws", "headers": [
        (b"cookie", ("astral_session=" + web_auth._sign(fixture[2])).encode())]}, unused, unused)
    socket.client_state = WebSocketState.CONNECTED
    socket.application_state = WebSocketState.CONNECTED
    orch.ui_sessions = {socket: claims}
    orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.default())

    async def send(ws, frame):
        assert ws is socket
        orch.sent.append(json.loads(frame))
        return True

    async def forbidden(*_):
        raise AssertionError("Work read invoked model setup")

    orch._safe_send = send
    orch.llm_configured_for = forbidden
    yield orch, socket, accepted.record.assignment_id
    original_adapter.close()


async def open_work(surface, *, mode="detail", params=None, generation=None):
    orch, socket, identity = surface
    values = {"mode": mode}
    if mode != "list":
        values["operation_id"] = identity
    values.update(params or {})
    generation = str(uuid4()) if generation is None else generation
    assert await chrome_events.handle_chrome_event(orch, socket, "chrome_open",
        {"surface": "work", "params": values}, orch.ui_sessions[socket]["sub"],
        request_generation=generation)
    return generation


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["list", "detail", "result"])
async def test_registered_web_read_uses_actual_public_rows_and_echoes_request(surface, mode):
    orch, socket, identity = surface
    generation = await open_work(surface, mode=mode)
    assert len(orch.sent) == 1
    frame = orch.sent[0]
    assert frame["type"] == "chrome_render" and frame["surface_key"] == "work"
    assert frame["request_generation"] == generation and frame["region"] == "modal"
    assert "Owner release work" in frame["html"]
    assert "work.accept" not in frame["html"] and "_raw_token" not in frame["html"]
    assert not orch._work_surface_reads
    if mode == "result":
        assert "Work has not completed" in frame["html"]
    assert chrome_events.open_surface_for(orch, socket) == "work"


@pytest.mark.asyncio
async def test_result_view_shares_exact_public_revision_without_widening_http_shape(surface):
    orch, socket, identity = surface
    api = WorkService(orch.persistent_assignments)
    claims = orch.ui_sessions[socket]
    value = await api.result_view(claims["sub"], claims, identity)
    assert value["operation"]["revision"] == value["result"]["revision"]
    assert set(value["result"]) == {"id", "revision", "result"}
    assert "result" not in await api.get(claims["sub"], claims, identity)
    assert set(await api.result(claims["sub"], claims, identity)) == {"id", "revision", "result"}


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"owner_id": "someone-else"}, {"mode": "resume"}, {"operation_id": "not-an-id"},
    {"mode": "list", "after_id": "broken"}, {"source": {"url": "secret"}},
])
async def test_invalid_navigation_renders_redacted_unavailable_without_task_data(surface, params):
    orch = surface[0]
    await open_work(surface, params=params)
    assert len(orch.sent) == 1
    assert "Work is unavailable" in orch.sent[0]["html"]
    assert "Owner release work" not in orch.sent[0]["html"]
    assert "someone-else" not in orch.sent[0]["html"]


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["", "wrong", str(uuid4()).upper()])
async def test_invalid_request_generation_never_reads_or_delivers(surface, generation, monkeypatch):
    async def forbidden(*_):
        raise AssertionError("invalid correlation read data")
    monkeypatch.setattr(work, "_state", forbidden)
    await open_work(surface, generation=generation)
    assert not surface[0].sent


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "same_owner_registration", "mutate_claims", "logout",
    "cookie_replaced", "policy", "feature", "composition", "store", "adapter", "session_store",
    "navigation", "expiry", "mock"])
async def test_changed_caller_or_composition_after_database_read_never_delivers(
    surface, fixture, runtime, monkeypatch, change,
):
    orch, socket, _ = surface
    original = work._state

    async def state(read, params):
        value = await original(read, params)
        if change == "owner":
            orch.ui_sessions[socket] = {"sub": "different", "exp": time.time() + 60}
        elif change == "same_owner_registration":
            orch.ui_sessions[socket] = dict(orch.ui_sessions[socket])
        elif change == "mutate_claims":
            orch.ui_sessions[socket]["realm_access"] = {"roles": []}
        elif change == "logout":
            fixture[0].delete(fixture[2])
        elif change == "cookie_replaced":
            replace_session_record(runtime, get_session_record(runtime, fixture[2]))
        elif change == "policy":
            monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "no-longer-trusted")
        elif change == "feature":
            monkeypatch.setattr(orch.persistent_assignments, "enabled", False)
        elif change == "composition":
            monkeypatch.setattr(orch, "persistent_assignments", object())
        elif change == "store":
            monkeypatch.setattr(orch.persistent_assignments, "store", object())
        elif change == "adapter":
            monkeypatch.setattr(orch.persistent_assignments.store, "async_runtime", object())
        elif change == "session_store":
            monkeypatch.setattr(orch, "web_sessions", object())
        elif change == "navigation":
            invalidate(orch, socket)
        elif change == "expiry":
            read.delivery = replace(read.delivery, expires_at=time.time() - 1)
        elif change == "mock":
            monkeypatch.setenv("USE_MOCK_AUTH", "true")
        return value

    monkeypatch.setattr(work, "_state", state)
    await open_work(surface)
    assert not orch.sent and not getattr(orch, "_work_surface_reads", {})


@pytest.mark.asyncio
async def test_later_read_wins_even_when_earlier_read_finishes_last(surface, monkeypatch):
    orch = surface[0]
    first_entered, release = asyncio.Event(), asyncio.Event()
    original = work._state
    count = 0

    async def state(read, params):
        nonlocal count
        count += 1
        value = await original(read, params)
        if count == 1:
            first_entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(work, "_state", state)
    first = asyncio.create_task(open_work(surface, mode="detail"))
    try:
        await asyncio.wait_for(first_entered.wait(), 5)
        newest = await open_work(surface, mode="list")
        release.set()
        await asyncio.wait_for(first, 5)
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert len(orch.sent) == 1 and orch.sent[0]["request_generation"] == newest


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["windows", "android", "ios", "macos", "watch"])
async def test_legacy_native_without_explicit_support_receives_no_work(surface, device):
    orch, socket, _ = surface
    orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": device})
    orch.ui_sessions[socket]["_client_capabilities"] = []
    await open_work(surface)
    assert not orch.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["windows", "android", "ios", "macos", "watch"])
async def test_supported_native_gets_same_correlated_work_view(surface, device):
    orch, _, _ = surface
    orch.rote.get_profile = lambda _: DeviceProfile.from_dict({"device_type": device})
    generation = await open_work(surface)
    assert len(orch.sent) == 1
    assert orch.sent[0]["type"] == "chrome_surface"
    assert orch.sent[0]["request_generation"] == generation
    assert orch.sent[0]["surface_key"] == "work"
    assert "View result" in json.dumps(orch.sent[0]["components"])
    assert "_raw_token" not in json.dumps(orch.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["client_state", "application_state"])
async def test_closed_asgi_transport_cannot_receive_work(surface, field):
    setattr(surface[1], field, WebSocketState.DISCONNECTED)
    await open_work(surface)
    assert not surface[0].sent


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie", ["absent", "forged", "duplicate"])
async def test_cookie_selection_is_exact_and_native_bearer_read_never_borrows_session(surface, cookie):
    orch, socket, _ = surface
    headers = socket.scope["headers"]
    if cookie == "absent":
        headers.clear()
    elif cookie == "forged":
        headers[:] = [(b"cookie", b"astral_session=forged")]
    else:
        headers.append(headers[0])
    await open_work(surface)
    assert bool(orch.sent) is (cookie == "absent")


@pytest.mark.asyncio
async def test_cancellation_propagates_and_releases_pending_read(surface, monkeypatch):
    entered = asyncio.Event()

    async def state(*_):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(work, "_state", state)
    task = asyncio.create_task(open_work(surface))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not surface[0].sent and not surface[0]._work_surface_reads


@pytest.mark.asyncio
async def test_constructor_failure_does_not_retain_private_registration(surface, monkeypatch):
    orch, socket, _ = surface
    monkeypatch.setattr(orch.persistent_assignments, "enabled", False)
    with pytest.raises(AssignmentError):
        WorkSurfaceRead(orch, socket, orch.ui_sessions[socket]["sub"])
    assert not orch._work_surface_reads


@pytest.mark.asyncio
async def test_cookie_replacement_during_first_jwt_check_is_not_adopted(surface, fixture, runtime, monkeypatch):
    original = auth.verify_production_token
    replaced = False

    async def verify(token):
        nonlocal replaced
        if not replaced:
            replaced = True
            replace_session_record(runtime, get_session_record(runtime, fixture[2]))
        return await original(token)

    monkeypatch.setattr(auth, "verify_production_token", verify)
    await open_work(surface)
    assert not surface[0].sent


@pytest.mark.parametrize("frame", [ChromeRender(), ChromeSurface(surface_key="agents")])
def test_legacy_chrome_wire_fields_remain_absent(frame):
    value = json.loads(frame.to_json())
    assert "request_generation" not in value
    if isinstance(frame, ChromeRender):
        assert "surface_key" not in value


@pytest.mark.parametrize("frame", [ChromeRender(surface_key="work"), ChromeSurface(surface_key="work"),
    ChromeRender(request_generation=str(uuid4())),
    ChromeSurface(surface_key="agents", request_generation=str(uuid4()))])
def test_work_correlation_is_mandatory_and_scoped(frame):
    with pytest.raises(ProtocolValidationError):
        frame.to_json()


@pytest.fixture
async def save_surface(saved, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.invalid")
    orch = saved.op.executor.orch
    orch.web_sessions = saved.op.sessions
    orch.sent = []
    raw = saved.fixture[3]()
    claims = await auth.verify_user(await auth.verify_production_token(raw))
    claims.update(_raw_token=raw)

    async def unused(*_):
        raise AssertionError("fixture transport is handled by captured safe_send")

    socket = WebSocket({"type": "websocket", "path": "/ws", "headers": [
        (b"cookie", ("astral_session=" + web_auth._sign(saved.op.sid)).encode()),
        (b"origin", b"https://app.invalid"),
    ]}, unused, unused)
    socket.client_state = WebSocketState.CONNECTED
    socket.application_state = WebSocketState.CONNECTED
    orch.ui_sessions[socket] = claims
    orch._ws_active_chat[id(socket)] = saved.chat_id

    async def send(ws, frame):
        assert ws is socket
        orch.sent.append(json.loads(frame))
        return True

    orch._safe_send = send
    try:
        yield saved, orch, socket
    finally:
        orch.ui_sessions.pop(socket, None)
        orch._ws_active_chat.pop(id(socket), None)


def _propose_payload(saved_value, command):
    return {"version": 1, "command": "propose", "operation_id": saved_value.op.completed.assignment_id,
            **{k: command[k] for k in ("submission_id", "publication_id", "expected_revision",
                                       "conversation_id", "expected_workspace_revision",
                                       "expected_workspace_publication_id")}}


def _save_payload(saved_value, action_id, review):
    return {"version": 1, "command": "save", "operation_id": saved_value.op.completed.assignment_id,
            "action_id": action_id, **approval_for(review)}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)
async def test_ws_propose_then_save_matches_the_http_route(save_surface):
    saved_value, orch, socket = save_surface
    command = proposal_body(saved_value)
    handled = await chrome_events.handle_chrome_event(
        orch, socket, "chrome_work_result_save", _propose_payload(saved_value, command), saved_value.op.owner)
    assert handled is True
    assert len(orch.sent) == 1
    review_frame = orch.sent[-1]
    assert "Review the exact content" in review_frame["html"]
    assert "Reviewed result destination" in review_frame["html"]
    assert command["submission_id"] in review_frame["html"]

    http_review = await save_post(saved_value, save_operation_path(saved_value), command)
    assert http_review.status_code == 200, http_review.text
    review = http_review.json()

    handled = await chrome_events.handle_chrome_event(
        orch, socket, "chrome_work_result_save",
        _save_payload(saved_value, command["submission_id"], review), saved_value.op.owner)
    assert handled is True
    assert len(orch.sent) == 2
    saved_frame = orch.sent[-1]
    assert "Result saved" in saved_frame["html"]
    assert saved_frame["type"] == "chrome_render" and saved_frame["region"] == "modal"

    with saved_value.op.runtime.transaction() as tx:
        chat = saved_value.op.runtime.repositories.history.conversations.get(
            tx, owner_id=saved_value.op.owner, conversation_id=saved_value.chat_id)
    assert (chat.render_revision, chat.publication_id) == (1, command["publication_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)
async def test_ws_save_never_authenticates_a_foreign_or_forged_caller(save_surface):
    saved_value, orch, socket = save_surface
    command = proposal_body(saved_value)
    payload = _propose_payload(saved_value, command)

    handled = await chrome_events.handle_chrome_event(
        orch, socket, "chrome_work_result_save", payload, "someone-else")
    assert handled is True
    assert "Save request refused" in orch.sent[-1]["html"] or "went wrong" in orch.sent[-1]["html"]

    socket.scope["headers"] = [(b"cookie", ("astral_session=" + web_auth._sign(saved_value.op.sid)).encode()),
                               (b"origin", b"https://evil.invalid")]
    handled = await chrome_events.handle_chrome_event(
        orch, socket, "chrome_work_result_save", payload, saved_value.op.owner)
    assert handled is True
    last = orch.sent[-1]["html"]
    assert "Save request refused" in last or "went wrong" in last

    with saved_value.op.runtime.transaction() as tx:
        chat = saved_value.op.runtime.repositories.history.conversations.get(
            tx, owner_id=saved_value.op.owner, conversation_id=saved_value.chat_id)
    assert (chat.render_revision, chat.publication_id) == (0, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)
async def test_ws_malformed_save_command_is_a_silent_no_op(save_surface):
    saved_value, orch, socket = save_surface
    handled = await chrome_events.handle_chrome_event(
        orch, socket, "chrome_work_result_save", {"command": "propose"}, saved_value.op.owner)
    assert handled is True
    assert not orch.sent
