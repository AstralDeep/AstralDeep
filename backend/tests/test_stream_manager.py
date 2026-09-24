"""Tests for orchestrator/stream_manager.py's constructable surface: params_hash
canonicalization, compute_backoff ranges, classify_error routing, StreamSubscription
invariants, and the token-revocation sweep.
"""

import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.stream_manager import (
    StreamManager,
    StreamState,
    StreamSubscription,
    classify_error,
    compute_backoff,
    params_hash,
    MAX_RETRY_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
)


class TestParamsHash:
    def test_deterministic_for_same_input(self):
        a = params_hash({"lat": 51.5, "lon": -0.12})
        b = params_hash({"lat": 51.5, "lon": -0.12})
        assert a == b

    def test_canonicalization_order_independent(self):
        a = params_hash({"lat": 51.5, "lon": -0.12})
        b = params_hash({"lon": -0.12, "lat": 51.5})
        assert a == b

    def test_different_values_produce_different_hashes(self):
        a = params_hash({"lat": 51.5, "lon": -0.12})
        b = params_hash({"lat": 52.0, "lon": -0.12})
        assert a != b

    def test_returns_16_hex_chars(self):
        h = params_hash({"k": "v"})
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


class TestComputeBackoff:
    @pytest.mark.parametrize("attempt", [1, 2, 3])
    def test_attempt_within_jitter_range(self, attempt):
        base = RETRY_BACKOFF_SECONDS[attempt - 1]
        for _ in range(20):
            value = compute_backoff(attempt)
            assert base * 0.8 <= value <= base * 1.2

    def test_attempt_4_raises(self):
        with pytest.raises(ValueError):
            compute_backoff(MAX_RETRY_ATTEMPTS + 1)

    def test_attempt_0_raises(self):
        with pytest.raises(ValueError):
            compute_backoff(0)


class TestClassifyError:
    def test_transient_codes(self):
        assert classify_error("tool_error") == "transient"
        assert classify_error("upstream_unavailable") == "transient"
        assert classify_error("rate_limited") == "transient"

    def test_auth_codes_bypass_retry(self):
        assert classify_error("unauthenticated") == "auth"
        assert classify_error("unauthorized") == "auth"

    def test_terminal_codes(self):
        assert classify_error("chunk_too_large") == "terminal"
        assert classify_error("cancelled") == "terminal"

    def test_unknown_code_defaults_transient(self):
        assert classify_error("brand_new_invented_error") == "transient"


class TestStreamManagerConstruct:
    def test_constructs_with_minimal_deps(self):
        rote = Mock()
        send = AsyncMock()
        get_session = Mock(return_value={"sub": "u1"})
        mgr = StreamManager(
            rote=rote, send_to_ws=send, get_user_session=get_session,
        )
        assert mgr._rote is rote
        assert mgr._send_to_ws is send
        assert mgr._get_user_session is get_session
        assert mgr._active == {}
        assert mgr._dormant == {}
        assert mgr._request_to_key == {}
        assert mgr._sweep_task is None

    def test_count_active_for_user_starts_zero(self):
        mgr = StreamManager(
            rote=Mock(), send_to_ws=AsyncMock(),
            get_user_session=Mock(return_value=None),
        )
        assert mgr._count_active_for_user("any-user") == 0
        assert mgr._count_dormant_for_user("any-user") == 0

    def test_validate_params_size_accepts_small(self):
        StreamManager._validate_params_size({"k": "v"})

    def test_validate_params_size_rejects_huge(self):
        big = {"k": "x" * (16 * 1024)}
        with pytest.raises(ValueError, match="exceeds"):
            StreamManager._validate_params_size(big)

    def test_subscribe_works_with_dispatcher(self):
        import asyncio
        mgr = StreamManager(
            rote=Mock(), send_to_ws=AsyncMock(),
            get_user_session=Mock(return_value={"sub": "u1"}),
        )
        ws = Mock()
        loop = asyncio.new_event_loop()
        try:
            stream_id, attached = loop.run_until_complete(
                mgr.subscribe(
                    ws=ws, user_id="u1", chat_id="c1",
                    tool_name="t1", agent_id="a1", params={"x": 1},
                )
            )
            assert stream_id.startswith("stream-")
            assert attached is False
            assert len(mgr._active) == 1
        finally:
            loop.close()

    def test_shutdown_clears_state(self):
        mgr = StreamManager(
            rote=Mock(), send_to_ws=AsyncMock(),
            get_user_session=Mock(return_value=None),
        )
        sub = StreamSubscription(
            stream_id="s1", user_id="u1", chat_id="c1",
            tool_name="t", agent_id="a", params={}, params_hash="h",
            component_id="s1",
        )
        mgr._active[sub.key] = sub
        mgr.shutdown()
        assert mgr._active == {}
        assert mgr._dormant == {}
        assert mgr._shutdown is True


class TestStreamSubscriptionInvariants:
    def _make(self, **overrides):
        defaults = dict(
            stream_id="s1", user_id="u1", chat_id="c1", tool_name="t",
            agent_id="a", params={"x": 1}, params_hash="h", component_id="s1",
        )
        defaults.update(overrides)
        return StreamSubscription(**defaults)

    def test_key_shape(self):
        sub = self._make()
        assert sub.key == ("u1", "c1", "t", "h")

    def test_initial_state_starting(self):
        sub = self._make()
        assert sub.state == StreamState.STARTING
        assert sub.retry_attempt == 0
        assert sub.next_retry_at is None
        assert sub.subscribers == []
        assert sub.delivered_count == 0
        assert sub.dropped_count == 0

    def test_default_chunk_bounds(self):
        sub = self._make()
        assert 1 <= sub.min_fps <= sub.max_fps <= 60
        assert sub.max_chunk_bytes > 0


class TestTokenRevocationSweep:
    @pytest.mark.asyncio
    async def test_expired_token_triggers_unauthenticated_error(self):
        import json
        import time as time_mod

        sessions = {}
        sent = []
        async def send(ws, payload):
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

        class FakeWS:
            pass
        ws = FakeWS()
        sessions[ws] = {"sub": "alice", "expires_at": int(time_mod.time()) + 3600}

        sid, _ = await mgr.subscribe(
            ws=ws, user_id="alice", chat_id="c", tool_name="t",
            agent_id="a", params={},
        )
        sessions[ws]["expires_at"] = int(time_mod.time()) - 60

        await mgr._sweep_token_revocation()

        msgs = [json.loads(p) for w, p in sent]
        auth_errs = [
            m for m in msgs
            if (m.get("error") or {}).get("code") == "unauthenticated"
        ]
        assert len(auth_errs) >= 1
        assert sid not in [s.stream_id for s in mgr._active.values()]
        assert ("alice", "c") in mgr._dormant
