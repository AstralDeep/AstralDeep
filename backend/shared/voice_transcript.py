"""Canonical, short-lived transcript proofs for conversational voice.

The worker and orchestrator derive a per-session key from the worker-control
secret.  The proof authenticates immutable recognition/turn bindings and the
canonical text digest; neither key, proof, nor digest is durable conversation
content.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_KEY_DOMAIN = b"astraldeep.voice.transcript.session-key.v1\x00"
_PROOF_DOMAIN = b"astraldeep.voice.transcript.proof.v1\x00"
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_LOWER_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_CHARS = 8_000
_MAX_TEXT_BYTES = 24_000
_MAX_PROOF_LIFETIME = timedelta(minutes=2)


class TranscriptProofError(RuntimeError):
    """Content-free transcript refusal safe for logs and client errors."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TranscriptSessionScope:
    """Immutable worker assignment scope used to derive one session key."""

    session_id: str
    generation: int
    assignment_id: str
    worker_identity: str

    def __post_init__(self) -> None:
        for name in ("session_id", "assignment_id"):
            _uuid4(getattr(self, name), name)
        _positive(self.generation, "generation")
        if (
            not isinstance(self.worker_identity, str)
            or _OPAQUE.fullmatch(self.worker_identity) is None
        ):
            raise TranscriptProofError("invalid_worker_identity")


@dataclass(frozen=True, slots=True)
class TranscriptProofBinding:
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
    detected_language: str

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
            _uuid4(getattr(self, name), name)
        for name in (
            "generation",
            "media_grant_revision",
            "chat_context_revision",
        ):
            _positive(getattr(self, name), name)
        if (
            not isinstance(self.worker_identity, str)
            or _OPAQUE.fullmatch(self.worker_identity) is None
        ):
            raise TranscriptProofError("invalid_worker_identity")
        if (
            not isinstance(self.detected_language, str)
            or _LANGUAGE.fullmatch(self.detected_language) is None
        ):
            raise TranscriptProofError("invalid_transcript_language")

    @property
    def session_scope(self) -> TranscriptSessionScope:
        """Return the assignment-only scope without transcript identifiers."""

        return TranscriptSessionScope(
            session_id=self.session_id,
            generation=self.generation,
            assignment_id=self.assignment_id,
            worker_identity=self.worker_identity,
        )


@dataclass(frozen=True, slots=True, repr=False)
class IssuedTranscriptProof:
    canonical_text: str
    text_digest_sha256: str
    transcript_proof: str
    proof_expires_at: str

    def __repr__(self) -> str:
        return (
            "IssuedTranscriptProof(canonical_text=<redacted>, "
            "text_digest_sha256=<redacted>, transcript_proof=<redacted>, "
            f"proof_expires_at={self.proof_expires_at!r})"
        )


def canonical_transcript(text: object) -> str:
    """Normalize ASR text exactly and reject control/surrogate ambiguity."""

    if not isinstance(text, str):
        raise TranscriptProofError("invalid_transcript_text")
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n")).strip()
    if not normalized:
        raise TranscriptProofError("empty_transcript")
    if len(normalized) > _MAX_TEXT_CHARS:
        raise TranscriptProofError("transcript_text_too_large")
    for character in normalized:
        codepoint = ord(character)
        if (
            character == "\r"
            or (codepoint < 32 and character not in {"\t", "\n"})
            or codepoint == 127
            or unicodedata.category(character) == "Cs"
        ):
            raise TranscriptProofError("invalid_transcript_text")
    encoded = normalized.encode("utf-8", errors="strict")
    if len(encoded) > _MAX_TEXT_BYTES:
        raise TranscriptProofError("transcript_text_too_large")
    return normalized


def derive_session_proof_key(
    worker_control_secret: bytes,
    binding: TranscriptProofBinding | TranscriptSessionScope,
) -> bytes:
    """Derive one non-exported key bound to the exact worker assignment."""

    secret = _secret(worker_control_secret)
    scope = _lines(
        "ADVSK1",
        binding.session_id,
        binding.generation,
        binding.assignment_id,
        binding.worker_identity,
    )
    return hmac.new(secret, _KEY_DOMAIN + scope, hashlib.sha256).digest()


def issue_transcript_proof_with_key(
    session_proof_key: bytes,
    binding: TranscriptProofBinding,
    text: object,
    *,
    now: datetime,
    lifetime_seconds: int = 120,
) -> IssuedTranscriptProof:
    """Issue a proof from an already derived, memory-only session key."""

    if (
        isinstance(lifetime_seconds, bool)
        or not isinstance(lifetime_seconds, int)
        or not 1 <= lifetime_seconds <= 120
    ):
        raise TranscriptProofError("invalid_transcript_proof_lifetime")
    issued_at = _aware(now, "invalid_transcript_proof_clock")
    canonical = canonical_transcript(text)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    expires_at = issued_at + timedelta(seconds=lifetime_seconds)
    expires_text = _timestamp(expires_at)
    proof = _proof_with_key(session_proof_key, binding, digest, expires_text)
    return IssuedTranscriptProof(
        canonical_text=canonical,
        text_digest_sha256=digest,
        transcript_proof=proof,
        proof_expires_at=expires_text,
    )


def issue_transcript_proof(
    worker_control_secret: bytes,
    binding: TranscriptProofBinding,
    text: object,
    *,
    now: datetime,
    lifetime_seconds: int = 120,
) -> IssuedTranscriptProof:
    """Canonicalize and sign one final transcript for at most two minutes."""

    return issue_transcript_proof_with_key(
        derive_session_proof_key(worker_control_secret, binding),
        binding,
        text,
        now=now,
        lifetime_seconds=lifetime_seconds,
    )


def verify_transcript_proof_with_key(
    session_proof_key: bytes,
    binding: TranscriptProofBinding,
    text: object,
    *,
    text_digest_sha256: object,
    transcript_proof: object,
    proof_expires_at: object,
    now: datetime,
) -> str:
    """Verify one proof from an already derived session key."""

    checked_now = _aware(now, "invalid_transcript_proof_clock")
    canonical = canonical_transcript(text)
    if canonical != text:
        raise TranscriptProofError("noncanonical_transcript")
    if (
        not isinstance(text_digest_sha256, str)
        or _LOWER_HEX.fullmatch(text_digest_sha256) is None
    ):
        raise TranscriptProofError("invalid_transcript_digest")
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(text_digest_sha256, expected_digest):
        raise TranscriptProofError("transcript_digest_mismatch")
    if (
        not isinstance(transcript_proof, str)
        or _LOWER_HEX.fullmatch(transcript_proof) is None
    ):
        raise TranscriptProofError("invalid_transcript_proof")
    if not isinstance(proof_expires_at, str):
        raise TranscriptProofError("invalid_transcript_proof_expiry")
    expiry = _parse_timestamp(proof_expires_at)
    if _timestamp(expiry) != proof_expires_at:
        raise TranscriptProofError("invalid_transcript_proof_expiry")
    if expiry <= checked_now:
        raise TranscriptProofError("transcript_proof_expired")
    if expiry > checked_now + _MAX_PROOF_LIFETIME:
        raise TranscriptProofError("transcript_proof_lifetime_exceeded")
    expected_proof = _proof_with_key(
        session_proof_key,
        binding,
        text_digest_sha256,
        proof_expires_at,
    )
    if not hmac.compare_digest(transcript_proof, expected_proof):
        raise TranscriptProofError("transcript_proof_mismatch")
    return canonical


def verify_transcript_proof(
    worker_control_secret: bytes,
    binding: TranscriptProofBinding,
    text: object,
    *,
    text_digest_sha256: object,
    transcript_proof: object,
    proof_expires_at: object,
    now: datetime,
) -> str:
    """Verify canonical text, digest, expiry, and HMAC in constant time."""

    return verify_transcript_proof_with_key(
        derive_session_proof_key(worker_control_secret, binding),
        binding,
        text,
        text_digest_sha256=text_digest_sha256,
        transcript_proof=transcript_proof,
        proof_expires_at=proof_expires_at,
        now=now,
    )


def _proof_with_key(
    session_proof_key: bytes,
    binding: TranscriptProofBinding,
    digest: str,
    proof_expires_at: str,
) -> str:
    key = _session_key(session_proof_key)
    payload = _lines(
        "ADVT1",
        binding.session_id,
        binding.generation,
        binding.media_grant_revision,
        binding.turn_id,
        binding.client_turn_id,
        binding.submission_id,
        binding.request_generation,
        binding.chat_id,
        binding.chat_context_revision,
        binding.worker_identity,
        binding.detected_language,
        digest,
        proof_expires_at,
    )
    return hmac.new(key, _PROOF_DOMAIN + payload, hashlib.sha256).hexdigest()


def _lines(*values: object) -> bytes:
    return ("\n".join(str(value) for value in values) + "\n").encode("ascii")


def _secret(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise TranscriptProofError("invalid_worker_control_secret")
    return bytes(value)


def _session_key(value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) != hashlib.sha256().digest_size:
        raise TranscriptProofError("invalid_transcript_session_key")
    return bytes(value)


def _uuid4(value: object, name: str) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        raise TranscriptProofError(f"invalid_{name}")
    return value


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TranscriptProofError(f"invalid_{name}")
    return value


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TranscriptProofError(code)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _parse_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise TranscriptProofError("invalid_transcript_proof_expiry")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise TranscriptProofError("invalid_transcript_proof_expiry") from None
    return parsed.astimezone(UTC)


__all__ = [
    "IssuedTranscriptProof",
    "TranscriptProofBinding",
    "TranscriptProofError",
    "TranscriptSessionScope",
    "canonical_transcript",
    "derive_session_proof_key",
    "issue_transcript_proof",
    "issue_transcript_proof_with_key",
    "verify_transcript_proof",
    "verify_transcript_proof_with_key",
]
