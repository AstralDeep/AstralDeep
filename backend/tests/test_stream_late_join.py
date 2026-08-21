"""055-uniform-artifacts — late-join: a device opening a chat mid-stream gets
the stream's current component state, not a blank placeholder.

Server half: ``load_chat`` attaches the loading socket to ACTIVE
subscriptions of that (user, chat) — ``resume()`` only covers DORMANT — and
``StreamManager.replay_retained`` re-delivers the retained content chunk to
just the attaching socket (after its ``stream_subscribed`` ack, so clients
key the placeholder first). Flag off keeps load_chat's frames byte-identical
to pre-055. Requires the docker-compose Postgres; skipped where unreachable.
"""
import asyncio
import json
import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.feature_flags import flags
from shared.protocol import ToolStreamData

pytestmark = pytest.mark.asyncio

AGENT = "fake-late-join-1"
TOOL = "live_fake_feed"


class HoldOpenAgent:
    """Accepts the stream dispatch and never ends it — the subscription stays
    ACTIVE while tests inject chunks directly via handle_agent_chunk."""

    def __init__(self):
        self.requests = []
        self.cancels = []

    async def handle_mcp_request(self, ws, msg):
        self.requests.append(msg)

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
    user_id = f"late-join-{uuid.uuid4().hex[:8]}"

    def make_ws():
        task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
        ws = VirtualWebSocket(task)
        orch.ui_sessions[ws] = {"sub": user_id}
        orch.ui_clients.append(ws)
        orch.rote.register_device(ws, {})
        return ws

    ws1, ws2 = make_ws(), make_ws()
    chat_id = await asyncio.to_thread(
        orch.history.create_chat,
        user_id=user_id,
    )
    orch._ws_active_chat[id(ws1)] = chat_id
    agent = HoldOpenAgent()
    orch.local_agents[AGENT] = agent
    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed.return_value = True
    orch._streamable_tools[TOOL] = {
        "agent_id": AGENT, "kind": "push", "max_fps": 30, "min_fps": 5,
        "max_chunk_bytes": 65536, "default_interval": 2,
        "min_interval": 1, "max_interval": 30,
    }
    try:
        yield orch, ws1, ws2, chat_id, user_id
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


async def _subscribe_and_chunk(orch, ws1, chat_id, user_id, seq=1, value=1):
    """Subscribe ws1 and (when seq) inject one content chunk from the agent."""
    stream_id, attached = await orch.stream_manager.subscribe(
        ws=ws1, user_id=user_id, chat_id=chat_id,
        tool_name=TOOL, agent_id=AGENT, params={"interval_s": 1},
        tool_metadata=orch._streamable_tools[TOOL],
    )
    assert attached is False
    if seq:
        await _emit(orch, stream_id, seq, value)
    return stream_id


async def _emit(orch, stream_id, seq, value):
    sub = orch.stream_manager.subscription_for_stream(stream_id)
    await orch.stream_manager.handle_agent_chunk(ToolStreamData(
        request_id=sub.request_id, stream_id=stream_id, agent_id=AGENT,
        tool_name=TOOL, seq=seq,
        components=[{"type": "metric", "title": "Feed", "value": value}],
    ))
    await asyncio.sleep(0.1)


async def _load_chat(orch, ws, chat_id):
    await orch.handle_ui_message(ws, json.dumps({
        "type": "ui_event", "action": "load_chat",
        "payload": {"chat_id": chat_id},
    }))


async def test_load_chat_attaches_and_replays_current_state(env):
    orch, ws1, ws2, chat_id, user_id = env
    stream_id = await _subscribe_and_chunk(orch, ws1, chat_id, user_id)

    await _load_chat(orch, ws2, chat_id)

    acks = [f for f in _frames(ws2, "stream_subscribed")
            if f.get("stream_id") == stream_id]
    assert len(acks) == 1, f"loading socket not attached: {ws2.task.outputs}"
    assert acks[0]["attached"] is True
    assert acks[0]["tool_name"] == TOOL
    assert acks[0]["component_id"].startswith("wc_")

    replays = _frames(ws2, "ui_stream_data")
    assert len(replays) == 1, "exactly one retained-chunk replay expected"
    assert replays[0]["seq"] == 1
    assert replays[0]["components"][0]["value"] == 1
    assert replays[0]["component_id"] == acks[0]["component_id"]
    # Ack precedes the replay so clients key the placeholder first.
    outputs = ws2.task.outputs
    assert outputs.index(acks[0]) < outputs.index(replays[0])

    # The agent run was NOT re-dispatched by the attach.
    agent = orch.local_agents[AGENT]
    assert len(agent.requests) == 1

    # Subsequent live chunks fan out to BOTH sockets.
    await _emit(orch, stream_id, 2, 2)
    for ws in (ws1, ws2):
        seqs = [f["seq"] for f in _frames(ws, "ui_stream_data")]
        assert 2 in seqs, f"live chunk missing on {ws}: {seqs}"


async def test_load_chat_attach_without_retained_chunk_sends_no_replay(env):
    orch, ws1, ws2, chat_id, user_id = env
    stream_id = await _subscribe_and_chunk(
        orch, ws1, chat_id, user_id, seq=0)  # no chunk yet

    await _load_chat(orch, ws2, chat_id)

    acks = [f for f in _frames(ws2, "stream_subscribed")
            if f.get("stream_id") == stream_id]
    assert len(acks) == 1
    assert _frames(ws2, "ui_stream_data") == []


async def test_flag_off_load_chat_does_not_attach(env):
    orch, ws1, ws2, chat_id, user_id = env
    flags._flags["stream_artifacts"] = False  # push_flags restores it
    stream_id = await _subscribe_and_chunk(orch, ws1, chat_id, user_id)

    await _load_chat(orch, ws2, chat_id)

    assert _frames(ws2, "stream_subscribed") == []
    assert _frames(ws2, "ui_stream_data") == []
    sub = orch.stream_manager.subscription_for_stream(stream_id)
    assert ws2 not in sub.subscribers
