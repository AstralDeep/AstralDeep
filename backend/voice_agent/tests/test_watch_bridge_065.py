"""Strict, bounded Watch PCM bridge tests for Feature 065."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from voice_agent.watch_bridge import (
    WATCH_CAPTURE_BYTES,
    WATCH_HEADER,
    WATCH_MAX_RATE_FRAMES,
    WATCH_MICROPHONE_KIND,
    WatchBridgeError,
    WatchBridgeServer,
    WatchPcmFrame,
    WatchTicketReplayStore,
    _IngressFence,
)
from voice_agent.watch_ticket import (
    derive_watch_nonce,
    issue_watch_ticket,
    verify_watch_ticket,
)


NOW = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
SECRET = b"watch-ticket-test-secret-that-is-long-enough"
SESSION_ID = "00000000-0000-4000-8000-000000000201"
DEVICE_ID = "00000000-0000-4000-8000-000000000202"
CONNECTION_ID = "00000000-0000-4000-8000-000000000203"
ACTIVATION_ID = "00000000-0000-4000-8000-000000000204"
WORKER_ID = "voice-worker-a"


def _claims():
    nonce = derive_watch_nonce(
        SECRET,
        user_id="owner",
        session_key=ACTIVATION_ID,
        generation=1,
        media_grant_revision=1,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
    )
    ticket = issue_watch_ticket(
        SECRET,
        user_id="owner",
        session_id=SESSION_ID,
        generation=1,
        media_grant_revision=1,
        worker_identity=WORKER_ID,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        nonce=nonce,
    )
    return ticket, verify_watch_ticket(
        ticket,
        SECRET,
        now=NOW,
        expected_worker_identity=WORKER_ID,
    )


def _frame(sequence: int, timestamp_us: int) -> WatchPcmFrame:
    return WatchPcmFrame(
        kind=WATCH_MICROPHONE_KIND,
        sequence=sequence,
        timestamp_us=timestamp_us,
        payload=b"\0" * WATCH_CAPTURE_BYTES,
    )


def test_advc_microphone_frame_round_trips_with_exact_size() -> None:
    frame = _frame(0, 0)
    encoded = frame.encode()
    assert len(encoded) == WATCH_HEADER.size + WATCH_CAPTURE_BYTES
    parsed = WatchPcmFrame.parse_microphone(encoded)
    assert parsed.sequence == 0
    assert parsed.timestamp_us == 0
    assert parsed.payload == b"\0" * WATCH_CAPTURE_BYTES


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: b"FAIL" + value[4:],
        lambda value: value[:-1],
        lambda value: value[:6] + b"\x00\x01" + value[8:],
        lambda value: value[:5] + b"\x02" + value[6:],
    ),
)
def test_advc_parser_rejects_header_kind_flags_and_size(mutate) -> None:
    with pytest.raises(WatchBridgeError):
        WatchPcmFrame.parse_microphone(mutate(_frame(0, 0).encode()))


def test_ingress_fence_rejects_sequence_timestamp_and_rate_abuse() -> None:
    clock = [0.0]
    fence = _IngressFence(monotonic=lambda: clock[0])
    fence.accept(_frame(0, 0))
    with pytest.raises(WatchBridgeError, match="audio_sequence_gap"):
        fence.accept(_frame(2, 20_000))

    fence = _IngressFence(monotonic=lambda: clock[0])
    fence.accept(_frame(0, 0))
    with pytest.raises(WatchBridgeError, match="invalid_audio_timestamp"):
        fence.accept(_frame(1, 0))

    fence = _IngressFence(monotonic=lambda: clock[0])
    for sequence in range(WATCH_MAX_RATE_FRAMES):
        fence.accept(_frame(sequence, sequence * 20_000))
    with pytest.raises(WatchBridgeError, match="audio_rate_exceeded"):
        fence.accept(
            _frame(WATCH_MAX_RATE_FRAMES, WATCH_MAX_RATE_FRAMES * 20_000)
        )


@pytest.mark.asyncio
async def test_ticket_nonce_is_consumed_once_and_pruned_after_expiry() -> None:
    _, claims = _claims()
    store = WatchTicketReplayStore(capacity=1)
    await store.consume(claims, now=NOW)
    assert store.retained_count == 1
    with pytest.raises(WatchBridgeError, match="ticket_replayed"):
        await store.consume(claims, now=NOW)
    with pytest.raises(WatchBridgeError, match="ticket_expired"):
        await store.consume(claims, now=claims.expires_at)
    assert store.retained_count == 0


class _Socket:
    def __init__(self, authorization: str, messages: list[str | bytes]) -> None:
        self.request = SimpleNamespace(
            path="/api/voice/watch-bridge",
            headers={"Authorization": authorization},
        )
        self.messages = iter(messages)
        self.sent: list[str | bytes] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        return next(self.messages)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class _Runtime:
    def __init__(self) -> None:
        self.attached = 0
        self.detached = 0
        self.frames: list[WatchPcmFrame] = []

    async def attach_bridge(self, _socket, _claims) -> None:
        self.attached += 1

    async def detach_bridge(self, _socket, _reason: str) -> None:
        self.detached += 1

    async def feed_microphone_frame(self, _socket, frame: WatchPcmFrame) -> None:
        self.frames.append(frame)

    async def interrupt_from_bridge(self) -> None:
        return None


@pytest.mark.asyncio
async def test_server_authenticates_exact_assignment_and_relays_only_pcm() -> None:
    ticket, _ = _claims()
    runtime = _Runtime()

    class Supervisor:
        def watch_session(self, **fences):
            assert fences == {
                "session_id": SESSION_ID,
                "generation": 1,
                "media_grant_revision": 1,
            }
            return runtime

    socket = _Socket("Bearer " + ticket, [_frame(0, 0).encode()])
    server = WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7890,
        utcnow=lambda: NOW,
    )
    await server.handle(socket)
    assert runtime.attached == 1
    assert runtime.detached == 1
    assert [frame.sequence for frame in runtime.frames] == [0]
    assert socket.closed == [(1008, "bridge_error")]


@pytest.mark.asyncio
async def test_server_rejects_origin_and_replayed_ticket_before_assignment() -> None:
    ticket, _ = _claims()

    class Supervisor:
        def watch_session(self, **_fences):
            raise AssertionError("unauthorized ticket reached assignment lookup")

    server = WatchBridgeServer(
        supervisor=Supervisor(),
        secret=SECRET,
        worker_identity=WORKER_ID,
        host="127.0.0.1",
        port=7890,
        utcnow=lambda: NOW,
    )
    first = _Socket("Bearer " + ticket, [])
    first.request.headers["Origin"] = "https://example.invalid"
    await server.handle(first)
    assert first.closed == [(1008, "origin_rejected")]

    await server._authenticate(_Socket("Bearer " + ticket, []))
    with pytest.raises(WatchBridgeError, match="ticket_replayed"):
        await server._authenticate(_Socket("Bearer " + ticket, []))
