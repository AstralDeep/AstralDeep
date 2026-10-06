"""Tests for the StreamManager lifecycle in orchestrator/stream_manager.py: subscribe
through first chunk delivery, pause-on-leave to dormant, resume-on-return, and the one
lifetime a stream with a declared duration keeps, against a manager wired with mocked dependencies.
"""

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import stream_manager
from orchestrator.stream_manager import (
    LIFETIME_GRACE_SECONDS,
    StreamLifetime,
    StreamManager,
    StreamState,
)
from shared.protocol import ToolStreamData, ToolStreamEnd


class FakeWebSocket:
    def __init__(self):
        self.sent: list = []
        self.closed = False

    def __hash__(self):
        return id(self)


def _make_manager_with_dispatcher(dispatcher_returns_request_id: str = "req-1"):
    rote = Mock()
    rote.adapt = Mock(side_effect=lambda ws, components: components)
    send_to_ws = AsyncMock()
    sessions = {}
    def get_session(ws):
        return sessions.get(ws)
    dispatcher = AsyncMock(return_value=dispatcher_returns_request_id)
    canceller = AsyncMock()
    mgr = StreamManager(
        rote=rote,
        send_to_ws=send_to_ws,
        get_user_session=get_session,
        agent_dispatcher=dispatcher,
        agent_canceller=canceller,
        validate_chat_ownership=None,
    )
    return mgr, {
        "rote": rote, "send_to_ws": send_to_ws, "sessions": sessions,
        "dispatcher": dispatcher, "canceller": canceller,
    }


class TestUS1HappyPath:
    @pytest.mark.asyncio
    async def test_subscribe_creates_active_entry(self):
        mgr, deps = _make_manager_with_dispatcher("req-test-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}

        stream_id, attached = await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="live_temperature", agent_id="weather",
            params={"latitude": 51.5, "longitude": -0.12, "interval_s": 5},
        )

        assert stream_id.startswith("stream-")
        assert attached is False
        assert len(mgr._active) == 1
        sub = next(iter(mgr._active.values()))
        assert sub.stream_id == stream_id
        assert sub.state == StreamState.STARTING
        assert sub.subscribers == [ws]
        assert sub.user_id == "alice"
        assert sub.chat_id == "chat-1"
        assert sub.request_id == "req-test-1"
        assert mgr._request_to_key["req-test-1"] == sub.key
        deps["dispatcher"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subscribe_rejects_when_session_missing(self):
        mgr, deps = _make_manager_with_dispatcher()
        ws = FakeWebSocket()
        with pytest.raises(ValueError, match="no active session"):
            await mgr.subscribe(
                ws=ws, user_id="alice", chat_id="c1",
                tool_name="t", agent_id="a", params={},
            )

    @pytest.mark.asyncio
    async def test_subscribe_rejects_user_mismatch(self):
        mgr, deps = _make_manager_with_dispatcher()
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        with pytest.raises(ValueError, match="user_id does not match"):
            await mgr.subscribe(
                ws=ws, user_id="bob", chat_id="c1",
                tool_name="t", agent_id="a", params={},
            )

    @pytest.mark.asyncio
    async def test_first_chunk_transitions_to_active_and_sends(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        stream_id, _ = await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="live_temperature", agent_id="weather",
            params={"latitude": 51.5, "longitude": -0.12},
        )

        chunk_msg = ToolStreamData(
            request_id="req-1",
            stream_id=stream_id,
            agent_id="weather",
            tool_name="live_temperature",
            seq=1,
            components=[{"type": "metric", "id": stream_id, "value": "12C"}],
        )
        await mgr.handle_agent_chunk(chunk_msg)
        await asyncio.sleep(0.05)

        sub = mgr._active[next(iter(mgr._active))]
        assert sub.state == StreamState.ACTIVE
        assert sub.delivered_count >= 1
        assert deps["send_to_ws"].await_count >= 1
        sent_args = deps["send_to_ws"].await_args_list[0]
        sent_ws, sent_payload = sent_args.args
        assert sent_ws is ws
        wire = json.loads(sent_payload)
        assert wire["type"] == "ui_stream_data"
        assert wire["stream_id"] == stream_id
        assert wire["session_id"] == "chat-1"
        assert wire["seq"] == 1
        assert wire["components"][0]["value"] == "12C"
        assert wire["components"][0]["id"] == stream_id

    @pytest.mark.asyncio
    async def test_multiple_chunks_increment_delivered(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        stream_id, _ = await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="live_temperature", agent_id="weather",
            params={"latitude": 51.5, "longitude": -0.12},
        )

        for i in range(3):
            await mgr.handle_agent_chunk(ToolStreamData(
                request_id="req-1", stream_id=stream_id,
                agent_id="weather", tool_name="live_temperature",
                seq=i + 1,
                components=[{"type": "metric", "id": stream_id, "value": str(i)}],
            ))
            await asyncio.sleep(0.15)

        sub = mgr._active[next(iter(mgr._active))]
        assert sub.delivered_count >= 1
        assert deps["send_to_ws"].await_count >= 1

    @pytest.mark.asyncio
    async def test_handle_agent_end_sends_terminal_and_cleans_up(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        stream_id, _ = await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="live_temperature", agent_id="weather",
            params={"latitude": 51.5, "longitude": -0.12},
        )
        await mgr.handle_agent_end(ToolStreamEnd(request_id="req-1", stream_id=stream_id))
        assert stream_id not in [s.stream_id for s in mgr._active.values()]
        assert "req-1" not in mgr._request_to_key
        terminal_calls = [
            call for call in deps["send_to_ws"].await_args_list
            if "terminal" in call.args[1] and json.loads(call.args[1]).get("terminal") is True
        ]
        assert len(terminal_calls) == 1

    @pytest.mark.asyncio
    async def test_unknown_request_id_chunk_dropped_silently(self):
        mgr, _ = _make_manager_with_dispatcher()
        await mgr.handle_agent_chunk(ToolStreamData(
            request_id="never-existed", stream_id="x", agent_id="a",
            tool_name="t", seq=1,
        ))
        assert len(mgr._active) == 0

    @pytest.mark.asyncio
    async def test_per_user_cap_enforced(self):
        from orchestrator.stream_manager import MAX_STREAM_SUBSCRIPTIONS
        mgr, deps = _make_manager_with_dispatcher()
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        for i in range(MAX_STREAM_SUBSCRIPTIONS):
            await mgr.subscribe(
                ws=ws, user_id="alice", chat_id=f"chat-{i}",
                tool_name="t", agent_id="a", params={"k": i},
            )
        with pytest.raises(ValueError, match="limit"):
            await mgr.subscribe(
                ws=ws, user_id="alice", chat_id="chat-overflow",
                tool_name="t", agent_id="a", params={"k": "extra"},
            )


class TestUS2PauseOnLeave:
    @pytest.mark.asyncio
    async def test_pause_on_load_chat_moves_to_dormant(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        stream_id, _ = await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="live_temperature", agent_id="weather", params={},
        )
        assert len(mgr._active) == 1

        await mgr.pause_chat(ws, "chat-1")

        assert len(mgr._active) == 0
        assert ("alice", "chat-1") in mgr._dormant
        dormant_sub = mgr._dormant[("alice", "chat-1")][next(iter(mgr._dormant[("alice", "chat-1")]))]
        assert dormant_sub.state == StreamState.DORMANT
        assert dormant_sub.subscribers == []
        assert dormant_sub.task is None
        deps["canceller"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_moves_all_to_dormant(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="t1", agent_id="a", params={"k": 1},
        )
        assert len(mgr._active) == 1

        await mgr.detach(ws)

        assert len(mgr._active) == 0
        assert ("alice", "chat-1") in mgr._dormant

    @pytest.mark.asyncio
    async def test_pause_chat_only_affects_named_chat(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws1 = FakeWebSocket()
        ws2 = FakeWebSocket()
        deps["sessions"][ws1] = {"sub": "alice"}
        deps["sessions"][ws2] = {"sub": "alice"}

        await mgr.subscribe(
            ws=ws1, user_id="alice", chat_id="chat-1",
            tool_name="t1", agent_id="a", params={"k": 1},
        )
        await mgr.subscribe(
            ws=ws2, user_id="alice", chat_id="chat-2",
            tool_name="t1", agent_id="a", params={"k": 2},
        )
        assert len(mgr._active) == 2

        await mgr.pause_chat(ws1, "chat-1")
        active_chats = {sub.chat_id for sub in mgr._active.values()}
        assert active_chats == {"chat-2"}
        assert ("alice", "chat-1") in mgr._dormant
        assert ("alice", "chat-2") not in mgr._dormant

    @pytest.mark.asyncio
    async def test_dormant_ttl_eviction(self):
        from orchestrator.stream_manager import DORMANT_TTL_SECONDS
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="t", agent_id="a", params={},
        )
        await mgr.detach(ws)
        assert ("alice", "chat-1") in mgr._dormant

        sub = next(iter(mgr._dormant[("alice", "chat-1")].values()))
        sub.created_at = sub.created_at - DORMANT_TTL_SECONDS - 10

        mgr._sweep_dormant_ttl()

        assert ("alice", "chat-1") not in mgr._dormant

    @pytest.mark.asyncio
    async def test_dormant_lru_eviction(self):
        from orchestrator.stream_manager import MAX_DORMANT_PER_USER
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        for i in range(MAX_DORMANT_PER_USER):
            await mgr.subscribe(
                ws=ws, user_id="alice", chat_id=f"chat-{i}",
                tool_name="t", agent_id="a", params={"k": i},
            )
            await mgr.detach(ws)
            deps["sessions"][ws] = {"sub": "alice"}
        assert mgr._count_dormant_for_user("alice") == MAX_DORMANT_PER_USER

        await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-overflow",
            tool_name="t", agent_id="a", params={"k": "extra"},
        )
        await mgr.detach(ws)
        assert mgr._count_dormant_for_user("alice") == MAX_DORMANT_PER_USER


class TestUS3ResumeOnReturn:
    @pytest.mark.asyncio
    async def test_resume_after_pause_with_same_stream_id(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        original_id, _ = await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="t", agent_id="a", params={"k": 1},
        )
        await mgr.pause_chat(ws, "chat-1")
        assert ("alice", "chat-1") in mgr._dormant

        deps["dispatcher"].return_value = "req-resumed"
        resumed = await mgr.resume(ws, "alice", "chat-1")
        assert len(resumed) == 1
        resumed_stream_id, resumed_tool = resumed[0]
        assert resumed_stream_id == original_id
        assert resumed_tool == "t"
        assert ("alice", "chat-1") not in mgr._dormant
        sub = next(iter(mgr._active.values()))
        assert sub.state == StreamState.STARTING
        assert sub.subscribers == [ws]
        assert sub.request_id == "req-resumed"
        assert deps["dispatcher"].await_count == 2

    @pytest.mark.asyncio
    async def test_resume_with_no_dormant_returns_empty(self):
        mgr, deps = _make_manager_with_dispatcher()
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        resumed = await mgr.resume(ws, "alice", "chat-fresh")
        assert resumed == []

    @pytest.mark.asyncio
    async def test_resume_when_agent_gone_sends_failure_chunk(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        deps["sessions"][ws] = {"sub": "alice"}
        await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="chat-1",
            tool_name="t", agent_id="a", params={"k": 1},
        )
        await mgr.pause_chat(ws, "chat-1")

        deps["dispatcher"].side_effect = RuntimeError("agent gone")

        resumed = await mgr.resume(ws, "alice", "chat-1")
        assert resumed == []
        sent = [json.loads(call.args[1]) for call in deps["send_to_ws"].await_args_list]
        failure_msgs = [
            m for m in sent
            if m.get("error") and m["error"].get("phase") == "failed"
        ]
        assert len(failure_msgs) >= 1
        assert failure_msgs[-1]["error"]["code"] == "upstream_unavailable"
        assert failure_msgs[-1]["error"]["retryable"] is True
        assert ("alice", "chat-1") not in mgr._dormant
        assert len(mgr._active) == 0


WATCH = {"lifetime": StreamLifetime("minutes", 60.0, 2.0, 1.0, 10.0)}


async def _watch(mgr, deps, ws, chat_id="chat-1", metadata=WATCH):
    deps["sessions"][ws] = {"sub": "alice"}
    stream_id, attached = await mgr.subscribe(
        ws=ws, user_id="alice", chat_id=chat_id, tool_name="watch", agent_id="a",
        params={"minutes": 5}, tool_metadata=metadata,
    )
    return mgr.subscription_for_stream(stream_id), attached


def _chunk(sub, seq):
    return ToolStreamData(
        request_id=sub.request_id, stream_id=sub.stream_id, agent_id="a", tool_name="watch", seq=seq,
        components=[{"type": "metric", "id": sub.stream_id, "value": str(seq)}],
    )


def _overdue(sub, grace=0.0):
    sub.expires_at = time.monotonic() - grace - 1


class TestDeclaredLifetime:
    @pytest.mark.asyncio
    async def test_reconnects_resume_the_stream_without_moving_its_deadline(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        sub, _ = await _watch(mgr, deps, ws)
        deadline = sub.expires_at
        assert 0 < deadline - time.monotonic() <= 300

        for _ in range(3):
            await mgr.detach(ws)
            assert sub.state == StreamState.DORMANT
            assert [stream_id for stream_id, _tool in await mgr.resume(ws, "alice", "chat-1")] == [sub.stream_id]
            assert sub.expires_at == deadline

        assert deps["dispatcher"].await_count == 4
        assert deps["canceller"].await_count == 3

    @pytest.mark.asyncio
    async def test_a_stream_without_a_declared_duration_never_expires(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        sub, _ = await _watch(mgr, deps, ws, metadata={"max_fps": 30})
        assert sub.expires_at is None and not sub.expired(time.monotonic() + 10**9)

        await mgr._sweep_lifetimes()
        await mgr.detach(ws)
        await mgr._sweep_lifetimes()
        assert await mgr.resume(ws, "alice", "chat-1") == [(sub.stream_id, "watch")]

    @pytest.mark.asyncio
    async def test_a_return_after_the_deadline_ends_the_stream_instead_of_restarting_it(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        mgr.terminal_hook = AsyncMock()
        ws = FakeWebSocket()
        sub, _ = await _watch(mgr, deps, ws)
        await mgr.detach(ws)
        _overdue(sub)

        assert await mgr.resume(ws, "alice", "chat-1") == []

        assert deps["dispatcher"].await_count == 1
        assert not mgr._active and not mgr._dormant
        assert (sub.state, sub.state_reason) == (StreamState.STOPPED, "duration_elapsed")
        mgr.terminal_hook.assert_awaited_once_with(sub)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("elapsed", [False, True])
    async def test_asking_again_wakes_a_paused_stream_or_starts_a_new_one_after_its_deadline(self, elapsed):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        mgr.terminal_hook = AsyncMock()
        ws = FakeWebSocket()
        first, _ = await _watch(mgr, deps, ws)
        deadline = first.expires_at
        await mgr.detach(ws)
        if elapsed:
            _overdue(first)

        second, attached = await _watch(mgr, deps, ws)

        assert attached is False and not mgr._dormant and deps["dispatcher"].await_count == 2
        assert list(mgr._active.values()) == [second]
        if elapsed:
            assert second is not first and second.stream_id != first.stream_id
            assert second.expires_at > time.monotonic()
            assert (first.state, first.state_reason) == (StreamState.STOPPED, "duration_elapsed")
            mgr.terminal_hook.assert_awaited_once_with(first)
        else:
            assert second is first and first.expires_at == deadline
            assert first.state_reason == "wake_on_attach"
            mgr.terminal_hook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_run_past_its_deadline_is_ended_by_its_next_chunk_after_the_grace_period(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        order = []
        mgr.terminal_hook = AsyncMock(side_effect=lambda _sub: order.append("saved"))
        deps["canceller"].side_effect = lambda *_args: order.append("cancelled")
        ws = FakeWebSocket()
        sub, _ = await _watch(mgr, deps, ws)

        _overdue(sub)
        await mgr.handle_agent_chunk(_chunk(sub, 1))
        assert sub.state == StreamState.ACTIVE and mgr._active and order == []

        _overdue(sub, LIFETIME_GRACE_SECONDS)
        await mgr.handle_agent_chunk(_chunk(sub, 2))

        assert (sub.state, sub.state_reason) == (StreamState.STOPPED, "duration_elapsed")
        assert not mgr._active and not mgr._dormant and "req-1" not in mgr._request_to_key
        assert order == ["saved", "cancelled"]
        deps["canceller"].assert_awaited_once_with("a", "req-1", sub.stream_id)
        last = json.loads(deps["send_to_ws"].await_args_list[-1].args[1])
        assert last["terminal"] is True and last["components"] == [] and last["seq"] == 2
        await mgr.handle_agent_chunk(_chunk(sub, 3))
        assert order == ["saved", "cancelled"]

    @pytest.mark.asyncio
    async def test_the_sweep_ends_only_overdue_streams(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        mgr.terminal_hook = AsyncMock()
        sockets = [FakeWebSocket() for _ in range(5)]
        deps["dispatcher"].side_effect = [f"req-{index}" for index in range(5)]
        overrun, in_grace, paused_late, paused, endless = [
            (await _watch(mgr, deps, ws, chat_id=f"chat-{index}", metadata=WATCH if index < 4 else {}))[0]
            for index, ws in enumerate(sockets)
        ]
        await mgr.detach(sockets[2])
        await mgr.detach(sockets[3])
        _overdue(overrun, LIFETIME_GRACE_SECONDS)
        _overdue(in_grace)
        _overdue(paused_late)

        await mgr._sweep_lifetimes()

        assert list(mgr._active.values()) == [in_grace, endless]
        assert list(mgr._dormant) == [("alice", "chat-3")]
        assert mgr._dormant[("alice", "chat-3")][paused.params_hash] is paused
        for ended in (overrun, paused_late):
            assert (ended.state, ended.state_reason) == (StreamState.STOPPED, "duration_elapsed")
        assert [call.args[0] for call in mgr.terminal_hook.await_args_list] == [overrun, paused_late]

    @pytest.mark.asyncio
    async def test_a_retry_after_the_deadline_ends_the_stream(self):
        mgr, deps = _make_manager_with_dispatcher("req-1")
        mgr.terminal_hook = AsyncMock()
        ws = FakeWebSocket()
        sub, _ = await _watch(mgr, deps, ws)
        await mgr._enter_reconnecting(sub, "upstream_unavailable", "synthetic outage")
        _overdue(sub)

        await mgr._retry(sub)

        assert deps["dispatcher"].await_count == 1 and not mgr._active
        assert (sub.state, sub.state_reason) == (StreamState.STOPPED, "duration_elapsed")
        assert sub._retry_handle is None
        mgr.terminal_hook.assert_awaited_once_with(sub)

    @pytest.mark.asyncio
    async def test_the_background_sweep_enforces_deadlines(self, monkeypatch):
        monkeypatch.setattr(stream_manager, "SWEEP_INTERVAL_SECONDS", 0.01)
        mgr, deps = _make_manager_with_dispatcher("req-1")
        ws = FakeWebSocket()
        sub, _ = await _watch(mgr, deps, ws)
        _overdue(sub, LIFETIME_GRACE_SECONDS)
        try:
            async with asyncio.timeout(10):
                while mgr._active:
                    await asyncio.sleep(0.01)
        finally:
            mgr.shutdown()
        assert (sub.state, sub.state_reason) == (StreamState.STOPPED, "duration_elapsed")
        deps["canceller"].assert_awaited_once_with("a", "req-1", sub.stream_id)
