"""Short-lived UI voice-control bindings for Feature 065.

The bearer is minted only after an authenticated ``register_ui`` frame.  It
binds one Keycloak subject to one UUID4 device and one UUID4 connection
generation, and is never stored.  Durable voice-session rows retain only the
binding identifier and expiry so REST mutations can compare server state after
verifying this signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4


_AUDIENCE = "astraldeep.voice-control"
_DOMAIN = b"astraldeep.voice.control-binding.v1\x00"
_MAX_LIFETIME = timedelta(minutes=10)
_MAX_TOKEN_BYTES = 512
_SUBJECT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_PROCESS_DEVELOPMENT_KEY = secrets.token_bytes(32)


class VoiceControlBindingError(RuntimeError):
    """A content-free control-binding refusal safe for a client/log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ClientLocalTurnAuthority:
    """Memory-only authority for one bounded client-local recognition turn."""

    socket_id: int
    user_id: str
    device_id: str
    connection_generation: str
    binding_id: str
    session_id: str
    generation: int
    speech_revision: int
    client_turn_id: str
    turn_id: str
    submission_id: str
    request_generation: str
    chat_id: str
    chat_context_revision: int
    recognition_sequence: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClientLocalTurnReservation:
    """One content-free, bounded reservation made before durable insertion."""

    reservation_id: str
    socket_id: int
    user_id: str
    device_id: str
    connection_generation: str
    binding_id: str
    session_id: str
    generation: int
    speech_revision: int
    client_turn_id: str
    chat_id: str
    chat_context_revision: int
    recognition_sequence: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _ClientLocalSequenceFence:
    user_id: str
    device_id: str
    connection_generation: str
    session_id: str
    generation: int
    sequence: int
    expires_at: datetime


class ClientLocalBindingRegistry:
    """Bounded, process-local socket and turn fencing for local speech.

    The registry never treats a client capability claim as authority. Every
    admission rechecks the authenticated socket binding and durable session
    snapshot supplied by the server before it creates or returns a turn
    authority. No transcript content is stored; a digest exists only while an
    admitted final is in flight and is scrubbed with the turn.
    """

    def __init__(self, *, capacity: int = 256) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("invalid_local_binding_capacity")
        self._capacity = capacity
        self._sequences: dict[tuple[int, str, int], _ClientLocalSequenceFence] = {}
        self._reservations: dict[
            tuple[str, str], ClientLocalTurnReservation
        ] = {}
        self._turns: dict[tuple[str, str], ClientLocalTurnAuthority] = {}
        self._inflight_final_digests: dict[tuple[str, str], str] = {}

    @staticmethod
    def _authorize(
        *,
        socket_id: int,
        current_socket_id: int | None,
        user_id: str,
        claims: Any,
        session: Any,
        frame: Any,
        now: datetime,
    ) -> datetime:
        checked_now = _aware(now, "now")
        try:
            frame.validate()
            expires_at = min(
                _aware(claims.expires_at, "claims.expires_at"),
                _aware(session.control_binding_expires_at, "control_binding_expires_at"),
                _aware(session.lease_expires_at, "lease_expires_at"),
            )
            valid = (
                current_socket_id == socket_id
                and claims.subject == user_id == session.user_id
                and claims.device_id == frame.device_id == session.device_id
                and claims.connection_generation
                == frame.connection_generation
                == session.owner_connection_generation
                and claims.binding_id == session.control_binding_id
                and frame.session_id == session.session_id
                and frame.generation == session.generation
                and frame.speech_revision == session.media_grant_revision
                and session.speech_backend == "client_local"
                and session.state == "active"
                and session.foreground_active is True
                and session.microphone_enabled is True
                and session.speech_muted is False
                and session.applied_visible_chat_id == session.visible_chat_id
                and session.applied_chat_context_revision
                == session.chat_context_revision
                and expires_at > checked_now
            )
        except (AttributeError, TypeError, ValueError, VoiceControlBindingError):
            valid = False
            expires_at = checked_now
        if not valid:
            raise VoiceControlBindingError("invalid_binding")
        return expires_at

    def _advance_sequence(
        self,
        *,
        socket_id: int,
        session_id: str,
        generation: int,
        sequence: int,
        user_id: str,
        device_id: str,
        connection_generation: str,
        expires_at: datetime,
    ) -> None:
        key = (socket_id, session_id, generation)
        current = self._sequences.get(key)
        if current is None and len(self._sequences) >= self._capacity:
            raise VoiceControlBindingError("capacity_exhausted")
        if current is not None and sequence <= current.sequence:
            raise VoiceControlBindingError("invalid_binding")
        self._sequences[key] = _ClientLocalSequenceFence(
            user_id=user_id,
            device_id=device_id,
            connection_generation=connection_generation,
            session_id=session_id,
            generation=generation,
            sequence=sequence,
            expires_at=expires_at,
        )

    def authorize_ready(
        self,
        *,
        socket_id: int,
        current_socket_id: int | None,
        user_id: str,
        claims: Any,
        session: Any,
        frame: Any,
        now: datetime,
    ) -> None:
        self._prune(now)
        expires_at = self._authorize(
            socket_id=socket_id,
            current_socket_id=current_socket_id,
            user_id=user_id,
            claims=claims,
            session=session,
            frame=frame,
            now=now,
        )
        self._advance_sequence(
            socket_id=socket_id,
            session_id=frame.session_id,
            generation=frame.generation,
            sequence=frame.client_sequence,
            user_id=user_id,
            device_id=frame.device_id,
            connection_generation=frame.connection_generation,
            expires_at=expires_at,
        )

    def authorize_recognition_start(
        self,
        *,
        socket_id: int,
        current_socket_id: int | None,
        user_id: str,
        claims: Any,
        session: Any,
        frame: Any,
        now: datetime,
    ) -> datetime:
        self._prune(now)
        expires_at = self._authorize(
            socket_id=socket_id,
            current_socket_id=current_socket_id,
            user_id=user_id,
            claims=claims,
            session=session,
            frame=frame,
            now=now,
        )
        if (
            frame.chat_id != session.visible_chat_id
            or frame.chat_context_revision != session.chat_context_revision
        ):
            raise VoiceControlBindingError("invalid_binding")
        self._advance_sequence(
            socket_id=socket_id,
            session_id=frame.session_id,
            generation=frame.generation,
            sequence=frame.recognition_sequence,
            user_id=user_id,
            device_id=frame.device_id,
            connection_generation=frame.connection_generation,
            expires_at=expires_at,
        )
        return expires_at

    def reserve_turn(
        self,
        *,
        socket_id: int,
        current_socket_id: int | None,
        user_id: str,
        claims: Any,
        session: Any,
        frame: Any,
        now: datetime,
    ) -> ClientLocalTurnReservation | ClientLocalTurnAuthority:
        """Reserve ephemeral authority before a durable recognizing row exists."""

        self._prune(now)
        key = (user_id, frame.client_turn_id)
        existing = self._turns.get(key)
        if existing is not None:
            if self._matches_start(existing, socket_id=socket_id, frame=frame, now=now):
                return existing
            raise VoiceControlBindingError("invalid_binding")
        pending = self._reservations.get(key)
        if pending is not None:
            # A matching reservation is still owned by the first uncancellable
            # repository insert. A concurrent retry must not start a second
            # insert; it may retry after that exact reservation resolves.
            raise VoiceControlBindingError("invalid_binding")
        if len(self._turns) + len(self._reservations) >= self._capacity:
            raise VoiceControlBindingError("capacity_exhausted")
        authority_expiry = min(
            self.authorize_recognition_start(
                socket_id=socket_id,
                current_socket_id=current_socket_id,
                user_id=user_id,
                claims=claims,
                session=session,
                frame=frame,
                now=now,
            ),
            _aware(now, "now") + timedelta(minutes=2),
        )
        reservation = ClientLocalTurnReservation(
            reservation_id=str(uuid4()),
            socket_id=socket_id,
            user_id=user_id,
            device_id=frame.device_id,
            connection_generation=frame.connection_generation,
            binding_id=claims.binding_id,
            session_id=frame.session_id,
            generation=frame.generation,
            speech_revision=frame.speech_revision,
            client_turn_id=frame.client_turn_id,
            chat_id=frame.chat_id,
            chat_context_revision=frame.chat_context_revision,
            recognition_sequence=frame.recognition_sequence,
            expires_at=authority_expiry,
        )
        self._reservations[key] = reservation
        return reservation

    @staticmethod
    def _matches_start(authority: Any, *, socket_id: int, frame: Any, now: datetime) -> bool:
        return (
            authority.socket_id == socket_id
            and authority.session_id == frame.session_id
            and authority.generation == frame.generation
            and authority.speech_revision == frame.speech_revision
            and authority.chat_id == frame.chat_id
            and authority.chat_context_revision == frame.chat_context_revision
            and authority.recognition_sequence == frame.recognition_sequence
            and authority.expires_at > now
        )

    def finalize_turn(
        self,
        *,
        reservation: ClientLocalTurnReservation,
        turn: Any,
        now: datetime,
    ) -> ClientLocalTurnAuthority:
        """Convert the exact live reservation into final ephemeral authority."""

        self._prune(now)
        key = (reservation.user_id, reservation.client_turn_id)
        if self._reservations.get(key) != reservation or reservation.expires_at <= now:
            raise VoiceControlBindingError("invalid_binding")
        authority = ClientLocalTurnAuthority(
            socket_id=reservation.socket_id,
            user_id=reservation.user_id,
            device_id=reservation.device_id,
            connection_generation=reservation.connection_generation,
            binding_id=reservation.binding_id,
            session_id=reservation.session_id,
            generation=reservation.generation,
            speech_revision=reservation.speech_revision,
            client_turn_id=reservation.client_turn_id,
            turn_id=turn.turn_id,
            submission_id=turn.submission_id,
            request_generation=turn.request_generation,
            chat_id=reservation.chat_id,
            chat_context_revision=reservation.chat_context_revision,
            recognition_sequence=reservation.recognition_sequence,
            expires_at=reservation.expires_at,
        )
        self._reservations.pop(key, None)
        self._turns[key] = authority
        return authority

    def release_reservation(self, reservation: ClientLocalTurnReservation) -> None:
        key = (reservation.user_id, reservation.client_turn_id)
        if self._reservations.get(key) == reservation:
            self._reservations.pop(key, None)
            sequence_key = (
                reservation.socket_id,
                reservation.session_id,
                reservation.generation,
            )
            fence = self._sequences.get(sequence_key)
            if fence is not None and fence.sequence == reservation.recognition_sequence:
                self._sequences.pop(sequence_key, None)

    def bind_turn(
        self,
        *,
        socket_id: int,
        current_socket_id: int | None,
        user_id: str,
        claims: Any,
        session: Any,
        frame: Any,
        turn: Any,
        now: datetime,
    ) -> ClientLocalTurnAuthority:
        reserved = self.reserve_turn(
            socket_id=socket_id,
            current_socket_id=current_socket_id,
            user_id=user_id,
            claims=claims,
            session=session,
            frame=frame,
            now=now,
        )
        if isinstance(reserved, ClientLocalTurnAuthority):
            return reserved
        return self.finalize_turn(reservation=reserved, turn=turn, now=now)

    def get_turn(
        self,
        *,
        user_id: str,
        client_turn_id: str,
        now: datetime,
    ) -> ClientLocalTurnAuthority:
        self._prune(now)
        authority = self._turns.get((user_id, client_turn_id))
        if authority is None or authority.expires_at <= now:
            raise VoiceControlBindingError("invalid_binding")
        return authority

    def release_turn(self, *, user_id: str, client_turn_id: str) -> None:
        self._turns.pop((user_id, client_turn_id), None)
        self._inflight_final_digests.pop((user_id, client_turn_id), None)

    def release_request_authority(
        self,
        authority: ClientLocalTurnAuthority,
    ) -> None:
        """Release only authority and sequence created by one failed request."""

        key = (authority.user_id, authority.client_turn_id)
        if self._turns.get(key) is not authority:
            return
        self._turns.pop(key, None)
        self._inflight_final_digests.pop(key, None)
        sequence_key = (
            authority.socket_id,
            authority.session_id,
            authority.generation,
        )
        fence = self._sequences.get(sequence_key)
        if fence is not None and fence.sequence == authority.recognition_sequence:
            self._sequences.pop(sequence_key, None)

    def verify_final(
        self,
        *,
        socket_id: int,
        current_socket_id: int | None,
        user_id: str,
        frame: Any,
        now: datetime,
    ) -> tuple[str, bool]:
        """Return canonical text after exact in-flight turn verification."""

        from orchestrator.voice_sessions import canonicalize_local_transcript

        frame.validate()
        authority = self.get_turn(
            user_id=user_id,
            client_turn_id=frame.client_turn_id,
            now=now,
        )
        matches = (
            current_socket_id == socket_id == authority.socket_id
            and frame.device_id == authority.device_id
            and frame.connection_generation == authority.connection_generation
            and frame.session_id == authority.session_id
            and frame.generation == authority.generation
            and frame.speech_revision == authority.speech_revision
            and frame.turn_id == authority.turn_id
            and frame.submission_id == authority.submission_id
            and frame.request_generation == authority.request_generation
            and frame.chat_id == authority.chat_id
            and frame.chat_context_revision == authority.chat_context_revision
            and frame.recognition_sequence == authority.recognition_sequence
        )
        if not matches:
            raise VoiceControlBindingError("invalid_binding")
        canonical = canonicalize_local_transcript(
            frame.text,
            frame.text_digest_sha256,
        )
        key = (user_id, frame.client_turn_id)
        first_digest = self._inflight_final_digests.get(key)
        if first_digest is None:
            self._inflight_final_digests[key] = frame.text_digest_sha256
            return canonical, False
        if not hmac.compare_digest(first_digest, frame.text_digest_sha256):
            raise VoiceControlBindingError("altered_local_final")
        return canonical, True

    def verify_turn_frame(
        self,
        *,
        socket_id: int,
        current_socket_id: int | None,
        user_id: str,
        frame: Any,
        now: datetime,
    ) -> ClientLocalTurnAuthority:
        frame.validate()
        authority = self.get_turn(
            user_id=user_id,
            client_turn_id=frame.client_turn_id,
            now=now,
        )
        if not (
            current_socket_id == socket_id == authority.socket_id
            and frame.device_id == authority.device_id
            and frame.connection_generation == authority.connection_generation
            and frame.session_id == authority.session_id
            and frame.generation == authority.generation
            and frame.speech_revision == authority.speech_revision
            and frame.turn_id == authority.turn_id
            and frame.submission_id == authority.submission_id
            and frame.request_generation == authority.request_generation
            and frame.chat_id == authority.chat_id
            and frame.chat_context_revision == authority.chat_context_revision
            and frame.recognition_sequence == authority.recognition_sequence
        ):
            raise VoiceControlBindingError("invalid_binding")
        return authority

    def clear_connection(
        self,
        *,
        user_id: str,
        device_id: str,
        connection_generation: str,
        socket_id: int | None = None,
    ) -> None:
        def matches(authority: Any) -> bool:
            return (
                authority.user_id == user_id
                and authority.device_id == device_id
                and authority.connection_generation == connection_generation
                and (socket_id is None or authority.socket_id == socket_id)
            )

        self._turns = {
            key: authority
            for key, authority in self._turns.items()
            if not matches(authority)
        }
        self._reservations = {
            key: reservation
            for key, reservation in self._reservations.items()
            if not matches(reservation)
        }
        self._inflight_final_digests = {
            key: digest
            for key, digest in self._inflight_final_digests.items()
            if key in self._turns
        }
        self._sequences = {
            key: value
            for key, value in self._sequences.items()
            if not (
                value.user_id == user_id
                and value.device_id == device_id
                and value.connection_generation == connection_generation
                and (socket_id is None or key[0] == socket_id)
            )
        }

    def clear_session(
        self,
        *,
        session_id: str,
        generation: int,
        user_id: str | None = None,
    ) -> None:
        def matches(authority: Any) -> bool:
            return (
                authority.session_id == session_id
                and authority.generation == generation
                and (user_id is None or authority.user_id == user_id)
            )

        self._turns = {
            key: authority
            for key, authority in self._turns.items()
            if not matches(authority)
        }
        self._reservations = {
            key: reservation
            for key, reservation in self._reservations.items()
            if not matches(reservation)
        }
        self._inflight_final_digests = {
            key: digest
            for key, digest in self._inflight_final_digests.items()
            if key in self._turns
        }
        self._sequences = {
            key: value
            for key, value in self._sequences.items()
            if not (
                value.session_id == session_id
                and value.generation == generation
                and (user_id is None or value.user_id == user_id)
            )
        }

    def _prune(self, now: datetime) -> None:
        checked_now = _aware(now, "now")
        self._turns = {
            key: authority
            for key, authority in self._turns.items()
            if authority.expires_at > checked_now
        }
        self._reservations = {
            key: reservation
            for key, reservation in self._reservations.items()
            if reservation.expires_at > checked_now
        }
        self._sequences = {
            key: fence
            for key, fence in self._sequences.items()
            if fence.expires_at > checked_now
        }
        self._inflight_final_digests = {
            key: digest
            for key, digest in self._inflight_final_digests.items()
            if key in self._turns
        }


@dataclass(frozen=True, slots=True)
class VoiceControlClaims:
    """Non-secret signed scope retained by a socket/session row."""

    subject: str
    device_id: str
    connection_generation: str
    binding_id: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _subject(self.subject)
        for name in ("device_id", "connection_generation", "binding_id"):
            _uuid4(getattr(self, name), name)
        issued = _aware(self.issued_at, "issued_at")
        expires = _aware(self.expires_at, "expires_at")
        if expires <= issued or expires - issued > _MAX_LIFETIME:
            raise VoiceControlBindingError("invalid_binding_lifetime")


@dataclass(frozen=True, slots=True, repr=False)
class IssuedVoiceControlBinding:
    """One bearer delivery plus its safe server-side scope."""

    bearer: str
    claims: VoiceControlClaims

    def __repr__(self) -> str:
        return f"IssuedVoiceControlBinding(bearer=<redacted>, claims={self.claims!r})"


class VoiceControlBindingIssuer:
    """Mint and verify compact, domain-separated HMAC control bearers."""

    def __init__(
        self,
        secret: bytes,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise VoiceControlBindingError("weak_binding_secret")
        self._secret = bytes(secret)
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> "VoiceControlBindingIssuer":
        values = environ if environ is not None else os.environ
        environment = values.get("ASTRAL_ENV", "").strip().lower() or "production"
        configured = (
            values.get("VOICE_UI_BINDING_SECRET", "").strip()
            or values.get("MEMORY_HMAC_KEY", "").strip()
        )
        if not configured:
            if environment not in {"development", "dev", "test"}:
                raise VoiceControlBindingError("missing_binding_secret")
            secret = _PROCESS_DEVELOPMENT_KEY
        else:
            secret = configured.encode("utf-8")
        return cls(secret, clock=clock)

    def mint(
        self,
        *,
        subject: str,
        device_id: str,
        connection_generation: str,
        credential_expires_at: datetime,
    ) -> IssuedVoiceControlBinding:
        """Mint one bearer that never outlives the authenticated credential."""

        checked_subject = _subject(subject)
        checked_device = _uuid4(device_id, "device_id")
        checked_connection = _uuid4(
            connection_generation,
            "connection_generation",
        )
        # The compact bearer serializes NumericDate values as whole seconds.
        # Retain claims at that same precision so the socket-side current-
        # binding fence compares equal to claims reconstructed by ``verify``.
        # Without this normalization, ordinary real clocks (which nearly
        # always include microseconds) make every newly minted binding appear
        # stale even though its signature and scope are valid.
        now = _aware(self._clock(), "clock").replace(microsecond=0)
        credential_expiry = _aware(
            credential_expires_at,
            "credential_expires_at",
        ).replace(microsecond=0)
        expires = min(now + _MAX_LIFETIME, credential_expiry)
        if expires <= now:
            raise VoiceControlBindingError("credential_expired")
        claims = VoiceControlClaims(
            subject=checked_subject,
            device_id=checked_device,
            connection_generation=checked_connection,
            binding_id=str(uuid4()),
            issued_at=now,
            expires_at=expires,
        )
        payload = _canonical_payload(claims)
        encoded = _b64url(payload)
        signature = _b64url(hmac.new(self._secret, _DOMAIN + payload, hashlib.sha256).digest())
        bearer = f"v1.{encoded}.{signature}"
        if len(bearer.encode("ascii")) > _MAX_TOKEN_BYTES:
            raise VoiceControlBindingError("binding_token_too_large")
        return IssuedVoiceControlBinding(bearer=bearer, claims=claims)

    def verify(
        self,
        bearer: str,
        *,
        expected_subject: str,
        expected_device_id: str,
        expected_connection_generation: str,
        expected_binding_id: str | None = None,
        expected_expires_at: datetime | None = None,
    ) -> VoiceControlClaims:
        """Verify signature, lifetime, and every caller/server scope fence."""

        if (
            not isinstance(bearer, str)
            or not 32 <= len(bearer.encode("utf-8")) <= _MAX_TOKEN_BYTES
        ):
            raise VoiceControlBindingError("invalid_binding")
        try:
            version, encoded, encoded_signature = bearer.split(".")
            if version != "v1":
                raise ValueError
            payload = _b64url_decode(encoded)
            signature = _b64url_decode(encoded_signature)
        except (UnicodeError, ValueError):
            raise VoiceControlBindingError("invalid_binding") from None
        expected_signature = hmac.new(
            self._secret,
            _DOMAIN + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise VoiceControlBindingError("invalid_binding")
        claims = _parse_payload(payload)
        now = _aware(self._clock(), "clock")
        if claims.expires_at <= now:
            raise VoiceControlBindingError("binding_expired")
        if claims.issued_at > now + timedelta(seconds=5):
            raise VoiceControlBindingError("binding_not_yet_valid")
        if (
            claims.subject != _subject(expected_subject)
            or claims.device_id != _uuid4(expected_device_id, "device_id")
            or claims.connection_generation
            != _uuid4(expected_connection_generation, "connection_generation")
            or (
                expected_binding_id is not None
                and claims.binding_id != _uuid4(expected_binding_id, "binding_id")
            )
        ):
            raise VoiceControlBindingError("binding_scope_mismatch")
        if expected_expires_at is not None and claims.expires_at != _aware(
            expected_expires_at,
            "expected_expires_at",
        ):
            raise VoiceControlBindingError("binding_scope_mismatch")
        return claims

    def __repr__(self) -> str:
        return "VoiceControlBindingIssuer(secret=<redacted>)"


def _canonical_payload(claims: VoiceControlClaims) -> bytes:
    return json.dumps(
        {
            "aud": _AUDIENCE,
            "binding_id": claims.binding_id,
            "connection_generation": claims.connection_generation,
            "device_id": claims.device_id,
            "exp": int(claims.expires_at.timestamp()),
            "iat": int(claims.issued_at.timestamp()),
            "sub": claims.subject,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _parse_payload(payload: bytes) -> VoiceControlClaims:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: _invalid_json(),
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise VoiceControlBindingError("invalid_binding") from None
    expected = {
        "aud",
        "binding_id",
        "connection_generation",
        "device_id",
        "exp",
        "iat",
        "sub",
    }
    if not isinstance(value, dict) or set(value) != expected or value["aud"] != _AUDIENCE:
        raise VoiceControlBindingError("invalid_binding")
    if any(
        isinstance(value[name], bool) or not isinstance(value[name], int)
        for name in ("iat", "exp")
    ):
        raise VoiceControlBindingError("invalid_binding")
    try:
        issued = datetime.fromtimestamp(value["iat"], tz=UTC)
        expires = datetime.fromtimestamp(value["exp"], tz=UTC)
        return VoiceControlClaims(
            subject=value["sub"],
            device_id=value["device_id"],
            connection_generation=value["connection_generation"],
            binding_id=value["binding_id"],
            issued_at=issued,
            expires_at=expires,
        )
    except (KeyError, OSError, OverflowError, TypeError, ValueError, VoiceControlBindingError):
        raise VoiceControlBindingError("invalid_binding") from None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate")
        value[name] = item
    return value


def _invalid_json() -> None:
    raise ValueError("invalid")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("invalid")
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )
    if _b64url(decoded) != value:
        raise ValueError("invalid")
    return decoded


def _subject(value: object) -> str:
    if not isinstance(value, str) or _SUBJECT.fullmatch(value) is None:
        raise VoiceControlBindingError("invalid_binding_subject")
    return value


def _uuid4(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise VoiceControlBindingError(f"invalid_{field_name}")
    try:
        parsed = UUID(value)
    except ValueError:
        raise VoiceControlBindingError(f"invalid_{field_name}") from None
    if parsed.version != 4 or str(parsed) != value:
        raise VoiceControlBindingError(f"invalid_{field_name}")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise VoiceControlBindingError(f"invalid_{field_name}")
    return value.astimezone(UTC)
