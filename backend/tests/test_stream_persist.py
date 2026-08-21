"""055-uniform-artifacts US2 (T020) — stream persist-on-terminal + auto-subscribe.

With FF_STREAM_ARTIFACTS on, the orchestrator subscribes the originating
socket (and co-viewing sockets of the chat) itself at streaming-tool
dispatch, and on stream termination persists the retained last
content-bearing chunk into the workspace under the bridged identity
(source-tagged, snapshotted, audited, fanned as a normal ``ui_upsert``).
An abandoned stream persists an honest failed-state Alert under the SAME
identity. Flag off: no auto-subscribe, no persistence — today's ephemeral
``stream-<id>`` behavior exactly.
"""
from __future__ import annotations

import asyncio
import itertools
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.workspace import fingerprint
from shared.feature_flags import flags
from shared.protocol import ToolStreamData, ToolStreamEnd

pytestmark = pytest.mark.asyncio

AGENT = "weather-stream-test"
TOOL = "live_temperature"
PARAMS = {"latitude": 51.5, "longitude": -0.12}


def _fresh_socket():
    """A VirtualWebSocket capturing every delivered frame."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
    return VirtualWebSocket(task)


def _frames(ws, ftype=None):
    return [f for f in ws.task.outputs if ftype is None or f.get("type") == ftype]


def _install_dispatcher(orch):
    """Replace the agent dispatcher with a recorder (no real agent)."""
    calls = []
    counter = itertools.count(1)

    async def _dispatch(
        agent_id,
        tool_name,
        params,
        stream_id,
        user_id,
        _websocket,
        _chat_id,
    ):
        rid = f"req-{next(counter)}"
        calls.append({"agent_id": agent_id, "tool_name": tool_name,
                      "params": params, "stream_id": stream_id,
                      "request_id": rid})
        return rid

    orch.stream_manager._agent_dispatcher = _dispatch
    return calls


async def _feed_chunk(orch, sub, seq, components, error=None, terminal=False):
    msg = ToolStreamData(
        request_id=sub.request_id, stream_id=sub.stream_id,
        agent_id=sub.agent_id, tool_name=sub.tool_name,
        seq=seq, components=components, error=error, terminal=terminal,
    )
    await orch.handle_agent_message(None, msg.to_json())


async def _feed_end(orch, sub):
    msg = ToolStreamEnd(request_id=sub.request_id, stream_id=sub.stream_id)
    await orch.handle_agent_message(None, msg.to_json())


def _metric(sub, value):
    """A chunk component as the agent SDK ships it (top-level id = stream id)."""
    return {"type": "metric", "title": "Temp", "value": value, "id": sub.stream_id}


@pytest.fixture
def push_flags():
    prior = {k: flags._flags[k] for k in ("tool_streaming", "stream_artifacts")}
    flags._flags["tool_streaming"] = True
    flags._flags["stream_artifacts"] = True
    yield
    flags._flags.update(prior)


@pytest.fixture
def stream_artifacts_off():
    prior = flags._flags["stream_artifacts"]
    flags._flags["stream_artifacts"] = False
    yield
    flags._flags["stream_artifacts"] = prior


@pytest.fixture()
async def env(push_flags):
    """A real Orchestrator + registered socket + fresh chat + fake push tool."""
    from orchestrator.orchestrator import Orchestrator
    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    user_id = f"stream-persist-{uuid.uuid4().hex[:8]}"
    ws = _fresh_socket()
    orch.ui_sessions[ws] = {"sub": user_id}
    orch.ui_clients.append(ws)
    orch.rote.register_device(ws, {})
    chat_id = await asyncio.to_thread(
        orch.history.create_chat,
        user_id=user_id,
    )
    orch._ws_active_chat[id(ws)] = chat_id
    orch._streamable_tools[TOOL] = {
        "agent_id": AGENT, "kind": "push", "max_fps": 30, "min_fps": 5,
        "max_chunk_bytes": 65536, "default_interval": 2,
        "min_interval": 1, "max_interval": 30,
    }
    try:
        yield orch, ws, chat_id, user_id
    finally:
        try:
            orch.stream_manager.shutdown()
        except Exception:
            pass
        try:
            await asyncio.to_thread(
                orch.history.delete_chat,
                chat_id,
                user_id=user_id,
            )
        except Exception:
            pass
        await orch._close_started_services()


async def _auto_subscribed(orch, ws, chat_id, user_id, params=PARAMS):
    """Auto-subscribe and return the live subscription record."""
    await orch._auto_subscribe_stream_artifacts(ws, chat_id, user_id, TOOL, params)
    acks = _frames(ws, "stream_subscribed")
    assert acks, "auto-subscribe must ack with stream_subscribed"
    sub = orch.stream_manager.subscription_for_stream(acks[-1]["stream_id"])
    assert sub is not None
    return sub


# ---------------------------------------------------------------------------
# Auto-subscribe at streaming-tool dispatch
# ---------------------------------------------------------------------------

class TestAutoSubscribe:
    async def test_originating_socket_subscribed_with_bridged_identity(self, env):
        orch, ws, chat_id, user_id = env
        calls = _install_dispatcher(orch)

        sub = await _auto_subscribed(orch, ws, chat_id, user_id)
        expected = fingerprint(AGENT, TOOL, PARAMS)
        assert sub.component_id == expected
        assert ws in sub.subscribers
        assert calls and calls[0]["tool_name"] == TOOL

        ack = _frames(ws, "stream_subscribed")[0]
        assert ack["component_id"] == expected
        assert ack["session_id"] == chat_id
        assert ack["attached"] is False

    async def test_coviewing_sockets_attach_to_same_subscription(self, env):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        ws2 = _fresh_socket()
        orch.ui_sessions[ws2] = {"sub": user_id}
        orch.ui_clients.append(ws2)
        orch.rote.register_device(ws2, {})
        orch._ws_active_chat[id(ws2)] = chat_id

        sub = await _auto_subscribed(orch, ws, chat_id, user_id)
        assert ws in sub.subscribers and ws2 in sub.subscribers

        ack2 = _frames(ws2, "stream_subscribed")[0]
        assert ack2["stream_id"] == sub.stream_id
        assert ack2["attached"] is True
        assert ack2["component_id"] == sub.component_id

    async def test_private_params_do_not_change_identity(self, env):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        dirty = dict(PARAMS, _credentials="enc", _delegation_token="tok")
        sub = await _auto_subscribed(orch, ws, chat_id, user_id, params=dirty)
        assert sub.component_id == fingerprint(AGENT, TOOL, PARAMS)
        assert "_credentials" not in sub.params

    async def test_poll_tools_are_not_auto_subscribed(self, env):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        orch._streamable_tools["cpu_load"] = {
            "agent_id": AGENT, "kind": "poll",
            "default_interval": 2, "min_interval": 1, "max_interval": 30,
        }
        await orch._auto_subscribe_stream_artifacts(
            ws, chat_id, user_id, "cpu_load", {})
        assert not orch.stream_manager._active
        assert not ws.task.outputs

    async def test_flag_off_no_auto_subscribe(self, env, stream_artifacts_off):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        await orch._auto_subscribe_stream_artifacts(
            ws, chat_id, user_id, TOOL, PARAMS)
        assert not orch.stream_manager._active
        assert not ws.task.outputs


# ---------------------------------------------------------------------------
# Persist-on-terminal
# ---------------------------------------------------------------------------

class TestPersistOnTerminal:
    async def test_stream_persists_and_rehydrates(self, env):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        sub = await _auto_subscribed(orch, ws, chat_id, user_id)
        cid = sub.component_id

        await _feed_chunk(orch, sub, 1, [_metric(sub, "12C")])
        await _feed_chunk(orch, sub, 2, [_metric(sub, "13C")])
        await _feed_end(orch, sub)
        await asyncio.sleep(0.05)

        row = await orch.workspace.aget_by_component_id(chat_id, user_id, cid)
        assert row is not None, "terminal state must persist under the bridged identity"
        comp = row["component_data"]
        assert comp["value"] == "13C", "the LAST content-bearing chunk persists"
        assert comp["_source_agent"] == AGENT
        assert comp["_source_tool"] == TOOL
        assert comp["_source_params"] == PARAMS
        assert not str(comp.get("id", "")).startswith("stream-")

        # Reload path: load_chat re-hydrates from live_components.
        live = await orch.workspace.alive_components(chat_id, user_id)
        assert any(c.get("component_id") == cid for c in live)

        # The persist fanned a normal ui_upsert to the chat's sockets.
        upserts = _frames(ws, "ui_upsert")
        assert upserts, "terminal persist must fan a ui_upsert"
        op_cids = {op.get("component_id")
                   for f in upserts for op in f.get("ops", [])}
        assert cid in op_cids

        # And captured a workspace snapshot for the timeline.
        snaps = await orch.workspace.alist_snapshots(chat_id, user_id)
        assert any(s.get("cause") == "stream" for s in snaps)

    async def test_rerun_supersedes_same_identity(self, env):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        sub = await _auto_subscribed(orch, ws, chat_id, user_id)
        cid = sub.component_id
        await _feed_chunk(orch, sub, 1, [_metric(sub, "12C")])
        await _feed_end(orch, sub)

        sub2 = await _auto_subscribed(orch, ws, chat_id, user_id)
        assert sub2.component_id == cid, "same params ⇒ same workspace identity"
        await _feed_chunk(orch, sub2, 1, [_metric(sub2, "99C")])
        await _feed_end(orch, sub2)
        await asyncio.sleep(0.05)

        rows = await orch.workspace.alive_rows(chat_id, user_id)
        matching = [r for r in rows if r.get("component_id") == cid]
        assert len(matching) == 1, "re-run must supersede, never duplicate"
        assert matching[0]["component_data"]["value"] == "99C"

    async def test_abandoned_stream_persists_failed_alert(self, env):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        sub = await _auto_subscribed(orch, ws, chat_id, user_id)
        cid = sub.component_id

        await _feed_chunk(orch, sub, 1, [_metric(sub, "12C")])
        # Terminal-class error (agent died mid-stream) — resolves FAILED.
        await _feed_chunk(orch, sub, 2, [], error={
            "code": "cancelled", "message": "agent connection lost",
        }, terminal=True)
        await asyncio.sleep(0.05)

        row = await orch.workspace.aget_by_component_id(chat_id, user_id, cid)
        assert row is not None, "abandonment must persist an honest state"
        comp = row["component_data"]
        assert comp["type"] == "alert", "the content must NOT persist — the alert does"
        assert comp["variant"] == "error"
        assert TOOL in comp["message"]
        assert comp["_source_agent"] == AGENT
        assert comp["_source_tool"] == TOOL

    async def test_contentless_stream_persists_nothing(self, env):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        sub = await _auto_subscribed(orch, ws, chat_id, user_id)

        await _feed_end(orch, sub)
        await asyncio.sleep(0.05)

        rows = await orch.workspace.alive_rows(chat_id, user_id)
        assert not rows, "no content ever streamed ⇒ nothing to persist"


# ---------------------------------------------------------------------------
# FF_STREAM_ARTIFACTS off = today's behavior
# ---------------------------------------------------------------------------

class TestFlagOff:
    async def test_full_cycle_persists_nothing(self, env, stream_artifacts_off):
        orch, ws, chat_id, user_id = env
        _install_dispatcher(orch)
        # Today's entry point: a client-driven subscribe (auto-subscribe is off).
        stream_id, attached = await orch.stream_manager.subscribe(
            ws=ws, user_id=user_id, chat_id=chat_id,
            tool_name=TOOL, agent_id=AGENT, params=dict(PARAMS),
        )
        assert attached is False
        sub = orch.stream_manager.subscription_for_stream(stream_id)
        assert sub.bridged_component_id is None

        await _feed_chunk(orch, sub, 1, [_metric(sub, "12C")])
        await _feed_end(orch, sub)
        await asyncio.sleep(0.1)

        rows = await orch.workspace.alive_rows(chat_id, user_id)
        assert not rows, "flag off must never persist streamed content"
        snaps = await orch.workspace.alist_snapshots(chat_id, user_id)
        assert not snaps
        assert not _frames(ws, "ui_upsert")
        for frame in _frames(ws, "ui_stream_data"):
            assert "component_id" not in frame
