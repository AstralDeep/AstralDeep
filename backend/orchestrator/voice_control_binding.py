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
from typing import Callable, Mapping
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
