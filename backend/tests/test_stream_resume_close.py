"""End-to-end proof that a resumed push stream ends at its original deadline with its
closing card: resume, a wake by asking again, and a retry after a transient error all
re-dispatch the same stream id, the real agent runner reports the elapsed time to the
tool, and the tool's closing chunk becomes the state the orchestrator saves.
"""

import asyncio
import os
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.stream_manager import LIFETIME_GRACE_SECONDS, StreamLifetime, StreamState
from shared.base_agent import BaseA2AAgent
from shared.feature_flags import flags
from shared.stream_sdk import StreamComponents, streaming_tool

pytestmark = pytest.mark.asyncio

AGENT = "probe-local-1"
TOOL = "live_resume_probe"
PROBE_POLL = 0.01
PROBE_DECLARED_S = 0.06

PROBE_STATE: dict = {}


@streaming_tool(
    name=TOOL, description="probe stream that honours the elapsed hint",
    input_schema={"type": "object", "properties": {
        "minutes": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
    }},
    max_fps=30, min_fps=5,
)
async def _live_resume_probe(args, credentials):
    PROBE_STATE["runs"] += 1
    PROBE_STATE["elapsed"].append(args.get("_stream_elapsed_s"))
    if PROBE_STATE["error_first"] and PROBE_STATE["runs"] == 1:
        yield StreamComponents(
            components=[],
            error={"code": "tool_error", "message": "transient probe outage"},
            terminal=True,
        )
        return
    hold = PROBE_STATE.get("hold")
    yield StreamComponents(
        components=[{"type": "metric", "title": "probe", "value": f"live-{PROBE_STATE['runs']}"}],
    )
    if hold is not None:
        await hold.wait()
    deadline = asyncio.get_running_loop().time() + max(
        0.0, PROBE_DECLARED_S - float(args.get("_stream_elapsed_s") or 0.0)
    )
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(PROBE_POLL)
        yield StreamComponents(
            components=[{"type": "metric", "title": "probe", "value": f"poll-{PROBE_STATE['runs']}"}],
        )
    yield StreamComponents(
        components=[{"type": "metric", "title": "probe", "value": "final"}], terminal=True,
    )


class ProbeAgent(BaseA2AAgent):
    agent_id = AGENT
    service_name = "Resume Probe"
    description = "probe for resumed stream closing cards"
    skill_tags = ["test"]


@pytest.fixture
def probe_state():
    PROBE_STATE.clear()
    PROBE_STATE.update({"runs": 0, "elapsed": [], "hold": None, "error_first": False})
    yield PROBE_STATE
    PROBE_STATE.clear()


@pytest.fixture
def push_flags():
    prior = {k: flags._flags[k] for k in ("tool_streaming", "stream_artifacts")}
    flags._flags["tool_streaming"] = True
    flags._flags["stream_artifacts"] = True
    yield
    flags._flags.update(prior)


@pytest.fixture
async def env(push_flags, probe_state, monkeypatch):
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    monkeypatch.setattr(
        BaseA2AAgent, "_init_crypto",
        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}),
    )
    agent = ProbeAgent(mcp_server=SimpleNamespace(tools={TOOL: {"function": _live_resume_probe}}), port=8997)
    user_id = f"resume-close-{uuid.uuid4().hex[:8]}"
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id="")
    ws = VirtualWebSocket(task)
    orch.ui_sessions[ws] = {"sub": user_id}
    orch.ui_clients.append(ws)
    orch.rote.register_device(ws, {})
    chat_id = await asyncio.to_thread(orch.history.create_chat, user_id=user_id)
    orch._ws_active_chat[id(ws)] = chat_id
    orch.local_agents[AGENT] = agent
    orch.tool_permissions = MagicMock()
    orch.tool_permissions.is_tool_allowed.return_value = True
    orch._streamable_tools[TOOL] = {
        "agent_id": AGENT, "kind": "push", "max_fps": 30, "min_fps": 5,
        "max_chunk_bytes": 65536, "default_interval": 2,
        "min_interval": 1, "max_interval": 30,
        "lifetime": StreamLifetime("minutes", 60.0, 1.0, 1.0, 10.0),
    }
    try:
        yield orch, ws, chat_id, user_id, agent
    finally:
        try:
            orch.stream_manager.shutdown()
        except Exception:
            pass
        for entry in list(agent._active_streams.values()):
            if not entry[0].done():
                entry[0].cancel()
        pending = [t for t in agent._stream_wrapper_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            await asyncio.to_thread(orch.history.delete_chat, chat_id, user_id=user_id)
        except Exception:
            pass
        await orch._close_started_services()


async def _start(orch, ws, chat_id, user_id):
    stream_id, _ = await orch.stream_manager.subscribe(
        ws=ws, user_id=user_id, chat_id=chat_id, tool_name=TOOL, agent_id=AGENT,
        params={"minutes": 1}, tool_metadata=orch._streamable_tools[TOOL],
    )
    return orch.stream_manager.subscription_for_stream(stream_id)


def _frames(ws, ftype):
    return [f for f in ws.task.outputs if f.get("type") == ftype]


async def _wait_for(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    limit = loop.time() + timeout
    while not predicate():
        if loop.time() > limit:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


async def _persisted_values(orch, chat_id, user_id):
    live = await asyncio.to_thread(orch.workspace.live_components, chat_id, user_id)
    return [component["value"] for component in live if component.get("type") == "metric"]


def _closing_frames(ws):
    return [
        f for f in _frames(ws, "ui_stream_data")
        if f.get("terminal") and f.get("components")
    ]


async def test_a_first_run_reports_no_elapsed_time_and_sends_its_closing_card(env):
    orch, ws, chat_id, user_id, agent = env
    sub = await _start(orch, ws, chat_id, user_id)
    await _wait_for(lambda: bool(_closing_frames(ws)))
    assert PROBE_STATE["elapsed"] and PROBE_STATE["elapsed"][0] == 0.0
    assert sub.retained_chunk.components[0]["value"] == "final"
    assert sub.expires_at is not None and sub.expires_at > time.monotonic()


async def test_a_resumed_stream_ends_at_its_deadline_with_its_closing_card(env):
    orch, ws, chat_id, user_id, agent = env
    sub = await _start(orch, ws, chat_id, user_id)
    await _wait_for(lambda: PROBE_STATE["runs"] >= 1 and bool(_frames(ws, "ui_stream_data")))
    deadline = sub.expires_at
    await orch.stream_manager.detach(ws)
    assert sub.state is StreamState.DORMANT

    agent._stream_first_seen[sub.stream_id] = time.monotonic() - 10_000
    resumed = await orch.stream_manager.resume(ws, user_id, chat_id)
    assert resumed == [(sub.stream_id, TOOL)]
    assert sub.expires_at == deadline

    await _wait_for(lambda: bool(_closing_frames(ws)))
    assert PROBE_STATE["runs"] == 2
    assert PROBE_STATE["elapsed"][-1] >= 10_000
    assert sub.retained_chunk.components[0]["value"] == "final"

    sub.expires_at = time.monotonic() - LIFETIME_GRACE_SECONDS - 1
    await orch.stream_manager._sweep_lifetimes()
    assert sub.state is StreamState.STOPPED and sub.state_reason == "duration_elapsed"
    assert sub.persist_done is True
    assert await _persisted_values(orch, chat_id, user_id) == ["final"]


async def test_asking_again_wakes_the_dormant_stream_with_its_elapsed_time(env):
    orch, ws, chat_id, user_id, agent = env
    sub = await _start(orch, ws, chat_id, user_id)
    await _wait_for(lambda: PROBE_STATE["runs"] >= 1 and bool(_frames(ws, "ui_stream_data")))
    deadline = sub.expires_at
    await orch.stream_manager.detach(ws)
    assert sub.state is StreamState.DORMANT

    agent._stream_first_seen[sub.stream_id] = time.monotonic() - 10_000
    stream_id, attached = await orch.stream_manager.subscribe(
        ws=ws, user_id=user_id, chat_id=chat_id, tool_name=TOOL, agent_id=AGENT,
        params={"minutes": 1}, tool_metadata=orch._streamable_tools[TOOL],
    )
    assert stream_id == sub.stream_id and attached is False
    assert sub.expires_at == deadline

    await _wait_for(lambda: bool(_closing_frames(ws)))
    assert PROBE_STATE["runs"] == 2
    assert PROBE_STATE["elapsed"][-1] >= 10_000
    assert sub.retained_chunk.components[0]["value"] == "final"

    sub.expires_at = time.monotonic() - LIFETIME_GRACE_SECONDS - 1
    await orch.stream_manager._sweep_lifetimes()
    assert sub.state_reason == "duration_elapsed" and sub.persist_done is True
    assert await _persisted_values(orch, chat_id, user_id) == ["final"]


async def test_a_retry_after_a_transient_error_keeps_counting_toward_the_deadline(env):
    orch, ws, chat_id, user_id, agent = env
    PROBE_STATE["error_first"] = True
    sub = await _start(orch, ws, chat_id, user_id)
    await _wait_for(lambda: sub.state is StreamState.RECONNECTING, timeout=10.0)
    deadline = sub.expires_at

    agent._stream_first_seen[sub.stream_id] = time.monotonic() - 10_000
    await orch.stream_manager._retry(sub)
    await _wait_for(lambda: bool(_closing_frames(ws)))
    assert PROBE_STATE["runs"] == 2
    assert PROBE_STATE["elapsed"][-1] >= 10_000
    assert sub.retained_chunk.components[0]["value"] == "final"
    assert sub.expires_at == deadline

    sub.expires_at = time.monotonic() - LIFETIME_GRACE_SECONDS - 1
    await orch.stream_manager._sweep_lifetimes()
    assert sub.state_reason == "duration_elapsed" and sub.persist_done is True
    assert await _persisted_values(orch, chat_id, user_id) == ["final"]


async def test_a_run_that_ignores_the_hint_is_still_ended_by_the_deadline(env):
    orch, ws, chat_id, user_id, agent = env
    PROBE_STATE["hold"] = asyncio.Event()
    sub = await _start(orch, ws, chat_id, user_id)
    await _wait_for(lambda: PROBE_STATE["runs"] >= 1 and bool(_frames(ws, "ui_stream_data")))
    assert PROBE_STATE["elapsed"][0] == 0.0

    sub.expires_at = time.monotonic() - 60
    await orch.stream_manager._sweep_lifetimes()
    assert sub.state is StreamState.STOPPED and sub.state_reason == "duration_elapsed"
    assert not orch.stream_manager._active and not orch.stream_manager._dormant
    await _wait_for(lambda: all(task.done() for task in agent._stream_wrapper_tasks))
    cancelled = [f for f in _frames(ws, "ui_stream_data") if (f.get("error") or {}).get("code") == "cancelled"]
    assert not cancelled, "a cancelled error chunk must not reach the subscriber after teardown"
