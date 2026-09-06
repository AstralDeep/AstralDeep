"""Feature-066 T030/FR-034 voice status surface tests.

Pins the credential-free projection contract: WorkerPool registry facts,
bounded admission-refusal retention recorded ONLY at the three genuine
refusal exits (never on the healthy challenge-issue leg, never after a
worker was admitted), and the authenticated GET /api/voice/status route.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from orchestrator.auth import require_user_id
from orchestrator.voice_api import router as voice_router
from orchestrator.voice_bootstrap import VoiceServices
from orchestrator.voice_backend import SpeechBackendSelection
from orchestrator.voice_coordinator import (
    FIXED_VOICE_PROFILE,
    AdmissionRefusal,
    SessionBindRequest,
    WorkerPool,
    WorkerPoolPolicy,
    WorkerStatusEntry,
)
from orchestrator.voice_worker_endpoint import (
    CHALLENGE_EXPIRES_HEADER,
    CHALLENGE_ISSUED_HEADER,
    CHALLENGE_NONCE_HEADER,
    WORKER_CONTROL_PATH,
    AdmissionRefusalLog,
    UpgradeChallenge,
    WorkerChallengeStore,
    WorkerControlConfigError,
    WorkerControlSettings,
    install_router,
)
from voice_agent.control import Challenge, build_challenge_response_headers


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
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

    def advance(self, seconds: float) -> None:
        self.utc += timedelta(seconds=seconds)
        self.epoch += int(seconds)
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
        "sent_at": "2026-08-04T12:00:00Z",
        "worker_identity": identity,
        "max_sessions": 2,
        "runtime_closure_sha256": CLOSURE,
        "profile": dict(FIXED_VOICE_PROFILE),
    }


def _bind_request(number: int) -> SessionBindRequest:
    return SessionBindRequest(
        session_id=_uuid(50 + number),
        generation=1,
        room_name=f"room-{number}",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity=f"client-{number}",
        visible_chat_id=_uuid(70 + number),
        chat_context_revision=1,
    )


def _app(
    *,
    clock: Clock | None = None,
    settings: WorkerControlSettings | None = None,
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
        [
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
    )
    return TestClient(app), pool, endpoint, resolved_clock


def _request_challenge(client: TestClient) -> UpgradeChallenge:
    with pytest.raises(WebSocketDenialResponse) as caught:
        with client.websocket_connect(WORKER_CONTROL_PATH):
            pass
    response = caught.value
    assert response.status_code == 401
    return UpgradeChallenge(
        nonce=response.headers[CHALLENGE_NONCE_HEADER],
        issued_at=int(response.headers[CHALLENGE_ISSUED_HEADER]),
        expires_at=int(response.headers[CHALLENGE_EXPIRES_HEADER]),
    )


def _signed_headers(
    challenge: UpgradeChallenge,
    *,
    identity: str = "voice-worker-a",
    secret: bytes = SECRET,
) -> dict[str, str]:
    return build_challenge_response_headers(
        secret,
        identity,
        Challenge(
            challenge.nonce,
            challenge.issued_at,
            challenge.expires_at,
        ),
        timestamp=EPOCH,
    )


# ---------------------------------------------------------------------------
# WorkerPool.worker_status projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_status_projects_registry_facts_and_session_load() -> None:
    clock = Clock()
    pool = WorkerPool(
        _policy(), utcnow=clock.utcnow, monotonic=clock.monotonic
    )
    assert pool.worker_status() == ()

    await pool.register_worker(
        _registration(),
        FakeSocket(),
        authenticated_identity="voice-worker-a",
    )
    entries = pool.worker_status()
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, WorkerStatusEntry)
    assert entry.worker_identity == "voice-worker-a"
    assert entry.accepted_max_sessions == 2
    assert entry.active_sessions == 0
    assert entry.registered_at == NOW

    await pool.reserve_session(_bind_request(1))
    assert pool.worker_status()[0].active_sessions == 1

    # The projection always explains the aggregate readiness() counters.
    readiness = pool.readiness()
    assert readiness.worker_count == len(pool.worker_status())
    assert readiness.capacity_available == sum(
        item.accepted_max_sessions - item.active_sessions
        for item in pool.worker_status()
    )


@pytest.mark.asyncio
async def test_worker_status_drops_lease_expired_connections() -> None:
    clock = Clock()
    pool = WorkerPool(
        _policy(), utcnow=clock.utcnow, monotonic=clock.monotonic
    )
    await pool.register_worker(
        _registration(),
        FakeSocket(),
        authenticated_identity="voice-worker-a",
    )
    assert len(pool.worker_status()) == 1
    clock.advance(7)  # beyond connection_lease_seconds=6
    assert pool.worker_status() == ()


# ---------------------------------------------------------------------------
# AdmissionRefusalLog retention semantics
# ---------------------------------------------------------------------------


def test_refusal_log_is_bounded_newest_first_and_sanitizes() -> None:
    stamps = iter(NOW + timedelta(seconds=index) for index in range(10))
    log = AdmissionRefusalLog(capacity=3, utcnow=lambda: next(stamps))
    for index in range(5):
        log.record("authentication", f"reason_{index}")
    entries = log.snapshot()
    assert [item.reason for item in entries] == [
        "reason_4",
        "reason_3",
        "reason_2",
    ]
    assert all(isinstance(item, AdmissionRefusal) for item in entries)
    assert entries[0].occurred_at > entries[1].occurred_at

    log.record("weird-stage", "NOT A VALID CODE!!")
    latest = log.snapshot()[0]
    assert latest.stage == "registration"
    assert latest.reason == "admission_refused"


def test_refusal_log_rejects_invalid_capacity() -> None:
    with pytest.raises(WorkerControlConfigError):
        AdmissionRefusalLog(capacity=0)
    with pytest.raises(WorkerControlConfigError):
        AdmissionRefusalLog(capacity=65)


def test_auth_spam_cannot_evict_registration_refusals() -> None:
    # The pre-accept authentication path is reachable unauthenticated, so
    # its churn must never rotate a genuine (signed-challenge) registration
    # refusal out of the operator view.
    stamps = iter(NOW + timedelta(seconds=index) for index in range(40))
    log = AdmissionRefusalLog(capacity=3, utcnow=lambda: next(stamps))
    log.record("registration", "closure_mismatch")
    for _index in range(20):
        log.record("authentication", "invalid_challenge")
    entries = log.snapshot()
    registration = [item for item in entries if item.stage == "registration"]
    assert [item.reason for item in registration] == ["closure_mismatch"]
    assert len(entries) == 4  # 3 bounded auth entries + the registration one
    assert entries[0].stage == "authentication"  # newest first across stages
    assert entries[-1].reason == "closure_mismatch"


# ---------------------------------------------------------------------------
# Endpoint records ONLY at the three genuine refusal exits
# ---------------------------------------------------------------------------


def test_healthy_challenge_issue_leg_records_no_refusal() -> None:
    client, _pool, endpoint, _clock = _app()
    _request_challenge(client)
    assert endpoint.admission_refusals() == ()


def test_authentication_refusal_is_recorded_with_reason() -> None:
    client, _pool, endpoint, _clock = _app()
    challenge = _request_challenge(client)
    wrong_secret = b"another-32-byte-secret-that-never-matches!!"
    with pytest.raises(WebSocketDenialResponse) as caught:
        with client.websocket_connect(
            WORKER_CONTROL_PATH,
            headers=_signed_headers(challenge, secret=wrong_secret),
        ):
            pass
    assert caught.value.status_code == 401
    refusals = endpoint.admission_refusals()
    assert len(refusals) == 1
    assert refusals[0].stage == "authentication"
    assert refusals[0].reason == "invalid_authentication"


def test_registration_refusal_is_recorded_with_exact_code() -> None:
    client, _pool, endpoint, _clock = _app()
    challenge = _request_challenge(client)
    registration = _registration()
    registration["runtime_closure_sha256"] = "b" * 64
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(registration))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    refusals = endpoint.admission_refusals()
    assert len(refusals) == 1
    assert refusals[0].stage == "registration"
    assert refusals[0].reason == "closure_mismatch"


def test_registration_timeout_is_recorded() -> None:
    client, _pool, endpoint, _clock = _app(
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
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    refusals = endpoint.admission_refusals()
    assert len(refusals) == 1
    assert refusals[0].stage == "registration"
    assert refusals[0].reason == "registration_timeout"


def test_admitted_worker_protocol_violation_records_no_refusal() -> None:
    client, _pool, endpoint, _clock = _app()
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration()))
        assert socket.receive_json()["type"] == "worker_registered"
        socket.send_text("this is not json")
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert endpoint.admission_refusals() == ()


# ---------------------------------------------------------------------------
# VoiceServices.voice_status projection shape
# ---------------------------------------------------------------------------


class _StatusSelf:
    """Duck-typed self for the pure VoiceServices.voice_status projection."""

    def __init__(self, pool: WorkerPool, endpoint) -> None:
        self.worker_pool = pool
        self.worker_endpoint = endpoint


def test_voice_status_projection_shape_and_stamps() -> None:
    client, pool, endpoint, _clock = _app()
    challenge = _request_challenge(client)
    with client.websocket_connect(
        WORKER_CONTROL_PATH,
        headers=_signed_headers(challenge),
    ) as socket:
        socket.send_text(json.dumps(_registration()))
        assert socket.receive_json()["type"] == "worker_registered"
        endpoint.refusals.record("authentication", "invalid_authentication")

        value = VoiceServices.voice_status(_StatusSelf(pool, endpoint))
        assert value["ready"] is True
        assert value["reason"] == "ready"
        assert value["worker_count"] == 1
        assert value["capacity_total"] == 2
        assert value["capacity_available"] == 2
        assert value["profile"] == dict(FIXED_VOICE_PROFILE)
        assert value["workers"] == [
            {
                "worker_identity": "voice-worker-a",
                "accepted_max_sessions": 2,
                "active_sessions": 0,
                "registered_at": "2026-08-04T12:00:00Z",
            }
        ]
        assert value["recent_refusals"][0]["stage"] == "authentication"
        assert value["recent_refusals"][0]["reason"] == "invalid_authentication"
        assert value["recent_refusals"][0]["occurred_at"].endswith("Z")
        # Credential-free by construction: no token/secret-bearing keys.
        encoded = json.dumps(value)
        for forbidden in ("join_token", "api_key", "api_secret", "credential"):
            assert forbidden not in encoded


def test_voice_status_without_endpoint_has_empty_refusals() -> None:
    clock = Clock()
    pool = WorkerPool(
        _policy(), utcnow=clock.utcnow, monotonic=clock.monotonic
    )
    value = VoiceServices.voice_status(_StatusSelf(pool, None))
    assert value["ready"] is False
    assert value["reason"] == "worker_unavailable"
    assert value["workers"] == []
    assert value["recent_refusals"] == []


# ---------------------------------------------------------------------------
# GET /api/voice/status route
# ---------------------------------------------------------------------------


class _FakeServices:
    def __init__(self) -> None:
        self.calls = 0
        self.backend_selection = SpeechBackendSelection.from_environ({})
        self.speech_backend = self.backend_selection.value

    def voice_status(self) -> dict[str, object]:
        self.calls += 1
        return {
            "ready": False,
            "reason": "worker_unavailable",
            "worker_count": 0,
            "capacity_total": 0,
            "capacity_available": 0,
            "profile": dict(FIXED_VOICE_PROFILE),
            "workers": [],
            "recent_refusals": [
                {
                    "stage": "authentication",
                    "reason": "invalid_authentication",
                    "occurred_at": "2026-08-04T12:00:00Z",
                }
            ],
        }


class _FakeOrchestrator:
    def __init__(self, services) -> None:
        self.voice_services = services
        self.voice_runtime = services


def _status_app(services) -> TestClient:
    app = FastAPI()
    app.state.orchestrator = _FakeOrchestrator(services)
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(voice_router)
    return TestClient(app)


def test_status_route_returns_projection_with_no_store() -> None:
    services = _FakeServices()
    client = _status_app(services)
    response = client.get("/api/voice/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["reason"] == "worker_unavailable"
    assert body["recent_refusals"][0]["reason"] == "invalid_authentication"
    assert services.calls == 1


def test_status_route_requires_voice_services() -> None:
    app = FastAPI()
    app.state.orchestrator = object()

    class _NoServices:
        voice_services = None

    app.state.orchestrator = _NoServices()
    app.dependency_overrides[require_user_id] = lambda: "user-a"
    app.include_router(voice_router)
    client = TestClient(app)
    response = client.get("/api/voice/status")
    assert response.status_code == 503
    assert response.json()["code"] == "voice_unavailable"


def test_status_rate_limit_bucket_is_independent_of_capability() -> None:
    services = _FakeServices()
    client = _status_app(services)
    for _index in range(30):
        assert client.get("/api/voice/status").status_code == 200
    limited = client.get("/api/voice/status")
    assert limited.status_code == 429
    assert limited.json()["code"] == "voice_rate_limited"
    # The capability limiter runs BEFORE its runtime lookup, so with a shared
    # bucket this would 429; the fake lacks get_capability, so passing the
    # limiter surfaces as the 503 runtime refusal instead.
    capability = client.get("/api/voice/capability")
    assert capability.status_code == 503
