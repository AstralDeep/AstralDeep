"""Production worker wiring and control-notice integration guards for Feature 065."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest
import voice_agent.main as main_module
from voice_agent.config import WorkerConfig
from voice_agent.control import PoolClient, ProtocolViolation
from voice_agent.session import (
    DirectRtcSession,
    SessionBinding,
    SessionNotice,
    SessionSupervisor,
    WorkerRtcGrant,
)

NOW = datetime(2026, 7, 31, 16, 0, tzinfo=UTC)


def _uuid(value: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | value))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _config() -> WorkerConfig:
    return WorkerConfig.from_environ(
        {
            "ASTRAL_ENV": "production",
            "ASTRAL_VOICE_CONTROL_URL": "wss://control.internal/voice",
            "VOICE_CONTROL_SECRET": "c" * 32,
            "VOICE_WORKER_IDENTITY": "voice-worker-a",
            "VOICE_WORKER_MAX_SESSIONS": "2",
            "VOICE_WORKER_CLOSURE_SHA256": "a" * 64,
            "VOICE_SPEECH_BASE_URL": "https://speech.internal/v1",
            "VOICE_SPEECH_API_KEY": "speech-key",
        }
    )


def _binding() -> SessionBinding:
    return SessionBinding(
        session_id=_uuid(1),
        generation=1,
        assignment_id=_uuid(2),
        room_name="room-a",
        worker_identity="voice-worker-a",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-a",
        grant_expires_at=NOW + timedelta(minutes=4),
        worker_rtc_grant=WorkerRtcGrant(
            revision=1,
            livekit_url="wss://livekit.internal",
            join_token="token-" + "x" * 32,
            issued_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=4),
            room_name="room-a",
            worker_identity="voice-worker-a",
        ),
        visible_chat_id=_uuid(3),
        chat_context_revision=1,
    )


def _base(frame_type: str, sequence: int, **values: Any) -> dict[str, Any]:
    return {
        "type": frame_type,
        "schema_version": "1",
        "message_id": _uuid(100 + sequence),
        "session_id": _uuid(1),
        "generation": 1,
        "sequence": sequence,
        "sent_at": _iso(NOW),
        **values,
    }


def _speak_frame(**updates: Any) -> dict[str, Any]:
    frame = _base(
        "speak",
        0,
        announcement_id=_uuid(20),
        announcement_sequence=1,
        media_grant_revision=1,
        transport="livekit",
        turn_id=None,
        kind="greeting",
        quantum_role="single",
        quantum_index=0,
        max_duration_samples=96_000,
        text="Hello. What can I help with?",
        sensitive_authorized=False,
        expires_at=_iso(NOW + timedelta(minutes=1)),
    )
    frame.update(updates)
    return frame


class RecordingRuntime:
    def __init__(self, binding: SessionBinding) -> None:
        self.binding = binding
        self.delivered: list[dict[str, Any]] = []
        self.closed = asyncio.Event()

    async def run(self) -> None:
        await self.closed.wait()

    def deliver(self, frame: dict[str, Any]) -> None:
        self.delivered.append(frame)

    async def close(self, reason: str) -> None:
        del reason
        self.closed.set()

    @property
    def media_state(self) -> str:
        return "ready"


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        raise EOFError

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason


@pytest.mark.asyncio
async def test_pool_delivers_strict_supported_coordinator_frames_in_order() -> None:
    runtimes: list[RecordingRuntime] = []

    def runtime_factory(binding: SessionBinding) -> RecordingRuntime:
        runtime = RecordingRuntime(binding)
        runtimes.append(runtime)
        return runtime

    supervisor = SessionSupervisor(max_sessions=1, session_factory=runtime_factory)
    await supervisor.start(_binding())
    client = PoolClient(_config(), supervisor=supervisor, utcnow=lambda: NOW)
    frames = [
        _base(
            "set_capture",
            0,
            media_grant_revision=1,
            enabled=True,
        ),
        _base(
            "session_context_update",
            1,
            media_grant_revision=1,
            visible_chat_id=_uuid(4),
            chat_context_revision=2,
        ),
        _base(
            "speak",
            2,
            announcement_id=_uuid(20),
            announcement_sequence=1,
            media_grant_revision=1,
            transport="livekit",
            turn_id=None,
            kind="greeting",
            quantum_role="single",
            quantum_index=0,
            max_duration_samples=96_000,
            text="Hello. What can I help with?",
            sensitive_authorized=False,
            expires_at=_iso(NOW + timedelta(minutes=1)),
        ),
        _base(
            "stop_speech",
            3,
            media_grant_revision=1,
            announcement_id=_uuid(20),
            reason="user_stop",
        ),
        _base(
            "media_grant_rotated",
            4,
            refresh_id=_uuid(21),
            previous_media_grant_revision=1,
            media_grant_revision=2,
            client_participant_identity="client-b",
            transport="livekit",
            grant_expires_at=_iso(NOW + timedelta(minutes=3)),
        ),
        _base(
            "set_capture",
            5,
            media_grant_revision=2,
            enabled=False,
        ),
        _base(
            "turn_bound",
            6,
            client_turn_id=_uuid(30),
            turn_id=_uuid(31),
            chat_id=_uuid(3),
            chat_context_revision=1,
            media_grant_revision=1,
            submission_id=_uuid(32),
            request_generation=_uuid(33),
        ),
        _base(
            "transcript_accepted",
            7,
            turn_id=_uuid(31),
            client_turn_id=_uuid(30),
            submission_id=_uuid(32),
            request_generation=_uuid(33),
            chat_id=_uuid(3),
            media_grant_revision=1,
            accepted_message_id=17,
        ),
    ]

    for frame in frames:
        await client._dispatch(frame)

    assert [frame["type"] for frame in runtimes[0].delivered] == [
        "set_capture",
        "session_context_update",
        "speak",
        "stop_speech",
        "media_grant_rotated",
        "set_capture",
        "turn_bound",
        "transcript_accepted",
    ]
    await supervisor.end(_uuid(1), 1, 2, "user")


@pytest.mark.asyncio
async def test_invalid_supported_frame_does_not_consume_sequence() -> None:
    supervisor = SessionSupervisor(max_sessions=1, session_factory=RecordingRuntime)
    await supervisor.start(_binding())
    client = PoolClient(_config(), supervisor=supervisor, utcnow=lambda: NOW)
    malformed = _base(
        "set_capture",
        0,
        media_grant_revision=1,
        enabled=True,
        unexpected=True,
    )
    with pytest.raises(ProtocolViolation, match="invalid_set_capture_fields"):
        await client._dispatch(malformed)

    valid = _base("set_capture", 0, media_grant_revision=1, enabled=True)
    await client._dispatch(valid)

    invalid_result = _base(
        "speak",
        1,
        announcement_id=_uuid(20),
        announcement_sequence=1,
        media_grant_revision=1,
        transport="livekit",
        turn_id=_uuid(22),
        kind="result",
        quantum_role="single",
        quantum_index=0,
        max_duration_samples=96_000,
        text="Result",
        sensitive_authorized=False,
        expires_at=_iso(NOW + timedelta(minutes=1)),
    )
    with pytest.raises(ProtocolViolation, match="invalid_speak_quantum"):
        await client._dispatch(invalid_result)
    await supervisor.shutdown("test")


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"transport": "other"}, "invalid_transport"),
        ({"kind": "other"}, "invalid_speech_kind"),
        ({"turn_id": _uuid(22)}, "announcement_turn_mismatch"),
        ({"quantum_role": "other"}, "invalid_speak_quantum"),
        ({"phrase_key": "not allowed"}, "invalid_phrase_key"),
        ({"text": "   "}, "invalid_speech_text"),
        ({"sensitive_authorized": 1}, "invalid_sensitive_authorization"),
        (
            {"expires_at": _iso(NOW - timedelta(seconds=1))},
            "speak_command_expired",
        ),
    ],
)
def test_speak_validator_rejects_each_sensitive_boundary(
    updates: dict[str, Any], reason: str
) -> None:
    client = PoolClient(_config(), utcnow=lambda: NOW)
    with pytest.raises(ProtocolViolation, match=reason):
        client._validate_speak(_speak_frame(**updates))


def test_speak_validator_accepts_bounded_result_quanta() -> None:
    client = PoolClient(_config(), utcnow=lambda: NOW)
    opening = _speak_frame(
        turn_id=_uuid(22),
        kind="result",
        quantum_role="result_opening",
        max_duration_samples=36_000,
        result_reserved_samples_after=36_000,
        phrase_key="result.opening",
    )
    client._validate_speak(opening)
    continuation = _speak_frame(
        turn_id=_uuid(22),
        kind="result",
        quantum_role="result_continuation",
        quantum_index=1,
        result_reserved_samples_after=96_000,
    )
    client._validate_speak(continuation)


def test_supported_frame_and_notice_constructor_fail_closed() -> None:
    with pytest.raises(ValueError, match="invalid_notice_queue_size"):
        PoolClient(_config(), notice_queue_size=0)
    client = PoolClient(_config(), utcnow=lambda: NOW)
    with pytest.raises(ProtocolViolation, match="invalid_session_notice"):
        client.enqueue_session_notice(_binding(), object())
    assert (
        client.enqueue_session_notice(_binding(), SessionNotice("internal_only"))
        is False
    )
    invalid_capture = _base("set_capture", 0, media_grant_revision=1, enabled=1)
    with pytest.raises(ProtocolViolation, match="invalid_capture_state"):
        client._validate_set_capture(invalid_capture)


@pytest.mark.asyncio
async def test_notice_queue_is_bounded_content_free_and_sequence_fenced() -> None:
    client = PoolClient(
        _config(),
        utcnow=lambda: NOW,
        notice_queue_size=2,
    )
    binding = _binding()
    assert client.enqueue_session_notice(
        binding,
        SessionNotice(
            "worker_ready",
            metadata={
                "assignment_id": _uuid(2),
                "worker_identity": "voice-worker-a",
                "worker_rtc_grant_revision": 1,
                "profile_ready": True,
            },
        ),
    )
    assert client.enqueue_session_notice(
        binding,
        SessionNotice(
            "media_state",
            metadata={"state": "listening", "text": "must-not-queue"},
        ),
    )
    with pytest.raises(ProtocolViolation, match="outbound_notice_queue_full"):
        client.enqueue_session_notice(
            binding,
            SessionNotice("media_state", metadata={"state": "failed"}),
        )
    assert (
        client.enqueue_session_notice(
            binding,
            SessionNotice("final_transcript", text="private transcript"),
        )
        is False
    )

    socket = RecordingSocket()
    await asyncio.wait_for(client._send_next_notice(socket), timeout=1)
    await asyncio.wait_for(client._send_next_notice(socket), timeout=1)
    frames = [json.loads(item) for item in socket.sent]
    assert [item["sequence"] for item in frames] == [0, 1]
    assert [item["type"] for item in frames] == ["worker_ready", "media_state"]
    assert "text" not in socket.sent[1]
    assert "must-not-queue" not in "".join(socket.sent)


@pytest.mark.asyncio
async def test_notice_translation_covers_content_free_worker_frames() -> None:
    client = PoolClient(_config(), utcnow=lambda: NOW)
    binding = _binding()
    notices = [
        SessionNotice(
            "session_context_applied",
            metadata={
                "media_grant_revision": 1,
                "visible_chat_id": _uuid(3),
                "chat_context_revision": 1,
            },
        ),
        SessionNotice(
            "media_grant_applied",
            metadata={
                "refresh_id": _uuid(10),
                "media_grant_revision": 1,
                "client_participant_identity": "client-a",
            },
        ),
        SessionNotice(
            "recognition_started",
            metadata={
                "client_turn_id": _uuid(11),
                "media_grant_revision": 1,
                "visible_chat_id": _uuid(3),
                "chat_context_revision": 1,
            },
        ),
        SessionNotice(
            "recognition_failed",
            reason="asr_failed",
            metadata={"client_turn_id": _uuid(11)},
        ),
        SessionNotice(
            "speech_finished",
            announcement_id=_uuid(12),
            metadata={
                "announcement_sequence": 1,
                "media_grant_revision": 1,
                "turn_id": None,
                "kind": "greeting",
                "quantum_role": "single",
                "quantum_index": 0,
                "duration_ms": 40,
            },
        ),
        SessionNotice(
            "transcript_emitted",
            language="en",
            metadata={
                "turn_id": _uuid(13),
                "client_turn_id": _uuid(11),
                "submission_id": _uuid(14),
                "request_generation": _uuid(15),
                "chat_id": _uuid(3),
                "chat_context_revision": 1,
                "media_grant_revision": 1,
                "final": True,
                "utf8_bytes": 12,
                "text_digest_sha256": "b" * 64,
                "proof_expires_at": _iso(NOW + timedelta(minutes=1)),
                "transcript_proof": "must-not-cross-control",
            },
        ),
    ]
    for notice in notices:
        assert client.enqueue_session_notice(binding, notice)

    socket = RecordingSocket()
    for _notice in notices:
        await asyncio.wait_for(client._send_next_notice(socket), timeout=1)
    frames = [json.loads(item) for item in socket.sent]
    assert [item["type"] for item in frames] == [
        "session_context_applied",
        "media_grant_applied",
        "recognition_started",
        "recognition_failed",
        "speech_finished",
        "transcript_emitted",
    ]
    assert [item["sequence"] for item in frames] == list(range(len(notices)))
    assert all("occurred_at" in item for item in frames)
    rendered = "".join(socket.sent)
    assert "transcript_proof" not in rendered
    assert "must-not-cross-control" not in rendered


def test_notice_translation_rejects_malformed_internal_metadata() -> None:
    client = PoolClient(_config(), utcnow=lambda: NOW)
    binding = _binding()
    transcript_metadata = {
        "turn_id": _uuid(13),
        "client_turn_id": _uuid(14),
        "submission_id": _uuid(15),
        "request_generation": _uuid(16),
        "chat_id": _uuid(3),
        "chat_context_revision": 1,
        "media_grant_revision": 1,
        "final": True,
        "utf8_bytes": 4,
        "text_digest_sha256": "b" * 64,
        "proof_expires_at": _iso(NOW + timedelta(minutes=1)),
    }
    notices = [
        SessionNotice("media_state", metadata={"state": "other"}),
        SessionNotice(
            "transcript_emitted",
            language="x",
            metadata=transcript_metadata,
        ),
        SessionNotice(
            "transcript_emitted",
            language="en",
            metadata={**transcript_metadata, "text_digest_sha256": "bad"},
        ),
        SessionNotice(
            "speech_failed",
            announcement_id=_uuid(12),
            metadata={"kind": "other", "quantum_role": "single"},
        ),
        SessionNotice(
            "speech_failed",
            announcement_id=_uuid(12),
            metadata={
                "announcement_sequence": 1,
                "media_grant_revision": 1,
                "turn_id": _uuid(13),
                "kind": "greeting",
                "quantum_role": "single",
                "quantum_index": 0,
            },
        ),
        SessionNotice(
            "recognition_failed",
            reason="provider_body",
            metadata={"client_turn_id": _uuid(14)},
        ),
        SessionNotice(
            "recognition_failed",
            reason="asr_failed",
            metadata={"client_turn_id": "not-a-uuid"},
        ),
    ]
    reasons = [
        "invalid_notice_media_state",
        "invalid_notice_language",
        "invalid_notice_transcript_digest",
        "invalid_notice_speech_binding",
        "invalid_notice_speech_binding",
        "invalid_notice_recognition_failure_reason",
        "invalid_notice_client_turn_id",
    ]
    for notice, reason in zip(notices, reasons, strict=True):
        assert client.enqueue_session_notice(binding, notice)
        queued = client._outbound_notices.get_nowait()
        with pytest.raises(ProtocolViolation, match=reason):
            client._notice_payload(queued)


def test_speech_notice_translation_includes_turn_reservation_and_reason() -> None:
    client = PoolClient(_config(), utcnow=lambda: NOW)
    binding = _binding()
    notice = SessionNotice(
        "speech_failed",
        reason="tts_failed",
        announcement_id=_uuid(12),
        metadata={
            "announcement_sequence": 2,
            "media_grant_revision": 1,
            "turn_id": _uuid(13),
            "kind": "result",
            "quantum_role": "result_continuation",
            "quantum_index": 1,
            "result_reserved_samples_after": 96_000,
            "duration_ms": 500,
        },
    )
    assert client.enqueue_session_notice(binding, notice)
    payload = client._notice_payload(client._outbound_notices.get_nowait())
    assert payload["turn_id"] == _uuid(13)
    assert payload["result_reserved_samples_after"] == 96_000
    assert payload["duration_ms"] == 500
    assert payload["reason"] == "tts_failed"


def test_production_builder_constructs_direct_rtc_with_shared_speech_adapters() -> None:
    transport = object()
    rtc_factory = object()
    vad_instances: list[object] = []

    def vad_factory() -> object:
        value = object()
        vad_instances.append(value)
        return value

    client = main_module.build_pool_client(
        _config(),
        transport=transport,
        rtc_factory=rtc_factory,
        vad_factory=vad_factory,
    )
    runtime = client.supervisor._session_factory(_binding())
    assert isinstance(runtime, DirectRtcSession)
    assert runtime._rtc_factory is rtc_factory
    assert runtime._vad is vad_instances[0]
    assert runtime._asr._transport is transport
    assert runtime._tts._transport is transport
    assert runtime._asr._api_key == "speech-key"
    assert runtime._tts._api_key == "speech-key"
    assert (
        runtime._notice_sink(SessionNotice("final_transcript", text="secret")) is False
    )
    watch_binding = _binding()
    watch_binding.transport = "watch_pcm_websocket"
    watch_runtime = client.supervisor._session_factory(watch_binding)
    assert isinstance(watch_runtime, main_module.WatchPcmSession)
    assert watch_runtime._asr is runtime._asr
    assert watch_runtime._tts is runtime._tts


def test_production_builder_constructs_the_fixed_origin_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []

    class Transport:
        def __init__(
            self,
            origin: str,
            *,
            allow_insecure_loopback_development: bool,
        ) -> None:
            calls.append((origin, allow_insecure_loopback_development))

    module = ModuleType("voice_agent.streaming_egress")
    module.FixedOriginHttpTransport = Transport
    monkeypatch.setitem(sys.modules, "voice_agent.streaming_egress", module)
    client = main_module.build_pool_client(
        _config(), rtc_factory=object(), vad_factory=object
    )
    assert isinstance(client, PoolClient)
    assert calls == [("https://speech.internal/v1", False)]


@pytest.mark.asyncio
async def test_run_worker_uses_production_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Client:
        supervisor = object()

        async def run_forever(self, stop: asyncio.Event) -> None:
            assert isinstance(stop, asyncio.Event)
            events.append("run")

    class Bridge:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["supervisor"] is Client.supervisor
            events.append("bridge")

        async def start(self) -> None:
            events.append("bridge-start")

        async def close(self) -> None:
            events.append("bridge-close")

    class Guard:
        def assert_clean(self, names: set[str]) -> None:
            assert "voice_agent.main" in names

        def install(self) -> None:
            return None

    monkeypatch.setattr(main_module, "RuntimeImportGuard", Guard)
    monkeypatch.setattr(main_module, "assert_runtime_distributions", lambda: None)

    async def preflight(_config: WorkerConfig) -> None:
        events.append("preflight")

    monkeypatch.setattr(main_module, "run_speech_preflight", preflight)
    monkeypatch.setattr(main_module, "build_pool_client", lambda config: Client())
    monkeypatch.setattr(main_module, "WatchBridgeServer", Bridge)
    await main_module.run_worker(_config())
    assert events == [
        "preflight",
        "bridge",
        "bridge-start",
        "run",
        "bridge-close",
    ]
