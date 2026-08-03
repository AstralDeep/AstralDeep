"""Authenticated, bounded worker-pool control transport for Feature 065."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from websockets.asyncio.client import connect as WebsocketsConnect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidStatus

from .config import VoiceProfile, WorkerConfig
from .session import (
    AssignmentConflict,
    CapacityExceeded,
    ClosedSessionRace,
    ProtocolViolation,
    SessionBinding,
    SessionNotice,
    SessionSupervisor,
    WorkerRtcGrant,
)

MAX_CONTROL_FRAME_BYTES = 15 * 1024
MAX_SOCKET_QUEUE = 16
MAX_SESSION_FRAME_RATE = 120
FRAME_RATE_WINDOW_SECONDS = 1.0
REGISTRATION_TIMEOUT_SECONDS = 5.0
SEND_TIMEOUT_SECONDS = 5.0
OUTBOUND_NOTICE_QUEUE_SIZE = 128
CHALLENGE_MAX_LIFETIME_SECONDS = 30
CHALLENGE_CLOCK_SKEW_SECONDS = 5

CHALLENGE_NONCE_HEADER = "X-Astral-Voice-Challenge"
CHALLENGE_ISSUED_HEADER = "X-Astral-Voice-Challenge-Issued-At"
CHALLENGE_EXPIRES_HEADER = "X-Astral-Voice-Challenge-Expires-At"
WORKER_HEADER = "X-Astral-Voice-Worker"
NONCE_HEADER = "X-Astral-Voice-Nonce"
TIMESTAMP_HEADER = "X-Astral-Voice-Timestamp"
SIGNATURE_HEADER = "X-Astral-Voice-Signature"

_CHALLENGE_DOMAIN = b"astraldeep.voice.worker-control.challenge.v1"
_NONCE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

COORDINATOR_FRAME_TYPES = frozenset(
    {
        "worker_registered",
        "session_bind",
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
WORKER_FRAME_TYPES = frozenset(
    {
        "worker_register",
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
_STOP_SPEECH_REASONS = frozenset(
    {"barge_in", "user_stop", "terminal", "mute", "takeover", "end", "stale"}
)
_SPEECH_KINDS = frozenset(
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
_SINGLE_SPEECH_KINDS = _SPEECH_KINDS - {"result"}
_SPEECH_ROLES = frozenset({"single", "result_opening", "result_continuation"})
_MEDIA_STATES = frozenset(
    {
        "connecting",
        "listening",
        "speech_detected",
        "transcribing",
        "reconnecting",
        "failed",
        "ended",
    }
)
_NOTICE_KINDS = frozenset(
    {
        "worker_ready",
        "media_grant_applied",
        "session_context_applied",
        "recognition_started",
        "recognition_failed",
        "speech_started",
        "speech_finished",
        "speech_interrupted",
        "speech_failed",
        "media_state",
        "transcript_emitted",
    }
)
_NOTICE_METADATA_KEYS: dict[str, frozenset[str]] = {
    "worker_ready": frozenset(
        {
            "assignment_id",
            "worker_identity",
            "worker_rtc_grant_revision",
            "profile_ready",
        }
    ),
    "media_grant_applied": frozenset(
        {
            "refresh_id",
            "media_grant_revision",
            "client_participant_identity",
        }
    ),
    "session_context_applied": frozenset(
        {
            "media_grant_revision",
            "visible_chat_id",
            "chat_context_revision",
        }
    ),
    "recognition_started": frozenset(
        {
            "client_turn_id",
            "media_grant_revision",
            "visible_chat_id",
            "chat_context_revision",
        }
    ),
    "recognition_failed": frozenset({"client_turn_id"}),
    "speech_started": frozenset(
        {
            "announcement_sequence",
            "media_grant_revision",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "result_reserved_samples_after",
            "duration_ms",
        }
    ),
    "speech_finished": frozenset(
        {
            "announcement_sequence",
            "media_grant_revision",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "result_reserved_samples_after",
            "duration_ms",
        }
    ),
    "speech_interrupted": frozenset(
        {
            "announcement_sequence",
            "media_grant_revision",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "result_reserved_samples_after",
            "duration_ms",
        }
    ),
    "speech_failed": frozenset(
        {
            "announcement_sequence",
            "media_grant_revision",
            "turn_id",
            "kind",
            "quantum_role",
            "quantum_index",
            "result_reserved_samples_after",
            "duration_ms",
        }
    ),
    "media_state": frozenset({"state"}),
    "transcript_emitted": frozenset(
        {
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
            "chat_context_revision",
            "media_grant_revision",
            "final",
            "utf8_bytes",
            "text_digest_sha256",
            "proof_expires_at",
        }
    ),
}


class ChallengeError(RuntimeError):
    """A content-free worker challenge failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PoolConnectionError(RuntimeError):
    """A content-free transient pool connection failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SessionNoticeContext:
    """Bearer-free session snapshot retained in the outbound notice queue."""

    session_id: str
    generation: int
    assignment_id: str
    worker_identity: str
    worker_rtc_grant_revision: int
    media_grant_revision: int
    client_participant_identity: str
    visible_chat_id: str
    chat_context_revision: int

    @classmethod
    def from_binding(cls, binding: SessionBinding) -> SessionNoticeContext:
        return cls(
            session_id=binding.session_id,
            generation=binding.generation,
            assignment_id=binding.assignment_id,
            worker_identity=binding.worker_identity,
            worker_rtc_grant_revision=binding.worker_rtc_grant_revision,
            media_grant_revision=binding.media_grant_revision,
            client_participant_identity=binding.client_participant_identity,
            visible_chat_id=binding.visible_chat_id,
            chat_context_revision=binding.chat_context_revision,
        )


@dataclass(frozen=True, slots=True)
class _QueuedNotice:
    context: SessionNoticeContext
    notice: SessionNotice


@dataclass(frozen=True, slots=True)
class Challenge:
    """One short-lived HTTP-upgrade challenge, with no reusable credential."""

    nonce: str
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if _NONCE.fullmatch(self.nonce) is None:
            raise ChallengeError("invalid_challenge_nonce")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.issued_at, self.expires_at)
        ):
            raise ChallengeError("invalid_challenge_timestamp")
        lifetime = self.expires_at - self.issued_at
        if not 1 <= lifetime <= CHALLENGE_MAX_LIFETIME_SECONDS:
            raise ChallengeError("invalid_challenge_lifetime")

    def __repr__(self) -> str:
        return (
            "Challenge(nonce=<redacted>, issued_at=<redacted>, expires_at=<redacted>)"
        )


class ChallengeRequired(PoolConnectionError):
    """The server rejected an initial upgrade with a valid challenge."""

    def __init__(self, challenge: Challenge) -> None:
        self.challenge = challenge
        super().__init__("challenge_required")


class ChallengeReplayWindow:
    """Bound memory while refusing to reuse every still-live nonce."""

    def __init__(self, *, capacity: int = 64) -> None:
        if not 1 <= capacity <= 1_024:
            raise ValueError("invalid_challenge_window_capacity")
        self._capacity = capacity
        self._claimed: dict[str, int] = {}

    def claim(self, challenge: Challenge, *, now: int) -> None:
        if isinstance(now, bool) or not isinstance(now, int):
            raise ChallengeError("invalid_challenge_clock")
        if now < challenge.issued_at - CHALLENGE_CLOCK_SKEW_SECONDS:
            raise ChallengeError("challenge_not_yet_valid")
        if now > challenge.expires_at:
            raise ChallengeError("challenge_expired")
        expired = [nonce for nonce, expiry in self._claimed.items() if expiry < now]
        for nonce in expired:
            del self._claimed[nonce]
        if challenge.nonce in self._claimed:
            raise ChallengeError("challenge_replayed")
        if len(self._claimed) >= self._capacity:
            raise ChallengeError("challenge_window_full")
        self._claimed[challenge.nonce] = challenge.expires_at


def sign_challenge(
    secret: bytes,
    *,
    worker_identity: str,
    nonce: str,
    timestamp: int,
) -> str:
    """Return a domain-separated SHA-256 HMAC for one upgrade retry."""

    if not isinstance(secret, bytes) or not secret:
        raise ChallengeError("invalid_challenge_secret")
    if _OPAQUE_ID.fullmatch(worker_identity) is None:
        raise ChallengeError("invalid_worker_identity")
    if _NONCE.fullmatch(nonce) is None:
        raise ChallengeError("invalid_challenge_nonce")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ChallengeError("invalid_challenge_timestamp")
    canonical = b"\n".join(
        (
            _CHALLENGE_DOMAIN,
            worker_identity.encode("ascii"),
            nonce.encode("ascii"),
            str(timestamp).encode("ascii"),
        )
    )
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def build_challenge_response_headers(
    secret: bytes,
    worker_identity: str,
    challenge: Challenge,
    *,
    timestamp: int,
) -> dict[str, str]:
    """Build the only four authentication headers sent by the worker."""

    return {
        WORKER_HEADER: worker_identity,
        NONCE_HEADER: challenge.nonce,
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign_challenge(
            secret,
            worker_identity=worker_identity,
            nonce=challenge.nonce,
            timestamp=timestamp,
        ),
    }


def verify_challenge_response(
    secret: bytes,
    challenge: Challenge,
    headers: Mapping[str, str],
    *,
    expected_worker_identity: str,
    now: int,
) -> bool:
    """Constant-time verification helper shared with coordinator tests."""

    try:
        normalized = {name.lower(): value for name, value in headers.items()}
        identity = normalized[WORKER_HEADER.lower()]
        nonce = normalized[NONCE_HEADER.lower()]
        timestamp_text = normalized[TIMESTAMP_HEADER.lower()]
        signature = normalized[SIGNATURE_HEADER.lower()]
        if not all(
            isinstance(value, str)
            for value in (identity, nonce, timestamp_text, signature)
        ):
            return False
        timestamp = int(timestamp_text, 10)
    except (KeyError, TypeError, ValueError):
        return False
    if (
        identity != expected_worker_identity
        or nonce != challenge.nonce
        or not challenge.issued_at <= now <= challenge.expires_at
        or not challenge.issued_at <= timestamp <= challenge.expires_at
        or abs(timestamp - now) > CHALLENGE_CLOCK_SKEW_SECONDS
        or _SHA256.fullmatch(signature) is None
    ):
        return False
    try:
        expected = sign_challenge(
            secret,
            worker_identity=identity,
            nonce=nonce,
            timestamp=timestamp,
        )
    except ChallengeError:
        return False
    return hmac.compare_digest(expected, signature)


class PoolSocket(Protocol):
    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class PoolConnector(Protocol):
    async def open(self, url: str, *, headers: Mapping[str, str]) -> PoolSocket: ...


ConnectFactory = Callable[..., Awaitable[PoolSocket]]


class _NoRedirectConnect(WebsocketsConnect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


class WebsocketsPoolConnector:
    """Create one proxy-free, redirect-free, transport-bounded WebSocket."""

    def __init__(self, *, connect_factory: ConnectFactory | None = None) -> None:
        self._connect_factory = connect_factory or _NoRedirectConnect

    async def open(self, url: str, *, headers: Mapping[str, str]) -> PoolSocket:
        try:
            return await self._connect_factory(
                url,
                additional_headers=dict(headers),
                proxy=None,
                compression=None,
                open_timeout=5.0,
                ping_interval=10.0,
                ping_timeout=5.0,
                close_timeout=3.0,
                max_size=MAX_CONTROL_FRAME_BYTES,
                max_queue=MAX_SOCKET_QUEUE,
                write_limit=16 * 1024,
                user_agent_header=None,
            )
        except InvalidStatus as exc:
            if exc.response.status_code == 401:
                try:
                    challenge = Challenge(
                        nonce=_single_header(
                            exc.response.headers, CHALLENGE_NONCE_HEADER
                        ),
                        issued_at=int(
                            _single_header(
                                exc.response.headers, CHALLENGE_ISSUED_HEADER
                            ),
                            10,
                        ),
                        expires_at=int(
                            _single_header(
                                exc.response.headers, CHALLENGE_EXPIRES_HEADER
                            ),
                            10,
                        ),
                    )
                except (ChallengeError, KeyError, TypeError, ValueError):
                    raise ChallengeError("invalid_challenge_headers") from None
                raise ChallengeRequired(challenge) from None
            raise PoolConnectionError("upgrade_rejected") from None
        except (ChallengeError, ChallengeRequired):
            raise
        except Exception:
            raise PoolConnectionError("connection_failed") from None


def _single_header(headers: Mapping[str, str], name: str) -> str:
    try:
        value = headers[name]
    except Exception:
        raise KeyError(name) from None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("invalid_header")
    return value


def decode_control_frame(payload: str | bytes) -> dict[str, Any]:
    """Decode one strict JSON object after enforcing the byte ceiling."""

    if not isinstance(payload, str):
        raise ProtocolViolation("text_frame_required")
    try:
        encoded = payload.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ProtocolViolation("invalid_utf8") from None
    if len(encoded) > MAX_CONTROL_FRAME_BYTES:
        raise ProtocolViolation("frame_too_large")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: _raise_invalid_number(),
        )
    except ProtocolViolation:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ProtocolViolation("invalid_json") from None
    if not isinstance(value, dict):
        raise ProtocolViolation("object_frame_required")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ProtocolViolation("duplicate_json_key")
        value[name] = item
    return value


def _raise_invalid_number() -> None:
    raise ProtocolViolation("invalid_json_number")


class FrameRateLimiter:
    """Fixed-window admission with history bounded by the configured maximum."""

    def __init__(self, *, max_frames: int, window_seconds: float) -> None:
        if not 1 <= max_frames <= 10_000 or not 0 < window_seconds <= 60:
            raise ValueError("invalid_frame_rate_limit")
        self._max_frames = max_frames
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque(maxlen=max_frames)

    @property
    def retained_count(self) -> int:
        return len(self._timestamps)

    def check(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max_frames:
            raise ProtocolViolation("frame_rate_exceeded")
        self._timestamps.append(now)


def parse_session_bind(
    frame: Mapping[str, Any],
    *,
    expected_worker_identity: str,
    now: datetime,
) -> SessionBinding:
    """Validate an assignment and its nested purpose-bound RTC grant."""

    required = {
        "type",
        "schema_version",
        "message_id",
        "session_id",
        "generation",
        "sequence",
        "sent_at",
        "assignment_id",
        "room_name",
        "worker_identity",
        "transport",
        "media_grant_revision",
        "worker_rtc_grant_revision",
        "client_participant_identity",
        "grant_expires_at",
        "worker_rtc_grant",
        "visible_chat_id",
        "chat_context_revision",
        "profile",
    }
    _exact_keys(frame, required, "invalid_session_bind_fields")
    if frame["type"] != "session_bind" or frame["schema_version"] != "1":
        raise ProtocolViolation("invalid_session_bind_type")
    _uuid4(frame["message_id"], "invalid_message_id")
    session_id = _uuid4(frame["session_id"], "invalid_session_id")
    assignment_id = _uuid4(frame["assignment_id"], "invalid_assignment_id")
    _parse_timestamp(frame["sent_at"], "invalid_sent_at")
    generation = _bounded_int(frame["generation"], 1, 2**63 - 1, "invalid_generation")
    _bounded_int(frame["sequence"], 0, 2**63 - 1, "invalid_sequence")
    room_name = _opaque(frame["room_name"], "invalid_room_name")
    worker_identity = _opaque(frame["worker_identity"], "invalid_worker_identity")
    if worker_identity != expected_worker_identity:
        raise ProtocolViolation("worker_identity_mismatch")
    transport = frame["transport"]
    if transport not in {"livekit", "watch_pcm_websocket"}:
        raise ProtocolViolation("invalid_transport")
    media_revision = _bounded_int(
        frame["media_grant_revision"],
        1,
        2**63 - 1,
        "invalid_media_grant_revision",
    )
    worker_revision = _bounded_int(
        frame["worker_rtc_grant_revision"],
        1,
        2**63 - 1,
        "invalid_worker_rtc_grant_revision",
    )
    client_identity = _opaque(
        frame["client_participant_identity"],
        "invalid_client_participant_identity",
    )
    grant_expires_at = _parse_timestamp(
        frame["grant_expires_at"], "invalid_grant_expires_at"
    )
    if grant_expires_at <= now:
        raise ProtocolViolation("media_grant_expired")
    visible_chat_id = _uuid4(frame["visible_chat_id"], "invalid_visible_chat_id")
    chat_revision = _bounded_int(
        frame["chat_context_revision"],
        1,
        2**63 - 1,
        "invalid_chat_context_revision",
    )
    if frame["profile"] != VoiceProfile().to_dict():
        raise ProtocolViolation("profile_mismatch")

    grant_value = frame["worker_rtc_grant"]
    if not isinstance(grant_value, Mapping):
        raise ProtocolViolation("invalid_worker_rtc_grant")
    _exact_keys(
        grant_value,
        {
            "revision",
            "livekit_url",
            "join_token",
            "issued_at",
            "expires_at",
            "room_name",
            "worker_identity",
        },
        "invalid_worker_rtc_grant_fields",
    )
    grant_revision = _bounded_int(
        grant_value["revision"], 1, 2**63 - 1, "invalid_worker_grant_revision"
    )
    if grant_revision != worker_revision:
        raise ProtocolViolation("grant_revision_mismatch")
    grant_room = _opaque(grant_value["room_name"], "invalid_grant_room")
    if grant_room != room_name:
        raise ProtocolViolation("grant_room_mismatch")
    grant_worker = _opaque(grant_value["worker_identity"], "invalid_grant_worker")
    if grant_worker != worker_identity:
        raise ProtocolViolation("grant_worker_mismatch")
    livekit_url = _validate_livekit_url(grant_value["livekit_url"])
    join_token = grant_value["join_token"]
    if not isinstance(join_token, str) or not 32 <= len(join_token) <= 8_192:
        raise ProtocolViolation("invalid_worker_join_token")
    issued_at = _parse_timestamp(grant_value["issued_at"], "invalid_grant_issued_at")
    expires_at = _parse_timestamp(grant_value["expires_at"], "invalid_grant_expires_at")
    if expires_at != grant_expires_at:
        raise ProtocolViolation("grant_expiry_mismatch")
    if issued_at > now + timedelta(seconds=CHALLENGE_CLOCK_SKEW_SECONDS):
        raise ProtocolViolation("grant_not_yet_valid")
    if expires_at <= now:
        raise ProtocolViolation("grant_expired")
    if expires_at <= issued_at:
        raise ProtocolViolation("invalid_grant_lifetime")
    if expires_at - issued_at > timedelta(minutes=5):
        raise ProtocolViolation("grant_lifetime_exceeded")

    return SessionBinding(
        session_id=session_id,
        generation=generation,
        assignment_id=assignment_id,
        room_name=room_name,
        worker_identity=worker_identity,
        transport=transport,
        media_grant_revision=media_revision,
        worker_rtc_grant_revision=worker_revision,
        client_participant_identity=client_identity,
        grant_expires_at=grant_expires_at,
        worker_rtc_grant=WorkerRtcGrant(
            revision=grant_revision,
            livekit_url=livekit_url,
            join_token=join_token,
            issued_at=issued_at,
            expires_at=expires_at,
            room_name=grant_room,
            worker_identity=grant_worker,
        ),
        visible_chat_id=visible_chat_id,
        chat_context_revision=chat_revision,
    )


class PoolClient:
    """Own one challenge-authenticated multiplexed worker-pool connection."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        connector: PoolConnector | None = None,
        supervisor: SessionSupervisor | None = None,
        utcnow: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        notice_queue_size: int = OUTBOUND_NOTICE_QUEUE_SIZE,
    ) -> None:
        if not 1 <= notice_queue_size <= OUTBOUND_NOTICE_QUEUE_SIZE:
            raise ValueError("invalid_notice_queue_size")
        self.config = config
        self.connector = connector or WebsocketsPoolConnector()
        self.supervisor = supervisor or SessionSupervisor(
            max_sessions=config.max_sessions
        )
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or __import__("time").monotonic
        self._challenge_replay = ChallengeReplayWindow()
        self._receive_limiter = FrameRateLimiter(
            max_frames=MAX_SESSION_FRAME_RATE,
            window_seconds=FRAME_RATE_WINDOW_SECONDS,
        )
        self._session_receive_sequences: dict[tuple[str, int], int] = {}
        self._session_send_sequences: dict[tuple[str, int], int] = {}
        self._socket: PoolSocket | None = None
        self._send_lock = asyncio.Lock()
        self._session_sequence_lock = asyncio.Lock()
        self._outbound_notices: asyncio.Queue[_QueuedNotice] = asyncio.Queue(
            notice_queue_size
        )
        self._running = False

    def enqueue_session_notice(
        self, binding: SessionBinding, notice: SessionNotice
    ) -> bool:
        """Synchronously queue one content-free worker-control notification.

        RTC callbacks and the serialized media owner may call this method, but
        it performs no socket I/O and never retains transcript or speech text.
        Final transcript content is published only through LiveKit reliable
        data to the expected participant.
        """

        if not isinstance(notice, SessionNotice):
            raise ProtocolViolation("invalid_session_notice")
        if notice.text is not None or notice.kind == "final_transcript":
            return False
        if notice.kind not in _NOTICE_KINDS:
            return False
        if not isinstance(notice.metadata, Mapping):
            raise ProtocolViolation("invalid_session_notice_metadata")
        allowed = _NOTICE_METADATA_KEYS[notice.kind]
        metadata = {
            key: value
            for key, value in notice.metadata.items()
            if key in allowed and _notice_scalar(value)
        }
        sanitized = SessionNotice(
            notice.kind,
            reason=notice.reason,
            announcement_id=notice.announcement_id,
            language=notice.language,
            metadata=metadata,
        )
        queued = _QueuedNotice(
            context=SessionNoticeContext.from_binding(binding),
            notice=sanitized,
        )
        try:
            self._outbound_notices.put_nowait(queued)
        except asyncio.QueueFull:
            raise ProtocolViolation("outbound_notice_queue_full") from None
        return True

    async def run_connection(self) -> None:
        """Authenticate, register, process bounded frames, and always clean up."""

        if self._running:
            raise RuntimeError("pool_connection_already_running")
        self._running = True
        socket: PoolSocket | None = None
        heartbeat_task: asyncio.Task[None] | None = None
        outbound_task: asyncio.Task[None] | None = None
        receive_task: asyncio.Task[None] | None = None
        close_code = 1000
        close_reason = "normal"
        try:
            socket = await self._authenticate()
            self._socket = socket
            self._session_receive_sequences.clear()
            self._session_send_sequences.clear()
            self._clear_outbound_notices()
            await self._send(socket, self._worker_register_frame())
            registered_payload = await asyncio.wait_for(
                socket.recv(), timeout=REGISTRATION_TIMEOUT_SECONDS
            )
            registered = self._receive_frame(registered_payload)
            heartbeat_interval, connection_id = await self._accept_registration(
                registered
            )
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(socket, heartbeat_interval, connection_id),
                name="voice-worker-heartbeats",
            )
            outbound_task = asyncio.create_task(
                self._outbound_loop(socket),
                name="voice-worker-notices",
            )
            receive_task = asyncio.create_task(
                self._receive_loop(socket),
                name="voice-worker-control-receive",
            )
            done, _pending = await asyncio.wait(
                {heartbeat_task, outbound_task, receive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for completed in done:
                completed.result()
        except ProtocolViolation:
            close_code = 1008
            close_reason = "protocol_violation"
            raise
        except asyncio.TimeoutError:
            close_code = 1008
            close_reason = "registration_timeout"
            raise ProtocolViolation("registration_timeout") from None
        except asyncio.CancelledError:
            close_code = 1001
            close_reason = "worker_shutdown"
            raise
        except ConnectionClosed:
            close_code = 1011
            close_reason = "connection_lost"
            raise PoolConnectionError("connection_lost") from None
        except PoolConnectionError:
            close_code = 1011
            close_reason = "connection_failed"
            raise
        finally:
            background = [
                task
                for task in (heartbeat_task, outbound_task, receive_task)
                if task is not None
            ]
            for task in background:
                task.cancel()
            if background:
                await asyncio.gather(*background, return_exceptions=True)
            await self.supervisor.shutdown("control_connection_closed")
            self._clear_outbound_notices()
            if socket is not None:
                await _safe_close(socket, close_code, close_reason)
            self._socket = None
            self._running = False

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Reconnect transient losses with bounded backoff until shutdown."""

        backoff = 0.5
        while not stop.is_set():
            connection = asyncio.create_task(self.run_connection())
            stopping = asyncio.create_task(stop.wait())
            done, _pending = await asyncio.wait(
                {connection, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopping in done:
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
                return
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
            try:
                connection.result()
            except (ChallengeError, ProtocolViolation):
                raise
            except PoolConnectionError:
                pass
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, 5.0)

    async def _receive_loop(self, socket: PoolSocket) -> None:
        while True:
            try:
                payload = await socket.recv()
            except (EOFError, ConnectionClosedOK):
                return
            frame = self._receive_frame(payload)
            await self._dispatch_or_isolate(socket, frame)

    async def _dispatch_or_isolate(
        self,
        socket: PoolSocket,
        frame: dict[str, Any],
    ) -> None:
        """Keep an attributable session fault off the multiplexed transport."""

        try:
            await self._dispatch(frame)
        except ProtocolViolation:
            race = await self._isolate_session_violation(frame)
            if race is None:
                raise
            await self._send_session_payload(
                socket,
                race.session_id,
                race.generation,
                {
                    "type": "media_state",
                    "state": "failed",
                    "reason": "control_protocol_error",
                    "occurred_at": _format_timestamp(self._aware_now()),
                },
            )
            await self._prune_session_sequences()
        finally:
            if frame.get("type") == "session_bind":
                grant = frame.get("worker_rtc_grant")
                if isinstance(grant, dict) and "join_token" in grant:
                    grant["join_token"] = ""

    async def _isolate_session_violation(
        self,
        frame: Mapping[str, Any],
    ) -> ClosedSessionRace | None:
        """Return a terminal fence only for a safely attributable command.

        Decode, direction, rate-limit, registration, and malformed-base faults
        remain connection-fatal.  Never-bound non-bind commands also remain
        fatal because they cannot be tied to an authenticated assignment.
        """

        try:
            _validate_session_base(frame)
        except ProtocolViolation:
            return None
        frame_type = frame.get("type")
        allow_unbound = frame_type == "session_bind"
        media_grant_revision = 1
        if allow_unbound:
            if frame.get("worker_identity") != self.config.worker_identity:
                return None
            try:
                _uuid4(frame.get("assignment_id"), "invalid_assignment_id")
                media_grant_revision = _bounded_int(
                    frame.get("media_grant_revision"),
                    1,
                    2**63 - 1,
                    "invalid_media_grant_revision",
                )
                _bounded_int(
                    frame.get("worker_rtc_grant_revision"),
                    1,
                    2**63 - 1,
                    "invalid_worker_rtc_grant_revision",
                )
            except ProtocolViolation:
                return None
        race = await self.supervisor.reject_control_frame(
            session_id=frame["session_id"],
            generation=frame["generation"],
            media_grant_revision=media_grant_revision,
            allow_unbound=allow_unbound,
        )
        if race is None:
            return None
        key = (race.session_id, race.generation)
        received_sequence = frame["sequence"]
        expected = self._session_receive_sequences.get(key, 0)
        if received_sequence >= expected:
            self._session_receive_sequences[key] = received_sequence + 1
        return race

    async def _outbound_loop(self, socket: PoolSocket) -> None:
        while True:
            await self._send_next_notice(socket)

    async def _send_next_notice(self, socket: PoolSocket) -> None:
        """Send one queued content-free notice through the sequence fence."""

        queued = await self._outbound_notices.get()
        payload = self._notice_payload(queued)
        await self._send_session_payload(
            socket,
            queued.context.session_id,
            queued.context.generation,
            payload,
        )

    def _clear_outbound_notices(self) -> None:
        while not self._outbound_notices.empty():
            try:
                self._outbound_notices.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _authenticate(self) -> PoolSocket:
        try:
            unauthenticated = await self.connector.open(
                self.config.control_url, headers={}
            )
        except ChallengeRequired as exc:
            challenge = exc.challenge
        else:
            await _safe_close(unauthenticated, 1008, "challenge_required")
            raise ChallengeError("challenge_not_required")
        now = int(self._aware_now().timestamp())
        self._challenge_replay.claim(challenge, now=now)
        response_timestamp = max(now, challenge.issued_at)
        headers = build_challenge_response_headers(
            self.config.control_secret,
            self.config.worker_identity,
            challenge,
            timestamp=response_timestamp,
        )
        try:
            return await self.connector.open(self.config.control_url, headers=headers)
        except ChallengeRequired:
            raise ChallengeError("challenge_rejected") from None

    def _worker_register_frame(self) -> dict[str, Any]:
        return {
            "type": "worker_register",
            "schema_version": "1",
            "message_id": str(uuid4()),
            "sequence": 0,
            "sent_at": _format_timestamp(self._aware_now()),
            "worker_identity": self.config.worker_identity,
            "max_sessions": self.config.max_sessions,
            "runtime_closure_sha256": self.config.runtime_closure_sha256,
            "profile": self.config.profile.to_dict(),
        }

    def _receive_frame(self, payload: str | bytes) -> dict[str, Any]:
        self._receive_limiter.check(self._monotonic())
        frame = decode_control_frame(payload)
        frame_type = frame.get("type")
        if not isinstance(frame_type, str):
            raise ProtocolViolation("missing_frame_type")
        if frame_type in WORKER_FRAME_TYPES:
            raise ProtocolViolation("wrong_direction")
        if frame_type not in COORDINATOR_FRAME_TYPES:
            raise ProtocolViolation("unknown_frame_type")
        return frame

    async def _accept_registration(
        self, frame: Mapping[str, Any]
    ) -> tuple[int, str]:
        required = {
            "type",
            "schema_version",
            "message_id",
            "sequence",
            "sent_at",
            "worker_identity",
            "connection_id",
            "accepted_max_sessions",
            "heartbeat_interval_seconds",
            "registered_at",
        }
        _exact_keys(frame, required, "invalid_worker_registered_fields")
        if frame["type"] != "worker_registered" or frame["schema_version"] != "1":
            raise ProtocolViolation("registration_required")
        _uuid4(frame["message_id"], "invalid_message_id")
        connection_id = _uuid4(frame["connection_id"], "invalid_connection_id")
        _parse_timestamp(frame["sent_at"], "invalid_sent_at")
        _parse_timestamp(frame["registered_at"], "invalid_registered_at")
        if frame["worker_identity"] != self.config.worker_identity:
            raise ProtocolViolation("worker_identity_mismatch")
        sequence = _bounded_int(frame["sequence"], 0, 2**63 - 1, "invalid_sequence")
        if sequence != 0:
            raise ProtocolViolation("sequence_out_of_order")
        capacity = _bounded_int(
            frame["accepted_max_sessions"], 1, 100, "invalid_accepted_capacity"
        )
        await self.supervisor.set_capacity(capacity)
        return (
            _bounded_int(
                frame["heartbeat_interval_seconds"],
                5,
                60,
                "invalid_heartbeat_interval",
            ),
            connection_id,
        )

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        frame_type = frame["type"]
        if frame_type == "worker_registered":
            raise ProtocolViolation("duplicate_registration")
        if frame_type == "session_bind":
            binding = parse_session_bind(
                frame,
                expected_worker_identity=self.config.worker_identity,
                now=self._aware_now(),
            )
            try:
                self._check_session_sequence(frame)
                await self.supervisor.start(binding)
            except CapacityExceeded as exc:
                binding.clear_secrets()
                raise ProtocolViolation("capacity_exceeded") from exc
            except AssignmentConflict as exc:
                binding.clear_secrets()
                raise ProtocolViolation("assignment_conflict") from exc
            except BaseException:
                binding.clear_secrets()
                raise
            await self._prune_session_sequences()
            return
        if frame_type == "end_session":
            self._validate_end_session(frame)
            self._check_session_sequence(frame)
            await self.supervisor.end(
                frame["session_id"],
                frame["generation"],
                frame["media_grant_revision"],
                frame["reason"],
            )
            await self._prune_session_sequences()
            return
        validators: dict[str, Callable[[Mapping[str, Any]], None]] = {
            "set_capture": self._validate_set_capture,
            "session_context_update": self._validate_session_context_update,
            "speak": self._validate_speak,
            "stop_speech": self._validate_stop_speech,
            "media_grant_rotated": self._validate_media_grant_rotated,
            "turn_bound": self._validate_turn_bound,
            "transcript_accepted": self._validate_transcript_disposition,
            "transcript_rejected": self._validate_transcript_disposition,
        }
        validator = validators.get(frame_type)
        if validator is not None:
            validator(frame)
            self._check_session_sequence(frame)
            try:
                self.supervisor.deliver(frame)
            except ClosedSessionRace as race:
                await self._reconcile_closed_session(race)
                await self._prune_session_sequences()
            return
        _validate_session_base(frame)
        self._check_session_sequence(frame)
        raise ProtocolViolation("unsupported_frame_type")

    def _validate_set_capture(self, frame: Mapping[str, Any]) -> None:
        _exact_keys(
            frame,
            _session_keys("media_grant_revision", "enabled"),
            "invalid_set_capture_fields",
        )
        _validate_session_base(frame)
        _bounded_int(
            frame["media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_media_grant_revision",
        )
        if not isinstance(frame["enabled"], bool):
            raise ProtocolViolation("invalid_capture_state")

    def _validate_session_context_update(self, frame: Mapping[str, Any]) -> None:
        _exact_keys(
            frame,
            _session_keys(
                "media_grant_revision",
                "visible_chat_id",
                "chat_context_revision",
            ),
            "invalid_session_context_fields",
        )
        _validate_session_base(frame)
        _bounded_int(
            frame["media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_media_grant_revision",
        )
        _uuid4(frame["visible_chat_id"], "invalid_visible_chat_id")
        _bounded_int(
            frame["chat_context_revision"],
            1,
            2**63 - 1,
            "invalid_chat_context_revision",
        )

    def _validate_stop_speech(self, frame: Mapping[str, Any]) -> None:
        optional = {"announcement_id"} if "announcement_id" in frame else set()
        _exact_keys(
            frame,
            _session_keys("media_grant_revision", "reason") | optional,
            "invalid_stop_speech_fields",
        )
        _validate_session_base(frame)
        _bounded_int(
            frame["media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_media_grant_revision",
        )
        announcement_id = frame.get("announcement_id")
        if announcement_id is not None:
            _uuid4(announcement_id, "invalid_announcement_id")
        if frame["reason"] not in _STOP_SPEECH_REASONS:
            raise ProtocolViolation("invalid_stop_speech_reason")

    def _validate_turn_bound(self, frame: Mapping[str, Any]) -> None:
        _exact_keys(
            frame,
            _session_keys(
                "client_turn_id",
                "turn_id",
                "chat_id",
                "chat_context_revision",
                "media_grant_revision",
                "submission_id",
                "request_generation",
            ),
            "invalid_turn_bound_fields",
        )
        _validate_session_base(frame)
        for name in (
            "client_turn_id",
            "turn_id",
            "chat_id",
            "submission_id",
            "request_generation",
        ):
            _uuid4(frame[name], f"invalid_{name}")
        _bounded_int(
            frame["chat_context_revision"],
            1,
            2**63 - 1,
            "invalid_chat_context_revision",
        )
        _bounded_int(
            frame["media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_media_grant_revision",
        )

    def _validate_transcript_disposition(self, frame: Mapping[str, Any]) -> None:
        frame_type = frame.get("type")
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
        _exact_keys(
            frame,
            _session_keys(*expected),
            "invalid_transcript_disposition_fields",
        )
        _validate_session_base(frame)
        for name in (
            "turn_id",
            "client_turn_id",
            "submission_id",
            "request_generation",
            "chat_id",
        ):
            _uuid4(frame[name], f"invalid_{name}")
        _bounded_int(
            frame["media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_media_grant_revision",
        )
        if frame_type == "transcript_accepted":
            _bounded_int(
                frame["accepted_message_id"],
                1,
                2**63 - 1,
                "invalid_accepted_message_id",
            )
            return
        if frame_type != "transcript_rejected":
            raise ProtocolViolation("invalid_transcript_disposition_type")
        if frame["reason"] not in {
            "capacity_exhausted",
            "chat_unavailable",
            "invalid_binding",
            "invalid_proof",
            "proof_expired",
            "permission_denied",
            "stale_session",
            "malformed_final",
        }:
            raise ProtocolViolation("invalid_transcript_rejection_reason")
        if frame["retry_policy"] not in {"explicit_user_retry", "none"}:
            raise ProtocolViolation("invalid_transcript_retry_policy")

    def _validate_media_grant_rotated(self, frame: Mapping[str, Any]) -> None:
        _exact_keys(
            frame,
            _session_keys(
                "refresh_id",
                "previous_media_grant_revision",
                "media_grant_revision",
                "client_participant_identity",
                "transport",
                "grant_expires_at",
            ),
            "invalid_media_grant_rotated_fields",
        )
        _validate_session_base(frame)
        _uuid4(frame["refresh_id"], "invalid_refresh_id")
        previous = _bounded_int(
            frame["previous_media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_previous_media_grant_revision",
        )
        revision = _bounded_int(
            frame["media_grant_revision"],
            2,
            2**63 - 1,
            "invalid_media_grant_revision",
        )
        if revision != previous + 1:
            raise ProtocolViolation("invalid_media_grant_revision")
        _opaque(
            frame["client_participant_identity"],
            "invalid_client_participant_identity",
        )
        if frame["transport"] not in {"livekit", "watch_pcm_websocket"}:
            raise ProtocolViolation("invalid_transport")
        if (
            _parse_timestamp(frame["grant_expires_at"], "invalid_grant_expires_at")
            <= self._aware_now()
        ):
            raise ProtocolViolation("media_grant_expired")

    def _validate_speak(self, frame: Mapping[str, Any]) -> None:
        optional = {
            key
            for key in ("result_reserved_samples_after", "phrase_key")
            if key in frame
        }
        _exact_keys(
            frame,
            _session_keys(
                "announcement_id",
                "announcement_sequence",
                "media_grant_revision",
                "transport",
                "turn_id",
                "kind",
                "quantum_role",
                "quantum_index",
                "max_duration_samples",
                "text",
                "sensitive_authorized",
                "expires_at",
            )
            | optional,
            "invalid_speak_fields",
        )
        _validate_session_base(frame)
        _uuid4(frame["announcement_id"], "invalid_announcement_id")
        _bounded_int(
            frame["announcement_sequence"],
            1,
            2**63 - 1,
            "invalid_announcement_sequence",
        )
        _bounded_int(
            frame["media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_media_grant_revision",
        )
        if frame["transport"] not in {"livekit", "watch_pcm_websocket"}:
            raise ProtocolViolation("invalid_transport")
        kind = frame["kind"]
        if kind not in _SPEECH_KINDS:
            raise ProtocolViolation("invalid_speech_kind")
        turn_id = frame["turn_id"]
        if kind == "greeting":
            if turn_id is not None:
                raise ProtocolViolation("announcement_turn_mismatch")
        else:
            _uuid4(turn_id, "announcement_turn_mismatch")
        role = frame["quantum_role"]
        if role not in _SPEECH_ROLES:
            raise ProtocolViolation("invalid_speak_quantum")
        index = _bounded_int(frame["quantum_index"], 0, 31, "invalid_speak_quantum")
        ceiling = _bounded_int(
            frame["max_duration_samples"],
            1,
            96_000,
            "invalid_speak_sample_ceiling",
        )
        reserved = frame.get("result_reserved_samples_after")
        if role == "single":
            if kind not in _SINGLE_SPEECH_KINDS or index != 0 or reserved is not None:
                raise ProtocolViolation("invalid_speak_quantum")
        elif role == "result_opening":
            if (
                kind != "result"
                or index != 0
                or ceiling > 36_000
                or not _valid_int(reserved, 1, 36_000)
            ):
                raise ProtocolViolation("invalid_speak_quantum")
        elif (
            kind != "result"
            or not 1 <= index <= 31
            or not _valid_int(reserved, 1, 720_000)
        ):
            raise ProtocolViolation("invalid_speak_quantum")
        phrase_key = frame.get("phrase_key")
        if phrase_key is not None:
            _opaque(phrase_key, "invalid_phrase_key")
        text = frame["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 4_096:
            raise ProtocolViolation("invalid_speech_text")
        if not isinstance(frame["sensitive_authorized"], bool):
            raise ProtocolViolation("invalid_sensitive_authorization")
        expires_at = _parse_timestamp(frame["expires_at"], "invalid_expires_at")
        if expires_at <= self._aware_now():
            raise ProtocolViolation("speak_command_expired")

    def _validate_end_session(self, frame: Mapping[str, Any]) -> None:
        _exact_keys(
            frame,
            {
                "type",
                "schema_version",
                "message_id",
                "session_id",
                "generation",
                "sequence",
                "sent_at",
                "media_grant_revision",
                "reason",
            },
            "invalid_end_session_fields",
        )
        _validate_session_base(frame)
        _bounded_int(
            frame["media_grant_revision"],
            1,
            2**63 - 1,
            "invalid_media_grant_revision",
        )
        if frame["reason"] not in _END_REASONS:
            raise ProtocolViolation("invalid_end_reason")

    def _check_session_sequence(self, frame: Mapping[str, Any]) -> None:
        key = (frame["session_id"], frame["generation"])
        expected = self._session_receive_sequences.get(key, 0)
        if frame["sequence"] != expected:
            raise ProtocolViolation("sequence_out_of_order")
        self._session_receive_sequences[key] = expected + 1

    async def _heartbeat_loop(
        self,
        socket: PoolSocket,
        interval_seconds: int,
        connection_id: str,
    ) -> None:
        pool_sequence = 1
        while True:
            await asyncio.sleep(interval_seconds)
            await self._prune_session_sequences()
            await self._send(
                socket,
                {
                    "type": "pool_heartbeat",
                    "schema_version": "1",
                    "message_id": str(uuid4()),
                    "sequence": pool_sequence,
                    "sent_at": _format_timestamp(self._aware_now()),
                    "worker_identity": self.config.worker_identity,
                    "connection_id": connection_id,
                },
            )
            pool_sequence += 1
            for session_id, generation, media_state in self.supervisor.session_states():
                await self._send_session_payload(
                    socket,
                    session_id,
                    generation,
                    {"type": "heartbeat", "media_state": media_state},
                )

    async def _reconcile_closed_session(self, race: ClosedSessionRace) -> None:
        socket = self._socket
        if socket is None:
            raise PoolConnectionError("connection_lost")
        await self._send_session_payload(
            socket,
            race.session_id,
            race.generation,
            {
                "type": "media_state",
                "state": race.media_state,
                "reason": "session_closed",
                "occurred_at": _format_timestamp(self._aware_now()),
            },
        )

    async def _prune_session_sequences(self) -> None:
        retained = self.supervisor.retained_sequence_fences()
        for key in tuple(self._session_receive_sequences):
            if key not in retained:
                del self._session_receive_sequences[key]
        async with self._session_sequence_lock:
            for key in tuple(self._session_send_sequences):
                if key not in retained:
                    del self._session_send_sequences[key]

    async def _send_session_payload(
        self,
        socket: PoolSocket,
        session_id: str,
        generation: int,
        payload: Mapping[str, Any],
    ) -> None:
        key = (session_id, generation)
        async with self._session_sequence_lock:
            sequence = self._session_send_sequences.get(key, 0)
            frame = {
                "schema_version": "1",
                "message_id": str(uuid4()),
                "session_id": session_id,
                "generation": generation,
                "sequence": sequence,
                "sent_at": _format_timestamp(self._aware_now()),
                **payload,
            }
            await self._send(socket, frame)
            self._session_send_sequences[key] = sequence + 1

    def _notice_payload(self, queued: _QueuedNotice) -> dict[str, Any]:
        notice = queued.notice
        metadata = notice.metadata
        self._validate_notice_context(queued)
        now = _format_timestamp(self._aware_now())
        if notice.kind == "worker_ready":
            return {
                "type": "worker_ready",
                "assignment_id": _uuid4(
                    metadata.get("assignment_id"), "invalid_notice_assignment_id"
                ),
                "worker_identity": _opaque(
                    metadata.get("worker_identity"),
                    "invalid_notice_worker_identity",
                ),
                "worker_rtc_grant_revision": _bounded_int(
                    metadata.get("worker_rtc_grant_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_worker_grant_revision",
                ),
                "profile_ready": _required_bool(
                    metadata.get("profile_ready"), "invalid_notice_profile_ready"
                ),
            }
        if notice.kind == "session_context_applied":
            return {
                "type": notice.kind,
                "media_grant_revision": _bounded_int(
                    metadata.get("media_grant_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_media_grant_revision",
                ),
                "visible_chat_id": _uuid4(
                    metadata.get("visible_chat_id"), "invalid_notice_chat_id"
                ),
                "chat_context_revision": _bounded_int(
                    metadata.get("chat_context_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_chat_revision",
                ),
                "occurred_at": now,
            }
        if notice.kind == "media_grant_applied":
            return {
                "type": notice.kind,
                "refresh_id": _uuid4(
                    metadata.get("refresh_id"), "invalid_notice_refresh_id"
                ),
                "media_grant_revision": _bounded_int(
                    metadata.get("media_grant_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_media_grant_revision",
                ),
                "client_participant_identity": _opaque(
                    metadata.get("client_participant_identity"),
                    "invalid_notice_client_identity",
                ),
                "occurred_at": now,
            }
        if notice.kind == "recognition_started":
            return {
                "type": notice.kind,
                "client_turn_id": _uuid4(
                    metadata.get("client_turn_id"),
                    "invalid_notice_client_turn_id",
                ),
                "media_grant_revision": _bounded_int(
                    metadata.get("media_grant_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_media_grant_revision",
                ),
                "visible_chat_id": _uuid4(
                    metadata.get("visible_chat_id"), "invalid_notice_chat_id"
                ),
                "chat_context_revision": _bounded_int(
                    metadata.get("chat_context_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_chat_revision",
                ),
                "occurred_at": now,
            }
        if notice.kind == "recognition_failed":
            if notice.reason not in {
                "asr_failed",
                "empty_transcript",
                "invalid_asr_result",
                "self_speech",
            }:
                raise ProtocolViolation("invalid_notice_recognition_failure_reason")
            return {
                "type": notice.kind,
                "client_turn_id": _uuid4(
                    metadata.get("client_turn_id"),
                    "invalid_notice_client_turn_id",
                ),
                "reason": notice.reason,
                "occurred_at": now,
            }
        if notice.kind in {
            "speech_started",
            "speech_finished",
            "speech_interrupted",
            "speech_failed",
        }:
            return self._speech_notice_payload(notice, now)
        if notice.kind == "media_state":
            state = metadata.get("state")
            if state not in _MEDIA_STATES:
                raise ProtocolViolation("invalid_notice_media_state")
            payload: dict[str, Any] = {
                "type": notice.kind,
                "state": state,
                "occurred_at": now,
            }
            if notice.reason is not None:
                payload["reason"] = _reason(notice.reason, "invalid_notice_reason")
            return payload
        if notice.kind == "transcript_emitted":
            language = notice.language
            if (
                not isinstance(language, str)
                or not 2 <= len(language) <= 32
                or _LANGUAGE.fullmatch(language) is None
            ):
                raise ProtocolViolation("invalid_notice_language")
            if metadata.get("final") is not True:
                raise ProtocolViolation("invalid_notice_final")
            digest = metadata.get("text_digest_sha256")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                raise ProtocolViolation("invalid_notice_transcript_digest")
            proof_expiry = metadata.get("proof_expires_at")
            _parse_timestamp(proof_expiry, "invalid_notice_proof_expiry")
            return {
                "type": notice.kind,
                "turn_id": _uuid4(metadata.get("turn_id"), "invalid_notice_turn_id"),
                "client_turn_id": _uuid4(
                    metadata.get("client_turn_id"),
                    "invalid_notice_client_turn_id",
                ),
                "submission_id": _uuid4(
                    metadata.get("submission_id"),
                    "invalid_notice_submission_id",
                ),
                "request_generation": _uuid4(
                    metadata.get("request_generation"),
                    "invalid_notice_request_generation",
                ),
                "chat_id": _uuid4(metadata.get("chat_id"), "invalid_notice_chat_id"),
                "chat_context_revision": _bounded_int(
                    metadata.get("chat_context_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_chat_revision",
                ),
                "media_grant_revision": _bounded_int(
                    metadata.get("media_grant_revision"),
                    1,
                    2**63 - 1,
                    "invalid_notice_media_grant_revision",
                ),
                "final": _required_bool(metadata.get("final"), "invalid_notice_final"),
                "detected_language": language,
                "utf8_bytes": _bounded_int(
                    metadata.get("utf8_bytes"),
                    1,
                    12_000,
                    "invalid_notice_transcript_size",
                ),
                "text_digest_sha256": digest,
                "proof_expires_at": proof_expiry,
                "occurred_at": now,
            }
        raise ProtocolViolation("unsupported_session_notice")

    def _validate_notice_context(self, queued: _QueuedNotice) -> None:
        context = queued.context
        notice = queued.notice
        metadata = notice.metadata
        if (
            notice.kind != "transcript_emitted"
            and "media_grant_revision" in metadata
            and metadata["media_grant_revision"] != context.media_grant_revision
        ):
            raise ProtocolViolation("session_notice_binding_mismatch")
        if notice.kind in {"session_context_applied", "recognition_started"} and (
            metadata.get("visible_chat_id") != context.visible_chat_id
            or metadata.get("chat_context_revision") != context.chat_context_revision
        ):
            raise ProtocolViolation("session_notice_binding_mismatch")
        if notice.kind == "media_grant_applied" and (
            metadata.get("client_participant_identity")
            != context.client_participant_identity
        ):
            raise ProtocolViolation("session_notice_binding_mismatch")
        if notice.kind == "worker_ready" and (
            metadata.get("assignment_id") != context.assignment_id
            or metadata.get("worker_identity") != context.worker_identity
            or metadata.get("worker_rtc_grant_revision")
            != context.worker_rtc_grant_revision
        ):
            raise ProtocolViolation("session_notice_binding_mismatch")

    def _speech_notice_payload(
        self, notice: SessionNotice, occurred_at: str
    ) -> dict[str, Any]:
        metadata = notice.metadata
        announcement_id = _uuid4(
            notice.announcement_id, "invalid_notice_announcement_id"
        )
        kind = metadata.get("kind")
        role = metadata.get("quantum_role")
        if kind not in _SPEECH_KINDS or role not in _SPEECH_ROLES:
            raise ProtocolViolation("invalid_notice_speech_binding")
        turn_id = metadata.get("turn_id")
        if kind == "greeting":
            if turn_id is not None:
                raise ProtocolViolation("invalid_notice_speech_binding")
        else:
            turn_id = _uuid4(turn_id, "invalid_notice_turn_id")
        payload: dict[str, Any] = {
            "type": notice.kind,
            "announcement_id": announcement_id,
            "announcement_sequence": _bounded_int(
                metadata.get("announcement_sequence"),
                1,
                2**63 - 1,
                "invalid_notice_announcement_sequence",
            ),
            "media_grant_revision": _bounded_int(
                metadata.get("media_grant_revision"),
                1,
                2**63 - 1,
                "invalid_notice_media_grant_revision",
            ),
            "turn_id": turn_id,
            "kind": kind,
            "quantum_role": role,
            "quantum_index": _bounded_int(
                metadata.get("quantum_index"),
                0,
                31,
                "invalid_notice_quantum_index",
            ),
            "occurred_at": occurred_at,
        }
        reserved = metadata.get("result_reserved_samples_after")
        if reserved is not None:
            payload["result_reserved_samples_after"] = _bounded_int(
                reserved,
                1,
                720_000,
                "invalid_notice_result_reservation",
            )
        duration = metadata.get("duration_ms")
        if duration is not None:
            payload["duration_ms"] = _bounded_int(
                duration, 0, 4_000, "invalid_notice_duration"
            )
        if notice.reason is not None:
            payload["reason"] = _reason(notice.reason, "invalid_notice_speech_reason")
        return payload

    async def _send(self, socket: PoolSocket, frame: Mapping[str, Any]) -> None:
        try:
            payload = json.dumps(
                frame,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ProtocolViolation("invalid_outgoing_frame") from None
        if len(payload.encode("utf-8")) > MAX_CONTROL_FRAME_BYTES:
            raise ProtocolViolation("outgoing_frame_too_large")
        async with self._send_lock:
            try:
                await asyncio.wait_for(
                    socket.send(payload), timeout=SEND_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                raise PoolConnectionError("send_timeout") from None
            except Exception:
                raise PoolConnectionError("send_failed") from None

    def _aware_now(self) -> datetime:
        value = self._utcnow()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("worker_clock_must_be_timezone_aware")
        return value.astimezone(UTC)


def _validate_session_base(frame: Mapping[str, Any]) -> None:
    if frame.get("schema_version") != "1":
        raise ProtocolViolation("invalid_schema_version")
    _uuid4(frame.get("message_id"), "invalid_message_id")
    _uuid4(frame.get("session_id"), "invalid_session_id")
    _bounded_int(frame.get("generation"), 1, 2**63 - 1, "invalid_generation")
    _bounded_int(frame.get("sequence"), 0, 2**63 - 1, "invalid_sequence")
    _parse_timestamp(frame.get("sent_at"), "invalid_sent_at")


def _session_keys(*extra: str) -> set[str]:
    return {
        "type",
        "schema_version",
        "message_id",
        "session_id",
        "generation",
        "sequence",
        "sent_at",
        *extra,
    }


def _exact_keys(value: Mapping[str, Any], required: set[str], code: str) -> None:
    if set(value) != required:
        raise ProtocolViolation(code)


def _uuid4(value: Any, code: str) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        raise ProtocolViolation(code)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ProtocolViolation(code) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ProtocolViolation(code)
    return value


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolViolation(code)
    if not minimum <= value <= maximum:
        raise ProtocolViolation(code)
    return value


def _valid_int(value: Any, minimum: int, maximum: int) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and minimum <= value <= maximum
    )


def _required_bool(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolViolation(code)
    return value


def _opaque(value: Any, code: str) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise ProtocolViolation(code)
    return value


def _reason(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or re.fullmatch(r"[a-z0-9_]+", value) is None
    ):
        raise ProtocolViolation(code)
    return value


def _notice_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, bool))


def _parse_timestamp(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ProtocolViolation(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolViolation(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolViolation(code)
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_livekit_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise ProtocolViolation("invalid_livekit_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProtocolViolation("invalid_livekit_url") from exc
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ProtocolViolation("invalid_livekit_url")
    return value


async def _safe_close(socket: PoolSocket, code: int, reason: str) -> None:
    try:
        await asyncio.wait_for(socket.close(code=code, reason=reason), timeout=3.0)
    except (Exception, asyncio.CancelledError):
        return
