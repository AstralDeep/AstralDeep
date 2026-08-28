"""Deterministic five-user isolation proofs for conversational voice 065."""

from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator, Mapping
from unittest.mock import Mock

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.voice_control_binding import (
    VoiceControlBindingError,
    VoiceControlBindingIssuer,
)
from orchestrator.voice_coordinator import (
    FIXED_VOICE_PROFILE,
    CapacityUnavailable,
    ControlProtocolError,
    SessionBindRequest,
    StaleFence,
    VoiceCoordinator,
    WorkerPool,
    WorkerPoolPolicy,
    deterministic_uuid4,
)
from orchestrator.voice_runtime import ActivatedVoiceMedia, VoiceSessionRuntime
from orchestrator.voice_sessions import (
    SessionControl,
    TranscriptSubmission,
    TranscriptSubmissionRejected,
    VoiceSessionNotFound,
    VoiceSessionRepositoryError,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    OperationNotFoundError,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    RefusedAdmission,
    WorkAdmissionCoordinator,
)
from tests.helpers.voice_plane_runtime import (
    VoicePlaneTestRuntime,
    isolated_voice_plane_runtime,
    plane_work_admission_repository,
    voice_session_repository,
)
from shared.protocol import RegisterUI
from shared.voice_transcript import TranscriptProofBinding, issue_transcript_proof


NOW = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)
USER_COUNT = 5
USERS = tuple(f"voice-isolation-user-{index}" for index in range(USER_COUNT))
CHATS = tuple(
    deterministic_uuid4("voice-isolation-chat", str(index))
    for index in range(USER_COUNT)
)
DEVICES = tuple(
    deterministic_uuid4("voice-isolation-device", str(index))
    for index in range(USER_COUNT)
)
CONNECTIONS = tuple(
    deterministic_uuid4("voice-isolation-connection", str(index))
    for index in range(USER_COUNT)
)
ACTIVATIONS = tuple(
    deterministic_uuid4("voice-isolation-activation", str(index))
    for index in range(USER_COUNT)
)
TAKEOVER_ACTIVATIONS = tuple(
    deterministic_uuid4("voice-isolation-takeover", str(index))
    for index in range(USER_COUNT)
)
TRANSCRIPTS = tuple(
    f"synthetic isolation phrase belonging only to user {index}"
    for index in range(USER_COUNT)
)
WORKER_CONTROL_SECRET = b"w" * 32
WORKER_TOKEN_SENTINEL = "worker-room-secret-sentinel-" + "w" * 40
CLIENT_TOKEN_SENTINEL = "client-room-secret-sentinel-" + "c" * 40
CLOSURE_SHA256 = "a" * 64


@pytest.fixture(scope="module")
def database() -> Iterator[VoicePlaneTestRuntime]:
    with isolated_voice_plane_runtime("voice_isolation_065") as runtime:
        yield runtime


class _ReadyValue:
    status = "ready"
    reason = "ready"

    @staticmethod
    def to_dict() -> dict[str, str]:
        return {"schema_version": "1", "status": "ready", "reason": "ready"}


class _ReadyCapability:
    async def readiness(self) -> _ReadyValue:
        return _ReadyValue()


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


def _timestamp(value: datetime = NOW) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _worker_policy() -> WorkerPoolPolicy:
    return WorkerPoolPolicy(
        runtime_closure_sha256=CLOSURE_SHA256,
        max_workers=USER_COUNT,
        max_sessions_per_worker=1,
        max_total_sessions=USER_COUNT,
        heartbeat_interval_seconds=5,
        connection_lease_seconds=30,
        send_timeout_seconds=0.5,
        allow_insecure_livekit_url=True,
    )


def _worker_registration(identity: str) -> dict[str, Any]:
    return {
        "type": "worker_register",
        "schema_version": "1",
        "message_id": deterministic_uuid4("voice-isolation-register", identity),
        "sequence": 0,
        "sent_at": _timestamp(),
        "worker_identity": identity,
        "max_sessions": 1,
        "runtime_closure_sha256": CLOSURE_SHA256,
        "profile": dict(FIXED_VOICE_PROFILE),
    }


def _worker_grant(
    worker_identity: str,
    request: SessionBindRequest,
) -> dict[str, Any]:
    return {
        "revision": request.worker_rtc_grant_revision,
        "livekit_url": "ws://livekit:7880",
        "join_token": WORKER_TOKEN_SENTINEL,
        "issued_at": _timestamp(),
        "expires_at": _timestamp(NOW + timedelta(minutes=5)),
        "room_name": request.room_name,
        "worker_identity": worker_identity,
    }


class _PoolMedia:
    """Small media adapter that still drives the real worker control authority."""

    def __init__(self, pool: WorkerPool) -> None:
        self.pool = pool
        self.bindings: dict[str, tuple[Any, SessionBindRequest]] = {}
        self.events: list[dict[str, Any]] = []

    async def activate(self, session: Any) -> ActivatedVoiceMedia:
        request = SessionBindRequest(
            session_id=session.session_id,
            generation=session.generation,
            room_name=session.room_name,
            transport=session.transport,
            media_grant_revision=session.media_grant_revision,
            worker_rtc_grant_revision=session.worker_rtc_grant_revision,
            client_participant_identity=session.participant_identity,
            visible_chat_id=session.visible_chat_id,
            chat_context_revision=session.chat_context_revision,
        )
        reservation = await self.pool.reserve_session(request)
        await self.pool.deliver_session_bind(
            reservation,
            request,
            _worker_grant(reservation.worker_identity, request),
        )
        await self.pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(
                {
                    "type": "worker_ready",
                    "schema_version": "1",
                    "message_id": deterministic_uuid4(
                        "voice-isolation-ready", reservation.assignment_id
                    ),
                    "session_id": session.session_id,
                    "generation": session.generation,
                    "sequence": 0,
                    "sent_at": _timestamp(),
                    "assignment_id": reservation.assignment_id,
                    "worker_identity": reservation.worker_identity,
                    "worker_rtc_grant_revision": (session.worker_rtc_grant_revision),
                    "profile_ready": True,
                }
            ),
        )
        await self.pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(
                {
                    "type": "session_context_applied",
                    "schema_version": "1",
                    "message_id": deterministic_uuid4(
                        "voice-isolation-context", reservation.assignment_id
                    ),
                    "session_id": session.session_id,
                    "generation": session.generation,
                    "sequence": 1,
                    "sent_at": _timestamp(),
                    "media_grant_revision": session.media_grant_revision,
                    "visible_chat_id": session.visible_chat_id,
                    "chat_context_revision": session.chat_context_revision,
                    "occurred_at": _timestamp(),
                }
            ),
        )
        self.bindings[session.session_id] = (reservation, request)
        self.events.append(
            {
                "event": "activated",
                "session_id": session.session_id,
                "generation": session.generation,
            }
        )
        return ActivatedVoiceMedia(
            assignment_id=reservation.assignment_id,
            worker_identity=reservation.worker_identity,
            worker_grant_issued_at=NOW,
            worker_grant_expires_at=NOW + timedelta(minutes=5),
            client_grant={
                "grant_id": deterministic_uuid4(
                    "voice-isolation-client-grant", session.session_id
                ),
                "transport": "livekit",
                "join_token": CLIENT_TOKEN_SENTINEL,
            },
        )

    async def apply_context(self, session: Any) -> None:
        reservation, _request = self.bindings[session.session_id]
        await self.pool.send_session_command(
            reservation,
            "session_context_update",
            {
                "media_grant_revision": session.media_grant_revision,
                "visible_chat_id": session.visible_chat_id,
                "chat_context_revision": session.chat_context_revision,
            },
        )
        snapshot = self.pool.assignment_snapshot(session.session_id)
        await self.pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(
                {
                    "type": "session_context_applied",
                    "schema_version": "1",
                    "message_id": deterministic_uuid4(
                        "voice-isolation-context-update",
                        reservation.assignment_id,
                        str(session.chat_context_revision),
                    ),
                    "session_id": session.session_id,
                    "generation": session.generation,
                    "sequence": snapshot.next_incoming_sequence,
                    "sent_at": _timestamp(),
                    "media_grant_revision": session.media_grant_revision,
                    "visible_chat_id": session.visible_chat_id,
                    "chat_context_revision": session.chat_context_revision,
                    "occurred_at": _timestamp(),
                }
            ),
        )

    async def set_capture(self, session: Any, enabled: bool) -> None:
        self.events.append(
            {
                "event": "capture",
                "session_id": session.session_id,
                "enabled": enabled,
            }
        )

    async def stop_speech(self, session: Any) -> None:
        self.events.append({"event": "stop_speech", "session_id": session.session_id})

    async def end(self, session: Any, reason: str) -> None:
        binding = self.bindings.pop(session.session_id, None)
        if binding is not None:
            reservation, _request = binding
            await self.pool.send_session_command(
                reservation,
                "end_session",
                {
                    "media_grant_revision": session.media_grant_revision,
                    "reason": reason,
                },
            )
            assert await self.pool.release_session(
                session.session_id,
                session.generation,
                reservation.assignment_id,
            )
        self.events.append(
            {
                "event": "ended",
                "session_id": session.session_id,
                "generation": session.generation,
                "reason": reason,
            }
        )

    async def abort(self, session: Any) -> None:
        await self.end(session, "media_error")

    async def assignment_is_current(
        self,
        session: Any,
        *,
        assignment_id: str,
        worker_identity: str,
    ) -> bool:
        binding = self.bindings.get(session.session_id)
        if binding is None:
            return False
        reservation, _request = binding
        if (
            reservation.assignment_id != assignment_id
            or reservation.worker_identity != worker_identity
        ):
            return False
        try:
            current = await self.pool.current_reservation(
                session_id=session.session_id,
                generation=session.generation,
            )
        except Exception:
            return False
        return current == reservation

    async def rotate_media_grant(
        self,
        previous: Any,
        session: Any,
        *,
        refresh_id: str,
    ) -> Mapping[str, Any]:
        del previous, session, refresh_id
        raise AssertionError("media grant rotation is not part of T112")


def _control_orchestrator() -> tuple[Orchestrator, dict[int, list[dict[str, Any]]]]:
    orchestrator = object.__new__(Orchestrator)
    orchestrator._voice_binding_issuer = VoiceControlBindingIssuer(
        b"b" * 32,
        clock=lambda: NOW,
    )
    orchestrator._voice_control_bindings = {}
    orchestrator._voice_device_bindings = {}
    orchestrator._voice_composer_tasks = {}
    orchestrator._voice_composer_revisions = {}
    orchestrator._voice_device_kinds = {}
    sent: dict[int, list[dict[str, Any]]] = {}

    async def safe_send(websocket: object, payload: str) -> bool:
        sent.setdefault(id(websocket), []).append(json.loads(payload))
        return True

    orchestrator._safe_send = safe_send
    return orchestrator, sent


async def _issue_controls() -> tuple[
    Orchestrator,
    tuple[object, ...],
    tuple[dict[str, Any], ...],
    tuple[str, ...],
    dict[int, list[dict[str, Any]]],
]:
    orchestrator, sent = _control_orchestrator()
    sockets: list[object] = []
    controls: list[dict[str, Any]] = []
    bearers: list[str] = []
    for index, user_id in enumerate(USERS):
        websocket = object()
        sockets.append(websocket)
        registration = RegisterUI(
            device_id=DEVICES[index],
            connection_generation=CONNECTIONS[index],
        )
        assert await orchestrator._issue_voice_control_binding(
            websocket,
            registration,
            {
                "sub": user_id,
                "exp": int((NOW + timedelta(minutes=10)).timestamp()),
            },
        )
        frame = sent[id(websocket)][-1]
        claims = orchestrator.validate_voice_control_binding(
            bearer=frame["binding"],
            subject=user_id,
            device_id=DEVICES[index],
            connection_generation=CONNECTIONS[index],
        )
        bearers.append(frame["binding"])
        controls.append(
            {
                "subject": user_id,
                "device_id": claims.device_id,
                "connection_generation": claims.connection_generation,
                "binding_id": claims.binding_id,
                "binding_expires_at": claims.expires_at,
            }
        )
    return (
        orchestrator,
        tuple(sockets),
        tuple(controls),
        tuple(bearers),
        sent,
    )


def _activation_request(index: int, *, takeover: bool = False) -> dict[str, Any]:
    request: dict[str, Any] = {
        "device_id": DEVICES[index],
        "device_kind": "web",
        "visible_chat_id": CHATS[index],
        "activation_id": (
            TAKEOVER_ACTIVATIONS[index] if takeover else ACTIVATIONS[index]
        ),
        "capability": {
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
            "full_duplex": True,
            "transport": "livekit",
        },
        "foreground_active": True,
    }
    if takeover:
        request.update(
            {
                "expected_generation": 1,
                "expected_media_grant_revision": 1,
            }
        )
    return request


def _recognition_frame(session: Any, reservation: Any, sequence: int) -> dict[str, Any]:
    return {
        "type": "recognition_started",
        "schema_version": "1",
        "message_id": deterministic_uuid4(
            "voice-isolation-recognition-message", session.session_id
        ),
        "session_id": session.session_id,
        "generation": session.generation,
        "sequence": sequence,
        "sent_at": _timestamp(NOW + timedelta(seconds=1)),
        "client_turn_id": deterministic_uuid4(
            "voice-isolation-client-turn", session.session_id
        ),
        "media_grant_revision": session.media_grant_revision,
        "visible_chat_id": session.visible_chat_id,
        "chat_context_revision": session.chat_context_revision,
        "occurred_at": _timestamp(NOW + timedelta(seconds=1)),
    }


def _transcript_submission(
    session: Any,
    binding: Any,
    text: str,
) -> TranscriptSubmission:
    proof_binding = TranscriptProofBinding(
        session_id=binding.session_id,
        generation=binding.generation,
        media_grant_revision=binding.media_grant_revision,
        assignment_id=binding.assignment_id,
        worker_identity=binding.worker_identity,
        turn_id=binding.turn_id,
        client_turn_id=binding.client_turn_id,
        submission_id=binding.submission_id,
        request_generation=binding.request_generation,
        chat_id=binding.chat_id,
        chat_context_revision=binding.chat_context_revision,
        detected_language="en",
    )
    proof = issue_transcript_proof(
        WORKER_CONTROL_SECRET,
        proof_binding,
        text,
        now=NOW + timedelta(seconds=2),
    )
    return TranscriptSubmission(
        user_id=session.user_id,
        session_id=binding.session_id,
        generation=binding.generation,
        media_grant_revision=binding.media_grant_revision,
        turn_id=binding.turn_id,
        client_turn_id=binding.client_turn_id,
        submission_id=binding.submission_id,
        request_generation=binding.request_generation,
        chat_id=binding.chat_id,
        chat_context_revision=binding.chat_context_revision,
        source_participant_identity=binding.worker_identity,
        detected_language="en",
        text=proof.canonical_text,
        text_digest_sha256=proof.text_digest_sha256,
        transcript_proof=proof.transcript_proof,
        proof_expires_at=proof.proof_expires_at,
    )


@pytest.mark.asyncio
async def test_five_control_bindings_reject_cross_user_device_and_connection() -> None:
    orchestrator, sockets, _controls, bearers, sent = await _issue_controls()
    errors: list[Exception] = []

    for index, bearer in enumerate(bearers):
        other = (index + 1) % USER_COUNT
        for subject, device_id, connection_generation in (
            (USERS[other], DEVICES[index], CONNECTIONS[index]),
            (USERS[index], DEVICES[other], CONNECTIONS[index]),
            (USERS[index], DEVICES[index], CONNECTIONS[other]),
        ):
            with pytest.raises(
                VoiceControlBindingError,
                match="binding_scope_mismatch",
            ) as caught:
                orchestrator.validate_voice_control_binding(
                    bearer=bearer,
                    subject=subject,
                    device_id=device_id,
                    connection_generation=connection_generation,
                )
            errors.append(caught.value)

    replacement_socket = object()
    replacement_connection = deterministic_uuid4(
        "voice-isolation-replacement-connection"
    )
    orchestrator.voice_services = SimpleNamespace(
        clear_local_connection=Mock()
    )
    assert await orchestrator._issue_voice_control_binding(
        replacement_socket,
        RegisterUI(
            device_id=DEVICES[0],
            connection_generation=replacement_connection,
        ),
        {
            "sub": USERS[0],
            "exp": int((NOW + timedelta(minutes=10)).timestamp()),
        },
    )
    with pytest.raises(
        VoiceControlBindingError,
        match="binding_not_current",
    ) as caught:
        orchestrator.validate_voice_control_binding(
            bearer=bearers[0],
            subject=USERS[0],
            device_id=DEVICES[0],
            connection_generation=CONNECTIONS[0],
        )
    errors.append(caught.value)
    for index in range(1, USER_COUNT):
        assert (
            orchestrator.validate_voice_control_binding(
                bearer=bearers[index],
                subject=USERS[index],
                device_id=DEVICES[index],
                connection_generation=CONNECTIONS[index],
            ).subject
            == USERS[index]
        )

    assert id(sockets[0]) not in orchestrator._voice_control_bindings
    assert len(orchestrator._voice_control_bindings) == USER_COUNT
    assert all(len(sent[id(websocket)]) == 1 for websocket in sockets)
    assert all(
        frame["type"] == "voice_control_binding"
        for frames in sent.values()
        for frame in frames
    )
    error_dump = repr(errors)
    for bearer in bearers:
        assert bearer not in error_dump
    for content in TRANSCRIPTS:
        assert content not in error_dump
    assert all(user not in error_dump for user in USERS)


@pytest.mark.asyncio
async def test_five_runtime_users_keep_rooms_turns_and_takeovers_isolated(
    database: VoicePlaneTestRuntime,
) -> None:
    for user_id, chat_id in zip(USERS, CHATS, strict=True):
        await asyncio.to_thread(
            database.execute,
            "INSERT INTO chats (id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, 'Five-user isolation', 1, 1)",
            (chat_id, user_id),
        )
    repository = voice_session_repository(database)
    pool = WorkerPool(
        _worker_policy(),
        utcnow=lambda: NOW,
        monotonic=lambda: 100.0,
    )
    worker_sockets: dict[str, _Socket] = {}
    for index in range(USER_COUNT):
        identity = f"voice-isolation-worker-{index}"
        socket = _Socket()
        worker_sockets[identity] = socket
        await pool.register_worker(
            _worker_registration(identity),
            socket,
            authenticated_identity=identity,
        )
    media = _PoolMedia(pool)
    metrics = RuntimeObservability(
        deployment_instance="isolation",
        clock=lambda: NOW,
    )
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=_ReadyCapability(),
        media=media,
        replica_id="voice-isolation-replica",
        clock=lambda: NOW,
        observability=metrics,
    )
    control_orchestrator, _sockets, controls, bearers, _sent = await _issue_controls()

    created = []
    sessions = []
    for index, user_id in enumerate(USERS):
        result = await runtime.create_session(
            user_id=user_id,
            control=controls[index],
            request=_activation_request(index),
        )
        created.append(result)
        session = await asyncio.to_thread(
            repository.get_live_session,
            user_id=user_id,
        )
        assert session is not None
        sessions.append(session)

    assert len({session.session_id for session in sessions}) == USER_COUNT
    assert len({session.room_name for session in sessions}) == USER_COUNT
    assert len({session.worker_identity for session in sessions}) == USER_COUNT
    assert pool.readiness().capacity_available == 0
    for index, session in enumerate(sessions):
        assert session.user_id == USERS[index]
        assert session.visible_chat_id == CHATS[index]
        assert session.device_id == DEVICES[index]
        assert session.state == "active"
        assert created[index].payload["session"]["session_id"] == session.session_id
        projection = created[index].payload["session"]
        assert "room_name" not in projection
        assert "worker_identity" not in projection
        assert USERS[index] not in repr(projection)
        assert TRANSCRIPTS[index] not in repr(projection)

    initial_binding_by_worker = {
        reservation.worker_identity: (session, reservation, request)
        for session in sessions
        for reservation, request in (media.bindings[session.session_id],)
    }
    assert set(initial_binding_by_worker) == set(worker_sockets)
    for worker_identity, socket in worker_sockets.items():
        session, reservation, request = initial_binding_by_worker[worker_identity]
        frames = [json.loads(item) for item in socket.sent]
        binds = [frame for frame in frames if frame["type"] == "session_bind"]
        assert len(binds) == 1
        assert binds[0]["session_id"] == session.session_id
        assert binds[0]["room_name"] == session.room_name
        assert binds[0]["visible_chat_id"] == session.visible_chat_id
        assert binds[0]["worker_identity"] == reservation.worker_identity
        assert binds[0]["client_participant_identity"] == (
            request.client_participant_identity
        )

    first_reservation, first_request = media.bindings[sessions[0].session_id]
    first_socket = worker_sockets[first_reservation.worker_identity]
    sent_before_wrong_room = len(first_socket.sent)
    with pytest.raises(
        ControlProtocolError,
        match="grant_room_mismatch",
    ) as wrong_room:
        await pool.deliver_session_bind(
            first_reservation,
            first_request,
            {
                **_worker_grant(first_reservation.worker_identity, first_request),
                "room_name": sessions[1].room_name,
            },
        )
    assert len(first_socket.sent) == sent_before_wrong_room
    assert WORKER_TOKEN_SENTINEL not in repr(wrong_room.value)
    assert sessions[0].room_name not in repr(wrong_room.value)
    assert sessions[1].room_name not in repr(wrong_room.value)

    overflow_request = SessionBindRequest(
        session_id=deterministic_uuid4("voice-isolation-overflow-session"),
        generation=1,
        room_name="voice-isolation-overflow-room",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="voice-isolation-overflow-client",
        visible_chat_id=deterministic_uuid4("voice-isolation-overflow-chat"),
        chat_context_revision=1,
    )
    with pytest.raises(
        CapacityUnavailable,
        match="deployment_capacity_exhausted",
    ) as worker_capacity:
        await pool.reserve_session(overflow_request)
    assert overflow_request.room_name not in repr(worker_capacity.value)

    bad_control = SessionControl(
        device_id=DEVICES[1],
        connection_generation=CONNECTIONS[0],
        binding_id=controls[0]["binding_id"],
        binding_expires_at=controls[0]["binding_expires_at"],
    )
    with pytest.raises(
        VoiceSessionRepositoryError,
        match="binding_scope_mismatch",
    ) as wrong_device:
        await asyncio.to_thread(
            repository.get_controlled_session,
            user_id=USERS[0],
            session_id=sessions[0].session_id,
            expected_generation=sessions[0].generation,
            expected_media_grant_revision=sessions[0].media_grant_revision,
            control=bad_control,
            now=NOW + timedelta(seconds=1),
        )
    assert USERS[0] not in repr(wrong_device.value)
    assert DEVICES[0] not in repr(wrong_device.value)

    coordinator = VoiceCoordinator(
        pool,
        repository,
        replica_id="voice-isolation-replica",
        utcnow=lambda: NOW + timedelta(seconds=1),
    )
    bindings = []
    for index, session in enumerate(sessions):
        reservation, _request = media.bindings[session.session_id]
        sequence = pool.assignment_snapshot(session.session_id).next_incoming_sequence
        recognition = _recognition_frame(session, reservation, sequence)
        if index == 0:
            other_reservation, _other_request = media.bindings[sessions[1].session_id]
            with pytest.raises(
                StaleFence,
                match="stale_assignment",
            ) as wrong_connection:
                await pool.receive_worker_frame(
                    other_reservation.connection_id,
                    json.dumps(recognition),
                )
            assert session.room_name not in repr(wrong_connection.value)
            assert TRANSCRIPTS[index] not in repr(wrong_connection.value)
        accepted_frame = await pool.receive_worker_frame(
            reservation.connection_id,
            json.dumps(recognition),
        )
        binding = await coordinator.bind_recognition_started(accepted_frame)
        bindings.append(binding)
        with pytest.raises(VoiceSessionNotFound):
            await asyncio.to_thread(
                repository.get_turn,
                user_id=USERS[(index + 1) % USER_COUNT],
                turn_id=binding.turn_id,
            )

    submissions = tuple(
        _transcript_submission(session, binding, TRANSCRIPTS[index])
        for index, (session, binding) in enumerate(zip(sessions, bindings, strict=True))
    )
    admissions = await asyncio.gather(
        *(
            asyncio.to_thread(
                repository.admit_transcript,
                submission,
                worker_control_secret=WORKER_CONTROL_SECRET,
                now=NOW + timedelta(seconds=3),
            )
            for submission in submissions
        )
    )
    assert [admission.canonical_text for admission in admissions] == list(TRANSCRIPTS)
    assert all(admission.turn.state == "submitting" for admission in admissions)
    assert [admission.turn.user_id for admission in admissions] == list(USERS)
    assert [admission.turn.chat_id for admission in admissions] == list(CHATS)

    transplanted = replace(submissions[0], user_id=USERS[1])
    with pytest.raises(
        TranscriptSubmissionRejected,
        match="invalid_binding",
    ) as cross_user_transcript:
        await asyncio.to_thread(
            repository.admit_transcript,
            transplanted,
            worker_control_secret=WORKER_CONTROL_SECRET,
            now=NOW + timedelta(seconds=4),
        )
    assert TRANSCRIPTS[0] not in repr(cross_user_transcript.value)
    assert USERS[0] not in repr(cross_user_transcript.value)

    wrong_user_request = _activation_request(1, takeover=True)
    with pytest.raises(VoiceSessionNotFound) as wrong_user_takeover:
        await runtime.take_over_session(
            user_id=USERS[1],
            session_id=sessions[0].session_id,
            control=controls[1],
            request=wrong_user_request,
        )
    assert USERS[0] not in repr(wrong_user_takeover.value)
    assert sessions[0].session_id not in repr(wrong_user_takeover.value)

    takeover_results = await asyncio.gather(
        *(
            runtime.take_over_session(
                user_id=user_id,
                session_id=sessions[index].session_id,
                control=controls[index],
                request=_activation_request(index, takeover=True),
            )
            for index, user_id in enumerate(USERS)
        )
    )
    replacements = []
    for index, old in enumerate(sessions):
        ended = await asyncio.to_thread(
            repository.get_session,
            user_id=USERS[index],
            session_id=old.session_id,
        )
        assert ended.state == "ended"
        assert ended.end_reason == "takeover"
        abandoned = await asyncio.to_thread(
            repository.get_turn,
            user_id=USERS[index],
            turn_id=bindings[index].turn_id,
        )
        assert abandoned.state == "abandoned"
        assert abandoned.rejection_reason == "stale_session"
        assert abandoned.rejection_retry_policy == "explicit_user_retry"
        replacement = await asyncio.to_thread(
            repository.get_live_session,
            user_id=USERS[index],
        )
        assert replacement is not None
        replacements.append(replacement)
        assert replacement.generation == 2
        assert replacement.takeover_of_session_id == old.session_id
        assert replacement.user_id == USERS[index]
        assert replacement.visible_chat_id == CHATS[index]
        assert takeover_results[index].payload["session"]["session_id"] == (
            replacement.session_id
        )
    assert len({item.session_id for item in replacements}) == USER_COUNT
    assert len({item.room_name for item in replacements}) == USER_COUNT
    assert pool.readiness().capacity_available == 0
    assert (
        (
            await asyncio.to_thread(
                database.fetch_one,
                "SELECT COUNT(*) AS count FROM voice_session WHERE ended_at IS NULL",
            )
        )["count"]
        == USER_COUNT
    )

    database_voice_dump = repr(
        await asyncio.to_thread(
            database.fetch_all,
            "SELECT turn_id, user_id, chat_id, session_id, state, "
            "rejection_reason FROM voice_turn ORDER BY user_id"
        )
    )
    worker_event_dump = repr(
        [
            json.loads(payload)
            for socket in worker_sockets.values()
            for payload in socket.sent
        ]
    )
    metric_dump = repr(metrics.snapshot())
    media_event_dump = repr(media.events)
    for transcript in TRANSCRIPTS:
        assert transcript not in database_voice_dump
        assert transcript not in worker_event_dump
        assert transcript not in metric_dump
        assert transcript not in media_event_dump
    for user_id in USERS:
        assert user_id not in worker_event_dump
        assert user_id not in metric_dump
        assert user_id not in media_event_dump
    for bearer in bearers:
        assert bearer not in worker_event_dump
        assert bearer not in metric_dump
        assert bearer not in media_event_dump
    assert WORKER_TOKEN_SENTINEL not in metric_dump
    assert CLIENT_TOKEN_SENTINEL not in metric_dump
    assert "voice_session_total" in metric_dump
    assert "voice_takeover_total" in metric_dump

    # Keep the current bindings authoritative after every unrelated takeover.
    for index, bearer in enumerate(bearers):
        assert (
            control_orchestrator.validate_voice_control_binding(
                bearer=bearer,
                subject=USERS[index],
                device_id=DEVICES[index],
                connection_generation=CONNECTIONS[index],
            ).binding_id
            == controls[index]["binding_id"]
        )


def _admission_classes() -> tuple[AdmissionClassConfig, ...]:
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=20,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="voice-isolation-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=20,
            queue_limit=20,
            max_wait_ms=5_000,
            config_revision="voice-isolation-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.VOICE_INTERACTIVE,
            parent_class_name=AdmissionClass.INTERACTIVE,
            active_limit=10,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="voice-isolation-065",
        ),
    )


def _admission_request(user_index: int, request_index: int) -> OperationRequest:
    submission_id = uuid.UUID(
        deterministic_uuid4(
            "voice-isolation-admission-submission",
            str(user_index),
            str(request_index),
        )
    )
    return OperationRequest(
        operation_kind="voice_chat_message",
        admission_class=AdmissionClass.VOICE_INTERACTIVE,
        owner=OperationOwner(OwnerScope.USER, USERS[user_index], None),
        submission_id=submission_id,
        idempotency_namespace="voice_chat_message",
        idempotency_key=str(submission_id),
        normalized_input_digest="ab" * 32,
        chat_id=CHATS[user_index],
        parent_operation_id=None,
        connection_generation=uuid.UUID(CONNECTIONS[user_index]),
        request_generation=uuid.UUID(
            deterministic_uuid4(
                "voice-isolation-admission-generation",
                str(user_index),
                str(request_index),
            )
        ),
    )


def test_postgres_capacity_race_is_bounded_per_each_of_five_users(
    database: VoicePlaneTestRuntime,
) -> None:
    coordinator = WorkAdmissionCoordinator(
        admission_classes=_admission_classes(),
        repository=plane_work_admission_repository(database),
        operation_retention=timedelta(hours=24),
    )
    requests = tuple(
        _admission_request(user_index, request_index)
        for user_index in range(USER_COUNT)
        for request_index in range(3)
    )
    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        results = tuple(executor.map(coordinator.submit, requests))

    for user_index, user_id in enumerate(USERS):
        owned = [
            result
            for request, result in zip(requests, results, strict=True)
            if request.owner.owner_user_id == user_id
        ]
        accepted = [item for item in owned if not isinstance(item, RefusedAdmission)]
        refused = [item for item in owned if isinstance(item, RefusedAdmission)]
        assert len(accepted) == 2
        assert all(item.state is OperationState.RUNNING for item in accepted)
        assert refused == [
            RefusedAdmission(
                accepted=False,
                code="capacity_exceeded",
                retryable=True,
                retry_after_ms=1_000,
            )
        ]
        accepted_ids = {item.operation_id for item in accepted}
        assert len(accepted_ids) == 2
        for operation_id in accepted_ids:
            with pytest.raises(OperationNotFoundError, match="operation not found"):
                coordinator.query_operation(
                    owner=OperationOwner(
                        OwnerScope.USER,
                        USERS[(user_index + 1) % USER_COUNT],
                        None,
                    ),
                    operation_id=operation_id,
                )

    status = coordinator.inspect_admission_class(AdmissionClass.VOICE_INTERACTIVE)
    assert status.active_count == USER_COUNT * 2
    assert status.queued_count == 0
    for request, result in zip(requests, results, strict=True):
        reconciled = coordinator.reconcile_submission(
            owner=request.owner,
            submission_id=request.submission_id,
        )
        if isinstance(result, RefusedAdmission):
            assert reconciled == result
        else:
            assert reconciled.accepted
            assert reconciled.operation.operation_id == result.operation_id
        with pytest.raises(
            OperationNotFoundError,
            match="operation submission not found",
        ):
            coordinator.reconcile_submission(
                owner=OperationOwner(
                    OwnerScope.USER,
                    USERS[(USERS.index(request.owner.owner_user_id) + 1) % USER_COUNT],
                    None,
                ),
                submission_id=request.submission_id,
            )

    result_dump = repr(results)
    assert all(transcript not in result_dump for transcript in TRANSCRIPTS)
    assert WORKER_TOKEN_SENTINEL not in result_dump
    assert CLIENT_TOKEN_SENTINEL not in result_dump
