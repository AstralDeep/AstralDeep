"""PostgreSQL ownership and idempotency authority for conversational voice.

This module stores only content-free session/turn correlation.  It never
stores audio, transcript text, recap text, media bearers, or credentials.
Every mutating method locks the owner/session row and applies explicit
generation/revision compare-and-swap fences before changing state.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from shared.voice_transcript import (
    TranscriptProofBinding,
    TranscriptProofError,
    verify_transcript_proof,
)

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

from orchestrator.voice_coordinator import (
    APPROVED_PHRASE_KEYS,
    CADENCE_TARGET_SECONDS,
    PREACCEPTANCE_REJECTION_PHRASES,
    AnnouncementClaimRequest,
    AnnouncementMutation,
    AnnouncementState,
    AnnouncementStateAdapter,
    ClaimUnavailable,
    ControlLeaseAdapter,
    ControlLeaseState,
    PhraseBook,
    RecognitionStart,
    StaleFence,
    TranscriptTurnBinding,
    deterministic_uuid4,
)

IDLE_TIMEOUT = timedelta(minutes=5)
GRANT_REPLAY_WINDOW = timedelta(seconds=30)
_TERMINAL_TURN_STATES = frozenset(
    {"succeeded", "failed", "refused", "cancelled", "abandoned"}
)
_ACTIVE_TURN_STATES = frozenset(
    {"recognizing", "submitting", "accepted", "processing", "waiting_on_user"}
)
_DEVICE_KINDS = frozenset({"web", "windows", "android", "ios", "macos", "watchos"})
_TRANSPORTS = frozenset({"livekit", "watch_pcm_websocket", "client_local"})
_PLAYOUT_KINDS = frozenset(
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
_FOREGROUND_REASONS = frozenset(
    {
        "foreground",
        "backgrounded",
        "locked",
        "audio_interrupted",
        "route_unavailable",
        "connection_lost",
    }
)
_END_REASONS = frozenset(
    {
        "user",
        "idle",
        "takeover",
        "logout",
        "auth_expired",
        "chat_deleted",
        "chat_unauthorized",
        "lease_expired",
        "media_error",
        "shutdown",
    }
)
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


class VoiceSessionRepositoryError(RuntimeError):
    """Content-free repository failure safe for typed problem mapping."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VoiceSessionNotFound(VoiceSessionRepositoryError):
    """The resource is absent or not owned by the authenticated principal."""


class TakeoverRequired(VoiceSessionRepositoryError):
    """Another device currently owns the authenticated user's media session."""

    def __init__(self, current: VoiceSessionRecord) -> None:
        self.current = current
        super().__init__("voice_takeover_required")


class IdempotencyConflict(VoiceSessionRepositoryError):
    """An idempotency key was replayed with different or expired metadata."""


class ContextSyncPending(VoiceSessionRepositoryError):
    """A desired chat context still awaits the ordered worker acknowledgement."""


class StaleSessionFence(StaleFence):
    """A session generation, grant revision, or context revision is stale."""


class TranscriptSubmissionRejected(VoiceSessionRepositoryError):
    """A final transcript failed before ordinary message acceptance."""

    def __init__(self, reason: str, retry_policy: str) -> None:
        self.reason = reason
        self.retry_policy = retry_policy
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class CreateSession:
    """Validated non-secret fields needed to create one owner session."""

    user_id: str
    activation_id: str
    device_id: str
    device_kind: str
    transport: str
    room_name: str | None
    participant_identity: str | None
    visible_chat_id: str
    owner_connection_generation: str
    control_binding_id: str
    control_binding_expires_at: datetime
    lease_expires_at: datetime
    media_grant_nonce_hash: bytes | None = field(repr=False)
    media_grant_issued_at: datetime | None = field(repr=False)
    media_grant_expires_at: datetime | None = field(repr=False)
    speech_backend: str = "llm_factory"

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _user_id(self.user_id))
        for name in (
            "activation_id",
            "device_id",
            "visible_chat_id",
            "owner_connection_generation",
            "control_binding_id",
        ):
            object.__setattr__(
                self, name, _uuid4(getattr(self, name), f"invalid_{name}")
            )
        if self.device_kind not in _DEVICE_KINDS:
            raise ValueError("invalid_device_kind")
        if self.speech_backend not in {"llm_factory", "client_local"}:
            raise ValueError("invalid_speech_backend")
        if self.transport not in _TRANSPORTS:
            raise ValueError("invalid_transport")
        if self.speech_backend == "llm_factory" and self.transport == "client_local":
            raise ValueError("invalid_transport")
        if self.speech_backend == "client_local" and self.transport != "client_local":
            raise ValueError("invalid_transport")
        for name in ("control_binding_expires_at", "lease_expires_at"):
            object.__setattr__(
                self, name, _aware(getattr(self, name), f"invalid_{name}")
            )
        if self.speech_backend == "llm_factory":
            object.__setattr__(
                self, "room_name", _opaque(self.room_name, "invalid_room_name")
            )
            object.__setattr__(
                self,
                "participant_identity",
                _opaque(
                    self.participant_identity,
                    "invalid_participant_identity",
                ),
            )
            for name in ("media_grant_issued_at", "media_grant_expires_at"):
                object.__setattr__(
                    self, name, _aware(getattr(self, name), f"invalid_{name}")
                )
            nonce_hash = _nonce_hash(self.media_grant_nonce_hash)
            object.__setattr__(self, "media_grant_nonce_hash", nonce_hash)
            if self.media_grant_expires_at <= self.media_grant_issued_at:
                raise ValueError("invalid_media_grant_expiry")
        elif any(
            value is not None
            for value in (
                self.room_name,
                self.participant_identity,
                self.media_grant_nonce_hash,
                self.media_grant_issued_at,
                self.media_grant_expires_at,
            )
        ):
            raise ValueError("client_local_remote_media_fields")


@dataclass(frozen=True, slots=True)
class SessionTakeover:
    """Explicit replacement request fenced to the prior session generation."""

    previous_session_id: str
    expected_generation: int
    expected_media_grant_revision: int
    create: CreateSession

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "previous_session_id",
            _uuid4(self.previous_session_id, "invalid_previous_session_id"),
        )
        _positive(self.expected_generation, "invalid_expected_generation")
        _positive(
            self.expected_media_grant_revision,
            "invalid_expected_media_grant_revision",
        )
        if not isinstance(self.create, CreateSession):
            raise TypeError("create must be CreateSession")


@dataclass(frozen=True, slots=True)
class MediaGrantRefresh:
    """Non-secret metadata for one idempotent media-grant rotation."""

    user_id: str
    session_id: str
    refresh_id: str
    expected_generation: int
    expected_media_grant_revision: int
    participant_identity: str
    nonce_hash: bytes = field(repr=False)
    issued_at: datetime = field(repr=False)
    expires_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _user_id(self.user_id))
        for name in ("session_id", "refresh_id"):
            object.__setattr__(
                self, name, _uuid4(getattr(self, name), f"invalid_{name}")
            )
        _positive(self.expected_generation, "invalid_expected_generation")
        _positive(
            self.expected_media_grant_revision,
            "invalid_expected_media_grant_revision",
        )
        object.__setattr__(
            self,
            "participant_identity",
            _opaque(self.participant_identity, "invalid_participant_identity"),
        )
        object.__setattr__(self, "nonce_hash", _nonce_hash(self.nonce_hash))
        object.__setattr__(
            self, "issued_at", _aware(self.issued_at, "invalid_grant_issued_at")
        )
        object.__setattr__(
            self, "expires_at", _aware(self.expires_at, "invalid_grant_expires_at")
        )
        if self.expires_at <= self.issued_at:
            raise ValueError("invalid_media_grant_expiry")


@dataclass(frozen=True, slots=True)
class RecognitionBinding:
    """Immutable recognition-time chat/session fields echoed by the worker."""

    user_id: str
    session_id: str
    session_generation: int
    media_grant_revision: int
    client_turn_id: str
    chat_id: str
    chat_context_revision: int
    execution_base_render_revision: int
    control_owner_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _user_id(self.user_id))
        for name in ("session_id", "client_turn_id", "chat_id"):
            object.__setattr__(
                self, name, _uuid4(getattr(self, name), f"invalid_{name}")
            )
        _positive(self.session_generation, "invalid_session_generation")
        _positive(self.media_grant_revision, "invalid_media_grant_revision")
        _positive(self.chat_context_revision, "invalid_chat_context_revision")
        if (
            isinstance(self.execution_base_render_revision, bool)
            or not isinstance(self.execution_base_render_revision, int)
            or self.execution_base_render_revision < 0
        ):
            raise ValueError("invalid_execution_base_render_revision")
        object.__setattr__(
            self,
            "control_owner_id",
            _opaque(self.control_owner_id, "invalid_control_owner_id", max_length=128),
        )


@dataclass(frozen=True, slots=True, repr=False)
class TranscriptSubmission:
    """One exact client-forwarded media final; content is never represented."""

    user_id: str
    session_id: str
    generation: int
    media_grant_revision: int
    turn_id: str
    client_turn_id: str
    submission_id: str
    request_generation: str
    chat_id: str
    chat_context_revision: int
    source_participant_identity: str
    detected_language: str
    text: str = field(repr=False)
    text_digest_sha256: str = field(repr=False)
    transcript_proof: str = field(repr=False)
    proof_expires_at: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _user_id(self.user_id))
        for name in (
            "session_id",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
        ):
            object.__setattr__(
                self,
                name,
                _uuid4(getattr(self, name), f"invalid_{name}"),
            )
        for name in (
            "generation",
            "media_grant_revision",
            "chat_context_revision",
        ):
            _positive(getattr(self, name), f"invalid_{name}")
        object.__setattr__(
            self,
            "source_participant_identity",
            _opaque(
                self.source_participant_identity,
                "invalid_source_participant_identity",
                max_length=128,
            ),
        )
        if (
            not isinstance(self.detected_language, str)
            or re.fullmatch(
                r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*",
                self.detected_language,
            )
            is None
        ):
            raise ValueError("invalid_detected_language")
        for name in (
            "text",
            "text_digest_sha256",
            "transcript_proof",
            "proof_expires_at",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"invalid_{name}")

    def __repr__(self) -> str:
        return (
            "TranscriptSubmission("
            f"user_id={self.user_id!r}, session_id={self.session_id!r}, "
            f"generation={self.generation!r}, turn_id={self.turn_id!r}, "
            f"client_turn_id={self.client_turn_id!r}, "
            f"submission_id={self.submission_id!r}, "
            "text=<redacted>, digest=<redacted>, proof=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LocalTranscriptSubmission:
    """One call-stack-only local transcript attestation.

    The server constructs this only after the authenticated socket registry
    validates the client final. It contains no client-minted worker proof and
    is never persisted.
    """

    user_id: str
    session_id: str
    generation: int
    speech_revision: int
    turn_id: str
    client_turn_id: str
    submission_id: str
    request_generation: str
    chat_id: str
    chat_context_revision: int
    device_id: str
    connection_generation: str
    binding_id: str
    detected_language: str
    canonical_text: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _user_id(self.user_id))
        for name in (
            "session_id",
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
            "device_id",
            "connection_generation",
            "binding_id",
        ):
            object.__setattr__(
                self,
                name,
                _uuid4(getattr(self, name), f"invalid_{name}"),
            )
        for name in ("generation", "speech_revision", "chat_context_revision"):
            _positive(getattr(self, name), f"invalid_{name}")
        if self.detected_language != "en":
            raise ValueError("invalid_detected_language")
        if not isinstance(self.canonical_text, str):
            raise TypeError("invalid_transcript_text")

    @classmethod
    def from_authority(
        cls,
        *,
        user_id: str,
        authority: Any,
        detected_language: str,
        canonical_text: str,
    ) -> "LocalTranscriptSubmission":
        return cls(
            user_id=user_id,
            session_id=authority.session_id,
            generation=authority.generation,
            speech_revision=authority.speech_revision,
            turn_id=authority.turn_id,
            client_turn_id=authority.client_turn_id,
            submission_id=authority.submission_id,
            request_generation=authority.request_generation,
            chat_id=authority.chat_id,
            chat_context_revision=authority.chat_context_revision,
            device_id=authority.device_id,
            connection_generation=authority.connection_generation,
            binding_id=authority.binding_id,
            detected_language=detected_language,
            canonical_text=canonical_text,
        )

    def __repr__(self) -> str:
        return (
            "LocalTranscriptSubmission("
            f"user_id={self.user_id!r}, session_id={self.session_id!r}, "
            f"turn_id={self.turn_id!r}, client_turn_id={self.client_turn_id!r}, "
            "canonical_text=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TranscriptAdmission:
    """Verified canonical content plus its content-free durable turn."""

    canonical_text: str = field(repr=False)
    turn: VoiceTurnRecord
    replayed: bool = False

    def __repr__(self) -> str:
        return (
            "TranscriptAdmission(canonical_text=<redacted>, "
            f"turn={self.turn!r}, replayed={self.replayed!r})"
        )


@dataclass(frozen=True, slots=True)
class SessionControl:
    """Verified current UI binding fields used for an owner-row mutation."""

    device_id: str
    connection_generation: str
    binding_id: str
    binding_expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("device_id", "connection_generation", "binding_id"):
            object.__setattr__(
                self, name, _uuid4(getattr(self, name), f"invalid_{name}")
            )
        object.__setattr__(
            self,
            "binding_expires_at",
            _aware(self.binding_expires_at, "invalid_binding_expiry"),
        )


@dataclass(frozen=True, slots=True)
class SessionUpdate:
    """Strict optional session controls applied under one PostgreSQL row lock."""

    user_id: str
    session_id: str
    expected_generation: int
    expected_media_grant_revision: int
    control: SessionControl
    visible_chat_id: str | None = None
    speech_muted: bool | None = None
    microphone_enabled: bool | None = None
    foreground_active: bool | None = None
    foreground_reason: str | None = None
    interaction: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _user_id(self.user_id))
        object.__setattr__(
            self, "session_id", _uuid4(self.session_id, "invalid_session_id")
        )
        _positive(self.expected_generation, "invalid_expected_generation")
        _positive(
            self.expected_media_grant_revision,
            "invalid_expected_media_grant_revision",
        )
        if not isinstance(self.control, SessionControl):
            raise TypeError("control must be SessionControl")
        if self.visible_chat_id is not None:
            object.__setattr__(
                self,
                "visible_chat_id",
                _uuid4(self.visible_chat_id, "invalid_visible_chat_id"),
            )
        for name in (
            "speech_muted",
            "microphone_enabled",
            "foreground_active",
            "interaction",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"invalid_{name}")
        if (self.foreground_active is None) != (self.foreground_reason is None):
            raise ValueError("invalid_foreground_state")
        if self.foreground_reason is not None and self.foreground_reason not in (
            _FOREGROUND_REASONS
        ):
            raise ValueError("invalid_foreground_reason")
        if self.foreground_active is True and self.foreground_reason != "foreground":
            raise ValueError("invalid_foreground_state")
        if self.foreground_active is False and (
            self.foreground_reason == "foreground"
            or self.microphone_enabled is not False
        ):
            raise ValueError("invalid_foreground_state")
        if self.interaction is False:
            raise ValueError("invalid_interaction")
        if all(
            value is None
            for value in (
                self.visible_chat_id,
                self.speech_muted,
                self.microphone_enabled,
                self.foreground_active,
                self.interaction,
            )
        ):
            raise ValueError("empty_session_update")


@dataclass(frozen=True, slots=True)
class VoiceSessionRecord:
    """Content-free durable snapshot of one voice session row."""

    session_id: str
    user_id: str
    activation_id: str
    device_id: str
    device_kind: str
    transport: str
    room_name: str | None
    participant_identity: str | None
    worker_identity: str | None
    visible_chat_id: str
    chat_context_revision: int
    applied_visible_chat_id: str | None
    applied_chat_context_revision: int | None
    state: str
    speech_muted: bool
    microphone_enabled: bool
    foreground_active: bool
    foreground_reason: str
    generation: int
    media_grant_revision: int
    owner_connection_generation: str
    control_binding_id: str
    control_binding_expires_at: datetime
    lease_expires_at: datetime
    control_owner_id: str | None
    control_lease_expires_at: datetime | None
    last_interaction_at: datetime
    idle_started_at: datetime | None
    started_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    chat_unavailable_at: datetime | None
    takeover_of_session_id: str | None
    media_grant_nonce_hash: bytes | None = field(repr=False)
    media_grant_expires_at: datetime | None = field(repr=False)
    media_grant_consumed_at: datetime | None = field(repr=False)
    last_media_refresh_id: str | None
    media_grant_issued_at: datetime | None = field(repr=False)
    worker_assignment_id: str | None
    worker_rtc_grant_revision: int | None
    worker_rtc_grant_issued_at: datetime | None = field(repr=False)
    worker_rtc_grant_expires_at: datetime | None = field(repr=False)
    speech_backend: str = "llm_factory"

    @property
    def chat_context_synced(self) -> bool:
        """Whether the worker has acknowledged the desired chat context."""

        return (
            self.applied_visible_chat_id == self.visible_chat_id
            and self.applied_chat_context_revision == self.chat_context_revision
        )

    @property
    def idle_expires_at(self) -> datetime | None:
        """Return the fixed five-minute true-idle deadline, when active."""

        if self.idle_started_at is None:
            return None
        return self.idle_started_at + IDLE_TIMEOUT


@dataclass(frozen=True, slots=True)
class VoiceTurnRecord:
    """Content-free durable snapshot of one recognition/dispatch correlation."""

    turn_id: str
    client_turn_id: str
    session_id: str
    session_generation: int
    media_grant_revision: int
    user_id: str
    chat_id: str
    chat_context_revision: int
    execution_base_render_revision: int
    submission_id: str
    request_generation: str
    message_id: int | None
    acceptance_commit_id: str | None
    result_commit_id: str | None
    operation_id: str | None
    state: str
    is_foreground: bool
    detected_language: str | None
    spoken_output_policy: str
    output_reason: str
    terminal_kind: str | None
    rejection_reason: str | None
    rejection_retry_policy: str | None
    recap_source: str
    sensitivity: str
    announcement_sequence: int
    result_reserved_samples: int
    result_quantum_count: int
    last_phrase_key: str | None
    next_announcement_due_at: datetime | None
    accepted_at: datetime | None
    processing_started_at: datetime | None
    terminal_at: datetime | None
    created_at: datetime
    updated_at: datetime
    result_request_generation: str | None = None
    origin_chat_unavailable_at: datetime | None = None
    origin_chat_unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SessionMutation:
    session: VoiceSessionRecord
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class TurnMutation:
    turn: VoiceTurnRecord
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class ChatUnavailableMutation:
    """Content-free receipt for one owner/chat lifecycle fence."""

    user_id: str
    chat_id: str
    reason: str
    chat_deleted: bool
    replayed: bool
    ended_sessions: tuple[VoiceSessionRecord, ...]
    announcement_session_keys: tuple[tuple[str, int], ...]
    unaccepted_turn_ids: tuple[str, ...]
    accepted_turn_ids: tuple[str, ...]
    aborted_result_commit_ids: tuple[str, ...]


class VoiceSessionRepository:
    """Deep voice policy over Plane-owned session and turn persistence."""

    def __init__(
        self,
        *,
        plane_runtime: Any,
        plane_repositories: Any | None = None,
        uuid_factory: Any = uuid.uuid4,
        control_lease_ttl_seconds: int = 15,
    ) -> None:
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        if not callable(getattr(plane_runtime, "transaction", None)):
            raise TypeError("plane_runtime must provide transaction()")
        repository, runtime = repository_from(
            "voice",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=None,
        )
        self._plane = PlaneRepositoryContext(
            repository=repository,
            plane_runtime=runtime,
        )
        self._voice = repository
        self._uuid_factory = uuid_factory
        self._control_leases = ControlLeaseAdapter(
            ttl_seconds=control_lease_ttl_seconds
        )
        self._announcements = AnnouncementStateAdapter(
            PhraseBook(APPROVED_PHRASE_KEYS)
        )

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self._plane.transaction() as transaction:
            yield transaction

    @contextmanager
    def _transaction_or_existing(self, transaction: Any | None) -> Iterator[Any]:
        """Join a caller-owned Plane transaction or open an application transaction."""

        if transaction is not None:
            required = ("execute", "fetch_one", "fetch_all")
            if not all(callable(getattr(transaction, name, None)) for name in required):
                raise TypeError(
                    "transaction must provide execute(), fetch_one(), and fetch_all()"
                )
            yield transaction
            return
        with self._transaction() as owned:
            yield owned

    def create_session(
        self, request: CreateSession, *, now: datetime
    ) -> SessionMutation:
        """Create one live owner session or return the exact activation replay."""

        if not isinstance(request, CreateSession):
            raise TypeError("request must be CreateSession")
        now = _aware(now, "invalid_current_time")
        self._validate_create_lifetimes(request, now)
        with self._transaction() as transaction:
            self._lock_identity(transaction, "owner", request.user_id)
            existing = self._activation_row(
                transaction, request.user_id, request.activation_id
            )
            if existing is not None:
                self._assert_activation_replay(existing, request, takeover_of=None)
                return SessionMutation(_session(existing), replayed=True)
            current = self._voice.get_live_session_record(
                transaction,
                owner_id=request.user_id,
                for_update=True,
            )
            if current is not None:
                raise TakeoverRequired(_session(current))
            row = self._insert_session(
                transaction,
                request,
                generation=1,
                takeover_of=None,
                now=now,
            )
        return SessionMutation(_session(row))

    def take_over_session(
        self,
        request: SessionTakeover,
        *,
        now: datetime,
    ) -> SessionMutation:
        """Atomically end one owner generation and create its explicit replacement."""

        if not isinstance(request, SessionTakeover):
            raise TypeError("request must be SessionTakeover")
        now = _aware(now, "invalid_current_time")
        create = request.create
        self._validate_create_lifetimes(create, now)
        with self._transaction() as transaction:
            self._lock_identity(transaction, "owner", create.user_id)
            existing = self._activation_row(
                transaction, create.user_id, create.activation_id
            )
            if existing is not None:
                self._assert_activation_replay(
                    existing,
                    create,
                    takeover_of=request.previous_session_id,
                )
                return SessionMutation(_session(existing), replayed=True)
            previous = self._session_for_update(
                transaction,
                create.user_id,
                request.previous_session_id,
            )
            if previous.get("ended_at") is not None:
                current = self._voice.get_live_session_record(
                    transaction,
                    owner_id=create.user_id,
                    for_update=True,
                )
                if current is not None and int(current["generation"]) > (
                    request.expected_generation
                ):
                    raise StaleSessionFence("stale_generation")
            self._assert_live(previous)
            self._assert_fences(
                previous,
                request.expected_generation,
                request.expected_media_grant_revision,
            )
            self._end_session_row(
                transaction,
                previous,
                reason="takeover",
                now=now,
            )
            row = self._insert_session(
                transaction,
                create,
                generation=int(previous["generation"]) + 1,
                takeover_of=request.previous_session_id,
                now=now,
            )
        return SessionMutation(_session(row))

    def get_session(self, *, user_id: str, session_id: str) -> VoiceSessionRecord:
        """Return an owner-scoped session without revealing foreign ownership."""

        user_id = _user_id(user_id)
        session_id = _uuid4(session_id, "invalid_session_id")
        with self._transaction() as transaction:
            row = self._voice.get_session_record(
                transaction,
                owner_id=user_id,
                session_id=session_id,
            )
            if row is None:
                raise VoiceSessionNotFound("voice_session_not_found")
        return _session(row)

    def get_live_session(self, *, user_id: str) -> VoiceSessionRecord | None:
        """Return the user's sole unended session, if present."""

        user_id = _user_id(user_id)
        with self._transaction() as transaction:
            row = self._voice.get_live_session_record(
                transaction,
                owner_id=user_id,
            )
        return None if row is None else _session(row)

    def mark_chat_unavailable(
        self,
        *,
        user_id: str,
        chat_id: str,
        reason: str,
        delete_chat: bool,
        now: datetime,
    ) -> ChatUnavailableMutation:
        """Fence one unavailable owner/chat before optional hard deletion.

        The transaction intentionally leaves ``operation_record`` untouched:
        accepted side effects and audit retain their ordinary lifecycle while
        the voice correlation, private publication stage, and speech path are
        terminally fenced. Retained voice IDs are tombstones, never renewed
        authorization for the chat.
        """

        user_id = _user_id(user_id)
        chat_id = _opaque(chat_id, "invalid_chat_id", max_length=255)
        if reason not in {"deleted", "access_revoked"}:
            raise ValueError("invalid_chat_unavailable_reason")
        if not isinstance(delete_chat, bool):
            raise TypeError("delete_chat must be a boolean")
        if delete_chat != (reason == "deleted"):
            raise ValueError("chat_unavailable_reason_delete_mismatch")
        now = _aware(now, "invalid_current_time")
        session_end_reason = (
            "chat_deleted" if reason == "deleted" else "chat_unauthorized"
        )
        with self._transaction() as transaction:
            self._lock_identity(transaction, "owner_chat", user_id, chat_id)
            chat_available = self._voice.chat_exists(
                transaction,
                owner_id=user_id,
                chat_id=chat_id,
                for_update=True,
            )
            if not chat_available:
                return ChatUnavailableMutation(
                    user_id=user_id,
                    chat_id=chat_id,
                    reason=reason,
                    chat_deleted=False,
                    replayed=True,
                    ended_sessions=(),
                    announcement_session_keys=(),
                    unaccepted_turn_ids=(),
                    accepted_turn_ids=(),
                    aborted_result_commit_ids=(),
                )

            # Match the turn-before-session order used by transcript
            # admission and atomic message acceptance. The owner/chat row lock
            # prevents a new publication from entering behind this fence.
            turns = list(
                self._voice.list_chat_turn_records_for_update(
                    transaction,
                    owner_id=user_id,
                    chat_id=chat_id,
                )
            )
            affected_sessions = self._voice.list_chat_session_records_for_update(
                transaction,
                owner_id=user_id,
                chat_id=chat_id,
            )
            ended_sessions = tuple(
                ended
                for row in affected_sessions
                if row.get("ended_at") is None
                for ended in (
                    self._end_session_row(
                        transaction,
                        row,
                        reason=session_end_reason,
                        now=now,
                        chat_unavailable_at=now,
                        abandon_unaccepted=False,
                    ),
                )
            )

            unaccepted_turn_ids = tuple(
                str(row["turn_id"])
                for row in turns
                if row.get("accepted_at") is None
                and str(row["state"]) in {"recognizing", "submitting"}
            )
            accepted_turn_ids = tuple(
                str(row["turn_id"])
                for row in turns
                if row.get("origin_chat_unavailable_at") is None
                and (
                    row.get("accepted_at") is not None
                    or str(row["state"])
                    in {"accepted", "processing", "waiting_on_user"}
                )
            )
            if unaccepted_turn_ids:
                self._voice.abandon_chat_turns(
                    transaction,
                    owner_id=user_id,
                    turn_ids=unaccepted_turn_ids,
                    reason=reason,
                    now=now,
                    accepted=False,
                )
            if accepted_turn_ids:
                self._voice.abandon_chat_turns(
                    transaction,
                    owner_id=user_id,
                    turn_ids=accepted_turn_ids,
                    reason=reason,
                    now=now,
                    accepted=True,
                )

            aborted_result_commit_ids = (
                self._voice.abort_staged_chat_result_commits(
                    transaction,
                    owner_id=user_id,
                    chat_id=chat_id,
                    now=now,
                )
            )

            deleted = False
            if delete_chat:
                deleted = self._voice.delete_owned_chat(
                    transaction,
                    owner_id=user_id,
                    chat_id=chat_id,
                )

        announcement_session_keys = tuple(
            sorted(
                {
                    (str(row["session_id"]), int(row["session_generation"]))
                    for row in turns
                    if str(row["turn_id"]) in accepted_turn_ids
                }
            )
        )
        return ChatUnavailableMutation(
            user_id=user_id,
            chat_id=chat_id,
            reason=reason,
            chat_deleted=deleted,
            replayed=not bool(
                deleted
                or ended_sessions
                or unaccepted_turn_ids
                or accepted_turn_ids
                or aborted_result_commit_ids
            ),
            ended_sessions=ended_sessions,
            announcement_session_keys=announcement_session_keys,
            unaccepted_turn_ids=unaccepted_turn_ids,
            accepted_turn_ids=accepted_turn_ids,
            aborted_result_commit_ids=aborted_result_commit_ids,
        )

    def get_controlled_session(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        expected_media_grant_revision: int,
        control: SessionControl,
        now: datetime,
    ) -> VoiceSessionRecord:
        """Return and, for the same device, atomically adopt a fresh UI binding."""

        if not isinstance(control, SessionControl):
            raise TypeError("control must be SessionControl")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            row = self._apply_control_binding(transaction, row, control, now)
        return _session(row)

    def update_session(
        self,
        request: SessionUpdate,
        *,
        now: datetime,
    ) -> VoiceSessionRecord:
        """Apply owner state, context, mute, and interaction under one CAS lock."""

        if not isinstance(request, SessionUpdate):
            raise TypeError("request must be SessionUpdate")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(
                transaction,
                request.user_id,
                request.session_id,
            )
            self._assert_live(row)
            self._assert_fences(
                row,
                request.expected_generation,
                request.expected_media_grant_revision,
            )
            row = self._apply_control_binding(
                transaction,
                row,
                request.control,
                now,
            )

            visible_chat_id = row["visible_chat_id"]
            chat_context_revision = int(row["chat_context_revision"])
            if (
                request.visible_chat_id is not None
                and request.visible_chat_id != visible_chat_id
            ):
                if not _context_synced(row):
                    raise ContextSyncPending("chat_context_sync_pending")
                visible_chat_id = request.visible_chat_id
                chat_context_revision += 1

            foreground_active = (
                bool(row["foreground_active"])
                if request.foreground_active is None
                else request.foreground_active
            )
            foreground_reason = (
                str(row["foreground_reason"])
                if request.foreground_reason is None
                else request.foreground_reason
            )
            microphone_enabled = (
                bool(row["microphone_enabled"])
                if request.microphone_enabled is None
                else request.microphone_enabled
            )
            if not foreground_active:
                microphone_enabled = False
                state = "suspended"
            elif row["state"] in {"suspended", "reconnecting"}:
                state = "reconnecting"
            else:
                state = row["state"]
            speech_muted = (
                bool(row["speech_muted"])
                if request.speech_muted is None
                else request.speech_muted
            )
            interaction = request.interaction is True or (
                request.visible_chat_id is not None
                and request.visible_chat_id != row["visible_chat_id"]
            )
            last_interaction_at = now if interaction else row["last_interaction_at"]
            idle_started_at = None if interaction else row.get("idle_started_at")
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=request.user_id,
                session_id=request.session_id,
                updates={
                    "visible_chat_id": visible_chat_id,
                    "chat_context_revision": chat_context_revision,
                    "speech_muted": speech_muted,
                    "microphone_enabled": microphone_enabled,
                    "foreground_active": foreground_active,
                    "foreground_reason": foreground_reason,
                    "state": state,
                    "last_interaction_at": last_interaction_at,
                    "idle_started_at": idle_started_at,
                    "updated_at": now,
                },
            )
            if updated is None:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_session_update_failed")
        return _session(updated)

    def end_session(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        expected_media_grant_revision: int,
        control: SessionControl,
        reason: str,
        now: datetime,
    ) -> VoiceSessionRecord:
        """End media ownership without mutating or cancelling accepted turns."""

        if not isinstance(control, SessionControl):
            raise TypeError("control must be SessionControl")
        if reason not in _END_REASONS:
            raise ValueError("invalid_end_reason")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            if row.get("ended_at") is not None:
                self._assert_fences(
                    row,
                    expected_generation,
                    expected_media_grant_revision,
                )
                if reason == "user":
                    # A user-intent end of an already-ended generation is
                    # satisfied, not stale: the lease reaper (or another
                    # server-side end) may have fenced this exact session
                    # moments earlier, and refusing the owner's DELETE only
                    # wedges the client on a session that no longer exists.
                    # The same-device authorization mirrors
                    # _apply_control_binding: bindings rotate, so only the
                    # authenticated owner device (already verified by the
                    # control-binding gate upstream) must match.
                    if str(row["device_id"]) != control.device_id:
                        raise VoiceSessionRepositoryError(
                            "binding_scope_mismatch"
                        )
                    if control.binding_expires_at <= now:
                        raise VoiceSessionRepositoryError("binding_expired")
                    return _session(row)
                self._assert_control_replay(row, control, now)
                if row.get("end_reason") != reason:
                    raise StaleSessionFence("session_already_ended")
                return _session(row)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            row = self._apply_control_binding(transaction, row, control, now)
            ended = self._end_session_row(
                transaction,
                row,
                reason=reason,
                now=now,
            )
        return ended

    def end_live_user_session(
        self,
        *,
        user_id: str,
        reason: str,
        now: datetime,
    ) -> VoiceSessionRecord | None:
        """End one user's current media session for an authenticated lifecycle.

        This service-side seam is used only after logout/auth-expiry authority
        has already been established.  It cannot cancel accepted operations;
        only recognizing/submitting rows for the ended generation are
        abandoned.  Repeated callbacks are a credential-free no-op once no
        live owner remains.
        """

        user_id = _user_id(user_id)
        if reason not in {"logout", "auth_expired"}:
            raise ValueError("invalid_identity_end_reason")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            self._lock_identity(transaction, "owner", user_id)
            row = self._voice.get_live_session_record(
                transaction,
                owner_id=user_id,
                for_update=True,
            )
            if row is None:
                return None
            ended = self._end_session_row(
                transaction,
                row,
                reason=reason,
                now=now,
            )
        return ended

    def end_owned_sessions(
        self,
        *,
        owner_id: str,
        reason: str,
        now: datetime,
        batch_size: int = 1_000,
    ) -> tuple[VoiceSessionRecord, ...]:
        """Fence this coordinator replica's live rows before worker shutdown."""

        owner_id = _opaque(owner_id, "invalid_control_owner_id", max_length=128)
        if reason != "shutdown":
            raise ValueError("invalid_owned_end_reason")
        now = _aware(now, "invalid_current_time")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 1_000
        ):
            raise ValueError("invalid_owned_end_batch_size")
        with self._transaction() as transaction:
            rows = self._voice.list_owned_live_session_records_for_administration(
                transaction,
                control_owner_id=owner_id,
                limit=batch_size,
            )
            ended = tuple(
                self._end_session_row(
                    transaction,
                    row,
                    reason=reason,
                    now=now,
                )
                for row in rows
            )
        return ended

    def expire_session_leases(self, *, now: datetime) -> tuple[VoiceSessionRecord, ...]:
        """End expired reconnect/media leases without cancelling accepted work."""

        now = _aware(now, "invalid_current_time")
        expired: list[VoiceSessionRecord] = []
        with self._transaction() as transaction:
            rows = self._voice.list_expired_session_records_for_administration(
                transaction,
                now=now,
            )
            for row in rows:
                expired.append(
                    self._end_session_row(
                        transaction,
                        row,
                        reason="lease_expired",
                        now=now,
                    )
                )
        return tuple(expired)

    def reconcile_ended_unaccepted_turns(
        self,
        *,
        now: datetime,
        batch_size: int = 100,
    ) -> tuple[VoiceTurnRecord, ...]:
        """Boundedly repair pre-acceptance rows left by an already-ended session."""

        now = _aware(now, "invalid_current_time")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 1_000
        ):
            raise ValueError("invalid_reconciliation_batch_size")
        with self._transaction() as transaction:
            rows = self._voice.reconcile_ended_unaccepted_turns_for_administration(
                transaction,
                now=now,
                limit=batch_size,
            )
        return tuple(_turn(row) for row in rows)

    def reconcile_ended_terminal_operation_turns(
        self,
        *,
        now: datetime,
        batch_size: int = 100,
    ) -> tuple[VoiceTurnRecord, ...]:
        """Repair ended-session rows whose exact operation is terminal.

        Ending media deliberately does not cancel accepted work.  If inline
        terminal delivery is then lost to a process/socket failure, the shared
        operation record remains the durable outcome authority.  This bounded
        repair handles only ended media generations and only exact
        user/chat/request/connection correlations.  Success additionally
        requires the exact committed acceptance/result pair and execution
        generation; a completed operation without that proof fails closed as
        result-unavailable.
        """

        now = _aware(now, "invalid_current_time")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 1_000
        ):
            raise ValueError("invalid_reconciliation_batch_size")
        with self._transaction() as transaction:
            rows = self._voice.reconcile_ended_terminal_operation_turns_for_administration(
                transaction,
                now=now,
                limit=batch_size,
            )
        return tuple(_turn(row) for row in rows)

    def mark_session_active(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        expected_media_grant_revision: int,
        now: datetime,
    ) -> VoiceSessionRecord:
        """Move a prepared foreground session to active under both fences."""

        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            if row["state"] == "active":
                return _session(row)
            if (
                row["state"] not in {"starting", "reconnecting"}
                or not row["foreground_active"]
            ):
                raise VoiceSessionRepositoryError("invalid_session_transition")
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                updates={"state": "active", "updated_at": now},
                require_live=True,
            )
            if updated is None:
                raise StaleSessionFence("session_ended")
        return _session(updated)

    def renew_session_lease(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        expected_media_grant_revision: int,
        lease_duration: timedelta,
        now: datetime,
    ) -> VoiceSessionRecord:
        """Renew a live session lease from authenticated server receipt time."""

        now = _aware(now, "invalid_current_time")
        duration = _duration(lease_duration, "invalid_lease_duration", 5, 300)
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                updates={"lease_expires_at": now + duration, "updated_at": now},
                require_live=True,
            )
            if updated is None:
                raise StaleSessionFence("session_ended")
        return _session(updated)

    def refresh_media_grant(
        self,
        request: MediaGrantRefresh,
        *,
        now: datetime,
    ) -> SessionMutation:
        """Rotate grant metadata exactly once without persisting bearer material."""

        if not isinstance(request, MediaGrantRefresh):
            raise TypeError("request must be MediaGrantRefresh")
        now = _aware(now, "invalid_current_time")
        if request.issued_at > now:
            raise ValueError("invalid_grant_issued_at")
        if request.expires_at <= now:
            raise ValueError("invalid_media_grant_expiry")
        with self._transaction() as transaction:
            row = self._session_for_update(
                transaction,
                request.user_id,
                request.session_id,
            )
            self._assert_live(row)
            if _uuid_text(row.get("last_media_refresh_id")) == request.refresh_id:
                self._assert_refresh_replay(row, request, now)
                return SessionMutation(_session(row), replayed=True)
            self._assert_fences(
                row,
                request.expected_generation,
                request.expected_media_grant_revision,
            )
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=request.user_id,
                session_id=request.session_id,
                updates={
                    "media_grant_revision": int(row["media_grant_revision"]) + 1,
                    "participant_identity": request.participant_identity,
                    "media_grant_nonce_hash": request.nonce_hash,
                    "media_grant_issued_at": request.issued_at,
                    "media_grant_expires_at": request.expires_at,
                    "media_grant_consumed_at": None,
                    "last_media_refresh_id": request.refresh_id,
                    "updated_at": now,
                },
                require_live=True,
            )
            if updated is None:
                raise StaleSessionFence("session_ended")
        return SessionMutation(_session(updated))

    def assign_worker(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        assignment_id: str,
        worker_identity: str,
        issued_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> SessionMutation:
        """Install one idempotent worker assignment and its non-secret grant fence."""

        assignment_id = _uuid4(assignment_id, "invalid_assignment_id")
        worker_identity = _opaque(worker_identity, "invalid_worker_identity")
        issued_at = _aware(issued_at, "invalid_worker_grant_issued_at")
        expires_at = _aware(expires_at, "invalid_worker_grant_expires_at")
        now = _aware(now, "invalid_current_time")
        if issued_at < now:
            raise ValueError("invalid_worker_grant_issued_at")
        if (
            expires_at <= now
            or expires_at <= issued_at
            or expires_at > issued_at + timedelta(minutes=5)
        ):
            raise ValueError("invalid_worker_grant_expiry")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            if int(row["generation"]) != expected_generation:
                raise StaleSessionFence("stale_generation")
            existing = _uuid_text(row.get("worker_assignment_id"))
            if existing is not None:
                if (
                    existing == assignment_id
                    and row.get("worker_identity") == worker_identity
                    and row.get("worker_rtc_grant_issued_at") == issued_at
                    and row.get("worker_rtc_grant_expires_at") == expires_at
                ):
                    return SessionMutation(_session(row), replayed=True)
                raise IdempotencyConflict("worker_assignment_owned")
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=session_id,
                updates={
                    "worker_assignment_id": assignment_id,
                    "worker_identity": worker_identity,
                    "worker_rtc_grant_issued_at": issued_at,
                    "worker_rtc_grant_expires_at": expires_at,
                    "updated_at": now,
                },
                require_live=True,
            )
            if updated is None:
                raise StaleSessionFence("session_ended")
        return SessionMutation(_session(updated))

    async def claim_control_lease(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        owner_id: str,
        now: datetime,
    ) -> ControlLeaseState:
        """Claim or renew one coordinator replica's durable control lease."""

        return await asyncio.to_thread(
            self._claim_control_lease_sync,
            user_id=user_id,
            session_id=session_id,
            generation=generation,
            owner_id=owner_id,
            now=now,
        )

    def _claim_control_lease_sync(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        owner_id: str,
        now: datetime,
    ) -> ControlLeaseState:
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            state = ControlLeaseState(
                generation=int(row["generation"]),
                owner_id=row.get("control_owner_id"),
                expires_at=row.get("control_lease_expires_at"),
            )
            claimed = self._control_leases.claim(
                state,
                generation=generation,
                owner_id=owner_id,
                now=now,
            )
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                updates={
                    "control_owner_id": claimed.owner_id,
                    "control_lease_expires_at": claimed.expires_at,
                    "updated_at": now,
                },
                require_live=True,
            )
            if updated is None:
                raise StaleSessionFence("session_ended")
        return claimed

    def renew_owned_control_leases(
        self,
        *,
        owner_id: str,
        now: datetime,
        batch_size: int = 1_000,
    ) -> tuple[VoiceSessionRecord, ...]:
        """Renew only still-live, still-owned coordinator control leases.

        Expired leases are deliberately not resurrected: once the expiry fence
        has passed, another replica is entitled to claim the session.  The
        bounded maintenance pass renews healthy ownership before that point.
        """

        owner_id = _opaque(owner_id, "invalid_control_owner_id", max_length=128)
        now = _aware(now, "invalid_current_time")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= 10_000
        ):
            raise ValueError("invalid_control_lease_batch_size")
        renewed: list[VoiceSessionRecord] = []
        with self._transaction() as transaction:
            rows = self._voice.list_renewable_control_session_records_for_administration(
                transaction,
                control_owner_id=owner_id,
                now=now,
                limit=batch_size,
            )
            for row in rows:
                claimed = self._control_leases.claim(
                    ControlLeaseState(
                        generation=int(row["generation"]),
                        owner_id=row.get("control_owner_id"),
                        expires_at=row.get("control_lease_expires_at"),
                    ),
                    generation=int(row["generation"]),
                    owner_id=owner_id,
                    now=now,
                )
                updated = self._voice.patch_session_record(
                    transaction,
                    owner_id=str(row["user_id"]),
                    session_id=str(row["session_id"]),
                    updates={
                        "control_lease_expires_at": claimed.expires_at,
                        "updated_at": now,
                    },
                    require_live=True,
                )
                if (
                    updated is not None
                    and updated.get("control_owner_id") == owner_id
                    and updated.get("control_lease_expires_at") == claimed.expires_at
                ):
                    renewed.append(_session(updated))
        return tuple(renewed)

    async def release_control_lease(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        owner_id: str,
    ) -> bool:
        """Release the lease only when the supplied replica still owns it."""

        return await asyncio.to_thread(
            self._release_control_lease_sync,
            user_id=user_id,
            session_id=session_id,
            generation=generation,
            owner_id=owner_id,
        )

    def _release_control_lease_sync(
        self,
        *,
        user_id: str,
        session_id: str,
        generation: int,
        owner_id: str,
    ) -> bool:
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            state = ControlLeaseState(
                generation=int(row["generation"]),
                owner_id=row.get("control_owner_id"),
                expires_at=row.get("control_lease_expires_at"),
            )
            released = self._control_leases.release(
                state,
                generation=generation,
                owner_id=owner_id,
            )
            if released == state:
                return False
            released_record = self._voice.release_control_lease_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
            )
            if not released_record:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_control_release_failed")
        return True

    async def claim_announcement(
        self,
        *,
        user_id: str,
        request: AnnouncementClaimRequest,
        now: datetime,
    ) -> AnnouncementMutation:
        """Reserve one content-free speech quantum under the turn row lock."""

        return await asyncio.to_thread(
            self._claim_announcement_sync,
            user_id=user_id,
            request=request,
            now=now,
        )

    def _claim_announcement_sync(
        self,
        *,
        user_id: str,
        request: AnnouncementClaimRequest,
        now: datetime,
    ) -> AnnouncementMutation:
        user_id = _user_id(user_id)
        if not isinstance(request, AnnouncementClaimRequest):
            raise TypeError("request must be AnnouncementClaimRequest")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._voice.get_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=request.turn_id,
                for_update=True,
            )
            if row is None or str(row["session_id"]) != request.session_id:
                raise VoiceSessionNotFound("voice_turn_not_found")
            session = self._session_for_update(
                transaction,
                user_id,
                request.session_id,
            )
            expected_revision = request.expected_media_grant_revision
            if expected_revision is not None and (
                int(row["media_grant_revision"]) != expected_revision
                or int(session["generation"]) != request.generation
                or int(session["media_grant_revision"]) != expected_revision
            ):
                raise StaleSessionFence("stale_media_grant_revision")
            chat_available = self._voice.chat_exists(
                transaction,
                owner_id=user_id,
                chat_id=str(row["chat_id"]),
            )
            rejection_reason = request.authorized_preacceptance_rejection_reason
            preacceptance_authorized = rejection_reason is not None
            if preacceptance_authorized:
                if rejection_reason not in PREACCEPTANCE_REJECTION_PHRASES:
                    raise ClaimUnavailable(
                        "preacceptance_refusal_not_authorized"
                    )
                if (
                    str(row["state"]) != "abandoned"
                    or row.get("rejection_reason") != rejection_reason
                    or row.get("message_id") is not None
                    or row.get("acceptance_commit_id") is not None
                    or row.get("accepted_at") is not None
                    or row.get("operation_id") is not None
                    or expected_revision is None
                    or session.get("ended_at") is not None
                    or str(session.get("state")) != "active"
                ):
                    raise ClaimUnavailable(
                        "preacceptance_refusal_not_authorized"
                    )
                if int(row["announcement_sequence"]) != 0:
                    raise ClaimUnavailable(
                        "preacceptance_refusal_already_announced"
                    )
            state = AnnouncementState(
                generation=int(row["session_generation"]),
                announcement_sequence=int(row["announcement_sequence"]),
                result_reserved_samples=int(row["result_reserved_samples"]),
                result_quantum_count=int(row["result_quantum_count"]),
                last_announcement_kind=row.get("last_announcement_kind"),
                last_phrase_key=row.get("last_phrase_key"),
                announcement_claim_id=_uuid_text(
                    row.get("announcement_claim_id")
                ),
                announcement_claim_expires_at=_optional_aware(
                    row.get("announcement_claim_expires_at")
                ),
                terminal=(
                    str(row["state"]) in _TERMINAL_TURN_STATES
                    and not request.authorized_terminal_sensitive_recap
                    and not preacceptance_authorized
                ),
                speech_enabled=(
                    session.get("ended_at") is None
                    and not bool(session["speech_muted"])
                ),
                origin_available=(
                    chat_available
                    and row.get("origin_chat_unavailable_at") is None
                    and session.get("chat_unavailable_at") is None
                ),
            )
            if request.authorized_terminal_sensitive_recap and not (
                str(row["state"]) == "succeeded"
                and str(row.get("sensitivity") or "unknown") == "sensitive"
                and row.get("result_commit_id") is not None
            ):
                raise ClaimUnavailable("sensitive_recap_not_authorized")
            mutation = self._announcements.claim(state, request, now=now)
            claimed = mutation.state
            next_due_at = (
                now + timedelta(seconds=CADENCE_TARGET_SECONDS)
                if request.kind in {"acknowledgement", "progress"}
                else None
            )
            updated = self._voice.patch_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=request.turn_id,
                updates={
                    "announcement_sequence": claimed.announcement_sequence,
                    "result_reserved_samples": claimed.result_reserved_samples,
                    "result_quantum_count": claimed.result_quantum_count,
                    "last_announcement_kind": claimed.last_announcement_kind,
                    "last_phrase_key": claimed.last_phrase_key,
                    "next_announcement_due_at": next_due_at,
                    "announcement_claim_id": claimed.announcement_claim_id,
                    "announcement_claim_expires_at": (
                        claimed.announcement_claim_expires_at
                    ),
                    "last_announcement_started_at": now,
                    "updated_at": now,
                },
            )
            if updated is None:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_announcement_claim_failed")
        return mutation

    async def complete_announcement(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        generation: int,
        claim_id: str,
    ) -> bool:
        """Release only the exact durable speech reservation claim."""

        return await asyncio.to_thread(
            self._complete_announcement_sync,
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
            generation=generation,
            claim_id=claim_id,
        )

    def _complete_announcement_sync(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        generation: int,
        claim_id: str,
    ) -> bool:
        user_id = _user_id(user_id)
        session_id = _uuid4(session_id, "invalid_session_id")
        turn_id = _uuid4(turn_id, "invalid_turn_id")
        claim_id = _uuid4(claim_id, "invalid_announcement_claim_id")
        _positive(generation, "invalid_generation")
        with self._transaction() as transaction:
            row = self._voice.get_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                for_update=True,
            )
            if row is None or str(row["session_id"]) != session_id:
                raise VoiceSessionNotFound("voice_turn_not_found")
            if row.get("announcement_claim_id") is None:
                return False
            state = AnnouncementState(
                generation=int(row["session_generation"]),
                announcement_sequence=int(row["announcement_sequence"]),
                result_reserved_samples=int(row["result_reserved_samples"]),
                result_quantum_count=int(row["result_quantum_count"]),
                last_announcement_kind=row.get("last_announcement_kind"),
                last_phrase_key=row.get("last_phrase_key"),
                announcement_claim_id=_uuid_text(
                    row.get("announcement_claim_id")
                ),
                announcement_claim_expires_at=_optional_aware(
                    row.get("announcement_claim_expires_at")
                ),
                terminal=str(row["state"]) in _TERMINAL_TURN_STATES,
            )
            completed = self._announcements.complete(
                state,
                generation=generation,
                claim_id=claim_id,
            )
            updated = self._voice.complete_announcement_claim(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                claim_id=completed.announcement_claim_id,
                claim_expires_at=completed.announcement_claim_expires_at,
            )
            if not updated:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_announcement_completion_failed")
        return True

    def request_chat_context_update(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        expected_media_grant_revision: int,
        expected_chat_context_revision: int,
        visible_chat_id: str,
        now: datetime,
    ) -> SessionMutation:
        """Advance desired context only after the prior desired value was applied."""

        visible_chat_id = _uuid4(visible_chat_id, "invalid_visible_chat_id")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            if int(row["chat_context_revision"]) != expected_chat_context_revision:
                raise StaleSessionFence("stale_chat_context_revision")
            if row["visible_chat_id"] == visible_chat_id:
                return SessionMutation(_session(row), replayed=True)
            if not _context_synced(row):
                raise ContextSyncPending("chat_context_sync_pending")
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                updates={
                    "visible_chat_id": visible_chat_id,
                    "chat_context_revision": int(row["chat_context_revision"]) + 1,
                    "last_interaction_at": now,
                    "idle_started_at": None,
                    "updated_at": now,
                },
            )
            if updated is None:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_chat_context_update_failed")
        return SessionMutation(_session(updated))

    def apply_chat_context(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        expected_media_grant_revision: int,
        control_owner_id: str,
        visible_chat_id: str,
        chat_context_revision: int,
        now: datetime,
    ) -> SessionMutation:
        """Apply only the exact desired context from the live control owner."""

        control_owner_id = _opaque(
            control_owner_id,
            "invalid_control_owner_id",
            max_length=128,
        )
        visible_chat_id = _uuid4(visible_chat_id, "invalid_visible_chat_id")
        _positive(chat_context_revision, "invalid_chat_context_revision")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            if (
                row.get("control_owner_id") != control_owner_id
                or row.get("control_lease_expires_at") is None
                or now >= row["control_lease_expires_at"]
            ):
                raise ClaimUnavailable("control_lease_not_owned")
            if (
                row["visible_chat_id"] != visible_chat_id
                or int(row["chat_context_revision"]) != chat_context_revision
            ):
                raise StaleSessionFence("stale_chat_context_revision")
            if (
                row.get("applied_visible_chat_id") == visible_chat_id
                and row.get("applied_chat_context_revision") == chat_context_revision
            ):
                return SessionMutation(_session(row), replayed=True)
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                updates={
                    "applied_visible_chat_id": visible_chat_id,
                    "applied_chat_context_revision": chat_context_revision,
                    "updated_at": now,
                },
            )
            if updated is None:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_chat_context_apply_failed")
        return SessionMutation(_session(updated))

    def bind_recognition_turn(
        self,
        request: RecognitionBinding,
        *,
        now: datetime,
    ) -> TurnMutation:
        """Allocate immutable turn/submission IDs once for a worker VAD start."""

        if not isinstance(request, RecognitionBinding):
            raise TypeError("request must be RecognitionBinding")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            self._lock_identity(
                transaction,
                "turn",
                request.user_id,
                request.client_turn_id,
            )
            existing = self._voice.get_client_turn_record(
                transaction,
                owner_id=request.user_id,
                client_turn_id=request.client_turn_id,
                for_update=True,
            )
            if existing is not None:
                if not _turn_binding_matches(existing, request):
                    raise IdempotencyConflict("client_turn_binding_mismatch")
                return TurnMutation(_turn(existing), replayed=True)
            session = self._session_for_update(
                transaction,
                request.user_id,
                request.session_id,
            )
            self._assert_live(session)
            self._assert_fences(
                session,
                request.session_generation,
                request.media_grant_revision,
            )
            if session["state"] != "active":
                raise VoiceSessionRepositoryError("session_not_active")
            if (
                session.get("control_owner_id") != request.control_owner_id
                or session.get("control_lease_expires_at") is None
                or now >= session["control_lease_expires_at"]
            ):
                raise ClaimUnavailable("control_lease_not_owned")
            if (
                not _context_synced(session)
                or session["visible_chat_id"] != request.chat_id
                or int(session["chat_context_revision"])
                != request.chat_context_revision
            ):
                raise StaleSessionFence("stale_chat_context_revision")
            turn_id = self._new_uuid4("turn_id")
            submission_id = self._new_uuid4("submission_id")
            request_generation = self._new_uuid4("request_generation")
            result_request_generation = self._new_uuid4(
                "result_request_generation"
            )
            row = self._voice.insert_turn_record(
                transaction,
                values={
                    "turn_id": turn_id,
                    "client_turn_id": request.client_turn_id,
                    "session_id": request.session_id,
                    "session_generation": request.session_generation,
                    "media_grant_revision": request.media_grant_revision,
                    "user_id": request.user_id,
                    "chat_id": request.chat_id,
                    "chat_context_revision": request.chat_context_revision,
                    "execution_base_render_revision": (
                        request.execution_base_render_revision
                    ),
                    "submission_id": submission_id,
                    "request_generation": request_generation,
                    "result_request_generation": result_request_generation,
                    "state": "recognizing",
                    "is_foreground": False,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return TurnMutation(_turn(row))

    async def bind_worker_recognition(
        self,
        *,
        start: RecognitionStart,
        control_owner_id: str,
        now: datetime,
    ) -> TurnMutation:
        """Bind an authenticated worker start without trusting worker ownership data."""

        return await asyncio.to_thread(
            self._bind_worker_recognition_sync,
            start=start,
            control_owner_id=control_owner_id,
            now=now,
        )

    def _bind_worker_recognition_sync(
        self,
        *,
        start: RecognitionStart,
        control_owner_id: str,
        now: datetime,
    ) -> TurnMutation:
        if not isinstance(start, RecognitionStart):
            raise TypeError("start must be RecognitionStart")
        control_owner_id = _opaque(
            control_owner_id,
            "invalid_control_owner_id",
            max_length=128,
        )
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            session = self._voice.get_session_record_for_administration(
                transaction,
                session_id=start.session_id,
                for_update=True,
            )
            if session is None:
                raise VoiceSessionNotFound("voice_session_not_found")
            self._assert_live(session)
            self._assert_fences(
                session,
                start.generation,
                start.media_grant_revision,
            )
            if session["state"] != "active":
                raise VoiceSessionRepositoryError("session_not_active")
            if (
                _uuid_text(session.get("worker_assignment_id")) != start.assignment_id
                or session.get("worker_identity") != start.worker_identity
                or session.get("worker_rtc_grant_expires_at") is None
                or now >= session["worker_rtc_grant_expires_at"]
            ):
                raise StaleSessionFence("stale_worker_assignment")
            if (
                session.get("control_owner_id") != control_owner_id
                or session.get("control_lease_expires_at") is None
                or now >= session["control_lease_expires_at"]
            ):
                raise ClaimUnavailable("control_lease_not_owned")
            if (
                not _context_synced(session)
                or session["visible_chat_id"] != start.chat_id
                or int(session["chat_context_revision"]) != start.chat_context_revision
            ):
                raise StaleSessionFence("stale_chat_context_revision")
            user_id = str(session["user_id"])
            self._lock_identity(
                transaction,
                "turn",
                user_id,
                start.client_turn_id,
            )
            existing = self._voice.get_client_turn_record(
                transaction,
                owner_id=user_id,
                client_turn_id=start.client_turn_id,
                for_update=True,
            )
            if existing is not None:
                if (
                    str(existing["session_id"]) != start.session_id
                    or int(existing["session_generation"]) != start.generation
                    or int(existing["media_grant_revision"])
                    != start.media_grant_revision
                    or str(existing["chat_id"]) != start.chat_id
                    or int(existing["chat_context_revision"])
                    != start.chat_context_revision
                ):
                    raise IdempotencyConflict("client_turn_binding_mismatch")
                return TurnMutation(_turn(existing), replayed=True)
            render_revision = self._voice.get_chat_render_revision(
                transaction,
                owner_id=user_id,
                chat_id=start.chat_id,
                for_share=True,
            )
            if render_revision is None:
                raise VoiceSessionNotFound("voice_chat_not_found")
            turn_id = self._new_uuid4("turn_id")
            submission_id = self._new_uuid4("submission_id")
            request_generation = self._new_uuid4("request_generation")
            result_request_generation = self._new_uuid4(
                "result_request_generation"
            )
            row = self._voice.insert_turn_record(
                transaction,
                values={
                    "turn_id": turn_id,
                    "client_turn_id": start.client_turn_id,
                    "session_id": start.session_id,
                    "session_generation": start.generation,
                    "media_grant_revision": start.media_grant_revision,
                    "user_id": user_id,
                    "chat_id": start.chat_id,
                    "chat_context_revision": start.chat_context_revision,
                    "execution_base_render_revision": render_revision,
                    "submission_id": submission_id,
                    "request_generation": request_generation,
                    "result_request_generation": result_request_generation,
                    "state": "recognizing",
                    "is_foreground": False,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return TurnMutation(_turn(row))

    async def reject_worker_recognition(
        self,
        *,
        binding: TranscriptTurnBinding,
        control_owner_id: str,
        now: datetime,
    ) -> TurnMutation:
        """Abandon one authenticated pre-final ASR failure without content."""

        return await asyncio.to_thread(
            self._abandon_worker_recognition_sync,
            binding=binding,
            control_owner_id=control_owner_id,
            now=now,
            retry_policy="explicit_user_retry",
        )

    async def suppress_worker_self_speech(
        self,
        *,
        binding: TranscriptTurnBinding,
        control_owner_id: str,
        now: datetime,
    ) -> TurnMutation:
        """Content-freely abandon recognized playback without inviting a retry."""

        return await asyncio.to_thread(
            self._abandon_worker_recognition_sync,
            binding=binding,
            control_owner_id=control_owner_id,
            now=now,
            retry_policy="none",
        )

    def _abandon_worker_recognition_sync(
        self,
        *,
        binding: TranscriptTurnBinding,
        control_owner_id: str,
        now: datetime,
        retry_policy: str,
    ) -> TurnMutation:
        if not isinstance(binding, TranscriptTurnBinding):
            raise TypeError("binding must be TranscriptTurnBinding")
        if retry_policy not in {"explicit_user_retry", "none"}:
            raise ValueError("invalid_recognition_retry_policy")
        control_owner_id = _opaque(
            control_owner_id,
            "invalid_control_owner_id",
            max_length=128,
        )
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            session = self._voice.get_session_record_for_administration(
                transaction,
                session_id=binding.session_id,
                for_update=True,
            )
            if session is None:
                raise VoiceSessionNotFound("voice_session_not_found")
            self._assert_live(session)
            if int(session["generation"]) != binding.generation:
                raise StaleSessionFence("stale_generation")
            if (
                _uuid_text(session.get("worker_assignment_id"))
                != binding.assignment_id
                or session.get("worker_identity") != binding.worker_identity
            ):
                raise StaleSessionFence("stale_worker_assignment")
            if (
                session.get("control_owner_id") != control_owner_id
                or session.get("control_lease_expires_at") is None
                or now >= session["control_lease_expires_at"]
            ):
                raise ClaimUnavailable("control_lease_not_owned")
            row = self._voice.get_turn_record_for_administration(
                transaction,
                turn_id=binding.turn_id,
                for_update=True,
            )
            if row is None:
                raise VoiceSessionNotFound("voice_turn_not_found")
            expected = {
                "session_id": binding.session_id,
                "session_generation": binding.generation,
                "media_grant_revision": binding.media_grant_revision,
                "turn_id": binding.turn_id,
                "client_turn_id": binding.client_turn_id,
                "submission_id": binding.submission_id,
                "request_generation": binding.request_generation,
                "chat_id": binding.chat_id,
                "chat_context_revision": binding.chat_context_revision,
            }
            for name, value in expected.items():
                actual = row[name]
                if name in {
                    "session_generation",
                    "media_grant_revision",
                    "chat_context_revision",
                }:
                    actual = int(actual)
                else:
                    actual = str(actual)
                if actual != value:
                    raise StaleSessionFence("recognition_binding_conflict")
            if row["state"] == "abandoned":
                if (
                    row.get("rejection_reason") == "malformed_final"
                    and row.get("rejection_retry_policy") == retry_policy
                ):
                    return TurnMutation(_turn(row), replayed=True)
                raise IdempotencyConflict("transcript_rejection_conflict")
            if row["state"] != "recognizing":
                raise VoiceSessionRepositoryError("voice_turn_already_accepted")
            row = self._voice.patch_turn_record(
                transaction,
                owner_id=str(row["user_id"]),
                turn_id=binding.turn_id,
                updates={
                    "state": "abandoned",
                    "terminal_kind": "abandoned",
                    "rejection_reason": "malformed_final",
                    "rejection_retry_policy": retry_policy,
                    "terminal_at": now,
                    "updated_at": now,
                },
                expected_states=("recognizing",),
            )
            if row is None:
                raise StaleSessionFence("voice_turn_rejection_lost")
        return TurnMutation(_turn(row))

    def admit_transcript(
        self,
        request: TranscriptSubmission,
        *,
        worker_control_secret: bytes,
        now: datetime,
    ) -> TranscriptAdmission:
        """Verify one final and move only its content-free row to submitting."""

        if not isinstance(request, TranscriptSubmission):
            raise TypeError("request must be TranscriptSubmission")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._voice.get_turn_record(
                transaction,
                owner_id=request.user_id,
                turn_id=request.turn_id,
                for_update=True,
            )
            if row is None:
                raise TranscriptSubmissionRejected(
                    "invalid_binding",
                    "explicit_user_retry",
                )
            session = self._voice.get_session_record(
                transaction,
                owner_id=request.user_id,
                session_id=request.session_id,
                for_update=True,
            )
            if session is None:
                raise TranscriptSubmissionRejected("stale_session", "none")
            self._assert_transcript_binding(row, session, request)
            try:
                binding = TranscriptProofBinding(
                    session_id=request.session_id,
                    generation=request.generation,
                    media_grant_revision=request.media_grant_revision,
                    assignment_id=str(session["worker_assignment_id"]),
                    worker_identity=request.source_participant_identity,
                    turn_id=request.turn_id,
                    client_turn_id=request.client_turn_id,
                    submission_id=request.submission_id,
                    request_generation=request.request_generation,
                    chat_id=request.chat_id,
                    chat_context_revision=request.chat_context_revision,
                    detected_language=request.detected_language,
                )
                canonical = verify_transcript_proof(
                    worker_control_secret,
                    binding,
                    request.text,
                    text_digest_sha256=request.text_digest_sha256,
                    transcript_proof=request.transcript_proof,
                    proof_expires_at=request.proof_expires_at,
                    now=now,
                )
            except TranscriptProofError as exc:
                if exc.code == "transcript_proof_expired":
                    reason = "proof_expired"
                elif exc.code in {
                    "empty_transcript",
                    "invalid_transcript_text",
                    "noncanonical_transcript",
                    "transcript_text_too_large",
                }:
                    reason = "malformed_final"
                else:
                    reason = "invalid_proof"
                raise TranscriptSubmissionRejected(
                    reason,
                    "explicit_user_retry",
                ) from None
            replayed = row["state"] == "submitting"
            if row["state"] == "recognizing":
                policy, output_reason = _language_policy(request.detected_language)
                result_request_generation = (
                    _uuid_text(row.get("result_request_generation"))
                    or self._new_uuid4("result_request_generation")
                )
                row = self._voice.patch_turn_record(
                    transaction,
                    owner_id=request.user_id,
                    turn_id=request.turn_id,
                    updates={
                        "state": "submitting",
                        "detected_language": request.detected_language,
                        "spoken_output_policy": policy,
                        "output_reason": output_reason,
                        "result_request_generation": result_request_generation,
                        "updated_at": now,
                    },
                    expected_states=("recognizing",),
                )
                if row is None:
                    raise TranscriptSubmissionRejected("invalid_binding", "none")
            elif row["state"] != "submitting":
                raise TranscriptSubmissionRejected("invalid_binding", "none")
            elif row.get("detected_language") != request.detected_language:
                raise TranscriptSubmissionRejected(
                    "invalid_binding",
                    "explicit_user_retry",
                )
            elif row.get("result_request_generation") is None:
                row = self._voice.patch_turn_record(
                    transaction,
                    owner_id=request.user_id,
                    turn_id=request.turn_id,
                    updates={
                        "result_request_generation": self._new_uuid4(
                            "result_request_generation"
                        ),
                        "updated_at": now,
                    },
                    expected_states=("submitting",),
                )
                if row is None:
                    raise TranscriptSubmissionRejected("invalid_binding", "none")
        return TranscriptAdmission(
            canonical_text=canonical,
            turn=_turn(row),
            replayed=replayed,
        )

    def admit_local_transcript(
        self,
        request: LocalTranscriptSubmission,
        *,
        now: datetime,
    ) -> TranscriptAdmission:
        """Admit a server-attested local final through the sibling lane.

        This deliberately shares the durable turn transition and return type
        with the remote lane while retaining separate authority: there is no
        worker assignment, HMAC secret, proof, endpoint, or credential.
        """

        if not isinstance(request, LocalTranscriptSubmission):
            raise TypeError("request must be LocalTranscriptSubmission")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._voice.get_turn_record(
                transaction,
                owner_id=request.user_id,
                turn_id=request.turn_id,
                for_update=True,
            )
            if row is None:
                raise TranscriptSubmissionRejected("invalid_binding", "none")
            session = self._voice.get_session_record(
                transaction,
                owner_id=request.user_id,
                session_id=request.session_id,
                for_update=True,
            )
            if session is None or session.get("ended_at") is not None:
                raise TranscriptSubmissionRejected("stale_session", "none")
            expected = {
                "session_id": request.session_id,
                "session_generation": request.generation,
                "media_grant_revision": request.speech_revision,
                "turn_id": request.turn_id,
                "client_turn_id": request.client_turn_id,
                "submission_id": request.submission_id,
                "request_generation": request.request_generation,
                "chat_id": request.chat_id,
                "chat_context_revision": request.chat_context_revision,
            }
            binding_matches = all(
                (
                    int(row[name]) == value
                    if name
                    in {
                        "session_generation",
                        "media_grant_revision",
                        "chat_context_revision",
                    }
                    else str(row[name]) == value
                )
                for name, value in expected.items()
            )
            session_matches = (
                session.get("speech_backend") == "client_local"
                and session.get("worker_assignment_id") is None
                and str(session["user_id"]) == request.user_id
                and str(session["device_id"]) == request.device_id
                and str(session["owner_connection_generation"])
                == request.connection_generation
                and str(session["control_binding_id"]) == request.binding_id
                and session["control_binding_expires_at"] > now
                and session["lease_expires_at"] > now
                and int(session["generation"]) == request.generation
                and int(session["media_grant_revision"])
                == request.speech_revision
                and session["state"] == "active"
                and bool(session["foreground_active"])
                and bool(session["microphone_enabled"])
                and not bool(session["speech_muted"])
                and str(session["visible_chat_id"]) == request.chat_id
                and int(session["chat_context_revision"])
                == request.chat_context_revision
                and session.get("applied_visible_chat_id")
                == session.get("visible_chat_id")
                and session.get("applied_chat_context_revision")
                == session.get("chat_context_revision")
            )
            if not binding_matches or not session_matches:
                raise TranscriptSubmissionRejected("invalid_binding", "none")
            replayed = row["state"] == "submitting"
            if row["state"] == "recognizing":
                policy, output_reason = _language_policy(request.detected_language)
                result_request_generation = (
                    _uuid_text(row.get("result_request_generation"))
                    or self._new_uuid4("result_request_generation")
                )
                row = self._voice.patch_turn_record(
                    transaction,
                    owner_id=request.user_id,
                    turn_id=request.turn_id,
                    updates={
                        "state": "submitting",
                        "detected_language": request.detected_language,
                        "spoken_output_policy": policy,
                        "output_reason": output_reason,
                        "result_request_generation": result_request_generation,
                        "updated_at": now,
                    },
                    expected_states=("recognizing",),
                )
                if row is None:
                    raise TranscriptSubmissionRejected("invalid_binding", "none")
            elif row["state"] != "submitting":
                raise TranscriptSubmissionRejected("invalid_binding", "none")
            elif row.get("detected_language") != request.detected_language:
                raise TranscriptSubmissionRejected("invalid_binding", "none")
        return TranscriptAdmission(
            canonical_text=request.canonical_text,
            turn=_turn(row),
            replayed=replayed,
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
        """Persist one idempotent pre-acceptance rejection without content."""

        user_id = _user_id(user_id)
        turn_id = _uuid4(turn_id, "invalid_turn_id")
        if reason not in {
            "capacity_exhausted",
            "chat_unavailable",
            "invalid_binding",
            "invalid_proof",
            "proof_expired",
            "permission_denied",
            "stale_session",
            "malformed_final",
        }:
            raise ValueError("invalid_rejection_reason")
        if retry_policy not in {"explicit_user_retry", "none"}:
            raise ValueError("invalid_retry_policy")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._voice.get_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                for_update=True,
            )
            if row is None:
                raise VoiceSessionNotFound("voice_turn_not_found")
            if row["state"] == "abandoned":
                if (
                    row.get("rejection_reason") == reason
                    and row.get("rejection_retry_policy") == retry_policy
                ):
                    return TurnMutation(_turn(row), replayed=True)
                raise IdempotencyConflict("transcript_rejection_conflict")
            if row["state"] not in {"recognizing", "submitting"}:
                raise VoiceSessionRepositoryError("voice_turn_already_accepted")
            row = self._voice.patch_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                updates={
                    "state": "abandoned",
                    "terminal_kind": "abandoned",
                    "rejection_reason": reason,
                    "rejection_retry_policy": retry_policy,
                    "terminal_at": now,
                    "updated_at": now,
                },
                expected_states=("recognizing", "submitting"),
            )
            if row is None:
                raise VoiceSessionRepositoryError("voice_turn_already_accepted")
        return TurnMutation(_turn(row))

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
        """Atomically bind ordinary message acceptance and foreground work.

        ``transaction`` is the narrow publication integration seam: the
        conversation repository may supply its already-fenced transaction so the
        user bubble, linked private result stage, and voice correlation either
        all commit or all roll back.
        """

        user_id = _user_id(user_id)
        turn_id = _uuid4(turn_id, "invalid_turn_id")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id < 1
        ):
            raise ValueError("invalid_message_id")
        accepted_connection_generation = _uuid4(
            accepted_connection_generation,
            "invalid_connection_generation",
        )
        acceptance_commit_id = (
            None
            if acceptance_commit_id is None
            else _uuid4(acceptance_commit_id, "invalid_acceptance_commit_id")
        )
        operation_id = (
            None
            if operation_id is None
            else _uuid4(operation_id, "invalid_operation_id")
        )
        result_commit_id = (
            None
            if result_commit_id is None
            else _uuid4(result_commit_id, "invalid_result_commit_id")
        )
        now = _aware(now, "invalid_current_time")
        with self._transaction_or_existing(transaction) as plane_transaction:
            row = self._voice.get_turn_record(
                plane_transaction,
                owner_id=user_id,
                turn_id=turn_id,
                for_update=True,
            )
            if row is None:
                raise VoiceSessionNotFound("voice_turn_not_found")
            replay_fields = (
                int(row["message_id"] or 0) == message_id
                and _uuid_text(row.get("accepted_connection_generation"))
                == accepted_connection_generation
                and _uuid_text(row.get("acceptance_commit_id"))
                == acceptance_commit_id
                and _uuid_text(row.get("result_commit_id"))
                == result_commit_id
                and _uuid_text(row.get("operation_id")) == operation_id
            )
            if row["state"] != "submitting":
                if row.get("accepted_at") is not None and replay_fields:
                    return TurnMutation(_turn(row), replayed=True)
                raise VoiceSessionRepositoryError("voice_turn_not_submitting")
            session = self._session_for_update(
                plane_transaction,
                user_id,
                str(row["session_id"]),
            )
            self._assert_live(session)
            self._assert_fences(
                session,
                int(row["session_generation"]),
                int(row["media_grant_revision"]),
            )
            if (
                str(session["visible_chat_id"]) != str(row["chat_id"])
                or int(session["chat_context_revision"])
                != int(row["chat_context_revision"])
            ):
                raise StaleSessionFence("stale_chat_context_revision")
            self._voice.clear_foreground_turns(
                plane_transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                now=now,
                except_turn_id=turn_id,
            )
            accepted = self._voice.patch_turn_record(
                plane_transaction,
                owner_id=user_id,
                turn_id=turn_id,
                updates={
                    "message_id": message_id,
                    "accepted_connection_generation": accepted_connection_generation,
                    "acceptance_commit_id": acceptance_commit_id,
                    "result_commit_id": result_commit_id,
                    "operation_id": operation_id,
                    "state": "processing",
                    "is_foreground": True,
                    "accepted_at": now,
                    "processing_started_at": now,
                    "updated_at": now,
                },
                expected_states=("submitting",),
            )
            if accepted is None:
                raise StaleSessionFence("voice_turn_acceptance_lost")
        return TurnMutation(_turn(accepted))

    def get_turn_by_submission(
        self,
        *,
        user_id: str,
        submission_id: str,
        request_generation: str,
    ) -> VoiceTurnRecord:
        """Resolve an exact owner-scoped submission tuple for replay handling."""

        user_id = _user_id(user_id)
        submission_id = _uuid4(submission_id, "invalid_submission_id")
        request_generation = _uuid4(
            request_generation,
            "invalid_request_generation",
        )
        with self._transaction() as transaction:
            row = self._voice.get_submission_record(
                transaction,
                owner_id=user_id,
                submission_id=submission_id,
                request_generation=request_generation,
            )
            if row is None:
                raise VoiceSessionNotFound("voice_turn_not_found")
        return _turn(row)

    def select_foreground_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        expected_generation: int,
        now: datetime,
    ) -> VoiceTurnRecord:
        """Select one foreground turn without cancelling any earlier work."""

        turn_id = _uuid4(turn_id, "invalid_turn_id")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            session = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(session)
            if int(session["generation"]) != expected_generation:
                raise StaleSessionFence("stale_generation")
            target = self._voice.get_turn_record(
                transaction,
                owner_id=_user_id(user_id),
                turn_id=turn_id,
                for_update=True,
            )
            if target is None or str(target["session_id"]) != _uuid4(
                session_id,
                "invalid_session_id",
            ):
                raise VoiceSessionNotFound("voice_turn_not_found")
            if target["state"] in _TERMINAL_TURN_STATES:
                raise VoiceSessionRepositoryError("voice_turn_terminal")
            # Clear before setting: PostgreSQL's immediate partial-unique check
            # can otherwise observe the new foreground row before it visits the
            # previous one within a single CASE update.
            self._voice.clear_foreground_turns(
                transaction,
                owner_id=user_id,
                session_id=session_id,
                now=now,
            )
            updated = self._voice.patch_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                updates={"is_foreground": True, "updated_at": now},
            )
            if updated is None:  # pragma: no cover - locked row invariant.
                raise VoiceSessionNotFound("voice_turn_not_found")
        return _turn(updated)

    def get_turn(self, *, user_id: str, turn_id: str) -> VoiceTurnRecord:
        """Return an owner-scoped voice turn."""

        user_id = _user_id(user_id)
        turn_id = _uuid4(turn_id, "invalid_turn_id")
        with self._transaction() as transaction:
            row = self._voice.get_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
            )
            if row is None:
                raise VoiceSessionNotFound("voice_turn_not_found")
        return _turn(row)

    def record_client_playout(
        self,
        *,
        user_id: str,
        device_id: str,
        connection_generation: str,
        session_id: str,
        generation: int,
        media_grant_revision: int,
        announcement_id: str,
        announcement_sequence: int,
        turn_id: str | None,
        kind: str,
        quantum_role: str,
        quantum_index: int,
        result_reserved_samples_after: int | None,
        phase: str,
        client_sequence: int,
        received_at: datetime,
    ) -> VoiceTurnRecord | None:
        """Persist one validated, content-free local-render observation.

        The caller's wall-clock observation is deliberately absent.  The
        transaction records only the server receipt time after rechecking the
        owner device/connection, live session, generation/grant, deterministic
        announcement identity, and strictly increasing client sequence.
        """

        user_id = _user_id(user_id)
        device_id = _uuid4(device_id, "invalid_device_id")
        connection_generation = _uuid4(
            connection_generation,
            "invalid_connection_generation",
        )
        session_id = _uuid4(session_id, "invalid_session_id")
        _positive(generation, "invalid_generation")
        _positive(media_grant_revision, "invalid_media_grant_revision")
        announcement_id = _uuid4(
            announcement_id,
            "invalid_announcement_id",
        )
        _positive(announcement_sequence, "invalid_announcement_sequence")
        if kind not in _PLAYOUT_KINDS:
            raise ValueError("invalid_announcement_kind")
        if (
            isinstance(quantum_index, bool)
            or not isinstance(quantum_index, int)
            or not 0 <= quantum_index <= 31
        ):
            raise ValueError("invalid_quantum_index")
        if phase not in {"started", "finished", "interrupted"}:
            raise ValueError("invalid_client_playout_phase")
        if (
            isinstance(client_sequence, bool)
            or not isinstance(client_sequence, int)
            or client_sequence < 0
        ):
            raise ValueError("invalid_client_sequence")
        received_at = _aware(received_at, "invalid_received_at")
        if turn_id is None:
            if kind != "greeting":
                raise ValueError("invalid_announcement_turn")
        else:
            turn_id = _uuid4(turn_id, "invalid_turn_id")
            if kind == "greeting":
                raise ValueError("invalid_announcement_turn")
        if quantum_role == "single":
            if (
                kind == "result"
                or quantum_index != 0
                or result_reserved_samples_after is not None
            ):
                raise ValueError("invalid_single_quantum")
        elif quantum_role == "result_opening":
            if kind != "result" or quantum_index != 0:
                raise ValueError("invalid_result_opening")
            _positive(
                result_reserved_samples_after,
                "invalid_result_reservation",
            )
            if int(result_reserved_samples_after) > 36_000:
                raise ValueError("invalid_result_reservation")
        elif quantum_role == "result_continuation":
            if kind != "result" or quantum_index < 1:
                raise ValueError("invalid_result_continuation")
            _positive(
                result_reserved_samples_after,
                "invalid_result_reservation",
            )
            if int(result_reserved_samples_after) > 720_000:
                raise ValueError("invalid_result_reservation")
        else:
            raise ValueError("invalid_quantum_role")

        with self._transaction() as transaction:
            session = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(session)
            self._assert_fences(
                session,
                generation,
                media_grant_revision,
            )
            if (
                str(session["device_id"]) != device_id
                or str(session["owner_connection_generation"])
                != connection_generation
            ):
                raise VoiceSessionRepositoryError("owner_connection_mismatch")
            if turn_id is None:
                expected_greeting = deterministic_uuid4(
                    "voice-greeting-v1",
                    session_id,
                    str(generation),
                )
                if (
                    announcement_id != expected_greeting
                    or announcement_sequence != 1
                    or quantum_role != "single"
                ):
                    raise StaleSessionFence("playout_fence_mismatch")
                return None

            row = self._voice.get_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                for_update=True,
            )
            if row is None or str(row["session_id"]) != session_id:
                raise VoiceSessionNotFound("voice_turn_not_found")
            if (
                int(row["session_generation"]) != generation
                or int(row["media_grant_revision"])
                != media_grant_revision
                or announcement_sequence > int(row["announcement_sequence"])
            ):
                raise StaleSessionFence("playout_fence_mismatch")
            expected_announcement = deterministic_uuid4(
                "voice-announcement-v1",
                session_id,
                turn_id,
                str(generation),
                str(announcement_sequence),
                kind,
                quantum_role,
                str(quantum_index),
            )
            if announcement_id != expected_announcement:
                raise StaleSessionFence("playout_fence_mismatch")
            if (
                announcement_sequence == int(row["announcement_sequence"])
                and row.get("last_announcement_kind") != kind
            ):
                raise StaleSessionFence("playout_fence_mismatch")
            if kind == "result" and (
                int(result_reserved_samples_after or 0)
                > int(row["result_reserved_samples"])
                or quantum_index >= int(row["result_quantum_count"])
            ):
                raise StaleSessionFence("result_reservation_mismatch")
            maximum = self._voice.max_client_playout_sequence(
                transaction,
                owner_id=user_id,
                session_id=session_id,
            )
            if maximum >= client_sequence:
                raise StaleSessionFence("client_sequence_out_of_order")
            updates: dict[str, Any] = {
                "last_client_playout_sequence": client_sequence,
                "updated_at": received_at,
            }
            if phase == "started":
                updates["last_client_playout_started_at"] = received_at
            elif phase == "finished":
                updates["last_client_playout_finished_at"] = received_at
            updated = self._voice.patch_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                updates=updates,
            )
            if updated is None:  # pragma: no cover - locked row is retained.
                raise RuntimeError("voice_playout_update_failed")
        return _turn(updated)

    def terminalize_turn(
        self,
        *,
        user_id: str,
        turn_id: str,
        terminal_kind: str,
        result_commit_id: str | None,
        recap_source: str,
        sensitivity: str,
        now: datetime,
    ) -> TurnMutation:
        """Apply one content-free terminal result fence without cancelling work."""

        user_id = _user_id(user_id)
        turn_id = _uuid4(turn_id, "invalid_turn_id")
        if terminal_kind not in {"succeeded", "failed", "refused", "cancelled"}:
            raise ValueError("invalid_terminal_kind")
        result_commit_id = (
            None
            if result_commit_id is None
            else _uuid4(result_commit_id, "invalid_result_commit_id")
        )
        if recap_source not in {
            "none",
            "authoritative_summary",
            "committed_visible_fallback",
            "sensitive_notice",
            "terminal_status",
        }:
            raise ValueError("invalid_recap_source")
        if sensitivity not in {"unknown", "sensitive", "non_sensitive"}:
            raise ValueError("invalid_sensitivity")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._voice.get_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                for_update=True,
            )
            if row is None:
                raise VoiceSessionNotFound("voice_turn_not_found")
            if str(row["state"]) in _TERMINAL_TURN_STATES:
                if (
                    str(row["state"]) == terminal_kind
                    and _uuid_text(row.get("result_commit_id"))
                    == result_commit_id
                    and str(row.get("recap_source") or "none") == recap_source
                    and str(row.get("sensitivity") or "unknown") == sensitivity
                ):
                    return TurnMutation(_turn(row), replayed=True)
                raise IdempotencyConflict("voice_turn_terminal_conflict")
            if str(row["state"]) not in {
                "accepted",
                "processing",
                "waiting_on_user",
            }:
                raise VoiceSessionRepositoryError("voice_turn_not_accepted")
            updated = self._voice.patch_turn_record(
                transaction,
                owner_id=user_id,
                turn_id=turn_id,
                updates={
                    "state": terminal_kind,
                    "terminal_kind": terminal_kind,
                    "result_commit_id": result_commit_id,
                    "recap_source": recap_source,
                    "sensitivity": sensitivity,
                    "is_foreground": False,
                    "next_announcement_due_at": None,
                    "terminal_at": now,
                    "updated_at": now,
                },
                expected_states=("accepted", "processing", "waiting_on_user"),
            )
            if updated is None:
                raise VoiceSessionRepositoryError("voice_turn_not_accepted")
        return TurnMutation(_turn(updated))

    def set_true_idle(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        listening: bool,
        user_input_gate: bool,
        now: datetime,
    ) -> VoiceSessionRecord:
        """Start/clear the idle clock from server-owned listening/gate state."""

        if not isinstance(listening, bool) or not isinstance(user_input_gate, bool):
            raise ValueError("invalid_idle_state")
        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            if int(row["generation"]) != expected_generation:
                raise StaleSessionFence("stale_generation")
            eligible = listening and not user_input_gate and row["state"] == "active"
            if eligible:
                eligible = not self._voice.has_turn_in_states(
                    transaction,
                    owner_id=user_id,
                    session_id=str(row["session_id"]),
                    states=tuple(_ACTIVE_TURN_STATES),
                )
            idle_started_at = (
                (
                    row.get("idle_started_at")
                    if eligible and row.get("idle_started_at")
                    else now
                )
                if eligible
                else None
            )
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                updates={"idle_started_at": idle_started_at, "updated_at": now},
            )
            if updated is None:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_idle_update_failed")
        return _session(updated)

    def record_interaction(
        self,
        *,
        user_id: str,
        session_id: str,
        expected_generation: int,
        now: datetime,
    ) -> VoiceSessionRecord:
        """Stamp authenticated receipt time; no client wall clock is accepted."""

        now = _aware(now, "invalid_current_time")
        with self._transaction() as transaction:
            row = self._session_for_update(transaction, user_id, session_id)
            self._assert_live(row)
            if int(row["generation"]) != expected_generation:
                raise StaleSessionFence("stale_generation")
            updated = self._voice.patch_session_record(
                transaction,
                owner_id=user_id,
                session_id=str(row["session_id"]),
                updates={
                    "last_interaction_at": now,
                    "idle_started_at": (
                        None if row.get("idle_started_at") is None else now
                    ),
                    "updated_at": now,
                },
            )
            if updated is None:  # pragma: no cover - locked row invariant.
                raise RuntimeError("voice_interaction_update_failed")
        return _session(updated)

    def expire_true_idle(self, *, now: datetime) -> tuple[VoiceSessionRecord, ...]:
        """End only rows that stayed continuously true-idle for five minutes."""

        now = _aware(now, "invalid_current_time")
        cutoff = now - IDLE_TIMEOUT
        expired: list[VoiceSessionRecord] = []
        with self._transaction() as transaction:
            rows = self._voice.list_true_idle_session_records_for_administration(
                transaction,
                cutoff=cutoff,
            )
            for row in rows:
                has_active_turn = self._voice.has_turn_in_states(
                    transaction,
                    owner_id=str(row["user_id"]),
                    session_id=str(row["session_id"]),
                    states=tuple(_ACTIVE_TURN_STATES),
                )
                if has_active_turn:
                    self._voice.patch_session_record(
                        transaction,
                        owner_id=str(row["user_id"]),
                        session_id=str(row["session_id"]),
                        updates={"idle_started_at": None, "updated_at": now},
                    )
                    continue
                expired.append(
                    self._end_session_row(
                        transaction,
                        row,
                        reason="idle",
                        now=now,
                    )
                )
        return tuple(expired)

    def _insert_session(
        self,
        transaction: Any,
        request: CreateSession,
        *,
        generation: int,
        takeover_of: str | None,
        now: datetime,
    ) -> Mapping[str, Any]:
        session_id = self._new_uuid4("session_id")
        row = self._voice.insert_session_record(
            transaction,
            values={
                "session_id": session_id,
                "user_id": request.user_id,
                "activation_id": request.activation_id,
                "device_id": request.device_id,
                "device_kind": request.device_kind,
                "speech_backend": request.speech_backend,
                "transport": request.transport,
                "room_name": request.room_name,
                "participant_identity": request.participant_identity,
                "visible_chat_id": request.visible_chat_id,
                "generation": generation,
                "media_grant_revision": 1,
                "owner_connection_generation": request.owner_connection_generation,
                "control_binding_id": request.control_binding_id,
                "control_binding_expires_at": request.control_binding_expires_at,
                "lease_expires_at": request.lease_expires_at,
                "last_interaction_at": now,
                "started_at": now,
                "updated_at": now,
                "takeover_of_session_id": takeover_of,
                "media_grant_nonce_hash": request.media_grant_nonce_hash,
                "media_grant_expires_at": request.media_grant_expires_at,
                "media_grant_issued_at": request.media_grant_issued_at,
                "worker_rtc_grant_revision": (
                    1 if request.speech_backend == "llm_factory" else None
                ),
            },
        )
        return row

    @staticmethod
    def _validate_create_lifetimes(request: CreateSession, now: datetime) -> None:
        if request.control_binding_expires_at <= now:
            raise ValueError("invalid_control_binding_expiry")
        if request.lease_expires_at <= now:
            raise ValueError("invalid_lease_expiry")
        if (
            request.speech_backend == "llm_factory"
            and request.media_grant_expires_at <= now
        ):
            raise ValueError("invalid_media_grant_expiry")

    def _activation_row(
        self,
        transaction: Any,
        user_id: str,
        activation_id: str,
    ) -> Any:
        return self._voice.get_activation_record(
            transaction,
            owner_id=user_id,
            activation_id=activation_id,
            for_update=True,
        )

    @staticmethod
    def _assert_activation_replay(
        row: Mapping[str, Any],
        request: CreateSession,
        *,
        takeover_of: str | None,
    ) -> None:
        immutable = (
            (str(row["device_id"]), request.device_id),
            (row["device_kind"], request.device_kind),
            (row.get("speech_backend", "llm_factory"), request.speech_backend),
            (row["transport"], request.transport),
            (row["room_name"], request.room_name),
            (row["participant_identity"], request.participant_identity),
            (_uuid_text(row.get("takeover_of_session_id")), takeover_of),
        )
        if any(actual != expected for actual, expected in immutable):
            raise IdempotencyConflict("activation_id_payload_mismatch")
        if int(row["chat_context_revision"]) == 1 and (
            row["visible_chat_id"] != request.visible_chat_id
        ):
            raise IdempotencyConflict("activation_id_payload_mismatch")

    @staticmethod
    def _assert_refresh_replay(
        row: Mapping[str, Any],
        request: MediaGrantRefresh,
        now: datetime,
    ) -> None:
        if int(row["generation"]) != request.expected_generation:
            raise StaleSessionFence("stale_generation")
        if int(row["media_grant_revision"]) != (
            request.expected_media_grant_revision + 1
        ):
            raise IdempotencyConflict("refresh_id_payload_mismatch")
        matches = (
            row["participant_identity"] == request.participant_identity
            and bytes(row["media_grant_nonce_hash"]) == request.nonce_hash
            and row["media_grant_issued_at"] == request.issued_at
            and row["media_grant_expires_at"] == request.expires_at
        )
        if not matches:
            raise IdempotencyConflict("refresh_id_payload_mismatch")
        if now > request.issued_at + GRANT_REPLAY_WINDOW:
            raise IdempotencyConflict("refresh_replay_expired")

    def _session_for_update(
        self,
        transaction: Any,
        user_id: str,
        session_id: str,
    ) -> Any:
        user_id = _user_id(user_id)
        session_id = _uuid4(session_id, "invalid_session_id")
        row = self._voice.get_session_record(
            transaction,
            owner_id=user_id,
            session_id=session_id,
            for_update=True,
        )
        if row is None:
            raise VoiceSessionNotFound("voice_session_not_found")
        return row

    def _abandon_unaccepted_session_turns(
        self,
        transaction: Any,
        *,
        owner_id: str,
        session_id: str,
        generation: int,
        now: datetime,
    ) -> None:
        """Terminalize pre-acceptance rows while preserving accepted work."""

        self._voice.abandon_unaccepted_session_turns(
            transaction,
            owner_id=owner_id,
            session_id=session_id,
            generation=generation,
            now=now,
        )

    def _end_session_row(
        self,
        transaction: Any,
        row: Mapping[str, Any],
        *,
        reason: str,
        now: datetime,
        chat_unavailable_at: datetime | None = None,
        abandon_unaccepted: bool = True,
    ) -> VoiceSessionRecord:
        """Apply the one terminal media fence used by every lifecycle path."""

        if reason not in _END_REASONS:
            raise ValueError("invalid_end_reason")
        if row.get("ended_at") is not None:
            return _session(row)
        updated = self._voice.patch_session_record(
            transaction,
            owner_id=str(row["user_id"]),
            session_id=str(row["session_id"]),
            updates={
                "state": "ended",
                "microphone_enabled": False,
                "foreground_active": False,
                "foreground_reason": "connection_lost",
                "ended_at": now,
                "end_reason": reason,
                "chat_unavailable_at": (
                    chat_unavailable_at
                    if chat_unavailable_at is not None
                    else row.get("chat_unavailable_at")
                ),
                "idle_started_at": None,
                "updated_at": now,
                "control_owner_id": None,
                "control_lease_expires_at": None,
            },
            require_live=True,
        )
        if updated is None:  # pragma: no cover - row lock/WHERE invariant.
            raise RuntimeError("voice_session_end_failed")
        if abandon_unaccepted:
            self._abandon_unaccepted_session_turns(
                transaction,
                owner_id=str(row["user_id"]),
                session_id=str(row["session_id"]),
                generation=int(row["generation"]),
                now=now,
            )
        return _session(updated)

    @staticmethod
    def _assert_control_replay(
        row: Mapping[str, Any],
        control: SessionControl,
        now: datetime,
    ) -> None:
        """Authorize an exact repeated end without mutating a terminal row."""

        if (
            str(row["device_id"]) != control.device_id
            or str(row["owner_connection_generation"])
            != control.connection_generation
            or str(row["control_binding_id"]) != control.binding_id
            or row["control_binding_expires_at"] != control.binding_expires_at
        ):
            raise VoiceSessionRepositoryError("binding_scope_mismatch")
        if control.binding_expires_at <= now:
            raise VoiceSessionRepositoryError("binding_expired")

    def _apply_control_binding(
        self,
        transaction: Any,
        row: Mapping[str, Any],
        control: SessionControl,
        now: datetime,
    ) -> Mapping[str, Any]:
        """Fence cross-device control and adopt only a verified same-device reconnect."""

        if str(row["device_id"]) != control.device_id:
            raise VoiceSessionRepositoryError("binding_scope_mismatch")
        if control.binding_expires_at <= now:
            raise VoiceSessionRepositoryError("binding_expired")
        if (
            str(row["owner_connection_generation"]) == control.connection_generation
            and str(row["control_binding_id"]) == control.binding_id
            and row["control_binding_expires_at"] == control.binding_expires_at
        ):
            return row
        updated = self._voice.patch_session_record(
            transaction,
            owner_id=str(row["user_id"]),
            session_id=str(row["session_id"]),
            updates={
                "owner_connection_generation": control.connection_generation,
                "control_binding_id": control.binding_id,
                "control_binding_expires_at": control.binding_expires_at,
                "updated_at": now,
            },
        )
        if updated is None:  # pragma: no cover - locked row cannot disappear.
            raise RuntimeError("voice_control_rebind_failed")
        return updated

    @staticmethod
    def _assert_live(row: Mapping[str, Any]) -> None:
        if row.get("ended_at") is not None:
            raise StaleSessionFence("session_ended")

    @staticmethod
    def _assert_fences(
        row: Mapping[str, Any],
        expected_generation: int,
        expected_media_grant_revision: int,
    ) -> None:
        _positive(expected_generation, "invalid_expected_generation")
        _positive(
            expected_media_grant_revision,
            "invalid_expected_media_grant_revision",
        )
        if int(row["generation"]) != expected_generation:
            raise StaleSessionFence("stale_generation")
        if int(row["media_grant_revision"]) != expected_media_grant_revision:
            raise StaleSessionFence("stale_media_grant_revision")

    @staticmethod
    def _assert_transcript_binding(
        turn: Mapping[str, Any],
        session: Mapping[str, Any],
        request: TranscriptSubmission,
    ) -> None:
        if (
            session.get("ended_at") is not None
            or int(session["generation"]) != request.generation
        ):
            raise TranscriptSubmissionRejected(
                "stale_session",
                "explicit_user_retry",
            )
        if (
            session.get("worker_assignment_id") is None
            or session.get("worker_identity") != request.source_participant_identity
        ):
            raise TranscriptSubmissionRejected(
                "invalid_binding",
                "explicit_user_retry",
            )
        expected = {
            "session_id": request.session_id,
            "session_generation": request.generation,
            "media_grant_revision": request.media_grant_revision,
            "turn_id": request.turn_id,
            "client_turn_id": request.client_turn_id,
            "submission_id": request.submission_id,
            "request_generation": request.request_generation,
            "chat_id": request.chat_id,
            "chat_context_revision": request.chat_context_revision,
        }
        for name, value in expected.items():
            actual = turn[name]
            if name in {
                "session_generation",
                "media_grant_revision",
                "chat_context_revision",
            }:
                actual = int(actual)
            else:
                actual = str(actual)
            if actual != value:
                raise TranscriptSubmissionRejected(
                    "invalid_binding",
                    "explicit_user_retry",
                )

    def _lock_identity(
        self,
        transaction: Any,
        namespace: str,
        *parts: str,
    ) -> None:
        self._voice.lock_identity(
            transaction,
            namespace=namespace,
            parts=parts,
        )

    def _new_uuid4(self, field_name: str) -> str:
        value = self._uuid_factory()
        if not isinstance(value, uuid.UUID) or value.version != 4:
            raise RuntimeError(f"{field_name}_factory_must_return_uuid4")
        return str(value)


def _session(row: Mapping[str, Any]) -> VoiceSessionRecord:
    return VoiceSessionRecord(
        session_id=str(row["session_id"]),
        user_id=str(row["user_id"]),
        activation_id=str(row["activation_id"]),
        device_id=str(row["device_id"]),
        device_kind=str(row["device_kind"]),
        speech_backend=str(row.get("speech_backend", "llm_factory")),
        transport=str(row["transport"]),
        room_name=(None if row.get("room_name") is None else str(row["room_name"])),
        participant_identity=(
            None
            if row.get("participant_identity") is None
            else str(row["participant_identity"])
        ),
        worker_identity=row.get("worker_identity"),
        visible_chat_id=str(row["visible_chat_id"]),
        chat_context_revision=int(row["chat_context_revision"]),
        applied_visible_chat_id=row.get("applied_visible_chat_id"),
        applied_chat_context_revision=(
            None
            if row.get("applied_chat_context_revision") is None
            else int(row["applied_chat_context_revision"])
        ),
        state=str(row["state"]),
        speech_muted=bool(row["speech_muted"]),
        microphone_enabled=bool(row["microphone_enabled"]),
        foreground_active=bool(row["foreground_active"]),
        foreground_reason=str(row["foreground_reason"]),
        generation=int(row["generation"]),
        media_grant_revision=int(row["media_grant_revision"]),
        owner_connection_generation=str(row["owner_connection_generation"]),
        control_binding_id=str(row["control_binding_id"]),
        control_binding_expires_at=_aware(
            row["control_binding_expires_at"], "invalid_control_binding_expiry"
        ),
        lease_expires_at=_aware(row["lease_expires_at"], "invalid_lease_expiry"),
        control_owner_id=row.get("control_owner_id"),
        control_lease_expires_at=_optional_aware(row.get("control_lease_expires_at")),
        last_interaction_at=_aware(
            row["last_interaction_at"], "invalid_last_interaction_at"
        ),
        idle_started_at=_optional_aware(row.get("idle_started_at")),
        started_at=_aware(row["started_at"], "invalid_started_at"),
        updated_at=_aware(row["updated_at"], "invalid_updated_at"),
        ended_at=_optional_aware(row.get("ended_at")),
        end_reason=row.get("end_reason"),
        chat_unavailable_at=_optional_aware(row.get("chat_unavailable_at")),
        takeover_of_session_id=_uuid_text(row.get("takeover_of_session_id")),
        media_grant_nonce_hash=(
            None
            if row.get("media_grant_nonce_hash") is None
            else bytes(row["media_grant_nonce_hash"])
        ),
        media_grant_expires_at=_optional_aware(row.get("media_grant_expires_at")),
        media_grant_consumed_at=_optional_aware(row.get("media_grant_consumed_at")),
        last_media_refresh_id=_uuid_text(row.get("last_media_refresh_id")),
        media_grant_issued_at=_optional_aware(row.get("media_grant_issued_at")),
        worker_assignment_id=_uuid_text(row.get("worker_assignment_id")),
        worker_rtc_grant_revision=(
            None
            if row.get("worker_rtc_grant_revision") is None
            else int(row["worker_rtc_grant_revision"])
        ),
        worker_rtc_grant_issued_at=_optional_aware(
            row.get("worker_rtc_grant_issued_at")
        ),
        worker_rtc_grant_expires_at=_optional_aware(
            row.get("worker_rtc_grant_expires_at")
        ),
    )


def _turn(row: Mapping[str, Any]) -> VoiceTurnRecord:
    return VoiceTurnRecord(
        turn_id=str(row["turn_id"]),
        client_turn_id=str(row["client_turn_id"]),
        session_id=str(row["session_id"]),
        session_generation=int(row["session_generation"]),
        media_grant_revision=int(row["media_grant_revision"]),
        user_id=str(row["user_id"]),
        chat_id=str(row["chat_id"]),
        chat_context_revision=int(row["chat_context_revision"]),
        execution_base_render_revision=int(row["execution_base_render_revision"]),
        submission_id=str(row["submission_id"]),
        request_generation=str(row["request_generation"]),
        result_request_generation=_uuid_text(
            row.get("result_request_generation")
        ),
        message_id=(None if row.get("message_id") is None else int(row["message_id"])),
        acceptance_commit_id=_uuid_text(row.get("acceptance_commit_id")),
        result_commit_id=_uuid_text(row.get("result_commit_id")),
        operation_id=_uuid_text(row.get("operation_id")),
        state=str(row["state"]),
        is_foreground=bool(row["is_foreground"]),
        detected_language=row.get("detected_language"),
        spoken_output_policy=str(row["spoken_output_policy"]),
        output_reason=str(row["output_reason"]),
        terminal_kind=row.get("terminal_kind"),
        rejection_reason=row.get("rejection_reason"),
        rejection_retry_policy=row.get("rejection_retry_policy"),
        origin_chat_unavailable_at=_optional_aware(
            row.get("origin_chat_unavailable_at")
        ),
        origin_chat_unavailable_reason=row.get(
            "origin_chat_unavailable_reason"
        ),
        recap_source=str(row.get("recap_source") or "none"),
        sensitivity=str(row.get("sensitivity") or "unknown"),
        announcement_sequence=int(row.get("announcement_sequence") or 0),
        result_reserved_samples=int(row.get("result_reserved_samples") or 0),
        result_quantum_count=int(row.get("result_quantum_count") or 0),
        last_phrase_key=row.get("last_phrase_key"),
        next_announcement_due_at=_optional_aware(
            row.get("next_announcement_due_at")
        ),
        accepted_at=_optional_aware(row.get("accepted_at")),
        processing_started_at=_optional_aware(
            row.get("processing_started_at")
        ),
        terminal_at=_optional_aware(row.get("terminal_at")),
        created_at=_aware(row["created_at"], "invalid_created_at"),
        updated_at=_aware(row["updated_at"], "invalid_updated_at"),
    )


def _turn_binding_matches(row: Mapping[str, Any], request: RecognitionBinding) -> bool:
    return (
        str(row["session_id"]) == request.session_id
        and int(row["session_generation"]) == request.session_generation
        and int(row["media_grant_revision"]) == request.media_grant_revision
        and str(row["chat_id"]) == request.chat_id
        and int(row["chat_context_revision"]) == request.chat_context_revision
        and int(row["execution_base_render_revision"])
        == request.execution_base_render_revision
    )


def _context_synced(row: Mapping[str, Any]) -> bool:
    return (
        row.get("applied_visible_chat_id") == row["visible_chat_id"]
        and row.get("applied_chat_context_revision") == row["chat_context_revision"]
    )


def _language_policy(detected_language: str) -> tuple[str, str]:
    if detected_language == "en" or detected_language.startswith("en-"):
        return "full_recap", "ready"
    return "english_lifecycle_only", "output_language_unsupported"


def canonicalize_local_transcript(text: str, text_digest_sha256: str) -> str:
    """Canonicalize one untrusted client-local final and verify its digest."""

    if not isinstance(text, str) or not isinstance(text_digest_sha256, str):
        raise TranscriptSubmissionRejected(
            "malformed_final",
            "explicit_user_retry",
        )
    canonical = unicodedata.normalize(
        "NFC",
        text.replace("\r\n", "\n").replace("\r", "\n"),
    ).strip()
    invalid_control = any(
        character not in {"\n", "\t"}
        and unicodedata.category(character).startswith("C")
        for character in canonical
    )
    encoded = canonical.encode("utf-8")
    if (
        not canonical
        or len(canonical) > 8_000
        or len(encoded) > 32_000
        or invalid_control
        or re.fullmatch(r"[0-9a-f]{64}", text_digest_sha256) is None
    ):
        raise TranscriptSubmissionRejected(
            "malformed_final",
            "explicit_user_retry",
        )
    expected = hashlib.sha256(encoded).hexdigest()
    if not hmac.compare_digest(expected, text_digest_sha256):
        raise TranscriptSubmissionRejected(
            "malformed_final",
            "explicit_user_retry",
        )
    return canonical


def _user_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_user_id")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 512:
        raise ValueError("invalid_user_id")
    return normalized


def _uuid4(value: Any, code: str) -> str:
    if isinstance(value, uuid.UUID):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError):
            raise ValueError(code) from None
    else:
        raise ValueError(code)
    canonical = str(parsed)
    if parsed.version != 4 or not isinstance(value, (str, uuid.UUID)):
        raise ValueError(code)
    if isinstance(value, str) and value != canonical:
        raise ValueError(code)
    return canonical


def _uuid_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _opaque(value: Any, code: str, *, max_length: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or _OPAQUE.fullmatch(value) is None
    ):
        raise ValueError(code)
    return value


def _positive(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(code)
    return value


def _aware(value: Any, code: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(code)
    return value.astimezone(UTC)


def _optional_aware(value: Any) -> datetime | None:
    return None if value is None else _aware(value, "invalid_persisted_timestamp")


def _nonce_hash(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)) or len(value) != 32:
        raise ValueError("invalid_nonce_hash")
    return bytes(value)


def _duration(
    value: Any,
    code: str,
    minimum_seconds: int,
    maximum_seconds: int,
) -> timedelta:
    if not isinstance(value, timedelta):
        raise ValueError(code)
    seconds = value.total_seconds()
    if not minimum_seconds <= seconds <= maximum_seconds:
        raise ValueError(code)
    return value
