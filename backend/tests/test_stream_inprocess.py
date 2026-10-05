"""Tests for push-streaming to in-process built-in agents in
orchestrator/orchestrator.py: local agents are routed through a LoopbackSocket into
handle_agent_message, and cancels reach the agent directly.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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

    async def handle_mcp_request(self, ws, msg):
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
    await asyncio.sleep(0.1)
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
    await asyncio.sleep(0.15)
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


@pytest.mark.parametrize("flag, interval", [(False, 5.0), (True, None)])
async def test_progress_is_not_persisted_without_the_flag_or_the_tool_opting_in(ongoing, flag, interval):
    orch, ws, chat_id, user_id, agent = ongoing
    flags._flags["stream_progress"] = flag
    if interval is None:
        orch._streamable_tools[TOOL].pop("persist_progress_s")
    sub = await _start(orch, ws, chat_id, user_id)
    await agent.values.put(1)
    await asyncio.sleep(0.2)
    assert await _persisted_values(orch, chat_id, user_id) == []
    assert sub.progress_persisted_at == 0
    await agent.values.put(None)
    await asyncio.sleep(0.25)
    assert await _persisted_values(orch, chat_id, user_id) == [1]


async def test_cancel_reaches_in_process_agent(env):
    orch, ws, chat_id, user_id, agent = env
    await orch._cancel_stream_request(AGENT, "req-x", "stream-x")
    assert len(agent.cancels) == 1
    assert agent.cancels[0].stream_id == "stream-x"
