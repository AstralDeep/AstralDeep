"""Client-local transcript admission invariants for Feature 075."""

from __future__ import annotations

import hashlib
import inspect
import json
import unicodedata
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_control_binding import (
    ClientLocalBindingRegistry,
    VoiceControlBindingError,
)
from orchestrator.voice_sessions import (
    LocalTranscriptSubmission,
    TranscriptSubmissionRejected,
    VoiceSessionRepository,
    canonicalize_local_transcript,
)
from shared.protocol import (
    VoiceLocalFinal,
    VoiceLocalPlayoutEvent,
    VoiceLocalReady,
    VoiceLocalRecognitionFailed,
    VoiceLocalRecognitionStarted,
)


NOW = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)


def _id() -> str:
    return str(uuid.uuid4())


def _final(**changes: object) -> VoiceLocalFinal:
    text = changes.pop("text", "  Cafe\u0301\r\nstatus  ")
    canonical = unicodedata.normalize("NFC", str(text).replace("\r\n", "\n")).strip()
    values = {
        "device_id": _id(),
        "connection_generation": _id(),
        "session_id": _id(),
        "generation": 1,
        "speech_revision": 1,
        "client_turn_id": _id(),
        "turn_id": _id(),
        "submission_id": _id(),
        "request_generation": _id(),
        "chat_id": _id(),
        "chat_context_revision": 1,
        "recognition_sequence": 1,
        "recognized_locale": "en-US",
        "text": text,
        "text_digest_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    values.update(changes)
    return VoiceLocalFinal(**values)


def _authority(frame: VoiceLocalFinal):
    return type(
        "Authority",
        (),
        {
            "socket_id": 7,
            "user_id": "owner-a",
            "device_id": frame.device_id,
            "connection_generation": frame.connection_generation,
            "binding_id": _id(),
            "session_id": frame.session_id,
            "generation": frame.generation,
            "speech_revision": frame.speech_revision,
            "client_turn_id": frame.client_turn_id,
            "turn_id": frame.turn_id,
            "submission_id": frame.submission_id,
            "request_generation": frame.request_generation,
            "chat_id": frame.chat_id,
            "chat_context_revision": frame.chat_context_revision,
            "recognition_sequence": frame.recognition_sequence,
            "expires_at": NOW + timedelta(minutes=2),
        },
    )()


def test_local_text_is_nfc_line_canonical_and_digest_verified() -> None:
    text = "  Cafe\u0301\r\nstatus  "
    expected = "Café\nstatus"
    digest = hashlib.sha256(expected.encode()).hexdigest()

    assert canonicalize_local_transcript(text, digest) == expected
    with pytest.raises(TranscriptSubmissionRejected, match="malformed_final"):
        canonicalize_local_transcript(text, "0" * 64)


@pytest.mark.parametrize(
    "text",
    ["", " \r\n ", "hello\x00", "hello\x07", "x" * 8001, "U0001f642" * 8001],
)
def test_local_text_rejects_empty_control_or_oversize(text: str) -> None:
    digest = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(TranscriptSubmissionRejected):
        canonicalize_local_transcript(text, digest)


def test_local_final_exact_replay_deduplicates_but_altered_replay_is_rejected() -> None:
    registry = ClientLocalBindingRegistry()
    frame = _final()
    authority = _authority(frame)
    registry._turns[("owner-a", frame.client_turn_id)] = authority

    canonical, replayed = registry.verify_final(
        socket_id=7,
        current_socket_id=7,
        user_id="owner-a",
        frame=frame,
        now=NOW,
    )
    assert canonical == "Café\nstatus"
    assert replayed is False
    assert registry.verify_final(
        socket_id=7,
        current_socket_id=7,
        user_id="owner-a",
        frame=frame,
        now=NOW,
    )[1] is True

    altered = _final(**{
        **frame.__dict__,
        "text": "different",
        "text_digest_sha256": hashlib.sha256(b"different").hexdigest(),
    })
    with pytest.raises(VoiceControlBindingError, match="altered_local_final"):
        registry.verify_final(
            socket_id=7,
            current_socket_id=7,
            user_id="owner-a",
            frame=altered,
            now=NOW,
        )


def test_local_submission_is_content_redacted_and_remote_hmac_lane_unchanged() -> None:
    frame = _final()
    request = LocalTranscriptSubmission.from_authority(
        user_id="owner-a",
        authority=_authority(frame),
        detected_language="en",
        canonical_text="Café\nstatus",
    )
    assert "Café" not in repr(request)
    remote_source = inspect.getsource(VoiceSessionRepository.admit_transcript)
    local_source = inspect.getsource(VoiceSessionRepository.admit_local_transcript)
    assert "worker_control_secret" in remote_source
    assert "verify_transcript_proof" in remote_source
    assert "worker_control_secret" not in local_source
    assert "verify_transcript_proof" not in local_source


def test_real_local_repository_admission_transitions_and_replays_exact_turn() -> None:
    frame = _final(text="hello")
    authority = _authority(frame)
    authority.binding_id = _id()
    request = LocalTranscriptSubmission.from_authority(
        user_id="owner-a",
        authority=authority,
        detected_language="en",
        canonical_text="hello",
    )
    row: dict[str, object] = {
        "turn_id": frame.turn_id,
        "client_turn_id": frame.client_turn_id,
        "session_id": frame.session_id,
        "session_generation": 1,
        "media_grant_revision": 1,
        "user_id": "owner-a",
        "chat_id": frame.chat_id,
        "chat_context_revision": 1,
        "execution_base_render_revision": 0,
        "submission_id": frame.submission_id,
        "request_generation": frame.request_generation,
        "result_request_generation": None,
        "message_id": None,
        "state": "recognizing",
        "is_foreground": True,
        "detected_language": None,
        "spoken_output_policy": "none",
        "output_reason": "none",
        "created_at": NOW,
        "updated_at": NOW,
    }
    session = {
        "session_id": frame.session_id,
        "user_id": "owner-a",
        "device_id": frame.device_id,
        "owner_connection_generation": frame.connection_generation,
        "control_binding_id": authority.binding_id,
        "control_binding_expires_at": NOW + timedelta(minutes=4),
        "lease_expires_at": NOW + timedelta(minutes=2),
        "generation": 1,
        "media_grant_revision": 1,
        "speech_backend": "client_local",
        "worker_assignment_id": None,
        "state": "active",
        "foreground_active": True,
        "microphone_enabled": True,
        "speech_muted": False,
        "visible_chat_id": frame.chat_id,
        "chat_context_revision": 1,
        "applied_visible_chat_id": frame.chat_id,
        "applied_chat_context_revision": 1,
        "ended_at": None,
    }

    def patch_turn_record(
        _transaction: object,
        *,
        updates: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        row.update(updates)
        return row

    voice = SimpleNamespace(
        get_turn_record=Mock(return_value=row),
        get_session_record=Mock(return_value=session),
        patch_turn_record=Mock(side_effect=patch_turn_record),
    )
    repository = VoiceSessionRepository.__new__(VoiceSessionRepository)
    repository._voice = voice
    repository._transaction = lambda: nullcontext(object())
    repository._new_uuid4 = lambda _label: _id()

    admitted = repository.admit_local_transcript(request, now=NOW)
    assert admitted.canonical_text == "hello"
    assert admitted.turn.state == "submitting"
    assert admitted.replayed is False
    assert voice.patch_turn_record.call_count == 1

    replay = repository.admit_local_transcript(request, now=NOW)
    assert replay.turn.turn_id == frame.turn_id
    assert replay.replayed is True

    row["detected_language"] = "fr"
    with pytest.raises(TranscriptSubmissionRejected, match="invalid_binding"):
        repository.admit_local_transcript(request, now=NOW)
    row["detected_language"] = "en"
    session["speech_muted"] = True
    with pytest.raises(TranscriptSubmissionRejected, match="invalid_binding"):
        repository.admit_local_transcript(request, now=NOW)


def test_local_dispatch_handler_has_only_the_ordinary_chat_entrypoint() -> None:
    source = inspect.getsource(Orchestrator._handle_voice_local_final)
    assert "handle_chat_message" in source
    for forbidden in (
        "route_request",
        "call_tool",
        "_dispatch_to_agent",
        "add_message",
        "publish_result",
    ):
        assert forbidden not in source


def _local_orchestrator(frame: VoiceLocalFinal, services: object) -> tuple[Orchestrator, object]:
    orchestrator = Orchestrator.__new__(Orchestrator)
    websocket = object()
    claims = SimpleNamespace(
        subject="owner-a",
        device_id=frame.device_id,
        connection_generation=frame.connection_generation,
        binding_id=_id(),
        expires_at=NOW + timedelta(minutes=4),
    )
    orchestrator.ui_sessions = {websocket: {"sub": "owner-a"}}
    orchestrator._voice_control_bindings = {id(websocket): claims}
    orchestrator._voice_device_bindings = {
        ("owner-a", frame.device_id): id(websocket)
    }
    orchestrator.voice_services = services
    orchestrator.history = SimpleNamespace(
        get_chat=lambda chat_id, *, user_id: {
            "chat_id": chat_id,
            "user_id": user_id,
            "render_revision": 7,
        }
    )
    orchestrator._safe_send = AsyncMock(return_value=True)
    orchestrator.handle_chat_message = AsyncMock()
    return orchestrator, websocket


@pytest.mark.asyncio
async def test_local_socket_handlers_bind_admit_dispatch_and_publish_exactly() -> None:
    final = _final()
    session = SimpleNamespace(
        device_id=final.device_id,
        owner_connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=final.generation,
        media_grant_revision=final.speech_revision,
        visible_chat_id=final.chat_id,
        chat_context_revision=final.chat_context_revision,
        applied_chat_context_revision=final.chat_context_revision,
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    turn = SimpleNamespace(
        turn_id=final.turn_id,
        submission_id=final.submission_id,
        request_generation=final.request_generation,
        chat_id=final.chat_id,
        chat_context_revision=final.chat_context_revision,
    )
    authority = _authority(final)
    services = SimpleNamespace(
        local_ready=AsyncMock(return_value=session),
        bind_local_recognition=AsyncMock(return_value=(turn, authority)),
        admit_local_final=AsyncMock(
            return_value=SimpleNamespace(canonical_text="Café\nstatus")
        ),
        handle_local_playout=AsyncMock(),
        local_bindings=SimpleNamespace(release_turn=Mock()),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    claims = orchestrator._client_local_socket_authority(websocket)
    assert claims[1:] == (
        "owner-a",
        orchestrator._voice_control_bindings[id(websocket)],
        id(websocket),
    )

    await orchestrator._handle_voice_local_ready(
        websocket,
        VoiceLocalReady(
            device_id=final.device_id,
            connection_generation=final.connection_generation,
            session_id=final.session_id,
            generation=1,
            speech_revision=1,
            client_sequence=1,
        ),
    )
    started = VoiceLocalRecognitionStarted(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id=final.client_turn_id,
        chat_id=final.chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
    )
    await orchestrator._handle_voice_local_recognition_started(websocket, started)
    await orchestrator._handle_voice_local_final(websocket, final)
    orchestrator.handle_chat_message.assert_awaited_once()
    services.local_bindings.release_turn.assert_called_once()

    announcement = SimpleNamespace(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        to_json=lambda: '{"type":"voice_local_announcement"}',
    )
    await orchestrator.publish_voice_local_announcement(announcement)

    playout = VoiceLocalPlayoutEvent(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        announcement_id=_id(),
        announcement_sequence=1,
        turn_id=final.turn_id,
        kind="failure",
        phase="started",
        client_sequence=1,
        observed_at="2026-08-28T18:00:00Z",
    )
    await orchestrator._handle_voice_local_playout_event(websocket, playout)
    services.handle_local_playout.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_socket_handlers_fail_closed_with_correlated_rejections() -> None:
    final = _final()
    orchestrator, websocket = _local_orchestrator(final, None)
    ready = VoiceLocalReady(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        client_sequence=1,
    )
    started = VoiceLocalRecognitionStarted(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id=final.client_turn_id,
        chat_id=final.chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
    )

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await orchestrator._handle_voice_local_ready(websocket, ready)
    await orchestrator._handle_voice_local_final(websocket, final)
    assert json.loads(orchestrator._safe_send.await_args.args[1])["reason"] == (
        "stale_session"
    )

    orchestrator.voice_services = SimpleNamespace()
    orchestrator.history = SimpleNamespace(get_chat=lambda *_args, **_kwargs: None)
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await orchestrator._handle_voice_local_recognition_started(websocket, started)
    await orchestrator._handle_voice_local_final(websocket, final)
    assert json.loads(orchestrator._safe_send.await_args.args[1])["reason"] == (
        "stale_chat_context"
    )

    orchestrator.history = SimpleNamespace(
        get_chat=lambda *_args, **_kwargs: {"render_revision": 1}
    )
    orchestrator.voice_services = SimpleNamespace(
        admit_local_final=AsyncMock(
            side_effect=VoiceControlBindingError("altered_local_final")
        )
    )
    await orchestrator._handle_voice_local_final(websocket, final)
    assert json.loads(orchestrator._safe_send.await_args.args[1])["reason"] == (
        "altered_local_final"
    )

    invalid = Orchestrator.__new__(Orchestrator)
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        invalid._client_local_socket_authority(object())
    announcement = SimpleNamespace(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        to_json=lambda: "{}",
    )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await invalid.publish_voice_local_announcement(announcement)
    orchestrator._safe_send = AsyncMock(return_value=False)
    with pytest.raises(
        VoiceControlBindingError, match="local_announcement_delivery_failed"
    ):
        await orchestrator.publish_voice_local_announcement(announcement)


@pytest.mark.asyncio
async def test_local_control_frames_enter_only_their_bounded_handlers() -> None:
    final = _final()
    orchestrator, websocket = _local_orchestrator(final, SimpleNamespace())
    orchestrator._handle_voice_local_ready = AsyncMock()
    orchestrator._handle_voice_local_recognition_started = AsyncMock()
    orchestrator._handle_voice_local_final = AsyncMock()
    orchestrator._handle_voice_local_playout_event = AsyncMock()

    ready = VoiceLocalReady(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        client_sequence=1,
    )
    started = VoiceLocalRecognitionStarted(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id=final.client_turn_id,
        chat_id=final.chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
    )
    playout = VoiceLocalPlayoutEvent(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        announcement_id=_id(),
        announcement_sequence=1,
        turn_id=final.turn_id,
        kind="failure",
        phase="started",
        client_sequence=1,
        observed_at="2026-08-28T18:00:00Z",
    )
    for value in (ready, started, final, playout):
        await orchestrator.handle_ui_message(websocket, value.to_json())

    orchestrator._handle_voice_local_ready.assert_awaited_once()
    orchestrator._handle_voice_local_recognition_started.assert_awaited_once()
    orchestrator._handle_voice_local_final.assert_awaited_once()
    orchestrator._handle_voice_local_playout_event.assert_awaited_once()

    # Failure is content-free and releases only its already-bound turn.
    authority = _authority(final)
    release = Mock()
    reject = Mock()
    orchestrator.voice_services = SimpleNamespace(
        local_bindings=SimpleNamespace(
            verify_turn_frame=Mock(return_value=authority),
            release_turn=release,
        ),
        repository=SimpleNamespace(reject_transcript=reject),
    )
    failed = VoiceLocalRecognitionFailed(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=1,
        speech_revision=1,
        client_turn_id=final.client_turn_id,
        turn_id=final.turn_id,
        submission_id=final.submission_id,
        request_generation=final.request_generation,
        chat_id=final.chat_id,
        chat_context_revision=1,
        recognition_sequence=1,
        reason="local_recognition_failed",
    )
    await orchestrator.handle_ui_message(websocket, failed.to_json())
    reject.assert_called_once()
    release.assert_called_once()
