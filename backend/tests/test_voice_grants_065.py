"""Least-privilege grant and worker-assignment fencing proofs for Feature 065."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from orchestrator.auth import require_user_id
from orchestrator.livekit_service import LiveKitService, LiveKitSettings
from orchestrator.voice_api import VoiceHttpResult
from orchestrator.voice_api import router as voice_router
from orchestrator.voice_control_binding import (
    VoiceControlBindingError,
    VoiceControlBindingIssuer,
    VoiceControlClaims,
)
from orchestrator.voice_coordinator import (
    FIXED_VOICE_PROFILE,
    ControlProtocolError,
    SessionBindRequest,
    StaleFence,
    WorkerPool,
    WorkerPoolPolicy,
)
from orchestrator.voice_worker_endpoint import (
    CHALLENGE_EXPIRES_HEADER,
    CHALLENGE_ISSUED_HEADER,
    CHALLENGE_NONCE_HEADER,
    UpgradeChallenge,
    WorkerChallengeStore,
    WorkerControlAuthError,
    WorkerControlEndpoint,
    WorkerControlSettings,
)
from shared.watch_ticket import (
    WatchTicketError,
    derive_watch_nonce,
    issue_watch_ticket,
    verify_watch_ticket,
)
from starlette.datastructures import Headers
from voice_agent.control import (
    Challenge,
    ProtocolViolation,
    build_challenge_response_headers,
    parse_session_bind,
)
from voice_agent.session import SessionSupervisor
from voice_agent.watch_bridge import WatchBridgeError, WatchTicketReplayStore

NOW = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
EPOCH = int(NOW.timestamp())
LIVEKIT_KEY = "lk-api-key-never-deliver"
LIVEKIT_SECRET = "lk-api-secret-never-deliver-32-bytes"
CONTROL_SECRET = b"worker-control-secret-at-least-32-bytes"
WATCH_SECRET = b"watch-ticket-secret-at-least-32-bytes"
CLOSURE = "a" * 64
USER_ID = "grant-owner@example.invalid"
WORKER_ID = "voice-worker-a"


def _uuid(number: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | number))


SESSION_ID = _uuid(1)
DEVICE_ID = _uuid(2)
CONNECTION_ID = _uuid(3)
REFRESH_ID = _uuid(4)
CHAT_ID = _uuid(5)


class _TokenIssuer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def issue(self, **claims: Any) -> str:
        self.calls.append(dict(claims))
        return f"join-{claims['identity']}-" + "x" * 40


class _RoomAdmin:
    async def probe_room_service(self) -> None:
        return None

    async def create_room(self, _room_name: str) -> None:
        return None

    async def remove_participant(self, _room_name: str, _identity: str) -> None:
        return None

    async def delete_room(self, _room_name: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _livekit_service() -> tuple[LiveKitService, _TokenIssuer]:
    issuer = _TokenIssuer()
    settings = LiveKitSettings(
        internal_url="https://livekit.internal",
        public_url="wss://voice.example.test",
        api_key=LIVEKIT_KEY,
        api_secret=LIVEKIT_SECRET,
        environment="production",
        grant_ttl_seconds=300,
    )
    return (
        LiveKitService(
            settings,
            token_issuer=issuer,
            admin=_RoomAdmin(),
            clock=lambda: NOW,
        ),
        issuer,
    )


@pytest.mark.asyncio
async def test_client_worker_and_watch_grants_are_separate_and_short_lived() -> None:
    service, issuer = _livekit_service()
    client = service.mint_client_grant(
        grant_id="client-grant-1",
        session_id=SESSION_ID,
        generation=2,
        media_grant_revision=3,
        room_name="voice-room-1",
        participant_identity="client-device-rev-3",
        worker_identity=WORKER_ID,
        issued_at=NOW,
    )
    worker = service.mint_worker_grant(
        revision=4,
        room_name="voice-room-1",
        worker_identity=WORKER_ID,
        issued_at=NOW,
    )

    assert client["expires_at"] == worker["expires_at"] == "2026-08-01T14:05:00Z"
    assert client["join_token"] != worker["join_token"]
    assert issuer.calls[0] == {
        "room_name": "voice-room-1",
        "identity": "client-device-rev-3",
        "issued_at": NOW,
        "ttl_seconds": 300,
        "can_publish": True,
        "can_subscribe": True,
        "can_publish_data": False,
        "can_publish_microphone": True,
    }
    assert issuer.calls[1]["identity"] == WORKER_ID
    assert issuer.calls[1]["can_publish_data"] is True
    assert issuer.calls[1]["can_publish_microphone"] is True
    serialized = repr((service, client, worker))
    assert LIVEKIT_KEY not in serialized
    assert LIVEKIT_SECRET not in serialized
    assert not {"api_key", "api_secret"}.intersection(client)
    assert not {"api_key", "api_secret"}.intersection(worker)

    nonce = derive_watch_nonce(
        WATCH_SECRET,
        user_id=USER_ID,
        session_key=REFRESH_ID,
        generation=2,
        media_grant_revision=3,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
    )
    ticket = issue_watch_ticket(
        WATCH_SECRET,
        user_id=USER_ID,
        session_id=SESSION_ID,
        generation=2,
        media_grant_revision=3,
        worker_identity=WORKER_ID,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        nonce=nonce,
    )
    claims = verify_watch_ticket(
        ticket,
        WATCH_SECRET,
        now=NOW + timedelta(seconds=1),
        expected_worker_identity=WORKER_ID,
    )
    assert claims.subject_digest_sha256 == hashlib.sha256(USER_ID.encode()).hexdigest()
    assert claims.device_id == DEVICE_ID
    assert claims.connection_generation == CONNECTION_ID
    assert claims.expires_at - claims.issued_at == timedelta(minutes=1)
    assert ticket not in repr(claims)
    assert USER_ID not in repr(claims)

    replay = WatchTicketReplayStore()
    await replay.consume(claims, now=NOW + timedelta(seconds=1))
    with pytest.raises(WatchBridgeError, match="ticket_replayed"):
        await replay.consume(claims, now=NOW + timedelta(seconds=2))
    with pytest.raises(WatchTicketError, match="wrong_worker"):
        verify_watch_ticket(
            ticket,
            WATCH_SECRET,
            now=NOW + timedelta(seconds=1),
            expected_worker_identity="voice-worker-b",
        )


def test_user_device_and_connection_scope_cannot_cross_control_binding() -> None:
    issuer = VoiceControlBindingIssuer(b"b" * 32, clock=lambda: NOW)
    issued = issuer.mint(
        subject=USER_ID,
        device_id=DEVICE_ID,
        connection_generation=CONNECTION_ID,
        credential_expires_at=NOW + timedelta(minutes=5),
    )
    assert (
        issuer.verify(
            issued.bearer,
            expected_subject=USER_ID,
            expected_device_id=DEVICE_ID,
            expected_connection_generation=CONNECTION_ID,
        )
        == issued.claims
    )

    wrong_scopes = (
        {
            "expected_subject": "other-owner@example.invalid",
            "expected_device_id": DEVICE_ID,
            "expected_connection_generation": CONNECTION_ID,
        },
        {
            "expected_subject": USER_ID,
            "expected_device_id": _uuid(20),
            "expected_connection_generation": CONNECTION_ID,
        },
        {
            "expected_subject": USER_ID,
            "expected_device_id": DEVICE_ID,
            "expected_connection_generation": _uuid(21),
        },
    )
    for scope in wrong_scopes:
        with pytest.raises(VoiceControlBindingError, match="binding_scope_mismatch"):
            issuer.verify(issued.bearer, **scope)
    assert issued.bearer not in repr(issued)
    assert "b" * 32 not in repr(issuer)


def _policy() -> WorkerPoolPolicy:
    return WorkerPoolPolicy(
        runtime_closure_sha256=CLOSURE,
        heartbeat_interval_seconds=5,
        connection_lease_seconds=10,
        send_timeout_seconds=0.1,
        allow_insecure_livekit_url=True,
    )


def _registration() -> dict[str, Any]:
    return {
        "type": "worker_register",
        "schema_version": "1",
        "message_id": _uuid(30),
        "sequence": 0,
        "sent_at": "2026-08-01T14:00:00Z",
        "worker_identity": WORKER_ID,
        "max_sessions": 2,
        "runtime_closure_sha256": CLOSURE,
        "profile": dict(FIXED_VOICE_PROFILE),
    }


class _WorkerSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class _UpgradeSocket:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.scope = {"query_string": b""}
        self.headers = Headers(headers=headers or {})
        self.denial: Any | None = None
        self.closed: list[tuple[int, str]] = []

    async def send_denial_response(self, response: Any) -> None:
        self.denial = response

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


@pytest.mark.asyncio
async def test_worker_challenge_is_authenticated_single_use_and_no_store() -> None:
    pool = WorkerPool(_policy(), utcnow=lambda: NOW, monotonic=lambda: 100.0)
    settings = WorkerControlSettings(secret=CONTROL_SECRET)
    challenges = WorkerChallengeStore(
        settings,
        epoch_seconds=lambda: EPOCH,
        nonce_factory=lambda: "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
    )
    endpoint = WorkerControlEndpoint(pool, settings, challenges=challenges)

    unauthenticated = _UpgradeSocket()
    await endpoint.handle(unauthenticated)  # type: ignore[arg-type]
    response = unauthenticated.denial
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.body == b""
    assert CONTROL_SECRET.decode() not in repr(response.headers)

    challenge = UpgradeChallenge(
        nonce=response.headers[CHALLENGE_NONCE_HEADER],
        issued_at=int(response.headers[CHALLENGE_ISSUED_HEADER]),
        expires_at=int(response.headers[CHALLENGE_EXPIRES_HEADER]),
    )
    signed = build_challenge_response_headers(
        CONTROL_SECRET,
        WORKER_ID,
        Challenge(challenge.nonce, challenge.issued_at, challenge.expires_at),
        timestamp=EPOCH,
    )
    authenticated = _UpgradeSocket(signed)
    assert endpoint._authenticate(authenticated) == WORKER_ID  # type: ignore[arg-type]
    with pytest.raises(WorkerControlAuthError, match="invalid_challenge"):
        endpoint._authenticate(authenticated)  # type: ignore[arg-type]

    second = WorkerChallengeStore(
        settings,
        epoch_seconds=lambda: EPOCH,
        nonce_factory=lambda: "nonce_BBBBBBBBBBBBBBBBBBBBBBBB",
    )
    endpoint = WorkerControlEndpoint(pool, settings, challenges=second)
    pending = second.issue()
    wrong_identity = build_challenge_response_headers(
        CONTROL_SECRET,
        WORKER_ID,
        Challenge(pending.nonce, pending.issued_at, pending.expires_at),
        timestamp=EPOCH,
    )
    wrong_identity["X-Astral-Voice-Worker"] = "voice-worker-b"
    with pytest.raises(WorkerControlAuthError, match="invalid_authentication"):
        endpoint._authenticate(_UpgradeSocket(wrong_identity))  # type: ignore[arg-type]


def test_client_grant_http_response_is_authenticated_no_store_and_secret_free() -> None:
    from orchestrator.voice_backend import SpeechBackendSelection
    selection = SpeechBackendSelection.from_environ({})
    bearer = "v1." + "a" * 64 + "." + "b" * 43
    client_grant = {
        "transport": "livekit",
        "session_id": SESSION_ID,
        "generation": 1,
        "media_grant_revision": 1,
        "expires_at": "2026-08-01T14:05:00Z",
        "url": "wss://voice.example.test",
        "join_token": "client-room-token-" + "x" * 40,
        "room_name": "voice-room-1",
        "participant_identity": "client-a",
        "worker_identity": WORKER_ID,
    }

    async def create_session(**_kwargs: Any) -> VoiceHttpResult:
        return VoiceHttpResult(
            payload={
                "session": {
                    "session_id": SESSION_ID,
                    "device_id": DEVICE_ID,
                    "generation": 1,
                    "media_grant_revision": 1,
                    "state": "starting",
                },
                "grant": client_grant,
            },
            status_code=201,
        )

    async def publish(**_kwargs: Any) -> None:
        return None

    orchestrator = SimpleNamespace(
        voice_runtime=SimpleNamespace(create_session=create_session,
            speech_backend=selection.value, backend_selection=selection),
        publish_voice_composer_state=publish,
        validate_voice_control_binding=lambda **_kwargs: VoiceControlClaims(
            subject=USER_ID,
            device_id=DEVICE_ID,
            connection_generation=CONNECTION_ID,
            binding_id=_uuid(40),
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        ),
    )
    app = FastAPI()
    app.state.orchestrator = orchestrator
    app.dependency_overrides[require_user_id] = lambda: USER_ID
    app.include_router(voice_router)

    with TestClient(app) as client:
        response = client.post(
            "/api/voice/sessions",
            headers={
                "X-Astral-Device-Id": DEVICE_ID,
                "X-Astral-Connection-Generation": CONNECTION_ID,
                "X-Astral-Voice-Control-Binding": bearer,
            },
            json={
                "device_id": DEVICE_ID,
                "device_kind": "web",
                "visible_chat_id": CHAT_ID,
                "activation_id": _uuid(41),
                "capability": {
                    "has_microphone": True,
                    "has_audio_output": True,
                    "microphone_permission": "authorized",
                    "full_duplex": True,
                    "transport": "livekit",
                },
                "foreground_active": True,
            },
        )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["grant"] == client_grant
    rendered = response.text
    assert LIVEKIT_KEY not in rendered
    assert LIVEKIT_SECRET not in rendered
    assert CONTROL_SECRET.decode() not in rendered


def _request(**changes: Any) -> SessionBindRequest:
    values = {
        "session_id": SESSION_ID,
        "generation": 1,
        "room_name": "voice-room-1",
        "transport": "livekit",
        "media_grant_revision": 1,
        "worker_rtc_grant_revision": 1,
        "client_participant_identity": "client-a",
        "visible_chat_id": CHAT_ID,
        "chat_context_revision": 1,
    }
    values.update(changes)
    return SessionBindRequest(**values)


def _worker_grant(**changes: Any) -> dict[str, Any]:
    values = {
        "revision": 1,
        "livekit_url": "ws://livekit:7880",
        "join_token": "worker-room-token-" + "x" * 40,
        "issued_at": "2026-08-01T14:00:00Z",
        "expires_at": "2026-08-01T14:05:00Z",
        "room_name": "voice-room-1",
        "worker_identity": WORKER_ID,
    }
    values.update(changes)
    return values


@pytest.mark.asyncio
async def test_assignment_fences_room_worker_generation_and_both_revisions() -> None:
    socket = _WorkerSocket()
    pool = WorkerPool(_policy(), utcnow=lambda: NOW, monotonic=lambda: 100.0)
    await pool.register_worker(
        _registration(), socket, authenticated_identity=WORKER_ID
    )
    request = _request()
    reservation = await pool.reserve_session(request)

    invalid_grants = (
        (_worker_grant(room_name="voice-room-2"), "grant_room_mismatch"),
        (_worker_grant(worker_identity="voice-worker-b"), "grant_worker_mismatch"),
        (_worker_grant(revision=2), "grant_revision_mismatch"),
        (
            _worker_grant(expires_at="2026-08-01T13:59:59Z"),
            "grant_expired",
        ),
    )
    for grant, code in invalid_grants:
        with pytest.raises(ControlProtocolError, match=code):
            await pool.deliver_session_bind(reservation, request, grant)

    with pytest.raises(StaleFence, match="stale_bind_request"):
        await pool.deliver_session_bind(
            reservation,
            _request(generation=2),
            _worker_grant(),
        )
    with pytest.raises(StaleFence, match="stale_worker_grant_revision"):
        await pool.deliver_session_bind(
            replace(reservation, worker_rtc_grant_revision=2),
            request,
            _worker_grant(),
        )

    frame = await pool.deliver_session_bind(reservation, request, _worker_grant())
    binding = parse_session_bind(
        frame,
        expected_worker_identity=WORKER_ID,
        now=NOW,
    )
    assert binding.worker_rtc_grant_revision == 1
    assert binding.media_grant_revision == 1
    assert binding.worker_rtc_grant.join_token not in repr(binding)

    wrong_room = {
        **frame,
        "room_name": "voice-room-2",
    }
    with pytest.raises(ProtocolViolation, match="grant_room_mismatch"):
        parse_session_bind(
            wrong_room,
            expected_worker_identity=WORKER_ID,
            now=NOW,
        )
    with pytest.raises(ProtocolViolation, match="worker_identity_mismatch"):
        parse_session_bind(
            frame,
            expected_worker_identity="voice-worker-b",
            now=NOW,
        )

    supervisor = SessionSupervisor(max_sessions=1)
    assert await supervisor.start(binding)
    with pytest.raises(ProtocolViolation, match="generation_mismatch"):
        supervisor.deliver({"session_id": SESSION_ID, "generation": 2})
    with pytest.raises(ProtocolViolation, match="media_grant_revision_mismatch"):
        supervisor.deliver(
            {
                "session_id": SESSION_ID,
                "generation": 1,
                "media_grant_revision": 2,
            }
        )
    await supervisor.end(SESSION_ID, 1, 1, "test")
