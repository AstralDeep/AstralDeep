"""Tests for orchestrator/orchestrator.py: starting a new chat retires the socket's old
presentation scope without cancelling in-flight work, and stale or replaced
connection generations never leak old-chat events into the new scope.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.orchestrator import Orchestrator
from rote.capabilities import DeviceProfile


OWNER = "scope-owner-088"
OTHER_OWNER = "other-scope-owner-088"
FENCE_FIELDS = {
    "chat_id",
    "connection_generation",
    "request_generation",
    "base_render_revision",
    "frame_sequence",
}


def _id() -> str:
    return str(uuid.uuid4())


class Socket:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_text(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


@pytest.fixture
def runtime(monkeypatch):
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    orchestrator = object.__new__(Orchestrator)
    socket, peer, foreign = Socket(), Socket(), Socket()
    old_chat, new_chat = _id(), _id()
    orchestrator.ui_clients = [socket, peer, foreign]
    orchestrator.ui_sessions = {
        socket: {"sub": OWNER},
        peer: {"sub": OWNER},
        foreign: {"sub": OTHER_OWNER},
    }
    orchestrator._ws_active_chat = {id(ws): old_chat for ws in (socket, peer, foreign)}
    orchestrator._ws_timeline_mode = {id(ws): True for ws in (socket, peer, foreign)}
    orchestrator._ws_welcome = {}
    orchestrator._conversation_scopes = {}
    for ws in (socket, peer, foreign):
        orchestrator._bind_conversation_scope(
            ws,
            chat_id=old_chat,
            connection_generation=_id(),
            request_generation=_id(),
            purpose="commit",
            base_render_revision=7,
        )
    orchestrator.history = SimpleNamespace(create_chat=Mock(return_value=new_chat))
    orchestrator.compute_tools_available_for_user = Mock(return_value=True)
    orchestrator.rote = SimpleNamespace(
        adapt=lambda _ws, components: copy.deepcopy(components),
        get_profile=lambda _ws: DeviceProfile.default(),
    )
    orchestrator._job_context = {"running-job": {"user_id": OWNER, "chat_id": old_chat}}
    orchestrator._refresh_history_after_commit = AsyncMock()
    orchestrator._conversation_snapshot_candidate = AsyncMock(
        return_value={"type": "conversation_snapshot", "chat_id": old_chat},
    )
    return SimpleNamespace(
        orchestrator=orchestrator,
        socket=socket,
        peer=peer,
        foreign=foreign,
        old_chat=old_chat,
        new_chat=new_chat,
    )


def _new_chat_request(runtime, *, correlated=False) -> dict:
    frame = {"type": "ui_event", "action": "new_chat", "payload": {}}
    if correlated:
        identities = {
            "schema_version": "1",
            "submission_id": _id(),
            "request_generation": _id(),
            "connection_generation": runtime.orchestrator._conversation_scopes[
                id(runtime.socket)
            ]["connection_generation"],
        }
        frame.update(identities)
        frame["payload"] = dict(identities)
    return frame


@pytest.mark.asyncio
@pytest.mark.parametrize("correlated", [False, True])
async def test_new_chat_welcome_is_unscoped_and_old_socket_context_is_retired(
    runtime,
    correlated,
):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    preserved_scopes = copy.deepcopy(orchestrator._conversation_scopes)
    old_jobs = copy.deepcopy(orchestrator._job_context)
    request = _new_chat_request(runtime, correlated=correlated)

    await orchestrator.handle_ui_message(socket, json.dumps(request))
    await asyncio.sleep(0)

    assert [frame["type"] for frame in socket.frames] == ["ui_render", "chat_created"]
    welcome, created = socket.frames
    assert not FENCE_FIELDS.intersection(welcome)
    assert welcome["target"] == "canvas"
    assert welcome["components"]
    assert created["payload"]["chat_id"] == runtime.new_chat
    assert created["payload"]["from_message"] is False
    if correlated:
        for field in ("connection_generation", "request_generation", "submission_id"):
            assert created[field] == request[field]
    assert id(socket) not in orchestrator._conversation_scopes
    assert id(socket) not in orchestrator._ws_active_chat
    assert id(socket) not in orchestrator._ws_timeline_mode
    assert orchestrator._ws_welcome[id(socket)] is True
    orchestrator.history.create_chat.assert_called_once_with(user_id=OWNER)
    assert orchestrator._job_context == old_jobs
    for ws in (runtime.peer, runtime.foreign):
        assert orchestrator._conversation_scopes[id(ws)] == preserved_scopes[id(ws)]
        assert orchestrator._ws_active_chat[id(ws)] == runtime.old_chat
        assert orchestrator._ws_timeline_mode[id(ws)] is True
        assert ws.frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("server_initiated", [False, True])
async def test_late_old_commit_fans_only_to_same_owner_still_viewing_old_chat(
    runtime,
    server_initiated,
):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    old_request = orchestrator._conversation_scopes[id(socket)]["request_generation"]
    orchestrator._conversation_scopes[id(runtime.peer)]["request_generation"] = (
        old_request
    )
    await orchestrator.handle_ui_message(socket, json.dumps(_new_chat_request(runtime)))
    socket.frames.clear()

    await orchestrator._deliver_committed_conversation_snapshot(
        socket,
        stage=SimpleNamespace(
            chat_id=runtime.old_chat, user_id=OWNER, base_render_revision=7
        ),
        request_generation=old_request,
        committed={"committed_render_revision": 8},
        server_initiated=server_initiated,
    )

    assert socket.frames == []
    assert runtime.foreign.frames == []
    assert runtime.peer.frames[-1]["type"] == "conversation_snapshot"
    assert orchestrator._sockets_on_chat(OWNER, runtime.old_chat) == [runtime.peer]
    assert orchestrator._job_context["running-job"]["chat_id"] == runtime.old_chat


@pytest.mark.asyncio
async def test_old_transient_is_never_relabelled_as_new_chat(runtime):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    old_frame = json.loads(
        orchestrator._scope_conversation_transient(
            socket,
            json.dumps({"type": "ui_render", "components": []}),
        )
    )
    await orchestrator.handle_ui_message(socket, json.dumps(_new_chat_request(runtime)))
    assert (
        json.loads(
            orchestrator._scope_conversation_transient(
                socket,
                json.dumps(old_frame),
            )
        )
        == old_frame
    )

    new_binding = orchestrator._bind_conversation_scope(
        socket,
        chat_id=runtime.new_chat,
        connection_generation=_id(),
        request_generation=_id(),
        purpose="commit",
        base_render_revision=0,
    )
    orchestrator._ws_active_chat[id(socket)] = runtime.new_chat
    assert (
        json.loads(
            orchestrator._scope_conversation_transient(
                socket,
                json.dumps(old_frame),
            )
        )
        == old_frame
    )
    assert new_binding["frame_sequence"] == 0
    current = json.loads(
        orchestrator._scope_conversation_transient(
            socket,
            json.dumps({"type": "ui_render", "components": []}),
        )
    )
    assert all(current[field] == new_binding[field] for field in FENCE_FIELDS)
    assert current["frame_sequence"] == 1
    assert current["chat_id"] != old_frame["chat_id"]


@pytest.mark.asyncio
async def test_unauthenticated_new_chat_does_not_retire_a_scope(runtime):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    scopes = copy.deepcopy(orchestrator._conversation_scopes)
    orchestrator.ui_sessions.pop(socket)
    await orchestrator.handle_ui_message(socket, json.dumps(_new_chat_request(runtime)))
    assert orchestrator._conversation_scopes == scopes
    assert orchestrator._ws_active_chat[id(socket)] == runtime.old_chat
    assert orchestrator._ws_timeline_mode[id(socket)] is True
    assert socket.frames == []
    orchestrator.history.create_chat.assert_not_called()


@pytest.mark.asyncio
async def test_failed_chat_creation_keeps_prior_scope(runtime):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    prior = orchestrator._conversation_scopes[id(socket)]
    orchestrator.history.create_chat.side_effect = RuntimeError("create unavailable")
    await orchestrator.handle_ui_message(socket, json.dumps(_new_chat_request(runtime)))
    assert orchestrator._conversation_scopes[id(socket)] is prior
    assert orchestrator._ws_active_chat[id(socket)] == runtime.old_chat
    assert orchestrator._ws_timeline_mode[id(socket)] is True
    assert not any(frame["type"] == "chat_created" for frame in socket.frames)


@pytest.mark.asyncio
async def test_welcome_failure_does_not_restore_the_old_scope(runtime):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    orchestrator.compute_tools_available_for_user.side_effect = RuntimeError(
        "tools unavailable"
    )
    await orchestrator.handle_ui_message(socket, json.dumps(_new_chat_request(runtime)))
    assert id(socket) not in orchestrator._conversation_scopes
    assert id(socket) not in orchestrator._ws_active_chat
    assert id(socket) not in orchestrator._ws_timeline_mode
    assert [frame["type"] for frame in socket.frames] == ["chat_created"]


@pytest.mark.asyncio
async def test_stale_connection_generation_is_refused_before_scope_reset(runtime):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    request = _new_chat_request(runtime, correlated=True)
    context = SimpleNamespace(
        websocket=socket, closing=False, connection_generation=uuid.uuid4()
    )
    scopes = copy.deepcopy(orchestrator._conversation_scopes)
    await orchestrator._enqueue_connection_frame(context, json.dumps(request), request)
    assert socket.frames[-1]["type"] == "error"
    assert socket.frames[-1]["accepted"] is False
    assert socket.frames[-1]["submission_id"] == request["submission_id"]
    assert socket.frames[-1]["code"] == "invalid_input"
    assert orchestrator._conversation_scopes == scopes
    orchestrator.history.create_chat.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["create", "tools"])
@pytest.mark.parametrize(
    "replacement", ["owner", "session", "connection", "generation", "logout", "closing"]
)
async def test_delayed_new_chat_never_changes_replacement_session(
    runtime,
    phase,
    replacement,
):
    import threading

    orchestrator, socket = runtime.orchestrator, runtime.socket
    context = SimpleNamespace(connection_generation=uuid.uuid4(), closing=False)
    orchestrator._connection_contexts = {id(socket): context}
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def delayed(*_args, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(timeout=5):
            raise AssertionError("test did not release the pending database read")
        return runtime.new_chat if phase == "create" else True

    if phase == "create":
        orchestrator.history.create_chat.side_effect = delayed
    else:
        orchestrator.compute_tools_available_for_user.side_effect = delayed
    task = asyncio.create_task(
        orchestrator.handle_ui_message(
            socket,
            json.dumps(_new_chat_request(runtime)),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if replacement == "owner":
            orchestrator.ui_sessions[socket]["sub"] = OTHER_OWNER
        elif replacement == "session":
            orchestrator.ui_sessions[socket] = {"sub": OWNER}
        elif replacement == "connection":
            orchestrator._connection_contexts[id(socket)] = SimpleNamespace(
                connection_generation=context.connection_generation,
                closing=False,
            )
        elif replacement == "generation":
            context.connection_generation = uuid.uuid4()
        elif replacement == "logout":
            orchestrator.ui_sessions.pop(socket)
        else:
            context.closing = True
        replacement_scope = {"chat_id": _id()}
        orchestrator._conversation_scopes[id(socket)] = replacement_scope
        orchestrator._ws_active_chat[id(socket)] = replacement_scope["chat_id"]
        orchestrator._ws_timeline_mode[id(socket)] = True
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert orchestrator._conversation_scopes[id(socket)] is replacement_scope
        assert orchestrator._ws_active_chat[id(socket)] == replacement_scope["chat_id"]
        assert orchestrator._ws_timeline_mode[id(socket)] is True
        assert socket.frames == []
        assert id(socket) not in orchestrator._ws_welcome
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["create", "tools", "render"])
async def test_replaced_session_during_failed_await_receives_no_old_error(
    runtime, phase
):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    entered = asyncio.Event()
    release = asyncio.Event()

    async def failing_await(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("unavailable after replacement")

    if phase == "render":
        orchestrator.send_ui_render = AsyncMock(side_effect=failing_await)
    else:
        import threading

        thread_release = threading.Event()
        loop = asyncio.get_running_loop()

        def failing_read(*_args, **_kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not thread_release.wait(timeout=5):
                raise AssertionError("test did not release the pending database read")
            raise RuntimeError("unavailable after replacement")

        if phase == "create":
            orchestrator.history.create_chat.side_effect = failing_read
        else:
            orchestrator.compute_tools_available_for_user.side_effect = failing_read
    task = asyncio.create_task(
        orchestrator.handle_ui_message(
            socket,
            json.dumps(_new_chat_request(runtime)),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        orchestrator.ui_sessions[socket] = {"sub": OWNER}
        release.set()
        if phase != "render":
            thread_release.set()
        await asyncio.wait_for(task, timeout=5)
        assert socket.frames == []
        assert id(socket) not in orchestrator._ws_welcome
    finally:
        release.set()
        if phase != "render":
            thread_release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_session_replacement_during_welcome_write_suppresses_followup(runtime):
    orchestrator, socket = runtime.orchestrator, runtime.socket
    session = orchestrator.ui_sessions[socket]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_send(raw):
        assert orchestrator.ui_sessions[socket] is session
        socket.frames.append(json.loads(raw))
        entered.set()
        await release.wait()

    socket.send_text = delayed_send
    task = asyncio.create_task(
        orchestrator.handle_ui_message(
            socket,
            json.dumps(_new_chat_request(runtime)),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        orchestrator.ui_sessions[socket] = {"sub": OWNER}
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert [frame["type"] for frame in socket.frames] == ["ui_render"]
        assert not FENCE_FIELDS.intersection(socket.frames[0])
        assert id(socket) not in orchestrator._ws_welcome
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
