"""055-uniform-artifacts US2 (T019) — stream→workspace identity bridge.

With FF_STREAM_ARTIFACTS on, ``StreamManager.subscribe`` assigns the
workspace rule-2 fingerprint to ``StreamSubscription.component_id``, every
``ui_stream_data`` frame carries it, and the newest content-bearing chunk is
retained on the subscription for the orchestrator's persist-on-terminal
(T020). Flag off: frames stay byte-identical to pre-055 and nothing is
retained. Narrative (``narrative-*``) and legacy polling frames never carry
the field.
"""
import asyncio
import json
import os
import sys
import types
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.stream_manager import StreamManager
from orchestrator.workspace import fingerprint
from shared.feature_flags import flags
from shared.protocol import ToolStreamData, ToolStreamEnd

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal websocket double — tracks messages sent to it for assertion."""
    def __init__(self):
        self.sent: list = []
        self.closed = False

    def __hash__(self):
        return id(self)


def _make_manager(dispatcher_returns_request_id: str = "req-1"):
    """StreamManager with mocked dependencies (same shape as
    test_stream_lifecycle). Returns (manager, deps)."""
    rote = Mock()
    rote.adapt = Mock(side_effect=lambda ws, components: components)
    send_to_ws = AsyncMock()
    sessions = {}

    def get_session(ws):
        return sessions.get(ws)

    mgr = StreamManager(
        rote=rote,
        send_to_ws=send_to_ws,
        get_user_session=get_session,
        agent_dispatcher=AsyncMock(return_value=dispatcher_returns_request_id),
        agent_canceller=AsyncMock(),
        validate_chat_ownership=None,
    )
    return mgr, {"send_to_ws": send_to_ws, "sessions": sessions}


async def _subscribed(mgr, deps, params=None, user_id="alice", chat_id="chat-1"):
    ws = FakeWebSocket()
    deps["sessions"][ws] = {"sub": user_id}
    stream_id, _ = await mgr.subscribe(
        ws=ws, user_id=user_id, chat_id=chat_id,
        tool_name="live_temperature", agent_id="weather",
        params=params if params is not None else {"latitude": 51.5, "longitude": -0.12},
    )
    return ws, stream_id


def _chunk(stream_id, seq, components):
    return ToolStreamData(
        request_id="req-1", stream_id=stream_id, agent_id="weather",
        tool_name="live_temperature", seq=seq, components=components,
    )


def _sent_frames(deps):
    return [json.loads(call.args[1]) for call in deps["send_to_ws"].await_args_list]


# The bridge behaviors under test are flag-on semantics, so force the flag
# rather than inherit the environment's default (the SC-009 CI job runs this
# suite with every 055 flag off). Autouse fixtures instantiate before the
# explicitly-requested stream_artifacts_off, so flag-off tests still win.
@pytest.fixture(autouse=True)
def stream_artifacts_on():
    prior = flags._flags["stream_artifacts"]
    flags._flags["stream_artifacts"] = True
    yield
    flags._flags["stream_artifacts"] = prior


@pytest.fixture
def stream_artifacts_off():
    prior = flags._flags["stream_artifacts"]
    flags._flags["stream_artifacts"] = False
    yield
    flags._flags["stream_artifacts"] = prior


# ---------------------------------------------------------------------------
# Identity assignment at subscribe
# ---------------------------------------------------------------------------

class TestIdentityAssignment:
    async def test_subscribe_assigns_rule2_fingerprint(self):
        mgr, deps = _make_manager()
        params = {"latitude": 51.5, "longitude": -0.12}
        ws, stream_id = await _subscribed(mgr, deps, params=params)

        sub = next(iter(mgr._active.values()))
        assert stream_id.startswith("stream-")
        assert sub.component_id == fingerprint("weather", "live_temperature", params)
        assert sub.component_id.startswith("wc_")
        assert sub.bridged_component_id == sub.component_id

    async def test_identity_stable_across_param_order(self):
        mgr, deps = _make_manager()
        await _subscribed(mgr, deps, params={"longitude": -0.12, "latitude": 51.5})
        sub = next(iter(mgr._active.values()))
        assert sub.component_id == fingerprint(
            "weather", "live_temperature", {"latitude": 51.5, "longitude": -0.12})

    async def test_different_params_get_different_identity(self):
        mgr, deps = _make_manager()
        await _subscribed(mgr, deps, params={"latitude": 51.5}, chat_id="chat-1")
        await _subscribed(mgr, deps, params={"latitude": 52.0}, chat_id="chat-2")
        ids = {s.component_id for s in mgr._active.values()}
        assert len(ids) == 2

    async def test_component_id_for_active_and_dormant(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        expected = fingerprint(
            "weather", "live_temperature", {"latitude": 51.5, "longitude": -0.12})
        assert mgr.component_id_for(stream_id) == expected

        # ws disconnect parks the subscription DORMANT — still resolvable.
        await mgr.detach(ws)
        assert mgr.component_id_for(stream_id) == expected
        assert mgr.component_id_for("stream-never-existed") is None

    async def test_flag_off_component_id_equals_stream_id(self, stream_artifacts_off):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        sub = next(iter(mgr._active.values()))
        assert sub.component_id == stream_id
        assert sub.bridged_component_id is None
        assert mgr.component_id_for(stream_id) is None


# ---------------------------------------------------------------------------
# Last content-bearing chunk retention (persist-on-terminal payload for T020)
# ---------------------------------------------------------------------------

class TestRetention:
    async def test_content_chunk_retained(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)

        sub = next(iter(mgr._active.values()))
        assert sub.retained_chunk is not None
        assert sub.retained_chunk.seq == 1
        assert sub.retained_chunk.components == [{"type": "metric", "value": "12C"}]

    async def test_heartbeat_does_not_overwrite(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await mgr.handle_agent_chunk(_chunk(stream_id, 2, []))  # empty delta
        await asyncio.sleep(0.05)

        sub = next(iter(mgr._active.values()))
        assert sub.retained_chunk.seq == 1

    async def test_newer_content_replaces(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)
        await mgr.handle_agent_chunk(_chunk(stream_id, 2, [{"type": "metric", "value": "13C"}]))
        await asyncio.sleep(0.05)

        sub = next(iter(mgr._active.values()))
        assert sub.retained_chunk.seq == 2
        assert sub.retained_chunk.components[0]["value"] == "13C"

    async def test_retained_survives_terminal(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)
        sub = next(iter(mgr._active.values()))

        await mgr.handle_agent_end(ToolStreamEnd(request_id="req-1", stream_id=stream_id))
        # Teardown must not clear the retention — the orchestrator's
        # persist-on-terminal wrapper reads it after handle_agent_end.
        assert sub.retained_chunk is not None
        assert sub.retained_chunk.components[0]["value"] == "12C"

    async def test_flag_off_no_retention(self, stream_artifacts_off):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)

        sub = next(iter(mgr._active.values()))
        assert sub.retained_chunk is None


# ---------------------------------------------------------------------------
# component_id field presence on the wire
# ---------------------------------------------------------------------------

class TestFrameFieldPresence:
    async def test_chunk_frames_carry_component_id(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)

        frames = _sent_frames(deps)
        assert frames, "no frame delivered"
        expected = fingerprint(
            "weather", "live_temperature", {"latitude": 51.5, "longitude": -0.12})
        for frame in frames:
            assert frame["type"] == "ui_stream_data"
            assert frame["component_id"] == expected

    async def test_terminal_frame_carries_component_id(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_end(ToolStreamEnd(request_id="req-1", stream_id=stream_id))

        frames = [f for f in _sent_frames(deps) if f.get("terminal") is True]
        assert len(frames) == 1
        assert frames[0]["component_id"].startswith("wc_")

    async def test_unsubscribe_ack_carries_component_id(self):
        # The unsubscribe ack goes through the single-ws builder
        # (_send_chunk_to_ws) — the other of the two frame builders.
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.unsubscribe(ws, stream_id)

        frames = [f for f in _sent_frames(deps) if f.get("terminal") is True]
        assert len(frames) == 1
        assert frames[0]["component_id"].startswith("wc_")

    async def test_flag_off_frames_byte_identical(self, stream_artifacts_off):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)
        await mgr.unsubscribe(ws, stream_id)

        frames = _sent_frames(deps)
        assert frames
        # Pre-055 key sets, exactly: fan-out frames carry html, the
        # single-ws ack does not. No component_id anywhere.
        fanout_keys = {"type", "stream_id", "session_id", "seq",
                       "components", "html", "raw", "terminal", "error"}
        ack_keys = fanout_keys - {"html"}
        for frame in frames:
            assert set(frame.keys()) in (fanout_keys, ack_keys)


# ---------------------------------------------------------------------------
# Narrative + legacy polling streams stay identity-less
# ---------------------------------------------------------------------------

class TestNarrativeAndLegacyExclusion:
    async def test_narrative_frames_never_carry_component_id(self):
        from orchestrator.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        sent = []

        async def record(ws, data):
            sent.append(json.loads(data))
            return True

        orch._safe_send = record
        rote = Mock()
        rote.get_profile = Mock(return_value=None)
        rote.adapt = Mock(side_effect=lambda ws, comps: comps)
        orch.rote = rote
        ws = FakeWebSocket()

        await orch._emit_narrative_frame(
            ws, "chat-1", "narrative-abc123def456", 1, "Hello **world**", terminal=False)
        await orch._emit_narrative_frame(
            ws, "chat-1", "narrative-abc123def456", 2, "", terminal=True)

        assert len(sent) == 2
        for frame in sent:
            assert frame["type"] == "ui_stream_data"
            assert frame["stream_id"].startswith("narrative-")
            assert "component_id" not in frame

    async def test_legacy_poll_path_never_carries_component_id(self):
        from orchestrator.orchestrator import Orchestrator, PreparedDispatch

        orch = Orchestrator.__new__(Orchestrator)
        sent = []

        async def record(ws, data):
            sent.append(json.loads(data))
            return True

        orch._safe_send = record
        orch.security_flags = {}
        orch.tool_permissions = types.SimpleNamespace(
            is_tool_allowed=lambda *a, **k: True)
        orch._get_user_id = lambda ws: "alice"
        orch._streamable_tools = {"cpu_load": {
            "agent_id": "sys-1", "kind": "poll",
            "default_interval": 1, "min_interval": 1, "max_interval": 5,
        }}
        orch._MAX_STREAM_SUBSCRIPTIONS = 10
        orch._stream_tasks = {}
        orch._stream_subs = {}
        orch._ws_active_chat = {}
        orch._authorize_and_prepare = AsyncMock(return_value=PreparedDispatch(
            args={},
            stream_params={},
            cap_job_id=None,
            delegation_token=None,
        ))
        orch._execute_with_retry_audited = AsyncMock(return_value=types.SimpleNamespace(
            error=None, ui_components=[{"type": "metric", "value": "0.5"}],
            result={}, correlation_id=None))
        ws = FakeWebSocket()

        await asyncio.wait_for(
            orch._handle_stream_subscribe(ws, {"tool_name": "cpu_load"}),
            timeout=1.0,
        )
        await asyncio.sleep(0.05)
        tasks = list(orch._stream_tasks.get(id(ws), {}).values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        types_seen = {f["type"] for f in sent}
        assert "stream_subscribed" in types_seen
        assert "stream_data" in types_seen
        for frame in sent:
            assert "component_id" not in frame


# ---------------------------------------------------------------------------
# Late-join attach replay — a socket joining mid-stream gets current state
# ---------------------------------------------------------------------------

class TestAttachReplay:
    """Attaching to an existing bridged subscription replays the retained
    content chunk to JUST the attaching socket (spec edge case: a device
    joining mid-stream gets the current component state, not a blank
    placeholder). Unbridged/flag-off attach stays send-free as before."""

    async def _attach_second_ws(self, mgr, deps):
        ws2 = FakeWebSocket()
        deps["sessions"][ws2] = {"sub": "alice"}
        sid, attached = await mgr.subscribe(
            ws=ws2, user_id="alice", chat_id="chat-1",
            tool_name="live_temperature", agent_id="weather",
            params={"latitude": 51.5, "longitude": -0.12},
        )
        return ws2, sid, attached

    async def test_attach_replays_retained_chunk_to_joining_ws_only(self):
        mgr, deps = _make_manager()
        ws1, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(
            _chunk(stream_id, 3, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)
        deps["send_to_ws"].reset_mock()

        ws2, sid, attached = await self._attach_second_ws(mgr, deps)
        assert (sid, attached) == (stream_id, True)

        calls = deps["send_to_ws"].await_args_list
        assert len(calls) == 1, "exactly one replay frame expected"
        assert calls[0].args[0] is ws2
        frame = json.loads(calls[0].args[1])
        assert frame["type"] == "ui_stream_data"
        assert frame["seq"] == 3  # stored seq: the joining ws has no seq state
        assert frame["components"] == [{"type": "metric", "value": "12C"}]
        assert frame["component_id"] == fingerprint(
            "weather", "live_temperature", {"latitude": 51.5, "longitude": -0.12})
        assert frame["terminal"] is False
        # Replay must match the fan-out shape web clients render from.
        assert "html" in frame

    async def test_no_replay_when_nothing_retained(self):
        mgr, deps = _make_manager()
        ws1, stream_id = await _subscribed(mgr, deps)
        deps["send_to_ws"].reset_mock()

        ws2, _, attached = await self._attach_second_ws(mgr, deps)
        assert attached is True
        assert deps["send_to_ws"].await_args_list == []

    async def test_already_attached_ws_gets_no_replay(self):
        mgr, deps = _make_manager()
        ws1, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(
            _chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)
        deps["send_to_ws"].reset_mock()

        sid, attached = await mgr.subscribe(
            ws=ws1, user_id="alice", chat_id="chat-1",
            tool_name="live_temperature", agent_id="weather",
            params={"latitude": 51.5, "longitude": -0.12},
        )
        assert (sid, attached) == (stream_id, True)
        assert deps["send_to_ws"].await_args_list == []

    async def test_flag_off_attach_sends_nothing(self, stream_artifacts_off):
        mgr, deps = _make_manager()
        ws1, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(
            _chunk(stream_id, 1, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)
        deps["send_to_ws"].reset_mock()

        ws2, _, attached = await self._attach_second_ws(mgr, deps)
        assert attached is True
        assert deps["send_to_ws"].await_args_list == []


class TestAttachToChat:
    """Manager half of the load_chat late-join wiring: walk _active for the
    (user, chat), attach the loading socket, report what was attached."""

    async def test_attaches_and_reports_new_socket(self):
        mgr, deps = _make_manager()
        ws1, stream_id = await _subscribed(mgr, deps)
        ws2 = FakeWebSocket()
        deps["sessions"][ws2] = {"sub": "alice"}

        attached = await mgr.attach_to_chat(ws2, "alice", "chat-1")
        assert attached == [(stream_id, "live_temperature")]
        sub = mgr.subscription_for_stream(stream_id)
        assert ws2 in sub.subscribers and ws1 in sub.subscribers
        # Idempotent: a second load of the same chat attaches nothing.
        assert await mgr.attach_to_chat(ws2, "alice", "chat-1") == []

    async def test_other_chat_and_other_user_excluded(self):
        mgr, deps = _make_manager()
        await _subscribed(mgr, deps, chat_id="chat-other")
        ws2 = FakeWebSocket()
        deps["sessions"][ws2] = {"sub": "alice"}
        assert await mgr.attach_to_chat(ws2, "alice", "chat-1") == []

        # Session mismatch is refused outright (defense in depth).
        ws3 = FakeWebSocket()
        deps["sessions"][ws3] = {"sub": "mallory"}
        assert await mgr.attach_to_chat(ws3, "alice", "chat-other") == []

    async def test_replay_retained_sends_only_bridged_retained(self):
        mgr, deps = _make_manager()
        ws1, stream_id = await _subscribed(mgr, deps)
        # Nothing retained yet → no send.
        await mgr.replay_retained(ws1, stream_id)
        assert deps["send_to_ws"].await_args_list == []

        await mgr.handle_agent_chunk(
            _chunk(stream_id, 2, [{"type": "metric", "value": "12C"}]))
        await asyncio.sleep(0.05)
        deps["send_to_ws"].reset_mock()
        await mgr.replay_retained(ws1, stream_id)
        frames = _sent_frames(deps)
        assert len(frames) == 1
        assert frames[0]["seq"] == 2
        assert frames[0]["component_id"].startswith("wc_")
        # Unknown stream: no-op.
        await mgr.replay_retained(ws1, "stream-never-existed")
        assert len(_sent_frames(deps)) == 1


# ---------------------------------------------------------------------------
# Seq continuation across retry/wake + terminal-hook coverage (review fixes)
# ---------------------------------------------------------------------------

class TestSeqContinuation:
    """A fresh agent run restarts seq near 0; the manager must offset it past
    the previous high-water or every client (dedup keyed on stream_id) drops
    the recovered frames."""

    async def test_retry_offsets_new_run_seqs(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        sub = mgr.subscription_for_stream(stream_id)
        for seq in (1, 2, 3):
            await mgr.handle_agent_chunk(_chunk(stream_id, seq, [{"type": "text"}]))
            await asyncio.sleep(0.01)
        assert sub.max_seq_seen == 3

        # Transient error → RECONNECTING; fire the retry immediately.
        await mgr._handle_error(sub, "upstream_unavailable", "blip")
        assert sub.state.value == "reconnecting"
        if sub._retry_handle is not None:
            sub._retry_handle.cancel()
            sub._retry_handle = None
        await mgr._retry(sub)
        assert sub.seq_offset == 3

        # New run restarts at seq 1 — the wire frame must land ABOVE 3.
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "text"}]))
        await asyncio.sleep(0.01)
        data_frames = [f for f in _sent_frames(deps)
                       if f.get("type") == "ui_stream_data" and f.get("seq")]
        assert data_frames[-1]["seq"] == 4

    async def test_terminal_seq_rides_above_high_water(self):
        mgr, deps = _make_manager()
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 7, [{"type": "text"}]))
        await asyncio.sleep(0.01)
        await mgr.handle_agent_end(ToolStreamEnd(request_id="req-1", stream_id=stream_id))
        frames = _sent_frames(deps)
        terminal = [f for f in frames if f.get("terminal")]
        assert terminal and terminal[-1]["seq"] == 8


class TestTerminalHook:
    """FR-011: every terminal transition — including the out-of-band ones no
    agent frame reaches — must offer the subscription to the persist hook."""

    def _hook(self):
        seen = []

        async def hook(sub):
            seen.append((sub.stream_id, sub.state.value, sub.state_reason))

        return hook, seen

    async def test_fail_subscription_fires_hook(self):
        mgr, deps = _make_manager()
        hook, seen = self._hook()
        mgr.terminal_hook = hook
        ws, stream_id = await _subscribed(mgr, deps)
        sub = mgr.subscription_for_stream(stream_id)
        await mgr._fail_subscription(sub, "upstream_unavailable", "dead", retryable=True)
        assert seen == [(stream_id, "failed", "upstream_unavailable")]

    async def test_agent_end_after_dormancy_is_unroutable_no_hook(self):
        # Dormancy cancels the agent run and pops the request mapping, so a
        # straggler ToolStreamEnd must be dropped — the TTL sweep owns the
        # abandoned-content persist instead.
        mgr, deps = _make_manager()
        hook, seen = self._hook()
        mgr.terminal_hook = hook
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.detach(ws)
        await mgr.handle_agent_end(ToolStreamEnd(request_id="req-1", stream_id=stream_id))
        assert seen == []
        assert mgr.subscription_for_stream(stream_id) is not None  # still parked dormant

    async def test_dormant_ttl_eviction_fires_hook(self):
        import orchestrator.stream_manager as sm
        mgr, deps = _make_manager()
        hook, seen = self._hook()
        mgr.terminal_hook = hook
        ws, stream_id = await _subscribed(mgr, deps)
        sub = mgr.subscription_for_stream(stream_id)
        await mgr.detach(ws)
        sub.created_at -= (sm.DORMANT_TTL_SECONDS + 1)
        mgr._sweep_dormant_ttl()
        await asyncio.sleep(0.05)  # hook is scheduled from the sync sweep
        assert seen == [(stream_id, "stopped", "dormant_ttl")]

    async def test_unsubscribe_fires_hook_as_success_terminal(self):
        mgr, deps = _make_manager()
        hook, seen = self._hook()
        mgr.terminal_hook = hook
        ws, stream_id = await _subscribed(mgr, deps)
        await mgr.handle_agent_chunk(_chunk(stream_id, 1, [{"type": "metric"}]))
        await asyncio.sleep(0.01)
        await mgr.unsubscribe(ws, stream_id)
        assert seen == [(stream_id, "stopped", "unsubscribe")]
