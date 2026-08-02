"""Short-lived purpose-bound tickets for the Feature-065 watch PCM bridge.

Tickets are signed capabilities, never authentication replacements.  Their
nonce is deterministically remintable during the bounded REST replay window,
while the worker consumes its digest once in memory before accepting audio.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


_DOMAIN_NONCE = b"astraldeep.voice.watch-bridge.nonce.v1\0"
_DOMAIN_TICKET = b"astraldeep.voice.watch-bridge.ticket.v1\0"
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class WatchTicketError(RuntimeError):
    """Content-free ticket refusal safe for a WebSocket close reason."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class WatchTicketClaims:
    session_id: str
    generation: int
    media_grant_revision: int
    worker_identity: str
    device_id: str
    connection_generation: str
    subject_digest_sha256: str
    issued_at: datetime
    expires_at: datetime
    nonce: bytes = field(repr=False)

    @property
    def nonce_hash(self) -> bytes:
        return hashlib.sha256(self.nonce).digest()

    def __repr__(self) -> str:
        return (
            "WatchTicketClaims("
            f"session_id={self.session_id!r}, generation={self.generation!r}, "
            f"media_grant_revision={self.media_grant_revision!r}, "
            f"worker_identity={self.worker_identity!r}, nonce=<redacted>)"
        )


def derive_watch_nonce(
    secret: bytes,
    *,
    user_id: str,
    session_key: str,
    generation: int,
    media_grant_revision: int,
    device_id: str,
    connection_generation: str,
) -> bytes:
    """Derive one replay-remintable nonce without retaining its bearer."""

    checked_secret = _secret(secret)
    fields = (
        _text(user_id, "invalid_user_id", 255),
        _uuid4(session_key, "invalid_session_key"),
        str(_positive(generation, "invalid_generation")),
        str(_positive(media_grant_revision, "invalid_media_grant_revision")),
        _uuid4(device_id, "invalid_device_id"),
        _uuid4(connection_generation, "invalid_connection_generation"),
    )
    message = b"\0".join(item.encode("utf-8") for item in fields)
    return hmac.new(checked_secret, _DOMAIN_NONCE + message, hashlib.sha256).digest()


def watch_participant_identity(nonce: bytes) -> str:
    checked = _nonce(nonce)
    return "watch-" + hashlib.sha256(checked).hexdigest()


def issue_watch_ticket(
    secret: bytes,
    *,
    user_id: str,
    session_id: str,
    generation: int,
    media_grant_revision: int,
    worker_identity: str,
    device_id: str,
    connection_generation: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: bytes,
) -> str:
    checked_secret = _secret(secret)
    issued = _aware(issued_at, "invalid_issued_at")
    expires = _aware(expires_at, "invalid_expires_at")
    if expires <= issued or expires - issued > timedelta(minutes=5):
        raise WatchTicketError("invalid_ticket_lifetime")
    payload = {
        "v": 1,
        "sid": _uuid4(session_id, "invalid_session_id"),
        "gen": _positive(generation, "invalid_generation"),
        "rev": _positive(media_grant_revision, "invalid_media_grant_revision"),
        "worker": _opaque(worker_identity, "invalid_worker_identity"),
        "device": _uuid4(device_id, "invalid_device_id"),
        "connection": _uuid4(
            connection_generation,
            "invalid_connection_generation",
        ),
        "sub": hashlib.sha256(
            _text(user_id, "invalid_user_id", 255).encode("utf-8")
        ).hexdigest(),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "nonce": _b64encode(_nonce(nonce)),
    }
    encoded = _b64encode(
        json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    )
    signature = hmac.new(
        checked_secret,
        _DOMAIN_TICKET + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"v1.{encoded}.{_b64encode(signature)}"


def verify_watch_ticket(
    ticket: str,
    secret: bytes,
    *,
    now: datetime,
    expected_worker_identity: str,
) -> WatchTicketClaims:
    checked_secret = _secret(secret)
    if not isinstance(ticket, str) or not 32 <= len(ticket) <= 8_192:
        raise WatchTicketError("invalid_ticket")
    parts = ticket.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise WatchTicketError("invalid_ticket")
    encoded, signature_text = parts[1], parts[2]
    try:
        supplied = _b64decode(signature_text)
    except WatchTicketError:
        raise WatchTicketError("invalid_ticket") from None
    expected = hmac.new(
        checked_secret,
        _DOMAIN_TICKET + encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise WatchTicketError("invalid_ticket")
    try:
        raw = _b64decode(encoded)
        if len(raw) > 2_048:
            raise WatchTicketError("invalid_ticket")
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, WatchTicketError):
        raise WatchTicketError("invalid_ticket") from None
    expected_keys = {
        "v",
        "sid",
        "gen",
        "rev",
        "worker",
        "device",
        "connection",
        "sub",
        "iat",
        "exp",
        "nonce",
    }
    if not isinstance(value, dict) or set(value) != expected_keys or value["v"] != 1:
        raise WatchTicketError("invalid_ticket")
    current = _aware(now, "invalid_current_time")
    issued = _epoch(value["iat"], "invalid_ticket")
    expires = _epoch(value["exp"], "invalid_ticket")
    if current < issued - timedelta(seconds=5) or current >= expires:
        raise WatchTicketError("ticket_expired")
    if expires <= issued or expires - issued > timedelta(minutes=5):
        raise WatchTicketError("invalid_ticket")
    worker = _opaque(value["worker"], "invalid_ticket")
    if worker != _opaque(expected_worker_identity, "invalid_worker_identity"):
        raise WatchTicketError("wrong_worker")
    subject_digest = value["sub"]
    if not isinstance(subject_digest, str) or _HEX64.fullmatch(subject_digest) is None:
        raise WatchTicketError("invalid_ticket")
    try:
        nonce = _b64decode(value["nonce"])
    except WatchTicketError:
        raise WatchTicketError("invalid_ticket") from None
    return WatchTicketClaims(
        session_id=_uuid4(value["sid"], "invalid_ticket"),
        generation=_positive(value["gen"], "invalid_ticket"),
        media_grant_revision=_positive(value["rev"], "invalid_ticket"),
        worker_identity=worker,
        device_id=_uuid4(value["device"], "invalid_ticket"),
        connection_generation=_uuid4(value["connection"], "invalid_ticket"),
        subject_digest_sha256=subject_digest,
        issued_at=issued,
        expires_at=expires,
        nonce=_nonce(nonce),
    )


def _secret(value: Any) -> bytes:
    if not isinstance(value, bytes) or not 32 <= len(value) <= 512:
        raise WatchTicketError("invalid_ticket_secret")
    return value


def _nonce(value: Any) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise WatchTicketError("invalid_ticket_nonce")
    return value


def _uuid4(value: Any, code: str) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        raise WatchTicketError(code)
    return value


def _opaque(value: Any, code: str) -> str:
    if not isinstance(value, str) or _OPAQUE.fullmatch(value) is None:
        raise WatchTicketError(code)
    return value


def _text(value: Any, code: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise WatchTicketError(code)
    return value


def _positive(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WatchTicketError(code)
    return value


def _aware(value: Any, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WatchTicketError(code)
    return value.astimezone(UTC)


def _epoch(value: Any, code: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WatchTicketError(code)
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise WatchTicketError(code) from None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: Any) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 8_192:
        raise WatchTicketError("invalid_ticket")
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise WatchTicketError("invalid_ticket")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise WatchTicketError("invalid_ticket") from None


__all__ = [
    "WatchTicketClaims",
    "WatchTicketError",
    "derive_watch_nonce",
    "issue_watch_ticket",
    "verify_watch_ticket",
    "watch_participant_identity",
]
