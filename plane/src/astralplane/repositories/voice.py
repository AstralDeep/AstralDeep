"""Durable metadata for voice sessions and turns, including guidance observations for
legacy dual-lock-order writers; excludes real-time media handling. Backs AstralDeep's
voice orchestration and voice_migration tests.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from astralplane.contracts import Record, Transaction
from astralplane.errors import LockContentionError, PlaneError, is_lock_contention
from astralplane.repositories import RepositoryConflictError, RepositoryValidationError


class VoiceSessionState(StrEnum):
    STARTING = "starting"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RECONNECTING = "reconnecting"
    ENDING = "ending"
    ENDED = "ended"
    ERROR = "error"


class VoiceTurnState(StrEnum):
    RECOGNIZING = "recognizing"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    WAITING_ON_USER = "waiting_on_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


_TERMINAL_TURN_STATES = {
    VoiceTurnState.SUCCEEDED,
    VoiceTurnState.FAILED,
    VoiceTurnState.REFUSED,
    VoiceTurnState.CANCELLED,
    VoiceTurnState.ABANDONED,
}

_SESSION_INSERT_FIELDS = (
    "session_id",
    "user_id",
    "activation_id",
    "device_id",
    "device_kind",
    "speech_backend",
    "transport",
    "room_name",
    "participant_identity",
    "visible_chat_id",
    "generation",
    "media_grant_revision",
    "owner_connection_generation",
    "control_binding_id",
    "control_binding_expires_at",
    "lease_expires_at",
    "last_interaction_at",
    "started_at",
    "updated_at",
    "takeover_of_session_id",
    "media_grant_nonce_hash",
    "media_grant_expires_at",
    "media_grant_issued_at",
    "worker_rtc_grant_revision",
)
_SESSION_PATCH_FIELDS = frozenset(
    {
        "participant_identity",
        "worker_identity",
        "visible_chat_id",
        "chat_context_revision",
        "applied_visible_chat_id",
        "applied_chat_context_revision",
        "state",
        "speech_muted",
        "microphone_enabled",
        "foreground_active",
        "foreground_reason",
        "generation",
        "media_grant_revision",
        "owner_connection_generation",
        "control_binding_id",
        "control_binding_expires_at",
        "lease_expires_at",
        "control_owner_id",
        "control_lease_expires_at",
        "last_interaction_at",
        "idle_started_at",
        "updated_at",
        "ended_at",
        "end_reason",
        "chat_unavailable_at",
        "media_grant_nonce_hash",
        "media_grant_expires_at",
        "media_grant_consumed_at",
        "last_media_refresh_id",
        "media_grant_issued_at",
        "worker_assignment_id",
        "worker_rtc_grant_revision",
        "worker_rtc_grant_issued_at",
        "worker_rtc_grant_expires_at",
    }
)
_TURN_INSERT_FIELDS = (
    "turn_id",
    "client_turn_id",
    "session_id",
    "session_generation",
    "media_grant_revision",
    "user_id",
    "chat_id",
    "chat_context_revision",
    "execution_base_render_revision",
    "submission_id",
    "request_generation",
    "result_request_generation",
    "state",
    "is_foreground",
    "created_at",
    "updated_at",
)
_TURN_PATCH_FIELDS = frozenset(
    {
        "detected_language",
        "spoken_output_policy",
        "output_reason",
        "result_request_generation",
        "accepted_connection_generation",
        "message_id",
        "acceptance_commit_id",
        "result_commit_id",
        "operation_id",
        "background_task_id",
        "state",
        "is_foreground",
        "terminal_kind",
        "rejection_reason",
        "rejection_retry_policy",
        "origin_chat_unavailable_at",
        "origin_chat_unavailable_reason",
        "result_id",
        "recap_source",
        "sensitivity",
        "sensitive_consent_at",
        "sensitive_consent_method",
        "sensitive_consent_consumed_at",
        "announcement_sequence",
        "result_reserved_samples",
        "result_quantum_count",
        "last_announcement_kind",
        "last_phrase_key",
        "next_announcement_due_at",
        "announcement_claim_id",
        "announcement_claim_expires_at",
        "last_announcement_started_at",
        "last_speech_finished_at",
        "last_client_playout_started_at",
        "last_client_playout_finished_at",
        "last_client_playout_sequence",
        "accepted_at",
        "processing_started_at",
        "waiting_started_at",
        "terminal_at",
        "updated_at",
    }
)


@dataclass(frozen=True, slots=True)
class VoiceSessionCreate:
    session_id: str
    owner_id: str
    activation_id: str
    device_id: str
    device_kind: str
    speech_backend: str
    transport: str
    room_name: str | None
    participant_identity: str | None
    visible_chat_id: str
    owner_connection_generation: str
    control_binding_id: str
    control_binding_expires_at: datetime
    lease_expires_at: datetime
    media_grant_nonce_hash: bytes | None
    media_grant_issued_at: datetime | None
    media_grant_expires_at: datetime | None
    started_at: datetime

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("session_id", self.session_id, 64),
            ("owner_id", self.owner_id, 512),
            ("activation_id", self.activation_id, 64),
            ("device_id", self.device_id, 64),
            ("visible_chat_id", self.visible_chat_id, 255),
            ("owner_connection_generation", self.owner_connection_generation, 64),
            ("control_binding_id", self.control_binding_id, 64),
        ):
            _required(name, value, maximum)
        if self.device_kind not in {"web", "windows", "android", "ios", "macos", "watchos"}:
            raise ValueError("device_kind is not supported")
        if not _valid_backend_core_shape(
            speech_backend=self.speech_backend,
            transport=self.transport,
            room_name=self.room_name,
            participant_identity=self.participant_identity,
            media_grant_nonce_hash=self.media_grant_nonce_hash,
            media_grant_issued_at=self.media_grant_issued_at,
            media_grant_expires_at=self.media_grant_expires_at,
            worker_rtc_grant_revision=(
                1 if self.speech_backend == "llm_factory" else None
            ),
        ):
            raise ValueError("voice session speech backend fields are inconsistent")
        if self.speech_backend == "llm_factory":
            assert self.room_name is not None
            assert self.participant_identity is not None
            assert self.media_grant_nonce_hash is not None
            assert self.media_grant_issued_at is not None
            assert self.media_grant_expires_at is not None
            _required("room_name", self.room_name, 255)
            _required("participant_identity", self.participant_identity, 255)
            if len(self.media_grant_nonce_hash) != 32:
                raise ValueError("media_grant_nonce_hash must contain 32 bytes")
        for name, value in (
            ("control_binding_expires_at", self.control_binding_expires_at),
            ("lease_expires_at", self.lease_expires_at),
            ("started_at", self.started_at),
        ):
            _aware(name, value)
        if min(self.control_binding_expires_at, self.lease_expires_at) <= self.started_at:
            raise ValueError("session leases must expire after the session starts")
        if self.speech_backend == "llm_factory":
            assert self.media_grant_issued_at is not None
            assert self.media_grant_expires_at is not None
            _aware("media_grant_issued_at", self.media_grant_issued_at)
            _aware("media_grant_expires_at", self.media_grant_expires_at)
        if (
            self.media_grant_expires_at is not None
            and self.media_grant_issued_at is not None
            and self.media_grant_expires_at <= self.media_grant_issued_at
        ):
            raise ValueError("grant expiry must follow grant issue time")


@dataclass(frozen=True, slots=True)
class VoiceSession:
    session_id: str
    owner_id: str
    activation_id: str
    device_id: str
    device_kind: str
    speech_backend: str
    transport: str
    visible_chat_id: str
    chat_context_revision: int
    state: VoiceSessionState
    generation: int
    owner_connection_generation: str
    lease_expires_at: datetime
    started_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    end_reason: str | None


@dataclass(frozen=True, slots=True)
class VoiceTurnCreate:
    turn_id: str
    client_turn_id: str
    session_id: str
    session_generation: int
    media_grant_revision: int
    owner_id: str
    chat_id: str
    chat_context_revision: int
    execution_base_render_revision: int
    submission_id: str
    request_generation: str

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("turn_id", self.turn_id, 64),
            ("client_turn_id", self.client_turn_id, 64),
            ("session_id", self.session_id, 64),
            ("owner_id", self.owner_id, 512),
            ("chat_id", self.chat_id, 255),
            ("submission_id", self.submission_id, 64),
            ("request_generation", self.request_generation, 64),
        ):
            _required(name, value, maximum)
        if (
            min(
                self.session_generation,
                self.media_grant_revision,
                self.chat_context_revision,
            )
            <= 0
        ):
            raise ValueError("voice turn generations must be positive")
        if self.execution_base_render_revision < 0:
            raise ValueError("execution_base_render_revision cannot be negative")


@dataclass(frozen=True, slots=True)
class VoiceTurn:
    turn_id: str
    client_turn_id: str
    session_id: str
    owner_id: str
    chat_id: str
    submission_id: str
    request_generation: str
    state: VoiceTurnState
    operation_id: str | None
    result_id: str | None
    terminal_kind: str | None
    accepted_at: datetime | None
    terminal_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class VoiceGuidanceObservation:
    session: Record = field(repr=False)
    turn: Record = field(repr=False)
    observed_at: datetime


class VoiceRepository:

    def assert_current_guidance_turn(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        turn_id: str,
        expected_session_generation: int,
        expected_media_grant_revision: int,
        operation_id: str,
    ) -> VoiceGuidanceObservation:
        if type(owner_id) is not str or not owner_id.strip() or len(owner_id) > 512:
            raise RepositoryValidationError("voice guidance owner is invalid")
        for value in (session_id, turn_id, operation_id):
            try:
                if type(value) is not str or len(value) != 36:
                    raise ValueError
                parsed = uuid.UUID(value)
                if parsed.version != 4 or str(parsed) != value:
                    raise ValueError
            except ValueError:
                raise RepositoryValidationError("voice guidance identity is invalid") from None
        for value in (expected_session_generation, expected_media_grant_revision):
            if type(value) is not int or not 1 <= value <= 2**63 - 1:
                raise RepositoryValidationError("voice guidance generation is invalid")
        try:
            with transaction.savepoint("voice_guidance_current_read"):
                # NOWAIT: legacy writers take these locks in both orders
                session = transaction.fetch_one(
                    "SELECT * FROM voice_session WHERE user_id=%s AND session_id=%s "
                    "FOR UPDATE NOWAIT", (owner_id, session_id),
                )
                turn = transaction.fetch_one(
                    "SELECT * FROM voice_turn WHERE user_id=%s AND turn_id=%s "
                    "FOR UPDATE NOWAIT", (owner_id, turn_id),
                )
                clock = transaction.fetch_one("SELECT clock_timestamp() AS now")
                if session is None or turn is None or clock is None:
                    raise RepositoryConflictError("voice guidance is unavailable")
                now = clock["now"]
                if not _is_aware_datetime(now):
                    raise RepositoryConflictError("voice guidance is unavailable")
                _validate_persisted_backend_shape(session)
                counters = (
                    (session["generation"], expected_session_generation),
                    (session["media_grant_revision"], expected_media_grant_revision),
                    (turn["session_generation"], expected_session_generation),
                    (turn["media_grant_revision"], expected_media_grant_revision),
                )
                if (
                    any(type(actual) is not int or actual != expected
                        for actual, expected in counters)
                    or str(session["session_id"]) != session_id
                    or session["user_id"] != owner_id
                    or session["state"] != "active"
                    or session["ended_at"] is not None
                    or session["chat_unavailable_at"] is not None
                    or not _is_aware_datetime(session["started_at"])
                    or not _is_aware_datetime(session["lease_expires_at"])
                    or not session["started_at"] <= now < session["lease_expires_at"]
                    or str(turn["turn_id"]) != turn_id
                    or str(turn["session_id"]) != session_id
                    or turn["user_id"] != owner_id
                    or str(turn["operation_id"]) != operation_id
                    or turn["state"] not in {"accepted", "processing", "waiting_on_user"}
                    or not _is_aware_datetime(turn["accepted_at"])
                    or turn["accepted_at"] > now
                    or turn["terminal_at"] is not None
                    or turn["terminal_kind"] is not None
                    or turn["origin_chat_unavailable_at"] is not None
                ):
                    raise RepositoryConflictError("voice guidance is unavailable")
                return VoiceGuidanceObservation(
                    MappingProxyType(dict(session)), MappingProxyType(dict(turn)),
                    now.astimezone(UTC),
                )
        except Exception as exc:
            if is_lock_contention(exc):
                raise LockContentionError("voice guidance is unavailable") from None
            if (getattr(exc, "pgcode", None) == "55P03"
                    or isinstance(exc, (KeyError, TypeError, ValueError))):
                raise RepositoryConflictError("voice guidance is unavailable") from None
            raise

    def lock_identity(
        self,
        transaction: Transaction,
        *,
        namespace: str,
        parts: tuple[str, ...],
    ) -> None:
        _required("namespace", namespace, 64)
        if not parts or any(not isinstance(part, str) or not part for part in parts):
            raise RepositoryValidationError("voice lock parts must be non-empty strings")
        digest = hashlib.sha256()
        digest.update(b"astraldeep.voice.repository.v1\0")
        digest.update(namespace.encode("ascii"))
        for part in parts:
            encoded = part.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        lock_id = int.from_bytes(digest.digest()[:8], "big", signed=True)
        transaction.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id,))

    def get_session_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        for_update: bool = False,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return transaction.fetch_one(
            "SELECT * FROM voice_session WHERE user_id = %s AND session_id = %s"
            + suffix,
            (owner_id, session_id),
        )

    def get_session_record_for_administration(
        self,
        transaction: Transaction,
        *,
        session_id: str,
        for_update: bool = False,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return transaction.fetch_one(
            "SELECT * FROM voice_session WHERE session_id = %s" + suffix,
            (session_id,),
        )

    def get_activation_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        activation_id: str,
        for_update: bool = True,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return transaction.fetch_one(
            "SELECT * FROM voice_session WHERE user_id = %s AND activation_id = %s"
            + suffix,
            (owner_id, activation_id),
        )

    def get_live_session_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        for_update: bool = False,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return transaction.fetch_one(
            "SELECT * FROM voice_session WHERE user_id = %s AND ended_at IS NULL"
            + suffix,
            (owner_id,),
        )

    def get_turn_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        turn_id: str,
        for_update: bool = False,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return transaction.fetch_one(
            "SELECT * FROM voice_turn WHERE user_id = %s AND turn_id = %s" + suffix,
            (owner_id, turn_id),
        )

    def get_turn_record_for_administration(
        self,
        transaction: Transaction,
        *,
        turn_id: str,
        for_update: bool = False,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return transaction.fetch_one(
            "SELECT * FROM voice_turn WHERE turn_id = %s" + suffix,
            (turn_id,),
        )

    def get_client_turn_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        client_turn_id: str,
        for_update: bool = False,
    ) -> Record | None:
        suffix = " FOR UPDATE" if for_update else ""
        return transaction.fetch_one(
            "SELECT * FROM voice_turn WHERE user_id = %s AND client_turn_id = %s"
            + suffix,
            (owner_id, client_turn_id),
        )

    def get_submission_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        submission_id: str,
        request_generation: str,
    ) -> Record | None:
        return transaction.fetch_one(
            "SELECT * FROM voice_turn WHERE user_id = %s "
            "AND submission_id = %s AND request_generation = %s",
            (owner_id, submission_id, request_generation),
        )

    def max_client_playout_sequence(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
    ) -> int:
        row = transaction.fetch_one(
            "SELECT COALESCE(MAX(last_client_playout_sequence), -1) AS sequence "
            "FROM voice_turn WHERE user_id = %s AND session_id = %s",
            (owner_id, session_id),
        )
        return -1 if row is None else int(row["sequence"])

    def has_turn_in_states(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        states: tuple[str, ...],
    ) -> bool:
        if not states:
            raise RepositoryValidationError("voice turn state inventory cannot be empty")
        return (
            transaction.fetch_one(
                "SELECT 1 FROM voice_turn WHERE session_id = %s "
                "AND user_id = %s AND state = ANY(%s) LIMIT 1",
                (session_id, owner_id, list(states)),
            )
            is not None
        )

    def list_true_idle_session_records_for_administration(
        self,
        transaction: Transaction,
        *,
        cutoff: datetime,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
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

    def chat_exists(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        for_update: bool = False,
    ) -> bool:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            transaction.fetch_one(
                "SELECT id FROM chats WHERE id = %s AND user_id = %s" + suffix,
                (chat_id, owner_id),
            )
            is not None
        )

    def get_chat_render_revision(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        for_share: bool = False,
    ) -> int | None:
        suffix = " FOR SHARE" if for_share else ""
        row = transaction.fetch_one(
            "SELECT render_revision FROM chats WHERE id = %s AND user_id = %s"
            + suffix,
            (chat_id, owner_id),
        )
        return None if row is None else int(row.get("render_revision") or 0)

    def list_chat_turn_records_for_update(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
            "SELECT * FROM voice_turn WHERE user_id = %s AND chat_id = %s "
            "ORDER BY created_at, turn_id FOR UPDATE",
            (owner_id, chat_id),
        )

    def list_chat_session_records_for_update(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
            "SELECT * FROM voice_session WHERE user_id = %s "
            "AND visible_chat_id = %s ORDER BY started_at, session_id FOR UPDATE",
            (owner_id, chat_id),
        )

    def abandon_chat_turns(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        turn_ids: tuple[str, ...],
        reason: str,
        now: datetime,
        accepted: bool,
    ) -> int:
        if not turn_ids:
            return 0
        if accepted:
            result = transaction.execute(
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
                    OR state IN ('accepted', 'processing', 'waiting_on_user')
                  )
                """,
                (now, reason, now, now, owner_id, list(turn_ids)),
            )
        else:
            result = transaction.execute(
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
                (now, now, owner_id, list(turn_ids)),
            )
        return int(result.rowcount)

    def abort_staged_chat_result_commits(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
        now: datetime,
    ) -> tuple[str, ...]:
        rows = transaction.fetch_all(
            """
            SELECT commit_id
            FROM conversation_commit
            WHERE chat_id = %s AND owner_user_id = %s
              AND publication_role = 'assistant_result'
              AND state = 'staged'
            ORDER BY started_at, commit_id
            FOR UPDATE
            """,
            (chat_id, owner_id),
        )
        commit_ids = tuple(str(row["commit_id"]) for row in rows)
        for commit_id in commit_ids:
            transaction.execute(
                "DELETE FROM saved_components WHERE conversation_commit_id = %s",
                (commit_id,),
            )
            transaction.execute(
                "DELETE FROM workspace_layout WHERE conversation_commit_id = %s",
                (commit_id,),
            )
            transaction.execute(
                "DELETE FROM messages WHERE conversation_commit_id = %s",
                (commit_id,),
            )
            transaction.execute(
                """
                UPDATE conversation_commit
                SET state = 'aborted', aborted_at = %s,
                    execution_base_commit_id = NULL
                WHERE commit_id = %s AND state = 'staged'
                """,
                (now, commit_id),
            )
        return commit_ids

    def delete_owned_chat(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        chat_id: str,
    ) -> bool:
        result = transaction.execute(
            "DELETE FROM chats WHERE id = %s AND user_id = %s",
            (chat_id, owner_id),
        )
        return int(result.rowcount) == 1

    def insert_session_record(
        self,
        transaction: Transaction,
        *,
        values: Mapping[str, object],
    ) -> Record:
        normalized = _exact_values(values, _SESSION_INSERT_FIELDS, "voice session")
        if not _valid_backend_core_shape(
            speech_backend=normalized["speech_backend"],
            transport=normalized["transport"],
            room_name=normalized["room_name"],
            participant_identity=normalized["participant_identity"],
            media_grant_nonce_hash=normalized["media_grant_nonce_hash"],
            media_grant_issued_at=normalized["media_grant_issued_at"],
            media_grant_expires_at=normalized["media_grant_expires_at"],
            worker_rtc_grant_revision=normalized["worker_rtc_grant_revision"],
        ):
            raise RepositoryValidationError(
                "voice session speech backend fields are inconsistent"
            )
        placeholders = ", ".join("%s" for _field in _SESSION_INSERT_FIELDS)
        row = transaction.fetch_one(
            "INSERT INTO voice_session ("
            + ", ".join(_SESSION_INSERT_FIELDS)
            + ") VALUES ("
            + placeholders
            + ") RETURNING *",
            tuple(normalized[field] for field in _SESSION_INSERT_FIELDS),
        )
        if row is None:
            raise PlaneError(
                "voice session insert returned no row",
                code="voice_session_insert_failed",
            )
        return row

    def insert_turn_record(
        self,
        transaction: Transaction,
        *,
        values: Mapping[str, object],
    ) -> Record:
        normalized = _exact_values(values, _TURN_INSERT_FIELDS, "voice turn")
        placeholders = ", ".join("%s" for _field in _TURN_INSERT_FIELDS)
        row = transaction.fetch_one(
            "INSERT INTO voice_turn ("
            + ", ".join(_TURN_INSERT_FIELDS)
            + ") VALUES ("
            + placeholders
            + ") RETURNING *",
            tuple(normalized[field] for field in _TURN_INSERT_FIELDS),
        )
        if row is None:
            raise PlaneError("voice turn insert returned no row", code="voice_turn_insert_failed")
        return row

    def patch_session_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        updates: Mapping[str, object],
        require_live: bool | None = None,
    ) -> Record | None:
        normalized = _patch_values(updates, _SESSION_PATCH_FIELDS, "voice session")
        assignments = ", ".join(f"{field} = %s" for field in normalized)
        predicates = ["session_id = %s", "user_id = %s"]
        parameters: list[object] = [*normalized.values(), session_id, owner_id]
        if require_live is True:
            predicates.append("ended_at IS NULL")
        elif require_live is False:
            predicates.append("ended_at IS NOT NULL")
        return transaction.fetch_one(
            "UPDATE voice_session SET "
            + assignments
            + " WHERE "
            + " AND ".join(predicates)
            + " RETURNING *",
            tuple(parameters),
        )

    def patch_turn_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        turn_id: str,
        updates: Mapping[str, object],
        expected_states: tuple[str, ...] | None = None,
    ) -> Record | None:
        normalized = _patch_values(updates, _TURN_PATCH_FIELDS, "voice turn")
        assignments = ", ".join(f"{field} = %s" for field in normalized)
        predicates = ["turn_id = %s", "user_id = %s"]
        parameters: list[object] = [*normalized.values(), turn_id, owner_id]
        if expected_states is not None:
            if not expected_states or any(not isinstance(state, str) for state in expected_states):
                raise RepositoryValidationError("expected voice turn states are invalid")
            predicates.append("state = ANY(%s)")
            parameters.append(list(expected_states))
        return transaction.fetch_one(
            "UPDATE voice_turn SET "
            + assignments
            + " WHERE "
            + " AND ".join(predicates)
            + " RETURNING *",
            tuple(parameters),
        )

    def complete_announcement_claim(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        turn_id: str,
        claim_id: str | None,
        claim_expires_at: datetime | None,
    ) -> bool:
        result = transaction.execute(
            """
            UPDATE voice_turn
            SET announcement_claim_id = %s,
                announcement_claim_expires_at = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE turn_id = %s AND user_id = %s
            """,
            (claim_id, claim_expires_at, turn_id, owner_id),
        )
        return int(result.rowcount) == 1

    def abandon_unaccepted_session_turns(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        generation: int,
        now: datetime,
    ) -> int:
        result = transaction.execute(
            """
            UPDATE voice_turn
            SET state = 'abandoned', terminal_kind = 'abandoned',
                rejection_reason = 'stale_session',
                rejection_retry_policy = 'explicit_user_retry',
                terminal_at = %s, updated_at = %s
            WHERE session_id = %s AND user_id = %s AND session_generation = %s
              AND state IN ('recognizing', 'submitting')
            """,
            (now, now, session_id, owner_id, generation),
        )
        return int(result.rowcount)

    def clear_foreground_turns(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        now: datetime,
        except_turn_id: str | None = None,
    ) -> int:
        exclusion = "" if except_turn_id is None else " AND turn_id <> %s"
        parameters: tuple[object, ...] = (now, session_id, owner_id)
        if except_turn_id is not None:
            parameters += (except_turn_id,)
        result = transaction.execute(
            """
            UPDATE voice_turn
            SET is_foreground = FALSE, updated_at = %s
            WHERE session_id = %s AND user_id = %s AND is_foreground
              AND state NOT IN (
                'succeeded', 'failed', 'refused', 'cancelled', 'abandoned'
              )
            """
            + exclusion,
            parameters,
        )
        return int(result.rowcount)

    def list_owned_live_session_records_for_administration(
        self,
        transaction: Transaction,
        *,
        control_owner_id: str,
        limit: int,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
            """
            SELECT * FROM voice_session
            WHERE ended_at IS NULL AND control_owner_id = %s
            ORDER BY started_at, session_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (control_owner_id, limit),
        )

    def list_expired_session_records_for_administration(
        self,
        transaction: Transaction,
        *,
        now: datetime,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
            """
            SELECT * FROM voice_session
            WHERE ended_at IS NULL AND lease_expires_at <= %s
            ORDER BY lease_expires_at, session_id
            FOR UPDATE SKIP LOCKED
            """,
            (now,),
        )

    def list_renewable_control_session_records_for_administration(
        self,
        transaction: Transaction,
        *,
        control_owner_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
            """
            SELECT * FROM voice_session
            WHERE ended_at IS NULL
              AND control_owner_id = %s
              AND control_lease_expires_at > %s
            ORDER BY control_lease_expires_at, session_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (control_owner_id, now, limit),
        )

    def release_control_lease_record(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
    ) -> bool:
        result = transaction.execute(
            """
            UPDATE voice_session
            SET control_owner_id = NULL, control_lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE session_id = %s AND user_id = %s
            """,
            (session_id, owner_id),
        )
        return int(result.rowcount) == 1

    def reconcile_ended_unaccepted_turns_for_administration(
        self,
        transaction: Transaction,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
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
            (limit, now, now),
        )

    def reconcile_ended_terminal_operation_turns_for_administration(
        self,
        transaction: Transaction,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[Record, ...]:
        return transaction.fetch_all(
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
                        AND acceptance.request_generation = turn.request_generation
                        AND acceptance.operation_id = turn.operation_id
                        AND acceptance.operation_execution_generation
                            = operation.execution_generation
                        AND result.state = 'committed'
                        AND result.publication_role = 'assistant_result'
                        AND result.owner_user_id = turn.user_id
                        AND result.chat_id = turn.chat_id
                        AND result.request_generation = turn.result_request_generation
                        AND result.operation_id = turn.operation_id
                        AND result.operation_execution_generation
                            = operation.execution_generation
                        AND result.parent_commit_id = turn.acceptance_commit_id
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
                  AND turn.state IN ('accepted', 'processing', 'waiting_on_user')
                  AND operation.state IN (
                    'completed', 'failed', 'cancelled', 'retryable'
                  )
                  AND operation.operation_kind = 'voice_chat_message'
                  AND operation.owner_scope = 'user'
                  AND operation.owner_user_id = turn.user_id
                  AND operation.chat_id = turn.chat_id
                  AND operation.request_generation = turn.request_generation
                  AND operation.connection_generation
                      = turn.accepted_connection_generation
                ORDER BY turn.updated_at, turn.turn_id
                FOR UPDATE OF turn SKIP LOCKED
                LIMIT %s
            ), candidates AS (
                SELECT
                    turn_id,
                    CASE
                        WHEN operation_state = 'completed' AND exact_result_committed
                            THEN 'succeeded'
                        WHEN operation_state = 'cancelled' THEN 'cancelled'
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
            (limit, now, now),
        )

    def create_session(self, transaction: Transaction, session: VoiceSessionCreate) -> VoiceSession:
        row = transaction.fetch_one(
            """
            INSERT INTO voice_session (
                session_id, user_id, activation_id, device_id, device_kind,
                speech_backend, transport, room_name, participant_identity,
                visible_chat_id,
                owner_connection_generation, control_binding_id,
                control_binding_expires_at, lease_expires_at,
                media_grant_nonce_hash, media_grant_issued_at,
                media_grant_expires_at, worker_rtc_grant_revision,
                started_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (user_id, activation_id) DO NOTHING
            RETURNING *
            """,
            (
                session.session_id,
                session.owner_id,
                session.activation_id,
                session.device_id,
                session.device_kind,
                session.speech_backend,
                session.transport,
                session.room_name,
                session.participant_identity,
                session.visible_chat_id,
                session.owner_connection_generation,
                session.control_binding_id,
                session.control_binding_expires_at,
                session.lease_expires_at,
                session.media_grant_nonce_hash,
                session.media_grant_issued_at,
                session.media_grant_expires_at,
                1 if session.speech_backend == "llm_factory" else None,
                session.started_at,
                session.started_at,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM voice_session WHERE user_id = %s AND activation_id = %s",
                (session.owner_id, session.activation_id),
            )
        if row is None or not _same_session(row, session):
            raise PlaneError(
                "voice activation identity has conflicting semantics",
                code="voice_session_idempotency_conflict",
                metadata={"owner_id": session.owner_id},
            )
        return _session(row)

    def get_session(
        self, transaction: Transaction, *, owner_id: str, session_id: str
    ) -> VoiceSession | None:
        row = transaction.fetch_one(
            "SELECT * FROM voice_session WHERE session_id = %s AND user_id = %s",
            (session_id, owner_id),
        )
        return None if row is None else _session(row)

    def get_live_session(self, transaction: Transaction, *, owner_id: str) -> VoiceSession | None:
        row = transaction.fetch_one(
            """
            SELECT * FROM voice_session
            WHERE user_id = %s AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (owner_id,),
        )
        return None if row is None else _session(row)

    def advance_session(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_generation: int,
        expected_context_revision: int,
        state: VoiceSessionState,
        visible_chat_id: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> VoiceSession:
        if state is VoiceSessionState.ENDED:
            raise ValueError("end_session owns the terminal transition")
        _required("visible_chat_id", visible_chat_id, 255)
        _aware("lease_expires_at", lease_expires_at)
        _aware("now", now)
        if min(expected_generation, expected_context_revision) <= 0 or lease_expires_at <= now:
            raise ValueError(
                "session transition requires current positive fences and a future lease"
            )
        row = transaction.fetch_one(
            """
            UPDATE voice_session
            SET state = %s, visible_chat_id = %s,
                chat_context_revision = chat_context_revision + 1,
                generation = generation + 1, lease_expires_at = %s,
                updated_at = %s
            WHERE session_id = %s AND user_id = %s AND ended_at IS NULL
              AND generation = %s AND chat_context_revision = %s
            RETURNING *
            """,
            (
                state.value,
                visible_chat_id,
                lease_expires_at,
                now,
                session_id,
                owner_id,
                expected_generation,
                expected_context_revision,
            ),
        )
        return _required_session(row, owner_id)

    def end_session(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        session_id: str,
        expected_generation: int,
        reason: str,
        ended_at: datetime,
    ) -> VoiceSession:
        if reason not in {
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
        }:
            raise ValueError("voice session end reason is not supported")
        _aware("ended_at", ended_at)
        row = transaction.fetch_one(
            """
            UPDATE voice_session
            SET state = 'ended', end_reason = %s, ended_at = %s,
                chat_unavailable_at = CASE
                    WHEN %s IN ('chat_deleted', 'chat_unauthorized') THEN %s
                    ELSE NULL
                END,
                generation = generation + 1, updated_at = %s
            WHERE session_id = %s AND user_id = %s AND ended_at IS NULL
              AND generation = %s
            RETURNING *
            """,
            (
                reason,
                ended_at,
                reason,
                ended_at,
                ended_at,
                session_id,
                owner_id,
                expected_generation,
            ),
        )
        return _required_session(row, owner_id)

    def create_turn(self, transaction: Transaction, turn: VoiceTurnCreate) -> VoiceTurn:
        row = transaction.fetch_one(
            """
            INSERT INTO voice_turn (
                turn_id, client_turn_id, session_id, session_generation,
                media_grant_revision, user_id, chat_id, chat_context_revision,
                execution_base_render_revision, submission_id, request_generation
            ) SELECT %s, %s, session_id, %s, %s, user_id, %s, %s, %s, %s, %s
                FROM voice_session
               WHERE session_id = %s AND user_id = %s AND ended_at IS NULL
                 AND generation = %s AND chat_context_revision = %s
                 AND visible_chat_id = %s AND media_grant_revision = %s
            ON CONFLICT (user_id, submission_id) DO NOTHING
            RETURNING *
            """,
            (
                turn.turn_id,
                turn.client_turn_id,
                turn.session_generation,
                turn.media_grant_revision,
                turn.chat_id,
                turn.chat_context_revision,
                turn.execution_base_render_revision,
                turn.submission_id,
                turn.request_generation,
                turn.session_id,
                turn.owner_id,
                turn.session_generation,
                turn.chat_context_revision,
                turn.chat_id,
                turn.media_grant_revision,
            ),
        )
        if row is None:
            row = transaction.fetch_one(
                "SELECT * FROM voice_turn WHERE user_id = %s AND submission_id = %s",
                (turn.owner_id, turn.submission_id),
            )
        if row is None or not _same_turn(row, turn):
            raise PlaneError(
                "voice turn submission has conflicting semantics or a stale session fence",
                code="voice_turn_idempotency_conflict",
                metadata={"owner_id": turn.owner_id},
            )
        return _turn(row)

    def get_turn(
        self, transaction: Transaction, *, owner_id: str, turn_id: str
    ) -> VoiceTurn | None:
        row = transaction.fetch_one(
            "SELECT * FROM voice_turn WHERE turn_id = %s AND user_id = %s",
            (turn_id, owner_id),
        )
        return None if row is None else _turn(row)

    def transition_turn(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        turn_id: str,
        expected_state: VoiceTurnState,
        state: VoiceTurnState,
        operation_id: str | None,
        result_id: str | None,
        now: datetime,
    ) -> VoiceTurn:
        _aware("now", now)
        terminal = state in _TERMINAL_TURN_STATES
        row = transaction.fetch_one(
            """
            UPDATE voice_turn
            SET state = %s, operation_id = COALESCE(operation_id, %s),
                result_id = COALESCE(result_id, %s), terminal_kind = %s,
                accepted_at = CASE WHEN %s = 'accepted' THEN COALESCE(accepted_at, %s)
                                   ELSE accepted_at END,
                terminal_at = %s, updated_at = %s
            WHERE turn_id = %s AND user_id = %s AND state = %s
              AND state NOT IN (
                  'succeeded', 'failed', 'refused', 'cancelled', 'abandoned'
              )
              AND (%s OR operation_id IS NULL OR operation_id::text = %s)
              AND (%s OR result_id IS NULL OR result_id = %s)
            RETURNING *
            """,
            (
                state.value,
                operation_id,
                result_id,
                state.value if terminal else None,
                state.value,
                now,
                now if terminal else None,
                now,
                turn_id,
                owner_id,
                expected_state.value,
                operation_id is None,
                operation_id,
                result_id is None,
                result_id,
            ),
        )
        if row is None:
            raise PlaneError(
                "voice turn state fence is stale",
                code="voice_turn_state_conflict",
                metadata={"owner_id": owner_id},
            )
        return _turn(row)


def _required(name: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")


def _exact_values(
    values: Mapping[str, object],
    expected: tuple[str, ...],
    subject: str,
) -> dict[str, object]:
    if not isinstance(values, Mapping):
        raise RepositoryValidationError(f"{subject} values must be a mapping")
    missing = set(expected) - set(values)
    unknown = set(values) - set(expected)
    if missing or unknown:
        raise RepositoryValidationError(
            f"{subject} values do not match the repository contract",
            metadata={"missing": sorted(missing), "unknown": sorted(unknown)},
        )
    return {field: values[field] for field in expected}


def _patch_values(
    values: Mapping[str, object],
    allowed: frozenset[str],
    subject: str,
) -> dict[str, object]:
    if not isinstance(values, Mapping) or not values:
        raise RepositoryValidationError(f"{subject} updates must be a non-empty mapping")
    unknown = set(values) - allowed
    if unknown:
        raise RepositoryValidationError(
            f"{subject} updates contain unsupported fields",
            metadata={"unknown": sorted(unknown)},
        )
    return dict(values)


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _valid_backend_core_shape(
    *,
    speech_backend: object,
    transport: object,
    room_name: object,
    participant_identity: object,
    media_grant_nonce_hash: object,
    media_grant_issued_at: object,
    media_grant_expires_at: object,
    worker_rtc_grant_revision: object,
) -> bool:
    remote_fields = (
        room_name,
        participant_identity,
        media_grant_nonce_hash,
        media_grant_issued_at,
        media_grant_expires_at,
        worker_rtc_grant_revision,
    )
    if speech_backend == "llm_factory":
        return (
            transport in {"livekit", "watch_pcm_websocket"}
            and all(value is not None for value in remote_fields)
            and isinstance(room_name, str)
            and 1 <= len(room_name) <= 255
            and isinstance(participant_identity, str)
            and 1 <= len(participant_identity) <= 255
            and isinstance(media_grant_nonce_hash, (bytes, bytearray, memoryview))
            and len(media_grant_nonce_hash) == 32
            and _is_aware_datetime(media_grant_issued_at)
            and _is_aware_datetime(media_grant_expires_at)
            and media_grant_expires_at > media_grant_issued_at
            and isinstance(worker_rtc_grant_revision, int)
            and not isinstance(worker_rtc_grant_revision, bool)
            and worker_rtc_grant_revision >= 1
        )
    if speech_backend == "client_local":
        return transport == "client_local" and all(
            value is None for value in remote_fields
        )
    return False


def _is_aware_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _valid_remote_worker_shape(row: Record) -> bool:
    worker_fields = (
        row.get("worker_identity"),
        row.get("worker_assignment_id"),
        row.get("worker_rtc_grant_issued_at"),
        row.get("worker_rtc_grant_expires_at"),
    )
    if all(value is None for value in worker_fields):
        return True
    worker_identity, _assignment_id, issued_at, expires_at = worker_fields
    return (
        all(value is not None for value in worker_fields)
        and isinstance(worker_identity, str)
        and 1 <= len(worker_identity) <= 255
        and _is_aware_datetime(issued_at)
        and _is_aware_datetime(expires_at)
        and expires_at > issued_at
    )


def _validate_persisted_backend_shape(row: Record) -> None:
    valid = _valid_backend_core_shape(
        speech_backend=row.get("speech_backend"),
        transport=row.get("transport"),
        room_name=row.get("room_name"),
        participant_identity=row.get("participant_identity"),
        media_grant_nonce_hash=row.get("media_grant_nonce_hash"),
        media_grant_issued_at=row.get("media_grant_issued_at"),
        media_grant_expires_at=row.get("media_grant_expires_at"),
        worker_rtc_grant_revision=row.get("worker_rtc_grant_revision"),
    )
    if row.get("speech_backend") == "llm_factory":
        consumed_at = row.get("media_grant_consumed_at")
        issued_at = row.get("media_grant_issued_at")
        valid = (
            valid
            and (
                consumed_at is None
                or (
                    _is_aware_datetime(consumed_at)
                    and _is_aware_datetime(issued_at)
                    and consumed_at >= issued_at
                )
            )
            and _valid_remote_worker_shape(row)
        )
    elif row.get("speech_backend") == "client_local":
        valid = valid and not any(
            row.get(field) is not None
            for field in (
                "worker_identity",
                "media_grant_consumed_at",
                "last_media_refresh_id",
                "worker_assignment_id",
                "worker_rtc_grant_issued_at",
                "worker_rtc_grant_expires_at",
            )
        )
    if not valid:
        raise RepositoryValidationError(
            "persisted voice session speech backend fields are inconsistent"
        )


def _same_session(row: Record, session: VoiceSessionCreate) -> bool:
    return all(
        (
            str(row["session_id"]) == session.session_id,
            str(row["user_id"]) == session.owner_id,
            str(row["activation_id"]) == session.activation_id,
            str(row["device_id"]) == session.device_id,
            str(row["device_kind"]) == session.device_kind,
            str(row["speech_backend"]) == session.speech_backend,
            str(row["transport"]) == session.transport,
            row["room_name"] == session.room_name,
            row["participant_identity"] == session.participant_identity,
            str(row["visible_chat_id"]) == session.visible_chat_id,
            str(row["owner_connection_generation"]) == session.owner_connection_generation,
            str(row["control_binding_id"]) == session.control_binding_id,
            row["control_binding_expires_at"] == session.control_binding_expires_at,
            row["lease_expires_at"] == session.lease_expires_at,
            (
                None
                if row["media_grant_nonce_hash"] is None
                else bytes(row["media_grant_nonce_hash"])
            )
            == session.media_grant_nonce_hash,
            row["media_grant_issued_at"] == session.media_grant_issued_at,
            row["media_grant_expires_at"] == session.media_grant_expires_at,
            row["started_at"] == session.started_at,
        )
    )


def _session(row: Record) -> VoiceSession:
    _validate_persisted_backend_shape(row)
    return VoiceSession(
        session_id=str(row["session_id"]),
        owner_id=str(row["user_id"]),
        activation_id=str(row["activation_id"]),
        device_id=str(row["device_id"]),
        device_kind=str(row["device_kind"]),
        speech_backend=str(row["speech_backend"]),
        transport=str(row["transport"]),
        visible_chat_id=str(row["visible_chat_id"]),
        chat_context_revision=int(row["chat_context_revision"]),
        state=VoiceSessionState(str(row["state"])),
        generation=int(row["generation"]),
        owner_connection_generation=str(row["owner_connection_generation"]),
        lease_expires_at=row["lease_expires_at"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        ended_at=row.get("ended_at"),
        end_reason=None if row.get("end_reason") is None else str(row["end_reason"]),
    )


def _required_session(row: Record | None, owner_id: str) -> VoiceSession:
    if row is None:
        raise PlaneError(
            "voice session owner or generation fence is stale",
            code="voice_session_state_conflict",
            metadata={"owner_id": owner_id},
        )
    return _session(row)


def _same_turn(row: Record, turn: VoiceTurnCreate) -> bool:
    return all(
        (
            str(row["turn_id"]) == turn.turn_id,
            str(row["client_turn_id"]) == turn.client_turn_id,
            str(row["session_id"]) == turn.session_id,
            int(row["session_generation"]) == turn.session_generation,
            int(row["media_grant_revision"]) == turn.media_grant_revision,
            str(row["user_id"]) == turn.owner_id,
            str(row["chat_id"]) == turn.chat_id,
            int(row["chat_context_revision"]) == turn.chat_context_revision,
            int(row["execution_base_render_revision"]) == turn.execution_base_render_revision,
            str(row["submission_id"]) == turn.submission_id,
            str(row["request_generation"]) == turn.request_generation,
        )
    )


def _turn(row: Record) -> VoiceTurn:
    return VoiceTurn(
        turn_id=str(row["turn_id"]),
        client_turn_id=str(row["client_turn_id"]),
        session_id=str(row["session_id"]),
        owner_id=str(row["user_id"]),
        chat_id=str(row["chat_id"]),
        submission_id=str(row["submission_id"]),
        request_generation=str(row["request_generation"]),
        state=VoiceTurnState(str(row["state"])),
        operation_id=None if row.get("operation_id") is None else str(row["operation_id"]),
        result_id=None if row.get("result_id") is None else str(row["result_id"]),
        terminal_kind=None if row.get("terminal_kind") is None else str(row["terminal_kind"]),
        accepted_at=row.get("accepted_at"),
        terminal_at=row.get("terminal_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = (
    "VoiceGuidanceObservation",
    "VoiceRepository",
    "VoiceSession",
    "VoiceSessionCreate",
    "VoiceSessionState",
    "VoiceTurn",
    "VoiceTurnCreate",
    "VoiceTurnState",
)
