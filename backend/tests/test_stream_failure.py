"""Tests for orchestrator/stream_manager.py: chunk_too_large fails straight to FAILED as
retryable, a cancelled stream fails as non-retryable, and one stream's failure never
affects its session siblings.
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


def _mgr():
    sessions = {}
    sent = []
    async def send(ws, payload):
        sent.append((ws, payload))
    dispatcher = AsyncMock()
    dispatcher.return_value = "req-1"
    rote = Mock()
    rote.adapt = Mock(side_effect=lambda ws, c: c)
    mgr = StreamManager(
        rote=rote, send_to_ws=send,
        get_user_session=lambda ws: sessions.get(ws),
        agent_dispatcher=dispatcher, agent_canceller=AsyncMock(),
        validate_chat_ownership=None,
    )
    return mgr, sessions, sent, dispatcher


@pytest.mark.asyncio
async def test_chunk_too_large_goes_directly_to_failed():
    mgr, sessions, sent, dispatcher = _mgr()
    ws = FakeWS()
    sessions[ws] = {"sub": "alice"}
    sid, _ = await mgr.subscribe(
        ws=ws, user_id="alice", chat_id="c", tool_name="t",
        agent_id="a", params={},
    )
    await mgr.handle_agent_chunk(ToolStreamData(
        request_id="req-1", stream_id=sid, agent_id="a", tool_name="t",
        seq=1, components=[],
        error={"code": "chunk_too_large", "message": "70KB"},
    ))
    await asyncio.sleep(0.05)
    assert sid not in [s.stream_id for s in mgr._active.values()]
    assert dispatcher.await_count == 1
    msgs = [json.loads(p) for w, p in sent]
    failed = [m for m in msgs if (m.get("error") or {}).get("phase") == "failed"]
    assert len(failed) >= 1
    assert failed[-1]["error"]["code"] == "chunk_too_large"
    assert failed[-1]["error"]["retryable"] is True


@pytest.mark.asyncio
async def test_cancelled_goes_to_failed_not_retryable():
    mgr, sessions, sent, dispatcher = _mgr()
    ws = FakeWS()
    sessions[ws] = {"sub": "alice"}
    sid, _ = await mgr.subscribe(
        ws=ws, user_id="alice", chat_id="c", tool_name="t",
        agent_id="a", params={},
    )
    await mgr.handle_agent_chunk(ToolStreamData(
        request_id="req-1", stream_id=sid, agent_id="a", tool_name="t",
        seq=1, components=[],
        error={"code": "cancelled", "message": "user_cancel"},
    ))
    await asyncio.sleep(0.05)
    msgs = [json.loads(p) for w, p in sent]
    failed = [m for m in msgs if (m.get("error") or {}).get("phase") == "failed"]
    assert any(m["error"]["code"] == "cancelled" and m["error"]["retryable"] is False
               for m in failed)


@pytest.mark.asyncio
async def test_one_failure_does_not_affect_other_streams():
    mgr, sessions, sent, dispatcher = _mgr()
    ws = FakeWS()
    sessions[ws] = {"sub": "alice"}
    dispatcher.side_effect = ["req-A", "req-B"]
    sid_a, _ = await mgr.subscribe(
        ws=ws, user_id="alice", chat_id="c", tool_name="t1",
        agent_id="a", params={"k": 1},
    )
    sid_b, _ = await mgr.subscribe(
        ws=ws, user_id="alice", chat_id="c", tool_name="t2",
        agent_id="a", params={"k": 2},
    )
    assert len(mgr._active) == 2

    await mgr.handle_agent_chunk(ToolStreamData(
        request_id="req-A", stream_id=sid_a, agent_id="a", tool_name="t1",
        seq=1, components=[],
        error={"code": "chunk_too_large", "message": "too big"},
    ))
    await asyncio.sleep(0.05)
    active_ids = [s.stream_id for s in mgr._active.values()]
    assert sid_a not in active_ids
    assert sid_b in active_ids
