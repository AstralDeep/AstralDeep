"""Exercises composed voice routes and runtime transitions with an in-memory repository and overridden HTTP identity.
Real binding verification covers stale and expired authority; scripted chat dispatches exercise transition formatting without qualifying permission, LETS or physical mobile acceptance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.auth import require_user_id
from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_api import router as voice_router
from orchestrator.voice_backend import SpeechBackendSelection, VoiceSpeechBackend
from orchestrator.voice_bootstrap import VoiceServices
from orchestrator.voice_control_binding import (
    VoiceControlBindingError,
    VoiceControlBindingIssuer,
    VoiceControlClaims,
)
from orchestrator.voice_runtime import ActivatedVoiceMedia, VoiceSessionRuntime
from orchestrator.voice_sessions import (
    RecognitionBinding,
    SessionMutation,
    StaleSessionFence,
    TranscriptAdmission,
    TurnMutation,
    VoiceSessionRecord,
    VoiceSessionRepositoryError,
    VoiceTurnRecord,
)
from shared.protocol import (
    Message,
    VoiceLocalFinal,
    VoiceLocalPlayoutEvent,
    VoiceLocalReady,
    VoiceLocalRecognitionStarted,
    VoicePlayoutEvent,
)

def _uuid(number: int) -> str:
    return str(UUID(int=(4 << 76) | (0x8 << 60) | number))


NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
USER = "trace-owner"
BEARER_SECRET = b"trace-matrix-binding-secret-0123456789abcdef"
DEVICE_A = _uuid(1)
DEVICE_B = _uuid(2)
CONNECTION_WIFI = _uuid(3)
CONNECTION_CELLULAR = _uuid(4)
CHAT_HOME = _uuid(5)
CHAT_NEW = _uuid(6)
TURN_TEXT = "trace transcript utterance for the composed matrix"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def utcnow(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _TraceSocket:
    __slots__ = ("device_id", "connection_generation")

    def __init__(self, device_id: str, connection_generation: str) -> None:
        self.device_id = device_id
        self.connection_generation = connection_generation


@dataclass
class _RegisteredBinding:
    socket: object
    claims: VoiceControlClaims
    bearer: str
    connection_generation: str


class _TraceRepository:
    def __init__(self, clock: _Clock, replica_id: str) -> None:
        self.clock = clock
        self.replica_id = replica_id
        self.sessions: dict[tuple[str, str], VoiceSessionRecord] = {}
        self.turns: dict[tuple[str, str], VoiceTurnRecord] = {}
        self.session_ids: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.playout_records: list[dict[str, Any]] = []

    def _require_fences(
        self,
        record: VoiceSessionRecord,
        *,
        expected_generation: int | None,
        expected_media_grant_revision: int | None,
        expected_speech_backend: str | None,
    ) -> None:
        if (
            expected_generation is not None
            and record.generation != expected_generation
        ):
            raise StaleSessionFence("stale_generation")
        if (
            expected_media_grant_revision is not None
            and record.media_grant_revision != expected_media_grant_revision
        ):
            raise StaleSessionFence("stale_media_grant_revision")
        if (
            expected_speech_backend is not None
            and record.speech_backend != expected_speech_backend
        ):
            raise VoiceSessionRepositoryError("invalid_binding")

    def _require_control(
        self,
        record: VoiceSessionRecord,
        control: Any,
    ) -> VoiceSessionRecord:
        if control is None:
            return record
        if record.device_id != control.device_id:
            raise VoiceSessionRepositoryError("binding_scope_mismatch")
        if control.binding_expires_at <= self.clock.utcnow():
            raise VoiceSessionRepositoryError("binding_expired")
        if (
            record.owner_connection_generation == control.connection_generation
            and record.control_binding_id == control.binding_id
        ):
            return record
        updated = replace(
            record,
            owner_connection_generation=control.connection_generation,
            control_binding_id=control.binding_id,
            control_binding_expires_at=control.binding_expires_at,
        )
        self.sessions[(record.user_id, record.session_id)] = updated
        return updated

    def create_session(self, request: Any, *, now: datetime) -> SessionMutation:
        self.calls.append(("create_session", request.activation_id))
        session_key = (request.user_id, request.activation_id)
        session_id = self.session_ids.get(session_key)
        if session_id is None:
            session_id = str(uuid4())
            self.session_ids[session_key] = session_id
        record = VoiceSessionRecord(
            session_id=session_id,
            user_id=request.user_id,
            activation_id=request.activation_id,
            device_id=request.device_id,
            device_kind=request.device_kind,
            transport=request.transport,
            room_name=request.room_name,
            participant_identity=request.participant_identity,
            worker_identity=None,
            visible_chat_id=request.visible_chat_id,
            chat_context_revision=1,
            applied_visible_chat_id=None,
            applied_chat_context_revision=None,
            state="starting",
            speech_muted=False,
            microphone_enabled=True,
            foreground_active=True,
            foreground_reason="foreground",
            generation=1,
            media_grant_revision=1,
            owner_connection_generation=request.owner_connection_generation,
            control_binding_id=request.control_binding_id,
            control_binding_expires_at=request.control_binding_expires_at,
            lease_expires_at=request.lease_expires_at,
            control_owner_id=None,
            control_lease_expires_at=None,
            last_interaction_at=now,
            idle_started_at=None,
            started_at=now,
            updated_at=now,
            ended_at=None,
            end_reason=None,
            chat_unavailable_at=None,
            takeover_of_session_id=None,
            media_grant_nonce_hash=request.media_grant_nonce_hash,
            media_grant_expires_at=request.media_grant_expires_at or now,
            media_grant_consumed_at=None,
            last_media_refresh_id=None,
            media_grant_issued_at=request.media_grant_issued_at or now,
            worker_assignment_id=None,
            worker_rtc_grant_revision=1,
            worker_rtc_grant_issued_at=None,
            worker_rtc_grant_expires_at=None,
            speech_backend=request.speech_backend,
        )
        self.sessions[(request.user_id, session_id)] = record
        return SessionMutation(record)

    def take_over_session(self, request: Any, *, now: datetime) -> SessionMutation:
        self.calls.append(("take_over_session", request.previous_session_id))
        previous = self.sessions[(request.user_id, request.previous_session_id)]
        record = replace(
            previous,
            generation=previous.generation + 1,
            state="starting",
            applied_visible_chat_id=None,
            applied_chat_context_revision=None,
            speech_backend=previous.speech_backend,
        )
        self.sessions[(request.user_id, request.session_id)] = record
        return SessionMutation(record)

    def get_session(self, *, user_id: str, session_id: str) -> VoiceSessionRecord:
        self.calls.append(("get_session", session_id))
        return self.sessions[(user_id, session_id)]

    def get_controlled_session(self, **kwargs: Any) -> VoiceSessionRecord:
        self.calls.append(("get_controlled_session", kwargs.get("session_id")))
        record = self.sessions[(kwargs["user_id"], kwargs["session_id"])]
        self._require_fences(
            record,
            expected_generation=kwargs.get("expected_generation"),
            expected_media_grant_revision=kwargs.get(
                "expected_media_grant_revision"
            ),
            expected_speech_backend=kwargs.get("expected_speech_backend"),
        )
        record = self._require_control(record, kwargs.get("control"))
        return record

    async def claim_control_lease(self, **kwargs: Any):
        self.calls.append(("claim_control_lease", kwargs["session_id"]))
        record = self.sessions[(kwargs["user_id"], kwargs["session_id"])]
        if record.generation != kwargs["generation"]:
            raise StaleSessionFence("stale_generation")
        updated = replace(
            record,
            control_owner_id=kwargs["owner_id"],
            control_lease_expires_at=kwargs["now"] + timedelta(seconds=15),
        )
        self.sessions[(kwargs["user_id"], kwargs["session_id"])] = updated
        return SimpleNamespace(
            owner_id=kwargs["owner_id"],
            expires_at=kwargs["now"] + timedelta(seconds=15),
        )

    def assign_worker(self, **kwargs: Any) -> SessionMutation:
        self.calls.append(("assign_worker", kwargs["session_id"]))
        record = self.sessions[(kwargs["user_id"], kwargs["session_id"])]
        updated = replace(
            record,
            worker_identity=kwargs["worker_identity"],
            worker_assignment_id=kwargs["assignment_id"],
        )
        self.sessions[(kwargs["user_id"], kwargs["session_id"])] = updated
        return SessionMutation(updated)

    def apply_chat_context(self, **kwargs: Any) -> SessionMutation:
        self.calls.append(("apply_chat_context", kwargs.get("visible_chat_id")))
        record = self.sessions[(kwargs["user_id"], kwargs["session_id"])]
        if record.generation != kwargs["expected_generation"]:
            raise StaleSessionFence("stale_generation")
        updated = replace(
            record,
            applied_visible_chat_id=kwargs["visible_chat_id"],
            applied_chat_context_revision=kwargs["chat_context_revision"],
        )
        self.sessions[(kwargs["user_id"], kwargs["session_id"])] = updated
        return SessionMutation(updated)

    def mark_session_active(self, **kwargs: Any) -> VoiceSessionRecord:
        self.calls.append(("mark_session_active", kwargs["session_id"]))
        record = self.sessions[(kwargs["user_id"], kwargs["session_id"])]
        if record.generation != kwargs["expected_generation"]:
            raise StaleSessionFence("stale_generation")
        if record.media_grant_revision != kwargs["expected_media_grant_revision"]:
            raise StaleSessionFence("stale_media_grant_revision")
        updated = replace(record, state="active")
        self.sessions[(kwargs["user_id"], kwargs["session_id"])] = updated
        return updated

    def update_session(self, request: Any, *, now: datetime) -> VoiceSessionRecord:
        self.calls.append(("update_session", request.session_id))
        record = self.sessions[(request.user_id, request.session_id)]
        self._require_fences(
            record,
            expected_generation=request.expected_generation,
            expected_media_grant_revision=request.expected_media_grant_revision,
            expected_speech_backend=request.expected_speech_backend,
        )
        record = self._require_control(record, request.control)
        state = record.state
        if request.foreground_active is False:
            state = "suspended"
        elif request.foreground_active is True:
            state = "reconnecting"
        chat_revision = record.chat_context_revision + (
            1
            if request.visible_chat_id
            and request.visible_chat_id != record.visible_chat_id
            else 0
        )
        updated = replace(
            record,
            state=state,
            foreground_active=(
                record.foreground_active
                if request.foreground_active is None
                else request.foreground_active
            ),
            foreground_reason=(
                request.foreground_reason or record.foreground_reason
            ),
            microphone_enabled=(
                record.microphone_enabled
                if request.microphone_enabled is None
                else request.microphone_enabled
            ),
            speech_muted=(
                record.speech_muted
                if request.speech_muted is None
                else request.speech_muted
            ),
            visible_chat_id=request.visible_chat_id or record.visible_chat_id,
            chat_context_revision=chat_revision,
        )
        self.sessions[(request.user_id, request.session_id)] = updated
        return updated

    def renew_session_lease(self, **kwargs: Any) -> VoiceSessionRecord:
        self.calls.append(("renew_session_lease", kwargs["session_id"]))
        record = self.sessions[(kwargs["user_id"], kwargs["session_id"])]
        if record.generation != kwargs["expected_generation"]:
            raise StaleSessionFence("stale_generation")
        updated = replace(
            record,
            lease_expires_at=kwargs["now"] + kwargs["lease_duration"],
        )
        self.sessions[(kwargs["user_id"], kwargs["session_id"])] = updated
        return updated

    def end_session(self, **kwargs: Any) -> VoiceSessionRecord:
        self.calls.append(("end_session", kwargs["session_id"]))
        record = self.sessions[(kwargs["user_id"], kwargs["session_id"])]
        self._require_fences(
            record,
            expected_generation=kwargs.get("expected_generation"),
            expected_media_grant_revision=kwargs.get(
                "expected_media_grant_revision"
            ),
            expected_speech_backend=None,
        )
        record = self._require_control(record, kwargs.get("control"))
        updated = replace(
            record,
            state="ended",
            ended_at=kwargs["now"],
            end_reason=kwargs["reason"],
            foreground_active=False,
            foreground_reason="connection_lost",
            microphone_enabled=False,
            control_owner_id=None,
            control_lease_expires_at=None,
        )
        self.sessions[(kwargs["user_id"], kwargs["session_id"])] = updated
        return updated

    def refresh_media_grant(self, request: Any, *, now: datetime) -> SessionMutation:
        self.calls.append(("refresh_media_grant", request.refresh_id))
        record = self.sessions[(request.user_id, request.session_id)]
        if record.last_media_refresh_id == request.refresh_id:
            return SessionMutation(record, replayed=True)
        updated = replace(
            record,
            media_grant_revision=record.media_grant_revision + 1,
            participant_identity=request.participant_identity,
            media_grant_nonce_hash=request.nonce_hash,
            media_grant_issued_at=request.issued_at,
            media_grant_expires_at=request.expires_at,
            last_media_refresh_id=request.refresh_id,
        )
        self.sessions[(request.user_id, request.session_id)] = updated
        return SessionMutation(updated)

    def bind_recognition_turn(
        self,
        binding: RecognitionBinding,
        *,
        now: datetime,
    ) -> TurnMutation:
        self.calls.append(("bind_recognition_turn", binding.client_turn_id))
        key = (binding.user_id, binding.client_turn_id)
        existing = self.turns.get(key)
        if existing is not None:
            return TurnMutation(existing, replayed=True)
        turn = VoiceTurnRecord(
            turn_id=str(uuid4()),
            client_turn_id=binding.client_turn_id,
            session_id=binding.session_id,
            session_generation=binding.session_generation,
            media_grant_revision=binding.media_grant_revision,
            user_id=binding.user_id,
            chat_id=binding.chat_id,
            chat_context_revision=binding.chat_context_revision,
            execution_base_render_revision=binding.execution_base_render_revision,
            submission_id=str(uuid4()),
            request_generation=str(uuid4()),
            message_id=None,
            acceptance_commit_id=None,
            result_commit_id=None,
            operation_id=None,
            state="recognizing",
            is_foreground=True,
            detected_language="en",
            spoken_output_policy="full_recap",
            output_reason="voice",
            terminal_kind=None,
            rejection_reason=None,
            rejection_retry_policy=None,
            recap_source="voice",
            sensitivity="normal",
            announcement_sequence=0,
            result_reserved_samples=0,
            result_quantum_count=0,
            last_phrase_key=None,
            next_announcement_due_at=None,
            accepted_at=None,
            processing_started_at=None,
            terminal_at=None,
            created_at=now,
            updated_at=now,
        )
        self.turns[key] = turn
        return TurnMutation(turn)

    def get_turn(self, *, user_id: str, turn_id: str) -> VoiceTurnRecord | None:
        self.calls.append(("get_turn", turn_id))
        for record in self.turns.values():
            if record.user_id == user_id and record.turn_id == turn_id:
                return record
        return None

    def admit_local_transcript(
        self,
        request: Any,
        *,
        now: datetime,
    ) -> TranscriptAdmission:
        self.calls.append(("admit_local_transcript", request.turn_id))
        turn = self.turns.get((request.user_id, request.client_turn_id))
        if turn is None or turn.turn_id != request.turn_id:
            raise VoiceControlBindingError("invalid_binding")
        session = self.sessions[(request.user_id, request.session_id)]
        if (
            session.ended_at is not None
            or session.state != "active"
            or not session.foreground_active
            or not session.microphone_enabled
            or session.generation != request.generation
            or session.media_grant_revision != request.speech_revision
            or session.control_owner_id != request.expected_control_owner_id
            or session.control_lease_expires_at is None
            or session.control_lease_expires_at <= now
            or session.lease_expires_at <= now
        ):
            raise VoiceControlBindingError("invalid_binding")
        if turn.state == "accepted":
            return TranscriptAdmission(
                canonical_text=request.canonical_text,
                turn=turn,
                replayed=True,
            )
        accepted_turn = replace(turn, state="accepted", accepted_at=now)
        self.turns[(request.user_id, request.client_turn_id)] = accepted_turn
        return TranscriptAdmission(
            canonical_text=request.canonical_text,
            turn=accepted_turn,
            replayed=False,
        )

    def reject_transcript(
        self,
        *,
        user_id: str,
        turn_id: str,
        reason: str,
        retry_policy: str,
        now: datetime,
    ) -> TurnMutation:
        self.calls.append(("reject_transcript", turn_id, reason))
        for key, turn in self.turns.items():
            if turn.user_id == user_id and turn.turn_id == turn_id:
                rejected = replace(
                    turn,
                    state="rejected",
                    rejection_reason=reason,
                    rejection_retry_policy=retry_policy,
                    terminal_kind="rejected",
                    terminal_at=now,
                )
                self.turns[key] = rejected
                return TurnMutation(rejected)
        raise VoiceControlBindingError("invalid_binding")

    def accept_transcript(
        self,
        *,
        user_id: str,
        turn_id: str,
        message_id: int,
        accepted_connection_generation: str,
        acceptance_commit_id: str | None,
        operation_id: str | None,
        now: datetime,
        result_commit_id: str | None = None,
        transaction: Any | None = None,
    ) -> TurnMutation:
        self.calls.append(("accept_transcript", turn_id, message_id))
        for key, turn in self.turns.items():
            if turn.user_id == user_id and turn.turn_id == turn_id:
                accepted = replace(
                    turn,
                    message_id=message_id,
                    acceptance_commit_id=acceptance_commit_id,
                    result_commit_id=result_commit_id,
                    operation_id=operation_id,
                    accepted_at=now,
                )
                self.turns[key] = accepted
                return TurnMutation(accepted)
        raise VoiceControlBindingError("invalid_binding")

    def abandon_preacceptance_turns(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        now: datetime,
    ) -> None:
        self.calls.append(("abandon_preacceptance_turns", session_id))
        for key, turn in self.turns.items():
            if (
                turn.user_id == user_id
                and turn.session_id == session_id
                and turn.session_generation == generation
                and turn.state not in {"accepted", "rejected", "abandoned"}
            ):
                self.turns[key] = replace(
                    turn,
                    state="abandoned",
                    terminal_kind="abandoned",
                    terminal_at=now,
                )

    def record_client_playout(self, **kwargs: Any) -> None:
        self.calls.append(("record_client_playout", kwargs["announcement_id"]))
        self.playout_records.append(kwargs)

    def durable_material(self) -> str:
        return json.dumps(
            {
                "sessions": [
                    vars(record) if hasattr(record, "__dict__") else repr(record)
                    for record in self.sessions.values()
                ],
                "turns": [
                    repr(turn) for turn in self.turns.values()
                ],
                "calls": [repr(call) for call in self.calls],
                "playouts": [repr(row) for row in self.playout_records],
            },
            default=repr,
        )


class _LocalMedia:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def activate(self, session: Any) -> ActivatedVoiceMedia:
        self.calls.append(("activate", session.session_id))
        raise AssertionError("client_local sessions never activate remote media")

    async def assignment_is_current(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def apply_context(self, session: Any) -> None:
        self.calls.append(("context", session.visible_chat_id))

    async def set_capture(self, session: Any, enabled: bool) -> None:
        self.calls.append(("capture", enabled))

    async def stop_speech(self, session: Any) -> None:
        self.calls.append(("stop_speech", session.session_id))

    async def barge_in(self, session: Any) -> None:
        self.calls.append(("barge_in", session.session_id))

    async def end(self, session: Any, reason: str) -> None:
        self.calls.append(("end", reason))

    async def abort(self, session: Any) -> None:
        self.calls.append(("abort", session.session_id))

    async def rotate_media_grant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("client_local sessions never rotate remote grants")


class _RemoteMedia:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, Any]] = []
        self.assignment_current = True

    async def activate(self, session: Any) -> ActivatedVoiceMedia:
        self.calls.append(("activate", session.session_id))
        return ActivatedVoiceMedia(
            assignment_id=_uuid(60),
            worker_identity="voice-worker-trace",
            worker_grant_issued_at=self.clock.utcnow(),
            worker_grant_expires_at=self.clock.utcnow() + timedelta(minutes=5),
            client_grant={
                "grant_id": "grant-trace",
                "transport": "livekit",
                "join_token": "trace-join-token-secret",
            },
        )

    async def assignment_is_current(
        self,
        session: Any,
        *,
        assignment_id: str,
        worker_identity: str,
    ) -> bool:
        self.calls.append(("assignment_current", assignment_id))
        return self.assignment_current

    async def apply_context(self, session: Any) -> None:
        self.calls.append(("context", session.visible_chat_id))

    async def set_capture(self, session: Any, enabled: bool) -> None:
        self.calls.append(("capture", enabled))

    async def stop_speech(self, session: Any) -> None:
        self.calls.append(("stop_speech", session.session_id))

    async def barge_in(self, session: Any) -> None:
        self.calls.append(("barge_in", session.session_id))

    async def end(self, session: Any, reason: str) -> None:
        self.calls.append(("end", reason))

    async def abort(self, session: Any) -> None:
        self.calls.append(("abort", session.session_id))

    async def rotate_media_grant(
        self,
        previous: Any,
        session: Any,
        *,
        refresh_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("rotate", session.media_grant_revision))
        return {
            "grant_id": "grant-rotated",
            "transport": session.transport,
            "session_id": session.session_id,
            "generation": session.generation,
            "media_grant_revision": session.media_grant_revision,
            "join_token": "trace-rotated-join-token-secret",
        }

    async def current_session(self, session_id: str, generation: int) -> Any:
        self.calls.append(("current_session", session_id))
        return SimpleNamespace(session_id=session_id, generation=generation)

    async def accept_client_playout(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("accept_client_playout", kwargs["event"].announcement_id))
        event = kwargs["event"]
        session = kwargs["session"]
        if (
            getattr(session, "ended_at", None) is not None
            or getattr(session, "state", "ended") != "active"
            or getattr(session, "foreground_active", False) is not True
        ):
            raise VoiceControlBindingError("invalid_binding")
        fence = SimpleNamespace(
            session_id=event.session_id,
            generation=event.generation,
            media_grant_revision=event.media_grant_revision,
            announcement_id=event.announcement_id,
            announcement_sequence=event.announcement_sequence,
            turn_id=event.turn_id,
            kind=event.kind,
            quantum_role=event.quantum_role,
            quantum_index=event.quantum_index,
            result_reserved_samples_after=0,
        )
        return SimpleNamespace(
            fence=fence,
            phase=event.phase,
            client_sequence=event.client_sequence,
            turn_announcement_sequence=event.announcement_sequence,
            received_at=self.clock.utcnow(),
        )

    async def release_capture_after_playout(self, *args: Any) -> None:
        self.calls.append(("release_capture", args[0].session_id))


class _LocalCapability:
    def __init__(self, status: str = "requires_client_readiness") -> None:
        self._status = status

    async def readiness(self) -> SimpleNamespace:
        return SimpleNamespace(
            status=self._status,
            reason="client_readiness_required",
        )

    def feature_enabled(self) -> bool:
        return True


class _RemoteCapability:
    async def readiness(self) -> SimpleNamespace:
        return SimpleNamespace(status="ready", reason="ready")


class _ChatHistory:
    def __init__(self) -> None:
        self.chats: dict[str, dict[str, Any]] = {
            CHAT_HOME: {"chat_id": CHAT_HOME, "render_revision": 4},
            CHAT_NEW: {"chat_id": CHAT_NEW, "render_revision": 2},
        }

    def get_chat(self, chat_id: str, *, user_id: str) -> dict[str, Any] | None:
        if user_id != USER:
            return None
        chat = self.chats.get(chat_id)
        return dict(chat) if chat is not None else None


class _Dispatcher:
    def __init__(self, harness: "_Harness") -> None:
        self.harness = harness
        self.dispatched: list[dict[str, Any]] = []
        self.denials: list[str] = []

    async def __call__(
        self,
        websocket: Any,
        message: str,
        chat_id: str,
        *,
        user_id: str,
        voice_dispatch: Any = None,
        **_: Any,
    ) -> None:
        if self.denials:
            reason = self.denials.pop(0)
            turn = voice_dispatch.admission.turn
            self.dispatched.append(
                {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "text": message,
                    "replayed": bool(voice_dispatch.admission.replayed),
                    "turn_id": turn.turn_id,
                    "denied": True,
                }
            )
            await self.harness.socket_orchestrator._reject_voice_submission(
                websocket,
                user_id=user_id,
                origin=voice_dispatch.origin,
                submission_id=str(turn.submission_id),
                request_generation=str(turn.request_generation),
                chat_id=str(turn.chat_id),
                connection_generation=voice_dispatch.connection_generation,
                reason=reason,
                retry_policy="none",
            )
            return
        self.dispatched.append(
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "text": message,
                "replayed": bool(voice_dispatch.admission.replayed),
                "turn_id": voice_dispatch.admission.turn.turn_id,
                "denied": False,
            }
        )
        await asyncio.to_thread(
            self.harness.services.repository.accept_transcript,
            user_id=user_id,
            turn_id=voice_dispatch.admission.turn.turn_id,
            message_id=len(self.dispatched) + 100,
            accepted_connection_generation=voice_dispatch.connection_generation,
            acceptance_commit_id="commit-trace",
            operation_id="operation-trace",
            now=self.harness.clock.utcnow(),
        )


async def _safe_send_recorder(self, websocket: Any, data: str) -> bool:
    self._sent_frames.append(data)
    return True


class _Harness:
    def __init__(self, backend: VoiceSpeechBackend, *, media_grant_seconds: int = 300) -> None:
        self.clock = _Clock()
        self.replica_id = "trace-replica"
        self.backend = backend
        self.selection = SpeechBackendSelection(
            value=backend,
            valid=True,
            source="explicit",
        )
        self.repository = _TraceRepository(self.clock, self.replica_id)
        self.capability = (
            _LocalCapability() if backend is VoiceSpeechBackend.CLIENT_LOCAL else _RemoteCapability()
        )
        self.media = _LocalMedia() if backend is VoiceSpeechBackend.CLIENT_LOCAL else _RemoteMedia(
            self.clock
        )
        self.runtime = VoiceSessionRuntime(
            repository=self.repository,
            capability=self.capability,
            media=self.media,
            replica_id=self.replica_id,
            clock=self.clock.utcnow,
            media_grant_seconds=media_grant_seconds,
            speech_backend=backend,
            backend_selection=self.selection,
        )
        self.services = VoiceServices(
            livekit=None,
            worker_pool=None,
            repository=self.repository,
            coordinator=None,
            capability=self.capability,
            media=self.media,
            runtime=self.runtime,
            worker_control_settings=None,
            speech_backend=backend,
            backend_selection=self.selection,
        )
        self.history = _ChatHistory()
        self.dispatcher = _Dispatcher(self)
        self.sent_frames: list[str] = []
        self.composer_publications: list[dict[str, Any]] = []
        self.issuer = VoiceControlBindingIssuer(
            BEARER_SECRET,
            clock=self.clock.utcnow,
        )
        self.ui_sessions: dict[int, dict[str, str]] = {}
        self._voice_control_bindings: dict[int, VoiceControlClaims] = {}
        self._voice_device_bindings: dict[tuple[str, str], int] = {}
        self.bindings: dict[str, _RegisteredBinding] = {}
        self.socket_orchestrator = self._build_socket_orchestrator()
        self._bind_runtime_handlers()
        self.app = self._build_api()

    def _build_socket_orchestrator(self) -> SimpleNamespace:
        stub = SimpleNamespace()
        stub.voice_services = self.services
        stub.voice_runtime = self.runtime
        stub.history = self.history
        stub.ui_sessions = self.ui_sessions
        stub._voice_control_bindings = self._voice_control_bindings
        stub._voice_device_bindings = self._voice_device_bindings
        stub._voice_binding_issuer = self.issuer
        stub._voice_binding_runtime_issuer = lambda: self.issuer
        stub._sent_frames = self.sent_frames
        stub._safe_send = MethodType(_safe_send_recorder, stub)
        stub._parsed_ui_frame = Orchestrator._parsed_ui_frame
        stub._rfc3339 = Orchestrator._rfc3339
        stub._voice_occurred_at = Orchestrator._voice_occurred_at
        stub._client_local_socket_authority = MethodType(
            Orchestrator._client_local_socket_authority, stub
        )
        stub._handle_voice_local_ready = MethodType(
            Orchestrator._handle_voice_local_ready, stub
        )
        stub._handle_voice_local_recognition_started = MethodType(
            Orchestrator._handle_voice_local_recognition_started, stub
        )
        stub._handle_voice_local_final = MethodType(
            Orchestrator._handle_voice_local_final, stub
        )
        stub._handle_voice_local_playout_event = MethodType(
            Orchestrator._handle_voice_local_playout_event, stub
        )
        stub._handle_voice_playout_event = MethodType(
            Orchestrator._handle_voice_playout_event, stub
        )
        stub._send_voice_local_rejection = MethodType(
            Orchestrator._send_voice_local_rejection, stub
        )
        stub._reject_voice_submission = MethodType(
            Orchestrator._reject_voice_submission, stub
        )
        stub.validate_voice_control_binding = MethodType(
            Orchestrator.validate_voice_control_binding, stub
        )
        stub.handle_chat_message = self.dispatcher
        stub.handle_ui_message = MethodType(Orchestrator.handle_ui_message, stub)
        stub.publish_voice_composer_state = self._publish_composer
        return stub

    async def _publish_composer(self, **kwargs: Any) -> None:
        self.composer_publications.append(kwargs)

    def _bind_runtime_handlers(self) -> None:
        self.runtime.bind_speech_stop_handler(self.services.stop_session_speech)
        self.runtime.bind_speech_suspend_handler(
            self.services.set_session_speech_suspended
        )
        self.runtime.bind_speech_mute_handler(
            self.services.set_session_speech_muted
        )
        self.runtime.bind_session_end_handler(
            self.services.handle_runtime_session_end
        )
        self.runtime.bind_local_buffer_cleanup_handler(
            self.services.cleanup_local_buffers
        )
        self.runtime.bind_local_session_end_fence_handlers(
            self.services.prepare_local_session_end,
            self.services.resolve_local_session_end,
        )

    def _build_api(self) -> FastAPI:
        app = FastAPI()
        app.state.orchestrator = self.socket_orchestrator
        app.dependency_overrides[require_user_id] = lambda: USER
        app.include_router(voice_router)
        return app

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://voice-trace.test",
        )

    def register_socket(
        self,
        connection_generation: str,
        *,
        device_id: str = DEVICE_A,
        credential_expires_at: datetime | None = None,
    ) -> _RegisteredBinding:
        socket = _TraceSocket(device_id, connection_generation)
        issued = self.issuer.mint(
            subject=USER,
            device_id=device_id,
            connection_generation=connection_generation,
            credential_expires_at=credential_expires_at or self.clock.utcnow() + timedelta(minutes=10),
        )
        displaced = self._voice_device_bindings.get((USER, device_id))
        if displaced is not None:
            cleanup = self.services.clear_local_connection
            cleanup(
                self._voice_control_bindings[displaced],
                socket_id=displaced,
            )
            self._voice_control_bindings.pop(displaced, None)
        self._voice_control_bindings[id(socket)] = issued.claims
        self._voice_device_bindings[(USER, device_id)] = id(socket)
        self.ui_sessions[socket] = {"sub": USER}
        binding = _RegisteredBinding(
            socket=socket,
            claims=issued.claims,
            bearer=issued.bearer,
            connection_generation=connection_generation,
        )
        self.bindings[connection_generation] = binding
        return binding

    def headers(self, binding: _RegisteredBinding) -> dict[str, str]:
        return {
            "X-Astral-Device-Id": binding.claims.device_id,
            "X-Astral-Connection-Generation": binding.connection_generation,
            "X-Astral-Voice-Control-Binding": binding.bearer,
        }

    async def send_frame(self, binding: _RegisteredBinding, frame: Any) -> None:
        await self.socket_orchestrator.handle_ui_message(
            binding.socket,
            frame.to_json(),
        )

    def sent_protocol_frames(self) -> list[Any]:
        parsed: list[Any] = []
        for payload in self.sent_frames:
            raw = json.loads(payload)
            try:
                parsed.append(Message.from_json(payload))
            except Exception:
                parsed.append(raw)
        return parsed

    def assert_no_sensitive_material_durable(self) -> None:
        durable = self.repository.durable_material()
        assert TURN_TEXT not in durable
        assert "raw-pcm-audio" not in durable
        assert "trace-join-token-secret" not in durable
        assert "trace-rotated-join-token-secret" not in durable
        for binding in self.bindings.values():
            assert binding.bearer not in durable
        for record in self.repository.sessions.values():
            assert getattr(record, "media_grant_nonce_hash", None) is None or isinstance(
                record.media_grant_nonce_hash, (bytes, type(None))
            )


def _frame_type(frame: Any) -> str:
    value = getattr(frame, "type", None)
    if isinstance(value, str):
        return value
    if isinstance(frame, dict):
        value = frame.get("type")
        return value if isinstance(value, str) else ""
    return ""


def _speech_revision(session: dict[str, Any]) -> int:
    value = session.get("speech_revision", session.get("media_grant_revision"))
    assert isinstance(value, int)
    return value


def _local_ready_frame(
    binding: _RegisteredBinding,
    session: dict[str, Any],
    *,
    client_sequence: int,
) -> VoiceLocalReady:
    return VoiceLocalReady(
        device_id=binding.claims.device_id,
        connection_generation=binding.connection_generation,
        session_id=session["session_id"],
        generation=session["generation"],
        speech_revision=_speech_revision(session),
        client_sequence=client_sequence,
    )


def _local_recognition_frame(
    binding: _RegisteredBinding,
    session: dict[str, Any],
    *,
    client_turn_id: str,
    chat_id: str,
    chat_context_revision: int,
    recognition_sequence: int,
) -> VoiceLocalRecognitionStarted:
    return VoiceLocalRecognitionStarted(
        device_id=binding.claims.device_id,
        connection_generation=binding.connection_generation,
        session_id=session["session_id"],
        generation=session["generation"],
        speech_revision=_speech_revision(session),
        client_turn_id=client_turn_id,
        chat_id=chat_id,
        chat_context_revision=chat_context_revision,
        recognition_sequence=recognition_sequence,
    )


def _local_final_frame(
    binding: _RegisteredBinding,
    turn_bound: dict[str, Any],
    *,
    text: str = TURN_TEXT,
) -> VoiceLocalFinal:
    return VoiceLocalFinal(
        device_id=binding.claims.device_id,
        connection_generation=binding.connection_generation,
        session_id=turn_bound["session_id"],
        generation=turn_bound["generation"],
        speech_revision=turn_bound["speech_revision"],
        client_turn_id=turn_bound["client_turn_id"],
        turn_id=turn_bound["turn_id"],
        submission_id=turn_bound["submission_id"],
        request_generation=turn_bound["request_generation"],
        chat_id=turn_bound["chat_id"],
        chat_context_revision=turn_bound["chat_context_revision"],
        recognition_sequence=turn_bound["recognition_sequence"],
        text=text,
        text_digest_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _local_capability_payload() -> dict[str, Any]:
    return {
        "contract": "client_local/v1",
        "transport": "client_local",
        "configured_locale": "en-US",
        "full_duplex": False,
        "has_microphone": True,
        "has_audio_output": True,
        "microphone_permission": "authorized",
        "recognition_permission": "authorized",
        "recognition_processing": "guaranteed_local",
        "recognition_locale": "ready",
        "recognition_installation": "ready",
        "synthesis_processing": "guaranteed_local",
        "synthesis_locale": "ready",
    }


def _remote_capability_payload() -> dict[str, Any]:
    return {
        "has_microphone": True,
        "has_audio_output": True,
        "microphone_permission": "authorized",
        "full_duplex": True,
        "transport": "livekit",
    }


async def _create_local_session(
    harness: _Harness,
    binding: _RegisteredBinding,
    *,
    chat_id: str = CHAT_HOME,
    device_kind: str = "ios",
    activation: str | None = None,
) -> httpx.Response:
    async with harness.client() as client:
        return await client.post(
            "/api/voice/v2/sessions",
            headers=harness.headers(binding),
            json={
                "schema_version": "2",
                "activation_id": activation or _uuid(20),
                "device_id": binding.claims.device_id,
                "device_kind": device_kind,
                "visible_chat_id": chat_id,
                "foreground_active": True,
                "client_capability": _local_capability_payload(),
            },
        )


async def _create_remote_session(
    harness: _Harness,
    binding: _RegisteredBinding,
    *,
    chat_id: str = CHAT_HOME,
    device_kind: str = "android",
    activation: str | None = None,
) -> httpx.Response:
    async with harness.client() as client:
        return await client.post(
            "/api/voice/sessions",
            headers=harness.headers(binding),
            json={
                "device_id": binding.claims.device_id,
                "device_kind": device_kind,
                "visible_chat_id": chat_id,
                "activation_id": activation or _uuid(21),
                "capability": _remote_capability_payload(),
                "foreground_active": True,
            },
        )


async def _patch_session(
    harness: _Harness,
    binding: _RegisteredBinding,
    session: dict[str, Any],
    **changes: Any,
) -> httpx.Response:
    body = {
        "expected_generation": session["generation"],
        "expected_media_grant_revision": _speech_revision(session),
    }
    body.update(changes)
    async with harness.client() as client:
        return await client.patch(
            f"/api/voice/sessions/{session['session_id']}",
            headers=harness.headers(binding),
            json=body,
        )


async def _ready_and_recognize(
    harness: _Harness,
    binding: _RegisteredBinding,
    session: dict[str, Any],
    *,
    chat_id: str = CHAT_HOME,
    client_sequence: int = 1,
    recognition_sequence: int | None = None,
    text: str = TURN_TEXT,
) -> tuple[dict[str, Any], str]:
    await harness.send_frame(
        binding,
        _local_ready_frame(
            binding,
            session,
            client_sequence=client_sequence,
        ),
    )
    ready_reply = Message.from_json(harness.sent_frames.pop())
    if recognition_sequence is None:
        recognition_sequence = client_sequence + 1
    client_turn_id = _uuid(30 + recognition_sequence)
    await harness.send_frame(
        binding,
        _local_recognition_frame(
            binding,
            session,
            client_turn_id=client_turn_id,
            chat_id=chat_id,
            chat_context_revision=session["chat_context_revision"],
            recognition_sequence=recognition_sequence,
        ),
    )
    bound_reply = Message.from_json(harness.sent_frames.pop())
    turn_bound = {
        "session_id": bound_reply.session_id,
        "generation": bound_reply.generation,
        "speech_revision": bound_reply.speech_revision,
        "client_turn_id": bound_reply.client_turn_id,
        "turn_id": bound_reply.turn_id,
        "submission_id": bound_reply.submission_id,
        "request_generation": bound_reply.request_generation,
        "chat_id": bound_reply.chat_id,
        "chat_context_revision": bound_reply.chat_context_revision,
        "recognition_sequence": bound_reply.recognition_sequence,
    }
    assert ready_reply.type == "voice_local_session_ready"
    assert bound_reply.type == "voice_local_turn_bound"
    await harness.send_frame(binding, _local_final_frame(binding, turn_bound, text=text))
    return turn_bound, client_turn_id


def _rejected_frames(harness: _Harness) -> list[Any]:
    return [
        frame
        for frame in harness.sent_protocol_frames()
        if _frame_type(frame) == "voice_local_final_rejected"
    ]


def _playout_frame(
    binding: _RegisteredBinding,
    session: dict[str, Any],
    announcement: dict[str, Any],
    *,
    phase: str,
    client_sequence: int,
    clock: _Clock,
) -> VoiceLocalPlayoutEvent:
    return VoiceLocalPlayoutEvent(
        device_id=binding.claims.device_id,
        connection_generation=binding.connection_generation,
        session_id=session["session_id"],
        generation=session["generation"],
        speech_revision=_speech_revision(session),
        announcement_id=announcement.announcement_id,
        announcement_sequence=announcement.announcement_sequence,
        turn_id=announcement.turn_id,
        kind=announcement.kind,
        phase=phase,
        client_sequence=client_sequence,
        observed_at=clock.utcnow().isoformat().replace("+00:00", "Z"),
    )


@pytest.fixture
def local_harness() -> _Harness:
    return _Harness(VoiceSpeechBackend.CLIENT_LOCAL)


@pytest.fixture
def remote_harness() -> _Harness:
    return _Harness(VoiceSpeechBackend.LLM_FACTORY)


async def test_local_connection_replacement_fences_stale_socket_and_restores_capture(
    local_harness: _Harness,
) -> None:
    harness = local_harness
    wifi = harness.register_socket(CONNECTION_WIFI)
    created = await _create_local_session(harness, wifi)
    assert created.status_code == 201, created.text
    session = created.json()

    await harness.send_frame(
        wifi,
        _local_ready_frame(wifi, session, client_sequence=1),
    )
    ready = Message.from_json(harness.sent_frames.pop())
    assert ready.type == "voice_local_session_ready"
    assert ready.connection_generation == CONNECTION_WIFI

    cellular = harness.register_socket(CONNECTION_CELLULAR)
    stale_update = await _patch_session(
        harness,
        wifi,
        session,
        foreground_active=False,
        foreground_reason="connection_lost",
        microphone_enabled=False,
    )
    assert stale_update.status_code == 403
    assert stale_update.json()["code"] == "binding_not_current"

    await harness.send_frame(
        wifi,
        _local_ready_frame(wifi, session, client_sequence=2),
    )
    stale_replies = [
        frame
        for frame in harness.sent_protocol_frames()
        if _frame_type(frame) == "voice_local_session_ready"
    ]
    assert stale_replies == []
    assert harness.media.calls == []

    dropped = await _patch_session(
        harness,
        cellular,
        session,
        foreground_active=False,
        foreground_reason="connection_lost",
        microphone_enabled=False,
    )
    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["state"] == "suspended"
    assert harness.media.calls == [("capture", False)]
    assert (session["session_id"], session["generation"]) in (
        harness.services.announcement_suspended_sessions
    )

    restored = await _patch_session(
        harness,
        cellular,
        session,
        foreground_active=True,
        foreground_reason="foreground",
        microphone_enabled=True,
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "active"
    assert ("capture", True) in harness.media.calls

    await harness.send_frame(
        cellular,
        _local_ready_frame(cellular, session, client_sequence=1),
    )
    recovered = Message.from_json(harness.sent_frames.pop())
    assert recovered.type == "voice_local_session_ready"
    assert recovered.connection_generation == CONNECTION_CELLULAR
    record = harness.repository.sessions[(USER, session["session_id"])]
    assert record.state == "active"
    harness.assert_no_sensitive_material_durable()


async def test_local_background_disables_capture_and_refuses_final_until_foreground(
    local_harness: _Harness,
) -> None:
    harness = local_harness
    binding = harness.register_socket(CONNECTION_WIFI)
    created = await _create_local_session(harness, binding)
    session = created.json()

    backgrounded = await _patch_session(
        harness,
        binding,
        session,
        foreground_active=False,
        foreground_reason="backgrounded",
        microphone_enabled=False,
    )
    assert backgrounded.status_code == 200
    assert backgrounded.json()["microphone_enabled"] is False
    assert harness.media.calls == [("capture", False)]
    assert (session["session_id"], session["generation"]) in (
        harness.services.announcement_suspended_sessions
    )

    await harness.send_frame(
        binding,
        _local_ready_frame(binding, session, client_sequence=1),
    )
    assert [
        frame
        for frame in harness.sent_protocol_frames()
        if _frame_type(frame) == "voice_local_session_ready"
    ] == []
    assert harness.dispatcher.dispatched == []
    assert harness.repository.turns == {}

    restored = await _patch_session(
        harness,
        binding,
        session,
        foreground_active=True,
        foreground_reason="foreground",
        microphone_enabled=True,
    )
    assert restored.status_code == 200
    assert restored.json()["state"] == "active"
    assert ("capture", True) in harness.media.calls
    harness.sent_frames.clear()

    turn_bound, _client_turn = await _ready_and_recognize(
        harness,
        binding,
        session,
        client_sequence=2,
    )
    accepted = [
        turn
        for turn in harness.repository.turns.values()
        if turn.state == "accepted"
    ]
    assert len(accepted) == 1
    assert accepted[0].client_turn_id == turn_bound["client_turn_id"]
    assert len(harness.dispatcher.dispatched) == 1
    assert harness.dispatcher.dispatched[0]["chat_id"] == CHAT_HOME
    assert harness.dispatcher.dispatched[0]["text"] == TURN_TEXT
    harness.assert_no_sensitive_material_durable()


async def test_local_playout_interruption_stops_speech_and_refuses_stale_playout(
    local_harness: _Harness,
) -> None:
    harness = local_harness
    binding = harness.register_socket(CONNECTION_WIFI)
    created = await _create_local_session(harness, binding)
    session = created.json()
    turn_bound, _client_turn = await _ready_and_recognize(
        harness, binding, session
    )

    announcement = harness.services.local_announcements.issue(
        session=harness.repository.sessions[(USER, session["session_id"])],
        kind="greeting",
        turn_id=None,
        requested_text="Hello, voice is ready.",
        output_policy="lifecycle",
        mute_revision=1,
        consent_revision=1,
        now=harness.clock.utcnow(),
    )
    await harness.send_frame(
        binding,
        _playout_frame(
            binding,
            session,
            announcement,
            phase="started",
            client_sequence=1,
            clock=harness.clock,
        ),
    )
    coupled = await _patch_session(
        harness,
        binding,
        session,
        foreground_active=False,
        foreground_reason="audio_interrupted",
    )
    assert coupled.status_code == 400
    interrupted = await _patch_session(
        harness,
        binding,
        session,
        foreground_active=False,
        foreground_reason="audio_interrupted",
        microphone_enabled=False,
    )
    assert interrupted.status_code == 200
    assert interrupted.json()["state"] == "suspended"
    assert (session["session_id"], session["generation"]) in (
        harness.services.announcement_suspended_sessions
    )

    await harness.send_frame(
        binding,
        _playout_frame(
            binding,
            session,
            announcement,
            phase="finished",
            client_sequence=2,
            clock=harness.clock,
        ),
    )
    assert harness.repository.playout_records == []

    harness.sent_frames.clear()
    restored = await _patch_session(
        harness,
        binding,
        session,
        foreground_active=True,
        foreground_reason="foreground",
        microphone_enabled=True,
    )
    assert restored.json()["state"] == "active"
    assert ("capture", True) in harness.media.calls
    harness.sent_frames.clear()
    await harness.send_frame(
        binding,
        _local_ready_frame(binding, session, client_sequence=3),
    )
    ready_reply = Message.from_json(harness.sent_frames.pop())
    assert ready_reply.type == "voice_local_session_ready"
    harness.sent_frames.clear()
    replay_bound, _replay_turn = await _ready_and_recognize(
        harness,
        binding,
        session,
        client_sequence=4,
    )
    accepted = [
        turn for turn in harness.repository.turns.values() if turn.state == "accepted"
    ]
    assert len(accepted) == 2
    assert len({turn.client_turn_id for turn in accepted}) == 2
    accepted_texts = [
        call["text"] for call in harness.dispatcher.dispatched
    ]
    assert accepted_texts.count(TURN_TEXT) == 2
    harness.assert_no_sensitive_material_durable()


async def test_local_chat_change_during_reconnect_routes_turn_to_new_chat(
    local_harness: _Harness,
) -> None:
    harness = local_harness
    binding = harness.register_socket(CONNECTION_WIFI)
    created = await _create_local_session(harness, binding)
    session = created.json()

    switched = await _patch_session(
        harness,
        binding,
        session,
        visible_chat_id=CHAT_NEW,
        foreground_active=True,
        foreground_reason="foreground",
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["visible_chat_id"] == CHAT_NEW
    assert switched.json()["chat_context_synced"] is True
    session = switched.json()
    context_calls = [call for call in harness.media.calls if call[0] == "context"]
    assert context_calls == [("context", CHAT_NEW)]

    turn_bound, _client_turn = await _ready_and_recognize(
        harness, binding, session, chat_id=CHAT_NEW
    )
    assert harness.dispatcher.dispatched[0]["chat_id"] == CHAT_NEW
    turn = next(iter(harness.repository.turns.values()))
    assert turn.chat_id == CHAT_NEW

    stale_chat = await _patch_session(
        harness,
        binding,
        session,
        visible_chat_id=CHAT_NEW,
        expected_generation=session["generation"] + 3,
        expected_media_grant_revision=session["media_grant_revision"],
    )
    assert stale_chat.status_code == 409
    assert stale_chat.json()["code"] == "stale_generation"
    harness.assert_no_sensitive_material_durable()


async def test_local_scripted_rejections_after_recovery_preserve_bounded_state(
    local_harness: _Harness,
) -> None:
    harness = local_harness
    binding = harness.register_socket(CONNECTION_WIFI)
    created = await _create_local_session(harness, binding)
    session = created.json()
    _turn_bound, _client_turn = await _ready_and_recognize(
        harness, binding, session
    )
    assert len(harness.dispatcher.dispatched) == 1

    backgrounded = await _patch_session(
        harness,
        binding,
        session,
        foreground_active=False,
        foreground_reason="backgrounded",
        microphone_enabled=False,
    )
    assert backgrounded.status_code == 200
    restored = await _patch_session(
        harness,
        binding,
        session,
        foreground_active=True,
        foreground_reason="foreground",
        microphone_enabled=True,
    )
    assert restored.json()["state"] == "active"
    assert ("capture", True) in harness.media.calls
    harness.sent_frames.clear()

    harness.dispatcher.denials.append("permission_denied")
    recovered_bound, _recovered_turn = await _ready_and_recognize(
        harness,
        binding,
        session,
        client_sequence=3,
    )
    harness.dispatcher.denials.append("capacity_exhausted")
    _again_bound, _again_turn = await _ready_and_recognize(
        harness,
        binding,
        session,
        client_sequence=5,
    )
    rejections = _rejected_frames(harness)
    assert [frame.reason for frame in rejections] == [
        "invalid_binding",
        "capacity_exhausted",
    ]
    assert [frame.retry_policy for frame in rejections] == ["none", "none"]
    rejected_turns = [
        turn
        for turn in harness.repository.turns.values()
        if turn.state == "rejected"
    ]
    assert len(rejected_turns) == 2
    assert [turn.rejection_reason for turn in rejected_turns] == [
        "permission_denied",
        "capacity_exhausted",
    ]
    accepted_after_recovery = [
        call for call in harness.dispatcher.dispatched if not call["denied"]
    ]
    assert len(accepted_after_recovery) == 1
    denied_dispatches = [
        call for call in harness.dispatcher.dispatched if call["denied"]
    ]
    assert [call["turn_id"] for call in denied_dispatches] == [
        recovered_bound["turn_id"],
        _again_bound["turn_id"],
    ]
    harness.assert_no_sensitive_material_durable()


async def test_remote_connection_replacement_refresh_and_chat_change_recover(
    remote_harness: _Harness,
) -> None:
    harness = remote_harness
    wifi = harness.register_socket(CONNECTION_WIFI)
    created = await _create_remote_session(harness, wifi)
    assert created.status_code == 201, created.text
    payload = created.json()
    session = payload["session"]
    assert payload["grant"]["join_token"] == "trace-join-token-secret"

    stale_update = await _patch_session(
        harness,
        wifi,
        session,
        foreground_active=False,
        foreground_reason="connection_lost",
        microphone_enabled=False,
    )
    assert stale_update.status_code == 200
    assert stale_update.json()["state"] == "suspended"
    cellular = harness.register_socket(CONNECTION_CELLULAR)
    assert wifi.socket is not cellular.socket
    stale_update = await _patch_session(
        harness,
        wifi,
        stale_update.json(),
        foreground_active=True,
        foreground_reason="foreground",
        microphone_enabled=True,
    )
    assert stale_update.status_code == 403
    assert stale_update.json()["code"] == "binding_not_current"

    displaced_refresh = await _refresh_grant(harness, wifi, session)
    assert displaced_refresh.status_code == 403
    assert displaced_refresh.json()["code"] == "binding_not_current"

    refreshed = await _refresh_grant(harness, cellular, session)
    assert refreshed.status_code == 201, refreshed.text
    refreshed_payload = refreshed.json()
    assert refreshed_payload["replayed"] is False
    assert "join_token" not in json.dumps(
        await _grant_state(harness, cellular, refreshed_payload["session"])
    )
    session = refreshed_payload["session"]
    assert session["media_grant_revision"] == 2

    replayed = await _refresh_grant_with_id(
        harness,
        cellular,
        session,
        refresh_id=refreshed_payload["refresh_id"],
        expected_media_grant_revision=1,
    )
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True

    stale_revision = await _refresh_grant_with_id(
        harness,
        cellular,
        session,
        refresh_id=_uuid(40),
        expected_media_grant_revision=1,
    )
    assert stale_revision.status_code == 409
    assert stale_revision.json()["code"] == "stale_media_grant_revision"

    chat_switch = await _patch_session(
        harness,
        cellular,
        session,
        visible_chat_id=CHAT_NEW,
        foreground_active=True,
        foreground_reason="foreground",
    )
    assert chat_switch.status_code == 200
    assert chat_switch.json()["chat_context_synced"] is True
    context_calls = [call for call in harness.media.calls if call[0] == "context"]
    assert context_calls[-1] == ("context", CHAT_NEW)

    ended = await _end_remote_session(harness, cellular, session)
    assert ended.status_code == 204, ended.text
    record = harness.repository.sessions[(USER, session["session_id"])]
    assert record.state == "ended"
    assert record.end_reason == "user"
    assert harness.runtime._active_worker_assignments == {}
    assert [call for call in harness.media.calls if call[0] == "end"] == [
        ("end", "user")
    ]
    harness.assert_no_sensitive_material_durable()


async def test_expired_current_control_binding_refuses_refresh_and_replay_before_effects(
    remote_harness: _Harness,
) -> None:
    harness = remote_harness
    harness.clock.now = NOW
    binding = harness.register_socket(
        CONNECTION_WIFI, credential_expires_at=NOW + timedelta(seconds=10)
    )
    created = await _create_remote_session(harness, binding)
    assert created.status_code == 201, created.text
    session = created.json()["session"]
    calls_before = list(harness.repository.calls)
    media_before = list(harness.media.calls)
    record_before = harness.repository.sessions[(USER, session["session_id"])]
    assignments_before = dict(harness.runtime._active_worker_assignments)
    harness.clock.advance(11)

    assert harness._voice_device_bindings[(USER, DEVICE_A)] == id(binding.socket)
    assert harness._voice_control_bindings[id(binding.socket)] == binding.claims
    for _ in range(2):
        refused = await _refresh_grant(harness, binding, session)
        assert refused.status_code == 403
        assert refused.json()["code"] == "binding_expired"
    assert harness.repository.calls == calls_before
    assert harness.media.calls == media_before
    assert harness.repository.sessions[(USER, session["session_id"])] == record_before
    assert harness.runtime._active_worker_assignments == assignments_before
    assert harness.dispatcher.dispatched == []
    assert harness.dispatcher.denials == []

    renewed = harness.register_socket(CONNECTION_WIFI)
    assert renewed.claims.binding_id != binding.claims.binding_id
    ended = await _end_remote_session(harness, renewed, session)
    assert ended.status_code == 204, ended.text
    assert harness.repository.sessions[(USER, session["session_id"])].end_reason == "user"
    assert harness.runtime._active_worker_assignments == {}
    assert [call for call in harness.media.calls if call[0] == "end"] == [("end", "user")]
    harness.assert_no_sensitive_material_durable()


async def test_expired_media_grant_refresh_and_replay_keep_current_control_authority() -> None:
    harness = _Harness(VoiceSpeechBackend.LLM_FACTORY, media_grant_seconds=30)
    harness.clock.now = NOW
    binding = harness.register_socket(CONNECTION_WIFI)
    created = await _create_remote_session(harness, binding)
    assert created.status_code == 201, created.text
    session = created.json()["session"]
    harness.clock.advance(31)
    assert harness.clock.utcnow() < binding.claims.expires_at
    state = json.loads(await _grant_state(harness, binding, session))
    assert state["grant_state"]["status"] == "expired"

    refreshed = await _refresh_grant(harness, binding, session)
    assert refreshed.status_code == 201, refreshed.text
    assert refreshed.json()["replayed"] is False
    updated = refreshed.json()["session"]
    assert updated["media_grant_revision"] == 2
    active = json.loads(await _grant_state(harness, binding, updated))
    assert active["grant_state"]["status"] == "active"
    replayed = await _refresh_grant_with_id(
        harness, binding, updated,
        refresh_id=refreshed.json()["refresh_id"], expected_media_grant_revision=1,
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["replayed"] is True
    assert replayed.json()["session"]["media_grant_revision"] == 2
    assert harness.dispatcher.dispatched == []
    assert harness.dispatcher.denials == []
    ended = await _end_remote_session(harness, binding, updated)
    assert ended.status_code == 204, ended.text
    assert harness.runtime._active_worker_assignments == {}
    assert [call for call in harness.media.calls if call[0] == "end"] == [("end", "user")]
    harness.assert_no_sensitive_material_durable()


async def test_remote_playout_observed_stale_socket_refused_and_interruption_stops(
    remote_harness: _Harness,
) -> None:
    harness = remote_harness
    binding = harness.register_socket(CONNECTION_WIFI)
    created = await _create_remote_session(harness, binding)
    session = created.json()["session"]

    event = VoicePlayoutEvent(
        device_id=binding.claims.device_id,
        connection_generation=binding.connection_generation,
        session_id=session["session_id"],
        generation=session["generation"],
        media_grant_revision=session["media_grant_revision"],
        announcement_id=_uuid(50),
        announcement_sequence=1,
        turn_id=None,
        kind="greeting",
        quantum_role="single",
        quantum_index=0,
        phase="started",
        client_sequence=1,
        observed_at=harness.clock.utcnow().isoformat().replace("+00:00", "Z"),
    )
    assert (
        await harness.socket_orchestrator._handle_voice_playout_event(
            binding.socket, event
        )
        is True
    )
    assert len(harness.repository.playout_records) == 1
    assert harness.repository.playout_records[0]["phase"] == "started"

    stale_socket = harness.register_socket(CONNECTION_CELLULAR)
    stale_event = replace(
        event,
        connection_generation=CONNECTION_WIFI,
    )
    assert (
        await harness.socket_orchestrator._handle_voice_playout_event(
            binding.socket, stale_event
        )
        is False
    )
    assert (
        await harness.socket_orchestrator._handle_voice_playout_event(
            stale_socket.socket, event
        )
        is False
    )
    assert len(harness.repository.playout_records) == 1

    backgrounded = await _patch_session(
        harness,
        stale_socket,
        session,
        foreground_active=False,
        foreground_reason="backgrounded",
        microphone_enabled=False,
    )
    assert backgrounded.status_code == 200
    assert ("capture", False) in harness.media.calls
    assert (session["session_id"], session["generation"]) in (
        harness.services.announcement_suspended_sessions
    )

    finished = replace(event, phase="finished", client_sequence=2)
    assert (
        await harness.socket_orchestrator._handle_voice_playout_event(
            stale_socket.socket, finished
        )
        is False
    )
    assert len(harness.repository.playout_records) == 1
    harness.assert_no_sensitive_material_durable()


async def test_backend_posture_routes_are_mutually_exclusive(
    local_harness: _Harness,
    remote_harness: _Harness,
) -> None:
    local_binding = local_harness.register_socket(CONNECTION_WIFI)
    local_create = await _create_local_session(local_harness, local_binding)
    assert local_create.status_code == 201

    remote_api_on_local = await _create_remote_session(local_harness, local_binding)
    assert remote_api_on_local.status_code == 503
    assert (
        remote_api_on_local.json()["code"] == "client_contract_upgrade_required"
    )
    local_v1_status = await _status(local_harness, local_binding, version="v1")
    assert local_v1_status.status_code == 503

    remote_binding = remote_harness.register_socket(CONNECTION_WIFI)
    remote_create = await _create_remote_session(remote_harness, remote_binding)
    assert remote_create.status_code == 201
    local_api_on_remote = await _create_local_session(
        remote_harness, remote_binding
    )
    assert local_api_on_remote.status_code == 409
    assert local_api_on_remote.json()["reason"] == "backend_mismatch"

    unbound = await local_harness.client().post(
        "/api/voice/v2/sessions",
        headers={"X-Astral-Device-Id": local_binding.claims.device_id},
        json={
            "schema_version": "2",
            "activation_id": _uuid(25),
            "device_id": local_binding.claims.device_id,
            "device_kind": "ios",
            "visible_chat_id": CHAT_HOME,
            "foreground_active": True,
            "client_capability": _local_capability_payload(),
        },
    )
    assert unbound.status_code == 403
    assert unbound.json()["reason"] == "invalid_binding"


async def _refresh_grant(
    harness: _Harness,
    binding: _RegisteredBinding,
    session: dict[str, Any],
    *,
    refresh_id: str | None = None,
    expected_media_grant_revision: int | None = None,
) -> httpx.Response:
    return await _refresh_grant_with_id(
        harness,
        binding,
        session,
        refresh_id=refresh_id or _uuid(41),
        expected_media_grant_revision=expected_media_grant_revision,
    )


async def _refresh_grant_with_id(
    harness: _Harness,
    binding: _RegisteredBinding,
    session: dict[str, Any],
    *,
    refresh_id: str,
    expected_media_grant_revision: int | None = None,
) -> httpx.Response:
    async with harness.client() as client:
        return await client.post(
            f"/api/voice/sessions/{session['session_id']}/media-grants",
            headers=harness.headers(binding),
            json={
                "refresh_id": refresh_id,
                "expected_generation": session["generation"],
                "expected_media_grant_revision": (
                    expected_media_grant_revision
                    if expected_media_grant_revision is not None
                    else session["media_grant_revision"]
                ),
                "device_id": binding.claims.device_id,
            },
        )


async def _grant_state(
    harness: _Harness,
    binding: _RegisteredBinding,
    session: dict[str, Any],
) -> str:
    async with harness.client() as client:
        response = await client.get(
            f"/api/voice/sessions/{session['session_id']}/media-grants",
            headers=harness.headers(binding),
        )
    assert response.status_code == 200, response.text
    rendered = json.dumps(response.json())
    assert "join_token" not in rendered
    assert "participant_identity" not in rendered
    return rendered


async def _end_remote_session(
    harness: _Harness,
    binding: _RegisteredBinding,
    session: dict[str, Any],
) -> httpx.Response:
    async with harness.client() as client:
        return await client.delete(
            f"/api/voice/sessions/{session['session_id']}",
            headers=harness.headers(binding),
            params={
                "expected_generation": session["generation"],
                "expected_media_grant_revision": session["media_grant_revision"],
            },
        )


async def _status(
    harness: _Harness,
    binding: _RegisteredBinding,
    *,
    version: str,
) -> httpx.Response:
    path = "/api/voice/status" if version == "v1" else "/api/voice/v2/status"
    async with harness.client() as client:
        return await client.get(
            path,
            headers=harness.headers(binding),
        )
