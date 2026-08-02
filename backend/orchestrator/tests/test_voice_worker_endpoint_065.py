"""Authenticated worker-control HTTP-upgrade tests for Feature 065."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from orchestrator.voice_coordinator import (
    FIXED_VOICE_PROFILE,
    CapacityUnavailable,
    RegistrationError,
    SessionBindRequest,
    StaleFence,
    WorkerPool,
    WorkerPoolPolicy,
    WorkerRegistrationReceipt,
)
from orchestrator.voice_worker_endpoint import (
    CHALLENGE_EXPIRES_HEADER,
    CHALLENGE_ISSUED_HEADER,
    CHALLENGE_NONCE_HEADER,
    WORKER_CONTROL_PATH,
    UpgradeChallenge,
    WorkerChallengeStore,
    WorkerControlAuthError,
    WorkerControlConfigError,
    WorkerControlSettings,
    install_router,
)
from voice_agent.control import Challenge, build_challenge_response_headers


NOW = datetime(2026, 7, 31, 21, 0, tzinfo=UTC)
EPOCH = int(NOW.timestamp())
CLOSURE = "a" * 64
SECRET = b"voice-control-test-secret-with-32-bytes-minimum"


def _uuid(number: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | number))


class Clock:
    def __init__(self) -> None:
        self.utc = NOW
        self.epoch = EPOCH
        self.mono = 100.0

    def utcnow(self) -> datetime:
        return self.utc

    def epoch_seconds(self) -> int:
        return self.epoch

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: int) -> None:
        self.utc += timedelta(seconds=seconds)
        self.epoch += seconds
        self.mono += seconds


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


def _policy() -> WorkerPoolPolicy:
    return WorkerPoolPolicy(
        runtime_closure_sha256=CLOSURE,
        heartbeat_interval_seconds=5,
        connection_lease_seconds=6,
        send_timeout_seconds=0.1,
        allow_insecure_livekit_url=True,
    )


def _registration(identity: str = "voice-worker-a") -> dict[str, object]:
    return {
        "type": "worker_register",
        "schema_version": "1",
        "message_id": _uuid(1),
        "sequence": 0,
        "sent_at": "2026-07-31T21:00:00Z",
        "worker_identity": identity,
        "max_sessions": 2,
        "runtime_closure_sha256": CLOSURE,
        "profile": dict(FIXED_VOICE_PROFILE),
    }


def _app(
    *,
    clock: Clock | None = None,
    nonces: list[str] | None = None,
    settings: WorkerControlSettings | None = None,
    disconnect_hook=None,
    frame_hook=None,
):
    resolved_clock = clock or Clock()
    pool = WorkerPool(
        _policy(),
        utcnow=resolved_clock.utcnow,
        monotonic=resolved_clock.monotonic,
    )
    resolved_settings = settings or WorkerControlSettings(
        secret=SECRET,
        lease_sweep_seconds=0.1,
    )
    nonce_values = iter(
        nonces
        or [
            "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
            "nonce_BBBBBBBBBBBBBBBBBBBBBBBB",
            "nonce_CCCCCCCCCCCCCCCCCCCCCCCC",
        ]
    )
    challenges = WorkerChallengeStore(
        resolved_settings,
        epoch_seconds=resolved_clock.epoch_seconds,
        nonce_factory=lambda: next(nonce_values),
    )
    app = FastAPI()
    endpoint = install_router(
        app,
        pool,
        settings=resolved_settings,
        challenges=challenges,
        disconnect_hook=disconnect_hook,
        frame_hook=frame_hook,
    )
    return TestClient(app), pool, endpoint, resolved_clock


def _request_challenge(client: TestClient) -> UpgradeChallenge:
    with pytest.raises(WebSocketDenialResponse) as caught:
        with client.websocket_connect(WORKER_CONTROL_PATH):
            pass
    response = caught.value
    assert response.status_code == 401
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    return UpgradeChallenge(
        nonce=response.headers[CHALLENGE_NONCE_HEADER],
        issued_at=int(response.headers[CHALLENGE_ISSUED_HEADER]),
        expires_at=int(response.headers[CHALLENGE_EXPIRES_HEADER]),
    )


def _signed_headers(
    challenge: UpgradeChallenge,
    *,
    identity: str = "voice-worker-a",
) -> dict[str, str]:
    return build_challenge_response_headers(
        SECRET,
        identity,
        Challenge(
            challenge.nonce,
            challenge.issued_at,
            challenge.expires_at,
        ),
        timestamp=EPOCH,
    )


def test_upgrade_challenge_interoperates_with_worker_and_unregisters() -> None:
    client, pool, endpoint, _clock = _app()
    challenge = _request_challenge(client)

    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration()))
        registered = socket.receive_json()
        assert registered["type"] == "worker_registered"
        assert registered["worker_identity"] == "voice-worker-a"
        assert registered["accepted_max_sessions"] == 2
        assert endpoint.readiness().ready is True

    assert pool.readiness().ready is False
    assert pool.readiness().worker_count == 0


def test_disconnect_hook_receives_credential_free_cleanup_fence() -> None:
    cleanups: list[tuple[str, tuple[str, ...]]] = []
    cleaned = threading.Event()

    async def hook(receipt, released: tuple[str, ...]) -> None:
        cleanups.append((receipt.worker_identity, released))
        cleaned.set()

    client, _pool, _endpoint, _clock = _app(disconnect_hook=hook)
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration()))
        assert socket.receive_json()["type"] == "worker_registered"

    assert cleaned.wait(timeout=1)
    assert cleanups == [("voice-worker-a", ())]


@pytest.mark.asyncio
async def test_replaced_assignment_fences_are_reconciled_before_receive_loop() -> None:
    cleanups: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    async def hook(receipt, released: tuple[str, ...]) -> None:
        cleanups.append(
            (receipt.worker_identity, receipt.fenced_assignments, released)
        )

    _client, _pool, endpoint, _clock = _app(disconnect_hook=hook)
    receipt = WorkerRegistrationReceipt(
        connection_id=_uuid(40),
        worker_identity="voice-worker-a",
        accepted_max_sessions=2,
        fenced_assignments=(_uuid(41),),
    )

    await endpoint._reconcile_registration_fences(receipt)

    assert cleanups == [("voice-worker-a", (_uuid(41),), ())]


@pytest.mark.asyncio
async def test_lease_sweep_delivers_exact_releases_before_unregister_noop() -> None:
    cleanups: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    async def hook(receipt, released: tuple[str, ...]) -> None:
        cleanups.append(
            (receipt.worker_identity, receipt.fenced_assignments, released)
        )

    class SilentWebSocket:
        async def receive(self):
            await asyncio.Event().wait()

    _client, pool, endpoint, clock = _app(disconnect_hook=hook)
    receipt = await pool.register_worker(
        _registration(),
        FakeSocket(),
        authenticated_identity="voice-worker-a",
    )
    request = SessionBindRequest(
        session_id=_uuid(50),
        generation=1,
        room_name="room-a",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-a",
        visible_chat_id=_uuid(51),
        chat_context_revision=1,
    )
    reservation = await pool.reserve_session(request)
    clock.advance(7)

    await endpoint._run_registered(SilentWebSocket(), receipt)  # type: ignore[arg-type]

    assert cleanups == [
        (
            "voice-worker-a",
            (reservation.assignment_id,),
            (request.session_id,),
        )
    ]
    assert await pool.unregister_worker(receipt.connection_id) == ()


@pytest.mark.asyncio
async def test_bad_session_frame_is_quarantined_without_worker_reconnect() -> None:
    cleanups: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    observed: list[str] = []

    async def disconnect_hook(receipt, released: tuple[str, ...]) -> None:
        cleanups.append((receipt.fenced_assignments, released))

    async def frame_hook(_receipt, frame) -> None:
        observed.append(frame["session_id"])

    class QueuedWebSocket:
        def __init__(self, events: list[dict[str, object]]) -> None:
            self.events = iter(events)

        async def receive(self):
            return next(self.events)

    _client, pool, endpoint, _clock = _app(
        disconnect_hook=disconnect_hook,
        frame_hook=frame_hook,
    )
    control_socket = FakeSocket()
    receipt = await pool.register_worker(
        _registration(),
        control_socket,
        authenticated_identity="voice-worker-a",
    )
    bad_request = SessionBindRequest(
        session_id=_uuid(60),
        generation=1,
        room_name="room-bad",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-bad",
        visible_chat_id=_uuid(61),
        chat_context_revision=1,
    )
    healthy_request = SessionBindRequest(
        session_id=_uuid(62),
        generation=1,
        room_name="room-healthy",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-healthy",
        visible_chat_id=_uuid(63),
        chat_context_revision=1,
    )
    bad = await pool.reserve_session(bad_request)
    healthy = await pool.reserve_session(healthy_request)
    bad_frame = {
        "type": "heartbeat",
        "schema_version": "1",
        "message_id": _uuid(64),
        "session_id": bad.session_id,
        "generation": 1,
        "sequence": 0,
        "sent_at": "2026-07-31T21:00:00Z",
        "media_state": "ready",
        "unexpected": True,
    }
    healthy_frame = {
        "type": "heartbeat",
        "schema_version": "1",
        "message_id": _uuid(65),
        "session_id": healthy.session_id,
        "generation": 1,
        "sequence": 0,
        "sent_at": "2026-07-31T21:00:00Z",
        "media_state": "ready",
    }
    websocket = QueuedWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps(bad_frame),
                "bytes": None,
            },
            {
                "type": "websocket.receive",
                "text": json.dumps(healthy_frame),
                "bytes": None,
            },
            {"type": "websocket.disconnect", "code": 1000, "reason": "done"},
        ]
    )

    with pytest.raises(WebSocketDisconnect):
        await endpoint._run_registered(websocket, receipt)  # type: ignore[arg-type]

    with pytest.raises(StaleFence, match="stale_assignment"):
        pool.assignment_snapshot(bad.session_id)
    peer = pool.assignment_snapshot(healthy.session_id)
    assert peer.next_incoming_sequence == 1
    assert peer.next_outgoing_sequence == 0
    assert observed == [healthy.session_id]
    assert cleanups == [((bad.assignment_id,), (bad.session_id,))]
    assert json.loads(control_socket.sent[-1])["type"] == "end_session"
    assert control_socket.closed == []
    assert pool.readiness().worker_count == 1
    assert not await pool.release_attributable_worker_frame_reservation(bad)
    assert pool.assignment_snapshot(healthy.session_id) == peer


@pytest.mark.asyncio
async def test_stale_quarantine_fence_cannot_release_rebound_assignment() -> None:
    _client, pool, _endpoint, _clock = _app()
    first_socket = FakeSocket()
    await pool.register_worker(
        _registration(),
        first_socket,
        authenticated_identity="voice-worker-a",
    )
    first_request = SessionBindRequest(
        session_id=_uuid(70),
        generation=1,
        room_name="room-a",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-a",
        visible_chat_id=_uuid(71),
        chat_context_revision=1,
    )
    stale = await pool.reserve_session(first_request)
    refreshed_request = SessionBindRequest(
        session_id=first_request.session_id,
        generation=first_request.generation,
        room_name=first_request.room_name,
        transport=first_request.transport,
        media_grant_revision=first_request.media_grant_revision,
        worker_rtc_grant_revision=2,
        client_participant_identity=first_request.client_participant_identity,
        visible_chat_id=first_request.visible_chat_id,
        chat_context_revision=first_request.chat_context_revision,
    )
    refreshed = await pool.reserve_session(refreshed_request)

    assert not await pool.release_attributable_worker_frame_reservation(stale)
    assert pool.assignment_snapshot(stale.session_id).worker_rtc_grant_revision == 2

    replacement_socket = FakeSocket()
    await pool.register_worker(
        _registration(),
        replacement_socket,
        authenticated_identity="voice-worker-a",
    )
    replacement = await pool.reserve_session(refreshed_request)

    assert replacement.connection_id != refreshed.connection_id
    assert replacement.assignment_id == refreshed.assignment_id
    assert not await pool.release_attributable_worker_frame_reservation(refreshed)
    assert pool.assignment_snapshot(replacement.session_id).connection_id == (
        replacement.connection_id
    )


def test_invalid_signature_consumes_challenge_and_replay_never_upgrades() -> None:
    client, pool, _endpoint, _clock = _app()
    challenge = _request_challenge(client)
    invalid = _signed_headers(challenge)
    invalid["X-Astral-Voice-Signature"] = "0" * 64

    for headers in (invalid, _signed_headers(challenge)):
        with pytest.raises(WebSocketDenialResponse) as caught:
            with client.websocket_connect(WORKER_CONTROL_PATH, headers=headers):
                pass
        assert caught.value.status_code == 401
        assert CHALLENGE_NONCE_HEADER not in caught.value.headers
        assert SECRET.decode() not in repr(caught.value.headers)
    assert pool.readiness().worker_count == 0


@pytest.mark.parametrize(
    "path",
    (
        WORKER_CONTROL_PATH + "?secret=must-not-reflect",
        WORKER_CONTROL_PATH + "?X-Astral-Voice-Signature=must-not-reflect",
    ),
)
def test_query_credentials_are_rejected_without_challenge_or_reflection(
    path: str,
) -> None:
    client, _pool, endpoint, _clock = _app()
    with pytest.raises(WebSocketDenialResponse) as caught:
        with client.websocket_connect(path):
            pass
    response = caught.value
    assert response.status_code == 400
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert CHALLENGE_NONCE_HEADER not in response.headers
    assert "must-not-reflect" not in repr(response.headers)
    assert endpoint.challenges.retained_count == 0


def test_authenticated_identity_must_equal_registration_identity() -> None:
    client, pool, _endpoint, _clock = _app()
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration("voice-worker-b")))
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code == 1008
        assert caught.value.reason == "protocol_violation"
    assert pool.readiness().worker_count == 0


@pytest.mark.parametrize(
    "send_invalid",
    (
        lambda socket: socket.send_bytes(b"{}"),
        lambda socket: socket.send_text("x" * (15 * 1024 + 1)),
        lambda socket: socket.send_text(
            '{"type":"worker_register","type":"worker_register"}'
        ),
    ),
)
def test_first_frame_is_text_bounded_and_duplicate_key_free(send_invalid) -> None:
    client, pool, _endpoint, _clock = _app()
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        send_invalid(socket)
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code == 1008
    assert pool.readiness().worker_count == 0


def test_post_registration_wrong_direction_is_closed_by_worker_pool() -> None:
    client, pool, _endpoint, _clock = _app()
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration()))
        assert socket.receive_json()["type"] == "worker_registered"
        socket.send_text(json.dumps({"type": "session_bind"}))
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code == 1008
        assert caught.value.reason == "protocol_violation"
    assert pool.readiness().worker_count == 0


def test_partial_or_ambient_upgrade_credentials_fail_without_nonce_issue() -> None:
    for headers in (
        {"X-Astral-Voice-Worker": "voice-worker-a"},
        {"Authorization": "Bearer must-not-reflect"},
        {"Cookie": "secret=must-not-reflect"},
    ):
        client, _pool, endpoint, _clock = _app()
        with pytest.raises(WebSocketDenialResponse) as caught:
            with client.websocket_connect(
                WORKER_CONTROL_PATH,
                headers=headers,
            ):
                pass
        assert caught.value.status_code == 401
        assert caught.value.content == b""
        assert CHALLENGE_NONCE_HEADER not in caught.value.headers
        assert "must-not-reflect" not in repr(caught.value.headers)
        assert endpoint.challenges.retained_count == 0


def test_challenge_capacity_returns_empty_no_store_503() -> None:
    client, _pool, endpoint, _clock = _app(
        settings=WorkerControlSettings(
            secret=SECRET,
            challenge_capacity=1,
            lease_sweep_seconds=0.1,
        )
    )
    endpoint.challenges.issue()
    with pytest.raises(WebSocketDenialResponse) as caught:
        with client.websocket_connect(WORKER_CONTROL_PATH):
            pass
    assert caught.value.status_code == 503
    assert caught.value.content == b""
    assert caught.value.headers["cache-control"] == "no-store"
    assert CHALLENGE_NONCE_HEADER not in caught.value.headers


def test_registration_timeout_closes_without_registering() -> None:
    client, pool, _endpoint, _clock = _app(
        settings=WorkerControlSettings(
            secret=SECRET,
            registration_timeout_seconds=0.1,
            lease_sweep_seconds=0.1,
        )
    )
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code == 1008
        assert caught.value.reason == "registration_timeout"
    assert pool.readiness().worker_count == 0


def test_registered_connection_is_swept_when_its_lease_expires() -> None:
    client, pool, _endpoint, clock = _app()
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration()))
        assert socket.receive_json()["type"] == "worker_registered"
        clock.advance(7)
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code == 4000
        assert caught.value.reason == "lease_expired"
    assert pool.readiness().worker_count == 0


def test_registered_idle_connection_stays_live_on_pool_heartbeats() -> None:
    observed: list[dict[str, object]] = []
    received = threading.Event()

    async def hook(_receipt, frame: dict[str, object]) -> None:
        observed.append(frame)
        received.set()

    client, pool, _endpoint, clock = _app(frame_hook=hook)
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration()))
        registered = socket.receive_json()
        for sequence in (1, 2):
            clock.advance(5)
            received.clear()
            socket.send_text(
                json.dumps(
                    {
                        "type": "pool_heartbeat",
                        "schema_version": "1",
                        "message_id": _uuid(40 + sequence),
                        "sequence": sequence,
                        "sent_at": "2026-07-31T21:00:05Z",
                        "worker_identity": "voice-worker-a",
                        "connection_id": registered["connection_id"],
                    }
                )
            )
            assert received.wait(timeout=1)
            assert pool.readiness().ready is True

    assert [frame["type"] for frame in observed] == [
        "pool_heartbeat",
        "pool_heartbeat",
    ]


def test_challenge_store_is_bounded_single_use_and_eagerly_expires() -> None:
    clock = Clock()
    settings = WorkerControlSettings(
        secret=SECRET,
        challenge_ttl_seconds=2,
        challenge_capacity=1,
    )
    nonces = iter(
        (
            "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
            "nonce_BBBBBBBBBBBBBBBBBBBBBBBB",
        )
    )
    store = WorkerChallengeStore(
        settings,
        epoch_seconds=clock.epoch_seconds,
        nonce_factory=lambda: next(nonces),
    )
    first = store.issue()
    assert repr(first) == "UpgradeChallenge(<redacted>)"
    with pytest.raises(WorkerControlAuthError, match="challenge_capacity_exhausted"):
        store.issue()
    assert store.consume(first.nonce) is first
    with pytest.raises(WorkerControlAuthError, match="invalid_challenge"):
        store.consume(first.nonce)

    second = store.issue()
    clock.advance(3)
    with pytest.raises(WorkerControlAuthError, match="expired_challenge"):
        store.consume(second.nonce)
    assert store.retained_count == 0


def test_challenge_store_rejects_bad_generation_clock_and_future_use() -> None:
    clock = Clock()
    settings = WorkerControlSettings(secret=SECRET, challenge_ttl_seconds=2)
    invalid = WorkerChallengeStore(
        settings,
        epoch_seconds=clock.epoch_seconds,
        nonce_factory=lambda: "bad",
    )
    with pytest.raises(WorkerControlAuthError, match="challenge_generation_failed"):
        invalid.issue()
    with pytest.raises(WorkerControlAuthError, match="invalid_challenge"):
        invalid.consume("bad")

    store = WorkerChallengeStore(
        settings,
        epoch_seconds=clock.epoch_seconds,
        nonce_factory=iter(
            (
                "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
                "nonce_BBBBBBBBBBBBBBBBBBBBBBBB",
                "nonce_CCCCCCCCCCCCCCCCCCCCCCCC",
            )
        ).__next__,
    )
    first = store.issue()
    clock.epoch -= 6
    with pytest.raises(WorkerControlAuthError, match="invalid_challenge"):
        store.consume(first.nonce)
    clock.epoch = EPOCH
    store.issue()
    clock.advance(3)
    assert store.issue().nonce == "nonce_CCCCCCCCCCCCCCCCCCCCCCCC"
    assert store.retained_count == 1

    broken_clock = WorkerChallengeStore(
        settings,
        epoch_seconds=lambda: True,
    )
    with pytest.raises(WorkerControlAuthError, match="invalid_challenge_clock"):
        broken_clock.issue()


@pytest.mark.asyncio
async def test_unregister_is_idempotent_and_releases_assignment_lease() -> None:
    clock = Clock()
    pool = WorkerPool(
        _policy(),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic,
    )
    receipt = await pool.register_worker(
        _registration(),
        FakeSocket(),
        authenticated_identity="voice-worker-a",
    )
    request = SessionBindRequest(
        session_id=_uuid(20),
        generation=1,
        room_name="room-a",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-a",
        visible_chat_id=_uuid(21),
        chat_context_revision=1,
    )
    await pool.reserve_session(request)

    assert await pool.unregister_worker(receipt.connection_id) == (request.session_id,)
    assert await pool.unregister_worker(receipt.connection_id) == ()
    with pytest.raises(StaleFence, match="stale_assignment"):
        pool.assignment_snapshot(request.session_id)


@pytest.mark.asyncio
async def test_pool_shutdown_fences_sockets_assignments_and_future_admission() -> None:
    clock = Clock()
    pool = WorkerPool(
        _policy(),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic,
    )
    first_socket = FakeSocket()
    second_socket = FakeSocket()
    await pool.register_worker(
        _registration("voice-worker-a"),
        first_socket,
        authenticated_identity="voice-worker-a",
    )
    await pool.register_worker(
        {**_registration("voice-worker-b"), "message_id": _uuid(2)},
        second_socket,
        authenticated_identity="voice-worker-b",
    )
    request = SessionBindRequest(
        session_id=_uuid(30),
        generation=1,
        room_name="room-a",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-a",
        visible_chat_id=_uuid(31),
        chat_context_revision=1,
    )
    await pool.reserve_session(request)

    assert await pool.shutdown() == (request.session_id,)
    assert await pool.shutdown() == ()
    assert first_socket.closed == [(1001, "coordinator_shutdown")]
    assert second_socket.closed == [(1001, "coordinator_shutdown")]
    assert pool.readiness().worker_count == 0
    with pytest.raises(RegistrationError, match="worker_pool_closed"):
        await pool.register_worker(
            _registration(),
            FakeSocket(),
            authenticated_identity="voice-worker-a",
        )
    with pytest.raises(CapacityUnavailable, match="worker_pool_closed"):
        await pool.reserve_session(request)


def test_settings_never_render_secret() -> None:
    configured = WorkerControlSettings(secret=SECRET)
    assert SECRET.decode() not in repr(configured)
    loaded = WorkerControlSettings.from_environ(
        {"VOICE_CONTROL_SECRET": SECRET.decode()}
    )
    assert loaded.secret == SECRET
    with pytest.raises(WorkerControlConfigError, match="missing_control_secret"):
        WorkerControlSettings.from_environ({})


@pytest.mark.parametrize(
    "values",
    (
        {"secret": b"short"},
        {"secret": SECRET, "challenge_ttl_seconds": 31},
        {"secret": SECRET, "challenge_capacity": 0},
        {"secret": SECRET, "registration_timeout_seconds": float("inf")},
        {"secret": SECRET, "lease_sweep_seconds": 0},
    ),
)
def test_settings_bounds_fail_closed(values: dict[str, object]) -> None:
    with pytest.raises(WorkerControlConfigError):
        WorkerControlSettings(**values)


def test_error_codes_cannot_echo_untrusted_detail() -> None:
    error = WorkerControlAuthError("SECRET endpoint detail")
    assert error.code == "worker_control_error"
    assert "SECRET" not in str(error)
