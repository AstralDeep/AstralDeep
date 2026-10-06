"""Exercises authenticated connection publication against the real conversation repositories and execution fences. It checks captured socket ownership, queued identity changes and component inference without altering operation ownership. It also checks that a component commit is announced to every idle socket its owner has on that chat."""

import asyncio
import copy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator.conversation_publication import current_conversation_publication
from orchestrator.history import ConversationNotFound, HistoryManager
from orchestrator.orchestrator import (
    ConnectionContext,
    Orchestrator,
    _ConnectionOperation,
    _CONNECTION_OPERATION_CONTEXT,
)
from orchestrator.work_admission import (
    AdmissionClass,
    OperationOwner,
    OperationState,
    OwnerScope,
    StaleExecutionFenceError,
)
from rote.capabilities import DeviceProfile
from tests.test_conversation_snapshot_060 import (
    _coordinator,
    _repository,
    _workspace_manager,
    database as database,
    postgres_database as postgres_database,
)


OWNER = "connection-publication-owner"
FOREIGN = "connection-publication-foreign"


class Socket:
    def __init__(self):
        self.frames = []

    async def send_text(self, raw):
        self.frames.append(json.loads(raw))


class Rote:
    def get_profile(self, _socket):
        return DeviceProfile.default()

    def adapt(self, _socket, components, *, cache=True):
        return copy.deepcopy(components)


def runtime():
    host = object.__new__(Orchestrator)
    socket = Socket()
    host.ui_sessions = {socket: {"sub": OWNER, "_raw_token": "never-copy-this-token"}}
    host.ui_clients = [socket]
    host._ws_active_chat = {}
    host._conversation_scopes = {}
    host._workspace_locks = {}
    host.rote = Rote()
    context = ConnectionContext(socket, uuid4(), 999999, connection_generation=uuid4(), registered=True)
    return host, socket, context


def frame(host, context, *, action="table_paginate", chat_id=None, **payload):
    request, submission = str(uuid4()), str(uuid4())
    body = {"type": "ui_event", "action": action, "submission_id": submission,
            "request_generation": request, "connection_generation": str(context.connection_generation),
            "payload": payload | {"request_generation": request, "submission_id": submission}}
    if chat_id is not None:
        body["session_id"] = chat_id
        body["payload"]["chat_id"] = chat_id
    result = host._connection_frame(context, json.dumps(body), body)
    assert result is not None
    return result


def work(context, ingress, *, owner=None, operation_id=None):
    return _ConnectionOperation(ingress, owner or OperationOwner(OwnerScope.CONNECTION, None, context.connection_scope_id),
                                operation_id or uuid4())


def attach_plane(host, database):
    host.history = HistoryManager(plane_runtime=database, plane_repositories=database.repositories)
    host.workspace = _workspace_manager(host.history, database)
    host.work_admission = _coordinator(database)
    host.conversation_commits = _repository(database, host.work_admission)


def admitted(host, context, ingress):
    _frame, owner, acceptance, projection = host._submit_connection_batch(context, [ingress])[0]
    assert acceptance.accepted
    claim = host.work_admission.claim_operation(AdmissionClass.INTERACTIVE, acceptance.operation_id)
    assert claim is not None
    return work(context, ingress, owner=owner, operation_id=acceptance.operation_id), claim, projection


@pytest.mark.asyncio
async def test_connection_publication_uses_captured_authenticated_owner_with_real_fence(database):
    host, socket, context = runtime()
    attach_plane(host, database)
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    ingress = frame(host, context, chat_id=chat)
    operation, claim, before = await asyncio.to_thread(admitted, host, context, ingress)
    assert ingress.authenticated_user_id == OWNER and "never-copy-this-token" not in repr(ingress)
    assert operation.owner.owner_scope is OwnerScope.CONNECTION and operation.owner.owner_user_id is None
    assert before.owner_scope is OwnerScope.CONNECTION
    assert claim.operation.owner_user_id is None and claim.operation.connection_scope_id == context.connection_scope_id
    handled = []

    async def handle(opened, _raw):
        stage = current_conversation_publication()
        assert opened is socket and stage.user_id == OWNER and stage.operation_fence == claim.fence
        current = await asyncio.to_thread(host.work_admission.assert_current_execution, claim.fence)
        assert current.owner_user_id is None and current.connection_scope_id == context.connection_scope_id
        assert current.execution_generation == claim.fence.execution_generation
        handled.append(stage.commit_id)
        await host._append_conversation_message(stage, chat_id=chat, user_id=OWNER, role="assistant", content="Synthetic owner result")

    host.handle_ui_message = handle
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": claim.operation, "owner": operation.owner,
                                               "execution_fence": claim.fence})
    try:
        await host._run_connection_ui_operation(context, operation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    assert len(handled) == 1 and current_conversation_publication() is None
    actual = await asyncio.to_thread(host.history.get_chat, chat, user_id=OWNER)
    assert actual["messages"][0]["content"] == "Synthetic owner result"
    assert await asyncio.to_thread(host.history.get_chat, chat, user_id=FOREIGN) is None
    assert await asyncio.to_thread(host.history.get_chat, chat, user_id="legacy") is None
    after = await asyncio.to_thread(host.work_admission.query_operation,
                                    owner=operation.owner, operation_id=operation.operation_id)
    assert after.state is OperationState.COMPLETED and after.owner_scope is OwnerScope.CONNECTION
    with pytest.raises(StaleExecutionFenceError):
        await asyncio.to_thread(host.work_admission.assert_current_execution, claim.fence)
    snapshots = [value for value in socket.frames if value.get("type") == "conversation_snapshot"]
    assert len(snapshots) == 1 and snapshots[0]["chat_id"] == chat


def idle_on(host, socket, chat, *, connection_generation=None, owner=OWNER):
    if socket not in host.ui_clients:
        host.ui_clients.append(socket)
    host.ui_sessions[socket] = {"sub": owner}
    host._ws_active_chat[id(socket)] = chat
    scope = host._bind_conversation_scope(socket, chat_id=chat, connection_generation=connection_generation or uuid4(),
                                          request_generation=uuid4(), purpose="hydration", base_render_revision=0)
    scope["snapshot_completed"] = True
    return scope


def continuity(socket):
    return [value for value in socket.frames
            if value.get("type") in {"conversation_commit_ready", "conversation_snapshot"}]


async def run_component_action(host, context, chat, handle):
    ingress = frame(host, context, action="component_action", chat_id=chat, component_id="card", kind="refresh")
    operation, claim, _ = await asyncio.to_thread(admitted, host, context, ingress)
    host.handle_ui_message = handle
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": claim.operation, "owner": operation.owner,
                                               "execution_fence": claim.fence})
    try:
        await host._run_connection_ui_operation(context, operation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    return str(claim.operation.request_generation)


async def refresh_card(host, chat):
    await host._append_conversation_message(current_conversation_publication(), chat_id=chat, user_id=OWNER,
                                            role="assistant", content="Synthetic refreshed card")


@pytest.mark.asyncio
async def test_component_commit_is_announced_to_every_idle_socket_of_the_owner_on_that_chat(database):
    host, socket, context = runtime()
    attach_plane(host, database)
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    elsewhere = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    hydrated = idle_on(host, socket, chat, connection_generation=context.connection_generation)
    peer, away, foreign = Socket(), Socket(), Socket()
    peer_scope = idle_on(host, peer, chat)
    idle_on(host, away, elsewhere)
    idle_on(host, foreign, chat, owner=FOREIGN)

    request = await run_component_action(host, context, chat, lambda _socket, _raw: refresh_card(host, chat))

    assert request != hydrated["request_generation"]
    for receiver, connection in ((socket, str(context.connection_generation)),
                                 (peer, peer_scope["connection_generation"])):
        ready, snapshot = continuity(receiver)
        assert receiver.frames.index(snapshot) == receiver.frames.index(ready) + 1
        assert ready == {"type": "conversation_commit_ready", "schema_version": 1, "chat_id": chat,
                         "connection_generation": connection, "request_generation": request, "render_revision": 1}
        assert snapshot["type"] == "conversation_snapshot" and snapshot["snapshot_purpose"] == "commit"
        assert (snapshot["chat_id"], snapshot["connection_generation"], snapshot["request_generation"],
                snapshot["render_revision"]) == (chat, connection, request, 1)
        assert snapshot["transcript"][0]["parts"][0]["text"] == "Synthetic refreshed card"
        scope = host._conversation_scopes[id(receiver)]
        assert scope["purpose"] == "commit" and scope["request_generation"] == request
        assert scope["base_render_revision"] == 1 and scope["snapshot_completed"] is True
    assert continuity(away) == [] and continuity(foreign) == []


@pytest.mark.asyncio
async def test_component_operation_that_changes_nothing_announces_nothing(database):
    host, socket, context = runtime()
    attach_plane(host, database)
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    idle_on(host, socket, chat, connection_generation=context.connection_generation)
    peer = Socket()
    peer_scope = copy.deepcopy(idle_on(host, peer, chat))

    await run_component_action(host, context, chat, AsyncMock())

    assert continuity(socket) == [] and peer.frames == []
    assert host._conversation_scopes[id(peer)] == peer_scope
    stored = await asyncio.to_thread(host.history.get_chat, chat, user_id=OWNER)
    assert stored["messages"] == []


@pytest.mark.asyncio
async def test_failed_prelude_withholds_that_socket_snapshot_without_blocking_its_peer(database):
    host, socket, context = runtime()
    attach_plane(host, database)
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    idle_on(host, socket, chat, connection_generation=context.connection_generation)
    peer = Socket()
    idle_on(host, peer, chat)
    delivered = socket.send_text

    async def lose_prelude(raw):
        if json.loads(raw).get("type") == "conversation_commit_ready":
            raise ConnectionError("synthetic transport loss")
        await delivered(raw)

    socket.send_text = lose_prelude

    request = await run_component_action(host, context, chat, lambda _socket, _raw: refresh_card(host, chat))

    assert continuity(socket) == []
    assert [value["type"] for value in continuity(peer)] == ["conversation_commit_ready", "conversation_snapshot"]
    assert all(value["request_generation"] == request for value in continuity(peer))
    stored = await asyncio.to_thread(host.history.get_chat, chat, user_id=OWNER)
    assert stored["messages"][0]["content"] == "Synthetic refreshed card"


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [None, {}, {"sub": ""}, {"sub": "legacy"}, {"sub": FOREIGN}, {"sub": 1}])
async def test_connection_publication_refuses_absent_or_changed_socket_owner_before_lookup(current):
    host, socket, context = runtime()
    ingress = frame(host, context, chat_id=str(uuid4()))
    operation = work(context, ingress)
    if current is None:
        host.ui_sessions.pop(socket)
    else:
        host.ui_sessions[socket] = current
    host._conversation_mutation_chat_id = AsyncMock()
    host.handle_ui_message = AsyncMock()
    with pytest.raises(RuntimeError, match="authenticated owner changed"):
        await host._run_connection_ui_operation(context, operation)
    host._conversation_mutation_chat_id.assert_not_awaited()
    host.handle_ui_message.assert_not_awaited()
    assert ingress.authenticated_user_id == OWNER


@pytest.mark.asyncio
async def test_unregistered_frame_cannot_adopt_later_authenticated_owner():
    host, socket, context = runtime()
    host.ui_sessions.clear()
    ingress = frame(host, context, chat_id=str(uuid4()))
    assert ingress.authenticated_user_id == "legacy"
    host.ui_sessions[socket] = {"sub": OWNER}
    host._conversation_mutation_chat_id = AsyncMock()
    with pytest.raises(RuntimeError, match="authenticated owner changed"):
        await host._run_connection_ui_operation(context, work(context, ingress))
    host._conversation_mutation_chat_id.assert_not_awaited()


@pytest.mark.parametrize("missing", ["registry", "socket"])
def test_frame_without_session_registry_or_socket_remains_unauthenticated(missing):
    host, _socket, context = runtime()
    if missing == "registry":
        del host.ui_sessions
    else:
        context = SimpleNamespace(connection_generation=context.connection_generation)
    ingress = frame(host, context, chat_id=str(uuid4()))
    assert ingress.authenticated_user_id is None


def test_frame_owner_is_captured_from_session_instead_of_payload():
    host, _socket, context = runtime()
    ingress = frame(host, context, chat_id=str(uuid4()), user_id=FOREIGN, authenticated_user_id=FOREIGN)
    assert ingress.authenticated_user_id == OWNER


@pytest.mark.asyncio
async def test_user_owned_operation_cannot_publish_as_another_authenticated_owner():
    host, _socket, context = runtime()
    ingress = frame(host, context, chat_id=str(uuid4()))
    operation = work(context, ingress, owner=OperationOwner(OwnerScope.USER, FOREIGN, None))
    host._conversation_mutation_chat_id = AsyncMock()
    with pytest.raises(RuntimeError, match="authenticated owner changed"):
        await host._run_connection_ui_operation(context, operation)
    host._conversation_mutation_chat_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_foreign_chat_is_refused_before_handler_or_publication(database):
    host, socket, context = runtime()
    attach_plane(host, database)
    chat = await asyncio.to_thread(host.history.create_chat, user_id=FOREIGN)
    ingress = frame(host, context, chat_id=chat)
    operation, claim, _ = await asyncio.to_thread(admitted, host, context, ingress)
    host.handle_ui_message = AsyncMock()
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": claim.operation, "owner": operation.owner,
                                               "execution_fence": claim.fence})
    try:
        with pytest.raises(ConversationNotFound):
            await host._run_connection_ui_operation(context, operation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    host.handle_ui_message.assert_not_awaited()
    actual = await asyncio.to_thread(host.history.get_chat, chat, user_id=FOREIGN)
    assert actual["messages"] == [] and socket.frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action,payload", [
    ("delete_saved_component", {"component_id": "own-component"}),
    ("combine_components", {"source_id": "foreign-component", "target_id": "own-component"}),
    ("condense_components", {"component_ids": ["foreign-component", "own-component"]}),
])
async def test_component_inference_uses_authenticated_owner_and_preserves_chat_fence(action, payload):
    host, _socket, context = runtime()
    chat = str(uuid4())
    ingress = frame(host, context, action=action, **payload)
    operation = work(context, ingress)
    lookups = []

    def lookup(identity, *, user_id):
        lookups.append((identity, user_id))
        return {"chat_id": chat} if identity == "own-component" and user_id == OWNER else None

    host.history = SimpleNamespace(get_component_by_id=lookup)
    host._begin_conversation_publication = AsyncMock(return_value=(None, None, None))
    host.handle_ui_message = AsyncMock()
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": SimpleNamespace(chat_id=chat)})
    try:
        with pytest.raises(RuntimeError, match="lacks publication authority"):
            await host._run_connection_ui_operation(context, operation)
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    assert lookups and all(owner == OWNER for _identity, owner in lookups)
    assert host._begin_conversation_publication.await_args.kwargs["user_id"] == OWNER
    host.handle_ui_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_inferred_component_cannot_rebind_an_unbound_operation():
    host, _socket, context = runtime()
    ingress = frame(host, context, action="delete_saved_component", component_id="own-component")
    host.history = SimpleNamespace(get_component_by_id=lambda *_args, **_kwargs: {"chat_id": str(uuid4())})
    host._begin_conversation_publication = AsyncMock()
    token = _CONNECTION_OPERATION_CONTEXT.set({"operation": SimpleNamespace(chat_id=None)})
    try:
        with pytest.raises(RuntimeError, match="operation is not chat-scoped"):
            await host._run_connection_ui_operation(context, work(context, ingress))
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    host._begin_conversation_publication.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_component_is_not_inferred_and_changed_owner_cannot_lookup_it():
    host, socket, context = runtime()
    ingress = frame(host, context, action="delete_saved_component", component_id="foreign-component")
    lookups = []
    host.history = SimpleNamespace(get_component_by_id=lambda identity, **kwargs: lookups.append((identity, kwargs["user_id"])))
    host.handle_ui_message = AsyncMock()
    operation = work(context, ingress)
    await host._run_connection_ui_operation(context, operation)
    assert lookups == [("foreign-component", OWNER)]
    host.handle_ui_message.assert_awaited_once()
    host.ui_sessions[socket] = {"sub": FOREIGN}
    lookups.clear()
    with pytest.raises(RuntimeError, match="authenticated owner changed"):
        await host._run_connection_ui_operation(context, replace(operation))
    assert lookups == []
