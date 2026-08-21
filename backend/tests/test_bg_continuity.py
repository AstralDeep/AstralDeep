"""055 — cross-device background-task continuity (FF_BG_CONTINUITY).

A long-running job started on ONE device surfaces on every other connected
device, pushed as it happens: task_started/task_completed fan to all the
user's sockets (completion works with the originator gone), a background
(VirtualWebSocket) turn's chat-rail narrative + terminal chat_status mirror
to real sockets on the chat, register_ui with a session_id resumes the chat
context and replays task state, completed-but-unnotified tasks replay once,
and the scheduled fallback chat is created before the turn so its output is
not silently dropped. Flag off restores originator-only frames
byte-identically. Requires the docker-compose Postgres; skipped where
unreachable.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.plane_repository_context import (  # noqa: E402
    PlaneRepositoryContext,
    plane_source_from_orchestrator,
)
from shared.feature_flags import flags  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bg_flag():
    prior = flags._flags.get("bg_continuity")
    flags._flags["bg_continuity"] = True
    yield
    flags._flags["bg_continuity"] = prior


@pytest.fixture
async def orch(bg_flag, monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.orchestrator import Orchestrator

    try:
        o = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    try:
        yield o
    finally:
        await asyncio.wait_for(o._close_started_services(), timeout=15.0)


class _CaptureSocket:
    """Frame-capture device double. Deliberately NOT a VirtualWebSocket: the
    production fan skips vws instances (they are background turns, not
    devices — a turn's own vws counting as "notified" would suppress the
    register_ui catch-up replay). Exposes the same .task.outputs read surface
    the assertions use."""

    def __init__(self):
        from types import SimpleNamespace
        self.task = SimpleNamespace(outputs=[])

    async def send_text(self, text):
        try:
            self.task.outputs.append(json.loads(text))
        except (ValueError, TypeError):
            pass

    async def send_json(self, obj):
        self.task.outputs.append(obj if isinstance(obj, dict) else json.loads(obj))


def _capture_socket(orch, user_id):
    ws = _CaptureSocket()
    orch.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    orch.ui_clients.append(ws)
    orch.rote.register_device(ws, {})
    return ws


def _frames(ws, ftype):
    return [f for f in ws.task.outputs if f.get("type") == ftype]


def _isolated_mock_identity():
    user_id = f"bgc-auth-{uuid.uuid4().hex}"
    claims = {
        "sub": user_id,
        "preferred_username": user_id,
        "email": f"{user_id}@invalid.example",
        "realm_access": {"roles": ["admin", "user"]},
        "resource_access": {
            "astral-frontend": {"roles": ["admin", "user"]}
        },
    }
    payload = base64.b64encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return user_id, f"mock.{payload}.signature"


async def _await_manager_tasks(orch):
    tasks = [t.asyncio_task for t in orch.async_task_manager._tasks.values()
             if t.asyncio_task]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    while writes := tuple(orch.async_task_manager._compatibility_write_tasks):
        await asyncio.gather(*writes, return_exceptions=True)


async def _handle_ui_message_and_join_background(orch, websocket, message):
    """Join register_ui's deliberately off-critical-path profile/audit writes."""
    spawned = []
    create_task = asyncio.create_task

    def _capture_register_task(coroutine, *args, **kwargs):
        task = create_task(coroutine, *args, **kwargs)
        code = getattr(coroutine, "cr_code", None)
        qualname = getattr(code, "co_qualname", "")
        if qualname == "to_thread" or qualname.endswith("._record_register_audit"):
            spawned.append(task)
        return task

    try:
        with patch.object(asyncio, "create_task", _capture_register_task):
            return await orch.handle_ui_message(websocket, message)
    finally:
        if spawned:
            await asyncio.wait_for(
                asyncio.gather(*spawned, return_exceptions=True),
                timeout=5.0,
            )


async def _background_task_record(orch, *, user_id, task_id):
    source = plane_source_from_orchestrator(orch)
    context = PlaneRepositoryContext(
        repository=source.plane_repositories.background_tasks,
        plane_runtime=source.plane_runtime,
    )
    return await context.call_async(
        context.repository.get,
        owner_id=user_id,
        task_id=task_id,
    )


async def _create_completed_background_task(
    orch,
    *,
    task_id,
    user_id,
    chat_id,
    title,
    summary,
):
    from astralplane.repositories.background_tasks import (
        BackgroundTaskRecord,
        BackgroundTaskStatus,
    )

    source = plane_source_from_orchestrator(orch)
    context = PlaneRepositoryContext(
        repository=source.plane_repositories.background_tasks,
        plane_runtime=source.plane_runtime,
    )
    completed_at = datetime.now(UTC)
    record = BackgroundTaskRecord(
        task_id=task_id,
        owner_id=user_id,
        conversation_id=chat_id,
        kind="async_chat",
        status=BackgroundTaskStatus.COMPLETED,
        title=title,
        summary=summary,
        created_at=completed_at,
        completed_at=completed_at,
    )
    return await context.call_async(context.repository.create, record=record)


async def _clear_owner_task_replays(orch, user_id):
    """Retire replay eligibility; durable task history stays retention-owned."""
    from astralplane.repositories.background_tasks import BackgroundTaskStatus

    source = plane_source_from_orchestrator(orch)
    context = PlaneRepositoryContext(
        repository=source.plane_repositories.background_tasks,
        plane_runtime=source.plane_runtime,
    )

    def _mark_notified(transaction):
        repository = context.repository
        terminal_states = (
            BackgroundTaskStatus.COMPLETED,
            BackgroundTaskStatus.FAILED,
            BackgroundTaskStatus.CANCELLED,
            BackgroundTaskStatus.RETRYABLE,
        )
        for status in terminal_states:
            records = repository.list_for_owner(
                transaction,
                owner_id=user_id,
                status=status,
                limit=1000,
            )
            for record in records:
                if record.notified:
                    continue
                repository.mark_notified(
                    transaction,
                    owner_id=user_id,
                    task_id=record.task_id,
                )
        remaining = tuple(
            record.task_id
            for status in terminal_states
            for record in repository.list_for_owner(
                transaction,
                owner_id=user_id,
                status=status,
                limit=1000,
            )
            if not record.notified
        )
        assert not remaining, (
            f"owner {user_id!r} retained replay-eligible tasks: {remaining!r}"
        )

    await context.call_async(_mark_notified)


async def _cleanup(orch, user_id, chat_ids=()):
    await _clear_owner_task_replays(orch, user_id)
    for cid in chat_ids:
        try:
            await asyncio.to_thread(orch.history.delete_chat, cid, user_id=user_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Items 1+2 — task frames fan to all the user's sockets
# ---------------------------------------------------------------------------

async def test_task_started_fans_to_second_socket(orch):
    user_id = f"bgc-{uuid.uuid4().hex[:8]}"
    ws1, ws2 = _capture_socket(orch, user_id), _capture_socket(orch, user_id)
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=user_id)

    async def fake_handle(websocket, message, chat_id, *args, **kwargs):
        pass

    orch.handle_chat_message = fake_handle
    message = "analyze the quarterly report thoroughly"
    await orch._dispatch_async_chat(ws1, message, chat_id, user_id=user_id)

    for ws in (ws1, ws2):
        started = _frames(ws, "task_started")
        assert len(started) == 1, f"task_started missing on {ws}"
        assert started[0]["payload"]["chat_id"] == chat_id
        assert started[0]["payload"]["title"] == message[:60]
    # processing_async stays originator-only (other devices key off
    # task_started; a bare chat_status has no chat_id to scope it).
    assert _frames(ws1, "chat_status")
    assert not _frames(ws2, "chat_status")

    await _await_manager_tasks(orch)
    await _cleanup(orch, user_id, [chat_id])


async def test_completion_fan_reaches_socket_joined_after_start(orch):
    user_id = f"bgc-{uuid.uuid4().hex[:8]}"
    ws1 = _capture_socket(orch, user_id)
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=user_id)
    hold = asyncio.Event()

    async def fake_handle(websocket, message, chat_id, *args, **kwargs):
        await hold.wait()
        # The narrative the turn would have produced (drives the summary).
        await websocket.send_text(json.dumps({
            "type": "ui_render", "target": "chat",
            "components": [{"type": "text", "content": "Report finished."}],
        }))

    orch.handle_chat_message = fake_handle
    await orch._dispatch_async_chat(ws1, "run the report", chat_id, user_id=user_id)

    # The originator disconnects; a NEW device connects after start.
    del orch.ui_sessions[ws1]
    orch.ui_clients.remove(ws1)
    ws3 = _capture_socket(orch, user_id)

    hold.set()
    await _await_manager_tasks(orch)

    completed = _frames(ws3, "task_completed")
    assert len(completed) == 1, "completion must reach a late-joined socket"
    payload = completed[0]["payload"]
    assert payload["chat_id"] == chat_id
    assert payload["status"] == "completed"
    assert payload["summary"] == "Report finished."

    row = await _background_task_record(
        orch,
        user_id=user_id,
        task_id=payload["task_id"],
    )
    assert row is not None
    assert row.status.value == "completed"
    assert row.summary == "Report finished."
    assert row.notified is True

    await _cleanup(orch, user_id, [chat_id])


# ---------------------------------------------------------------------------
# Item 3 — VirtualWebSocket turns fan narrative + terminal status
# ---------------------------------------------------------------------------

async def test_vws_narrative_and_done_reach_chat_socket(orch):
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    user_id = f"bgc-{uuid.uuid4().hex[:8]}"
    ws2 = _capture_socket(orch, user_id)
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=user_id)
    orch._ws_active_chat[id(ws2)] = chat_id
    vws = VirtualWebSocket(BackgroundTask(
        task_id="bgturn01", chat_id=chat_id, user_id=user_id))

    await orch.send_ui_render(
        vws, [{"type": "text", "content": "All done, here is the answer."}],
        target="chat")
    chat_renders = [f for f in _frames(ws2, "ui_render") if f.get("target") == "chat"]
    assert len(chat_renders) == 1, "chat narrative must mirror to the real socket"
    assert chat_renders[0]["components"][0]["content"] == "All done, here is the answer."

    # Canvas renders do NOT fan here (the workspace upsert path owns those).
    await orch.send_ui_render(
        vws, [{"type": "metric", "title": "M", "value": 1}], target="canvas")
    assert [f for f in _frames(ws2, "ui_render") if f.get("target") != "chat"] == []

    await orch._send_chat_status(vws, "done")
    done = [f for f in _frames(ws2, "chat_status") if f.get("status") == "done"]
    assert len(done) == 1, "terminal chat_status must mirror to the real socket"
    # The vws itself still captured its own copy (originator delivery intact).
    assert [f for f in vws.task.outputs
            if f.get("type") == "chat_status" and f.get("status") == "done"]

    await _cleanup(orch, user_id, [chat_id])


# ---------------------------------------------------------------------------
# Item 4 — register_ui session resume (+ item 5 in-flight replay)
# ---------------------------------------------------------------------------

async def test_register_ui_session_resume_replays_in_flight_task(orch):
    user_id, token = _isolated_mock_identity()
    await _cleanup(orch, user_id)
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=user_id)
    hold = asyncio.Event()

    async def slow(vws, **kw):
        await hold.wait()

    bg = await orch.async_task_manager.submit(
        chat_id, user_id, slow, title="slow analysis")

    ws = _capture_socket(orch, user_id)
    orch._registered_events[id(ws)] = asyncio.Event()
    await _handle_ui_message_and_join_background(orch, ws, json.dumps({
        "type": "register_ui", "token": token, "device": {},
        "session_id": chat_id}))

    assert orch._ws_active_chat.get(id(ws)) == chat_id, \
        "session_id must resume the chat context"
    statuses = [f for f in _frames(ws, "chat_status")
                if f.get("status") == "processing_async"]
    assert statuses, "joining device must see the running state"
    replays = [f for f in _frames(ws, "task_started")
               if f["payload"].get("task_id") == bg.task_id]
    assert replays and replays[0]["payload"]["replay"] is True
    assert replays[0]["payload"]["title"] == "slow analysis"

    # Foreign/invalid session_id: ignored silently, registration succeeds.
    other_user = f"someone-else-{uuid.uuid4().hex[:6]}"
    other_chat = await asyncio.to_thread(
        orch.history.create_chat, user_id=other_user)
    ws2 = _capture_socket(orch, user_id)
    orch._registered_events[id(ws2)] = asyncio.Event()
    await _handle_ui_message_and_join_background(orch, ws2, json.dumps({
        "type": "register_ui", "token": token, "device": {},
        "session_id": other_chat}))
    assert orch._ws_active_chat.get(id(ws2)) is None
    assert _frames(ws2, "rote_config"), "register must still succeed"

    hold.set()
    await _await_manager_tasks(orch)
    await _cleanup(orch, user_id, [chat_id])
    await _cleanup(orch, other_user, [other_chat])


# ---------------------------------------------------------------------------
# Item 5 — completed-but-unnotified replay marks notified
# ---------------------------------------------------------------------------

async def test_completed_unnotified_replay_marks_notified(orch):
    user_id, token = _isolated_mock_identity()
    await _cleanup(orch, user_id)
    task_id = uuid.uuid4().hex[:8]
    await _create_completed_background_task(
        orch,
        task_id=task_id,
        user_id=user_id,
        chat_id="chat-x",
        title="old job",
        summary="Finished while you were away.",
    )

    ws = _capture_socket(orch, user_id)
    orch._registered_events[id(ws)] = asyncio.Event()
    await _handle_ui_message_and_join_background(
        orch,
        ws,
        json.dumps({
            "type": "register_ui", "token": token, "device": {}
        }),
    )

    replays = [f for f in _frames(ws, "task_completed")
               if f["payload"].get("task_id") == task_id]
    assert len(replays) == 1
    assert replays[0]["payload"]["summary"] == "Finished while you were away."
    assert replays[0]["payload"]["replay"] is True

    row = await _background_task_record(
        orch,
        user_id=user_id,
        task_id=task_id,
    )
    assert row is not None and row.notified is True

    # A second registration replays nothing (notified sticks).
    ws2 = _capture_socket(orch, user_id)
    orch._registered_events[id(ws2)] = asyncio.Event()
    await _handle_ui_message_and_join_background(
        orch,
        ws2,
        json.dumps({
            "type": "register_ui", "token": token, "device": {}
        }),
    )
    assert [f for f in _frames(ws2, "task_completed")
            if f["payload"].get("task_id") == task_id] == []

    await _cleanup(orch, user_id)


# ---------------------------------------------------------------------------
# Kill switch — flag off restores originator-only frames byte-identically
# ---------------------------------------------------------------------------

async def test_flag_off_all_new_sends_absent(orch):
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    flags._flags["bg_continuity"] = False  # bg_flag fixture restores it
    user_id = f"bgc-{uuid.uuid4().hex[:8]}"
    ws1, ws2 = _capture_socket(orch, user_id), _capture_socket(orch, user_id)
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=user_id)
    orch._ws_active_chat[id(ws2)] = chat_id

    async def fake_handle(websocket, message, chat_id, *args, **kwargs):
        pass

    orch.handle_chat_message = fake_handle
    await orch._dispatch_async_chat(ws1, "legacy behavior", chat_id, user_id=user_id)
    await _await_manager_tasks(orch)

    # Originator frames: pre-055 shapes exactly (no title, no summary).
    started = _frames(ws1, "task_started")
    assert len(started) == 1
    assert list(started[0]["payload"].keys()) == ["task_id", "chat_id", "status"]
    completed = _frames(ws1, "task_completed")
    assert len(completed) == 1, "watcher notification must still arrive (item 1 fix)"
    assert list(completed[0]["payload"].keys()) == [
        "task_id", "chat_id", "status", "completed_at"]

    # The second socket sees nothing at all.
    assert ws2.task.outputs == []

    # No durable record with the flag off.
    row = await _background_task_record(
        orch,
        user_id=user_id,
        task_id=started[0]["payload"]["task_id"],
    )
    assert row is None

    # VirtualWebSocket turn frames stay captured-only.
    vws = VirtualWebSocket(BackgroundTask(
        task_id="bgoff01", chat_id=chat_id, user_id=user_id))
    await orch.send_ui_render(vws, [{"type": "text", "content": "hi"}], target="chat")
    await orch._send_chat_status(vws, "done")
    assert ws2.task.outputs == []
    assert [f for f in vws.task.outputs
            if f.get("type") == "chat_status" and f.get("status") == "done"] == \
        [{"type": "chat_status", "status": "done", "message": ""}]

    # register_ui ignores session_id with the flag off.
    auth_user, token = _isolated_mock_identity()
    ws3 = _capture_socket(orch, auth_user)
    orch._registered_events[id(ws3)] = asyncio.Event()
    await _handle_ui_message_and_join_background(orch, ws3, json.dumps({
        "type": "register_ui", "token": token, "device": {},
        "session_id": chat_id}))
    assert orch._ws_active_chat.get(id(ws3)) is None

    await _cleanup(orch, user_id, [chat_id])


# ---------------------------------------------------------------------------
# Item 6 — scheduled fallback chat exists before the turn runs
# ---------------------------------------------------------------------------

async def test_scheduled_fallback_chat_created(orch, monkeypatch):
    user_id = f"bgc-sched-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(orch._llm_store, "get_system",
                        AsyncMock(return_value=object()))

    async def fake_handle(websocket, message, chat_id, **kwargs):
        pass

    monkeypatch.setattr(orch, "handle_chat_message", fake_handle)
    await orch.run_scheduled_turn(
        user_id=user_id, chat_id=None, instruction="daily digest",
        agent_id=None, access_token="tok", allowed_scopes=[],
        correlation_id="bgc-corr-1")

    fallback = f"scheduled-{user_id}"
    row = await asyncio.to_thread(
        orch.history.get_conversation_record,
        fallback,
        user_id=user_id,
    )
    assert row is not None, "fallback chat must exist so history writes persist"

    # Flag off: pre-055 behavior (no chat created).
    flags._flags["bg_continuity"] = False
    off_user = f"bgc-sched-{uuid.uuid4().hex[:8]}"
    await orch.run_scheduled_turn(
        user_id=off_user, chat_id=None, instruction="daily digest",
        agent_id=None, access_token="tok", allowed_scopes=[],
        correlation_id="bgc-corr-2")
    assert await asyncio.to_thread(
        orch.history.get_conversation_record,
        f"scheduled-{off_user}",
        user_id=off_user,
    ) is None

    await _cleanup(orch, user_id, [fallback])


async def test_completion_with_no_real_sockets_stays_unnotified(orch):
    """The turn's own VirtualWebSocket must not count as a notified device —
    else the register_ui catch-up replay never fires for a user who started a
    task and closed every client before it finished (found live)."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    user_id = f"bgc-{uuid.uuid4().hex[:8]}"
    task = BackgroundTask(task_id=uuid.uuid4().hex[:8], chat_id="c", user_id=user_id)
    vws = VirtualWebSocket(task)
    orch.ui_sessions[vws] = {"sub": user_id}
    delivered = await orch._send_to_user_sockets(
        user_id, {"type": "task_completed", "payload": {"task_id": task.task_id}})
    assert delivered == 0, "vws must never count as a notified device"
    orch.ui_sessions.pop(vws, None)
