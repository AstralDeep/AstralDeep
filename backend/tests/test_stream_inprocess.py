"""Tests for push-streaming to in-process built-in agents in
orchestrator/orchestrator.py: local agents are routed through a LoopbackSocket into
handle_agent_message, cancels reach the agent directly, and a declared duration is
one deadline that reconnects cannot move.
"""

import asyncio
import os
import sys
import time
import uuid
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.stream_manager import LIFETIME_GRACE_SECONDS, StreamLifetime
from shared.feature_flags import flags
from shared.protocol import ToolStreamData, ToolStreamEnd

pytestmark = pytest.mark.asyncio

AGENT = "fake-local-1"
TOOL = "live_fake_metrics"


class FakeLocalStreamingAgent:
    def __init__(self):
        self.requests = []
        self.cancels = []
        self.release_stream = asyncio.Event()
        self.started = asyncio.Event()
        self.run_task = None

    async def handle_mcp_request(self, ws, msg):
        self.run_task = asyncio.current_task()
        self.started.set()
        self.requests.append(msg)
        await self.release_stream.wait()
        sid = msg.params["_stream_id"]
        chunk = ToolStreamData(
            request_id=msg.request_id, stream_id=sid, agent_id=AGENT,
            tool_name=msg.params["name"], seq=1,
            components=[{"type": "metric", "title": "M", "value": 42, "id": sid}],
        )
        await ws.send_text(chunk.to_json())
        await ws.send_text(
            ToolStreamEnd(request_id=msg.request_id, stream_id=sid).to_json())

    async def _handle_stream_cancel(self, msg):
        self.cancels.append(msg)


@pytest.fixture
def push_flags():
    prior = {k: flags._flags[k] for k in ("tool_streaming", "stream_artifacts")}
    flags._flags["tool_streaming"] = True
    flags._flags["stream_artifacts"] = True
    yield
    flags._flags.update(prior)


@pytest.fixture
async def env(push_flags):
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    user_id = f"inproc-stream-{uuid.uuid4().hex[:8]}"
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
    ws = VirtualWebSocket(task)
    orch.ui_sessions[ws] = {"sub": user_id}
    orch.ui_clients.append(ws)
    orch.rote.register_device(ws, {})
    chat_id = await asyncio.to_thread(
        orch.history.create_chat,
        user_id=user_id,
    )
    orch._ws_active_chat[id(ws)] = chat_id
    agent = FakeLocalStreamingAgent()
    orch.local_agents[AGENT] = agent
    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed.return_value = True
    orch._streamable_tools[TOOL] = {
        "agent_id": AGENT, "kind": "push", "max_fps": 30, "min_fps": 5,
        "max_chunk_bytes": 65536, "default_interval": 2,
        "min_interval": 1, "max_interval": 30,
    }
    try:
        yield orch, ws, chat_id, user_id, agent
    finally:
        try:
            if agent.run_task is not None:
                agent.release_stream.set()
                await asyncio.wait_for(asyncio.shield(agent.run_task), 30)
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


def _frames(ws, ftype):
    return [f for f in ws.task.outputs if f.get("type") == ftype]


async def test_subscribe_dispatches_in_process_and_streams(env):
    orch, ws, chat_id, user_id, agent = env
    stream_id, attached = await orch.stream_manager.subscribe(
        ws=ws, user_id=user_id, chat_id=chat_id,
        tool_name=TOOL, agent_id=AGENT, params={"interval_s": 1},
        tool_metadata=orch._streamable_tools[TOOL],
    )
    agent.release_stream.set()
    await asyncio.wait_for(agent.started.wait(), 30)
    await asyncio.wait_for(asyncio.shield(agent.run_task), 30)
    assert agent.requests, "in-process agent never received the stream request"
    assert agent.requests[0].params["_stream"] is True
    data = _frames(ws, "ui_stream_data")
    assert any(f.get("components") for f in data), f"no content frame arrived: {data}"
    assert any(f.get("component_id") for f in data), "bridged identity missing"


async def test_terminal_persists_via_loopback(env):
    orch, ws, chat_id, user_id, agent = env
    await orch.stream_manager.subscribe(
        ws=ws, user_id=user_id, chat_id=chat_id,
        tool_name=TOOL, agent_id=AGENT, params={"interval_s": 2},
        tool_metadata=orch._streamable_tools[TOOL],
    )
    agent.release_stream.set()
    await asyncio.wait_for(agent.started.wait(), 30)
    await asyncio.wait_for(asyncio.shield(agent.run_task), 30)
    live = await asyncio.to_thread(orch.workspace.live_components, chat_id, user_id)
    persisted = [c for c in live if c.get("type") == "metric"]
    assert persisted, f"streamed content not persisted on agent_end: {live}"
    assert persisted[0]["_source_agent"] == AGENT
    assert persisted[0]["component_id"].startswith("wc_")


class FakeOngoingStreamingAgent:
    def __init__(self):
        self.values = asyncio.Queue()
        self.finish = asyncio.Event()
        self.cancels = []

    async def handle_mcp_request(self, ws, msg):
        sid = msg.params["_stream_id"]
        seq = 0
        while not self.finish.is_set():
            value = await self.values.get()
            if value is None:
                break
            seq += 1
            await ws.send_text(ToolStreamData(
                request_id=msg.request_id, stream_id=sid, agent_id=AGENT, tool_name=msg.params["name"], seq=seq,
                components=[{"type": "metric", "title": "M", "value": value, "id": sid}],
            ).to_json())
        await ws.send_text(ToolStreamEnd(request_id=msg.request_id, stream_id=sid).to_json())

    async def _handle_stream_cancel(self, msg):
        self.cancels.append(msg)


@pytest.fixture
def ongoing(env):
    orch, ws, chat_id, user_id, _ = env
    agent = FakeOngoingStreamingAgent()
    orch.local_agents[AGENT] = agent
    prior = flags._flags.get("stream_progress", False)
    flags._flags["stream_progress"] = True
    orch._streamable_tools[TOOL]["persist_progress_s"] = 5.0
    orch._streamable_tools[TOOL]["lifetime"] = StreamLifetime("minutes", 60.0, 5.0, 1.0, 10.0)
    yield orch, ws, chat_id, user_id, agent
    flags._flags["stream_progress"] = prior


async def _persisted_values(orch, chat_id, user_id):
    live = await asyncio.to_thread(orch.workspace.live_components, chat_id, user_id)
    return [component["value"] for component in live if component.get("type") == "metric"]


async def _start(orch, ws, chat_id, user_id):
    stream_id, _ = await orch.stream_manager.subscribe(
        ws=ws, user_id=user_id, chat_id=chat_id, tool_name=TOOL, agent_id=AGENT, params={"interval_s": 3},
        tool_metadata=orch._streamable_tools[TOOL],
    )
    return orch.stream_manager.subscription_for_stream(stream_id)


async def test_progress_persists_while_the_stream_is_still_running(ongoing):
    orch, ws, chat_id, user_id, agent = ongoing
    sub = await _start(orch, ws, chat_id, user_id)
    await agent.values.put(1)
    await asyncio.sleep(0.2)
    assert await _persisted_values(orch, chat_id, user_id) == [1]
    assert sub.persist_done is False and sub.progress_persisted_at > 0
    await agent.values.put(2)
    await asyncio.sleep(0.2)
    assert await _persisted_values(orch, chat_id, user_id) == [1], "a second save inside the interval must wait"
    sub.progress_persisted_at -= 6
    await agent.values.put(2)
    await asyncio.sleep(0.2)
    assert await _persisted_values(orch, chat_id, user_id) == [2]
    saved_at = sub.progress_persisted_at
    sub.progress_persisted_at -= 6
    await agent.values.put(2)
    await asyncio.sleep(0.2)
    assert sub.progress_persisted_at == saved_at - 6, "unchanged content must not be saved again"
    await agent.values.put(3)
    await agent.values.put(None)
    await asyncio.sleep(0.25)
    assert await _persisted_values(orch, chat_id, user_id) == [3]
    assert sub.persist_done is True


async def test_a_stream_button_reaches_an_idle_socket_through_its_announced_first_save(ongoing):
    orch, ws, chat_id, user_id, agent = ongoing
    connection = str(uuid.uuid4())
    orch._bind_conversation_scope(
        ws, chat_id=chat_id, connection_generation=connection, request_generation=str(uuid.uuid4()),
        purpose="hydration", base_render_revision=0,
    )["snapshot_completed"] = True
    await orch._handle_push_stream_subscribe(ws, chat_id, {"tool_name": TOOL, "params": {"interval_s": 4}}, user_id)
    await agent.values.put(1)
    await asyncio.sleep(0.2)
    ready, snapshot = [
        frame for frame in ws.task.outputs
        if frame.get("type") in ("conversation_commit_ready", "conversation_snapshot")
    ]
    assert ready["type"] == "conversation_commit_ready" and snapshot["type"] == "conversation_snapshot"
    assert ws.task.outputs.index(snapshot) == ws.task.outputs.index(ready) + 1
    assert ready["connection_generation"] == snapshot["connection_generation"] == connection
    assert ready["request_generation"] == snapshot["request_generation"]
    assert ready["render_revision"] == snapshot["render_revision"] == 1
    assert [item["value"] for item in snapshot["canvas"]["components"] if item.get("type") == "metric"] == [1]
    await agent.values.put(None)
    await asyncio.sleep(0.25)


async def test_a_failed_progress_save_is_skipped_and_the_final_state_still_persists(ongoing, monkeypatch):
    orch, ws, chat_id, user_id, agent = ongoing
    sub = await _start(orch, ws, chat_id, user_id)
    save = orch.run_detached_conversation_mutation

    async def refuse(**_):
        raise RuntimeError("conversation is busy")

    monkeypatch.setattr(orch, "run_detached_conversation_mutation", refuse)
    await agent.values.put(1)
    await asyncio.sleep(0.2)
    assert await _persisted_values(orch, chat_id, user_id) == []
    assert sub.persist_done is False
    monkeypatch.setattr(orch, "run_detached_conversation_mutation", save)
    await agent.values.put(None)
    await asyncio.sleep(0.25)
    assert await _persisted_values(orch, chat_id, user_id) == [1]
    assert sub.persist_done is True


@pytest.mark.parametrize("flag, interval, bounded", [(False, 5.0, True), (True, None, True), (True, 5.0, False)])
async def test_progress_is_not_persisted_without_the_flag_the_tool_opting_in_and_a_declared_duration(
    ongoing, flag, interval, bounded,
):
    orch, ws, chat_id, user_id, agent = ongoing
    flags._flags["stream_progress"] = flag
    if interval is None:
        orch._streamable_tools[TOOL].pop("persist_progress_s")
    if not bounded:
        orch._streamable_tools[TOOL].pop("lifetime")
    sub = await _start(orch, ws, chat_id, user_id)
    await agent.values.put(1)
    await asyncio.sleep(0.2)
    assert await _persisted_values(orch, chat_id, user_id) == []
    assert sub.progress_persisted_at == 0
    await agent.values.put(None)
    await asyncio.sleep(0.25)
    assert await _persisted_values(orch, chat_id, user_id) == [1]


class FakeRestartedAgent:
    def __init__(self):
        self.requests = []
        self.cancels = []
        self.emit = asyncio.Queue()

    async def handle_mcp_request(self, ws, msg):
        self.requests.append(msg)
        sid = msg.params["_stream_id"]
        await self.emit.get()
        await ws.send_text(ToolStreamData(
            request_id=msg.request_id, stream_id=sid, agent_id=AGENT, tool_name=msg.params["name"], seq=1,
            components=[{"type": "metric", "title": "M", "value": len(self.requests), "id": sid}],
        ).to_json())

    async def _handle_stream_cancel(self, msg):
        self.cancels.append(msg)


async def test_a_reconnecting_client_cannot_keep_a_stream_past_its_requested_duration(ongoing):
    orch, ws, chat_id, user_id, _ = ongoing
    agent = FakeRestartedAgent()
    orch.local_agents[AGENT] = agent
    sub = await _start(orch, ws, chat_id, user_id)
    await agent.emit.put(None)
    await asyncio.sleep(0.2)
    deadline = sub.expires_at
    assert len(agent.requests) == 1 and await _persisted_values(orch, chat_id, user_id) == [1]

    await orch.stream_manager.detach(ws)
    await orch._resume_chat_streams(ws, user_id, chat_id)
    await agent.emit.put(None)
    await asyncio.sleep(0.2)
    assert len(agent.requests) == 2 and len(agent.cancels) == 1 and sub.expires_at == deadline

    await orch.stream_manager.detach(ws)
    sub.expires_at = time.monotonic() - 1
    acknowledged = len(_frames(ws, "stream_subscribed"))
    await orch._resume_chat_streams(ws, user_id, chat_id)
    await asyncio.sleep(0.2)

    assert len(agent.requests) == 2 and len(_frames(ws, "stream_subscribed")) == acknowledged
    assert not orch.stream_manager._active and not orch.stream_manager._dormant
    assert sub.state_reason == "duration_elapsed" and sub.persist_done is True
    assert await _persisted_values(orch, chat_id, user_id) == [2]


class FakeCancellableAgent:
    def __init__(self):
        self.values = asyncio.Queue()
        self.runs = {}
        self.cancelled = asyncio.Event()

    async def handle_mcp_request(self, ws, msg):
        sid = msg.params["_stream_id"]
        self.runs[sid] = asyncio.current_task()
        seq = 0
        try:
            while True:
                value = await self.values.get()
                seq += 1
                await ws.send_text(ToolStreamData(
                    request_id=msg.request_id, stream_id=sid, agent_id=AGENT, tool_name=msg.params["name"], seq=seq,
                    components=[{"type": "metric", "title": "M", "value": value, "id": sid}],
                ).to_json())
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def _handle_stream_cancel(self, msg):
        self.runs[msg.stream_id].cancel()


async def test_an_overdue_in_process_run_saves_its_last_state_before_it_is_cancelled(ongoing):
    orch, ws, chat_id, user_id, _ = ongoing
    agent = FakeCancellableAgent()
    orch.local_agents[AGENT] = agent
    sub = await _start(orch, ws, chat_id, user_id)
    await agent.values.put(1)
    await agent.values.put(2)
    await asyncio.sleep(0.2)
    assert await _persisted_values(orch, chat_id, user_id) == [1] and sub.persist_done is False

    sub.expires_at = time.monotonic() - LIFETIME_GRACE_SECONDS - 1
    await agent.values.put(3)
    await asyncio.wait_for(agent.cancelled.wait(), 5)

    assert sub.state_reason == "duration_elapsed" and sub.persist_done is True
    assert not orch.stream_manager._active and not orch.stream_manager._dormant
    assert await _persisted_values(orch, chat_id, user_id) == [2]
    assert any(frame.get("terminal") for frame in _frames(ws, "ui_stream_data"))


async def test_cancel_reaches_in_process_agent(env):
    orch, ws, chat_id, user_id, agent = env
    await orch._cancel_stream_request(AGENT, "req-x", "stream-x")
    assert len(agent.cancels) == 1
    assert agent.cancels[0].stream_id == "stream-x"
