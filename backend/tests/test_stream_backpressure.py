"""Tests that tool-output streaming coalesces under backpressure
(orchestrator/stream_manager.py): a high-rate source drops intermediate chunks under
the FPS cap, keeping the coalesce slot bounded to one value.
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.stream_manager import StreamManager
from shared.protocol import ToolStreamData


class FakeWS:
    pass


@pytest.mark.asyncio
async def test_high_rate_source_coalesces_to_single_slot():
    sessions = {}
    sent = []
    sent_lock = asyncio.Lock()

    async def send(ws, payload):
        async with sent_lock:
            await asyncio.sleep(0.005)
            sent.append((ws, payload))

    rote = Mock()
    rote.adapt = Mock(side_effect=lambda ws, c: c)
    dispatcher = AsyncMock()
    dispatcher.return_value = "req-1"
    mgr = StreamManager(
        rote=rote, send_to_ws=send,
        get_user_session=lambda ws: sessions.get(ws),
        agent_dispatcher=dispatcher, agent_canceller=AsyncMock(),
        validate_chat_ownership=None,
    )
    ws = FakeWS()
    sessions[ws] = {"sub": "alice"}
    sid, _ = await mgr.subscribe(
        ws=ws, user_id="alice", chat_id="c", tool_name="t",
        agent_id="a", params={}, tool_metadata={"max_fps": 30, "min_fps": 5},
    )

    for i in range(100):
        await mgr.handle_agent_chunk(ToolStreamData(
            request_id="req-1", stream_id=sid, agent_id="a", tool_name="t",
            seq=i + 1, components=[{"type": "metric", "id": sid, "value": str(i)}],
        ))
    await asyncio.sleep(1.5)

    sub = next(iter(mgr._active.values()))
    assert sub.dropped_count > 0
    assert sub.delivered_count + sub.dropped_count >= 90
    assert sub.delivered_count <= 60

    delivered_msgs = [json.loads(p) for w, p in sent]
    data_msgs = [m for m in delivered_msgs if m.get("components") and m["components"]]
    if data_msgs:
        last = data_msgs[-1]
        last_value_int = int(last["components"][0]["value"])
        assert last_value_int >= 80, (
            f"expected last-write-wins to deliver a near-final value, got {last_value_int}"
        )


@pytest.mark.asyncio
async def test_coalesce_slot_is_single_element():
    sessions = {}
    sent = []
    async def send(ws, payload):
        await asyncio.sleep(0.05)
        sent.append((ws, payload))
    rote = Mock()
    rote.adapt = Mock(side_effect=lambda ws, c: c)
    dispatcher = AsyncMock()
    dispatcher.return_value = "req-1"
    mgr = StreamManager(
        rote=rote, send_to_ws=send,
        get_user_session=lambda ws: sessions.get(ws),
        agent_dispatcher=dispatcher, agent_canceller=AsyncMock(),
        validate_chat_ownership=None,
    )
    ws = FakeWS()
    sessions[ws] = {"sub": "alice"}
    sid, _ = await mgr.subscribe(
        ws=ws, user_id="alice", chat_id="c", tool_name="t",
        agent_id="a", params={},
    )

    for i in range(10):
        await mgr.handle_agent_chunk(ToolStreamData(
            request_id="req-1", stream_id=sid, agent_id="a", tool_name="t",
            seq=i + 1, components=[{"type": "metric", "id": sid, "value": str(i)}],
        ))
        sub = next(iter(mgr._active.values()))
        assert sub.coalesce_slot is None or hasattr(sub.coalesce_slot, "stream_id")

    await asyncio.sleep(1.0)
