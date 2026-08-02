"""Golden and negative transcript-proof vectors for Feature 065."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from shared.voice_transcript import (
    TranscriptProofBinding,
    TranscriptProofError,
    TranscriptSessionScope,
    canonical_transcript,
    derive_session_proof_key,
    issue_transcript_proof,
    issue_transcript_proof_with_key,
    verify_transcript_proof,
    verify_transcript_proof_with_key,
)

NOW = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)
SECRET = b"voice-control-secret-golden-vector-065"
BINDING = TranscriptProofBinding(
    session_id="10000000-0000-4000-8000-000000000001",
    generation=7,
    media_grant_revision=3,
    assignment_id="20000000-0000-4000-8000-000000000002",
    worker_identity="voice-worker-a",
    turn_id="30000000-0000-4000-8000-000000000003",
    client_turn_id="40000000-0000-4000-8000-000000000004",
    submission_id="50000000-0000-4000-8000-000000000005",
    request_generation="60000000-0000-4000-8000-000000000006",
    chat_id="70000000-0000-4000-8000-000000000007",
    chat_context_revision=11,
    detected_language="en",
)


def test_canonical_transcript_normalization_and_strict_bounds() -> None:
    assert (
        canonical_transcript("  Cafe\u0301\r\nnext\tline  ") == "Caf\u00e9\nnext\tline"
    )
    for invalid, code in (
        ("", "empty_transcript"),
        (" \r\n ", "empty_transcript"),
        ("hello\x00world", "invalid_transcript_text"),
        ("hello\rworld", "invalid_transcript_text"),
        ("hello\u007fworld", "invalid_transcript_text"),
        ("hello\ud800world", "invalid_transcript_text"),
        ("x" * 8_001, "transcript_text_too_large"),
        ("\u0800" * 8_001, "transcript_text_too_large"),
        ("\U0001f600" * 7_000, "transcript_text_too_large"),
        (b"not text", "invalid_transcript_text"),
    ):
        with pytest.raises(TranscriptProofError, match=code):
            canonical_transcript(invalid)


def test_two_minute_hmac_golden_vector_matches_prederived_worker_key() -> None:
    session_key = derive_session_proof_key(SECRET, BINDING.session_scope)
    assert session_key.hex() == (
        "d40859ca2a6068b27dc3299de9c9eaabbde219779aa28c61741ccdf4071cb8fe"
    )

    direct = issue_transcript_proof(
        SECRET,
        BINDING,
        "  Cafe\u0301\r\nnext  ",
        now=NOW,
    )
    derived = issue_transcript_proof_with_key(
        session_key,
        BINDING,
        "  Cafe\u0301\r\nnext  ",
        now=NOW,
    )

    assert direct == derived
    assert direct.canonical_text == "Caf\u00e9\nnext"
    assert direct.text_digest_sha256 == (
        "475a17f57eaf35f36d2a6ab988252f15e377f5ed8195f40fd6bdf65f88c74bf4"
    )
    assert direct.transcript_proof == (
        "1b2f88e4f4f873fe0724a1665966b8d4ddf4b8bc550d2de7044678b055b2cb4a"
    )
    assert direct.proof_expires_at == "2026-07-31T20:02:00Z"
    assert (
        verify_transcript_proof(
            SECRET,
            BINDING,
            direct.canonical_text,
            text_digest_sha256=direct.text_digest_sha256,
            transcript_proof=direct.transcript_proof,
            proof_expires_at=direct.proof_expires_at,
            now=NOW + timedelta(seconds=119),
        )
        == direct.canonical_text
    )
    assert (
        verify_transcript_proof_with_key(
            session_key,
            BINDING,
            direct.canonical_text,
            text_digest_sha256=direct.text_digest_sha256,
            transcript_proof=direct.transcript_proof,
            proof_expires_at=direct.proof_expires_at,
            now=NOW + timedelta(seconds=1),
        )
        == direct.canonical_text
    )


@pytest.mark.parametrize(
    ("binding", "secret"),
    [
        (replace(BINDING, worker_identity="voice-worker-b"), SECRET),
        (
            replace(
                BINDING,
                assignment_id="20000000-0000-4000-8000-000000000009",
            ),
            SECRET,
        ),
        (replace(BINDING, media_grant_revision=4), SECRET),
        (
            replace(
                BINDING,
                turn_id="30000000-0000-4000-8000-000000000009",
            ),
            SECRET,
        ),
        (
            replace(
                BINDING,
                chat_id="70000000-0000-4000-8000-000000000009",
            ),
            SECRET,
        ),
        (BINDING, b"different-control-secret-for-vector-065"),
    ],
)
def test_proof_cannot_be_transplanted_to_another_worker_or_binding(
    binding: TranscriptProofBinding,
    secret: bytes,
) -> None:
    issued = issue_transcript_proof(SECRET, BINDING, "status please", now=NOW)
    with pytest.raises(TranscriptProofError, match="transcript_proof_mismatch"):
        verify_transcript_proof(
            secret,
            binding,
            issued.canonical_text,
            text_digest_sha256=issued.text_digest_sha256,
            transcript_proof=issued.transcript_proof,
            proof_expires_at=issued.proof_expires_at,
            now=NOW + timedelta(seconds=1),
        )


def test_altered_noncanonical_expired_and_overlong_proofs_fail_closed() -> None:
    issued = issue_transcript_proof(SECRET, BINDING, "status please", now=NOW)
    cases = (
        ({"text": "status changed"}, "transcript_digest_mismatch"),
        ({"text": " status please "}, "noncanonical_transcript"),
        ({"text_digest_sha256": "f" * 64}, "transcript_digest_mismatch"),
        ({"transcript_proof": "f" * 64}, "transcript_proof_mismatch"),
    )
    base = {
        "text": issued.canonical_text,
        "text_digest_sha256": issued.text_digest_sha256,
        "transcript_proof": issued.transcript_proof,
        "proof_expires_at": issued.proof_expires_at,
    }
    for changes, code in cases:
        values = {**base, **changes}
        with pytest.raises(TranscriptProofError, match=code):
            verify_transcript_proof(
                SECRET,
                BINDING,
                values.pop("text"),
                now=NOW + timedelta(seconds=1),
                **values,
            )
    with pytest.raises(TranscriptProofError, match="transcript_proof_expired"):
        verify_transcript_proof(
            SECRET,
            BINDING,
            issued.canonical_text,
            text_digest_sha256=issued.text_digest_sha256,
            transcript_proof=issued.transcript_proof,
            proof_expires_at=issued.proof_expires_at,
            now=NOW + timedelta(minutes=2),
        )
    with pytest.raises(
        TranscriptProofError, match="transcript_proof_lifetime_exceeded"
    ):
        verify_transcript_proof(
            SECRET,
            BINDING,
            issued.canonical_text,
            text_digest_sha256=issued.text_digest_sha256,
            transcript_proof=issued.transcript_proof,
            proof_expires_at="2026-07-31T20:02:02Z",
            now=NOW + timedelta(seconds=1),
        )


def test_proof_types_and_representations_do_not_expose_or_persist_content() -> None:
    issued = issue_transcript_proof(SECRET, BINDING, "private patient detail", now=NOW)
    rendered = repr(issued)
    assert "private patient detail" not in rendered
    assert issued.transcript_proof not in rendered
    assert issued.text_digest_sha256 not in rendered
    assert "<redacted>" in rendered

    scope = TranscriptSessionScope(
        BINDING.session_id,
        BINDING.generation,
        BINDING.assignment_id,
        BINDING.worker_identity,
    )
    assert scope == BINDING.session_scope

    migration_source = (Path(__file__).parents[1] / "shared" / "database.py").read_text(
        encoding="utf-8"
    )
    voice_turn_ddl = migration_source.split(
        "CREATE TABLE IF NOT EXISTS voice_turn (", 1
    )[1].split("CREATE UNIQUE INDEX IF NOT EXISTS ux_voice_turn_client_065", 1)[0]
    for forbidden_column in (
        "transcript_text",
        "transcript_proof",
        "text_digest_sha256",
        "proof_expires_at",
    ):
        assert forbidden_column not in voice_turn_ddl


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (
            lambda: TranscriptSessionScope(
                BINDING.session_id,
                BINDING.generation,
                BINDING.assignment_id,
                "invalid worker",
            ),
            "invalid_worker_identity",
        ),
        (
            lambda: replace(BINDING, worker_identity="invalid worker"),
            "invalid_worker_identity",
        ),
        (
            lambda: replace(BINDING, detected_language="EN"),
            "invalid_transcript_language",
        ),
        (
            lambda: replace(BINDING, session_id="not-a-uuid"),
            "invalid_session_id",
        ),
        (
            lambda: replace(BINDING, generation=0),
            "invalid_generation",
        ),
    ],
)
def test_proof_bindings_reject_invalid_assignment_scope(
    factory: Callable[[], object],
    code: str,
) -> None:
    with pytest.raises(TranscriptProofError, match=code):
        factory()


def test_proof_public_api_rejects_invalid_keys_clocks_and_wire_shapes() -> None:
    session_key = derive_session_proof_key(SECRET, BINDING.session_scope)
    issued = issue_transcript_proof(SECRET, BINDING, "status", now=NOW)

    for lifetime in (0, 121, True, 1.5):
        with pytest.raises(
            TranscriptProofError,
            match="invalid_transcript_proof_lifetime",
        ):
            issue_transcript_proof_with_key(
                session_key,
                BINDING,
                "status",
                now=NOW,
                lifetime_seconds=lifetime,  # type: ignore[arg-type]
            )
    with pytest.raises(
        TranscriptProofError,
        match="invalid_transcript_proof_clock",
    ):
        issue_transcript_proof(SECRET, BINDING, "status", now=NOW.replace(tzinfo=None))
    with pytest.raises(
        TranscriptProofError,
        match="invalid_worker_control_secret",
    ):
        derive_session_proof_key(b"short", BINDING.session_scope)
    with pytest.raises(
        TranscriptProofError,
        match="invalid_transcript_session_key",
    ):
        issue_transcript_proof_with_key(b"short", BINDING, "status", now=NOW)

    base = {
        "text_digest_sha256": issued.text_digest_sha256,
        "transcript_proof": issued.transcript_proof,
        "proof_expires_at": issued.proof_expires_at,
    }
    invalid_wire_values = (
        ({"text_digest_sha256": "not-a-digest"}, "invalid_transcript_digest"),
        ({"transcript_proof": "not-a-proof"}, "invalid_transcript_proof"),
        ({"proof_expires_at": 17}, "invalid_transcript_proof_expiry"),
        (
            {"proof_expires_at": "2026-07-31T20:02:00+00:00"},
            "invalid_transcript_proof_expiry",
        ),
        (
            {"proof_expires_at": "2026-07-31T20:02:00.000000Z"},
            "invalid_transcript_proof_expiry",
        ),
        ({"proof_expires_at": "not-a-timeZ"}, "invalid_transcript_proof_expiry"),
    )
    for changes, code in invalid_wire_values:
        with pytest.raises(TranscriptProofError, match=code):
            verify_transcript_proof(
                SECRET,
                BINDING,
                issued.canonical_text,
                now=NOW + timedelta(seconds=1),
                **{**base, **changes},
            )
