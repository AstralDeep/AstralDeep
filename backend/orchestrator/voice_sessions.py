"""PostgreSQL ownership and idempotency authority for conversational voice.

This module stores only content-free session/turn correlation.  It never
stores audio, transcript text, recap text, media bearers, or credentials.
Every mutating method locks the owner/session row and applies explicit
generation/revision compare-and-swap fences before changing state.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
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
_TRANSPORTS = frozenset({"livekit", "watch_pcm_websocket"})
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
    room_name: str
    participant_identity: str
    visible_chat_id: str
    owner_connection_generation: str
    control_binding_id: str
    control_binding_expires_at: datetime
    lease_expires_at: datetime
    media_grant_nonce_hash: bytes = field(repr=False)
    media_grant_issued_at: datetime = field(repr=False)
    media_grant_expires_at: datetime = field(repr=False)

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
        if self.transport not in _TRANSPORTS:
            raise ValueError("invalid_transport")
        object.__setattr__(
            self, "room_name", _opaque(self.room_name, "invalid_room_name")
        )
        object.__setattr__(
            self,
            "participant_identity",
            _opaque(self.participant_identity, "invalid_participant_identity"),
        )
        for name in (
            "control_binding_expires_at",
            "lease_expires_at",
            "media_grant_issued_at",
            "media_grant_expires_at",
        ):
            object.__setattr__(
                self, name, _aware(getattr(self, name), f"invalid_{name}")
            )
        nonce_hash = _nonce_hash(self.media_grant_nonce_hash)
        object.__setattr__(self, "media_grant_nonce_hash", nonce_hash)
        if self.media_grant_expires_at <= self.media_grant_issued_at:
            raise ValueError("invalid_media_grant_expiry")


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
    room_name: str
    participant_identity: str
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
    media_grant_nonce_hash: bytes = field(repr=False)
    media_grant_expires_at: datetime = field(repr=False)
    media_grant_consumed_at: datetime | None = field(repr=False)
    last_media_refresh_id: str | None
    media_grant_issued_at: datetime = field(repr=False)
    worker_assignment_id: str | None
    worker_rtc_grant_revision: int
    worker_rtc_grant_issued_at: datetime | None = field(repr=False)
    worker_rtc_grant_expires_at: datetime | None = field(repr=False)

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
    """Transactional PostgreSQL repository for Feature 065 session/turn state."""

    def __init__(
        self,
        database: Any,
        *,
        uuid_factory: Any = uuid.uuid4,
        control_lease_ttl_seconds: int = 15,
    ) -> None:
        if database is None or not callable(getattr(database, "_get_connection", None)):
            raise TypeError("database must provide _get_connection()")
        if not callable(uuid_factory):
            raise TypeError("uuid_factory must be callable")
        self._database = database
        self._uuid_factory = uuid_factory
        self._control_leases = ControlLeaseAdapter(
            ttl_seconds=control_lease_ttl_seconds
        )
        self._announcements = AnnouncementStateAdapter(
            PhraseBook(APPROVED_PHRASE_KEYS)
        )

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._database._get_connection()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

    @contextmanager
    def _transaction_or_existing(self, cursor: Any | None) -> Iterator[Any]:
        """Join a caller-owned PostgreSQL transaction or open one locally."""

        if cursor is not None:
            if not callable(getattr(cursor, "execute", None)):
                raise TypeError("transaction must be a PostgreSQL cursor")
            yield cursor
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
        with self._transaction() as cursor:
            self._lock_identity(cursor, "owner", request.user_id)
            existing = self._activation_row(
                cursor, request.user_id, request.activation_id
            )
            if existing is not None:
                self._assert_activation_replay(existing, request, takeover_of=None)
                return SessionMutation(_session(existing), replayed=True)
            cursor.execute(
                "SELECT * FROM voice_session WHERE user_id = %s "
                "AND ended_at IS NULL FOR UPDATE",
                (request.user_id,),
            )
            current = cursor.fetchone()
            if current is not None:
                raise TakeoverRequired(_session(current))
            row = self._insert_session(
                cursor,
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
        with self._transaction() as cursor:
            self._lock_identity(cursor, "owner", create.user_id)
            existing = self._activation_row(
                cursor, create.user_id, create.activation_id
            )
            if existing is not None:
                self._assert_activation_replay(
                    existing,
                    create,
                    takeover_of=request.previous_session_id,
                )
                return SessionMutation(_session(existing), replayed=True)
            previous = self._session_for_update(
                cursor,
                create.user_id,
                request.previous_session_id,
            )
            if previous.get("ended_at") is not None:
                cursor.execute(
                    "SELECT generation FROM voice_session "
                    "WHERE user_id = %s AND ended_at IS NULL FOR UPDATE",
                    (create.user_id,),
                )
                current = cursor.fetchone()
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
                cursor,
                previous,
                reason="takeover",
                now=now,
            )
            row = self._insert_session(
                cursor,
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_session WHERE user_id = %s AND session_id = %s",
                (user_id, session_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise VoiceSessionNotFound("voice_session_not_found")
        return _session(row)

    def get_live_session(self, *, user_id: str) -> VoiceSessionRecord | None:
        """Return the user's sole unended session, if present."""

        user_id = _user_id(user_id)
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_session WHERE user_id = %s AND ended_at IS NULL",
                (user_id,),
            )
            row = cursor.fetchone()
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
        with self._transaction() as cursor:
            self._lock_identity(cursor, "owner_chat", user_id, chat_id)
            cursor.execute(
                "SELECT id FROM chats WHERE id = %s AND user_id = %s "
                "FOR UPDATE",
                (chat_id, user_id),
            )
            chat = cursor.fetchone()
            if chat is None:
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
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND chat_id = %s "
                "ORDER BY created_at, turn_id FOR UPDATE",
                (user_id, chat_id),
            )
            turns = list(cursor.fetchall())
            cursor.execute(
                "SELECT * FROM voice_session WHERE user_id = %s "
                "AND visible_chat_id = %s ORDER BY started_at, session_id "
                "FOR UPDATE",
                (user_id, chat_id),
            )
            affected_sessions = tuple(cursor.fetchall())
            ended_sessions = tuple(
                ended
                for row in affected_sessions
                if row.get("ended_at") is None
                for ended in (
                    self._end_session_row(
                        cursor,
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
                cursor.execute(
                    """
                    UPDATE voice_turn
                    SET state = 'abandoned', terminal_kind = 'abandoned',
                        rejection_reason = 'chat_unavailable',
                        rejection_retry_policy = 'explicit_user_retry',
                        origin_chat_unavailable_at = NULL,
                        origin_chat_unavailable_reason = NULL,
                        is_foreground = FALSE,
                        next_announcement_due_at = NULL,
                        announcement_claim_id = NULL,
                        announcement_claim_expires_at = NULL,
                        terminal_at = %s, updated_at = %s
                    WHERE user_id = %s AND turn_id = ANY(%s::uuid[])
                      AND state IN ('recognizing', 'submitting')
                    """,
                    (now, now, user_id, list(unaccepted_turn_ids)),
                )
            if accepted_turn_ids:
                cursor.execute(
                    """
                    UPDATE voice_turn
                    SET state = 'abandoned', terminal_kind = 'abandoned',
                        rejection_reason = NULL,
                        rejection_retry_policy = NULL,
                        origin_chat_unavailable_at = %s,
                        origin_chat_unavailable_reason = %s,
                        is_foreground = FALSE,
                        next_announcement_due_at = NULL,
                        announcement_claim_id = NULL,
                        announcement_claim_expires_at = NULL,
                        terminal_at = %s, updated_at = %s
                    WHERE user_id = %s AND turn_id = ANY(%s::uuid[])
                      AND origin_chat_unavailable_at IS NULL
                      AND (
                        accepted_at IS NOT NULL
                        OR state IN (
                            'accepted', 'processing', 'waiting_on_user'
                        )
                      )
                    """,
                    (
                        now,
                        reason,
                        now,
                        now,
                        user_id,
                        list(accepted_turn_ids),
                    ),
                )

            cursor.execute(
                """
                SELECT commit_id
                FROM conversation_commit
                WHERE chat_id = %s AND owner_user_id = %s
                  AND publication_role = 'assistant_result'
                  AND state = 'staged'
                ORDER BY started_at, commit_id
                FOR UPDATE
                """,
                (chat_id, user_id),
            )
            aborted_result_commit_ids = tuple(
                str(row["commit_id"]) for row in cursor.fetchall()
            )
            for commit_id in aborted_result_commit_ids:
                cursor.execute(
                    "DELETE FROM saved_components "
                    "WHERE conversation_commit_id = %s",
                    (commit_id,),
                )
                cursor.execute(
                    "DELETE FROM workspace_layout "
                    "WHERE conversation_commit_id = %s",
                    (commit_id,),
                )
                cursor.execute(
                    "DELETE FROM messages WHERE conversation_commit_id = %s",
                    (commit_id,),
                )
                cursor.execute(
                    """
                    UPDATE conversation_commit
                    SET state = 'aborted', aborted_at = %s,
                        execution_base_commit_id = NULL
                    WHERE commit_id = %s AND state = 'staged'
                    """,
                    (now, commit_id),
                )

            deleted = False
            if delete_chat:
                cursor.execute(
                    "DELETE FROM chats WHERE id = %s AND user_id = %s",
                    (chat_id, user_id),
                )
                deleted = cursor.rowcount == 1

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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            row = self._apply_control_binding(cursor, row, control, now)
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, request.user_id, request.session_id)
            self._assert_live(row)
            self._assert_fences(
                row,
                request.expected_generation,
                request.expected_media_grant_revision,
            )
            row = self._apply_control_binding(cursor, row, request.control, now)

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
            cursor.execute(
                """
                UPDATE voice_session
                SET visible_chat_id = %s, chat_context_revision = %s,
                    speech_muted = %s, microphone_enabled = %s,
                    foreground_active = %s, foreground_reason = %s,
                    state = %s, last_interaction_at = %s,
                    idle_started_at = %s, updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (
                    visible_chat_id,
                    chat_context_revision,
                    speech_muted,
                    microphone_enabled,
                    foreground_active,
                    foreground_reason,
                    state,
                    last_interaction_at,
                    idle_started_at,
                    now,
                    request.session_id,
                ),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
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
            row = self._apply_control_binding(cursor, row, control, now)
            ended = self._end_session_row(
                cursor,
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
        with self._transaction() as cursor:
            self._lock_identity(cursor, "owner", user_id)
            cursor.execute(
                "SELECT * FROM voice_session WHERE user_id = %s "
                "AND ended_at IS NULL FOR UPDATE",
                (user_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            ended = self._end_session_row(
                cursor,
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
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT * FROM voice_session
                WHERE ended_at IS NULL AND control_owner_id = %s
                ORDER BY started_at, session_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (owner_id, batch_size),
            )
            rows = tuple(cursor.fetchall())
            ended = tuple(
                self._end_session_row(
                    cursor,
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
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT * FROM voice_session
                WHERE ended_at IS NULL AND lease_expires_at <= %s
                ORDER BY lease_expires_at, session_id
                FOR UPDATE SKIP LOCKED
                """,
                (now,),
            )
            for row in cursor.fetchall():
                expired.append(
                    self._end_session_row(
                        cursor,
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
        with self._transaction() as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT turn.turn_id
                    FROM voice_turn AS turn
                    JOIN voice_session AS session
                      ON session.session_id = turn.session_id
                    WHERE session.ended_at IS NOT NULL
                      AND turn.state IN ('recognizing', 'submitting')
                    ORDER BY turn.updated_at, turn.turn_id
                    FOR UPDATE OF turn SKIP LOCKED
                    LIMIT %s
                )
                UPDATE voice_turn AS turn
                SET state = 'abandoned', terminal_kind = 'abandoned',
                    rejection_reason = 'stale_session',
                    rejection_retry_policy = 'explicit_user_retry',
                    terminal_at = %s, updated_at = %s
                FROM candidates
                WHERE turn.turn_id = candidates.turn_id
                RETURNING turn.*
                """,
                (batch_size, now, now),
            )
            rows = cursor.fetchall()
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
        with self._transaction() as cursor:
            cursor.execute(
                """
                WITH operation_candidates AS (
                    SELECT
                        turn.turn_id,
                        operation.state AS operation_state,
                        (
                            acceptance.state = 'committed'
                            AND acceptance.publication_role = 'user_acceptance'
                            AND acceptance.owner_user_id = turn.user_id
                            AND acceptance.chat_id = turn.chat_id
                            AND acceptance.request_generation
                                = turn.request_generation
                            AND acceptance.operation_id = turn.operation_id
                            AND acceptance.operation_execution_generation
                                = operation.execution_generation
                            AND result.state = 'committed'
                            AND result.publication_role = 'assistant_result'
                            AND result.owner_user_id = turn.user_id
                            AND result.chat_id = turn.chat_id
                            AND result.request_generation
                                = turn.result_request_generation
                            AND result.operation_id = turn.operation_id
                            AND result.operation_execution_generation
                                = operation.execution_generation
                            AND result.parent_commit_id
                                = turn.acceptance_commit_id
                        ) AS exact_result_committed,
                        turn.result_commit_id
                    FROM voice_turn AS turn
                    JOIN voice_session AS session
                      ON session.session_id = turn.session_id
                    JOIN operation_record AS operation
                      ON operation.operation_id = turn.operation_id
                    LEFT JOIN conversation_commit AS acceptance
                      ON acceptance.commit_id = turn.acceptance_commit_id
                    LEFT JOIN conversation_commit AS result
                      ON result.commit_id = turn.result_commit_id
                    WHERE session.ended_at IS NOT NULL
                      AND turn.state IN (
                          'accepted', 'processing', 'waiting_on_user'
                      )
                      AND operation.state IN (
                          'completed', 'failed', 'cancelled', 'retryable'
                      )
                      AND operation.operation_kind = 'voice_chat_message'
                      AND operation.owner_scope = 'user'
                      AND operation.owner_user_id = turn.user_id
                      AND operation.chat_id = turn.chat_id
                      AND operation.request_generation
                          = turn.request_generation
                      AND operation.connection_generation
                          = turn.accepted_connection_generation
                    ORDER BY turn.updated_at, turn.turn_id
                    FOR UPDATE OF turn SKIP LOCKED
                    LIMIT %s
                ), candidates AS (
                    SELECT
                        turn_id,
                        CASE
                            WHEN operation_state = 'completed'
                             AND exact_result_committed
                                THEN 'succeeded'
                            WHEN operation_state = 'cancelled'
                                THEN 'cancelled'
                            ELSE 'failed'
                        END AS terminal_kind,
                        CASE
                            WHEN exact_result_committed THEN result_commit_id
                            ELSE NULL
                        END AS terminal_result_commit_id
                    FROM operation_candidates
                )
                UPDATE voice_turn AS turn
                SET state = candidates.terminal_kind,
                    terminal_kind = candidates.terminal_kind,
                    result_commit_id = candidates.terminal_result_commit_id,
                    recap_source = 'terminal_status',
                    sensitivity = 'unknown',
                    is_foreground = FALSE,
                    next_announcement_due_at = NULL,
                    announcement_claim_id = NULL,
                    announcement_claim_expires_at = NULL,
                    terminal_at = %s,
                    updated_at = %s
                FROM candidates
                WHERE turn.turn_id = candidates.turn_id
                RETURNING turn.*
                """,
                (batch_size, now, now),
            )
            rows = cursor.fetchall()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            if row["state"] == "active":
                return _session(row)
            if (
                row["state"] not in {"starting", "reconnecting"}
                or not row["foreground_active"]
            ):
                raise VoiceSessionRepositoryError("invalid_session_transition")
            cursor.execute(
                "UPDATE voice_session SET state = 'active', updated_at = %s "
                "WHERE session_id = %s RETURNING *",
                (now, row["session_id"]),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            cursor.execute(
                "UPDATE voice_session SET lease_expires_at = %s, updated_at = %s "
                "WHERE session_id = %s RETURNING *",
                (now + duration, now, row["session_id"]),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, request.user_id, request.session_id)
            self._assert_live(row)
            if _uuid_text(row.get("last_media_refresh_id")) == request.refresh_id:
                self._assert_refresh_replay(row, request, now)
                return SessionMutation(_session(row), replayed=True)
            self._assert_fences(
                row,
                request.expected_generation,
                request.expected_media_grant_revision,
            )
            cursor.execute(
                """
                UPDATE voice_session
                SET media_grant_revision = media_grant_revision + 1,
                    participant_identity = %s,
                    media_grant_nonce_hash = %s,
                    media_grant_issued_at = %s,
                    media_grant_expires_at = %s,
                    media_grant_consumed_at = NULL,
                    last_media_refresh_id = %s,
                    updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (
                    request.participant_identity,
                    request.nonce_hash,
                    request.issued_at,
                    request.expires_at,
                    request.refresh_id,
                    now,
                    request.session_id,
                ),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
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
            cursor.execute(
                """
                UPDATE voice_session
                SET worker_assignment_id = %s, worker_identity = %s,
                    worker_rtc_grant_issued_at = %s,
                    worker_rtc_grant_expires_at = %s,
                    updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (
                    assignment_id,
                    worker_identity,
                    issued_at,
                    expires_at,
                    now,
                    session_id,
                ),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
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
            cursor.execute(
                """
                UPDATE voice_session
                SET control_owner_id = %s, control_lease_expires_at = %s,
                    updated_at = %s
                WHERE session_id = %s
                """,
                (claimed.owner_id, claimed.expires_at, now, row["session_id"]),
            )
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
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT * FROM voice_session
                WHERE ended_at IS NULL
                  AND control_owner_id = %s
                  AND control_lease_expires_at > %s
                ORDER BY control_lease_expires_at, session_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (owner_id, now, batch_size),
            )
            for row in cursor.fetchall():
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
                cursor.execute(
                    """
                    UPDATE voice_session
                    SET control_lease_expires_at = %s, updated_at = %s
                    WHERE session_id = %s
                      AND ended_at IS NULL
                      AND control_owner_id = %s
                      AND control_lease_expires_at > %s
                    RETURNING *
                    """,
                    (
                        claimed.expires_at,
                        now,
                        row["session_id"],
                        owner_id,
                        now,
                    ),
                )
                updated = cursor.fetchone()
                if updated is not None:
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
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
            cursor.execute(
                """
                UPDATE voice_session
                SET control_owner_id = NULL, control_lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = %s
                """,
                (row["session_id"],),
            )
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s "
                "FOR UPDATE",
                (user_id, request.turn_id),
            )
            row = cursor.fetchone()
            if row is None or str(row["session_id"]) != request.session_id:
                raise VoiceSessionNotFound("voice_turn_not_found")
            session = self._session_for_update(
                cursor,
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
            cursor.execute(
                "SELECT 1 FROM chats WHERE id = %s AND user_id = %s",
                (row["chat_id"], user_id),
            )
            chat_available = cursor.fetchone() is not None
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
            cursor.execute(
                """
                UPDATE voice_turn
                SET announcement_sequence = %s,
                    result_reserved_samples = %s,
                    result_quantum_count = %s,
                    last_announcement_kind = %s,
                    last_phrase_key = %s,
                    next_announcement_due_at = %s,
                    announcement_claim_id = %s,
                    announcement_claim_expires_at = %s,
                    last_announcement_started_at = %s,
                    updated_at = %s
                WHERE user_id = %s AND turn_id = %s
                """,
                (
                    claimed.announcement_sequence,
                    claimed.result_reserved_samples,
                    claimed.result_quantum_count,
                    claimed.last_announcement_kind,
                    claimed.last_phrase_key,
                    next_due_at,
                    claimed.announcement_claim_id,
                    claimed.announcement_claim_expires_at,
                    now,
                    now,
                    user_id,
                    request.turn_id,
                ),
            )
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s "
                "FOR UPDATE",
                (user_id, turn_id),
            )
            row = cursor.fetchone()
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
            cursor.execute(
                """
                UPDATE voice_turn
                SET announcement_claim_id = %s,
                    announcement_claim_expires_at = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND turn_id = %s
                """,
                (
                    completed.announcement_claim_id,
                    completed.announcement_claim_expires_at,
                    user_id,
                    turn_id,
                ),
            )
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
            self._assert_live(row)
            self._assert_fences(row, expected_generation, expected_media_grant_revision)
            if int(row["chat_context_revision"]) != expected_chat_context_revision:
                raise StaleSessionFence("stale_chat_context_revision")
            if row["visible_chat_id"] == visible_chat_id:
                return SessionMutation(_session(row), replayed=True)
            if not _context_synced(row):
                raise ContextSyncPending("chat_context_sync_pending")
            cursor.execute(
                """
                UPDATE voice_session
                SET visible_chat_id = %s,
                    chat_context_revision = chat_context_revision + 1,
                    last_interaction_at = %s, idle_started_at = NULL,
                    updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (visible_chat_id, now, now, row["session_id"]),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
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
            cursor.execute(
                """
                UPDATE voice_session
                SET applied_visible_chat_id = %s,
                    applied_chat_context_revision = %s,
                    updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (visible_chat_id, chat_context_revision, now, row["session_id"]),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            self._lock_identity(cursor, "turn", request.user_id, request.client_turn_id)
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND client_turn_id = %s "
                "FOR UPDATE",
                (request.user_id, request.client_turn_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if not _turn_binding_matches(existing, request):
                    raise IdempotencyConflict("client_turn_binding_mismatch")
                return TurnMutation(_turn(existing), replayed=True)
            session = self._session_for_update(
                cursor,
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
            cursor.execute(
                """
                INSERT INTO voice_turn (
                    turn_id, client_turn_id, session_id, session_generation,
                    media_grant_revision, user_id, chat_id,
                    chat_context_revision, execution_base_render_revision,
                    submission_id, request_generation,
                    result_request_generation, state, is_foreground,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'recognizing', FALSE, %s, %s
                )
                RETURNING *
                """,
                (
                    turn_id,
                    request.client_turn_id,
                    request.session_id,
                    request.session_generation,
                    request.media_grant_revision,
                    request.user_id,
                    request.chat_id,
                    request.chat_context_revision,
                    request.execution_base_render_revision,
                    submission_id,
                    request_generation,
                    result_request_generation,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_session WHERE session_id = %s FOR UPDATE",
                (start.session_id,),
            )
            session = cursor.fetchone()
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
            self._lock_identity(cursor, "turn", user_id, start.client_turn_id)
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s "
                "AND client_turn_id = %s FOR UPDATE",
                (user_id, start.client_turn_id),
            )
            existing = cursor.fetchone()
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
            cursor.execute(
                "SELECT id, render_revision FROM chats "
                "WHERE id = %s AND user_id = %s FOR SHARE",
                (start.chat_id, user_id),
            )
            chat = cursor.fetchone()
            if chat is None:
                raise VoiceSessionNotFound("voice_chat_not_found")
            turn_id = self._new_uuid4("turn_id")
            submission_id = self._new_uuid4("submission_id")
            request_generation = self._new_uuid4("request_generation")
            result_request_generation = self._new_uuid4(
                "result_request_generation"
            )
            cursor.execute(
                """
                INSERT INTO voice_turn (
                    turn_id, client_turn_id, session_id, session_generation,
                    media_grant_revision, user_id, chat_id,
                    chat_context_revision, execution_base_render_revision,
                    submission_id, request_generation,
                    result_request_generation, state, is_foreground,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'recognizing', FALSE, %s, %s
                )
                RETURNING *
                """,
                (
                    turn_id,
                    start.client_turn_id,
                    start.session_id,
                    start.generation,
                    start.media_grant_revision,
                    user_id,
                    start.chat_id,
                    start.chat_context_revision,
                    int(chat.get("render_revision") or 0),
                    submission_id,
                    request_generation,
                    result_request_generation,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_session WHERE session_id = %s FOR UPDATE",
                (binding.session_id,),
            )
            session = cursor.fetchone()
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
            cursor.execute(
                "SELECT * FROM voice_turn WHERE turn_id = %s FOR UPDATE",
                (binding.turn_id,),
            )
            row = cursor.fetchone()
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
            cursor.execute(
                """
                UPDATE voice_turn
                SET state = 'abandoned', terminal_kind = 'abandoned',
                    rejection_reason = 'malformed_final',
                    rejection_retry_policy = %s,
                    terminal_at = %s, updated_at = %s
                WHERE turn_id = %s
                RETURNING *
                """,
                (retry_policy, now, now, binding.turn_id),
            )
            row = cursor.fetchone()
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s "
                "FOR UPDATE",
                (request.user_id, request.turn_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise TranscriptSubmissionRejected(
                    "invalid_binding",
                    "explicit_user_retry",
                )
            cursor.execute(
                "SELECT * FROM voice_session WHERE user_id = %s "
                "AND session_id = %s FOR UPDATE",
                (request.user_id, request.session_id),
            )
            session = cursor.fetchone()
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
                cursor.execute(
                    """
                    UPDATE voice_turn
                    SET state = 'submitting', detected_language = %s,
                        spoken_output_policy = %s, output_reason = %s,
                        result_request_generation = %s, updated_at = %s
                    WHERE turn_id = %s
                    RETURNING *
                    """,
                    (
                        request.detected_language,
                        policy,
                        output_reason,
                        result_request_generation,
                        now,
                        request.turn_id,
                    ),
                )
                row = cursor.fetchone()
            elif row["state"] != "submitting":
                raise TranscriptSubmissionRejected("invalid_binding", "none")
            elif row.get("detected_language") != request.detected_language:
                raise TranscriptSubmissionRejected(
                    "invalid_binding",
                    "explicit_user_retry",
                )
            elif row.get("result_request_generation") is None:
                cursor.execute(
                    "UPDATE voice_turn SET result_request_generation = %s, "
                    "updated_at = %s WHERE turn_id = %s RETURNING *",
                    (
                        self._new_uuid4("result_request_generation"),
                        now,
                        request.turn_id,
                    ),
                )
                row = cursor.fetchone()
        return TranscriptAdmission(
            canonical_text=canonical,
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s "
                "FOR UPDATE",
                (user_id, turn_id),
            )
            row = cursor.fetchone()
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
            cursor.execute(
                """
                UPDATE voice_turn
                SET state = 'abandoned', terminal_kind = 'abandoned',
                    rejection_reason = %s, rejection_retry_policy = %s,
                    terminal_at = %s, updated_at = %s
                WHERE turn_id = %s
                RETURNING *
                """,
                (reason, retry_policy, now, now, turn_id),
            )
            row = cursor.fetchone()
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
        conversation repository may supply its already-fenced cursor so the
        user bubble, linked private result stage, and voice correlation either
        all commit or all roll back. No caller may supply a non-cursor object.
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
        with self._transaction_or_existing(transaction) as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s "
                "FOR UPDATE",
                (user_id, turn_id),
            )
            row = cursor.fetchone()
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
                cursor,
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
            cursor.execute(
                """
                UPDATE voice_turn
                SET is_foreground = FALSE, updated_at = %s
                WHERE session_id = %s AND is_foreground
                  AND turn_id <> %s
                  AND state NOT IN (
                    'succeeded', 'failed', 'refused', 'cancelled', 'abandoned'
                  )
                """,
                (now, row["session_id"], turn_id),
            )
            cursor.execute(
                """
                UPDATE voice_turn
                SET message_id = %s, accepted_connection_generation = %s,
                    acceptance_commit_id = %s, result_commit_id = %s,
                    operation_id = %s,
                    state = 'processing', is_foreground = TRUE,
                    accepted_at = %s, processing_started_at = %s,
                    updated_at = %s
                WHERE turn_id = %s AND state = 'submitting'
                RETURNING *
                """,
                (
                    message_id,
                    accepted_connection_generation,
                    acceptance_commit_id,
                    result_commit_id,
                    operation_id,
                    now,
                    now,
                    now,
                    turn_id,
                ),
            )
            accepted = cursor.fetchone()
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s "
                "AND submission_id = %s AND request_generation = %s",
                (user_id, submission_id, request_generation),
            )
            row = cursor.fetchone()
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
        with self._transaction() as cursor:
            session = self._session_for_update(cursor, user_id, session_id)
            self._assert_live(session)
            if int(session["generation"]) != expected_generation:
                raise StaleSessionFence("stale_generation")
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND session_id = %s "
                "AND turn_id = %s FOR UPDATE",
                (_user_id(user_id), _uuid4(session_id, "invalid_session_id"), turn_id),
            )
            target = cursor.fetchone()
            if target is None:
                raise VoiceSessionNotFound("voice_turn_not_found")
            if target["state"] in _TERMINAL_TURN_STATES:
                raise VoiceSessionRepositoryError("voice_turn_terminal")
            # Clear before setting: PostgreSQL's immediate partial-unique check
            # can otherwise observe the new foreground row before it visits the
            # previous one within a single CASE update.
            cursor.execute(
                """
                UPDATE voice_turn
                SET is_foreground = FALSE, updated_at = %s
                WHERE session_id = %s AND is_foreground
                  AND state NOT IN (
                    'succeeded', 'failed', 'refused', 'cancelled', 'abandoned'
                  )
                """,
                (now, session_id),
            )
            cursor.execute(
                "UPDATE voice_turn SET is_foreground = TRUE, updated_at = %s "
                "WHERE turn_id = %s",
                (now, turn_id),
            )
            cursor.execute("SELECT * FROM voice_turn WHERE turn_id = %s", (turn_id,))
            updated = cursor.fetchone()
        return _turn(updated)

    def get_turn(self, *, user_id: str, turn_id: str) -> VoiceTurnRecord:
        """Return an owner-scoped voice turn."""

        user_id = _user_id(user_id)
        turn_id = _uuid4(turn_id, "invalid_turn_id")
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s",
                (user_id, turn_id),
            )
            row = cursor.fetchone()
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

        with self._transaction() as cursor:
            session = self._session_for_update(cursor, user_id, session_id)
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

            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s "
                "AND session_id = %s AND turn_id = %s FOR UPDATE",
                (user_id, session_id, turn_id),
            )
            row = cursor.fetchone()
            if row is None:
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
            cursor.execute(
                "SELECT COALESCE(MAX(last_client_playout_sequence), -1) "
                "AS sequence FROM voice_turn WHERE user_id = %s "
                "AND session_id = %s",
                (user_id, session_id),
            )
            maximum = cursor.fetchone()
            if int(maximum["sequence"]) >= client_sequence:
                raise StaleSessionFence("client_sequence_out_of_order")
            if phase == "started":
                cursor.execute(
                    """
                    UPDATE voice_turn
                    SET last_client_playout_started_at = %s,
                        last_client_playout_sequence = %s, updated_at = %s
                    WHERE user_id = %s AND turn_id = %s
                    RETURNING *
                    """,
                    (
                        received_at,
                        client_sequence,
                        received_at,
                        user_id,
                        turn_id,
                    ),
                )
            elif phase == "finished":
                cursor.execute(
                    """
                    UPDATE voice_turn
                    SET last_client_playout_finished_at = %s,
                        last_client_playout_sequence = %s, updated_at = %s
                    WHERE user_id = %s AND turn_id = %s
                    RETURNING *
                    """,
                    (
                        received_at,
                        client_sequence,
                        received_at,
                        user_id,
                        turn_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE voice_turn
                    SET last_client_playout_sequence = %s, updated_at = %s
                    WHERE user_id = %s AND turn_id = %s
                    RETURNING *
                    """,
                    (client_sequence, received_at, user_id, turn_id),
                )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s "
                "FOR UPDATE",
                (user_id, turn_id),
            )
            row = cursor.fetchone()
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
            cursor.execute(
                """
                UPDATE voice_turn
                SET state = %s, terminal_kind = %s,
                    result_commit_id = %s, recap_source = %s,
                    sensitivity = %s, is_foreground = FALSE,
                    next_announcement_due_at = NULL,
                    terminal_at = %s, updated_at = %s
                WHERE user_id = %s AND turn_id = %s
                RETURNING *
                """,
                (
                    terminal_kind,
                    terminal_kind,
                    result_commit_id,
                    recap_source,
                    sensitivity,
                    now,
                    now,
                    user_id,
                    turn_id,
                ),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
            self._assert_live(row)
            if int(row["generation"]) != expected_generation:
                raise StaleSessionFence("stale_generation")
            eligible = listening and not user_input_gate and row["state"] == "active"
            if eligible:
                cursor.execute(
                    "SELECT 1 FROM voice_turn WHERE session_id = %s "
                    "AND state = ANY(%s) LIMIT 1",
                    (row["session_id"], list(_ACTIVE_TURN_STATES)),
                )
                eligible = cursor.fetchone() is None
            idle_started_at = (
                (
                    row.get("idle_started_at")
                    if eligible and row.get("idle_started_at")
                    else now
                )
                if eligible
                else None
            )
            cursor.execute(
                "UPDATE voice_session SET idle_started_at = %s, updated_at = %s "
                "WHERE session_id = %s RETURNING *",
                (idle_started_at, now, row["session_id"]),
            )
            updated = cursor.fetchone()
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
        with self._transaction() as cursor:
            row = self._session_for_update(cursor, user_id, session_id)
            self._assert_live(row)
            if int(row["generation"]) != expected_generation:
                raise StaleSessionFence("stale_generation")
            cursor.execute(
                """
                UPDATE voice_session
                SET last_interaction_at = %s,
                    idle_started_at = CASE
                        WHEN idle_started_at IS NULL THEN NULL ELSE %s END,
                    updated_at = %s
                WHERE session_id = %s
                RETURNING *
                """,
                (now, now, now, row["session_id"]),
            )
            updated = cursor.fetchone()
        return _session(updated)

    def expire_true_idle(self, *, now: datetime) -> tuple[VoiceSessionRecord, ...]:
        """End only rows that stayed continuously true-idle for five minutes."""

        now = _aware(now, "invalid_current_time")
        cutoff = now - IDLE_TIMEOUT
        expired: list[VoiceSessionRecord] = []
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT * FROM voice_session
                WHERE ended_at IS NULL AND state = 'active'
                  AND idle_started_at IS NOT NULL
                  AND idle_started_at <= %s
                ORDER BY idle_started_at, session_id
                FOR UPDATE SKIP LOCKED
                """,
                (cutoff,),
            )
            for row in cursor.fetchall():
                cursor.execute(
                    "SELECT 1 FROM voice_turn WHERE session_id = %s "
                    "AND state = ANY(%s) LIMIT 1",
                    (row["session_id"], list(_ACTIVE_TURN_STATES)),
                )
                if cursor.fetchone() is not None:
                    cursor.execute(
                        "UPDATE voice_session SET idle_started_at = NULL, updated_at = %s "
                        "WHERE session_id = %s",
                        (now, row["session_id"]),
                    )
                    continue
                expired.append(
                    self._end_session_row(
                        cursor,
                        row,
                        reason="idle",
                        now=now,
                    )
                )
        return tuple(expired)

    def _insert_session(
        self,
        cursor: Any,
        request: CreateSession,
        *,
        generation: int,
        takeover_of: str | None,
        now: datetime,
    ) -> Mapping[str, Any]:
        session_id = self._new_uuid4("session_id")
        cursor.execute(
            """
            INSERT INTO voice_session (
                session_id, user_id, activation_id, device_id, device_kind,
                transport, room_name, participant_identity, visible_chat_id,
                generation, media_grant_revision,
                owner_connection_generation, control_binding_id,
                control_binding_expires_at, lease_expires_at,
                last_interaction_at, started_at, updated_at,
                takeover_of_session_id, media_grant_nonce_hash,
                media_grant_expires_at, media_grant_issued_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING *
            """,
            (
                session_id,
                request.user_id,
                request.activation_id,
                request.device_id,
                request.device_kind,
                request.transport,
                request.room_name,
                request.participant_identity,
                request.visible_chat_id,
                generation,
                request.owner_connection_generation,
                request.control_binding_id,
                request.control_binding_expires_at,
                request.lease_expires_at,
                now,
                now,
                now,
                takeover_of,
                request.media_grant_nonce_hash,
                request.media_grant_expires_at,
                request.media_grant_issued_at,
            ),
        )
        row = cursor.fetchone()
        if row is None:  # pragma: no cover - PostgreSQL RETURNING invariant.
            raise RuntimeError("voice_session_insert_failed")
        return row

    @staticmethod
    def _validate_create_lifetimes(request: CreateSession, now: datetime) -> None:
        if request.control_binding_expires_at <= now:
            raise ValueError("invalid_control_binding_expiry")
        if request.lease_expires_at <= now:
            raise ValueError("invalid_lease_expiry")
        if request.media_grant_expires_at <= now:
            raise ValueError("invalid_media_grant_expiry")

    @staticmethod
    def _activation_row(cursor: Any, user_id: str, activation_id: str) -> Any:
        cursor.execute(
            "SELECT * FROM voice_session WHERE user_id = %s AND activation_id = %s "
            "FOR UPDATE",
            (user_id, activation_id),
        )
        return cursor.fetchone()

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

    @staticmethod
    def _session_for_update(cursor: Any, user_id: str, session_id: str) -> Any:
        user_id = _user_id(user_id)
        session_id = _uuid4(session_id, "invalid_session_id")
        cursor.execute(
            "SELECT * FROM voice_session WHERE user_id = %s AND session_id = %s "
            "FOR UPDATE",
            (user_id, session_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise VoiceSessionNotFound("voice_session_not_found")
        return row

    @staticmethod
    def _abandon_unaccepted_session_turns(
        cursor: Any,
        *,
        session_id: str,
        generation: int,
        now: datetime,
    ) -> None:
        """Terminalize pre-acceptance rows while preserving accepted work."""

        cursor.execute(
            """
            UPDATE voice_turn
            SET state = 'abandoned', terminal_kind = 'abandoned',
                rejection_reason = 'stale_session',
                rejection_retry_policy = 'explicit_user_retry',
                terminal_at = %s, updated_at = %s
            WHERE session_id = %s AND session_generation = %s
              AND state IN ('recognizing', 'submitting')
            """,
            (now, now, session_id, generation),
        )

    @classmethod
    def _end_session_row(
        cls,
        cursor: Any,
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
        cursor.execute(
            """
            UPDATE voice_session
            SET state = 'ended', microphone_enabled = FALSE,
                foreground_active = FALSE,
                foreground_reason = 'connection_lost', ended_at = %s,
                end_reason = %s, chat_unavailable_at = COALESCE(%s, chat_unavailable_at),
                idle_started_at = NULL, updated_at = %s,
                control_owner_id = NULL, control_lease_expires_at = NULL
            WHERE session_id = %s AND ended_at IS NULL
            RETURNING *
            """,
            (
                now,
                reason,
                chat_unavailable_at,
                now,
                row["session_id"],
            ),
        )
        updated = cursor.fetchone()
        if updated is None:  # pragma: no cover - row lock/WHERE invariant.
            raise RuntimeError("voice_session_end_failed")
        if abandon_unaccepted:
            cls._abandon_unaccepted_session_turns(
                cursor,
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

    @staticmethod
    def _apply_control_binding(
        cursor: Any,
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
        cursor.execute(
            """
            UPDATE voice_session
            SET owner_connection_generation = %s, control_binding_id = %s,
                control_binding_expires_at = %s, updated_at = %s
            WHERE session_id = %s
            RETURNING *
            """,
            (
                control.connection_generation,
                control.binding_id,
                control.binding_expires_at,
                now,
                row["session_id"],
            ),
        )
        updated = cursor.fetchone()
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

    @staticmethod
    def _lock_identity(cursor: Any, namespace: str, *parts: str) -> None:
        digest = hashlib.sha256()
        digest.update(b"astraldeep.voice.repository.v1\0")
        digest.update(namespace.encode("ascii"))
        for part in parts:
            encoded = part.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        lock_id = int.from_bytes(digest.digest()[:8], "big", signed=True)
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))

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
        transport=str(row["transport"]),
        room_name=str(row["room_name"]),
        participant_identity=str(row["participant_identity"]),
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
        media_grant_nonce_hash=bytes(row["media_grant_nonce_hash"]),
        media_grant_expires_at=_aware(
            row["media_grant_expires_at"], "invalid_media_grant_expiry"
        ),
        media_grant_consumed_at=_optional_aware(row.get("media_grant_consumed_at")),
        last_media_refresh_id=_uuid_text(row.get("last_media_refresh_id")),
        media_grant_issued_at=_aware(
            row["media_grant_issued_at"], "invalid_media_grant_issued_at"
        ),
        worker_assignment_id=_uuid_text(row.get("worker_assignment_id")),
        worker_rtc_grant_revision=int(row["worker_rtc_grant_revision"]),
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
