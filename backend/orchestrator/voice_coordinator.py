"""Bounded coordinator and direct-RTC worker-pool authority for Feature 065.

The HTTP upgrade/challenge endpoint and PostgreSQL repository live at adjacent
integration seams.  This module owns the content-free, post-authentication
worker registry, deterministic assignment and frame fences, plus pure state
adapters that a row-locked repository can apply atomically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

MAX_CONTROL_FRAME_BYTES = 15 * 1024
MAX_RESULT_SAMPLES = 720_000
MAX_RESULT_QUANTA = 32
RESULT_OPENING_SAMPLES = 36_000
CONTINUATION_SAMPLES = 96_000
SINGLE_SAMPLES = 96_000
CADENCE_TARGET_SECONDS = 14.0
CADENCE_HARD_GAP_SECONDS = 20.0
ACKNOWLEDGEMENT_START_SECONDS = 1.5
HANDOFF_BUDGET_SECONDS = 0.25
MAX_CLIENT_PLAYOUT_FRAME_BYTES = 2 * 1024
MAX_ANNOUNCEMENT_MANIFEST_BYTES = 4 * 1024
MAX_CLIENT_PLAYOUT_EVENTS_PER_SECOND = 8
MAX_PENDING_TRANSCRIPTS = 4

FIXED_VOICE_PROFILE = MappingProxyType(
    {
        "asr_model": "Systran/faster-whisper-large-v3",
        "tts_model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
        "voice": "af_heart",
        "output_locale": "en-US",
        "format": "wav",
        "sample_rate_hz": 24_000,
    }
)

APPROVED_PHRASE_TEXT = MappingProxyType(
    {
        "hello_ready": "Hi! I'm ready when you are.",
        "on_it": "On it!",
        "working_on_it": "I'm on it.",
        "ill_get_started": "Let me take care of that.",
        "still_working": "I'm still working on that.",
        "making_progress": "I'm continuing with your request.",
        "keeping_at_it": "I'm keeping at it.",
        "action_needed": "I need something from you before I can continue.",
        "sign_in_needed": "Please sign in so I can continue.",
        "permission_needed": "Please grant the requested permission so I can continue.",
        "approval_needed": "Please review the approval request so I can continue.",
        "llm_setup_needed": (
            "Please set up your AI provider in Settings so I can continue."
        ),
        "sensitive_result_ready": "Your sensitive result is ready.",
        "request_failed": "I couldn't complete that request.",
        "request_refused": "I can't help with that request.",
        "request_busy": "I can't accept that request right now. Please try again.",
        "conversation_unavailable": (
            "That conversation is no longer available. Please choose one and try again."
        ),
        "request_retry_needed": "I couldn't accept that request. Please try again.",
        "request_not_understood": "I didn't understand that. Please try again.",
        "request_cancelled": "That request was cancelled.",
    }
)

APPROVED_PHRASE_KEYS = MappingProxyType(
    {
        "greeting": ("hello_ready",),
        "acknowledgement": ("on_it", "working_on_it", "ill_get_started"),
        "progress": ("still_working", "making_progress", "keeping_at_it"),
        "waiting": (
            "action_needed",
            "sign_in_needed",
            "permission_needed",
            "approval_needed",
        ),
        "sensitive_notice": ("sensitive_result_ready",),
        "failure": ("request_failed",),
        "refusal": ("request_refused",),
        "cancellation": ("request_cancelled",),
    }
)

# Pre-acceptance transcript dispositions never enter the accepted-turn cadence.
# Each reason is projected to one fixed, content-free instruction so user or
# model text cannot reach this TTS path. These keys are intentionally absent
# from APPROVED_PHRASE_KEYS: ordinary lifecycle selection must never pick a
# rejection-specific explanation at random.
PREACCEPTANCE_REJECTION_PHRASES = MappingProxyType(
    {
        "capacity_exhausted": ("refusal", "request_busy"),
        "chat_unavailable": ("refusal", "conversation_unavailable"),
        "invalid_binding": ("refusal", "request_retry_needed"),
        "invalid_proof": ("refusal", "request_retry_needed"),
        "proof_expired": ("refusal", "request_retry_needed"),
        "permission_denied": ("waiting", "llm_setup_needed"),
        "stale_session": ("refusal", "request_retry_needed"),
        "malformed_final": ("refusal", "request_not_understood"),
    }
)

_PHRASE_KIND_BY_KEY = MappingProxyType(
    {
        **{
            key: kind
            for kind, keys in APPROVED_PHRASE_KEYS.items()
            for key in keys
        },
        **{
            phrase_key: kind
            for kind, phrase_key in PREACCEPTANCE_REJECTION_PHRASES.values()
        },
    }
)
_LIFECYCLE_KIND = MappingProxyType(
    {
        "accepted": "acknowledgement",
        "processing": "progress",
        "waiting_on_user": "waiting",
        "succeeded": "result",
        "failed": "failure",
        "refused": "refusal",
        "cancelled": "cancellation",
    }
)
_WAITING_REASON_KEY = MappingProxyType(
    {
        "user_input": "action_needed",
        "login": "sign_in_needed",
        "permission": "permission_needed",
        "approval": "approval_needed",
    }
)

_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_PHRASE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_COORDINATOR_FRAME_TYPES = frozenset(
    {
        "media_grant_rotated",
        "session_context_update",
        "turn_bound",
        "transcript_accepted",
        "transcript_rejected",
        "speak",
        "stop_speech",
        "set_capture",
        "end_session",
    }
)
_WORKER_FRAME_TYPES = frozenset(
    {
        "pool_heartbeat",
        "media_grant_applied",
        "session_context_applied",
        "recognition_started",
        "recognition_failed",
        "worker_ready",
        "heartbeat",
        "speech_started",
        "speech_finished",
        "speech_interrupted",
        "speech_failed",
        "media_state",
        "transcript_emitted",
    }
)
_FORBIDDEN_WORKER_KEYS = frozenset(
    {
        "audio",
        "raw_audio",
        "text",
        "transcript",
        "join_token",
        "api_key",
        "api_secret",
        "secret",
        "credential",
    }
)
_ANNOUNCEMENT_KINDS = frozenset(
    {
        "greeting",
        "acknowledgement",
        "progress",
        "waiting",
        "result",
        "sensitive_notice",
        "failure",
        "refusal",
        "cancellation",
    }
)
_SHORT_TERMINAL_KINDS = frozenset(
    {"sensitive_notice", "failure", "refusal", "cancellation"}
)
_TRANSCRIPT_REJECTION_REASONS = frozenset(
    {
        "capacity_exhausted",
        "chat_unavailable",
        "invalid_binding",
        "invalid_proof",
        "proof_expired",
        "permission_denied",
        "stale_session",
        "malformed_final",
    }
)
_TRANSCRIPT_RETRY_POLICIES = frozenset({"explicit_user_retry", "none"})
_RECOGNITION_FAILURE_REASONS = frozenset(
    {"asr_failed", "empty_transcript", "invalid_asr_result", "self_speech"}
)


class VoiceCoordinatorError(RuntimeError):
    """A content-free error safe for logs, metrics, and problem mapping."""

    def __init__(self, code: str) -> None:
        if _REASON.fullmatch(code) is None:
            code = "voice_coordinator_error"
        self.code = code
        super().__init__(code)


class RegistrationError(VoiceCoordinatorError):
    """An authenticated worker registration was refused."""


class ControlProtocolError(VoiceCoordinatorError):
    """A control frame failed direction, structure, or fence validation."""


class ControlSendError(VoiceCoordinatorError):
    """A bounded control send failed with no content in the exception."""


class CapacityUnavailable(VoiceCoordinatorError):
    """No exact-profile worker capacity can be reserved immediately."""


class StaleFence(VoiceCoordinatorError):
    """A connection, generation, assignment, or revision is stale."""


class ClaimUnavailable(VoiceCoordinatorError):
    """A durable coordinator lease or announcement claim cannot be acquired."""


class WorkerSocket(Protocol):
    async def send(self, payload: str) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerPoolPolicy:
    """Validated deployment bounds and one approved runtime closure."""

    runtime_closure_sha256: str
    max_workers: int = 32
    max_sessions_per_worker: int = 100
    max_total_sessions: int = 1_000
    heartbeat_interval_seconds: int = 10
    connection_lease_seconds: int = 35
    send_timeout_seconds: float = 5.0
    max_receive_frames: int = 120
    receive_window_seconds: float = 1.0
    max_pending_session_sends: int = 16
    allow_unapproved_development_closure: bool = False
    allow_insecure_livekit_url: bool = False

    def __post_init__(self) -> None:
        digest = self.runtime_closure_sha256
        if _SHA256.fullmatch(digest) is None or (
            digest == "0" * 64 and not self.allow_unapproved_development_closure
        ):
            raise ValueError("invalid_closure_digest")
        if not 1 <= self.max_workers <= 1_000:
            raise ValueError("invalid_max_workers")
        if not 1 <= self.max_sessions_per_worker <= 100:
            raise ValueError("invalid_worker_capacity")
        if not 1 <= self.max_total_sessions <= 100_000:
            raise ValueError("invalid_total_capacity")
        if not 5 <= self.heartbeat_interval_seconds <= 60:
            raise ValueError("invalid_heartbeat_interval")
        if not (
            self.connection_lease_seconds > self.heartbeat_interval_seconds
            and self.connection_lease_seconds <= 300
        ):
            raise ValueError("invalid_connection_lease")
        if not math.isfinite(self.send_timeout_seconds) or not (
            0.01 <= self.send_timeout_seconds <= 30
        ):
            raise ValueError("invalid_send_timeout")
        if not 1 <= self.max_receive_frames <= 10_000:
            raise ValueError("invalid_frame_rate")
        if not math.isfinite(self.receive_window_seconds) or not (
            0.1 <= self.receive_window_seconds <= 60
        ):
            raise ValueError("invalid_frame_window")
        if not 1 <= self.max_pending_session_sends <= 64:
            raise ValueError("invalid_send_queue_bound")
        if not isinstance(self.allow_insecure_livekit_url, bool):
            raise ValueError("invalid_insecure_livekit_policy")


class CoordinatorClock:
    """Strict UTC/monotonic clock adapter used by state and transport code."""

    def __init__(
        self,
        *,
        utcnow: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or __import__("time").monotonic
        self._last_monotonic: float | None = None

    def utcnow(self) -> datetime:
        value = self._utcnow()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("utc_clock_must_be_timezone_aware")
        if value.utcoffset() is None:
            raise RuntimeError("utc_clock_must_be_timezone_aware")
        return value.astimezone(UTC)

    def monotonic(self) -> float:
        value = self._monotonic()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("invalid_monotonic_clock")
        result = float(value)
        if not math.isfinite(result):
            raise RuntimeError("invalid_monotonic_clock")
        if self._last_monotonic is not None and result < self._last_monotonic:
            raise RuntimeError("monotonic_clock_regressed")
        self._last_monotonic = result
        return result


@dataclass(frozen=True, slots=True)
class MonotonicDeadline:
    """One in-process deadline plus its conservative UTC recovery marker."""

    due_monotonic: float
    recovery_due_at: datetime


class MonotonicScheduler:
    """Convert bounded cadence delays to monotonic deadlines and recover them."""

    def __init__(
        self, clock: CoordinatorClock, *, max_delay_seconds: float = 300
    ) -> None:
        if not math.isfinite(max_delay_seconds) or not 1 <= max_delay_seconds <= 3_600:
            raise ValueError("invalid_max_schedule_delay")
        self._clock = clock
        self._max_delay = float(max_delay_seconds)

    def schedule_after(self, delay_seconds: float) -> MonotonicDeadline:
        delay = self._delay(delay_seconds)
        now_utc = self._clock.utcnow()
        now_mono = self._clock.monotonic()
        return MonotonicDeadline(
            due_monotonic=now_mono + delay,
            recovery_due_at=now_utc + timedelta(seconds=delay),
        )

    def recover(self, recovery_due_at: datetime) -> MonotonicDeadline:
        due = _aware(recovery_due_at, "invalid_recovery_due_at")
        now_utc = self._clock.utcnow()
        remaining = max(0.0, (due - now_utc).total_seconds())
        delay = self._delay(remaining)
        return MonotonicDeadline(
            due_monotonic=self._clock.monotonic() + delay,
            recovery_due_at=due,
        )

    def remaining(self, deadline: MonotonicDeadline) -> float:
        if not isinstance(deadline, MonotonicDeadline):
            raise ValueError("invalid_monotonic_deadline")
        return max(0.0, deadline.due_monotonic - self._clock.monotonic())

    def is_due(self, deadline: MonotonicDeadline) -> bool:
        return self.remaining(deadline) == 0.0

    def _delay(self, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid_schedule_delay")
        delay = float(value)
        if not math.isfinite(delay) or not 0 <= delay <= self._max_delay:
            raise ValueError("invalid_schedule_delay")
        return delay


@dataclass(frozen=True, slots=True)
class SessionBindRequest:
    """Credential-free durable fields required to bind one worker session."""

    session_id: str
    generation: int
    room_name: str
    transport: str
    media_grant_revision: int
    worker_rtc_grant_revision: int
    client_participant_identity: str
    visible_chat_id: str
    chat_context_revision: int

    def __post_init__(self) -> None:
        _uuid4(self.session_id, "invalid_session_id")
        _positive(self.generation, "invalid_generation")
        _opaque(self.room_name, "invalid_room_name")
        if self.transport not in {"livekit", "watch_pcm_websocket"}:
            raise ValueError("invalid_transport")
        _positive(self.media_grant_revision, "invalid_media_grant_revision")
        _positive(
            self.worker_rtc_grant_revision,
            "invalid_worker_rtc_grant_revision",
        )
        _opaque(
            self.client_participant_identity,
            "invalid_client_participant_identity",
        )
        _uuid4(self.visible_chat_id, "invalid_visible_chat_id")
        _positive(self.chat_context_revision, "invalid_chat_context_revision")


@dataclass(frozen=True, slots=True)
class SessionReservation:
    session_id: str
    generation: int
    assignment_id: str
    worker_identity: str
    connection_id: str
    worker_rtc_grant_revision: int


@dataclass(frozen=True, slots=True)
class RecognitionStart:
    """Content-free recognition binding accepted from one assigned worker."""

    session_id: str
    generation: int
    assignment_id: str
    worker_identity: str
    client_turn_id: str
    media_grant_revision: int
    chat_id: str
    chat_context_revision: int

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "assignment_id",
            "client_turn_id",
            "chat_id",
        ):
            _uuid4(getattr(self, name), f"invalid_{name}")
        _positive(self.generation, "invalid_generation")
        _positive(
            self.media_grant_revision,
            "invalid_media_grant_revision",
        )
        _positive(
            self.chat_context_revision,
            "invalid_chat_context_revision",
        )
        _opaque(self.worker_identity, "invalid_worker_identity")


@dataclass(frozen=True, slots=True)
class TranscriptTurnBinding:
    """Immutable identifiers returned by durable recognition allocation."""

    session_id: str
    generation: int
    media_grant_revision: int
    assignment_id: str
    worker_identity: str
    turn_id: str
    client_turn_id: str
    submission_id: str
    request_generation: str
    chat_id: str
    chat_context_revision: int

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "assignment_id",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
        ):
            _uuid4(getattr(self, name), f"invalid_{name}")
        for name in (
            "generation",
            "media_grant_revision",
            "chat_context_revision",
        ):
            _positive(getattr(self, name), f"invalid_{name}")
        _opaque(self.worker_identity, "invalid_worker_identity")

    @classmethod
    def from_turn(
        cls,
        turn: Any,
        *,
        assignment_id: str,
        worker_identity: str,
    ) -> TranscriptTurnBinding:
        """Copy only immutable, content-free fields from a repository row."""

        try:
            return cls(
                session_id=turn.session_id,
                generation=turn.session_generation,
                media_grant_revision=turn.media_grant_revision,
                assignment_id=assignment_id,
                worker_identity=worker_identity,
                turn_id=turn.turn_id,
                client_turn_id=turn.client_turn_id,
                submission_id=turn.submission_id,
                request_generation=turn.request_generation,
                chat_id=turn.chat_id,
                chat_context_revision=turn.chat_context_revision,
            )
        except (AttributeError, TypeError, ValueError):
            raise ClaimUnavailable("invalid_repository_recognition_binding") from None


@dataclass(frozen=True, slots=True)
class WorkerRegistrationReceipt:
    connection_id: str
    worker_identity: str
    accepted_max_sessions: int
    fenced_assignments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerConnectionRelease:
    """Credential-free exact assignments fenced by one worker lease expiry."""

    connection_id: str
    worker_identity: str
    accepted_max_sessions: int
    assignments: tuple[SessionReservation, ...]

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(item.session_id for item in self.assignments)

    @property
    def assignment_ids(self) -> tuple[str, ...]:
        return tuple(item.assignment_id for item in self.assignments)


@dataclass(frozen=True, slots=True)
class WorkerTerminalRelease:
    """Exact credential-free assignment released after terminal worker state."""

    reservation: SessionReservation
    terminal_state: str
    accepted_max_sessions: int

    @property
    def connection_id(self) -> str:
        return self.reservation.connection_id

    @property
    def session_id(self) -> str:
        return self.reservation.session_id

    @property
    def generation(self) -> int:
        return self.reservation.generation

    @property
    def assignment_id(self) -> str:
        return self.reservation.assignment_id

    @property
    def worker_identity(self) -> str:
        return self.reservation.worker_identity

    @property
    def worker_rtc_grant_revision(self) -> int:
        return self.reservation.worker_rtc_grant_revision


@dataclass(frozen=True, slots=True)
class AssignmentSnapshot:
    session_id: str
    generation: int
    assignment_id: str
    worker_identity: str
    connection_id: str
    worker_rtc_grant_revision: int
    ready: bool
    media_state: str
    next_outgoing_sequence: int
    next_incoming_sequence: int
    applied_visible_chat_id: str | None = None
    applied_chat_context_revision: int | None = None
    applied_media_refresh_id: str | None = None
    applied_media_grant_revision: int | None = None


@dataclass(frozen=True, slots=True)
class WorkerPoolReadiness:
    ready: bool
    reason: str
    worker_count: int
    capacity_total: int
    capacity_available: int
    profile: Mapping[str, str | int] = field(
        default_factory=lambda: FIXED_VOICE_PROFILE
    )


@dataclass(slots=True)
class _WorkerConnection:
    connection_id: str
    worker_identity: str
    capacity: int
    socket: WorkerSocket
    registered_at: datetime
    last_seen_monotonic: float
    next_incoming_pool_sequence: int = 1
    assignments: set[str] = field(default_factory=set)
    inbound_pool_times: deque[float] = field(default_factory=deque)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class _BoundedSendGate:
    """Serialize one session while bounding tasks waiting for its socket."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._pending = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> None:
        if self._pending >= self._limit:
            raise ControlSendError("send_queue_full")
        self._pending += 1
        try:
            await self._lock.acquire()
        except BaseException:
            self._pending -= 1
            raise

    async def __aexit__(self, *_error: object) -> None:
        self._lock.release()
        self._pending -= 1


@dataclass(slots=True)
class _WorkerAssignment:
    request: SessionBindRequest
    assignment_id: str
    worker_identity: str
    connection_id: str
    assigned_at: datetime
    expected_visible_chat_id: str
    expected_chat_context_revision: int
    next_outgoing_sequence: int = 0
    next_incoming_sequence: int = 0
    ready: bool = False
    media_state: str = "connecting"
    applied_visible_chat_id: str | None = None
    applied_chat_context_revision: int | None = None
    pending_media_refresh_id: str | None = None
    pending_media_grant_revision: int | None = None
    pending_client_participant_identity: str | None = None
    pending_media_rotation: tuple[Any, ...] | None = None
    applied_media_refresh_id: str | None = None
    applied_media_grant_revision: int | None = None
    recognitions: dict[str, RecognitionStart] = field(default_factory=dict)
    turn_bindings: dict[str, TranscriptTurnBinding] = field(default_factory=dict)
    state_changed: asyncio.Event = field(default_factory=asyncio.Event)
    inbound_times: deque[float] = field(default_factory=deque)
    send_gate: _BoundedSendGate = field(default_factory=lambda: _BoundedSendGate(16))


class WorkerPool:
    """Concurrency-safe exact-profile registry and session frame authority."""

    def __init__(
        self,
        policy: WorkerPoolPolicy,
        *,
        utcnow: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        uuid_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._policy = policy
        self._clock = CoordinatorClock(utcnow=utcnow, monotonic=monotonic)
        self._uuid_factory = uuid_factory or uuid4
        self._lock = asyncio.Lock()
        self._registration_lock = asyncio.Lock()
        self._connections: dict[str, _WorkerConnection] = {}
        self._identity_connections: dict[str, str] = {}
        self._assignments: dict[str, _WorkerAssignment] = {}
        self._replacing_identities: set[str] = set()
        self._closed = False
        self._shutdown_task: asyncio.Task[tuple[str, ...]] | None = None

    async def register_worker(
        self,
        frame: Mapping[str, Any],
        socket: WorkerSocket,
        *,
        authenticated_identity: str,
    ) -> WorkerRegistrationReceipt:
        """Register only an already challenge-authenticated exact worker."""

        identity, requested_capacity = self._parse_registration(
            frame, authenticated_identity=authenticated_identity
        )
        accepted_capacity = min(
            requested_capacity, self._policy.max_sessions_per_worker
        )
        async with self._registration_lock:
            async with self._lock:
                if self._closed:
                    raise RegistrationError("worker_pool_closed")
                replacing = identity in self._identity_connections
                if not replacing and len(self._connections) >= self._policy.max_workers:
                    raise RegistrationError("worker_registry_full")
                self._replacing_identities.add(identity)
            old_connection: _WorkerConnection | None = None
            fenced: list[str] = []
            committed = False
            try:
                connection_id = self._new_uuid4("connection_id")
                registered_at = self._clock.utcnow()
                response = {
                    "type": "worker_registered",
                    "schema_version": "1",
                    "message_id": self._new_uuid4("message_id"),
                    "sequence": 0,
                    "sent_at": _timestamp(registered_at),
                    "worker_identity": identity,
                    "connection_id": connection_id,
                    "accepted_max_sessions": accepted_capacity,
                    "heartbeat_interval_seconds": self._policy.heartbeat_interval_seconds,
                    "registered_at": _timestamp(registered_at),
                }
                temporary_lock = asyncio.Lock()
                await self._send_payload(socket, temporary_lock, response)
                new_connection = _WorkerConnection(
                    connection_id=connection_id,
                    worker_identity=identity,
                    capacity=accepted_capacity,
                    socket=socket,
                    registered_at=registered_at,
                    last_seen_monotonic=self._clock.monotonic(),
                )
                async with self._lock:
                    old_id = self._identity_connections.get(identity)
                    if old_id is not None:
                        old_connection = self._connections.pop(old_id, None)
                        if old_connection is not None:
                            fenced.extend(
                                self._remove_connection_assignments_locked(
                                    old_connection
                                )
                            )
                    self._connections[connection_id] = new_connection
                    self._identity_connections[identity] = connection_id
                    self._replacing_identities.discard(identity)
                    committed = True
            except BaseException:
                if not committed:
                    await asyncio.shield(self._abort_registration(identity, socket))
                raise
            if old_connection is not None:
                await asyncio.shield(
                    _safe_close(old_connection.socket, 4001, "connection_replaced")
                )
            return WorkerRegistrationReceipt(
                connection_id=connection_id,
                worker_identity=identity,
                accepted_max_sessions=accepted_capacity,
                fenced_assignments=tuple(sorted(fenced)),
            )

    async def _abort_registration(self, identity: str, socket: WorkerSocket) -> None:
        async with self._lock:
            self._replacing_identities.discard(identity)
        await _safe_close(socket, 1011, "registration_failed")

    async def reserve_session(self, request: SessionBindRequest) -> SessionReservation:
        """Atomically reserve a deterministic capacity slot, never a queue."""

        now_mono = self._clock.monotonic()
        now_utc = self._clock.utcnow()
        async with self._lock:
            if self._closed:
                raise CapacityUnavailable("worker_pool_closed")
            current = self._assignments.get(request.session_id)
            if current is not None:
                if request.generation < current.request.generation:
                    raise StaleFence("stale_generation")
                if request.generation == current.request.generation:
                    self._validate_idempotent_request(current.request, request)
                    if (
                        request.worker_rtc_grant_revision
                        < current.request.worker_rtc_grant_revision
                    ):
                        raise StaleFence("stale_worker_grant_revision")
                    if (
                        request.worker_rtc_grant_revision
                        > current.request.worker_rtc_grant_revision
                    ):
                        current.request = request
                        current.ready = False
                        current.media_state = "reconnecting"
                        current.expected_visible_chat_id = request.visible_chat_id
                        current.expected_chat_context_revision = (
                            request.chat_context_revision
                        )
                        current.applied_visible_chat_id = None
                        current.applied_chat_context_revision = None
                        current.pending_media_refresh_id = None
                        current.pending_media_grant_revision = None
                        current.pending_client_participant_identity = None
                        current.pending_media_rotation = None
                        current.applied_media_refresh_id = None
                        current.applied_media_grant_revision = None
                        self._signal_assignment(current)
                    return self._reservation(current)
                self._remove_assignment_locked(current)

            if len(self._assignments) >= self._policy.max_total_sessions:
                raise CapacityUnavailable("deployment_capacity_exhausted")
            candidates = [
                connection
                for connection in self._connections.values()
                if connection.worker_identity not in self._replacing_identities
                and self._connection_live(connection, now_mono)
                and len(connection.assignments) < connection.capacity
            ]
            if not candidates:
                raise CapacityUnavailable("worker_capacity_exhausted")
            connection = min(
                candidates,
                key=lambda item: (
                    Fraction(len(item.assignments), item.capacity),
                    item.worker_identity,
                    item.connection_id,
                ),
            )
            assignment_id = deterministic_uuid4(
                "voice-worker-assignment-v1",
                request.session_id,
                str(request.generation),
                connection.worker_identity,
            )
            assignment = _WorkerAssignment(
                request=request,
                assignment_id=assignment_id,
                worker_identity=connection.worker_identity,
                connection_id=connection.connection_id,
                # Worker-grant timestamps use the protocol/JWT NumericDate
                # precision of whole seconds. Retain the assignment fence at
                # that same precision so a grant minted later in the same
                # second cannot be misclassified as predating its assignment.
                assigned_at=now_utc.replace(microsecond=0),
                expected_visible_chat_id=request.visible_chat_id,
                expected_chat_context_revision=request.chat_context_revision,
                send_gate=_BoundedSendGate(self._policy.max_pending_session_sends),
            )
            self._assignments[request.session_id] = assignment
            connection.assignments.add(request.session_id)
            return self._reservation(assignment)

    async def deliver_session_bind(
        self,
        reservation: SessionReservation,
        request: SessionBindRequest,
        worker_rtc_grant: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate and deliver one memory-only worker grant inside a bind."""

        assignment = await self._current_assignment(reservation, request)
        self._validate_assignment_command_state(assignment, "session_bind")
        async with assignment.send_gate:
            assignment = await self._current_assignment(reservation, request)
            self._validate_assignment_command_state(assignment, "session_bind")
            grant = self._validate_worker_grant(assignment, request, worker_rtc_grant)
            async with self._lock:
                connection = self._current_connection_locked(assignment.connection_id)
                sequence = assignment.next_outgoing_sequence
            frame = {
                "type": "session_bind",
                "schema_version": "1",
                "message_id": deterministic_uuid4(
                    "voice-session-bind-v1",
                    assignment.assignment_id,
                    str(sequence),
                    str(request.worker_rtc_grant_revision),
                ),
                "session_id": request.session_id,
                "generation": request.generation,
                "sequence": sequence,
                "sent_at": _timestamp(self._clock.utcnow()),
                "assignment_id": assignment.assignment_id,
                "room_name": request.room_name,
                "worker_identity": assignment.worker_identity,
                "transport": request.transport,
                "media_grant_revision": request.media_grant_revision,
                "worker_rtc_grant_revision": request.worker_rtc_grant_revision,
                "client_participant_identity": request.client_participant_identity,
                "grant_expires_at": grant["expires_at"],
                "worker_rtc_grant": grant,
                "visible_chat_id": request.visible_chat_id,
                "chat_context_revision": request.chat_context_revision,
                "profile": dict(FIXED_VOICE_PROFILE),
            }
            _encode_frame(frame)
            sequence_committed = False
            try:
                async with self._lock:
                    current = self._assignments.get(request.session_id)
                    if current is not assignment:
                        raise StaleFence("stale_assignment")
                    if (
                        self._connections.get(connection.connection_id)
                        is not connection
                    ):
                        raise StaleFence("stale_connection")
                    if assignment.next_outgoing_sequence != sequence:
                        raise StaleFence("stale_outgoing_sequence")
                    self._validate_assignment_command_state(
                        assignment,
                        "session_bind",
                    )
                    assignment.next_outgoing_sequence += 1
                    sequence_committed = True
                await self._send_payload(connection.socket, connection.send_lock, frame)
            except asyncio.CancelledError:
                if sequence_committed:
                    await asyncio.shield(
                        self._fence_connection(
                            connection.connection_id, 1011, "send_cancelled"
                        )
                    )
                raise
            except ControlSendError:
                await self._fence_connection(
                    connection.connection_id, 1011, "send_failed"
                )
                raise
            return frame

    async def send_session_command(
        self,
        reservation: SessionReservation,
        frame_type: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Send one bounded coordinator-direction frame under session fences."""

        if frame_type not in _COORDINATOR_FRAME_TYPES:
            raise ControlProtocolError("wrong_direction")
        protected = {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
        }
        if not isinstance(fields, Mapping) or protected.intersection(fields):
            raise ControlProtocolError("protected_frame_field")
        _reject_forbidden_coordinator_content(fields)
        assignment = await self._current_assignment(reservation)
        async with assignment.send_gate:
            assignment = await self._current_assignment(reservation)
            self._validate_tracked_command(assignment, frame_type, fields)
            async with self._lock:
                connection = self._current_connection_locked(assignment.connection_id)
                sequence = assignment.next_outgoing_sequence
            frame = {
                "type": frame_type,
                "schema_version": "1",
                "message_id": deterministic_uuid4(
                    "voice-control-command-v1",
                    assignment.assignment_id,
                    str(sequence),
                    frame_type,
                ),
                "session_id": assignment.request.session_id,
                "generation": assignment.request.generation,
                "sequence": sequence,
                "sent_at": _timestamp(self._clock.utcnow()),
                **dict(fields),
            }
            _encode_frame(frame)
            sequence_committed = False
            try:
                async with self._lock:
                    current = self._assignments.get(assignment.request.session_id)
                    if current is not assignment:
                        raise StaleFence("stale_assignment")
                    if (
                        self._connections.get(connection.connection_id)
                        is not connection
                    ):
                        raise StaleFence("stale_connection")
                    if assignment.next_outgoing_sequence != sequence:
                        raise StaleFence("stale_outgoing_sequence")
                    self._validate_assignment_command_state(assignment, frame_type)
                    assignment.next_outgoing_sequence += 1
                    self._commit_tracked_command(assignment, frame_type, fields)
                    sequence_committed = True
                await self._send_payload(connection.socket, connection.send_lock, frame)
            except asyncio.CancelledError:
                if sequence_committed:
                    await asyncio.shield(
                        self._fence_connection(
                            connection.connection_id, 1011, "send_cancelled"
                        )
                    )
                raise
            except ControlSendError:
                await self._fence_connection(
                    connection.connection_id, 1011, "send_failed"
                )
                raise
            return frame

    async def receive_worker_frame(
        self, connection_id: str, payload: str | bytes
    ) -> dict[str, Any]:
        """Authorize one worker-direction frame before any downstream effect."""

        _uuid4(connection_id, "invalid_connection_id", ControlProtocolError)
        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                raise StaleFence("stale_connection")
        frame = _decode_frame(payload)
        frame_type = frame.get("type")
        if frame_type in _COORDINATOR_FRAME_TYPES or frame_type in {
            "worker_registered",
            "session_bind",
        }:
            raise ControlProtocolError("wrong_direction")
        if frame_type not in _WORKER_FRAME_TYPES:
            raise ControlProtocolError("unknown_frame_type")
        _reject_forbidden_worker_content(frame)
        if frame_type == "pool_heartbeat":
            _validate_worker_pool_base(frame)
            self._validate_pool_heartbeat(frame)
            now_mono = self._clock.monotonic()
            async with self._lock:
                if self._connections.get(connection_id) is not connection:
                    raise StaleFence("stale_connection")
                if frame["connection_id"] != connection.connection_id:
                    raise StaleFence("stale_connection")
                if frame["worker_identity"] != connection.worker_identity:
                    raise StaleFence("worker_identity_mismatch")
                self._check_connection_receive_rate_locked(connection, now_mono)
                if frame["sequence"] != connection.next_incoming_pool_sequence:
                    raise ControlProtocolError("sequence_out_of_order")
                connection.next_incoming_pool_sequence += 1
                connection.last_seen_monotonic = now_mono
            return frame
        _validate_worker_base(frame)

        now_mono = self._clock.monotonic()
        async with self._lock:
            if self._connections.get(connection_id) is not connection:
                raise StaleFence("stale_connection")
            assignment = self._assignments.get(frame["session_id"])
            if assignment is None or assignment.connection_id != connection_id:
                raise StaleFence("stale_assignment")
            if frame["generation"] != assignment.request.generation:
                raise StaleFence("stale_generation")
            self._check_receive_rate_locked(assignment, now_mono)
            if frame["sequence"] != assignment.next_incoming_sequence:
                raise ControlProtocolError("sequence_out_of_order")
            if frame_type == "worker_ready":
                self._validate_worker_ready(frame, assignment)
            elif frame_type == "heartbeat":
                self._validate_heartbeat(frame)
            elif frame_type == "session_context_applied":
                self._validate_session_context_applied(frame, assignment)
            elif frame_type == "media_grant_applied":
                self._validate_media_grant_applied(frame, assignment)
            elif frame_type == "recognition_started":
                self._validate_recognition_started(frame, assignment)
            elif frame_type == "recognition_failed":
                self._validate_recognition_failed(frame, assignment)
            elif frame_type == "media_state":
                self._validate_media_state(frame)
            assignment.next_incoming_sequence += 1
            connection.last_seen_monotonic = now_mono
            terminal_before = assignment.media_state in {"failed", "ended"}
            if frame_type == "worker_ready" and not terminal_before:
                assignment.ready = bool(frame["profile_ready"])
                assignment.media_state = "ready" if assignment.ready else "failed"
            elif frame_type == "heartbeat":
                if not terminal_before:
                    assignment.media_state = frame["media_state"]
                elif frame["media_state"] == "ended":
                    assignment.media_state = "ended"
                if terminal_before or frame["media_state"] in {"failed", "ended"}:
                    assignment.ready = False
            elif frame_type == "media_state" and frame["state"] in {
                "failed",
                "ended",
            }:
                assignment.media_state = frame["state"]
                assignment.ready = False
            elif frame_type == "session_context_applied":
                assignment.applied_visible_chat_id = frame["visible_chat_id"]
                assignment.applied_chat_context_revision = frame[
                    "chat_context_revision"
                ]
            elif frame_type == "media_grant_applied":
                assignment.applied_media_refresh_id = frame["refresh_id"]
                assignment.applied_media_grant_revision = frame["media_grant_revision"]
            elif frame_type == "recognition_started":
                client_turn_id = frame["client_turn_id"]
                assignment.recognitions.setdefault(
                    client_turn_id,
                    RecognitionStart(
                        session_id=assignment.request.session_id,
                        generation=assignment.request.generation,
                        assignment_id=assignment.assignment_id,
                        worker_identity=assignment.worker_identity,
                        client_turn_id=client_turn_id,
                        media_grant_revision=frame["media_grant_revision"],
                        chat_id=frame["visible_chat_id"],
                        chat_context_revision=frame["chat_context_revision"],
                    ),
                )
            if frame_type in {
                "worker_ready",
                "heartbeat",
                "media_state",
                "session_context_applied",
                "media_grant_applied",
            }:
                self._signal_assignment(assignment)
            return frame

    async def attributable_worker_frame_reservation(
        self,
        connection_id: str,
        payload: str | bytes,
    ) -> tuple[SessionReservation, int] | None:
        """Resolve only an exact session-local rejected worker frame.

        This is deliberately a second strict decode after
        :meth:`receive_worker_frame` rejects a payload.  Undecodable envelopes,
        forbidden content, pool-global frames, bad base identities, and stale
        connection/session/generation/assignment fences return ``None`` so the
        endpoint retains its connection-fatal behavior.
        """

        try:
            frame = _decode_frame(payload)
            frame_type = frame.get("type")
            if frame_type not in _WORKER_FRAME_TYPES or frame_type == "pool_heartbeat":
                return None
            _reject_forbidden_worker_content(frame)
            _validate_worker_base(frame)
        except (ControlProtocolError, StaleFence):
            return None
        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                return None
            assignment = self._assignments.get(frame["session_id"])
            if (
                assignment is None
                or assignment.connection_id != connection_id
                or assignment.request.generation != frame["generation"]
            ):
                return None
            if (
                "assignment_id" in frame
                and frame["assignment_id"] != assignment.assignment_id
            ):
                return None
            if (
                "worker_identity" in frame
                and frame["worker_identity"] != assignment.worker_identity
            ):
                return None
            return (
                self._reservation(assignment),
                assignment.request.media_grant_revision,
            )

    async def release_attributable_worker_frame_reservation(
        self,
        reservation: SessionReservation,
    ) -> bool:
        """Release only the unchanged assignment classified for quarantine."""

        async with self._lock:
            assignment = self._assignments.get(reservation.session_id)
            if assignment is None:
                return False
            if (
                assignment.request.generation != reservation.generation
                or assignment.assignment_id != reservation.assignment_id
                or assignment.worker_identity != reservation.worker_identity
                or assignment.connection_id != reservation.connection_id
                or assignment.request.worker_rtc_grant_revision
                != reservation.worker_rtc_grant_revision
                or self._connections.get(assignment.connection_id) is None
            ):
                return False
            self._remove_assignment_locked(assignment)
            return True

    async def await_session_ready(
        self,
        *,
        session_id: str,
        generation: int,
        visible_chat_id: str,
        chat_context_revision: int,
        timeout_seconds: float = 10,
    ) -> AssignmentSnapshot:
        """Wait boundedly for profile readiness and the exact applied chat fence."""

        _uuid4(session_id, "invalid_session_id", ControlProtocolError)
        _positive(generation, "invalid_generation", ControlProtocolError)
        _uuid4(visible_chat_id, "invalid_visible_chat_id", ControlProtocolError)
        _positive(
            chat_context_revision,
            "invalid_chat_context_revision",
            ControlProtocolError,
        )
        return await self._await_assignment_state(
            session_id=session_id,
            generation=generation,
            timeout_seconds=timeout_seconds,
            timeout_code="worker_session_ready_timeout",
            predicate=lambda assignment: (
                assignment.ready
                and assignment.media_state == "ready"
                and assignment.expected_visible_chat_id == visible_chat_id
                and assignment.expected_chat_context_revision == chat_context_revision
                and assignment.applied_visible_chat_id == visible_chat_id
                and assignment.applied_chat_context_revision == chat_context_revision
            ),
        )

    async def await_media_grant_applied(
        self,
        *,
        session_id: str,
        generation: int,
        refresh_id: str,
        media_grant_revision: int,
        client_participant_identity: str,
        timeout_seconds: float = 10,
    ) -> AssignmentSnapshot:
        """Wait boundedly for the exact ordered publisher-rotation acknowledgement."""

        _uuid4(session_id, "invalid_session_id", ControlProtocolError)
        _positive(generation, "invalid_generation", ControlProtocolError)
        _uuid4(refresh_id, "invalid_refresh_id", ControlProtocolError)
        _positive(
            media_grant_revision,
            "invalid_media_grant_revision",
            ControlProtocolError,
        )
        _opaque(
            client_participant_identity,
            "invalid_client_participant_identity",
            ControlProtocolError,
        )
        return await self._await_assignment_state(
            session_id=session_id,
            generation=generation,
            timeout_seconds=timeout_seconds,
            timeout_code="media_grant_applied_timeout",
            predicate=lambda assignment: (
                assignment.pending_media_refresh_id == refresh_id
                and assignment.pending_media_grant_revision == media_grant_revision
                and assignment.applied_media_refresh_id == refresh_id
                and assignment.applied_media_grant_revision == media_grant_revision
                and assignment.pending_client_participant_identity
                == client_participant_identity
            ),
        )

    async def touch_connection(self, connection_id: str) -> None:
        """Record a transport-level ping/pong receipt using monotonic time."""

        _uuid4(connection_id, "invalid_connection_id", ControlProtocolError)
        now = self._clock.monotonic()
        async with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                raise StaleFence("stale_connection")
            connection.last_seen_monotonic = now

    async def unregister_worker(self, connection_id: str) -> tuple[str, ...]:
        """Fence one exact transport and release all of its in-memory leases.

        A stale disconnect is intentionally idempotent: a replaced worker's
        endpoint must never be able to remove the replacement connection.
        The returned values are the session IDs whose assignments were
        released, so the endpoint owner can reconcile adjacent durable leases.
        """

        _uuid4(connection_id, "invalid_connection_id", ControlProtocolError)
        return await self._fence_connection(
            connection_id,
            1001,
            "connection_closed",
        )

    async def shutdown(self) -> tuple[str, ...]:
        """Stop admission and share one bounded cleanup across all callers."""

        async with self._lock:
            task = self._shutdown_task
            if task is not None and task.done():
                task.result()
                return ()
            if task is None:
                task = asyncio.create_task(
                    self._shutdown_once(),
                    name="voice-worker-pool-shutdown",
                )
                self._shutdown_task = task
        return await asyncio.shield(task)

    async def _shutdown_once(self) -> tuple[str, ...]:
        """Fence every worker/assignment exactly once and close boundedly."""

        async with self._registration_lock:
            async with self._lock:
                self._closed = True
                connections = tuple(self._connections.values())
                released = tuple(sorted(self._assignments))
                for connection in connections:
                    self._remove_connection_locked(connection.connection_id)
                self._replacing_identities.clear()
        if connections:
            await asyncio.gather(
                *(
                    _safe_close(connection.socket, 1001, "coordinator_shutdown")
                    for connection in connections
                )
            )
        return released

    async def expire_connections(self) -> tuple[str, ...]:
        """Fence and close workers whose monotonic liveness lease elapsed."""

        releases = await self.expire_connection_leases()
        return tuple(item.connection_id for item in releases)

    async def expire_connection_leases(
        self,
    ) -> tuple[WorkerConnectionRelease, ...]:
        """Fence expired workers while preserving exact cleanup authority."""

        now = self._clock.monotonic()
        expired: list[tuple[_WorkerConnection, WorkerConnectionRelease]] = []
        async with self._lock:
            for connection_id, connection in tuple(self._connections.items()):
                if not self._connection_live(connection, now):
                    assignments = tuple(
                        self._reservation(assignment)
                        for session_id in sorted(connection.assignments)
                        if (assignment := self._assignments.get(session_id))
                        is not None
                        and assignment.connection_id == connection.connection_id
                    )
                    expired.append(
                        (
                            connection,
                            WorkerConnectionRelease(
                                connection_id=connection.connection_id,
                                worker_identity=connection.worker_identity,
                                accepted_max_sessions=connection.capacity,
                                assignments=assignments,
                            ),
                        )
                    )
                    self._remove_connection_locked(connection_id)
        for connection, _release in expired:
            await _safe_close(connection.socket, 4000, "lease_expired")
        return tuple(
            release
            for _connection, release in sorted(
                expired,
                key=lambda item: item[0].connection_id,
            )
        )

    async def release_session(
        self, session_id: str, generation: int, assignment_id: str
    ) -> bool:
        """Release only the exact assignment fence; stale releases do nothing."""

        _uuid4(session_id, "invalid_session_id", ControlProtocolError)
        _positive(generation, "invalid_generation", ControlProtocolError)
        _uuid4(assignment_id, "invalid_assignment_id", ControlProtocolError)
        async with self._lock:
            assignment = self._assignments.get(session_id)
            if assignment is None:
                return False
            if (
                assignment.request.generation != generation
                or assignment.assignment_id != assignment_id
            ):
                return False
            self._remove_assignment_locked(assignment)
            return True

    async def release_terminal_assignment(
        self,
        *,
        connection_id: str,
        session_id: str,
        generation: int,
        terminal_state: str,
    ) -> WorkerTerminalRelease | None:
        """Atomically release one assignment only while its terminal fence holds."""

        _uuid4(connection_id, "invalid_connection_id", ControlProtocolError)
        _uuid4(session_id, "invalid_session_id", ControlProtocolError)
        _positive(generation, "invalid_generation", ControlProtocolError)
        if terminal_state not in {"failed", "ended"}:
            raise ControlProtocolError("invalid_terminal_state")
        async with self._lock:
            connection = self._connections.get(connection_id)
            assignment = self._assignments.get(session_id)
            if connection is None or assignment is None:
                return None
            if (
                self._identity_connections.get(connection.worker_identity)
                != connection_id
                or assignment.connection_id != connection_id
                or assignment.worker_identity != connection.worker_identity
                or assignment.request.generation != generation
                or assignment.media_state != terminal_state
                or session_id not in connection.assignments
            ):
                return None
            reservation = self._reservation(assignment)
            self._remove_assignment_locked(assignment)
            return WorkerTerminalRelease(
                reservation=reservation,
                terminal_state=terminal_state,
                accepted_max_sessions=connection.capacity,
            )

    async def current_reservation(
        self,
        *,
        session_id: str,
        generation: int,
    ) -> SessionReservation:
        """Return the exact live assignment fence without exposing credentials."""

        _uuid4(session_id, "invalid_session_id", ControlProtocolError)
        _positive(generation, "invalid_generation", ControlProtocolError)
        async with self._lock:
            assignment = self._assignments.get(session_id)
            if assignment is None:
                raise StaleFence("stale_assignment")
            if assignment.request.generation != generation:
                raise StaleFence("stale_generation")
            self._current_connection_locked(assignment.connection_id)
            return self._reservation(assignment)

    async def current_recognition_binding(
        self,
        *,
        session_id: str,
        generation: int,
        client_turn_id: str,
    ) -> TranscriptTurnBinding:
        """Return one live, fully bound recognition under its assignment fence."""

        _uuid4(session_id, "invalid_session_id", ControlProtocolError)
        _positive(generation, "invalid_generation", ControlProtocolError)
        _uuid4(client_turn_id, "invalid_client_turn_id", ControlProtocolError)
        async with self._lock:
            assignment = self._assignments.get(session_id)
            if assignment is None:
                raise StaleFence("stale_assignment")
            if assignment.request.generation != generation:
                raise StaleFence("stale_generation")
            self._current_connection_locked(assignment.connection_id)
            recognition = assignment.recognitions.get(client_turn_id)
            binding = assignment.turn_bindings.get(client_turn_id)
            if recognition is None or binding is None:
                raise StaleFence("recognition_not_bound")
            if (
                recognition.session_id != binding.session_id
                or recognition.generation != binding.generation
                or recognition.assignment_id != binding.assignment_id
                or recognition.worker_identity != binding.worker_identity
                or recognition.client_turn_id != binding.client_turn_id
                or recognition.media_grant_revision != binding.media_grant_revision
                or recognition.chat_id != binding.chat_id
                or recognition.chat_context_revision
                != binding.chat_context_revision
            ):
                raise StaleFence("recognition_binding_conflict")
            return binding

    async def clear_suppressed_recognition(
        self,
        binding: TranscriptTurnBinding,
    ) -> None:
        """Clear one exact recognition binding without sending a disposition."""

        if not isinstance(binding, TranscriptTurnBinding):
            raise TypeError("binding must be TranscriptTurnBinding")
        async with self._lock:
            assignment = self._assignments.get(binding.session_id)
            if assignment is None:
                raise StaleFence("stale_assignment")
            if assignment.request.generation != binding.generation:
                raise StaleFence("stale_generation")
            if (
                assignment.assignment_id != binding.assignment_id
                or assignment.worker_identity != binding.worker_identity
            ):
                raise StaleFence("stale_worker_assignment")
            self._current_connection_locked(assignment.connection_id)
            recognition = assignment.recognitions.get(binding.client_turn_id)
            current = assignment.turn_bindings.get(binding.client_turn_id)
            if recognition is None or current is None:
                raise StaleFence("recognition_not_bound")
            if current != binding or (
                recognition.session_id != binding.session_id
                or recognition.generation != binding.generation
                or recognition.assignment_id != binding.assignment_id
                or recognition.worker_identity != binding.worker_identity
                or recognition.client_turn_id != binding.client_turn_id
                or recognition.media_grant_revision != binding.media_grant_revision
                or recognition.chat_id != binding.chat_id
                or recognition.chat_context_revision
                != binding.chat_context_revision
            ):
                raise StaleFence("recognition_binding_conflict")
            assignment.turn_bindings.pop(binding.client_turn_id)
            assignment.recognitions.pop(binding.client_turn_id)

    async def _await_assignment_state(
        self,
        *,
        session_id: str,
        generation: int,
        timeout_seconds: float,
        timeout_code: str,
        predicate: Callable[[_WorkerAssignment], bool],
    ) -> AssignmentSnapshot:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.01 <= float(timeout_seconds) <= 30
        ):
            raise ValueError("invalid_worker_wait_timeout")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(timeout_seconds)
        while True:
            async with self._lock:
                assignment = self._assignments.get(session_id)
                if assignment is None:
                    raise StaleFence("stale_assignment")
                if assignment.request.generation != generation:
                    raise StaleFence("stale_generation")
                if predicate(assignment):
                    return self._assignment_snapshot(assignment)
                changed = assignment.state_changed
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CapacityUnavailable(timeout_code)
            try:
                await asyncio.wait_for(changed.wait(), timeout=remaining)
            except TimeoutError:
                raise CapacityUnavailable(timeout_code) from None

    def assignment_snapshot(self, session_id: str) -> AssignmentSnapshot:
        """Return credential-free operational state for tests/observability."""

        assignment = self._assignments.get(session_id)
        if assignment is None:
            raise StaleFence("stale_assignment")
        return self._assignment_snapshot(assignment)

    @staticmethod
    def _assignment_snapshot(assignment: _WorkerAssignment) -> AssignmentSnapshot:
        return AssignmentSnapshot(
            session_id=assignment.request.session_id,
            generation=assignment.request.generation,
            assignment_id=assignment.assignment_id,
            worker_identity=assignment.worker_identity,
            connection_id=assignment.connection_id,
            worker_rtc_grant_revision=assignment.request.worker_rtc_grant_revision,
            ready=assignment.ready,
            media_state=assignment.media_state,
            next_outgoing_sequence=assignment.next_outgoing_sequence,
            next_incoming_sequence=assignment.next_incoming_sequence,
            applied_visible_chat_id=assignment.applied_visible_chat_id,
            applied_chat_context_revision=assignment.applied_chat_context_revision,
            applied_media_refresh_id=assignment.applied_media_refresh_id,
            applied_media_grant_revision=assignment.applied_media_grant_revision,
        )

    def readiness(self) -> WorkerPoolReadiness:
        """Return exact-profile, credential-free live capacity state."""

        now = self._clock.monotonic()
        live = [
            connection
            for connection in self._connections.values()
            if self._connection_live(connection, now)
            and connection.worker_identity not in self._replacing_identities
        ]
        total = sum(item.capacity for item in live)
        available = sum(max(0, item.capacity - len(item.assignments)) for item in live)
        available = min(
            available, max(0, self._policy.max_total_sessions - len(self._assignments))
        )
        if not live:
            reason = "worker_unavailable"
        elif available == 0:
            reason = "capacity_exhausted"
        else:
            reason = "ready"
        return WorkerPoolReadiness(
            ready=reason == "ready",
            reason=reason,
            worker_count=len(live),
            capacity_total=total,
            capacity_available=available,
        )

    def _parse_registration(
        self,
        frame: Mapping[str, Any],
        *,
        authenticated_identity: str,
    ) -> tuple[str, int]:
        required = {
            "type",
            "schema_version",
            "message_id",
            "sequence",
            "sent_at",
            "worker_identity",
            "max_sessions",
            "runtime_closure_sha256",
            "profile",
        }
        if not isinstance(frame, Mapping) or set(frame) != required:
            raise RegistrationError("invalid_registration_fields")
        if frame["type"] != "worker_register" or frame["schema_version"] != "1":
            raise RegistrationError("invalid_registration_type")
        _uuid4(frame["message_id"], "invalid_message_id", RegistrationError)
        if frame["sequence"] != 0:
            raise RegistrationError("invalid_registration_sequence")
        _parse_timestamp(frame["sent_at"], "invalid_sent_at", RegistrationError)
        identity = _opaque(
            frame["worker_identity"], "invalid_worker_identity", RegistrationError
        )
        authenticated = _opaque(
            authenticated_identity,
            "invalid_authenticated_identity",
            RegistrationError,
        )
        if identity != authenticated:
            raise RegistrationError("identity_mismatch")
        capacity = _bounded_int(
            frame["max_sessions"], 1, 100, "invalid_worker_capacity", RegistrationError
        )
        if frame["runtime_closure_sha256"] != self._policy.runtime_closure_sha256:
            raise RegistrationError("closure_mismatch")
        if frame["profile"] != FIXED_VOICE_PROFILE:
            raise RegistrationError("profile_mismatch")
        return identity, capacity

    async def _current_assignment(
        self,
        reservation: SessionReservation,
        request: SessionBindRequest | None = None,
    ) -> _WorkerAssignment:
        async with self._lock:
            assignment = self._assignments.get(reservation.session_id)
            if assignment is None:
                raise StaleFence("stale_assignment")
            if (
                assignment.request.generation != reservation.generation
                or assignment.assignment_id != reservation.assignment_id
                or assignment.worker_identity != reservation.worker_identity
                or assignment.connection_id != reservation.connection_id
            ):
                raise StaleFence("stale_assignment")
            if (
                reservation.worker_rtc_grant_revision
                != assignment.request.worker_rtc_grant_revision
            ):
                raise StaleFence("stale_worker_grant_revision")
            if request is not None and request != assignment.request:
                raise StaleFence("stale_bind_request")
            self._current_connection_locked(assignment.connection_id)
            return assignment

    def _validate_worker_grant(
        self,
        assignment: _WorkerAssignment,
        request: SessionBindRequest,
        grant: Mapping[str, Any],
    ) -> dict[str, Any]:
        required = {
            "revision",
            "livekit_url",
            "join_token",
            "issued_at",
            "expires_at",
            "room_name",
            "worker_identity",
        }
        if not isinstance(grant, Mapping) or set(grant) != required:
            raise ControlProtocolError("invalid_worker_grant_fields")
        revision = _positive(
            grant["revision"], "invalid_worker_grant_revision", ControlProtocolError
        )
        if revision != request.worker_rtc_grant_revision:
            raise ControlProtocolError("grant_revision_mismatch")
        if grant["room_name"] != request.room_name:
            raise ControlProtocolError("grant_room_mismatch")
        if grant["worker_identity"] != assignment.worker_identity:
            raise ControlProtocolError("grant_worker_mismatch")
        _validate_livekit_url(
            grant["livekit_url"],
            allow_insecure=self._policy.allow_insecure_livekit_url,
        )
        token = grant["join_token"]
        if not isinstance(token, str) or not 32 <= len(token) <= 8_192:
            raise ControlProtocolError("invalid_worker_join_token")
        issued_at = _parse_timestamp(
            grant["issued_at"], "invalid_grant_issued_at", ControlProtocolError
        )
        expires_at = _parse_timestamp(
            grant["expires_at"], "invalid_grant_expires_at", ControlProtocolError
        )
        now = self._clock.utcnow()
        if issued_at < assignment.assigned_at:
            raise ControlProtocolError("grant_predates_assignment")
        if issued_at > now + timedelta(seconds=5):
            raise ControlProtocolError("grant_not_yet_valid")
        if expires_at <= now:
            raise ControlProtocolError("grant_expired")
        if expires_at <= issued_at:
            raise ControlProtocolError("invalid_grant_lifetime")
        if expires_at - issued_at > timedelta(minutes=5):
            raise ControlProtocolError("grant_lifetime_exceeded")
        return dict(grant)

    def _validate_idempotent_request(
        self, current: SessionBindRequest, requested: SessionBindRequest
    ) -> None:
        if replace(current, worker_rtc_grant_revision=1) != replace(
            requested, worker_rtc_grant_revision=1
        ):
            raise StaleFence("assignment_request_conflict")

    def _validate_tracked_command(
        self,
        assignment: _WorkerAssignment,
        frame_type: str,
        fields: Mapping[str, Any],
    ) -> None:
        self._validate_assignment_command_state(assignment, frame_type)
        if frame_type == "session_context_update":
            if set(fields) != {
                "media_grant_revision",
                "visible_chat_id",
                "chat_context_revision",
            }:
                raise ControlProtocolError("invalid_context_update_fields")
            if (
                fields["media_grant_revision"]
                != assignment.request.media_grant_revision
            ):
                raise StaleFence("stale_media_grant_revision")
            _uuid4(
                fields["visible_chat_id"],
                "invalid_visible_chat_id",
                ControlProtocolError,
            )
            _positive(
                fields["chat_context_revision"],
                "invalid_chat_context_revision",
                ControlProtocolError,
            )
        elif frame_type == "media_grant_rotated":
            if set(fields) != {
                "refresh_id",
                "previous_media_grant_revision",
                "media_grant_revision",
                "client_participant_identity",
                "transport",
                "grant_expires_at",
            }:
                raise ControlProtocolError("invalid_media_rotation_fields")
            _uuid4(fields["refresh_id"], "invalid_refresh_id", ControlProtocolError)
            previous = _positive(
                fields["previous_media_grant_revision"],
                "invalid_previous_media_grant_revision",
                ControlProtocolError,
            )
            revision = _positive(
                fields["media_grant_revision"],
                "invalid_media_grant_revision",
                ControlProtocolError,
            )
            _opaque(
                fields["client_participant_identity"],
                "invalid_client_participant_identity",
                ControlProtocolError,
            )
            if fields["transport"] != assignment.request.transport:
                raise ControlProtocolError("transport_mismatch")
            expires_at = _parse_timestamp(
                fields["grant_expires_at"],
                "invalid_grant_expires_at",
                ControlProtocolError,
            )
            if expires_at <= self._clock.utcnow():
                raise ControlProtocolError("grant_expired")
            current_revision = assignment.request.media_grant_revision
            exact_pending_replay = (
                revision == current_revision
                and previous == revision - 1
                and assignment.pending_media_rotation
                == _media_rotation_fingerprint(fields)
            )
            if exact_pending_replay:
                return
            if previous != current_revision:
                raise StaleFence("stale_media_grant_revision")
            if revision != previous + 1:
                raise ControlProtocolError("invalid_media_grant_rotation")
        elif frame_type == "turn_bound":
            if set(fields) != {
                "client_turn_id",
                "turn_id",
                "chat_id",
                "chat_context_revision",
                "media_grant_revision",
                "submission_id",
                "request_generation",
            }:
                raise ControlProtocolError("invalid_turn_bound_fields")
            binding = self._transcript_binding_from_fields(assignment, fields)
            recognition = assignment.recognitions.get(binding.client_turn_id)
            if recognition is None:
                raise StaleFence("recognition_not_started")
            if (
                recognition.media_grant_revision != binding.media_grant_revision
                or recognition.chat_id != binding.chat_id
                or recognition.chat_context_revision != binding.chat_context_revision
            ):
                raise StaleFence("recognition_binding_conflict")
            existing = assignment.turn_bindings.get(binding.client_turn_id)
            if existing is not None and existing != binding:
                raise StaleFence("turn_binding_conflict")
        elif frame_type in {"transcript_accepted", "transcript_rejected"}:
            common = {
                "turn_id",
                "client_turn_id",
                "submission_id",
                "request_generation",
                "chat_id",
                "media_grant_revision",
            }
            expected = common | (
                {"accepted_message_id"}
                if frame_type == "transcript_accepted"
                else {"reason", "retry_policy"}
            )
            if set(fields) != expected:
                raise ControlProtocolError("invalid_transcript_disposition_fields")
            client_turn_id = _uuid4(
                fields.get("client_turn_id"),
                "invalid_client_turn_id",
                ControlProtocolError,
            )
            existing = assignment.turn_bindings.get(client_turn_id)
            if existing is None:
                raise StaleFence("turn_binding_conflict")
            for name in (
                "turn_id",
                "submission_id",
                "request_generation",
                "chat_id",
            ):
                _uuid4(fields.get(name), f"invalid_{name}", ControlProtocolError)
            _positive(
                fields.get("media_grant_revision"),
                "invalid_media_grant_revision",
                ControlProtocolError,
            )
            if any(fields[name] != getattr(existing, name) for name in common):
                raise StaleFence("turn_binding_conflict")
            if frame_type == "transcript_accepted":
                _positive(
                    fields["accepted_message_id"],
                    "invalid_accepted_message_id",
                    ControlProtocolError,
                )
            else:
                if fields["reason"] not in _TRANSCRIPT_REJECTION_REASONS:
                    raise ControlProtocolError("invalid_transcript_rejection_reason")
                if fields["retry_policy"] not in _TRANSCRIPT_RETRY_POLICIES:
                    raise ControlProtocolError("invalid_transcript_retry_policy")

    @staticmethod
    def _validate_assignment_command_state(
        assignment: _WorkerAssignment,
        frame_type: str,
    ) -> None:
        if (
            frame_type != "end_session"
            and assignment.media_state in {"failed", "ended"}
        ):
            raise StaleFence("terminal_assignment")

    def _commit_tracked_command(
        self,
        assignment: _WorkerAssignment,
        frame_type: str,
        fields: Mapping[str, Any],
    ) -> None:
        if frame_type == "session_context_update":
            assignment.expected_visible_chat_id = fields["visible_chat_id"]
            assignment.expected_chat_context_revision = fields["chat_context_revision"]
            assignment.applied_visible_chat_id = None
            assignment.applied_chat_context_revision = None
            assignment.request = replace(
                assignment.request,
                visible_chat_id=fields["visible_chat_id"],
                chat_context_revision=fields["chat_context_revision"],
            )
            self._signal_assignment(assignment)
        elif frame_type == "media_grant_rotated":
            fingerprint = _media_rotation_fingerprint(fields)
            if (
                assignment.pending_media_rotation == fingerprint
                and assignment.request.media_grant_revision
                == fields["media_grant_revision"]
            ):
                return
            assignment.pending_media_refresh_id = fields["refresh_id"]
            assignment.pending_media_grant_revision = fields["media_grant_revision"]
            assignment.pending_client_participant_identity = fields[
                "client_participant_identity"
            ]
            assignment.pending_media_rotation = fingerprint
            assignment.applied_media_refresh_id = None
            assignment.applied_media_grant_revision = None
            assignment.request = replace(
                assignment.request,
                media_grant_revision=fields["media_grant_revision"],
                client_participant_identity=fields["client_participant_identity"],
            )
            self._signal_assignment(assignment)
        elif frame_type == "turn_bound":
            binding = self._transcript_binding_from_fields(assignment, fields)
            assignment.turn_bindings[binding.client_turn_id] = binding
        elif frame_type in {"transcript_accepted", "transcript_rejected"}:
            client_turn_id = str(fields["client_turn_id"])
            assignment.turn_bindings.pop(client_turn_id, None)
            assignment.recognitions.pop(client_turn_id, None)

    @staticmethod
    def _transcript_binding_from_fields(
        assignment: _WorkerAssignment,
        fields: Mapping[str, Any],
    ) -> TranscriptTurnBinding:
        try:
            return TranscriptTurnBinding(
                session_id=assignment.request.session_id,
                generation=assignment.request.generation,
                media_grant_revision=fields.get("media_grant_revision"),
                assignment_id=assignment.assignment_id,
                worker_identity=assignment.worker_identity,
                turn_id=fields.get("turn_id"),
                client_turn_id=fields.get("client_turn_id"),
                submission_id=fields.get("submission_id"),
                request_generation=fields.get("request_generation"),
                chat_id=fields.get("chat_id"),
                chat_context_revision=fields.get("chat_context_revision"),
            )
        except (TypeError, ValueError):
            raise ControlProtocolError("invalid_transcript_binding") from None

    @staticmethod
    def _validate_session_context_applied(
        frame: Mapping[str, Any], assignment: _WorkerAssignment
    ) -> None:
        if set(frame) != {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "media_grant_revision",
            "visible_chat_id",
            "chat_context_revision",
            "occurred_at",
        }:
            raise ControlProtocolError("invalid_context_applied_fields")
        _uuid4(
            frame["visible_chat_id"],
            "invalid_visible_chat_id",
            ControlProtocolError,
        )
        _positive(
            frame["chat_context_revision"],
            "invalid_chat_context_revision",
            ControlProtocolError,
        )
        _parse_timestamp(
            frame["occurred_at"],
            "invalid_occurred_at",
            ControlProtocolError,
        )
        if frame["media_grant_revision"] != assignment.request.media_grant_revision:
            raise StaleFence("stale_media_grant_revision")
        if (
            frame["visible_chat_id"] != assignment.expected_visible_chat_id
            or frame["chat_context_revision"]
            != assignment.expected_chat_context_revision
        ):
            raise StaleFence("stale_chat_context_revision")

    @staticmethod
    def _validate_media_grant_applied(
        frame: Mapping[str, Any], assignment: _WorkerAssignment
    ) -> None:
        if set(frame) != {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "refresh_id",
            "media_grant_revision",
            "client_participant_identity",
            "occurred_at",
        }:
            raise ControlProtocolError("invalid_media_applied_fields")
        _uuid4(frame["refresh_id"], "invalid_refresh_id", ControlProtocolError)
        _positive(
            frame["media_grant_revision"],
            "invalid_media_grant_revision",
            ControlProtocolError,
        )
        _opaque(
            frame["client_participant_identity"],
            "invalid_client_participant_identity",
            ControlProtocolError,
        )
        _parse_timestamp(
            frame["occurred_at"],
            "invalid_occurred_at",
            ControlProtocolError,
        )
        if (
            frame["refresh_id"] != assignment.pending_media_refresh_id
            or frame["media_grant_revision"] != assignment.pending_media_grant_revision
            or frame["client_participant_identity"]
            != assignment.pending_client_participant_identity
        ):
            raise StaleFence("stale_media_grant_rotation")

    @staticmethod
    def _validate_recognition_started(
        frame: Mapping[str, Any], assignment: _WorkerAssignment
    ) -> None:
        if set(frame) != {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "client_turn_id",
            "media_grant_revision",
            "visible_chat_id",
            "chat_context_revision",
            "occurred_at",
        }:
            raise ControlProtocolError("invalid_recognition_started_fields")
        client_turn_id = _uuid4(
            frame["client_turn_id"],
            "invalid_client_turn_id",
            ControlProtocolError,
        )
        _uuid4(
            frame["visible_chat_id"],
            "invalid_visible_chat_id",
            ControlProtocolError,
        )
        _positive(
            frame["media_grant_revision"],
            "invalid_media_grant_revision",
            ControlProtocolError,
        )
        _positive(
            frame["chat_context_revision"],
            "invalid_chat_context_revision",
            ControlProtocolError,
        )
        _parse_timestamp(
            frame["occurred_at"],
            "invalid_occurred_at",
            ControlProtocolError,
        )
        if frame["media_grant_revision"] != assignment.request.media_grant_revision:
            raise StaleFence("stale_media_grant_revision")
        if (
            frame["visible_chat_id"] != assignment.applied_visible_chat_id
            or frame["chat_context_revision"]
            != assignment.applied_chat_context_revision
        ):
            raise StaleFence("stale_chat_context_revision")
        existing = assignment.recognitions.get(client_turn_id)
        if existing is not None:
            if (
                existing.media_grant_revision != frame["media_grant_revision"]
                or existing.chat_id != frame["visible_chat_id"]
                or existing.chat_context_revision != frame["chat_context_revision"]
            ):
                raise StaleFence("recognition_binding_conflict")
            return
        if len(assignment.recognitions) >= MAX_PENDING_TRANSCRIPTS:
            raise ControlProtocolError("recognition_capacity_exceeded")

    @staticmethod
    def _validate_recognition_failed(
        frame: Mapping[str, Any], assignment: _WorkerAssignment
    ) -> None:
        if set(frame) != {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "client_turn_id",
            "reason",
            "occurred_at",
        }:
            raise ControlProtocolError("invalid_recognition_failed_fields")
        client_turn_id = _uuid4(
            frame["client_turn_id"],
            "invalid_client_turn_id",
            ControlProtocolError,
        )
        if frame["reason"] not in _RECOGNITION_FAILURE_REASONS:
            raise ControlProtocolError("invalid_recognition_failure_reason")
        _parse_timestamp(
            frame["occurred_at"],
            "invalid_occurred_at",
            ControlProtocolError,
        )
        recognition = assignment.recognitions.get(client_turn_id)
        binding = assignment.turn_bindings.get(client_turn_id)
        if recognition is None or binding is None:
            raise StaleFence("recognition_not_bound")
        if (
            recognition.session_id != binding.session_id
            or recognition.generation != binding.generation
            or recognition.assignment_id != binding.assignment_id
            or recognition.worker_identity != binding.worker_identity
            or recognition.client_turn_id != binding.client_turn_id
            or recognition.media_grant_revision != binding.media_grant_revision
            or recognition.chat_id != binding.chat_id
            or recognition.chat_context_revision != binding.chat_context_revision
        ):
            raise StaleFence("recognition_binding_conflict")

    def _validate_worker_ready(
        self, frame: Mapping[str, Any], assignment: _WorkerAssignment
    ) -> None:
        required = {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "assignment_id",
            "worker_identity",
            "worker_rtc_grant_revision",
            "profile_ready",
        }
        allowed = required | {"reason"}
        if not required.issubset(frame) or not set(frame).issubset(allowed):
            raise ControlProtocolError("invalid_worker_ready_fields")
        _uuid4(frame["assignment_id"], "invalid_assignment_id", ControlProtocolError)
        if frame["assignment_id"] != assignment.assignment_id:
            raise StaleFence("stale_assignment")
        if frame["worker_identity"] != assignment.worker_identity:
            raise StaleFence("worker_identity_mismatch")
        revision = _positive(
            frame["worker_rtc_grant_revision"],
            "invalid_worker_grant_revision",
            ControlProtocolError,
        )
        if revision != assignment.request.worker_rtc_grant_revision:
            raise StaleFence("stale_worker_grant_revision")
        if not isinstance(frame["profile_ready"], bool):
            raise ControlProtocolError("invalid_profile_ready")
        if "reason" in frame and _REASON.fullmatch(frame["reason"]) is None:
            raise ControlProtocolError("invalid_worker_ready_reason")

    def _validate_heartbeat(self, frame: Mapping[str, Any]) -> None:
        if set(frame) != {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "media_state",
        }:
            raise ControlProtocolError("invalid_heartbeat_fields")
        if frame["media_state"] not in {
            "connecting",
            "ready",
            "reconnecting",
            "failed",
            "ended",
        }:
            raise ControlProtocolError("invalid_media_state")

    @staticmethod
    def _validate_pool_heartbeat(frame: Mapping[str, Any]) -> None:
        if set(frame) != {
            "type",
            "schema_version",
            "message_id",
            "sequence",
            "sent_at",
            "worker_identity",
            "connection_id",
        }:
            raise ControlProtocolError("invalid_pool_heartbeat_fields")
        _opaque(
            frame["worker_identity"],
            "invalid_worker_identity",
            ControlProtocolError,
        )
        _uuid4(
            frame["connection_id"],
            "invalid_connection_id",
            ControlProtocolError,
        )

    @staticmethod
    def _validate_media_state(frame: Mapping[str, Any]) -> None:
        required = {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "state",
            "occurred_at",
        }
        allowed = required | {"reason"}
        if not required.issubset(frame) or not set(frame).issubset(allowed):
            raise ControlProtocolError("invalid_media_state_fields")
        if frame["state"] not in {
            "connecting",
            "listening",
            "speech_detected",
            "transcribing",
            "reconnecting",
            "failed",
            "ended",
        }:
            raise ControlProtocolError("invalid_media_state")
        _parse_timestamp(
            frame["occurred_at"],
            "invalid_occurred_at",
            ControlProtocolError,
        )
        if "reason" in frame and (
            not isinstance(frame["reason"], str)
            or _REASON.fullmatch(frame["reason"]) is None
        ):
            raise ControlProtocolError("invalid_media_state_reason")

    def _check_receive_rate_locked(
        self, assignment: _WorkerAssignment, now: float
    ) -> None:
        cutoff = now - self._policy.receive_window_seconds
        while assignment.inbound_times and assignment.inbound_times[0] <= cutoff:
            assignment.inbound_times.popleft()
        if len(assignment.inbound_times) >= self._policy.max_receive_frames:
            raise ControlProtocolError("frame_rate_exceeded")
        assignment.inbound_times.append(now)

    def _check_connection_receive_rate_locked(
        self, connection: _WorkerConnection, now: float
    ) -> None:
        cutoff = now - self._policy.receive_window_seconds
        while (
            connection.inbound_pool_times
            and connection.inbound_pool_times[0] <= cutoff
        ):
            connection.inbound_pool_times.popleft()
        if len(connection.inbound_pool_times) >= self._policy.max_receive_frames:
            raise ControlProtocolError("frame_rate_exceeded")
        connection.inbound_pool_times.append(now)

    async def _send_payload(
        self,
        socket: WorkerSocket,
        lock: asyncio.Lock,
        frame: Mapping[str, Any],
    ) -> None:
        payload = _encode_frame(frame)
        async with lock:
            try:
                await asyncio.wait_for(
                    socket.send(payload), timeout=self._policy.send_timeout_seconds
                )
            except asyncio.TimeoutError:
                raise ControlSendError("send_timeout") from None
            except Exception:
                raise ControlSendError("send_failed") from None

    async def _fence_connection(
        self, connection_id: str, close_code: int, close_reason: str
    ) -> tuple[str, ...]:
        async with self._lock:
            current = self._connections.get(connection_id)
            assignments = (
                tuple(sorted(current.assignments)) if current is not None else ()
            )
            connection = self._remove_connection_locked(connection_id)
        if connection is None:
            return ()
        await _safe_close(connection.socket, close_code, close_reason)
        return assignments

    def _remove_connection_locked(self, connection_id: str) -> _WorkerConnection | None:
        connection = self._connections.pop(connection_id, None)
        if connection is None:
            return None
        if self._identity_connections.get(connection.worker_identity) == connection_id:
            del self._identity_connections[connection.worker_identity]
        self._remove_connection_assignments_locked(connection)
        return connection

    def _remove_connection_assignments_locked(
        self, connection: _WorkerConnection
    ) -> list[str]:
        fenced: list[str] = []
        for session_id in tuple(connection.assignments):
            assignment = self._assignments.get(session_id)
            if (
                assignment is not None
                and assignment.connection_id == connection.connection_id
            ):
                fenced.append(assignment.assignment_id)
                del self._assignments[session_id]
                self._signal_assignment(assignment)
            connection.assignments.discard(session_id)
        return fenced

    def _remove_assignment_locked(self, assignment: _WorkerAssignment) -> None:
        if self._assignments.get(assignment.request.session_id) is assignment:
            del self._assignments[assignment.request.session_id]
        connection = self._connections.get(assignment.connection_id)
        if connection is not None:
            connection.assignments.discard(assignment.request.session_id)
        self._signal_assignment(assignment)

    @staticmethod
    def _signal_assignment(assignment: _WorkerAssignment) -> None:
        changed = assignment.state_changed
        assignment.state_changed = asyncio.Event()
        changed.set()

    def _current_connection_locked(self, connection_id: str) -> _WorkerConnection:
        connection = self._connections.get(connection_id)
        if connection is None:
            raise StaleFence("stale_connection")
        return connection

    def _connection_live(self, connection: _WorkerConnection, now: float) -> bool:
        return (
            now - connection.last_seen_monotonic < self._policy.connection_lease_seconds
        )

    def _reservation(self, assignment: _WorkerAssignment) -> SessionReservation:
        return SessionReservation(
            session_id=assignment.request.session_id,
            generation=assignment.request.generation,
            assignment_id=assignment.assignment_id,
            worker_identity=assignment.worker_identity,
            connection_id=assignment.connection_id,
            worker_rtc_grant_revision=assignment.request.worker_rtc_grant_revision,
        )

    def _new_uuid4(self, field_name: str) -> str:
        value = self._uuid_factory()
        if not isinstance(value, UUID) or value.version != 4:
            raise RuntimeError(f"{field_name}_factory_must_return_uuid4")
        return str(value)


@dataclass(frozen=True, slots=True)
class ControlLeaseState:
    """Row-lock snapshot for one session's coordinator control lease."""

    generation: int
    owner_id: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _positive(self.generation, "invalid_generation")
        if (self.owner_id is None) != (self.expires_at is None):
            raise ValueError("invalid_control_lease_state")
        if self.owner_id is not None:
            _opaque(self.owner_id, "invalid_control_owner")
            _aware(self.expires_at, "invalid_control_lease_expiry")


class ControlLeaseAdapter:
    """Pure CAS rules for durable coordinator ownership and crash recovery."""

    def __init__(self, *, ttl_seconds: int = 15) -> None:
        if not 5 <= ttl_seconds <= 60:
            raise ValueError("invalid_control_lease_ttl")
        self._ttl = timedelta(seconds=ttl_seconds)

    def claim(
        self,
        state: ControlLeaseState,
        *,
        generation: int,
        owner_id: str,
        now: datetime,
    ) -> ControlLeaseState:
        now = _aware(now, "invalid_claim_time")
        _opaque(owner_id, "invalid_control_owner")
        if generation != state.generation:
            raise StaleFence("stale_generation")
        if (
            state.owner_id is not None
            and state.owner_id != owner_id
            and state.expires_at is not None
            and now < state.expires_at
        ):
            raise ClaimUnavailable("control_lease_owned")
        return ControlLeaseState(
            generation=state.generation,
            owner_id=owner_id,
            expires_at=now + self._ttl,
        )

    def release(
        self, state: ControlLeaseState, *, generation: int, owner_id: str
    ) -> ControlLeaseState:
        _opaque(owner_id, "invalid_control_owner")
        if generation != state.generation:
            raise StaleFence("stale_generation")
        if state.owner_id != owner_id:
            return state
        return ControlLeaseState(generation=state.generation)


class PhraseBook:
    """Validated allowlisted phrase-key selector with deterministic variation."""

    def __init__(self, phrases: Mapping[str, tuple[str, ...]]) -> None:
        if not isinstance(phrases, Mapping) or not phrases:
            raise ValueError("invalid_phrase_book")
        normalized: dict[str, tuple[str, ...]] = {}
        for kind, keys in phrases.items():
            if kind not in _ANNOUNCEMENT_KINDS or kind == "result":
                raise ValueError("invalid_phrase_kind")
            if not isinstance(keys, tuple) or not keys or len(keys) > 32:
                raise ValueError("invalid_phrase_keys")
            if any(
                not isinstance(key, str) or _PHRASE_KEY.fullmatch(key) is None
                for key in keys
            ):
                raise ValueError("invalid_phrase_key")
            if any(_PHRASE_KIND_BY_KEY.get(key) != kind for key in keys):
                raise ValueError("unapproved_phrase_key")
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate_phrase_key")
            if kind in {"acknowledgement", "progress"} and len(keys) < 2:
                raise ValueError("insufficient_phrase_variation")
            normalized[kind] = tuple(sorted(keys))
        self._phrases = MappingProxyType(normalized)

    def select(
        self,
        *,
        kind: str,
        stable_id: str,
        sequence: int,
        last_phrase_key: str | None,
    ) -> str:
        try:
            keys = self._phrases[kind]
        except KeyError:
            raise ClaimUnavailable("phrase_kind_unavailable") from None
        if not isinstance(stable_id, str) or not 1 <= len(stable_id) <= 128:
            raise ValueError("invalid_phrase_stable_id")
        _positive(sequence, "invalid_announcement_sequence")
        digest = hashlib.sha256(
            b"astraldeep.voice.phrase.v1\0"
            + stable_id.encode("utf-8")
            + b"\0"
            + kind.encode("ascii")
            + b"\0"
            + str(sequence).encode("ascii")
        ).digest()
        index = int.from_bytes(digest[:8], "big") % len(keys)
        selected = keys[index]
        if len(keys) > 1 and selected == last_phrase_key:
            selected = keys[(index + 1) % len(keys)]
        return selected

    def text(self, phrase_key: str) -> str:
        """Resolve only an approved key; caller text never enters this path."""

        if not isinstance(phrase_key, str):
            raise ValueError("invalid_phrase_key")
        try:
            return APPROVED_PHRASE_TEXT[phrase_key]
        except KeyError:
            raise ClaimUnavailable("phrase_key_unavailable") from None


@dataclass(frozen=True, slots=True)
class LifecyclePhrase:
    """Sanitized lifecycle selection with optional fixed allowlisted wording."""

    kind: str
    phrase_key: str | None
    text: str | None


class LifecyclePhraseSelector:
    """Translate sanitized committed lifecycle categories into safe speech."""

    def __init__(self, phrase_book: PhraseBook | None = None) -> None:
        self._phrase_book = phrase_book or PhraseBook(APPROVED_PHRASE_KEYS)

    def select(
        self,
        *,
        lifecycle: str,
        stable_id: str,
        sequence: int,
        last_phrase_key: str | None,
        waiting_reason: str | None = None,
    ) -> LifecyclePhrase:
        try:
            kind = _LIFECYCLE_KIND[lifecycle]
        except (KeyError, TypeError):
            raise ControlProtocolError("invalid_lifecycle_state") from None
        if kind != "waiting" and waiting_reason is not None:
            raise ControlProtocolError("unexpected_waiting_reason")
        if kind == "result":
            return LifecyclePhrase(kind="result", phrase_key=None, text=None)
        if kind == "waiting" and waiting_reason is not None:
            try:
                phrase_key = _WAITING_REASON_KEY[waiting_reason]
            except (KeyError, TypeError):
                raise ControlProtocolError("invalid_waiting_reason") from None
        else:
            phrase_key = self._phrase_book.select(
                kind=kind,
                stable_id=stable_id,
                sequence=sequence,
                last_phrase_key=last_phrase_key,
            )
        return LifecyclePhrase(
            kind=kind,
            phrase_key=phrase_key,
            text=self._phrase_book.text(phrase_key),
        )


@dataclass(frozen=True, slots=True)
class CadenceTurnSnapshot:
    """Content-free durable/recovery projection for one accepted voice turn."""

    session_id: str
    turn_id: str
    generation: int
    media_grant_revision: int
    announcement_sequence: int
    last_phrase_key: str | None
    next_due_at: datetime | None
    lifecycle: str
    acknowledgement_started: bool
    muted: bool
    terminal: bool

    def __post_init__(self) -> None:
        _uuid4(self.session_id, "invalid_session_id")
        _uuid4(self.turn_id, "invalid_turn_id")
        _positive(self.generation, "invalid_generation")
        _positive(self.media_grant_revision, "invalid_media_grant_revision")
        _bounded_int(
            self.announcement_sequence,
            0,
            2**63 - 1,
            "invalid_announcement_sequence",
        )
        if self.last_phrase_key is not None and (
            self.last_phrase_key not in APPROVED_PHRASE_TEXT
        ):
            raise ValueError("invalid_last_phrase_key")
        if self.next_due_at is not None:
            object.__setattr__(
                self,
                "next_due_at",
                _aware(self.next_due_at, "invalid_next_announcement_due_at"),
            )
        if self.lifecycle not in _LIFECYCLE_KIND:
            raise ValueError("invalid_lifecycle_state")
        if not all(
            isinstance(value, bool)
            for value in (self.acknowledgement_started, self.muted, self.terminal)
        ):
            raise ValueError("invalid_cadence_recovery")
        terminal_lifecycle = self.lifecycle in {
            "succeeded",
            "failed",
            "refused",
            "cancelled",
        }
        if (
            self.acknowledgement_started != (self.announcement_sequence > 0)
            or self.terminal != terminal_lifecycle
            or (self.announcement_sequence == 0 and self.next_due_at is not None)
            or (
                self.lifecycle in {"accepted", "waiting_on_user"}
                and self.next_due_at is not None
            )
            or (self.terminal and self.next_due_at is not None)
        ):
            raise ValueError("invalid_cadence_recovery")


@dataclass(frozen=True, slots=True)
class CadenceDecision:
    """One deterministic, deadline-fenced quantum offered to durable claim code."""

    announcement_id: str
    session_id: str
    turn_id: str
    generation: int
    media_grant_revision: int
    sequence: int
    kind: str
    quantum_role: str
    quantum_index: int
    max_duration_samples: int
    phrase_key: str | None
    text: str | None
    target_start_monotonic: float
    latest_start_monotonic: float
    terminal: bool


@dataclass(frozen=True, slots=True)
class PlayoutCompletion:
    """Later valid source/client finish observation using server receipt time."""

    announcement_id: str
    turn_id: str | None
    source_finished_at: datetime
    client_finished_at: datetime
    completed_at: datetime
    completed_monotonic: float

    def __post_init__(self) -> None:
        _uuid4(self.announcement_id, "invalid_announcement_id")
        if self.turn_id is not None:
            _uuid4(self.turn_id, "invalid_turn_id")
        source = _aware(self.source_finished_at, "invalid_source_finished_at")
        client = _aware(self.client_finished_at, "invalid_client_finished_at")
        completed = _aware(self.completed_at, "invalid_completed_at")
        object.__setattr__(self, "source_finished_at", source)
        object.__setattr__(self, "client_finished_at", client)
        object.__setattr__(self, "completed_at", completed)
        if completed != max(source, client):
            raise ValueError("invalid_playout_completion")
        if (
            isinstance(self.completed_monotonic, bool)
            or not isinstance(self.completed_monotonic, (int, float))
            or not math.isfinite(float(self.completed_monotonic))
        ):
            raise ValueError("invalid_playout_completion")


@dataclass(slots=True)
class _CadenceTurn:
    snapshot: CadenceTurnSnapshot
    target_start_monotonic: float
    latest_start_monotonic: float
    order: int
    waiting_reason: str | None = None
    waiting_pending: bool = False
    terminal_pending: bool = False
    terminal_suppressed: bool = False


class SpeechCadenceScheduler:
    """Deterministic two-turn, one-physical-stream cadence authority."""

    _TERMINAL_LIFECYCLES = frozenset({"succeeded", "failed", "refused", "cancelled"})

    def __init__(
        self,
        clock: CoordinatorClock,
        *,
        phrase_selector: LifecyclePhraseSelector | None = None,
    ) -> None:
        if not isinstance(clock, CoordinatorClock):
            raise TypeError("clock must be CoordinatorClock")
        self._clock = clock
        self._scheduler = MonotonicScheduler(clock, max_delay_seconds=300)
        self._selector = phrase_selector or LifecyclePhraseSelector()
        self._turns: dict[str, _CadenceTurn] = {}
        self._order = 0
        self._offered: CadenceDecision | None = None
        self._stream: CadenceDecision | None = None
        # Latest legal start for an already-due peer after stream release.
        # This is a maximum latency budget; it never delays an earlier start.
        self._handoff_deadline_at: float | None = None
        self._handoff_enforced = False
        self._failed = False

    def add_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        generation: int,
        media_grant_revision: int,
        announcement_sequence: int = 0,
        last_phrase_key: str | None = None,
        next_due_at: datetime | None = None,
    ) -> None:
        """Register an accepted turn or restore its processing cadence marker."""

        acknowledged = announcement_sequence > 0
        lifecycle = "processing" if acknowledged else "accepted"
        self.restore_turn(
            CadenceTurnSnapshot(
                session_id=session_id,
                turn_id=turn_id,
                generation=generation,
                media_grant_revision=media_grant_revision,
                announcement_sequence=announcement_sequence,
                last_phrase_key=last_phrase_key,
                next_due_at=next_due_at,
                lifecycle=lifecycle,
                acknowledgement_started=acknowledged,
                muted=False,
                terminal=False,
            )
        )

    def restore_turn(self, snapshot: CadenceTurnSnapshot) -> None:
        """Recover from durable state using UTC only to rebuild a monotonic timer."""

        if not isinstance(snapshot, CadenceTurnSnapshot):
            raise TypeError("snapshot must be CadenceTurnSnapshot")
        if snapshot.turn_id in self._turns:
            raise VoiceCoordinatorError("voice_turn_already_registered")
        if len(self._turns) >= 2:
            raise CapacityUnavailable("active_voice_turn_limit")
        now = self._clock.monotonic()
        if snapshot.announcement_sequence == 0:
            target = now
            latest = now + ACKNOWLEDGEMENT_START_SECONDS
        elif snapshot.next_due_at is not None:
            deadline = self._scheduler.recover(snapshot.next_due_at)
            target = deadline.due_monotonic
            latest = target + (CADENCE_HARD_GAP_SECONDS - CADENCE_TARGET_SECONDS)
        else:
            target = math.inf
            latest = math.inf
        self._order += 1
        self._turns[snapshot.turn_id] = _CadenceTurn(
            snapshot=snapshot,
            target_start_monotonic=target,
            latest_start_monotonic=latest,
            order=self._order,
        )

    def snapshot(self, turn_id: str) -> CadenceTurnSnapshot:
        """Return the content-free fields needed for restart reconstruction."""

        return self._turn(turn_id).snapshot

    def has_turn(self, turn_id: str) -> bool:
        """Return whether this scheduler owns the exact validated turn id."""

        _uuid4(turn_id, "invalid_turn_id")
        return turn_id in self._turns

    @property
    def active_turn_count(self) -> int:
        """Return the bounded number of turns owned by this output stream."""

        return len(self._turns)

    def next_wake_delay(self) -> float | None:
        """Return a monotonic delay until useful scheduler work may begin.

        Lifecycle mutations wake the production runner independently.  This
        value therefore contains no wall-clock or content and is safe to use
        only as a bounded local sleep hint.
        """

        if self._failed:
            raise VoiceCoordinatorError("speech_scheduler_failed")
        if self._offered is not None:
            return 0.0
        if self._stream is not None:
            return None
        now = self._clock.monotonic()
        targets: list[float] = []
        for turn in self._turns.values():
            if turn.snapshot.muted:
                continue
            if turn.terminal_pending or turn.waiting_pending:
                targets.append(now)
            elif (
                not turn.snapshot.terminal
                and turn.snapshot.lifecycle in {"accepted", "processing"}
            ):
                targets.append(turn.target_start_monotonic)
        if not targets:
            return None
        target = min(targets)
        return max(0.0, target - now)

    def next_hard_deadline_delay(self) -> float | None:
        """Return time left before the earliest eligible hard start bound."""

        if self._failed:
            raise VoiceCoordinatorError("speech_scheduler_failed")
        now = self._clock.monotonic()
        deadlines = [
            turn.latest_start_monotonic
            for turn in self._turns.values()
            if not turn.snapshot.muted
            and (
                turn.terminal_pending
                or turn.waiting_pending
                or (
                    not turn.snapshot.terminal
                    and turn.snapshot.lifecycle in {"accepted", "processing"}
                )
            )
        ]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def remove_turn(self, turn_id: str) -> None:
        """Release a quiescent terminal turn from the two-turn scheduler."""

        turn = self._turn(turn_id)
        if (
            not turn.snapshot.terminal
            or turn.terminal_pending
            or turn.waiting_pending
            or (self._offered is not None and self._offered.turn_id == turn_id)
            or (self._stream is not None and self._stream.turn_id == turn_id)
        ):
            raise VoiceCoordinatorError("voice_turn_not_quiescent")
        del self._turns[turn_id]

    def abandon_turn(self, turn_id: str) -> bool:
        """Fence one unavailable origin without requiring task cancellation.

        Chat deletion/auth loss is a voice-publication lifecycle, not an
        agent-operation terminal state.  It may therefore remove an active
        cadence stream immediately while the accepted operation continues in
        the ordinary background execution path.
        """

        self._turn(turn_id)
        preempted = self._preempt_stream(turn_id)
        self._cancel_offer(turn_id)
        del self._turns[turn_id]
        return preempted

    def next_decision(self) -> CadenceDecision | None:
        """Offer the highest-priority due quantum without starting a second stream."""

        if self._failed:
            raise VoiceCoordinatorError("speech_scheduler_failed")
        if self._stream is not None:
            return None
        if self._offered is not None:
            return self._offered
        now = self._clock.monotonic()
        candidates = self._due_candidates(now)
        if not candidates:
            return None
        candidate = min(
            candidates,
            key=lambda turn: (
                0 if turn.terminal_pending else 1,
                turn.latest_start_monotonic,
                turn.order,
            ),
        )
        if now > candidate.latest_start_monotonic + 1e-9:
            self._failed = True
            raise VoiceCoordinatorError("cadence_deadline_exceeded")
        if (
            self._handoff_enforced
            and self._handoff_deadline_at is not None
            and now > self._handoff_deadline_at + 1e-9
            and any(
                turn.target_start_monotonic <= self._handoff_deadline_at
                for turn in candidates
            )
        ):
            self._failed = True
            raise VoiceCoordinatorError("stream_handoff_budget_exceeded")
        decision = self._decision(candidate)
        other_deadlines = [
            item.latest_start_monotonic for item in candidates if item is not candidate
        ]
        if other_deadlines and (
            now
            + decision.max_duration_samples / FIXED_VOICE_PROFILE["sample_rate_hz"]
            + HANDOFF_BUDGET_SECONDS
            > min(other_deadlines) + 1e-9
        ):
            candidate = min(candidates, key=lambda turn: turn.latest_start_monotonic)
            decision = self._decision(candidate)
        self._offered = decision
        return decision

    def start(self, decision: CadenceDecision) -> None:
        """Commit one offered quantum to the sole physical stream."""

        if decision != self._offered:
            raise StaleFence("stale_cadence_decision")
        now = self._clock.monotonic()
        if now > decision.latest_start_monotonic + 1e-9:
            self._failed = True
            raise VoiceCoordinatorError("cadence_deadline_exceeded")
        if (
            self._handoff_enforced
            and self._handoff_deadline_at is not None
            and now > self._handoff_deadline_at + 1e-9
            and decision.target_start_monotonic <= self._handoff_deadline_at
        ):
            self._failed = True
            raise VoiceCoordinatorError("stream_handoff_budget_exceeded")
        turn = self._turn(decision.turn_id)
        snapshot = turn.snapshot
        lifecycle = snapshot.lifecycle
        acknowledged = snapshot.acknowledgement_started
        if decision.kind == "acknowledgement":
            acknowledged = True
            if not snapshot.terminal and snapshot.lifecycle != "waiting_on_user":
                lifecycle = "processing"
        turn.snapshot = replace(
            snapshot,
            announcement_sequence=decision.sequence,
            last_phrase_key=decision.phrase_key or snapshot.last_phrase_key,
            next_due_at=None,
            lifecycle=lifecycle,
            acknowledgement_started=acknowledged,
        )
        if decision.kind == "waiting":
            turn.waiting_pending = False
        if decision.terminal:
            turn.terminal_pending = False
        self._stream = decision
        self._offered = None
        self._handoff_deadline_at = None
        self._handoff_enforced = False

    def finish(self, decision: CadenceDecision, completion: PlayoutCompletion) -> None:
        """Advance cadence only from fully matched source and client finishes."""

        if decision != self._stream:
            raise StaleFence("stale_cadence_stream")
        if (
            completion.announcement_id != decision.announcement_id
            or completion.turn_id != decision.turn_id
        ):
            raise ControlProtocolError("playout_completion_mismatch")
        now = self._clock.monotonic()
        if completion.completed_monotonic > now + 1e-9:
            raise ControlProtocolError("future_playout_completion")
        self._stream = None
        turn = self._turn(decision.turn_id)
        if (
            turn.snapshot.lifecycle == "processing"
            and not turn.snapshot.muted
            and not turn.snapshot.terminal
        ):
            target = completion.completed_monotonic + CADENCE_TARGET_SECONDS
            latest = completion.completed_monotonic + CADENCE_HARD_GAP_SECONDS
            turn.target_start_monotonic = target
            turn.latest_start_monotonic = latest
            turn.snapshot = replace(
                turn.snapshot,
                next_due_at=completion.completed_at
                + timedelta(seconds=CADENCE_TARGET_SECONDS),
            )
        else:
            turn.target_start_monotonic = math.inf
            turn.latest_start_monotonic = math.inf
            turn.snapshot = replace(turn.snapshot, next_due_at=None)
        due_others = self._due_candidates(now)
        self._handoff_deadline_at = now + HANDOFF_BUDGET_SECONDS
        self._handoff_enforced = bool(due_others)

    def set_lifecycle(
        self,
        turn_id: str,
        lifecycle: str,
        *,
        waiting_reason: str | None = None,
    ) -> bool:
        """Apply a sanitized lifecycle fence; return whether speech was preempted."""

        turn = self._turn(turn_id)
        if lifecycle not in _LIFECYCLE_KIND or lifecycle == "accepted":
            raise ControlProtocolError("invalid_lifecycle_state")
        if lifecycle == "waiting_on_user":
            self._selector.select(
                lifecycle=lifecycle,
                stable_id=turn_id,
                sequence=turn.snapshot.announcement_sequence + 1,
                last_phrase_key=turn.snapshot.last_phrase_key,
                waiting_reason=waiting_reason,
            )
        elif waiting_reason is not None:
            raise ControlProtocolError("unexpected_waiting_reason")
        if turn.snapshot.terminal:
            if lifecycle == turn.snapshot.lifecycle:
                return False
            raise StaleFence("voice_turn_terminal")
        if lifecycle == "waiting_on_user" and (
            turn.snapshot.lifecycle == "waiting_on_user"
            and not turn.waiting_pending
            and waiting_reason == turn.waiting_reason
        ):
            return False
        if lifecycle == "processing" and turn.snapshot.lifecycle == "processing":
            return False
        terminal = lifecycle in self._TERMINAL_LIFECYCLES
        muted = turn.snapshot.muted
        turn.waiting_reason = waiting_reason
        turn.waiting_pending = lifecycle == "waiting_on_user" and not muted
        turn.terminal_pending = terminal and not muted
        turn.terminal_suppressed = terminal and muted
        next_due_at: datetime | None = None
        if lifecycle == "processing" and not muted:
            now_utc = self._clock.utcnow()
            now_mono = self._clock.monotonic()
            turn.target_start_monotonic = now_mono + CADENCE_TARGET_SECONDS
            turn.latest_start_monotonic = now_mono + CADENCE_HARD_GAP_SECONDS
            next_due_at = now_utc + timedelta(seconds=CADENCE_TARGET_SECONDS)
        else:
            turn.target_start_monotonic = (
                self._clock.monotonic()
                if turn.waiting_pending or turn.terminal_pending
                else math.inf
            )
            turn.latest_start_monotonic = (
                turn.target_start_monotonic + CADENCE_HARD_GAP_SECONDS
                if math.isfinite(turn.target_start_monotonic)
                else math.inf
            )
        turn.snapshot = replace(
            turn.snapshot,
            lifecycle=lifecycle,
            terminal=terminal,
            next_due_at=next_due_at,
        )
        self._cancel_offer(turn_id)
        return self._preempt_progress(turn_id)

    def set_muted(self, turn_id: str, muted: bool) -> bool:
        """Fence speech immediately; muted announcements are never burst-replayed."""

        if not isinstance(muted, bool):
            raise ValueError("invalid_speech_muted")
        turn = self._turn(turn_id)
        if turn.snapshot.muted == muted:
            return False
        turn.waiting_pending = False
        if muted and turn.terminal_pending:
            turn.terminal_suppressed = True
        turn.terminal_pending = False
        next_due_at: datetime | None = None
        if not muted and turn.snapshot.lifecycle == "processing":
            now_utc = self._clock.utcnow()
            now_mono = self._clock.monotonic()
            turn.target_start_monotonic = now_mono + CADENCE_TARGET_SECONDS
            turn.latest_start_monotonic = now_mono + CADENCE_HARD_GAP_SECONDS
            next_due_at = now_utc + timedelta(seconds=CADENCE_TARGET_SECONDS)
        else:
            turn.target_start_monotonic = math.inf
            turn.latest_start_monotonic = math.inf
        turn.snapshot = replace(
            turn.snapshot,
            muted=muted,
            next_due_at=next_due_at,
        )
        self._cancel_offer(turn_id)
        return self._preempt_stream(turn_id)

    def _decision(self, turn: _CadenceTurn) -> CadenceDecision:
        snapshot = turn.snapshot
        lifecycle = snapshot.lifecycle
        if not snapshot.acknowledgement_started:
            lifecycle = "accepted"
        elif turn.terminal_pending:
            lifecycle = snapshot.lifecycle
        elif turn.waiting_pending:
            lifecycle = "waiting_on_user"
        else:
            lifecycle = "processing"
        sequence = snapshot.announcement_sequence + 1
        selected = self._selector.select(
            lifecycle=lifecycle,
            stable_id=snapshot.turn_id,
            sequence=sequence,
            last_phrase_key=snapshot.last_phrase_key,
            waiting_reason=(
                turn.waiting_reason if lifecycle == "waiting_on_user" else None
            ),
        )
        terminal = lifecycle in self._TERMINAL_LIFECYCLES
        role = "result_opening" if selected.kind == "result" else "single"
        max_samples = (
            RESULT_OPENING_SAMPLES
            if terminal or selected.kind == "result"
            else SINGLE_SAMPLES
        )
        announcement_id = deterministic_uuid4(
            "voice-announcement-v1",
            snapshot.session_id,
            snapshot.turn_id,
            str(snapshot.generation),
            str(sequence),
            selected.kind,
            role,
            "0",
        )
        return CadenceDecision(
            announcement_id=announcement_id,
            session_id=snapshot.session_id,
            turn_id=snapshot.turn_id,
            generation=snapshot.generation,
            media_grant_revision=snapshot.media_grant_revision,
            sequence=sequence,
            kind=selected.kind,
            quantum_role=role,
            quantum_index=0,
            max_duration_samples=max_samples,
            phrase_key=selected.phrase_key,
            text=selected.text,
            target_start_monotonic=turn.target_start_monotonic,
            latest_start_monotonic=turn.latest_start_monotonic,
            terminal=terminal,
        )

    def _due_candidates(self, now: float) -> list[_CadenceTurn]:
        return [
            turn
            for turn in self._turns.values()
            if not turn.snapshot.muted
            and (
                turn.terminal_pending
                or turn.waiting_pending
                or (
                    not turn.snapshot.terminal
                    and turn.snapshot.lifecycle in {"accepted", "processing"}
                    and now + 1e-9 >= turn.target_start_monotonic
                )
            )
        ]

    def _cancel_offer(self, turn_id: str) -> None:
        if self._offered is not None and self._offered.turn_id == turn_id:
            self._offered = None

    def _preempt_progress(self, turn_id: str) -> bool:
        if (
            self._stream is not None
            and self._stream.turn_id == turn_id
            and self._stream.kind == "progress"
        ):
            return self._preempt_stream(turn_id)
        return False

    def _preempt_stream(self, turn_id: str) -> bool:
        if self._stream is None or self._stream.turn_id != turn_id:
            return False
        self._stream = None
        now = self._clock.monotonic()
        self._handoff_deadline_at = now + HANDOFF_BUDGET_SECONDS
        self._handoff_enforced = True
        return True

    def _turn(self, turn_id: str) -> _CadenceTurn:
        _uuid4(turn_id, "invalid_turn_id")
        try:
            return self._turns[turn_id]
        except KeyError:
            raise StaleFence("voice_turn_not_registered") from None


@dataclass(frozen=True, slots=True)
class AnnouncementFence:
    """Expected content-free binding shared by command, media, and observations."""

    session_id: str
    generation: int
    media_grant_revision: int
    announcement_id: str
    announcement_sequence: int
    turn_id: str | None
    kind: str
    quantum_role: str
    quantum_index: int
    result_reserved_samples_after: int | None
    max_duration_samples: int
    worker_identity: str
    device_id: str
    connection_generation: str
    transport: str

    def __post_init__(self) -> None:
        _uuid4(self.session_id, "invalid_session_id")
        _positive(self.generation, "invalid_generation")
        _positive(self.media_grant_revision, "invalid_media_grant_revision")
        _uuid4(self.announcement_id, "invalid_announcement_id")
        _positive(self.announcement_sequence, "invalid_announcement_sequence")
        if self.kind not in _ANNOUNCEMENT_KINDS:
            raise ValueError("invalid_announcement_kind")
        if self.kind == "greeting":
            if self.turn_id is not None:
                raise ValueError("invalid_announcement_turn")
        else:
            _uuid4(self.turn_id, "invalid_turn_id")
        _bounded_int(self.quantum_index, 0, 31, "invalid_quantum_index")
        _bounded_int(
            self.max_duration_samples,
            1,
            SINGLE_SAMPLES,
            "invalid_max_duration_samples",
        )
        if self.quantum_role == "single":
            if (
                self.kind == "result"
                or self.quantum_index != 0
                or self.result_reserved_samples_after is not None
            ):
                raise ValueError("invalid_single_quantum")
        elif self.quantum_role == "result_opening":
            if (
                self.kind != "result"
                or self.quantum_index != 0
                or self.max_duration_samples > RESULT_OPENING_SAMPLES
            ):
                raise ValueError("invalid_result_opening")
            _bounded_int(
                self.result_reserved_samples_after,
                1,
                RESULT_OPENING_SAMPLES,
                "invalid_result_reservation",
            )
        elif self.quantum_role == "result_continuation":
            if self.kind != "result" or self.quantum_index < 1:
                raise ValueError("invalid_result_continuation")
            _bounded_int(
                self.result_reserved_samples_after,
                1,
                MAX_RESULT_SAMPLES,
                "invalid_result_reservation",
            )
        else:
            raise ValueError("invalid_quantum_role")
        _opaque(self.worker_identity, "invalid_worker_identity")
        _uuid4(self.device_id, "invalid_device_id")
        _uuid4(self.connection_generation, "invalid_connection_generation")
        if self.transport not in {"livekit", "watch_pcm_websocket"}:
            raise ValueError("invalid_voice_transport")


@dataclass(frozen=True, slots=True)
class PlayoutHealth:
    status: str
    reason: str | None
    completion: PlayoutCompletion | None


@dataclass(slots=True)
class _PlayoutRecord:
    fence: AnnouncementFence
    registered_at: datetime
    registered_monotonic: float
    manifest: Mapping[str, Any] | None = None
    manifest_received_at: datetime | None = None
    source_phase: str | None = None
    client_phase: str | None = None
    source_finished_at: datetime | None = None
    source_finished_monotonic: float | None = None
    client_finished_at: datetime | None = None
    client_finished_monotonic: float | None = None
    completion: PlayoutCompletion | None = None
    degraded_reason: str | None = None


class PlayoutEvidenceTracker:
    """Strict source/client observation validator; receipt time drives cadence."""

    def __init__(
        self,
        clock: CoordinatorClock,
        *,
        missing_event_timeout_seconds: float = 5,
    ) -> None:
        if not isinstance(clock, CoordinatorClock):
            raise TypeError("clock must be CoordinatorClock")
        if (
            isinstance(missing_event_timeout_seconds, bool)
            or not isinstance(missing_event_timeout_seconds, (int, float))
            or not math.isfinite(float(missing_event_timeout_seconds))
            or not 1 <= float(missing_event_timeout_seconds) <= 30
        ):
            raise ValueError("invalid_missing_event_timeout")
        self._clock = clock
        self._missing_timeout = float(missing_event_timeout_seconds)
        self._records: dict[str, _PlayoutRecord] = {}
        self._source_sequences: dict[tuple[str, str, int], int] = {}
        self._client_sequences: dict[tuple[str, str], int] = {}
        self._client_event_times: dict[tuple[str, str], deque[float]] = {}

    def register(self, fence: AnnouncementFence) -> None:
        """Register one exact command fence before accepting manifest/events."""

        if not isinstance(fence, AnnouncementFence):
            raise TypeError("fence must be AnnouncementFence")
        if fence.announcement_id in self._records:
            raise ControlProtocolError("announcement_already_registered")
        self._records[fence.announcement_id] = _PlayoutRecord(
            fence=fence,
            registered_at=self._clock.utcnow(),
            registered_monotonic=self._clock.monotonic(),
        )

    def record_manifest(self, frame: Mapping[str, Any]) -> None:
        """Accept one bounded media manifest before any renderable audio."""

        if _json_frame_size(frame, "invalid_announcement_manifest") > (
            MAX_ANNOUNCEMENT_MANIFEST_BYTES
        ):
            raise ControlProtocolError("announcement_manifest_too_large")
        if not isinstance(frame, Mapping):
            raise ControlProtocolError("invalid_announcement_manifest")
        record = self._record(frame.get("announcement_id"))
        self._assert_not_degraded(record)
        if record.manifest is not None:
            raise ControlProtocolError("duplicate_manifest")
        fence = record.fence
        common = {
            "type",
            "schema_version",
            "session_id",
            "generation",
            "media_grant_revision",
            "announcement_id",
            "announcement_sequence",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "transport",
            "worker_identity",
            "sample_rate_hz",
            "duration_samples",
        }
        if fence.result_reserved_samples_after is not None:
            common.add("result_reserved_samples_after")
        locator = (
            {"track_sid", "track_name"}
            if fence.transport == "livekit"
            else {"first_media_sequence", "last_media_sequence"}
        )
        if set(frame) != common | locator:
            raise ControlProtocolError("invalid_announcement_manifest_fields")
        if (
            frame["type"] != "voice_announcement_media"
            or frame["schema_version"] != "1"
        ):
            raise ControlProtocolError("invalid_announcement_manifest")
        self._validate_fences(frame, fence)
        if frame["transport"] != fence.transport:
            raise ControlProtocolError("playout_fence_mismatch")
        if frame["worker_identity"] != fence.worker_identity:
            raise ControlProtocolError("worker_identity_mismatch")
        if frame["sample_rate_hz"] != FIXED_VOICE_PROFILE["sample_rate_hz"]:
            raise ControlProtocolError("invalid_sample_rate")
        duration = _bounded_int(
            frame["duration_samples"],
            1,
            2**63 - 1,
            "invalid_duration_samples",
            ControlProtocolError,
        )
        if duration > SINGLE_SAMPLES or duration > fence.max_duration_samples:
            raise ControlProtocolError("manifest_sample_budget_exceeded")
        if fence.quantum_role == "result_opening" and duration > RESULT_OPENING_SAMPLES:
            raise ControlProtocolError("manifest_sample_budget_exceeded")
        self._validate_result_reservation(frame, fence)
        if fence.transport == "livekit":
            _opaque(frame["track_sid"], "invalid_track_sid", ControlProtocolError)
            _opaque(frame["track_name"], "invalid_track_name", ControlProtocolError)
        else:
            first = _bounded_int(
                frame["first_media_sequence"],
                0,
                2**63 - 1,
                "invalid_media_sequence",
                ControlProtocolError,
            )
            last = _bounded_int(
                frame["last_media_sequence"],
                first,
                2**63 - 1,
                "invalid_media_sequence",
                ControlProtocolError,
            )
            if last - first + 1 != duration:
                raise ControlProtocolError("watch_sample_range_mismatch")
        record.manifest = MappingProxyType(dict(frame))
        record.manifest_received_at = self._clock.utcnow()

    def record_source(
        self,
        frame: Mapping[str, Any],
        *,
        worker_identity: str,
    ) -> PlayoutCompletion | None:
        """Validate an authenticated worker lifecycle event and its exact order."""

        if _json_frame_size(frame, "invalid_source_event") > MAX_CONTROL_FRAME_BYTES:
            raise ControlProtocolError("source_event_too_large")
        if not isinstance(frame, Mapping):
            raise ControlProtocolError("invalid_source_event")
        record = self._record(frame.get("announcement_id"))
        self._assert_not_degraded(record)
        fence = record.fence
        worker_identity = _opaque(
            worker_identity,
            "invalid_worker_identity",
            ControlProtocolError,
        )
        if worker_identity != fence.worker_identity:
            raise ControlProtocolError("worker_identity_mismatch")
        required = {
            "type",
            "schema_version",
            "message_id",
            "session_id",
            "generation",
            "sequence",
            "sent_at",
            "announcement_id",
            "announcement_sequence",
            "media_grant_revision",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "occurred_at",
        }
        if fence.result_reserved_samples_after is not None:
            required.add("result_reserved_samples_after")
        allowed = required | {"reason", "duration_ms"}
        if not required.issubset(frame) or not set(frame).issubset(allowed):
            raise ControlProtocolError("invalid_source_event_fields")
        _validate_worker_base(frame)
        self._validate_fences(frame, fence)
        self._validate_result_reservation(frame, fence)
        _parse_timestamp(
            frame["occurred_at"], "invalid_occurred_at", ControlProtocolError
        )
        event_type = frame["type"]
        if event_type not in {
            "speech_started",
            "speech_finished",
            "speech_interrupted",
            "speech_failed",
        }:
            raise ControlProtocolError("invalid_source_event_type")
        phase = event_type.removeprefix("speech_")
        if "reason" in frame and (
            not isinstance(frame["reason"], str)
            or _REASON.fullmatch(frame["reason"]) is None
        ):
            raise ControlProtocolError("invalid_source_event_reason")
        if "duration_ms" in frame:
            duration_limit = 1_500 if fence.quantum_role == "result_opening" else 4_000
            _bounded_int(
                frame["duration_ms"],
                0,
                duration_limit,
                "invalid_source_duration",
                ControlProtocolError,
            )
        self._validate_source_phase(record, phase)
        sequence_key = (worker_identity, fence.session_id, fence.generation)
        expected_sequence = self._source_sequences.get(sequence_key, -1) + 1
        if frame["sequence"] != expected_sequence:
            raise ControlProtocolError("source_sequence_out_of_order")
        receipt_at = self._clock.utcnow()
        receipt_mono = self._clock.monotonic()
        self._source_sequences[sequence_key] = expected_sequence
        record.source_phase = phase
        if phase == "finished":
            record.source_finished_at = receipt_at
            record.source_finished_monotonic = receipt_mono
        return self._maybe_complete(record)

    def record_client(self, frame: Mapping[str, Any]) -> PlayoutCompletion | None:
        """Validate a content-free owner-bound local-render observation."""

        if _json_frame_size(frame, "invalid_client_playout_event") > (
            MAX_CLIENT_PLAYOUT_FRAME_BYTES
        ):
            raise ControlProtocolError("client_frame_too_large")
        if not isinstance(frame, Mapping):
            raise ControlProtocolError("invalid_client_playout_event")
        record = self._record(frame.get("announcement_id"))
        self._assert_not_degraded(record)
        fence = record.fence
        required = {
            "type",
            "schema_version",
            "device_id",
            "connection_generation",
            "session_id",
            "generation",
            "media_grant_revision",
            "announcement_id",
            "announcement_sequence",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "phase",
            "client_sequence",
            "observed_at",
        }
        if fence.result_reserved_samples_after is not None:
            required.add("result_reserved_samples_after")
        if set(frame) != required:
            raise ControlProtocolError("invalid_client_playout_fields")
        if frame["type"] != "voice_playout_event" or frame["schema_version"] != "1":
            raise ControlProtocolError("invalid_client_playout_event")
        _uuid4(frame["device_id"], "invalid_device_id", ControlProtocolError)
        _uuid4(
            frame["connection_generation"],
            "invalid_connection_generation",
            ControlProtocolError,
        )
        if (
            frame["device_id"] != fence.device_id
            or frame["connection_generation"] != fence.connection_generation
        ):
            raise ControlProtocolError("owner_connection_mismatch")
        self._validate_fences(frame, fence)
        self._validate_result_reservation(frame, fence)
        _parse_timestamp(
            frame["observed_at"], "invalid_observed_at", ControlProtocolError
        )
        phase = frame["phase"]
        if phase not in {"started", "finished", "interrupted"}:
            raise ControlProtocolError("invalid_client_playout_phase")
        self._validate_client_phase(record, phase)
        sequence = _bounded_int(
            frame["client_sequence"],
            0,
            2**63 - 1,
            "invalid_client_sequence",
            ControlProtocolError,
        )
        sequence_key = (fence.device_id, fence.connection_generation)
        if sequence <= self._client_sequences.get(sequence_key, -1):
            raise ControlProtocolError("client_sequence_out_of_order")
        receipt_mono = self._clock.monotonic()
        self._check_client_rate(sequence_key, receipt_mono)
        receipt_at = self._clock.utcnow()
        self._client_sequences[sequence_key] = sequence
        record.client_phase = phase
        if phase == "finished":
            record.client_finished_at = receipt_at
            record.client_finished_monotonic = receipt_mono
        return self._maybe_complete(record)

    def expire_missing(self) -> tuple[str, ...]:
        """Degrade announcements whose required evidence never arrived."""

        now = self._clock.monotonic()
        expired: list[str] = []
        for announcement_id, record in self._records.items():
            if (
                record.completion is not None
                or record.degraded_reason is not None
                or record.source_phase in {"interrupted", "failed"}
                or record.client_phase == "interrupted"
                or now < record.registered_monotonic + self._missing_timeout
            ):
                continue
            record.degraded_reason = self._missing_reason(record)
            expired.append(announcement_id)
        return tuple(sorted(expired))

    def health(self, announcement_id: str) -> PlayoutHealth:
        record = self._record(announcement_id)
        if record.degraded_reason is not None:
            return PlayoutHealth("degraded", record.degraded_reason, None)
        if record.completion is not None:
            return PlayoutHealth("completed", None, record.completion)
        if record.source_phase == "failed":
            return PlayoutHealth("failed", "source_failed", None)
        if record.source_phase == "interrupted" or record.client_phase == "interrupted":
            return PlayoutHealth("interrupted", "playout_interrupted", None)
        return PlayoutHealth("pending", None, None)

    def _record(self, announcement_id: Any) -> _PlayoutRecord:
        _uuid4(
            announcement_id,
            "invalid_announcement_id",
            ControlProtocolError,
        )
        try:
            return self._records[announcement_id]
        except KeyError:
            raise ControlProtocolError("unknown_announcement") from None

    @staticmethod
    def _assert_not_degraded(record: _PlayoutRecord) -> None:
        if record.degraded_reason is not None:
            raise ControlProtocolError("playout_degraded")
        if record.completion is not None:
            raise ControlProtocolError("playout_already_completed")

    @staticmethod
    def _validate_fences(frame: Mapping[str, Any], fence: AnnouncementFence) -> None:
        if frame.get("generation") != fence.generation:
            raise ControlProtocolError("stale_generation")
        if frame.get("media_grant_revision") != fence.media_grant_revision:
            raise ControlProtocolError("stale_media_grant_revision")
        expected = {
            "session_id": fence.session_id,
            "announcement_id": fence.announcement_id,
            "announcement_sequence": fence.announcement_sequence,
            "turn_id": fence.turn_id,
            "kind": fence.kind,
            "quantum_role": fence.quantum_role,
            "quantum_index": fence.quantum_index,
        }
        if any(frame.get(key) != value for key, value in expected.items()):
            raise ControlProtocolError("playout_fence_mismatch")

    @staticmethod
    def _validate_result_reservation(
        frame: Mapping[str, Any], fence: AnnouncementFence
    ) -> None:
        if frame.get("result_reserved_samples_after") != (
            fence.result_reserved_samples_after
        ):
            raise ControlProtocolError("result_reservation_mismatch")

    @staticmethod
    def _validate_source_phase(record: _PlayoutRecord, phase: str) -> None:
        if phase == "failed":
            if record.source_phase is not None:
                raise ControlProtocolError("source_event_out_of_order")
            return
        if record.manifest is None:
            raise ControlProtocolError("manifest_required")
        if phase == "started":
            if record.source_phase is not None:
                raise ControlProtocolError("source_event_out_of_order")
        elif phase in {"finished", "interrupted"}:
            if record.source_phase != "started":
                raise ControlProtocolError("source_event_out_of_order")
        else:  # pragma: no cover - type enum is checked by the caller.
            raise ControlProtocolError("invalid_source_event_type")

    @staticmethod
    def _validate_client_phase(record: _PlayoutRecord, phase: str) -> None:
        if record.manifest is None:
            raise ControlProtocolError("manifest_required")
        if phase == "started":
            if record.client_phase is not None:
                raise ControlProtocolError("client_playout_out_of_order")
        elif record.client_phase != "started":
            raise ControlProtocolError("client_playout_out_of_order")

    def _check_client_rate(self, sequence_key: tuple[str, str], now: float) -> None:
        events = self._client_event_times.setdefault(sequence_key, deque())
        cutoff = now - 1.0
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= MAX_CLIENT_PLAYOUT_EVENTS_PER_SECOND:
            raise ControlProtocolError("client_playout_rate_exceeded")
        events.append(now)

    @staticmethod
    def _missing_reason(record: _PlayoutRecord) -> str:
        if record.manifest is None:
            return "missing_manifest"
        if record.source_phase is None:
            return "missing_source_start"
        if record.client_phase is None:
            return "missing_client_start"
        if record.source_finished_at is None:
            return "missing_source_finish"
        if record.client_finished_at is None:
            return "missing_client_finish"
        return "missing_playout_evidence"

    @staticmethod
    def _maybe_complete(record: _PlayoutRecord) -> PlayoutCompletion | None:
        if record.completion is not None:
            return record.completion
        if (
            record.source_finished_at is None
            or record.source_finished_monotonic is None
            or record.client_finished_at is None
            or record.client_finished_monotonic is None
        ):
            return None
        completed_at = max(
            record.source_finished_at,
            record.client_finished_at,
        )
        record.completion = PlayoutCompletion(
            announcement_id=record.fence.announcement_id,
            turn_id=record.fence.turn_id,
            source_finished_at=record.source_finished_at,
            client_finished_at=record.client_finished_at,
            completed_at=completed_at,
            completed_monotonic=max(
                record.source_finished_monotonic,
                record.client_finished_monotonic,
            ),
        )
        return record.completion


@dataclass(frozen=True, slots=True)
class AnnouncementState:
    """Content-free voice_turn scheduling columns under a row lock."""

    generation: int
    announcement_sequence: int = 0
    result_reserved_samples: int = 0
    result_quantum_count: int = 0
    last_announcement_kind: str | None = None
    last_phrase_key: str | None = None
    announcement_claim_id: str | None = None
    announcement_claim_expires_at: datetime | None = None
    terminal: bool = False
    speech_enabled: bool = True
    origin_available: bool = True

    def __post_init__(self) -> None:
        _positive(self.generation, "invalid_generation")
        _bounded_int(
            self.announcement_sequence,
            0,
            2**63 - 1,
            "invalid_announcement_sequence",
        )
        _bounded_int(
            self.result_reserved_samples,
            0,
            MAX_RESULT_SAMPLES,
            "invalid_result_reservation",
        )
        _bounded_int(
            self.result_quantum_count,
            0,
            MAX_RESULT_QUANTA,
            "invalid_result_quantum_count",
        )
        if self.last_announcement_kind is not None and (
            self.last_announcement_kind not in _ANNOUNCEMENT_KINDS
        ):
            raise ValueError("invalid_last_announcement_kind")
        if self.last_phrase_key is not None and (
            _PHRASE_KEY.fullmatch(self.last_phrase_key) is None
        ):
            raise ValueError("invalid_last_phrase_key")
        if (self.announcement_claim_id is None) != (
            self.announcement_claim_expires_at is None
        ):
            raise ValueError("invalid_announcement_claim_state")
        if self.announcement_claim_id is not None:
            _uuid4(self.announcement_claim_id, "invalid_announcement_claim_id")
            _aware(
                self.announcement_claim_expires_at,
                "invalid_announcement_claim_expiry",
            )


@dataclass(frozen=True, slots=True)
class AnnouncementClaimRequest:
    session_id: str
    turn_id: str
    generation: int
    claim_id: str
    kind: str
    quantum_role: str
    expected_sequence: int
    expected_result_reserved_samples: int
    expected_phrase_key: str | None = None
    authorized_terminal_sensitive_recap: bool = False
    expected_media_grant_revision: int | None = None
    authorized_preacceptance_rejection_reason: str | None = None

    def __post_init__(self) -> None:
        _uuid4(self.session_id, "invalid_session_id")
        _uuid4(self.turn_id, "invalid_turn_id")
        _positive(self.generation, "invalid_generation")
        _uuid4(self.claim_id, "invalid_announcement_claim_id")
        if self.kind not in _ANNOUNCEMENT_KINDS or self.kind == "greeting":
            raise ValueError("invalid_announcement_kind")
        if self.quantum_role not in {
            "single",
            "result_opening",
            "result_continuation",
        }:
            raise ValueError("invalid_quantum_role")
        _bounded_int(
            self.expected_sequence,
            0,
            2**63 - 1,
            "invalid_expected_sequence",
        )
        _bounded_int(
            self.expected_result_reserved_samples,
            0,
            MAX_RESULT_SAMPLES,
            "invalid_expected_reservation",
        )
        if not isinstance(self.authorized_terminal_sensitive_recap, bool):
            raise ValueError("invalid_sensitive_recap_authorization")
        if self.authorized_terminal_sensitive_recap and self.kind != "result":
            raise ValueError("invalid_sensitive_recap_authorization")
        if self.expected_phrase_key is not None and (
            _PHRASE_KIND_BY_KEY.get(self.expected_phrase_key) != self.kind
        ):
            raise ValueError("invalid_expected_phrase_key")
        if self.expected_media_grant_revision is not None:
            _positive(
                self.expected_media_grant_revision,
                "invalid_expected_media_grant_revision",
            )
        reason = self.authorized_preacceptance_rejection_reason
        if reason is not None:
            if self.authorized_terminal_sensitive_recap:
                raise ValueError("conflicting_announcement_authorization")
            try:
                expected_kind, expected_phrase_key = (
                    PREACCEPTANCE_REJECTION_PHRASES[reason]
                )
            except (KeyError, TypeError):
                raise ValueError("invalid_preacceptance_rejection_reason") from None
            if (
                self.expected_media_grant_revision is None
                or self.kind != expected_kind
                or self.quantum_role != "single"
                or self.expected_phrase_key != expected_phrase_key
            ):
                raise ValueError("invalid_preacceptance_rejection_authorization")


@dataclass(frozen=True, slots=True)
class AnnouncementClaim:
    claim_id: str
    announcement_id: str
    sequence: int
    kind: str
    quantum_role: str
    quantum_index: int
    max_duration_samples: int
    result_reserved_samples_after: int | None
    phrase_key: str | None
    claim_expires_at: datetime
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class AnnouncementMutation:
    state: AnnouncementState
    claim: AnnouncementClaim


class AnnouncementStateAdapter:
    """Pure row-lock/CAS announcement reservation and crash-recovery rules."""

    def __init__(self, phrase_book: PhraseBook, *, claim_ttl_seconds: int = 5) -> None:
        if not 1 <= claim_ttl_seconds <= 30:
            raise ValueError("invalid_announcement_claim_ttl")
        self._phrase_book = phrase_book
        self._claim_ttl = timedelta(seconds=claim_ttl_seconds)

    def claim(
        self,
        state: AnnouncementState,
        request: AnnouncementClaimRequest,
        *,
        now: datetime,
    ) -> AnnouncementMutation:
        now = _aware(now, "invalid_claim_time")
        self._validate_claim_fences(state, request)
        if state.announcement_claim_id is not None:
            assert state.announcement_claim_expires_at is not None
            if now < state.announcement_claim_expires_at:
                if state.announcement_claim_id != request.claim_id:
                    raise ClaimUnavailable("announcement_claim_owned")
                role, _index, _samples, _reserved = self._current_quantum(state)
                if (
                    request.kind != state.last_announcement_kind
                    or request.quantum_role != role
                    or (
                        request.expected_phrase_key is not None
                        and request.expected_phrase_key != state.last_phrase_key
                    )
                ):
                    raise ClaimUnavailable("announcement_claim_mismatch")
                return AnnouncementMutation(
                    state=state,
                    claim=self._existing_claim(
                        state, request, state.announcement_claim_expires_at
                    ),
                )
            return self._recover_expired_claim(state, request, now)
        return self._reserve_new_claim(state, request, now)

    def complete(
        self, state: AnnouncementState, *, generation: int, claim_id: str
    ) -> AnnouncementState:
        _uuid4(claim_id, "invalid_announcement_claim_id")
        if generation != state.generation:
            raise StaleFence("stale_generation")
        if state.announcement_claim_id != claim_id:
            raise ClaimUnavailable("announcement_claim_not_owned")
        return replace(
            state,
            announcement_claim_id=None,
            announcement_claim_expires_at=None,
        )

    def _validate_claim_fences(
        self, state: AnnouncementState, request: AnnouncementClaimRequest
    ) -> None:
        if request.generation != state.generation:
            raise StaleFence("stale_generation")
        if state.terminal:
            raise ClaimUnavailable("announcement_terminal")
        if not state.speech_enabled:
            raise ClaimUnavailable("speech_disabled")
        if not state.origin_available:
            raise ClaimUnavailable("origin_unavailable")
        if (
            request.expected_sequence != state.announcement_sequence
            or request.expected_result_reserved_samples != state.result_reserved_samples
        ):
            raise ClaimUnavailable("announcement_cas_miss")

    def _reserve_new_claim(
        self,
        state: AnnouncementState,
        request: AnnouncementClaimRequest,
        now: datetime,
    ) -> AnnouncementMutation:
        sequence = state.announcement_sequence + 1
        if request.kind == "result":
            expected_role = (
                "result_opening"
                if state.result_quantum_count == 0
                else "result_continuation"
            )
            if request.quantum_role != expected_role:
                raise ClaimUnavailable("invalid_result_quantum")
            quantum_index = state.result_quantum_count
            max_samples = (
                RESULT_OPENING_SAMPLES if quantum_index == 0 else CONTINUATION_SAMPLES
            )
            if quantum_index >= MAX_RESULT_QUANTA:
                raise ClaimUnavailable("result_quantum_budget_exhausted")
            reserved_after = state.result_reserved_samples + max_samples
            if reserved_after > MAX_RESULT_SAMPLES:
                raise ClaimUnavailable("result_sample_budget_exhausted")
            claim_phrase_key = None
            next_phrase_key = state.last_phrase_key
            next_count = state.result_quantum_count + 1
        else:
            if request.quantum_role != "single":
                raise ClaimUnavailable("invalid_single_quantum")
            quantum_index = 0
            max_samples = (
                RESULT_OPENING_SAMPLES
                if request.kind in _SHORT_TERMINAL_KINDS
                else SINGLE_SAMPLES
            )
            reserved_after = None
            next_count = state.result_quantum_count
            claim_phrase_key = request.expected_phrase_key
            if claim_phrase_key is None:
                claim_phrase_key = self._phrase_book.select(
                    kind=request.kind,
                    stable_id=request.turn_id,
                    sequence=sequence,
                    last_phrase_key=state.last_phrase_key,
                )
            next_phrase_key = claim_phrase_key
        announcement_id = _announcement_id(
            request, sequence, request.quantum_role, quantum_index
        )
        expires_at = now + self._claim_ttl
        next_state = replace(
            state,
            announcement_sequence=sequence,
            result_reserved_samples=(
                reserved_after
                if reserved_after is not None
                else state.result_reserved_samples
            ),
            result_quantum_count=next_count,
            last_announcement_kind=request.kind,
            last_phrase_key=next_phrase_key,
            announcement_claim_id=request.claim_id,
            announcement_claim_expires_at=expires_at,
        )
        return AnnouncementMutation(
            state=next_state,
            claim=AnnouncementClaim(
                claim_id=request.claim_id,
                announcement_id=announcement_id,
                sequence=sequence,
                kind=request.kind,
                quantum_role=request.quantum_role,
                quantum_index=quantum_index,
                max_duration_samples=max_samples,
                result_reserved_samples_after=reserved_after,
                phrase_key=claim_phrase_key,
                claim_expires_at=expires_at,
            ),
        )

    def _recover_expired_claim(
        self,
        state: AnnouncementState,
        request: AnnouncementClaimRequest,
        now: datetime,
    ) -> AnnouncementMutation:
        if request.kind != state.last_announcement_kind:
            raise ClaimUnavailable("claim_recovery_required")
        if (
            request.expected_phrase_key is not None
            and request.expected_phrase_key != state.last_phrase_key
        ):
            raise ClaimUnavailable("claim_recovery_required")
        role, _index, _samples, _reserved = self._current_quantum(state)
        if request.quantum_role != role:
            raise ClaimUnavailable("claim_recovery_required")
        expires_at = now + self._claim_ttl
        next_state = replace(
            state,
            announcement_claim_id=request.claim_id,
            announcement_claim_expires_at=expires_at,
        )
        claim = self._existing_claim(next_state, request, expires_at)
        return AnnouncementMutation(
            state=next_state, claim=replace(claim, recovered=True)
        )

    def _existing_claim(
        self,
        state: AnnouncementState,
        request: AnnouncementClaimRequest,
        expires_at: datetime,
    ) -> AnnouncementClaim:
        role, index, max_samples, reserved_after = self._current_quantum(state)
        return AnnouncementClaim(
            claim_id=request.claim_id,
            announcement_id=_announcement_id(
                request, state.announcement_sequence, role, index
            ),
            sequence=state.announcement_sequence,
            kind=request.kind,
            quantum_role=role,
            quantum_index=index,
            max_duration_samples=max_samples,
            result_reserved_samples_after=reserved_after,
            phrase_key=(
                None
                if state.last_announcement_kind == "result"
                else state.last_phrase_key
            ),
            claim_expires_at=expires_at,
        )

    def _current_quantum(
        self, state: AnnouncementState
    ) -> tuple[str, int, int, int | None]:
        if state.last_announcement_kind == "result":
            if state.result_quantum_count < 1:
                raise ClaimUnavailable("invalid_recovery_state")
            index = state.result_quantum_count - 1
            role = "result_opening" if index == 0 else "result_continuation"
            samples = RESULT_OPENING_SAMPLES if index == 0 else CONTINUATION_SAMPLES
            return role, index, samples, state.result_reserved_samples
        samples = (
            RESULT_OPENING_SAMPLES
            if state.last_announcement_kind in _SHORT_TERMINAL_KINDS
            else SINGLE_SAMPLES
        )
        return "single", 0, samples, None


class VoiceCoordinatorRepository(Protocol):
    """Atomic PostgreSQL seam; implementations apply the pure adapters above."""

    async def claim_control_lease(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        owner_id: str,
        now: datetime,
    ) -> ControlLeaseState: ...

    async def release_control_lease(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        owner_id: str,
    ) -> bool: ...

    async def claim_announcement(
        self,
        *,
        user_id: str,
        request: AnnouncementClaimRequest,
        now: datetime,
    ) -> AnnouncementMutation: ...

    async def complete_announcement(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        generation: int,
        claim_id: str,
    ) -> bool: ...

    async def bind_worker_recognition(
        self,
        *,
        start: RecognitionStart,
        control_owner_id: str,
        now: datetime,
    ) -> Any: ...

    async def reject_worker_recognition(
        self,
        *,
        binding: TranscriptTurnBinding,
        control_owner_id: str,
        now: datetime,
    ) -> Any: ...

    async def suppress_worker_self_speech(
        self,
        *,
        binding: TranscriptTurnBinding,
        control_owner_id: str,
        now: datetime,
    ) -> Any: ...


class VoiceCoordinator:
    """Thin authority facade joining worker-pool and durable claim seams."""

    def __init__(
        self,
        worker_pool: WorkerPool,
        repository: VoiceCoordinatorRepository,
        *,
        replica_id: str,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self.worker_pool = worker_pool
        self._repository = repository
        self._replica_id = _opaque(replica_id, "invalid_replica_id")
        self._utcnow = utcnow or (lambda: datetime.now(UTC))

    @property
    def replica_id(self) -> str:
        """Return the non-secret durable coordinator ownership identity."""

        return self._replica_id

    async def claim_session_control(
        self, *, user_id: str, session_id: str, generation: int
    ) -> ControlLeaseState:
        """Claim/renew one DB control lease and verify the returned fence."""

        user_id = _user_id(user_id)
        _uuid4(session_id, "invalid_session_id")
        _positive(generation, "invalid_generation")
        now = _aware(self._utcnow(), "invalid_coordinator_clock")
        try:
            state = await self._repository.claim_control_lease(
                user_id=user_id,
                session_id=session_id,
                generation=generation,
                owner_id=self._replica_id,
                now=now,
            )
        except VoiceCoordinatorError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ClaimUnavailable("coordinator_repository_failed") from None
        if (
            not isinstance(state, ControlLeaseState)
            or state.generation != generation
            or state.owner_id != self._replica_id
            or state.expires_at is None
            or not now < state.expires_at <= now + timedelta(seconds=60)
        ):
            raise ClaimUnavailable("invalid_repository_control_claim")
        return state

    async def release_session_control(
        self, *, user_id: str, session_id: str, generation: int
    ) -> bool:
        """Release only this replica's exact durable session fence."""

        user_id = _user_id(user_id)
        _uuid4(session_id, "invalid_session_id")
        _positive(generation, "invalid_generation")
        try:
            result = await self._repository.release_control_lease(
                user_id=user_id,
                session_id=session_id,
                generation=generation,
                owner_id=self._replica_id,
            )
        except VoiceCoordinatorError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ClaimUnavailable("coordinator_repository_failed") from None
        if not isinstance(result, bool):
            raise ClaimUnavailable("invalid_repository_release")
        return result

    async def bind_recognition_started(
        self,
        frame: Mapping[str, Any],
    ) -> TranscriptTurnBinding:
        """Durably bind one accepted recognition frame, then notify its worker."""

        if not isinstance(frame, Mapping) or frame.get("type") != "recognition_started":
            raise ControlProtocolError("invalid_recognition_started_frame")
        session_id = _uuid4(
            frame.get("session_id"),
            "invalid_session_id",
            ControlProtocolError,
        )
        generation = _positive(
            frame.get("generation"),
            "invalid_generation",
            ControlProtocolError,
        )
        reservation = await self.worker_pool.current_reservation(
            session_id=session_id,
            generation=generation,
        )
        try:
            start = RecognitionStart(
                session_id=session_id,
                generation=generation,
                assignment_id=reservation.assignment_id,
                worker_identity=reservation.worker_identity,
                client_turn_id=frame.get("client_turn_id"),
                media_grant_revision=frame.get("media_grant_revision"),
                chat_id=frame.get("visible_chat_id"),
                chat_context_revision=frame.get("chat_context_revision"),
            )
        except (TypeError, ValueError):
            raise ControlProtocolError("invalid_recognition_started_frame") from None
        now = _aware(self._utcnow(), "invalid_coordinator_clock")
        try:
            mutation = await self._repository.bind_worker_recognition(
                start=start,
                control_owner_id=self._replica_id,
                now=now,
            )
        except VoiceCoordinatorError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ClaimUnavailable("coordinator_repository_failed") from None
        turn = getattr(mutation, "turn", None)
        binding = TranscriptTurnBinding.from_turn(
            turn,
            assignment_id=reservation.assignment_id,
            worker_identity=reservation.worker_identity,
        )
        if (
            binding.session_id != start.session_id
            or binding.generation != start.generation
            or binding.media_grant_revision != start.media_grant_revision
            or binding.client_turn_id != start.client_turn_id
            or binding.chat_id != start.chat_id
            or binding.chat_context_revision != start.chat_context_revision
        ):
            raise ClaimUnavailable("invalid_repository_recognition_binding")
        await self.worker_pool.send_session_command(
            reservation,
            "turn_bound",
            {
                "client_turn_id": binding.client_turn_id,
                "turn_id": binding.turn_id,
                "chat_id": binding.chat_id,
                "chat_context_revision": binding.chat_context_revision,
                "media_grant_revision": binding.media_grant_revision,
                "submission_id": binding.submission_id,
                "request_generation": binding.request_generation,
            },
        )
        return binding

    async def reject_recognition_failed(
        self,
        frame: Mapping[str, Any],
    ) -> Any:
        """Durably abandon one authenticated ASR failure, then clear its fence."""

        if not isinstance(frame, Mapping) or frame.get("type") != "recognition_failed":
            raise ControlProtocolError("invalid_recognition_failed_frame")
        session_id = _uuid4(
            frame.get("session_id"),
            "invalid_session_id",
            ControlProtocolError,
        )
        generation = _positive(
            frame.get("generation"),
            "invalid_generation",
            ControlProtocolError,
        )
        client_turn_id = _uuid4(
            frame.get("client_turn_id"),
            "invalid_client_turn_id",
            ControlProtocolError,
        )
        if frame.get("reason") not in _RECOGNITION_FAILURE_REASONS - {"self_speech"}:
            raise ControlProtocolError("invalid_recognition_failure_reason")
        binding = await self.worker_pool.current_recognition_binding(
            session_id=session_id,
            generation=generation,
            client_turn_id=client_turn_id,
        )
        now = _aware(self._utcnow(), "invalid_coordinator_clock")
        try:
            mutation = await self._repository.reject_worker_recognition(
                binding=binding,
                control_owner_id=self._replica_id,
                now=now,
            )
        except VoiceCoordinatorError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ClaimUnavailable("coordinator_repository_failed") from None
        turn = getattr(mutation, "turn", None)
        returned = TranscriptTurnBinding.from_turn(
            turn,
            assignment_id=binding.assignment_id,
            worker_identity=binding.worker_identity,
        )
        if (
            returned != binding
            or getattr(turn, "state", None) != "abandoned"
            or getattr(turn, "rejection_reason", None) != "malformed_final"
            or getattr(turn, "rejection_retry_policy", None)
            != "explicit_user_retry"
        ):
            raise ClaimUnavailable("invalid_repository_recognition_rejection")
        await self.emit_transcript_rejected(
            turn,
            reason="malformed_final",
            retry_policy="explicit_user_retry",
        )
        return mutation

    async def suppress_self_speech(
        self,
        frame: Mapping[str, Any],
    ) -> Any:
        """Durably suppress authenticated playback recognition without output."""

        if not isinstance(frame, Mapping) or frame.get("type") != "recognition_failed":
            raise ControlProtocolError("invalid_recognition_failed_frame")
        session_id = _uuid4(
            frame.get("session_id"),
            "invalid_session_id",
            ControlProtocolError,
        )
        generation = _positive(
            frame.get("generation"),
            "invalid_generation",
            ControlProtocolError,
        )
        client_turn_id = _uuid4(
            frame.get("client_turn_id"),
            "invalid_client_turn_id",
            ControlProtocolError,
        )
        if frame.get("reason") != "self_speech":
            raise ControlProtocolError("invalid_recognition_failure_reason")
        binding = await self.worker_pool.current_recognition_binding(
            session_id=session_id,
            generation=generation,
            client_turn_id=client_turn_id,
        )
        now = _aware(self._utcnow(), "invalid_coordinator_clock")
        try:
            mutation = await self._repository.suppress_worker_self_speech(
                binding=binding,
                control_owner_id=self._replica_id,
                now=now,
            )
        except VoiceCoordinatorError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ClaimUnavailable("coordinator_repository_failed") from None
        turn = getattr(mutation, "turn", None)
        returned = TranscriptTurnBinding.from_turn(
            turn,
            assignment_id=binding.assignment_id,
            worker_identity=binding.worker_identity,
        )
        if (
            returned != binding
            or getattr(turn, "state", None) != "abandoned"
            or getattr(turn, "rejection_reason", None) != "malformed_final"
            or getattr(turn, "rejection_retry_policy", None) != "none"
        ):
            raise ClaimUnavailable("invalid_repository_self_speech_suppression")
        await self.worker_pool.clear_suppressed_recognition(binding)
        return mutation

    async def emit_transcript_accepted(
        self,
        turn: Any,
        *,
        accepted_message_id: int,
    ) -> dict[str, Any]:
        """Send the post-commit, fully correlated acceptance disposition."""

        _positive(
            accepted_message_id,
            "invalid_accepted_message_id",
            ControlProtocolError,
        )
        binding, reservation = await self._current_transcript_binding(turn)
        return await self.worker_pool.send_session_command(
            reservation,
            "transcript_accepted",
            {
                **self._disposition_fields(binding),
                "accepted_message_id": accepted_message_id,
            },
        )

    async def emit_transcript_rejected(
        self,
        turn: Any,
        *,
        reason: str,
        retry_policy: str,
    ) -> dict[str, Any]:
        """Send one fully correlated, terminal pre-acceptance refusal."""

        if reason not in _TRANSCRIPT_REJECTION_REASONS:
            raise ControlProtocolError("invalid_transcript_rejection_reason")
        if retry_policy not in _TRANSCRIPT_RETRY_POLICIES:
            raise ControlProtocolError("invalid_transcript_retry_policy")
        binding, reservation = await self._current_transcript_binding(turn)
        return await self.worker_pool.send_session_command(
            reservation,
            "transcript_rejected",
            {
                **self._disposition_fields(binding),
                "reason": reason,
                "retry_policy": retry_policy,
            },
        )

    async def _current_transcript_binding(
        self,
        turn: Any,
    ) -> tuple[TranscriptTurnBinding, SessionReservation]:
        try:
            session_id = turn.session_id
            generation = turn.session_generation
        except AttributeError:
            raise ControlProtocolError("invalid_transcript_turn") from None
        reservation = await self.worker_pool.current_reservation(
            session_id=session_id,
            generation=generation,
        )
        binding = TranscriptTurnBinding.from_turn(
            turn,
            assignment_id=reservation.assignment_id,
            worker_identity=reservation.worker_identity,
        )
        return binding, reservation

    @staticmethod
    def _disposition_fields(
        binding: TranscriptTurnBinding,
    ) -> dict[str, Any]:
        return {
            "turn_id": binding.turn_id,
            "client_turn_id": binding.client_turn_id,
            "submission_id": binding.submission_id,
            "request_generation": binding.request_generation,
            "chat_id": binding.chat_id,
            "media_grant_revision": binding.media_grant_revision,
        }

    async def claim_turn_announcement(
        self, *, user_id: str, request: AnnouncementClaimRequest
    ) -> AnnouncementMutation:
        """Atomically claim one sequence/reservation through the DB seam."""

        user_id = _user_id(user_id)
        now = _aware(self._utcnow(), "invalid_coordinator_clock")
        try:
            mutation = await self._repository.claim_announcement(
                user_id=user_id,
                request=request,
                now=now,
            )
        except VoiceCoordinatorError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ClaimUnavailable("coordinator_repository_failed") from None
        if not isinstance(mutation, AnnouncementMutation):
            raise ClaimUnavailable("invalid_repository_announcement_claim")
        state, claim = mutation.state, mutation.claim
        expected_announcement_id = _announcement_id(
            request, claim.sequence, claim.quantum_role, claim.quantum_index
        )
        if (
            state.generation != request.generation
            or state.announcement_claim_id != request.claim_id
            or claim.claim_id != request.claim_id
            or claim.kind != request.kind
            or claim.quantum_role != request.quantum_role
            or claim.sequence != state.announcement_sequence
            or claim.announcement_id != expected_announcement_id
            or claim.claim_expires_at != state.announcement_claim_expires_at
            or (
                request.expected_phrase_key is not None
                and claim.phrase_key != request.expected_phrase_key
            )
            or not now < claim.claim_expires_at <= now + timedelta(seconds=30)
        ):
            raise ClaimUnavailable("invalid_repository_announcement_claim")
        if claim.kind == "result":
            if (
                claim.quantum_index != state.result_quantum_count - 1
                or claim.max_duration_samples
                != (
                    RESULT_OPENING_SAMPLES
                    if claim.quantum_index == 0
                    else CONTINUATION_SAMPLES
                )
                or claim.result_reserved_samples_after != state.result_reserved_samples
                or claim.phrase_key is not None
            ):
                raise ClaimUnavailable("invalid_repository_announcement_claim")
        elif (
            claim.quantum_index != 0
            or claim.max_duration_samples
            != (
                RESULT_OPENING_SAMPLES
                if claim.kind in _SHORT_TERMINAL_KINDS
                else SINGLE_SAMPLES
            )
            or claim.result_reserved_samples_after is not None
            or claim.phrase_key != state.last_phrase_key
        ):
            raise ClaimUnavailable("invalid_repository_announcement_claim")
        try:
            _uuid4(claim.announcement_id, "invalid_repository_announcement_id")
        except ValueError:
            raise ClaimUnavailable("invalid_repository_announcement_claim") from None
        return mutation

    async def complete_turn_announcement(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        generation: int,
        claim_id: str,
    ) -> bool:
        """Release one exact claim without changing its reserved audio budget."""

        user_id = _user_id(user_id)
        _uuid4(session_id, "invalid_session_id")
        _uuid4(turn_id, "invalid_turn_id")
        _positive(generation, "invalid_generation")
        _uuid4(claim_id, "invalid_announcement_claim_id")
        try:
            result = await self._repository.complete_announcement(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                generation=generation,
                claim_id=claim_id,
            )
        except VoiceCoordinatorError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ClaimUnavailable("coordinator_repository_failed") from None
        if not isinstance(result, bool):
            raise ClaimUnavailable("invalid_repository_release")
        return result


def deterministic_uuid4(domain: str, *parts: str) -> str:
    """Return a stable RFC-4122 UUIDv4-shaped id from canonical fence fields."""

    values = (domain, *parts)
    if any(
        not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4_096
        for value in values
    ):
        raise ValueError("invalid_deterministic_id_part")
    digest = hashlib.sha256(b"astraldeep.voice.uuid4.v1\0")
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return str(UUID(bytes=digest.digest()[:16], version=4))


def _announcement_id(
    request: AnnouncementClaimRequest,
    sequence: int,
    role: str,
    quantum_index: int,
) -> str:
    return deterministic_uuid4(
        "voice-announcement-v1",
        request.session_id,
        request.turn_id,
        str(request.generation),
        str(sequence),
        request.kind,
        role,
        str(quantum_index),
    )


def _decode_frame(payload: str | bytes) -> dict[str, Any]:
    if not isinstance(payload, str):
        raise ControlProtocolError("text_frame_required")
    try:
        encoded = payload.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ControlProtocolError("invalid_utf8") from None
    if len(encoded) > MAX_CONTROL_FRAME_BYTES:
        raise ControlProtocolError("frame_too_large")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: _invalid_json_number(),
        )
    except ControlProtocolError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ControlProtocolError("invalid_json") from None
    if not isinstance(value, dict):
        raise ControlProtocolError("object_frame_required")
    return value


def _json_frame_size(value: Any, code: str) -> int:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        return len(payload.encode("utf-8", errors="strict"))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ControlProtocolError(code) from None


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlProtocolError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_json_number() -> None:
    raise ControlProtocolError("invalid_json_number")


def _encode_frame(frame: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(
            frame,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ControlProtocolError("invalid_outgoing_frame") from None
    if len(payload.encode("utf-8")) > MAX_CONTROL_FRAME_BYTES:
        raise ControlProtocolError("outgoing_frame_too_large")
    return payload


def _validate_worker_base(frame: Mapping[str, Any]) -> None:
    required = {
        "type",
        "schema_version",
        "message_id",
        "session_id",
        "generation",
        "sequence",
        "sent_at",
    }
    if not required.issubset(frame):
        raise ControlProtocolError("invalid_worker_frame_base")
    if frame["schema_version"] != "1":
        raise ControlProtocolError("invalid_schema_version")
    _uuid4(frame["message_id"], "invalid_message_id", ControlProtocolError)
    _uuid4(frame["session_id"], "invalid_session_id", ControlProtocolError)
    _positive(frame["generation"], "invalid_generation", ControlProtocolError)
    _bounded_int(
        frame["sequence"],
        0,
        2**63 - 1,
        "invalid_sequence",
        ControlProtocolError,
    )
    _parse_timestamp(frame["sent_at"], "invalid_sent_at", ControlProtocolError)


def _validate_worker_pool_base(frame: Mapping[str, Any]) -> None:
    required = {
        "type",
        "schema_version",
        "message_id",
        "sequence",
        "sent_at",
    }
    if not required.issubset(frame):
        raise ControlProtocolError("invalid_worker_pool_frame_base")
    if frame["schema_version"] != "1":
        raise ControlProtocolError("invalid_schema_version")
    _uuid4(frame["message_id"], "invalid_message_id", ControlProtocolError)
    _bounded_int(
        frame["sequence"],
        1,
        2**63 - 1,
        "invalid_sequence",
        ControlProtocolError,
    )
    _parse_timestamp(frame["sent_at"], "invalid_sent_at", ControlProtocolError)


def _reject_forbidden_worker_content(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _FORBIDDEN_WORKER_KEYS:
                raise ControlProtocolError("forbidden_worker_content")
            _reject_forbidden_worker_content(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_worker_content(item)


def _reject_forbidden_coordinator_content(value: Any) -> None:
    forbidden = _FORBIDDEN_WORKER_KEYS - {"text"}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in forbidden or key == "worker_rtc_grant":
                raise ControlProtocolError("forbidden_coordinator_content")
            _reject_forbidden_coordinator_content(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_coordinator_content(item)


def _media_rotation_fingerprint(fields: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the complete non-secret idempotency tuple for one rotation."""

    return (
        fields.get("refresh_id"),
        fields.get("previous_media_grant_revision"),
        fields.get("media_grant_revision"),
        fields.get("client_participant_identity"),
        fields.get("transport"),
        fields.get("grant_expires_at"),
    )


def _validate_livekit_url(value: Any, *, allow_insecure: bool) -> None:
    if not isinstance(value, str) or len(value) > 2_048:
        raise ControlProtocolError("invalid_livekit_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ControlProtocolError("invalid_livekit_url") from None
    if (
        parsed.scheme not in ({"ws", "wss"} if allow_insecure else {"wss"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ControlProtocolError("invalid_livekit_url")


async def _safe_close(socket: WorkerSocket, code: int, reason: str) -> None:
    try:
        await asyncio.wait_for(socket.close(code=code, reason=reason), timeout=3.0)
    except Exception:
        return


def _uuid4(
    value: Any,
    code: str,
    error_type: type[Exception] = ValueError,
) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        raise error_type(code)
    try:
        parsed = UUID(value)
    except ValueError:
        raise error_type(code) from None
    if parsed.version != 4 or str(parsed) != value:
        raise error_type(code)
    return value


def _opaque(
    value: Any,
    code: str,
    error_type: type[Exception] = ValueError,
) -> str:
    if not isinstance(value, str) or _OPAQUE.fullmatch(value) is None:
        raise error_type(code)
    return value


def _positive(
    value: Any,
    code: str,
    error_type: type[Exception] = ValueError,
) -> int:
    return _bounded_int(value, 1, 2**63 - 1, code, error_type)


def _bounded_int(
    value: Any,
    minimum: int,
    maximum: int,
    code: str,
    error_type: type[Exception] = ValueError,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(code)
    if not minimum <= value <= maximum:
        raise error_type(code)
    return value


def _aware(value: Any, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(code)
    if value.utcoffset() is None:
        raise ValueError(code)
    return value.astimezone(UTC)


def _parse_timestamp(
    value: Any,
    code: str,
    error_type: type[Exception] = ValueError,
) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise error_type(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise error_type(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise error_type(code)
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _user_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= 255
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("invalid_user_id")
    return value
