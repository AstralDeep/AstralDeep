"""Tests for orchestrator/stream_manager.py cross-user isolation: stream data reaches
only the subscribing user, unauthorized unsubscribe is rejected, and one subscriber's
auth failure doesn't affect others sharing the stream.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.stream_manager import StreamManager
from shared.protocol import ToolStreamData


class FakeWS:
    def __init__(self, label: str):
        self.label = label
        self.sent: list = []

    def __repr__(self):
        return f"<FakeWS {self.label}>"


def _make_isolated_manager():
    rote = Mock()
    rote.adapt = Mock(side_effect=lambda ws, c: c)
    sessions: dict = {}
    sent_log: list = []
    async def send_to_ws(ws, payload):
        sent_log.append((ws, payload))
    dispatcher = AsyncMock()
    dispatcher.return_value = "req-stub"
    canceller = AsyncMock()
    mgr = StreamManager(
        rote=rote,
        send_to_ws=send_to_ws,
        get_user_session=lambda ws: sessions.get(ws),
        agent_dispatcher=dispatcher,
        agent_canceller=canceller,
        validate_chat_ownership=None,
    )
    return mgr, sessions, sent_log, dispatcher


@pytest.mark.asyncio
async def test_two_users_no_crossleak():
    mgr, sessions, sent_log, dispatcher = _make_isolated_manager()
    ws_a = FakeWS("alice")
    ws_b = FakeWS("bob")
    sessions[ws_a] = {"sub": "alice"}
    sessions[ws_b] = {"sub": "bob"}

    dispatcher.side_effect = ["req-alice", "req-bob"]

    sid_a, _ = await mgr.subscribe(
        ws=ws_a, user_id="alice", chat_id="chat-a",
        tool_name="live_temperature", agent_id="weather",
        params={"latitude": 51.5, "longitude": -0.12},
    )
    sid_b, _ = await mgr.subscribe(
        ws=ws_b, user_id="bob", chat_id="chat-b",
        tool_name="live_temperature", agent_id="weather",
        params={"latitude": 40.7, "longitude": -74.0},
    )
    assert sid_a != sid_b
    assert len(mgr._active) == 2

    await mgr.handle_agent_chunk(ToolStreamData(
        request_id="req-alice", stream_id=sid_a, agent_id="weather",
        tool_name="live_temperature", seq=1,
        components=[{"type": "metric", "id": sid_a, "value": "alice-data"}],
    ))
    await mgr.handle_agent_chunk(ToolStreamData(
        request_id="req-bob", stream_id=sid_b, agent_id="weather",
        tool_name="live_temperature", seq=1,
        components=[{"type": "metric", "id": sid_b, "value": "bob-data"}],
    ))
    import asyncio
    await asyncio.sleep(0.1)

    alice_msgs = [json.loads(p) for w, p in sent_log if w is ws_a]
    bob_msgs = [json.loads(p) for w, p in sent_log if w is ws_b]
    assert all(m["stream_id"] == sid_a for m in alice_msgs)
    assert all(m["stream_id"] == sid_b for m in bob_msgs)
    assert not any("bob-data" in str(m) for m in alice_msgs)
    assert not any("alice-data" in str(m) for m in bob_msgs)


@pytest.mark.asyncio
async def test_unauthorized_unsubscribe_rejected():
    mgr, sessions, sent_log, dispatcher = _make_isolated_manager()
    ws_a = FakeWS("alice")
    ws_b = FakeWS("bob")
    sessions[ws_a] = {"sub": "alice"}
    sessions[ws_b] = {"sub": "bob"}

    dispatcher.side_effect = ["req-alice", "req-bob"]
    sid_a, _ = await mgr.subscribe(
        ws=ws_a, user_id="alice", chat_id="ca",
        tool_name="t", agent_id="a", params={},
    )
    sid_b, _ = await mgr.subscribe(
        ws=ws_b, user_id="bob", chat_id="cb",
        tool_name="t", agent_id="a", params={},
    )

    with pytest.raises(ValueError, match="not authorized"):
        await mgr.unsubscribe(ws_a, sid_b)
    assert any(s.stream_id == sid_b for s in mgr._active.values())


@pytest.mark.asyncio
async def test_per_subscriber_auth_failure_isolates_to_failing_ws():
    mgr, sessions, sent_log, dispatcher = _make_isolated_manager()
    ws_good = FakeWS("good-tab")
    ws_bad = FakeWS("expired-tab")
    # 0 disables the expiry check, it isn't epoch-expired
    sessions[ws_good] = {"sub": "alice", "expires_at": 0}
    sessions[ws_bad] = {"sub": "alice", "expires_at": 0}

    dispatcher.return_value = "req-1"
    sid, attached = await mgr.subscribe(
        ws=ws_good, user_id="alice", chat_id="chat-1",
        tool_name="t", agent_id="a", params={"k": 1},
    )
    assert attached is False
    sid2, attached2 = await mgr.subscribe(
        ws=ws_bad, user_id="alice", chat_id="chat-1",
        tool_name="t", agent_id="a", params={"k": 1},
    )
    assert sid == sid2
    assert attached2 is True
    sub = mgr._active[next(iter(mgr._active))]
    assert len(sub.subscribers) == 2

    import time as time_mod
    sessions[ws_bad]["expires_at"] = int(time_mod.time()) - 60

    await mgr.handle_agent_chunk(ToolStreamData(
        request_id="req-1", stream_id=sid, agent_id="a", tool_name="t",
        seq=1, components=[{"type": "metric", "id": sid, "value": "12C"}],
    ))
    import asyncio
    await asyncio.sleep(0.1)

    good_msgs = [json.loads(p) for w, p in sent_log if w is ws_good]
    assert any(m["components"] and m["components"][0].get("value") == "12C" for m in good_msgs)
    bad_msgs = [json.loads(p) for w, p in sent_log if w is ws_bad]
    auth_errs = [m for m in bad_msgs if m.get("error", {}).get("code") == "unauthenticated"]
    assert len(auth_errs) >= 1
    assert ws_bad not in sub.subscribers
    assert ws_good in sub.subscribers
