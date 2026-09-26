"""Verify scoped device hydration preserves committed views and connection authority.
These tests exercise the real orchestrator handler and ROTE with synthetic conversation storage.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.viewport_hydration import refresh_viewport_snapshot
from orchestrator.work_admission import OperationOwner, OwnerScope
from rote.rote import ROTE
from shared.protocol import UIEvent


CHAT = "11111111-1111-4111-8111-111111111111"
CONNECTION = "22222222-2222-4222-8222-222222222222"
PREVIOUS = "33333333-3333-4333-8333-333333333333"
REQUEST = "44444444-4444-4444-8444-444444444444"
SUBMISSION = "55555555-5555-4555-8555-555555555555"


def message(**changes):
    return UIEvent(
        action="update_device", session_id=CHAT, submission_id=SUBMISSION,
        request_generation=REQUEST, connection_generation=CONNECTION,
        payload={"device": {"device_type": "macos", "viewport_width": 320},
                 "chat_id": CHAT, "base_render_revision": 7, "snapshot_purpose": "hydration", **changes},
    )


def harness():
    host, socket = object.__new__(Orchestrator), object()
    host.ui_sessions = {socket: {"sub": "owner"}}
    host.ui_clients = [socket]
    host._ws_active_chat = {id(socket): CHAT}
    host._ws_timeline_mode = {}
    host._conversation_scopes = {}
    host._connection_contexts = {}
    host.rote = ROTE()
    host.rote.register_device(socket, {"device_type": "macos", "viewport_width": 1440})
    context = SimpleNamespace(registered=True, closing=False, connection_generation=uuid.UUID(CONNECTION),
                              connection_scope_id=uuid.uuid4(), operations={})
    host._connection_contexts[id(socket)] = context
    binding = host._bind_conversation_scope(
        socket, chat_id=CHAT, connection_generation=CONNECTION,
        request_generation=PREVIOUS, purpose="commit", base_render_revision=7,
    )
    binding["snapshot_completed"] = True
    owner = OperationOwner(OwnerScope.CONNECTION, None, context.connection_scope_id)
    operation = SimpleNamespace(operation_id=uuid.uuid4(), chat_id=CHAT,
                                connection_generation=uuid.UUID(CONNECTION), request_generation=uuid.UUID(REQUEST))
    authority = (operation, owner, object())
    host._conversation_authority = lambda *_args: authority
    host.work_admission = SimpleNamespace(fenced_transaction=Mock(side_effect=lambda _fence: nullcontext()))
    canonical = [{"type": "grid", "component_id": "result", "columns": 3,
                  "children": [{"type": "text", "content": "Synthetic result"}]}]

    def build_snapshot(**scope):
        return {"type": "conversation_snapshot", "schema_version": 1, "snapshot_id": str(uuid.uuid4()),
                "chat_id": scope["chat_id"], "connection_generation": scope["connection_generation"],
                "request_generation": scope["request_generation"], "snapshot_purpose": scope["snapshot_purpose"],
                "render_revision": 7, "committed_at": "2026-09-25T23:00:00Z", "transcript": [],
                "canvas": {"components": copy.deepcopy(canonical), "layouts": []}}

    host.conversation_commits = SimpleNamespace(build_snapshot=Mock(side_effect=build_snapshot))
    host.speech_server_available = lambda: False
    host._safe_send = AsyncMock(return_value=True)
    return host, socket, binding, authority


async def refresh(host, socket, binding, authority, event=None):
    await refresh_viewport_snapshot(host, socket, event or message(), "owner",
                                    host.ui_sessions.get(socket), authority, binding)


def frames(host):
    return [json.loads(call.args[1]) for call in host._safe_send.await_args_list]


async def test_handler_delivers_canonical_narrow_then_wide_snapshots_without_revision_mutation(monkeypatch):
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    host, socket, _binding, authority = harness()
    for width, expected in ((320, "container"), (1440, "grid")):
        request = str(uuid.uuid4())
        authority[0].request_generation = uuid.UUID(request)
        event = message(device={"device_type": "macos", "viewport_width": width})
        event.request_generation = request
        await host.handle_ui_message(socket, event.to_json())
        ack, snapshot = frames(host)[-2:]
        assert ack["type"] == "rote_config" and ack["viewport_snapshot_supported"] is True
        assert (ack["chat_id"], ack["connection_generation"], ack["request_generation"]) == (CHAT, CONNECTION, request)
        assert snapshot["type"] == "conversation_snapshot"
        assert snapshot["canvas"]["components"][0]["type"] == expected
        assert snapshot["render_revision"] == 7 and snapshot["request_generation"] == request
        assert host._conversation_scopes[id(socket)]["snapshot_completed"] is True
        assert host._ws_active_chat[id(socket)] == CHAT
    assert host.conversation_commits.build_snapshot.call_count == 2
    assert not any(frame["type"].startswith("ui_") for frame in frames(host))
    assert host.rote.get_cached_components(socket)[0]["columns"] == 3


@pytest.mark.parametrize("change", [
    "owner", "registration", "connection", "context", "closed", "unregistered", "chat", "scope",
    "pending_commit", "pending_hydration", "revision", "old_request", "operation_chat", "operation_connection",
    "operation_request", "owner_scope", "timeline", "other_operation", "registering",
])
async def test_foreign_stale_or_busy_refresh_never_reads_or_replaces_committed_content(change):
    host, socket, binding, authority = harness()
    registration = host.ui_sessions[socket]
    if change == "owner":
        registration["sub"] = "replacement"
    elif change == "registration":
        host.ui_sessions[socket] = {"sub": "owner"}
    elif change == "connection":
        host._connection_contexts[id(socket)].connection_generation = uuid.uuid4()
    elif change == "context":
        host._connection_contexts.clear()
    elif change in {"closed", "unregistered"}:
        setattr(host._connection_contexts[id(socket)], "closing" if change == "closed" else "registered", change == "closed")
    elif change == "chat":
        host._ws_active_chat[id(socket)] = str(uuid.uuid4())
    elif change == "scope":
        host._conversation_scopes[id(socket)] = dict(binding)
    elif change.startswith("pending_"):
        binding.update(snapshot_completed=False, purpose=change.removeprefix("pending_"))
    elif change == "revision":
        binding["base_render_revision"] = 8
    elif change == "old_request":
        binding["request_generation"] = REQUEST
    elif change.startswith("operation_"):
        setattr(authority[0], {"operation_chat": "chat_id", "operation_connection": "connection_generation",
                              "operation_request": "request_generation"}[change], uuid.uuid4())
    elif change == "owner_scope":
        authority = (authority[0], OperationOwner(OwnerScope.CONNECTION, None, uuid.uuid4()), authority[2])
    elif change == "timeline":
        host._ws_timeline_mode[id(socket)] = True
    elif change == "other_operation":
        host._connection_contexts[id(socket)].operations[uuid.uuid4()] = SimpleNamespace(lane_complete=None)
    elif change == "registering":
        host._connection_contexts[id(socket)].work_registrations_pending = 1
    before = host._conversation_scopes.copy()
    await refresh_viewport_snapshot(host, socket, message(), "owner", registration, authority, binding)
    host.conversation_commits.build_snapshot.assert_not_called()
    assert host._conversation_scopes == before
    assert not frames(host) if change in {"owner", "registration"} else frames(host)[0]["code"] == "viewport_snapshot_rejected"


@pytest.mark.parametrize("changes", [{"base_render_revision": True}, {"base_render_revision": -1},
                                     {"base_render_revision": "7"}, {"chat_id": "invalid"},
                                     {"snapshot_purpose": "commit"}])
async def test_malformed_refresh_is_correlated_and_does_not_fall_back_to_transients(changes):
    host, socket, binding, authority = harness()
    await refresh(host, socket, binding, authority, message(**changes))
    error = frames(host)[0]
    assert error["type"] == "error" and error["code"] == "viewport_snapshot_rejected"
    assert error["request_generation"] == REQUEST and error["connection_generation"] == CONNECTION
    assert error["retryable"] is False
    host.conversation_commits.build_snapshot.assert_not_called()


@pytest.mark.parametrize("change", ["owner", "registration", "chat", "scope", "connection"])
async def test_changed_authority_during_storage_read_never_contaminates_rote_cache(change):
    host, socket, binding, authority = harness()
    cached = [{"type": "text", "content": "Committed"}]
    host.rote.adapt(socket, cached)
    original = host.conversation_commits.build_snapshot.side_effect

    def replacement(**scope):
        if change == "owner":
            host.ui_sessions[socket]["sub"] = "replacement"
        elif change == "registration":
            host.ui_sessions[socket] = {"sub": "owner"}
        elif change == "chat":
            host._ws_active_chat[id(socket)] = str(uuid.uuid4())
        elif change == "scope":
            host._conversation_scopes[id(socket)] = dict(binding)
        else:
            host._connection_contexts[id(socket)].connection_generation = uuid.uuid4()
        return original(**scope)

    host.conversation_commits.build_snapshot.side_effect = replacement
    await refresh(host, socket, binding, authority)
    assert host.rote.get_cached_components(socket) == cached
    assert not any(frame["type"] == "conversation_snapshot" for frame in frames(host))


@pytest.mark.parametrize("failure", ["storage", "fence", "adaptation", "send"])
async def test_failure_retains_previous_scope_and_reports_storage_failures(failure):
    host, socket, binding, authority = harness()
    host.rote.adapt(socket, [{"type": "text", "content": "Previous committed result"}])
    previous = host.rote.get_cached_components(socket)
    if failure == "storage":
        host.conversation_commits.build_snapshot.side_effect = RuntimeError("synthetic failure")
    elif failure == "fence":
        host.work_admission.fenced_transaction.side_effect = RuntimeError("stale fence")
    elif failure == "adaptation":
        host._adapt_conversation_snapshot = Mock(side_effect=RuntimeError("synthetic adaptation failure"))
    else:
        host._safe_send.return_value = False
    await refresh(host, socket, binding, authority)
    assert host._conversation_scopes[id(socket)] is binding
    assert host.rote.get_cached_components(socket) == previous
    if failure != "send":
        assert frames(host)[0]["code"] == "viewport_snapshot_retryable"
        assert frames(host)[0]["retryable"] is True


async def test_initial_hydration_marks_only_successfully_delivered_current_scope_complete():
    host, socket, _binding, _authority = harness()
    for delivered in (False, True):
        host._safe_send.return_value = delivered
        await host._emit_hydration_snapshot(
            socket, chat_id=CHAT, user_id="owner", connection_generation=CONNECTION, request_generation=REQUEST,
        )
        assert host._conversation_scopes[id(socket)]["snapshot_completed"] is delivered


async def test_other_completed_operation_does_not_block_refresh():
    host, socket, binding, authority = harness()
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    host._connection_contexts[id(socket)].operations[uuid.uuid4()] = SimpleNamespace(lane_complete=done)
    await refresh(host, socket, binding, authority)
    assert frames(host)[0]["type"] == "conversation_snapshot"


async def test_scope_replaced_while_profile_ack_is_suspended_is_not_stolen(monkeypatch):
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    host, socket, binding, _authority = harness()
    entered, resume = asyncio.Event(), asyncio.Event()
    sent = []

    async def send(_socket, encoded):
        sent.append(json.loads(encoded))
        if sent[-1]["type"] == "rote_config":
            entered.set()
            await resume.wait()
        return True

    host._safe_send = send
    task = asyncio.create_task(host.handle_ui_message(socket, message().to_json()))
    await asyncio.wait_for(entered.wait(), 2)
    replacement = dict(binding, request_generation=str(uuid.uuid4()))
    host._conversation_scopes[id(socket)] = replacement
    resume.set()
    await asyncio.wait_for(task, 2)
    assert host._conversation_scopes[id(socket)] is replacement
    assert [frame["type"] for frame in sent] == ["rote_config", "error"]
    host.conversation_commits.build_snapshot.assert_not_called()


async def test_scope_replaced_during_delivery_is_not_marked_complete():
    host, socket, binding, authority = harness()
    replacement = dict(binding, snapshot_completed=False)

    async def send(_socket, _encoded):
        host._conversation_scopes[id(socket)] = replacement
        return True

    host._safe_send = send
    await refresh(host, socket, binding, authority)
    assert host._conversation_scopes[id(socket)] is replacement
    assert replacement["snapshot_completed"] is False


async def test_request_retired_during_fence_check_does_not_read_snapshot():
    host, socket, binding, authority = harness()

    def replaced(_fence):
        host._conversation_scopes[id(socket)] = dict(binding)
        return nullcontext()

    host.work_admission.fenced_transaction.side_effect = replaced
    await refresh(host, socket, binding, authority)
    host.conversation_commits.build_snapshot.assert_not_called()
    assert frames(host)[0]["code"] == "viewport_snapshot_rejected"


@pytest.mark.parametrize("failure", [False, True])
async def test_changed_owner_during_send_preserves_replacement_cache_and_drops_old_scope(failure):
    host, socket, binding, authority = harness()
    replacement_cache = [{"type": "text", "content": "Replacement owner"}]

    async def send(_socket, _encoded):
        host.ui_sessions[socket] = {"sub": "replacement"}
        host.rote.adapt(socket, replacement_cache)
        return not failure

    host._safe_send = send
    await refresh(host, socket, binding, authority)
    assert id(socket) not in host._conversation_scopes
    assert host.rote.get_cached_components(socket) == replacement_cache


async def test_changed_owner_during_send_does_not_mutate_existing_cache():
    host, socket, binding, authority = harness()
    host.rote.adapt(socket, [{"type": "text", "content": "Previous owner"}])
    previous = host.rote.get_cached_components(socket)

    async def send(_socket, _encoded):
        host.ui_sessions[socket] = {"sub": "replacement"}
        return True

    host._safe_send = send
    await refresh(host, socket, binding, authority)
    assert host.rote.get_cached_components(socket) == previous


@pytest.mark.parametrize("revision", [6, 8])
async def test_layout_refresh_never_replaces_a_different_semantic_revision(revision):
    host, socket, binding, authority = harness()
    host.rote.adapt(socket, [{"type": "text", "content": "Previous committed result"}])
    previous = host.rote.get_cached_components(socket)
    snapshot = host.conversation_commits.build_snapshot(
        chat_id=CHAT, connection_generation=CONNECTION, request_generation=REQUEST, snapshot_purpose="hydration",
    )
    snapshot["render_revision"] = revision
    host.conversation_commits.build_snapshot = Mock(return_value=snapshot)
    await refresh(host, socket, binding, authority)
    error = frames(host)[0]
    assert error["type"] == "error" and error["retryable"] is (revision > 7)
    assert error["code"] == ("viewport_snapshot_retryable" if revision > 7 else "viewport_snapshot_rejected")
    assert host._conversation_scopes[id(socket)] is binding
    assert host.rote.get_cached_components(socket) == previous


async def test_cancelled_delivery_restores_prior_scope_without_writing_cache():
    host, socket, binding, authority = harness()
    entered = asyncio.Event()

    async def send(_socket, _encoded):
        entered.set()
        await asyncio.Event().wait()

    host._safe_send = send
    task = asyncio.create_task(refresh(host, socket, binding, authority))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host._conversation_scopes[id(socket)] is binding
    assert host.rote.get_cached_components(socket) is None
