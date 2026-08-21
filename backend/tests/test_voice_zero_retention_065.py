"""Cross-channel zero-retention inspections for Feature 065.

The final transcript is retained exactly once as the user's ordinary chat
message. Voice coordination state, worker buffers, diagnostics, telemetry,
audit metadata, and crash representations must remain content-free.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import re
import types
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from astralplane.database.legacy_baseline_066 import _LegacyBaseline066Builder
from orchestrator.history import ConversationCommitRepository
from orchestrator.orchestrator import Orchestrator, _VoiceDispatchContext
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.voice_sessions import (
    TranscriptSubmission,
    VoiceSessionRecord,
    VoiceSessionRepository,
    VoiceTurnRecord,
)
from voice_agent.config import VoiceProfile, WorkerConfig
from voice_agent.session import (
    BoundControlSession,
    SessionBinding,
    SessionNotice,
    WorkerRtcGrant,
)
from voice_agent.speech_adapters import (
    HttpResponse,
    SpeachesBatchSTT,
    SpeechAdapterError,
)

NOW = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)
PHI = "Patient Jane Doe has record 123-45-6789"
TRANSCRIPT = f"private transcript: {PHI}"
RECAP = f"private completion recap: {PHI}"
SPEECH_ENDPOINT = "https://speech.private.invalid/v1"
SPEECH_KEY = "speech-api-key-private-065"
JOIN_TOKEN = "livekit-join-token-private-" + "x" * 40
WATCH_TICKET = "watch-ticket-private-065"
PROVIDER_BODY = '{"error":"provider-private-body-065"}'
AUDIO_MARKER = "raw-audio-private-065"
SENSITIVE_VALUES = (
    PHI,
    TRANSCRIPT,
    RECAP,
    SPEECH_ENDPOINT,
    SPEECH_KEY,
    JOIN_TOKEN,
    WATCH_TICKET,
    PROVIDER_BODY,
    AUDIO_MARKER,
)


def _uuid(number: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | number))


SESSION_ID = _uuid(1)
DEVICE_ID = _uuid(2)
CONNECTION_ID = _uuid(3)
CHAT_ID = _uuid(4)
TURN_ID = _uuid(5)
CLIENT_TURN_ID = _uuid(6)
SUBMISSION_ID = _uuid(7)
REQUEST_ID = _uuid(8)
ASSIGNMENT_ID = _uuid(9)


class _MigrationCursor:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str, *_args: Any) -> None:
        self.queries.append(str(query))

    @staticmethod
    def fetchone() -> dict[str, str]:
        return {
            "admission_class": "operation_admission_class",
            "admission_slot": "operation_admission_slot",
            "operation_record": "operation_record",
            "background_task": "background_task",
            "conversation_commit": "conversation_commit",
            "workspace_layout": "workspace_layout",
            "messages": "messages",
        }


def _ddl_columns(query: str, table: str) -> set[str]:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*)\n\s{{12}}\)\s*$",
        query,
        flags=re.DOTALL,
    )
    assert match is not None
    columns: set[str] = set()
    for line in match.group(1).splitlines():
        column = re.match(
            r"\s{16}([a-z][a-z0-9_]*)\s+"
            r"(?:UUID|TEXT|BIGINT|INTEGER|BOOLEAN|TIMESTAMPTZ|BYTEA)",
            line,
        )
        if column is not None:
            columns.add(column.group(1))
    return columns


def test_database_schema_is_metadata_only_and_message_is_single_source() -> None:
    cursor = _MigrationCursor()
    _LegacyBaseline066Builder()._migrate_conversational_voice_065(cursor)  # noqa: SLF001

    session_ddl = next(
        query
        for query in cursor.queries
        if "CREATE TABLE IF NOT EXISTS voice_session" in query
    )
    turn_ddl = next(
        query
        for query in cursor.queries
        if "CREATE TABLE IF NOT EXISTS voice_turn" in query
    )
    session_columns = _ddl_columns(session_ddl, "voice_session")
    turn_columns = _ddl_columns(turn_ddl, "voice_turn")
    forbidden_content_columns = {
        "audio",
        "audio_bytes",
        "raw_audio",
        "transcript",
        "transcript_text",
        "text",
        "recap",
        "recap_text",
        "summary_text",
        "speech_endpoint",
        "endpoint",
        "api_key",
        "token",
        "ticket",
        "provider_body",
        "phi",
    }
    assert session_columns.isdisjoint(forbidden_content_columns)
    assert turn_columns.isdisjoint(forbidden_content_columns)
    assert {field.name for field in fields(VoiceSessionRecord)}.isdisjoint(
        forbidden_content_columns
    )
    assert {field.name for field in fields(VoiceTurnRecord)}.isdisjoint(
        forbidden_content_columns
    )
    assert "message_id" in turn_columns

    # Acceptance owns the one typed ordinary user-message append. Proof
    # admission and the content-free voice correlation update cannot create a
    # duplicate or regain a raw persistence path in Deep.
    acceptance_source = inspect.getsource(
        ConversationCommitRepository.accept_voice_turn
    )
    assert acceptance_source.count("append_next_to_staged_publication") == 1
    assert "INSERT INTO messages" not in acceptance_source
    assert '"role": "user"' in acceptance_source
    assert "accept_turn" in acceptance_source
    assert "INSERT INTO messages" not in inspect.getsource(
        VoiceSessionRepository.admit_transcript
    )
    assert "INSERT INTO messages" not in inspect.getsource(
        VoiceSessionRepository.accept_transcript
    )


def _binding() -> SessionBinding:
    grant = WorkerRtcGrant(
        revision=1,
        livekit_url=SPEECH_ENDPOINT,
        join_token=JOIN_TOKEN,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        room_name="voice-room-1",
        worker_identity="voice-worker-a",
    )
    return SessionBinding(
        session_id=SESSION_ID,
        generation=1,
        assignment_id=ASSIGNMENT_ID,
        room_name="voice-room-1",
        worker_identity="voice-worker-a",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="client-a",
        grant_expires_at=NOW + timedelta(minutes=5),
        worker_rtc_grant=grant,
        visible_chat_id=CHAT_ID,
        chat_context_revision=1,
    )


@pytest.mark.asyncio
async def test_worker_buffer_cleanup_creates_no_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    binding = _binding()
    runtime = BoundControlSession(binding, queue_size=1)
    buffered = {
        "audio": [AUDIO_MARKER, b"\x01\x02" * 320],
        "transcript": TRANSCRIPT,
        "recap": RECAP,
        "speech_endpoint": SPEECH_ENDPOINT,
        "api_key": SPEECH_KEY,
        "join_token": JOIN_TOKEN,
        "watch_ticket": WATCH_TICKET,
        "provider_body": PROVIDER_BODY,
        "phi": PHI,
    }
    task = asyncio.create_task(runtime.run())
    runtime.deliver(buffered)
    await runtime.close("test")
    await task

    assert buffered == {}
    assert runtime._queue.empty()
    assert binding.worker_rtc_grant.join_token == ""
    assert list(tmp_path.rglob("*")) == []


class _SpeechTransport:
    async def post(self, _request: Any) -> HttpResponse:
        return HttpResponse(
            status=503,
            headers={"x-private-key": SPEECH_KEY},
            body=PROVIDER_BODY.encode(),
        )

    async def get(self, _request: Any) -> HttpResponse:
        raise AssertionError("inventory is not used")


@pytest.mark.asyncio
async def test_log_and_metric_surfaces_accept_only_content_free_reasons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="voice.retention.065")
    adapter = SpeachesBatchSTT(
        transport=_SpeechTransport(),
        api_key=SPEECH_KEY,
    )
    with pytest.raises(SpeechAdapterError) as caught:
        await adapter.transcribe_pcm16(b"\0\0" * 320)
    logging.getLogger("voice.retention.065").warning(
        "voice_asr_failed reason=%s",
        caught.value.reason,
    )
    assert caught.value.reason == "upstream_unavailable"
    assert "voice_asr_failed reason=upstream_unavailable" in caplog.text
    for value in SENSITIVE_VALUES:
        assert value not in caplog.text
        assert value not in str(caught.value)

    metrics = RuntimeObservability(deployment_instance="retention_test")
    metrics.record_voice_event(
        "turn",
        "failed",
        reason="speech_unavailable",
        client_kind="web",
        transport="livekit",
    )
    for value in SENSITIVE_VALUES:
        with pytest.raises(ValueError):
            metrics.record_voice_event("turn", "failed", reason=value)
    rendered_metrics = repr(metrics.snapshot())
    for value in SENSITIVE_VALUES:
        assert value not in rendered_metrics


class _NonClosingStringIO(io.StringIO):
    def close(self) -> None:
        self.flush()


def test_frame_trace_is_metadata_only_even_for_content_bearing_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _NonClosingStringIO()
    monkeypatch.setattr("orchestrator.orchestrator.os.path.exists", lambda _path: True)
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: sink)
    frame = {
        "type": "ui_render",
        "components": [
            {"type": "text", "content": value} for value in SENSITIVE_VALUES
        ],
    }
    Orchestrator._trace_frame(
        None,
        SimpleNamespace(),
        json.dumps(frame),
        ok=True,
    )
    Orchestrator._trace_frame(
        None,
        SimpleNamespace(),
        json.dumps(frame),
        ok=False,
        error=f"socket rejected {SENSITIVE_VALUES[0]}",
    )

    trace = sink.getvalue()
    records = [json.loads(line) for line in trace.splitlines()]
    assert [record["type"] for record in records] == ["ui_render", "ui_render"]
    assert all("[REDACTED]" in record["frame"] for record in records)
    assert records[0]["error"] == ""
    assert records[1]["error"] == "redacted_send_failure"
    for value in SENSITIVE_VALUES:
        assert value not in trace


def _voice_frame() -> dict[str, Any]:
    return {
        "type": "ui_event",
        "action": "chat_message",
        "session_id": CHAT_ID,
        "submission_id": SUBMISSION_ID,
        "request_generation": REQUEST_ID,
        "connection_generation": CONNECTION_ID,
        "payload": {
            "chat_id": CHAT_ID,
            "message": " | ".join(SENSITIVE_VALUES),
            "summary_text": RECAP,
            "speech_endpoint": SPEECH_ENDPOINT,
            "api_key": SPEECH_KEY,
            "provider_body": PROVIDER_BODY,
            "voice_origin": {
                "schema_version": "1",
                "session_id": SESSION_ID,
                "generation": 1,
                "media_grant_revision": 1,
                "turn_id": TURN_ID,
                "client_turn_id": CLIENT_TURN_ID,
                "chat_context_revision": 1,
                "source_participant_identity": "voice-worker-a",
                "detected_language": "en",
                "text_digest_sha256": "a" * 64,
                "transcript_proof": "b" * 64,
                "proof_expires_at": (datetime.now(UTC) + timedelta(minutes=1))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            },
        },
    }


@pytest.mark.asyncio
async def test_voice_audit_retains_only_correlation_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = object()
    raw = _voice_frame()
    captured: list[dict[str, Any]] = []

    async def record_ws_action(**values: Any) -> None:
        captured.append(values)

    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", record_ws_action)
    runtime = SimpleNamespace()
    runtime.ui_sessions = {websocket: {"sub": "retention-owner"}}
    runtime._get_user_id = lambda _websocket: "retention-owner"
    runtime._parsed_ui_frame = Orchestrator._parsed_ui_frame
    runtime._ws_active_chat = {id(websocket): CHAT_ID}
    runtime.cancelled_sessions = {}

    async def retire(_websocket: object) -> None:
        raise AssertionError("voice input must not use typed welcome teardown")

    async def admit(
        _websocket: object,
        _msg: Any,
        **_kwargs: Any,
    ) -> _VoiceDispatchContext:
        return _VoiceDispatchContext(
            admission=SimpleNamespace(canonical_text=TRANSCRIPT),
            connection_generation=CONNECTION_ID,
            origin=SimpleNamespace(**raw["payload"]["voice_origin"]),
        )

    async def dispatched(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def safe_send(*_args: Any, **_kwargs: Any) -> bool:
        return True

    runtime._retire_welcome_canvas = retire
    runtime._admit_voice_chat_message = admit
    runtime._serialized_chat = dispatched
    runtime._safe_send = safe_send
    runtime.handle_ui_message = types.MethodType(
        Orchestrator.handle_ui_message,
        runtime,
    )

    await runtime.handle_ui_message(websocket, json.dumps(raw))
    await asyncio.sleep(0)
    assert len(captured) == 1
    audit_payload = captured[0]["payload"]
    assert set(audit_payload) == {
        "voice_origin",
        "chat_id",
        "submission_id",
        "request_generation",
    }
    assert set(audit_payload["voice_origin"]) == {
        "schema_version",
        "session_id",
        "generation",
        "media_grant_revision",
        "turn_id",
        "client_turn_id",
        "chat_context_revision",
    }
    serialized = json.dumps(audit_payload, sort_keys=True)
    for value in SENSITIVE_VALUES:
        assert value not in serialized
    assert "transcript_proof" not in serialized
    assert "text_digest_sha256" not in serialized
    assert "source_participant_identity" not in serialized


def test_crash_representations_redact_content_and_authority() -> None:
    config = WorkerConfig(
        environment="production",
        control_url="wss://control.private.invalid/worker",
        control_secret=b"control-secret-private-at-least-32-bytes",
        worker_identity="voice-worker-a",
        max_sessions=1,
        runtime_closure_sha256="c" * 64,
        watch_bridge_listen_host="127.0.0.1",
        watch_bridge_listen_port=7890,
        speech_base_url=SPEECH_ENDPOINT,
        speech_api_key=SPEECH_KEY,
        profile=VoiceProfile(),
    )
    submission = TranscriptSubmission(
        user_id="retention-owner",
        session_id=SESSION_ID,
        generation=1,
        media_grant_revision=1,
        turn_id=TURN_ID,
        client_turn_id=CLIENT_TURN_ID,
        submission_id=SUBMISSION_ID,
        request_generation=REQUEST_ID,
        chat_id=CHAT_ID,
        chat_context_revision=1,
        source_participant_identity="voice-worker-a",
        detected_language="en",
        text=TRANSCRIPT,
        text_digest_sha256="a" * 64,
        transcript_proof=JOIN_TOKEN,
        proof_expires_at="2026-08-01T16:01:00Z",
    )
    notice = SessionNotice(
        "transcript_emitted",
        text=RECAP,
        metadata={
            "provider_body": PROVIDER_BODY,
            "watch_ticket": WATCH_TICKET,
            "phi": PHI,
        },
    )
    grant = _binding().worker_rtc_grant
    rendered = repr((config, submission, notice, grant))
    for value in SENSITIVE_VALUES:
        assert value not in rendered
    assert "control-secret-private-at-least-32-bytes" not in rendered
    assert "wss://control.private.invalid/worker" not in rendered
    assert "<redacted>" in rendered
