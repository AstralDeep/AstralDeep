"""Boundary tests for the Feature 065 direct-RTC worker control plane."""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import hmac
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import voice_agent.control as control_module
import voice_agent.main as main_module
from voice_agent.config import ConfigError, WorkerConfig
from voice_agent.control import (
    Challenge,
    ChallengeError,
    ChallengeReplayWindow,
    ChallengeRequired,
    FrameRateLimiter,
    PoolClient,
    PoolConnectionError,
    ProtocolViolation,
    WebsocketsPoolConnector,
    build_challenge_response_headers,
    decode_control_frame,
    parse_session_bind,
    sign_challenge,
    verify_challenge_response,
)
from voice_agent.main import (
    ForbiddenRuntimeImport,
    RuntimeImportGuard,
    assert_runtime_distributions,
)
from voice_agent.session import (
    MAX_CLOSED_SESSION_FENCES,
    AssignmentConflict,
    BoundControlSession,
    CapacityExceeded,
    ClosedSessionRace,
    SessionSupervisor,
)
from voice_agent.speech_adapters import SpeechPreflightError
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

ASR_MODEL = "Systran/faster-whisper-large-v3"
TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX"
NOW = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)
SECRET = b"voice-control-test-secret-with-32-bytes-minimum"
SHA256 = "a" * 64


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _uuid(value: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | value))


def _profile() -> dict[str, Any]:
    return {
        "asr_model": ASR_MODEL,
        "tts_model": TTS_MODEL,
        "voice": "af_heart",
        "output_locale": "en-US",
        "format": "wav",
        "sample_rate_hz": 24000,
    }


def _valid_environment(**overrides: str) -> dict[str, str]:
    values = {
        "ASTRAL_ENV": "production",
        "ASTRAL_VOICE_CONTROL_URL": "wss://control.internal/api/voice/control",
        "VOICE_CONTROL_SECRET": SECRET.decode(),
        "VOICE_WORKER_IDENTITY": "voice-worker-a",
        "VOICE_WORKER_MAX_SESSIONS": "2",
        "VOICE_WORKER_CLOSURE_SHA256": SHA256,
        "VOICE_SPEECH_BASE_URL": "https://speech.internal/v1",
        "VOICE_SPEECH_API_KEY": "speech-secret-value",
        "OPENAI_BASE_URL": "",
        "OPENAI_API_KEY": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
    }
    values.update(overrides)
    return values


def _bind_frame(
    *,
    session_number: int = 10,
    assignment_number: int = 11,
    sequence: int = 0,
    generation: int = 1,
    worker_revision: int = 1,
    worker_identity: str = "voice-worker-a",
    room_name: str = "room-a",
    grant_room_name: str | None = None,
    grant_worker_identity: str | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    issued = issued_at or NOW - timedelta(seconds=1)
    expires = expires_at or NOW + timedelta(minutes=4)
    return {
        "type": "session_bind",
        "schema_version": "1",
        "message_id": _uuid(1_000 + session_number + sequence),
        "session_id": _uuid(session_number),
        "generation": generation,
        "sequence": sequence,
        "sent_at": _iso(NOW),
        "assignment_id": _uuid(assignment_number),
        "room_name": room_name,
        "worker_identity": worker_identity,
        "transport": "livekit",
        "media_grant_revision": 1,
        "worker_rtc_grant_revision": worker_revision,
        "client_participant_identity": "client-a",
        "grant_expires_at": _iso(expires),
        "worker_rtc_grant": {
            "revision": worker_revision,
            "livekit_url": "wss://livekit.internal",
            "join_token": "jwt-memory-only-" + "x" * 32,
            "issued_at": _iso(issued),
            "expires_at": _iso(expires),
            "room_name": grant_room_name or room_name,
            "worker_identity": grant_worker_identity or worker_identity,
        },
        "visible_chat_id": _uuid(20),
        "chat_context_revision": 1,
        "profile": _profile(),
    }


def _registered_frame(*, sequence: int = 0) -> dict[str, Any]:
    return {
        "type": "worker_registered",
        "schema_version": "1",
        "message_id": _uuid(30 + sequence),
        "sequence": sequence,
        "sent_at": _iso(NOW),
        "worker_identity": "voice-worker-a",
        "connection_id": _uuid(31),
        "accepted_max_sessions": 2,
        "heartbeat_interval_seconds": 5,
        "registered_at": _iso(NOW),
    }


class ManualClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.monotonic = 100.0

    def utcnow(self) -> datetime:
        return self.now

    def monotonic_time(self) -> float:
        return self.monotonic


class FakeSocket:
    def __init__(self, incoming: list[str] | None = None) -> None:
        self.incoming = asyncio.Queue[str | BaseException]()
        for item in incoming or []:
            self.incoming.put_nowait(item)
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if self.incoming.empty():
            raise EOFError
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class ChallengeConnector:
    def __init__(self, challenge: Challenge, socket: FakeSocket) -> None:
        self.challenge = challenge
        self.socket = socket
        self.headers: list[dict[str, str]] = []

    async def open(self, url: str, *, headers: Mapping[str, str]) -> FakeSocket:
        del url
        copied = dict(headers)
        self.headers.append(copied)
        if len(self.headers) == 1:
            raise ChallengeRequired(self.challenge)
        return self.socket


class FakeRuntime:
    def __init__(self, binding: Any, *, queue_size: int = 2) -> None:
        self.binding = binding
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(queue_size)
        self.closed: list[str] = []
        self.running = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self) -> None:
        self.running.set()
        await self.release.wait()

    def deliver(self, frame: dict[str, Any]) -> None:
        self.queue.put_nowait(frame)

    async def close(self, reason: str) -> None:
        self.closed.append(reason)
        self.release.set()

    @property
    def media_state(self) -> str:
        return "connecting"


def test_worker_config_accepts_only_the_fixed_server_side_profile() -> None:
    config = WorkerConfig.from_environ(_valid_environment())

    assert config.worker_identity == "voice-worker-a"
    assert config.max_sessions == 2
    assert config.runtime_closure_sha256 == SHA256
    assert config.profile.asr_model == ASR_MODEL
    assert config.profile.tts_model == TTS_MODEL
    assert config.profile.voice == "af_heart"
    assert config.profile.sample_rate_hz == 24000
    rendered = repr(config)
    assert SECRET.decode() not in rendered
    assert "speech-secret-value" not in rendered
    assert "speech.internal" not in rendered


@pytest.mark.parametrize(
    ("override", "code"),
    (
        ({"OPENAI_API_KEY": "forbidden"}, "legacy_provider_environment"),
        ({"LIVEKIT_API_SECRET": "forbidden"}, "livekit_api_environment"),
        ({"LIVEKIT_URL": "wss://forbidden"}, "livekit_api_environment"),
        ({"HTTPS_PROXY": "http://proxy.invalid"}, "ambient_proxy_environment"),
        ({"VOICE_WORKER_BIND": "0.0.0.0:8090"}, "unknown_worker_environment"),
        ({"VOICE_UNKNOWN_OPTION": "value"}, "unknown_worker_environment"),
        ({"VOICE_WORKER_MAX_SESSIONS": "0"}, "invalid_max_sessions"),
        ({"VOICE_WORKER_CLOSURE_SHA256": "bad"}, "invalid_closure_digest"),
        ({"VOICE_WORKER_CLOSURE_SHA256": "0" * 64}, "invalid_closure_digest"),
        ({"VOICE_WORKER_IDENTITY": "bad identity"}, "invalid_worker_identity"),
    ),
)
def test_worker_config_rejects_ambient_or_unknown_authority(
    override: dict[str, str], code: str
) -> None:
    with pytest.raises(ConfigError, match=f"^{code}$"):
        WorkerConfig.from_environ(_valid_environment(**override))


def test_worker_config_requires_tls_outside_explicit_development() -> None:
    with pytest.raises(ConfigError, match="insecure_control_url"):
        WorkerConfig.from_environ(
            _valid_environment(
                ASTRAL_VOICE_CONTROL_URL="ws://control.internal/api/voice/control"
            )
        )
    config = WorkerConfig.from_environ(
        _valid_environment(
            ASTRAL_ENV="development",
            ASTRAL_VOICE_CONTROL_URL="ws://control.internal/api/voice/control",
            VOICE_SPEECH_BASE_URL="http://speech.internal/v1",
        )
    )
    assert config.environment == "development"


def test_challenge_signature_has_a_stable_domain_separated_golden_value() -> None:
    signature = sign_challenge(
        SECRET,
        worker_identity="voice-worker-a",
        nonce="nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
        timestamp=1_785_510_400,
    )
    canonical = (
        b"astraldeep.voice.worker-control.challenge.v1\n"
        b"voice-worker-a\nnonce_AAAAAAAAAAAAAAAAAAAAAAAA\n1785510400"
    )
    assert signature == hmac.new(SECRET, canonical, hashlib.sha256).hexdigest()


def test_challenge_response_verifies_binding_and_rejects_tampering() -> None:
    challenge = Challenge(
        nonce="nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
        issued_at=1_785_510_395,
        expires_at=1_785_510_410,
    )
    headers = build_challenge_response_headers(
        SECRET,
        "voice-worker-a",
        challenge,
        timestamp=1_785_510_400,
    )
    assert verify_challenge_response(
        SECRET,
        challenge,
        headers,
        expected_worker_identity="voice-worker-a",
        now=1_785_510_400,
    )
    altered = dict(headers)
    altered["X-Astral-Voice-Signature"] = "0" * 64
    assert not verify_challenge_response(
        SECRET,
        challenge,
        altered,
        expected_worker_identity="voice-worker-a",
        now=1_785_510_400,
    )


def test_challenge_replay_window_is_bounded_single_use_and_expiring() -> None:
    replay = ChallengeReplayWindow(capacity=2)
    first = Challenge("nonce_AAAAAAAAAAAAAAAAAAAAAAAA", 95, 105)
    second = Challenge("nonce_BBBBBBBBBBBBBBBBBBBBBBBB", 95, 105)
    third = Challenge("nonce_CCCCCCCCCCCCCCCCCCCCCCCC", 95, 105)

    replay.claim(first, now=100)
    with pytest.raises(ChallengeError, match="challenge_replayed"):
        replay.claim(first, now=100)
    replay.claim(second, now=100)
    with pytest.raises(ChallengeError, match="challenge_window_full"):
        replay.claim(third, now=100)
    with pytest.raises(ChallengeError, match="challenge_expired"):
        replay.claim(third, now=106)


def test_runtime_import_guard_rejects_forbidden_boundaries() -> None:
    guard = RuntimeImportGuard()
    with pytest.raises(ForbiddenRuntimeImport, match="orchestrator"):
        guard.assert_clean({"asyncio", "orchestrator", "voice_agent.main"})
    with pytest.raises(ForbiddenRuntimeImport, match="livekit.api"):
        guard.find_spec("livekit.api", None)
    assert guard.find_spec("livekit.rtc", None) is None
    assert guard.find_spec("shared.streaming_egress", None) is None


def test_runtime_distribution_guard_rejects_agents_api_llm_and_database() -> None:
    assert_runtime_distributions({"livekit": "1.1.14", "websockets": "17.0.1"})
    with pytest.raises(ForbiddenRuntimeImport, match="livekit-api"):
        assert_runtime_distributions({"livekit": "1.1.14", "livekit-api": "1.2.0"})
    with pytest.raises(ForbiddenRuntimeImport, match="openai"):
        assert_runtime_distributions({"openai": "2.0.0"})


def test_worker_runtime_sources_do_not_import_product_authority_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = {
        "agents",
        "orchestrator",
        "shared.database",
        "shared.external_http",
        "livekit.api",
        "livekit.agents",
        "livekit.plugins",
        "openai",
    }
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not {
            name
            for name in imported
            if any(
                name == prefix or name.startswith(prefix + ".") for prefix in forbidden
            )
        }, path


@pytest.mark.asyncio
async def test_websockets_connector_disables_proxies_and_bounds_transport() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    socket = FakeSocket()

    async def connect(url: str, **kwargs: Any) -> FakeSocket:
        calls.append((url, kwargs))
        return socket

    connector = WebsocketsPoolConnector(connect_factory=connect)
    result = await connector.open(
        "wss://control.internal/api/voice/control",
        headers={"X-Test": "value"},
    )

    assert result is socket
    assert calls[0][0] == "wss://control.internal/api/voice/control"
    assert calls[0][1] == {
        "additional_headers": {"X-Test": "value"},
        "proxy": None,
        "compression": None,
        "open_timeout": 5.0,
        "ping_interval": 10.0,
        "ping_timeout": 5.0,
        "close_timeout": 3.0,
        "max_size": 15 * 1024,
        "max_queue": 16,
        "write_limit": 16 * 1024,
        "user_agent_header": None,
    }


def test_decode_control_frame_rejects_binary_oversize_and_non_objects() -> None:
    with pytest.raises(ProtocolViolation, match="text_frame_required"):
        decode_control_frame(b"{}")
    with pytest.raises(ProtocolViolation, match="frame_too_large"):
        decode_control_frame("x" * (15 * 1024 + 1))
    with pytest.raises(ProtocolViolation, match="object_frame_required"):
        decode_control_frame("[]")


def test_session_bind_validates_nested_grant_equality_and_expiry() -> None:
    binding = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    assert binding.room_name == "room-a"
    assert binding.worker_rtc_grant.room_name == binding.room_name
    assert binding.worker_rtc_grant.worker_identity == binding.worker_identity
    assert binding.worker_rtc_grant.join_token.startswith("jwt-memory-only-")
    assert "jwt-memory-only" not in repr(binding)

    with pytest.raises(ProtocolViolation, match="grant_room_mismatch"):
        parse_session_bind(
            _bind_frame(grant_room_name="other-room"),
            expected_worker_identity="voice-worker-a",
            now=NOW,
        )
    with pytest.raises(ProtocolViolation, match="grant_worker_mismatch"):
        parse_session_bind(
            _bind_frame(grant_worker_identity="other-worker"),
            expected_worker_identity="voice-worker-a",
            now=NOW,
        )
    mismatched_expiry = _bind_frame()
    mismatched_expiry["worker_rtc_grant"]["expires_at"] = _iso(
        NOW + timedelta(minutes=3)
    )
    with pytest.raises(ProtocolViolation, match="grant_expiry_mismatch"):
        parse_session_bind(
            mismatched_expiry,
            expected_worker_identity="voice-worker-a",
            now=NOW,
        )
    with pytest.raises(ProtocolViolation, match="grant_lifetime_exceeded"):
        parse_session_bind(
            _bind_frame(
                issued_at=NOW - timedelta(seconds=1),
                expires_at=NOW + timedelta(minutes=6),
            ),
            expected_worker_identity="voice-worker-a",
            now=NOW,
        )
    with pytest.raises(ProtocolViolation, match="grant_expired"):
        parse_session_bind(
            _bind_frame(expires_at=NOW - timedelta(seconds=1)),
            expected_worker_identity="voice-worker-a",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_session_supervisor_enforces_capacity_and_assignment_idempotency() -> (
    None
):
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    supervisor = SessionSupervisor(max_sessions=1, session_factory=factory)
    first = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    duplicate = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    second = parse_session_bind(
        _bind_frame(session_number=40, assignment_number=41),
        expected_worker_identity="voice-worker-a",
        now=NOW,
    )

    assert await supervisor.start(first) is True
    assert await supervisor.start(duplicate) is False
    assert len(runtimes) == 1
    assert duplicate.worker_rtc_grant.join_token == ""
    with pytest.raises(CapacityExceeded):
        await supervisor.start(second)
    assert second.worker_rtc_grant.join_token == ""

    await supervisor.shutdown("test_shutdown")
    assert runtimes[0].closed == ["test_shutdown"]
    assert first.worker_rtc_grant.join_token == ""
    assert supervisor.active_count == 0


@pytest.mark.asyncio
async def test_session_supervisor_replaces_higher_worker_grant_revision() -> None:
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    supervisor = SessionSupervisor(max_sessions=1, session_factory=factory)
    first = parse_session_bind(
        _bind_frame(worker_revision=1),
        expected_worker_identity="voice-worker-a",
        now=NOW,
    )
    active_reconnect = parse_session_bind(
        _bind_frame(worker_revision=2),
        expected_worker_identity="voice-worker-a",
        now=NOW,
    )
    assert await supervisor.start(first) is True
    assert await supervisor.start(active_reconnect) is True
    assert runtimes[0].closed == ["worker_grant_replaced"]
    assert first.worker_rtc_grant.join_token == ""
    assert supervisor.active_count == 1
    assert supervisor.session_states() == ((first.session_id, 1, "connecting"),)

    await supervisor.end(first.session_id, 1, 1, "media_error")
    closed_reconnect = parse_session_bind(
        _bind_frame(worker_revision=3),
        expected_worker_identity="voice-worker-a",
        now=NOW,
    )
    assert await supervisor.start(closed_reconnect) is True
    assert supervisor.active_count == 1
    assert runtimes[-1].binding.worker_rtc_grant_revision == 3

    conflicting = parse_session_bind(
        _bind_frame(assignment_number=999, worker_revision=4),
        expected_worker_identity="voice-worker-a",
        now=NOW,
    )
    with pytest.raises(AssignmentConflict, match="assignment_conflict"):
        await supervisor.start(conflicting)
    assert conflicting.worker_rtc_grant.join_token == ""
    await supervisor.shutdown("test_shutdown")


@pytest.mark.asyncio
async def test_session_supervisor_rejects_conflicting_assignment_and_queue_overflow() -> (
    None
):
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding, queue_size=1)
        runtimes.append(runtime)
        return runtime

    supervisor = SessionSupervisor(max_sessions=1, session_factory=factory)
    first = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    conflict = parse_session_bind(
        _bind_frame(assignment_number=999),
        expected_worker_identity="voice-worker-a",
        now=NOW,
    )
    await supervisor.start(first)
    await runtimes[0].running.wait()

    with pytest.raises(AssignmentConflict):
        await supervisor.start(conflict)
    assert conflict.worker_rtc_grant.join_token == ""

    frame = {
        "type": "set_capture",
        "session_id": first.session_id,
        "generation": first.generation,
    }
    supervisor.deliver(frame)
    with pytest.raises(ProtocolViolation, match="session_queue_full"):
        supervisor.deliver(frame)
    await supervisor.shutdown("test_shutdown")


def test_frame_rate_limiter_fails_closed_without_unbounded_history() -> None:
    limiter = FrameRateLimiter(max_frames=2, window_seconds=1.0)
    limiter.check(10.0)
    limiter.check(10.1)
    with pytest.raises(ProtocolViolation, match="frame_rate_exceeded"):
        limiter.check(10.2)
    limiter.check(11.2)
    assert limiter.retained_count <= 2


@pytest.mark.asyncio
async def test_pool_client_authenticates_registers_and_starts_one_assignment() -> None:
    clock = ManualClock()
    registered = json.dumps(_registered_frame(), separators=(",", ":"))
    bind = json.dumps(_bind_frame(), separators=(",", ":"))
    socket = FakeSocket([registered, bind])
    challenge = Challenge(
        nonce="nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
        issued_at=int(NOW.timestamp()) - 1,
        expires_at=int(NOW.timestamp()) + 10,
    )
    connector = ChallengeConnector(challenge, socket)
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    config = WorkerConfig.from_environ(_valid_environment())
    supervisor = SessionSupervisor(max_sessions=2, session_factory=factory)
    client = PoolClient(
        config,
        connector=connector,
        supervisor=supervisor,
        utcnow=clock.utcnow,
        monotonic=clock.monotonic_time,
    )

    await client.run_connection()

    assert len(connector.headers) == 2
    assert connector.headers[0] == {}
    assert verify_challenge_response(
        SECRET,
        challenge,
        connector.headers[1],
        expected_worker_identity="voice-worker-a",
        now=int(NOW.timestamp()),
    )
    sent = [json.loads(payload) for payload in socket.sent]
    register = sent[0]
    assert register["type"] == "worker_register"
    assert register["sequence"] == 0
    assert register["max_sessions"] == 2
    assert register["runtime_closure_sha256"] == SHA256
    assert register["profile"] == _profile()
    assert len(runtimes) == 1
    assert runtimes[0].binding.assignment_id == _uuid(11)
    assert socket.closed[-1][0] == 1000
    assert supervisor.active_count == 0
    assert runtimes[0].binding.worker_rtc_grant.join_token == ""


@pytest.mark.asyncio
async def test_pool_client_rejects_wrong_direction_and_sequence_before_side_effect() -> (
    None
):
    clock = ManualClock()
    wrong_direction = {
        "type": "worker_ready",
        "schema_version": "1",
        "message_id": _uuid(50),
        "session_id": _uuid(10),
        "generation": 1,
        "sequence": 0,
        "sent_at": _iso(NOW),
    }
    socket = FakeSocket(
        [
            json.dumps(_registered_frame()),
            json.dumps(wrong_direction),
        ]
    )
    connector = ChallengeConnector(
        Challenge(
            "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
            int(NOW.timestamp()) - 1,
            int(NOW.timestamp()) + 10,
        ),
        socket,
    )
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=connector,
        supervisor=SessionSupervisor(max_sessions=2),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic_time,
    )
    with pytest.raises(ProtocolViolation, match="wrong_direction"):
        await client.run_connection()
    assert client.supervisor.active_count == 0
    assert socket.closed[-1] == (1008, "protocol_violation")

    duplicate_sequence = _bind_frame()
    duplicate_sequence["sequence"] = 1
    socket2 = FakeSocket(
        [json.dumps(_registered_frame()), json.dumps(duplicate_sequence)]
    )
    client2 = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=ChallengeConnector(connector.challenge, socket2),
        supervisor=SessionSupervisor(max_sessions=2),
        utcnow=clock.utcnow,
        monotonic=clock.monotonic_time,
    )
    await client2.run_connection()
    assert client2.supervisor.active_count == 0
    sequence_rejection = json.loads(socket2.sent[-1])
    assert sequence_rejection["type"] == "media_state"
    assert sequence_rejection["state"] == "failed"
    assert sequence_rejection["reason"] == "control_protocol_error"
    assert socket2.closed[-1] == (1000, "normal")


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ({"ASTRAL_ENV": "mystery"}, "invalid_astral_environment"),
        ({"VOICE_CONTROL_SECRET": "short"}, "invalid_control_secret"),
        ({"VOICE_SPEECH_API_KEY": "x" * 8_193}, "invalid_speech_credential"),
        ({"VOICE_WORKER_MAX_SESSIONS": "many"}, "invalid_max_sessions"),
        ({"VOICE_WORKER_MAX_SESSIONS": "01"}, "invalid_max_sessions"),
        (
            {"ASTRAL_VOICE_CONTROL_URL": "ftp://control.internal/path"},
            "invalid_control_url",
        ),
        (
            {"ASTRAL_VOICE_CONTROL_URL": "wss://user@control.internal/path"},
            "invalid_control_url",
        ),
        (
            {"ASTRAL_VOICE_CONTROL_URL": "wss://control.internal:bad/path"},
            "invalid_control_url",
        ),
        (
            {"VOICE_SPEECH_BASE_URL": "http://speech.internal/v1"},
            "insecure_speech_url",
        ),
        (
            {"VOICE_SPEECH_BASE_URL": "https://speech.internal/v1?secret=x"},
            "invalid_speech_url",
        ),
    ),
)
def test_worker_config_fails_closed_for_malformed_values(
    mutation: dict[str, str], code: str
) -> None:
    with pytest.raises(ConfigError, match=f"^{code}$"):
        WorkerConfig.from_environ(_valid_environment(**mutation))


def test_worker_config_rejects_missing_whitespace_and_non_string_values() -> None:
    missing = _valid_environment()
    del missing["VOICE_SPEECH_API_KEY"]
    with pytest.raises(ConfigError, match="missing_voice_speech_api_key"):
        WorkerConfig.from_environ(missing)
    with pytest.raises(ConfigError, match="invalid_voice_worker_identity"):
        WorkerConfig.from_environ(
            _valid_environment(VOICE_WORKER_IDENTITY=" voice-worker-a")
        )
    malformed: dict[Any, Any] = _valid_environment()
    malformed[3] = "value"
    with pytest.raises(ConfigError, match="invalid_environment"):
        WorkerConfig.from_environ(malformed)


@pytest.mark.parametrize(
    ("args", "code"),
    (
        (("bad", 100, 105), "invalid_challenge_nonce"),
        (("nonce_AAAAAAAAAAAAAAAAAAAAAAAA", True, 105), "invalid_challenge_timestamp"),
        (("nonce_AAAAAAAAAAAAAAAAAAAAAAAA", 100, 131), "invalid_challenge_lifetime"),
    ),
)
def test_challenge_rejects_malformed_server_values(
    args: tuple[Any, Any, Any], code: str
) -> None:
    with pytest.raises(ChallengeError, match=code):
        Challenge(*args)


def test_challenge_helpers_reject_invalid_inputs_without_rendering_nonce() -> None:
    challenge = Challenge("nonce_AAAAAAAAAAAAAAAAAAAAAAAA", 100, 110)
    assert "nonce_A" not in repr(challenge)
    replay = ChallengeReplayWindow(capacity=1)
    with pytest.raises(ChallengeError, match="challenge_not_yet_valid"):
        replay.claim(challenge, now=90)
    with pytest.raises(ChallengeError, match="invalid_challenge_clock"):
        replay.claim(challenge, now=True)
    with pytest.raises(ValueError, match="invalid_challenge_window_capacity"):
        ChallengeReplayWindow(capacity=0)

    invalid_signatures = (
        (b"", "voice-worker-a", challenge.nonce, 100, "invalid_challenge_secret"),
        (SECRET, "bad identity", challenge.nonce, 100, "invalid_worker_identity"),
        (SECRET, "voice-worker-a", "bad", 100, "invalid_challenge_nonce"),
        (
            SECRET,
            "voice-worker-a",
            challenge.nonce,
            True,
            "invalid_challenge_timestamp",
        ),
    )
    for secret, identity, nonce, timestamp, code in invalid_signatures:
        with pytest.raises(ChallengeError, match=code):
            sign_challenge(
                secret,
                worker_identity=identity,
                nonce=nonce,
                timestamp=timestamp,
            )


def test_challenge_verifier_rejects_missing_stale_and_malformed_headers() -> None:
    challenge = Challenge("nonce_AAAAAAAAAAAAAAAAAAAAAAAA", 100, 110)
    assert not verify_challenge_response(
        SECRET,
        challenge,
        {},
        expected_worker_identity="voice-worker-a",
        now=105,
    )
    headers = build_challenge_response_headers(
        SECRET, "voice-worker-a", challenge, timestamp=100
    )
    assert not verify_challenge_response(
        SECRET,
        challenge,
        headers,
        expected_worker_identity="voice-worker-a",
        now=110,
    )
    malformed = dict(headers)
    malformed["X-Astral-Voice-Timestamp"] = "not-an-int"
    assert not verify_challenge_response(
        SECRET,
        challenge,
        malformed,
        expected_worker_identity="voice-worker-a",
        now=105,
    )


def _invalid_status(status: int, headers: Headers | None = None) -> InvalidStatus:
    return InvalidStatus(Response(status, "status", headers or Headers()))


@pytest.mark.asyncio
async def test_websockets_connector_extracts_upgrade_challenge() -> None:
    challenge_headers = Headers(
        {
            control_module.CHALLENGE_NONCE_HEADER: "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
            control_module.CHALLENGE_ISSUED_HEADER: "100",
            control_module.CHALLENGE_EXPIRES_HEADER: "110",
        }
    )

    async def challenged(_url: str, **_kwargs: Any) -> FakeSocket:
        raise _invalid_status(401, challenge_headers)

    with pytest.raises(ChallengeRequired) as captured:
        await WebsocketsPoolConnector(connect_factory=challenged).open(
            "wss://control.internal/path", headers={}
        )
    assert captured.value.challenge.issued_at == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (401, 403))
async def test_websockets_connector_redacts_upgrade_failures(status: int) -> None:
    async def rejected(_url: str, **_kwargs: Any) -> FakeSocket:
        raise _invalid_status(status)

    expected = ChallengeError if status == 401 else PoolConnectionError
    code = "invalid_challenge_headers" if status == 401 else "upgrade_rejected"
    with pytest.raises(expected, match=code):
        await WebsocketsPoolConnector(connect_factory=rejected).open(
            "wss://control.internal/path", headers={}
        )


@pytest.mark.asyncio
async def test_websockets_connector_maps_transport_failure_and_preserves_challenge() -> (
    None
):
    async def broken(_url: str, **_kwargs: Any) -> FakeSocket:
        raise RuntimeError("url and headers must not escape")

    with pytest.raises(PoolConnectionError, match="connection_failed"):
        await WebsocketsPoolConnector(connect_factory=broken).open(
            "wss://control.internal/path", headers={}
        )

    challenge = Challenge("nonce_AAAAAAAAAAAAAAAAAAAAAAAA", 100, 110)

    async def already_parsed(_url: str, **_kwargs: Any) -> FakeSocket:
        raise ChallengeRequired(challenge)

    with pytest.raises(ChallengeRequired):
        await WebsocketsPoolConnector(connect_factory=already_parsed).open(
            "wss://control.internal/path", headers={}
        )


@pytest.mark.parametrize(
    ("payload", "code"),
    (
        ('{"type":"a","type":"b"}', "duplicate_json_key"),
        ('{"value":NaN}', "invalid_json_number"),
        ("{", "invalid_json"),
        ("\ud800", "invalid_utf8"),
    ),
)
def test_decode_control_frame_rejects_ambiguous_json(payload: str, code: str) -> None:
    with pytest.raises(ProtocolViolation, match=code):
        decode_control_frame(payload)


def test_frame_rate_limiter_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="invalid_frame_rate_limit"):
        FrameRateLimiter(max_frames=0, window_seconds=1)


def _mutated_bind(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    frame = copy.deepcopy(_bind_frame())
    target: dict[str, Any] = frame
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    return frame


@pytest.mark.parametrize(
    ("path", "value", "code"),
    (
        (("type",), "other", "invalid_session_bind_type"),
        (("message_id",), "bad", "invalid_message_id"),
        (("generation",), True, "invalid_generation"),
        (("worker_identity",), "worker-b", "worker_identity_mismatch"),
        (("transport",), "other", "invalid_transport"),
        (
            ("grant_expires_at",),
            _iso(NOW - timedelta(seconds=1)),
            "media_grant_expired",
        ),
        (("visible_chat_id",), "bad", "invalid_visible_chat_id"),
        (("chat_context_revision",), 0, "invalid_chat_context_revision"),
        (("profile", "voice"), "af_sky", "profile_mismatch"),
        (("worker_rtc_grant", "revision"), 2, "grant_revision_mismatch"),
        (("worker_rtc_grant", "livekit_url"), "https://bad", "invalid_livekit_url"),
        (("worker_rtc_grant", "join_token"), "short", "invalid_worker_join_token"),
        (
            ("worker_rtc_grant", "issued_at"),
            _iso(NOW + timedelta(minutes=1)),
            "grant_not_yet_valid",
        ),
    ),
)
def test_session_bind_rejects_malformed_authority(
    path: tuple[str, ...], value: Any, code: str
) -> None:
    with pytest.raises(ProtocolViolation, match=code):
        parse_session_bind(
            _mutated_bind(path, value),
            expected_worker_identity="voice-worker-a",
            now=NOW,
        )


def test_session_bind_rejects_extra_or_non_object_grant() -> None:
    extra = _bind_frame()
    extra["unexpected"] = True
    with pytest.raises(ProtocolViolation, match="invalid_session_bind_fields"):
        parse_session_bind(extra, expected_worker_identity="voice-worker-a", now=NOW)
    non_object = _bind_frame()
    non_object["worker_rtc_grant"] = "secret"
    with pytest.raises(ProtocolViolation, match="invalid_worker_rtc_grant"):
        parse_session_bind(
            non_object, expected_worker_identity="voice-worker-a", now=NOW
        )
    nested_extra = _bind_frame()
    nested_extra["worker_rtc_grant"]["unexpected"] = True
    with pytest.raises(ProtocolViolation, match="invalid_worker_rtc_grant_fields"):
        parse_session_bind(
            nested_extra, expected_worker_identity="voice-worker-a", now=NOW
        )
    backwards = _bind_frame()
    backwards["worker_rtc_grant"]["issued_at"] = _iso(NOW + timedelta(seconds=1))
    backwards["worker_rtc_grant"]["expires_at"] = _iso(
        NOW + timedelta(milliseconds=500)
    )
    backwards["grant_expires_at"] = backwards["worker_rtc_grant"]["expires_at"]
    with pytest.raises(ProtocolViolation, match="invalid_grant_lifetime"):
        parse_session_bind(
            backwards, expected_worker_identity="voice-worker-a", now=NOW
        )


@pytest.mark.asyncio
async def test_default_bound_session_clears_nested_buffers_and_bearer() -> None:
    binding = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    runtime = BoundControlSession(binding, queue_size=1)
    buffered = {"text": "sensitive", "nested": [{"token": "temporary"}]}
    task = asyncio.create_task(runtime.run())
    runtime.deliver(buffered)
    assert runtime.media_state == "connecting"
    await runtime.close("test")
    await task
    assert runtime.media_state == "ended"
    assert buffered == {}
    assert binding.worker_rtc_grant.join_token == ""
    with pytest.raises(ProtocolViolation, match="session_closed"):
        runtime.deliver({})
    with pytest.raises(ValueError, match="invalid_session_queue_size"):
        BoundControlSession(binding, queue_size=0)


@pytest.mark.asyncio
async def test_supervisor_validates_capacity_delivery_end_and_factory_failure() -> None:
    with pytest.raises(ValueError, match="invalid_max_sessions"):
        SessionSupervisor(max_sessions=0)
    supervisor = SessionSupervisor(max_sessions=2)
    with pytest.raises(ProtocolViolation, match="invalid_accepted_capacity"):
        await supervisor.set_capacity(3)

    binding = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    await supervisor.start(binding)
    assert supervisor.session_states() == ((binding.session_id, 1, "connecting"),)
    with pytest.raises(ProtocolViolation, match="capacity_changed_after_assignment"):
        await supervisor.set_capacity(1)
    with pytest.raises(ProtocolViolation, match="unknown_session"):
        supervisor.deliver({"session_id": _uuid(999), "generation": 1})
    with pytest.raises(ProtocolViolation, match="generation_mismatch"):
        supervisor.deliver({"session_id": binding.session_id, "generation": 2})
    with pytest.raises(ProtocolViolation, match="media_grant_revision_mismatch"):
        supervisor.deliver(
            {
                "session_id": binding.session_id,
                "generation": 1,
                "media_grant_revision": 2,
            }
        )
    with pytest.raises(ProtocolViolation, match="generation_mismatch"):
        await supervisor.end(binding.session_id, 2, 1, "user")
    with pytest.raises(ProtocolViolation, match="media_grant_revision_mismatch"):
        await supervisor.end(binding.session_id, 1, 2, "user")
    await supervisor.end(binding.session_id, 1, 1, "user")
    assert supervisor.active_count == 0
    # A repeated, already-fenced lifecycle callback is an idempotent no-op.
    await supervisor.end(binding.session_id, 1, 1, "user")
    with pytest.raises(ProtocolViolation, match="media_grant_revision_mismatch"):
        await supervisor.end(binding.session_id, 1, 2, "user")
    stale_retry = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    with pytest.raises(AssignmentConflict, match="stale_assignment"):
        await supervisor.start(stale_retry)
    assert stale_retry.worker_rtc_grant.join_token == ""

    failed_binding = parse_session_bind(
        _bind_frame(session_number=80, assignment_number=81),
        expected_worker_identity="voice-worker-a",
        now=NOW,
    )

    def broken_factory(_binding: Any) -> FakeRuntime:
        raise RuntimeError("factory_failed")

    broken = SessionSupervisor(max_sessions=1, session_factory=broken_factory)
    with pytest.raises(RuntimeError, match="factory_failed"):
        await broken.start(failed_binding)
    assert failed_binding.worker_rtc_grant.join_token == ""


@pytest.mark.asyncio
async def test_closed_fence_retains_rotated_media_grant_revision() -> None:
    supervisor = SessionSupervisor(max_sessions=1, session_factory=FakeRuntime)
    binding = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    await supervisor.start(binding)
    supervisor.deliver(
        {
            "type": "media_grant_rotated",
            "session_id": binding.session_id,
            "generation": 1,
            "previous_media_grant_revision": 1,
            "media_grant_revision": 2,
        }
    )
    await supervisor.end(binding.session_id, 1, 2, "media_error")

    exact_late_command = {
        "type": "set_capture",
        "session_id": binding.session_id,
        "generation": 1,
        "media_grant_revision": 2,
    }
    with pytest.raises(ClosedSessionRace, match="session_closed_race"):
        supervisor.deliver(exact_late_command)
    with pytest.raises(ProtocolViolation, match="media_grant_revision_mismatch"):
        supervisor.deliver({**exact_late_command, "media_grant_revision": 1})
    await supervisor.end(binding.session_id, 1, 2, "media_error")
    with pytest.raises(ProtocolViolation, match="media_grant_revision_mismatch"):
        await supervisor.end(binding.session_id, 1, 1, "media_error")


@pytest.mark.asyncio
async def test_closed_fence_refreshes_eviction_order_and_rejects_older_end() -> None:
    supervisor = SessionSupervisor(max_sessions=1, session_factory=FakeRuntime)

    async def close_session(number: int, *, generation: int = 1) -> str:
        frame = _bind_frame(
            session_number=number,
            assignment_number=10_000 + number,
            generation=generation,
        )
        binding = parse_session_bind(
            frame,
            expected_worker_identity="voice-worker-a",
            now=NOW,
        )
        await supervisor.start(binding)
        await supervisor.end(binding.session_id, generation, 1, "media_error")
        return binding.session_id

    refreshed_session = await close_session(1)
    evicted_session = ""
    for number in range(2, MAX_CLOSED_SESSION_FENCES + 1):
        closed = await close_session(number)
        if number == 2:
            evicted_session = closed
    await close_session(1, generation=2)
    await close_session(MAX_CLOSED_SESSION_FENCES + 1)

    retained = supervisor.retained_sequence_fences()
    assert (refreshed_session, 2) in retained
    assert (evicted_session, 1) not in retained
    with pytest.raises(ProtocolViolation, match="generation_mismatch"):
        await supervisor.end(refreshed_session, 1, 1, "media_error")


@pytest.mark.asyncio
async def test_supervisor_removes_runtime_that_finishes_independently() -> None:
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    supervisor = SessionSupervisor(max_sessions=1, session_factory=factory)
    binding = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    await supervisor.start(binding)
    runtimes[0].release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert supervisor.active_count == 0
    assert binding.worker_rtc_grant.join_token == ""
    await supervisor.shutdown("test")


@pytest.mark.asyncio
async def test_concurrent_shutdown_callers_wait_for_the_same_cleanup() -> None:
    close_started = asyncio.Event()
    release = asyncio.Event()

    class SlowRuntime(FakeRuntime):
        async def close(self, reason: str) -> None:
            self.closed.append(reason)
            close_started.set()
            await release.wait()
            self.release.set()

    def factory(binding: Any) -> SlowRuntime:
        return SlowRuntime(binding)

    supervisor = SessionSupervisor(max_sessions=1, session_factory=factory)
    binding = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    await supervisor.start(binding)
    first = asyncio.create_task(supervisor.shutdown("first"))
    await close_started.wait()
    second = asyncio.create_task(supervisor.shutdown("second"))
    await asyncio.sleep(0)
    assert not second.done()
    assert binding.worker_rtc_grant.join_token == ""
    release.set()
    await asyncio.gather(first, second)


def _end_frame(
    binding_frame: Mapping[str, Any], *, sequence: int = 1
) -> dict[str, Any]:
    return {
        "type": "end_session",
        "schema_version": "1",
        "message_id": _uuid(700 + sequence),
        "session_id": binding_frame["session_id"],
        "generation": binding_frame["generation"],
        "sequence": sequence,
        "sent_at": _iso(NOW),
        "media_grant_revision": binding_frame["media_grant_revision"],
        "reason": "user",
    }


@pytest.mark.asyncio
async def test_pool_client_processes_end_session_without_leaking_assignment() -> None:
    bind = _bind_frame()
    socket = FakeSocket(
        [
            json.dumps(_registered_frame()),
            json.dumps(bind),
            json.dumps(_end_frame(bind)),
        ]
    )
    connector = ChallengeConnector(
        Challenge(
            "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
            int(NOW.timestamp()) - 1,
            int(NOW.timestamp()) + 10,
        ),
        socket,
    )
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=connector,
        utcnow=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    await client.run_connection()
    assert client.supervisor.active_count == 0
    assert socket.closed[-1][0] == 1000


@pytest.mark.asyncio
async def test_pool_client_reconciles_closed_race_without_closing_live_peer() -> None:
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    supervisor = SessionSupervisor(max_sessions=2, session_factory=factory)
    socket = FakeSocket()
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=supervisor,
        utcnow=lambda: NOW,
    )
    client._socket = socket
    bind = _bind_frame()
    peer_bind = _bind_frame(session_number=90, assignment_number=91)
    await client._dispatch(bind)
    await client._dispatch(peer_bind)
    runtimes[0].release.set()
    for _ in range(10):
        if supervisor.active_count == 1:
            break
        await asyncio.sleep(0)
    assert supervisor.active_count == 1

    late_capture = {
        "type": "set_capture",
        "schema_version": "1",
        "message_id": _uuid(750),
        "session_id": bind["session_id"],
        "generation": 1,
        "sequence": 1,
        "sent_at": _iso(NOW),
        "media_grant_revision": 1,
        "enabled": True,
    }
    await client._dispatch(late_capture)

    reconciliation = json.loads(socket.sent[-1])
    assert reconciliation["type"] == "media_state"
    assert reconciliation["session_id"] == bind["session_id"]
    assert reconciliation["generation"] == 1
    assert reconciliation["state"] == "ended"
    assert reconciliation["reason"] == "session_closed"
    assert supervisor.active_count == 1
    assert supervisor.session_states() == ((peer_bind["session_id"], 1, "connecting"),)
    assert runtimes[1].closed == []
    assert socket.closed == []

    peer_capture = {
        **late_capture,
        "message_id": _uuid(754),
        "session_id": peer_bind["session_id"],
    }
    await client._dispatch(peer_capture)
    assert runtimes[1].queue.get_nowait() == peer_capture

    wrong_revision_client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=supervisor,
        utcnow=lambda: NOW,
    )
    with pytest.raises(ProtocolViolation, match="media_grant_revision_mismatch"):
        await wrong_revision_client._dispatch(
            {
                **late_capture,
                "message_id": _uuid(752),
                "sequence": 0,
                "media_grant_revision": 2,
            }
        )

    wrong_generation_client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=supervisor,
        utcnow=lambda: NOW,
    )
    with pytest.raises(ProtocolViolation, match="generation_mismatch"):
        await wrong_generation_client._dispatch(
            {
                **late_capture,
                "message_id": _uuid(753),
                "generation": 2,
                "sequence": 0,
            }
        )

    never_bound = {
        **late_capture,
        "message_id": _uuid(751),
        "session_id": _uuid(999),
        "sequence": 0,
    }
    with pytest.raises(ProtocolViolation, match="unknown_session"):
        await client._dispatch(never_bound)
    await supervisor.shutdown("test_shutdown")


@pytest.mark.asyncio
async def test_pool_client_session_sequences_follow_closed_fence_bound() -> None:
    supervisor = SessionSupervisor(max_sessions=1, session_factory=FakeRuntime)
    socket = FakeSocket()
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=supervisor,
        utcnow=lambda: NOW,
    )
    client._socket = socket

    for number in range(1, 301):
        bind = _bind_frame(
            session_number=number,
            assignment_number=20_000 + number,
        )
        await client._dispatch(bind)
        await client._send_session_payload(
            socket,
            bind["session_id"],
            bind["generation"],
            {"type": "heartbeat", "media_state": "ready"},
        )
        await client._dispatch(_end_frame(bind))

    assert len(client._session_receive_sequences) == MAX_CLOSED_SESSION_FENCES
    assert len(client._session_send_sequences) == MAX_CLOSED_SESSION_FENCES
    assert len(supervisor.retained_sequence_fences()) == MAX_CLOSED_SESSION_FENCES


@pytest.mark.asyncio
async def test_pool_client_isolates_malformed_attributable_session_frames() -> None:
    bind = _bind_frame()
    unsupported = {
        "type": "turn_bound",
        "schema_version": "1",
        "message_id": _uuid(720),
        "session_id": bind["session_id"],
        "generation": 1,
        "sequence": 1,
        "sent_at": _iso(NOW),
        "media_grant_revision": 1,
        "enabled": True,
    }
    challenge = Challenge(
        "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
        int(NOW.timestamp()) - 1,
        int(NOW.timestamp()) + 10,
    )
    socket = FakeSocket(
        [json.dumps(_registered_frame()), json.dumps(bind), json.dumps(unsupported)]
    )
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=ChallengeConnector(challenge, socket),
        utcnow=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    await client.run_connection()
    malformed_command = json.loads(socket.sent[-1])
    assert malformed_command["type"] == "media_state"
    assert malformed_command["state"] == "failed"
    assert malformed_command["reason"] == "control_protocol_error"
    assert socket.closed[-1] == (1000, "normal")

    bad_end = _end_frame(bind)
    bad_end["reason"] = "other"
    socket2 = FakeSocket(
        [json.dumps(_registered_frame()), json.dumps(bind), json.dumps(bad_end)]
    )
    client2 = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=ChallengeConnector(challenge, socket2),
        utcnow=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    await client2.run_connection()
    malformed_end = json.loads(socket2.sent[-1])
    assert malformed_end["type"] == "media_state"
    assert malformed_end["state"] == "failed"
    assert malformed_end["reason"] == "control_protocol_error"
    assert socket2.closed[-1] == (1000, "normal")


@pytest.mark.asyncio
async def test_pool_client_isolates_capacity_failure_from_healthy_peer() -> None:
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    first = _bind_frame()
    second = _bind_frame(session_number=90, assignment_number=91)
    socket = FakeSocket()
    supervisor = SessionSupervisor(max_sessions=1, session_factory=factory)
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=supervisor,
        utcnow=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    await client._dispatch(first)

    await client._dispatch_or_isolate(socket, second)

    rejected = json.loads(socket.sent[-1])
    assert rejected["type"] == "media_state"
    assert rejected["session_id"] == second["session_id"]
    assert rejected["state"] == "failed"
    assert rejected["reason"] == "control_protocol_error"
    assert second["worker_rtc_grant"]["join_token"] == ""
    assert supervisor.active_count == 1
    assert runtimes[0].closed == []

    peer_capture = {
        "type": "set_capture",
        "schema_version": "1",
        "message_id": _uuid(1_490),
        "session_id": first["session_id"],
        "generation": 1,
        "sequence": 1,
        "sent_at": _iso(NOW),
        "media_grant_revision": 1,
        "enabled": True,
    }
    await client._dispatch_or_isolate(socket, peer_capture)
    assert runtimes[0].queue.get_nowait() == peer_capture
    assert socket.closed == []
    await supervisor.shutdown("test_shutdown")


@pytest.mark.asyncio
async def test_capacity_failure_does_not_finish_live_pool_connection() -> None:
    class BlockingSocket(FakeSocket):
        async def recv(self) -> str:
            item = await self.incoming.get()
            if isinstance(item, BaseException):
                raise item
            return item

    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    registered = _registered_frame()
    registered["accepted_max_sessions"] = 1
    first = _bind_frame()
    rejected = _bind_frame(session_number=90, assignment_number=91)
    peer_capture = {
        "type": "set_capture",
        "schema_version": "1",
        "message_id": _uuid(1_492),
        "session_id": first["session_id"],
        "generation": 1,
        "sequence": 1,
        "sent_at": _iso(NOW),
        "media_grant_revision": 1,
        "enabled": True,
    }
    socket = BlockingSocket(
        [
            json.dumps(registered),
            json.dumps(first),
            json.dumps(rejected),
            json.dumps(peer_capture),
        ]
    )
    challenge = Challenge(
        "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
        int(NOW.timestamp()) - 1,
        int(NOW.timestamp()) + 10,
    )
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=ChallengeConnector(challenge, socket),
        supervisor=SessionSupervisor(max_sessions=2, session_factory=factory),
        utcnow=lambda: NOW,
        monotonic=lambda: 1.0,
    )

    connection = asyncio.create_task(client.run_connection())
    for _ in range(20):
        if runtimes and not runtimes[0].queue.empty():
            break
        await asyncio.sleep(0)

    assert runtimes[0].queue.get_nowait() == peer_capture
    assert not connection.done()
    assert client.supervisor.active_count == 1
    assert socket.closed == []
    connection.cancel()
    result = (await asyncio.gather(connection, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert socket.closed[-1] == (1001, "worker_shutdown")


@pytest.mark.asyncio
async def test_pool_client_isolates_assignment_conflict_from_other_session() -> None:
    runtimes: list[FakeRuntime] = []

    def factory(binding: Any) -> FakeRuntime:
        runtime = FakeRuntime(binding)
        runtimes.append(runtime)
        return runtime

    first = _bind_frame()
    peer = _bind_frame(session_number=90, assignment_number=91)
    conflict = _bind_frame(assignment_number=999, sequence=1)
    socket = FakeSocket()
    supervisor = SessionSupervisor(max_sessions=2, session_factory=factory)
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=supervisor,
        utcnow=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    await client._dispatch(first)
    await client._dispatch(peer)

    await client._dispatch_or_isolate(socket, conflict)

    assert runtimes[0].closed == ["control_protocol_error"]
    assert runtimes[1].closed == []
    assert supervisor.active_count == 1
    peer_capture = {
        "type": "set_capture",
        "schema_version": "1",
        "message_id": _uuid(1_491),
        "session_id": peer["session_id"],
        "generation": 1,
        "sequence": 1,
        "sent_at": _iso(NOW),
        "media_grant_revision": 1,
        "enabled": True,
    }
    await client._dispatch_or_isolate(socket, peer_capture)
    assert runtimes[1].queue.get_nowait() == peer_capture
    assert socket.closed == []
    await supervisor.shutdown("test_shutdown")


@pytest.mark.asyncio
async def test_pool_client_requires_challenge_and_rejects_second_challenge() -> None:
    socket = FakeSocket()

    class NoChallengeConnector:
        async def open(self, _url: str, *, headers: Mapping[str, str]) -> FakeSocket:
            del headers
            return socket

    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=NoChallengeConnector(),
        utcnow=lambda: NOW,
    )
    with pytest.raises(ChallengeError, match="challenge_not_required"):
        await client.run_connection()
    assert socket.closed == [(1008, "challenge_required")]

    class DoubleChallengeConnector:
        async def open(self, _url: str, *, headers: Mapping[str, str]) -> FakeSocket:
            del headers
            raise ChallengeRequired(
                Challenge(
                    "nonce_AAAAAAAAAAAAAAAAAAAAAAAA",
                    int(NOW.timestamp()) - 1,
                    int(NOW.timestamp()) + 10,
                )
            )

    client2 = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        connector=DoubleChallengeConnector(),
        utcnow=lambda: NOW,
    )
    with pytest.raises(ChallengeError, match="challenge_rejected"):
        await client2.run_connection()


@pytest.mark.asyncio
async def test_pool_client_registration_validation_and_receive_type_failures() -> None:
    config = WorkerConfig.from_environ(_valid_environment())
    client = PoolClient(config, utcnow=lambda: NOW, monotonic=lambda: 1.0)
    for payload, code in (
        ("{}", "missing_frame_type"),
        ('{"type":"not_known"}', "unknown_frame_type"),
    ):
        with pytest.raises(ProtocolViolation, match=code):
            client._receive_frame(payload)

    wrong_identity = _registered_frame()
    wrong_identity["worker_identity"] = "worker-b"
    with pytest.raises(ProtocolViolation, match="worker_identity_mismatch"):
        await client._accept_registration(wrong_identity)
    wrong_sequence = _registered_frame(sequence=1)
    with pytest.raises(ProtocolViolation, match="sequence_out_of_order"):
        await client._accept_registration(wrong_sequence)
    wrong_shape = _registered_frame()
    wrong_shape["extra"] = True
    with pytest.raises(ProtocolViolation, match="invalid_worker_registered_fields"):
        await client._accept_registration(wrong_shape)


@pytest.mark.asyncio
async def test_pool_client_emits_connection_and_session_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = parse_session_bind(
        _bind_frame(), expected_worker_identity="voice-worker-a", now=NOW
    )
    supervisor = SessionSupervisor(max_sessions=1)
    await supervisor.start(binding)
    socket = FakeSocket()
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=supervisor,
        utcnow=lambda: NOW,
    )
    calls = 0

    async def one_iteration(_delay: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(control_module.asyncio, "sleep", one_iteration)
    with pytest.raises(asyncio.CancelledError):
        await client._heartbeat_loop(socket, 5, _uuid(31))
    pool_heartbeat = json.loads(socket.sent[0])
    assert pool_heartbeat == {
        "type": "pool_heartbeat",
        "schema_version": "1",
        "message_id": pool_heartbeat["message_id"],
        "sequence": 1,
        "sent_at": _iso(NOW),
        "worker_identity": "voice-worker-a",
        "connection_id": _uuid(31),
    }
    session_heartbeat = json.loads(socket.sent[1])
    assert session_heartbeat["type"] == "heartbeat"
    assert session_heartbeat["sequence"] == 0
    assert session_heartbeat["media_state"] == "connecting"
    await supervisor.shutdown("test")


@pytest.mark.asyncio
async def test_pool_client_emits_connection_heartbeat_with_zero_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeSocket()
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        supervisor=SessionSupervisor(max_sessions=1),
        utcnow=lambda: NOW,
    )
    calls = 0

    async def one_iteration(_delay: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(control_module.asyncio, "sleep", one_iteration)
    with pytest.raises(asyncio.CancelledError):
        await client._heartbeat_loop(socket, 5, _uuid(31))

    assert len(socket.sent) == 1
    assert json.loads(socket.sent[0])["type"] == "pool_heartbeat"


@pytest.mark.asyncio
async def test_pool_client_send_and_clock_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = PoolClient(
        WorkerConfig.from_environ(_valid_environment()), utcnow=lambda: NOW
    )
    with pytest.raises(ProtocolViolation, match="invalid_outgoing_frame"):
        await client._send(FakeSocket(), {"bad": {1, 2}})
    with pytest.raises(ProtocolViolation, match="outgoing_frame_too_large"):
        await client._send(FakeSocket(), {"value": "x" * (16 * 1024)})

    class BrokenSend(FakeSocket):
        async def send(self, payload: str) -> None:
            del payload
            raise RuntimeError("do not expose")

    with pytest.raises(PoolConnectionError, match="send_failed"):
        await client._send(BrokenSend(), {"ok": True})

    class SlowSend(FakeSocket):
        async def send(self, payload: str) -> None:
            del payload
            await asyncio.Event().wait()

    monkeypatch.setattr(control_module, "SEND_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(PoolConnectionError, match="send_timeout"):
        await client._send(SlowSend(), {"ok": True})

    naive = PoolClient(
        WorkerConfig.from_environ(_valid_environment()),
        utcnow=lambda: datetime(2026, 7, 31),
    )
    with pytest.raises(RuntimeError, match="worker_clock_must_be_timezone_aware"):
        naive._worker_register_frame()


def test_runtime_guard_installs_once_and_checks_exact_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = RuntimeImportGuard()
    original = list(sys.meta_path)
    try:
        guard.install()
        guard.install()
        assert sys.meta_path.count(guard) == 1
    finally:
        sys.meta_path[:] = original

    with pytest.raises(ForbiddenRuntimeImport, match="websockets"):
        assert_runtime_distributions({"livekit": "1.1.14", "websockets": "16.0"})

    class Distribution:
        def __init__(self, name: str, version: str) -> None:
            self.metadata = {"Name": name}
            self.version = version

    monkeypatch.setattr(
        main_module.importlib.metadata,
        "distributions",
        lambda: [
            Distribution("livekit", "1.1.14"),
            Distribution("websockets", "17.0.1"),
        ],
    )
    assert_runtime_distributions()


@pytest.mark.asyncio
async def test_run_worker_wires_guard_signals_and_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Guard:
        def assert_clean(self, names: set[str]) -> None:
            assert "voice_agent.main" in names
            events.append("clean")

        def install(self) -> None:
            events.append("installed")

    class Loop:
        def __init__(self) -> None:
            self.calls = 0

        def add_signal_handler(self, _signal: Any, _callback: Any) -> None:
            self.calls += 1
            if self.calls == 1:
                raise NotImplementedError
            events.append("signal")

    class Client:
        supervisor = object()

        async def run_forever(self, _stop: asyncio.Event) -> None:
            events.append("run")

    class Bridge:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["supervisor"] is Client.supervisor
            events.append("bridge")

        async def start(self) -> None:
            events.append("bridge-start")

        async def close(self) -> None:
            events.append("bridge-close")

    async def preflight(_config: WorkerConfig) -> None:
        events.append("preflight")

    monkeypatch.setattr(main_module, "RuntimeImportGuard", Guard)
    monkeypatch.setattr(
        main_module, "assert_runtime_distributions", lambda: events.append("closure")
    )
    monkeypatch.setattr(
        main_module,
        "build_pool_client",
        lambda _config: events.append("client") or Client(),
    )
    monkeypatch.setattr(main_module, "run_speech_preflight", preflight)
    monkeypatch.setattr(main_module, "WatchBridgeServer", Bridge)
    monkeypatch.setattr(main_module.asyncio, "get_running_loop", Loop)
    await main_module.run_worker(WorkerConfig.from_environ(_valid_environment()))
    assert events == [
        "clean",
        "installed",
        "closure",
        "signal",
        "preflight",
        "client",
        "bridge",
        "bridge-start",
        "run",
        "bridge-close",
    ]


@pytest.mark.parametrize(
    ("failure", "status", "prefix"),
    (
        (ConfigError("bad_config"), 78, "voice_worker_startup_failed"),
        (
            SpeechPreflightError("asr_unavailable"),
            78,
            "voice_worker_startup_failed",
        ),
        (ChallengeError("bad_challenge"), 1, "voice_worker_control_failed"),
        (KeyboardInterrupt(), 0, ""),
        (RuntimeError("secret-must-not-render"), 1, "voice_worker_failed:unexpected"),
        (None, 0, ""),
    ),
)
def test_main_returns_content_free_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException | None,
    status: int,
    prefix: str,
) -> None:
    def run(value: Any) -> None:
        value.close()
        if failure is not None:
            raise failure

    monkeypatch.setattr(main_module.asyncio, "run", run)
    assert main_module.main() == status
    rendered = capsys.readouterr().err
    assert rendered.startswith(prefix)
    assert "secret-must-not-render" not in rendered
