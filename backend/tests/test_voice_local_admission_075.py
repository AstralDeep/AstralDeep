"""Client-local transcript admission invariants for Feature 075."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import unicodedata
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.orchestrator import ConnectionContext
from orchestrator.orchestrator import CONNECTION_INGRESS_LIMIT
from orchestrator.voice_backend import VoiceSpeechBackend
from orchestrator.voice_bootstrap import VoiceServices
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
from orchestrator.voice_coordinator import ClientLocalAnnouncementRegistry
from orchestrator.work_admission import OperationOwner, OperationState, OwnerScope
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
        expected_control_owner_id="voice-coordinator-local-1",
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
        expected_control_owner_id="voice-coordinator-local-1",
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
        "control_owner_id": "voice-coordinator-local-1",
        "control_lease_expires_at": NOW + timedelta(minutes=1),
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

    session["control_owner_id"] = "another-replica"
    with pytest.raises(TranscriptSubmissionRejected, match="invalid_binding"):
        repository.admit_local_transcript(request, now=NOW)
    session["control_owner_id"] = "voice-coordinator-local-1"
    session["control_lease_expires_at"] = NOW
    with pytest.raises(TranscriptSubmissionRejected, match="invalid_binding"):
        repository.admit_local_transcript(request, now=NOW)
    session["control_lease_expires_at"] = NOW + timedelta(minutes=1)

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


@pytest.mark.parametrize("terminal_path", ["success", "failure", "cancellation", "replay"])
def test_terminal_local_operation_scrubs_text_for_every_terminal_path(
    terminal_path: str,
) -> None:
    operation_id = uuid.uuid4()
    frame = SimpleNamespace(
        operation_kind="voice_chat_message",
        raw='{"text":"private","text_digest_sha256":"secret"}',
        parsed={
            "type": "voice_local_final",
            "text": f"private-{terminal_path}",
            "text_digest_sha256": "a" * 64,
            "payload": {"text": "nested-private", "digest": "nested-secret"},
            "submission_id": str(uuid.uuid4()),
        },
    )
    work = SimpleNamespace(
        operation_id=operation_id,
        frame=frame,
        subscribers={1: object()},
    )
    context = SimpleNamespace(operations={operation_id: work})

    Orchestrator._scrub_terminal_voice_operation(context, work)

    assert operation_id not in context.operations
    assert work.subscribers == {}
    assert frame.raw == ""
    assert "text" not in frame.parsed
    assert "text_digest_sha256" not in frame.parsed
    assert "payload" not in frame.parsed

    remote_frame = SimpleNamespace(
        operation_kind="voice_chat_message",
        raw="remote-wire",
        parsed={"type": "ui_event", "payload": {"text": "remote"}},
    )
    remote_work = SimpleNamespace(
        operation_id=uuid.uuid4(),
        frame=remote_frame,
        subscribers={1: object()},
    )
    remote_context = SimpleNamespace(
        operations={remote_work.operation_id: remote_work}
    )
    Orchestrator._scrub_terminal_voice_operation(remote_context, remote_work)
    assert remote_context.operations[remote_work.operation_id] is remote_work
    assert remote_frame.raw == "remote-wire"


def _local_orchestrator(frame: VoiceLocalFinal, services: object) -> tuple[Orchestrator, object]:
    orchestrator = Orchestrator.__new__(Orchestrator)
    websocket = object()
    claims = SimpleNamespace(
        subject="owner-a",
        device_id=frame.device_id,
        connection_generation=frame.connection_generation,
        binding_id=_id(),
        expires_at=datetime.now(UTC) + timedelta(minutes=4),
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
    services.local_bindings.release_turn.assert_not_called()

    announcement = SimpleNamespace(
        device_id=final.device_id,
        connection_generation=final.connection_generation,
        session_id=final.session_id,
        generation=final.generation,
        speech_revision=final.speech_revision,
        announcement_id=_id(),
        to_json=lambda: '{"type":"voice_local_announcement"}',
    )
    live_session = SimpleNamespace(
        **session.__dict__,
        speech_backend="client_local",
        state="active",
        ended_at=None,
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        control_binding_id=orchestrator._voice_control_bindings[id(websocket)].binding_id,
        control_binding_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    services.repository = SimpleNamespace(get_session=Mock(return_value=live_session))
    services._require_current_local_control = Mock()
    services.local_announcements = SimpleNamespace(
        authorize_delivery=Mock(),
        discard=Mock(),
    )
    await orchestrator.publish_voice_local_announcement(announcement)
    orchestrator._safe_send.reset_mock()
    services._require_current_local_control.side_effect = VoiceControlBindingError(
        "invalid_binding"
    )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await orchestrator.publish_voice_local_announcement(announcement)
    orchestrator._safe_send.assert_not_awaited()
    services._require_current_local_control.side_effect = None

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
@pytest.mark.parametrize(
    "mutation",
    [
        "socket_displaced",
        "speech_muted",
        "backgrounded",
        "generation_fenced",
        "expired",
        "consent_advanced",
        "mute_advanced",
    ],
)
async def test_delayed_local_announcement_reauthorizes_after_repository_await(
    mutation: str,
) -> None:
    now = datetime.now(UTC)
    session = SimpleNamespace(
        session_id=_id(),
        user_id="owner-a",
        device_id=_id(),
        owner_connection_generation=_id(),
        generation=1,
        media_grant_revision=1,
        foreground_active=True,
        microphone_enabled=True,
        speech_muted=False,
        state="active",
        speech_backend="client_local",
        ended_at=None,
        control_owner_id="voice-coordinator-local-1",
        control_lease_expires_at=now + timedelta(minutes=1),
        lease_expires_at=now + timedelta(minutes=2),
        control_binding_id=_id(),
        control_binding_expires_at=now + timedelta(minutes=2),
    )
    announcements = ClientLocalAnnouncementRegistry()
    frame = announcements.issue(
        session=session,
        kind="failure",
        turn_id=_id(),
        requested_text="ignored",
        output_policy="lifecycle",
        mute_revision=1,
        consent_revision=1,
        now=now,
    )
    entered = threading.Event()
    release = threading.Event()

    class Repository:
        def get_session(self, **_kwargs: object) -> object:
            entered.set()
            assert release.wait(timeout=2)
            return session

    services = VoiceServices(
        livekit=None,
        worker_pool=None,
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=None,
        capability=None,
        media=None,
        runtime=SimpleNamespace(_replica_id="voice-coordinator-local-1"),
        worker_control_settings=None,
        speech_backend=VoiceSpeechBackend.CLIENT_LOCAL,
        local_announcements=announcements,
    )
    websocket = object()
    claims = SimpleNamespace(
        subject="owner-a",
        device_id=session.device_id,
        connection_generation=session.owner_connection_generation,
        binding_id=session.control_binding_id,
        expires_at=now + timedelta(minutes=2),
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.voice_services = services
    orchestrator.ui_sessions = {websocket: {"sub": "owner-a"}}
    orchestrator._voice_control_bindings = {id(websocket): claims}
    orchestrator._voice_device_bindings = {
        ("owner-a", session.device_id): id(websocket)
    }
    orchestrator._safe_send = AsyncMock(return_value=True)

    task = asyncio.create_task(orchestrator.publish_voice_local_announcement(frame))
    assert await asyncio.to_thread(entered.wait, 1)
    if mutation == "socket_displaced":
        orchestrator._voice_device_bindings[("owner-a", session.device_id)] = 999
    elif mutation == "speech_muted":
        session.speech_muted = True
    elif mutation == "backgrounded":
        session.foreground_active = False
    elif mutation == "generation_fenced":
        session.generation = 2
    elif mutation == "expired":
        announcements._announcements[frame.announcement_id]["expires_at"] = (
            now - timedelta(seconds=1)
        )
    else:
        announcements.fence_session(
            session_id=frame.session_id,
            generation=frame.generation,
            bump_mute=mutation == "mute_advanced",
            bump_consent=mutation == "consent_advanced",
            now=now,
        )
    release.set()

    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await task
    orchestrator._safe_send.assert_not_awaited()
    assert announcements.retained_counts()["announcements"] == 0


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

    orchestrator.voice_services = SimpleNamespace(reject_local_turn=AsyncMock())
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
        ),
        reject_local_turn=AsyncMock(),
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
        session_id=final.session_id,
        generation=final.generation,
        speech_revision=final.speech_revision,
        announcement_id=_id(),
        to_json=lambda: "{}",
    )
    with pytest.raises(VoiceControlBindingError, match="invalid_binding"):
        await invalid.publish_voice_local_announcement(announcement)
    orchestrator._safe_send = AsyncMock(return_value=False)
    binding = orchestrator._voice_control_bindings[id(websocket)]
    session = SimpleNamespace(
        device_id=final.device_id,
        owner_connection_generation=final.connection_generation,
        generation=final.generation,
        media_grant_revision=final.speech_revision,
        control_binding_id=binding.binding_id,
    )
    orchestrator.voice_services.repository = SimpleNamespace(
        get_session=Mock(return_value=session)
    )
    orchestrator.voice_services._require_current_local_control = Mock()
    orchestrator.voice_services.local_announcements = SimpleNamespace(
        authorize_delivery=Mock(),
        discard=Mock(),
    )
    with pytest.raises(
        VoiceControlBindingError, match="local_announcement_delivery_failed"
    ):
        await orchestrator.publish_voice_local_announcement(announcement)


@pytest.mark.asyncio
async def test_local_final_enters_through_real_connection_ingress_once() -> None:
    final = _final()
    services = SimpleNamespace(
        admit_local_final=AsyncMock(
            return_value=SimpleNamespace(canonical_text="hello")
        ),
        verify_local_final_authority=AsyncMock(return_value="hello"),
        reject_local_turn=AsyncMock(),
        local_bindings=SimpleNamespace(release_turn=Mock()),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    orchestrator._send_frame_refusal = AsyncMock(return_value=True)
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )

    async def admission_pump(value: ConnectionContext) -> None:
        ingress = value.ingress.popleft()
        await orchestrator.handle_ui_message(websocket, ingress.raw)

    orchestrator._connection_admission_pump = admission_pump
    assert await orchestrator._route_ui_frame(context, final.to_json())
    assert context.admission_task is not None
    await context.admission_task

    orchestrator.handle_chat_message.assert_awaited_once()
    services.admit_local_final.assert_awaited_once()
    orchestrator._send_frame_refusal.assert_not_awaited()


def test_local_final_connection_identity_rejects_missing_or_cross_chat() -> None:
    final = _final()
    orchestrator, websocket = _local_orchestrator(final, SimpleNamespace())
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )
    valid = json.loads(final.to_json())
    assert orchestrator._connection_frame(context, final.to_json(), valid) is not None

    missing = dict(valid)
    missing.pop("chat_id")
    assert orchestrator._connection_frame(
        context, json.dumps(missing), missing
    ) is None
    conflicting = dict(valid)
    conflicting["payload"] = {"chat_id": _id()}
    assert orchestrator._connection_frame(
        context, json.dumps(conflicting), conflicting
    ) is None


@pytest.mark.asyncio
async def test_local_final_verification_precedes_durable_operation_replay_lookup() -> None:
    final = _final()
    verify = AsyncMock(return_value="Café\nstatus")
    services = SimpleNamespace(
        verify_local_final_authority=verify,
        reject_local_turn=AsyncMock(),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    orchestrator._send_voice_local_rejection = AsyncMock()
    orchestrator._call_work_admission = AsyncMock(return_value=[])
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )
    parsed = json.loads(final.to_json())
    ingress = orchestrator._connection_frame(context, final.to_json(), parsed)
    assert ingress is not None
    context.ingress.append(ingress)

    await orchestrator._connection_admission_pump(context)

    verify.assert_awaited_once()
    orchestrator._call_work_admission.assert_awaited_once()

    verify.reset_mock(side_effect=True)
    verify.side_effect = VoiceControlBindingError("altered_local_final")
    orchestrator._call_work_admission.reset_mock()
    ingress.local_final_verified = False
    context.ingress.append(ingress)
    await orchestrator._connection_admission_pump(context)
    orchestrator._send_voice_local_rejection.assert_awaited_once()
    services.reject_local_turn.assert_awaited_once()
    orchestrator._call_work_admission.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_socket_local_final_retry_is_verified_before_any_suppression() -> None:
    final = _final()
    verify = AsyncMock(return_value="Café\nstatus")
    services = SimpleNamespace(
        verify_local_final_authority=verify,
        reject_local_turn=AsyncMock(),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    routed: list[str] = []

    async def pump(context: ConnectionContext) -> None:
        routed.extend(frame.raw for frame in context.ingress)
        context.ingress.clear()

    orchestrator._connection_admission_pump = pump
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )

    for _ in range(2):
        assert await orchestrator._route_ui_frame(context, final.to_json())
        assert context.admission_task is not None
        await context.admission_task

    assert verify.await_count == 2
    assert routed == [final.to_json(), final.to_json()]


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", ["altered_text", "invalid_digest"])
async def test_same_socket_altered_local_final_reuse_is_rejected_and_cleaned(
    reuse: str,
) -> None:
    final = _final()
    verify = AsyncMock(
        side_effect=[
            "Café\nstatus",
            VoiceControlBindingError("altered_local_final"),
        ]
    )
    services = SimpleNamespace(
        verify_local_final_authority=verify,
        reject_local_turn=AsyncMock(),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    orchestrator._send_voice_local_rejection = AsyncMock()

    async def pump(context: ConnectionContext) -> None:
        context.ingress.clear()

    orchestrator._connection_admission_pump = pump
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )
    assert await orchestrator._route_ui_frame(context, final.to_json())
    assert context.admission_task is not None
    await context.admission_task

    reused = json.loads(final.to_json())
    if reuse == "altered_text":
        reused["text"] = "altered private text"
        reused["text_digest_sha256"] = hashlib.sha256(
            reused["text"].encode()
        ).hexdigest()
    else:
        reused["text_digest_sha256"] = "b" * 64
    assert await orchestrator._route_ui_frame(context, json.dumps(reused))

    assert verify.await_count == 2
    services.reject_local_turn.assert_awaited_once()
    orchestrator._send_voice_local_rejection.assert_awaited_once()
    assert not context.ingress


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "projection_fails"),
    [
        (OperationState.COMPLETED, False),
        (OperationState.FAILED, False),
        (OperationState.CANCELLED, False),
        (OperationState.RETRYABLE, False),
        (OperationState.COMPLETED, True),
    ],
)
async def test_terminal_local_replay_scrubs_through_actual_admission_pump(
    state: OperationState,
    projection_fails: bool,
) -> None:
    final = _final(text="private replay text")
    services = SimpleNamespace(
        verify_local_final_authority=AsyncMock(return_value="private replay text"),
        reject_local_turn=AsyncMock(),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )
    operation_id = uuid.uuid4()
    owner = OperationOwner(
        owner_scope=OwnerScope.USER,
        owner_user_id="owner-a",
        connection_scope_id=context.connection_scope_id,
    )
    result = SimpleNamespace(accepted=True, operation_id=operation_id)
    projection = SimpleNamespace(
        operation_id=operation_id,
        state=state,
    )
    captured: list[object] = []

    async def admit(_method: object, _context: object, batch: list[object]) -> object:
        captured.extend(batch)
        return [(batch[0], owner, result, projection)]

    orchestrator._call_work_admission = admit
    orchestrator._send_operation_projection = AsyncMock(
        side_effect=(RuntimeError("send unavailable") if projection_fails else None)
    )
    orchestrator._replay_voice_ack_if_accepted = AsyncMock()

    assert await orchestrator._route_ui_frame(context, final.to_json())
    assert context.admission_task is not None
    if projection_fails:
        with pytest.raises(RuntimeError, match="send unavailable"):
            await context.admission_task
    else:
        await context.admission_task

    ingress = captured[0]
    assert operation_id not in context.operations
    assert operation_id not in orchestrator._reconnectable_operations
    assert ingress.raw == ""  # type: ignore[attr-defined]
    assert "text" not in ingress.parsed  # type: ignore[attr-defined]
    assert "text_digest_sha256" not in ingress.parsed  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ingress_capacity_refusal_verifies_and_cleans_local_final() -> None:
    final = _final()
    services = SimpleNamespace(
        verify_local_final_authority=AsyncMock(return_value="Café\nstatus"),
        reject_local_turn=AsyncMock(),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    orchestrator._send_voice_local_rejection = AsyncMock()
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )
    context.ingress.extend([object()] * CONNECTION_INGRESS_LIMIT)

    assert await orchestrator._route_ui_frame(context, final.to_json())

    services.verify_local_final_authority.assert_awaited_once()
    services.reject_local_turn.assert_awaited_once()
    orchestrator._send_voice_local_rejection.assert_awaited_once()
    assert len(context.ingress) == CONNECTION_INGRESS_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["exception", "conflict", "projection_failure"])
async def test_admission_refusal_branches_clean_exact_local_final(
    outcome: str,
) -> None:
    final = _final()
    services = SimpleNamespace(
        verify_local_final_authority=AsyncMock(return_value="Café\nstatus"),
        reject_local_turn=AsyncMock(),
    )
    orchestrator, websocket = _local_orchestrator(final, services)
    orchestrator._ws_active_chat = {id(websocket): final.chat_id}
    orchestrator._send_voice_local_rejection = AsyncMock()
    context = ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(final.connection_generation),
        registered=True,
    )
    operation_id = uuid.uuid4()
    owner = OperationOwner(
        owner_scope=OwnerScope.USER,
        owner_user_id="owner-a",
        connection_scope_id=context.connection_scope_id,
    )

    async def admit(_method: object, _context: object, batch: list[object]) -> object:
        frame = batch[0]
        if outcome == "exception":
            result: object = RuntimeError("repository unavailable")
        elif outcome == "conflict":
            result = SimpleNamespace(
                accepted=False,
                code="idempotency_conflict",
                retryable=False,
                retry_after_ms=None,
            )
        else:
            result = SimpleNamespace(accepted=True, operation_id=operation_id)
        return [(frame, owner, result, None)]

    orchestrator._call_work_admission = admit
    orchestrator._send_operation_accepted = AsyncMock(
        side_effect=(
            RuntimeError("projection unavailable")
            if outcome == "projection_failure"
            else None
        )
    )
    orchestrator.work_admission = SimpleNamespace(cancel=Mock())

    assert await orchestrator._route_ui_frame(context, final.to_json())
    assert context.admission_task is not None
    await context.admission_task

    services.reject_local_turn.assert_awaited_once()
    orchestrator._send_voice_local_rejection.assert_awaited_once()

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
