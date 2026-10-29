"""Check transcript proof rejection and redaction at the worker boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voice_agent.voice_transcript import (
    TranscriptProofBinding,
    TranscriptProofError,
    TranscriptSessionScope,
    canonical_transcript,
    issue_transcript_proof,
    verify_transcript_proof,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
SECRET = b"worker-control-secret-for-release-tests"
BINDING = TranscriptProofBinding(
    session_id="10000000-0000-4000-8000-000000000001",
    generation=4,
    media_grant_revision=2,
    assignment_id="20000000-0000-4000-8000-000000000002",
    worker_identity="worker-private",
    turn_id="30000000-0000-4000-8000-000000000003",
    client_turn_id="40000000-0000-4000-8000-000000000004",
    submission_id="50000000-0000-4000-8000-000000000005",
    request_generation="60000000-0000-4000-8000-000000000006",
    chat_id="70000000-0000-4000-8000-000000000007",
    chat_context_revision=9,
    detected_language="en-US",
)


def test_transcript_proof_redacts_private_text_and_rejects_bad_canonical_forms() -> None:
    private_text = "private clinical utterance"
    issued = issue_transcript_proof(SECRET, BINDING, private_text, now=NOW)

    assert private_text not in repr(issued)
    assert issued.transcript_proof not in repr(issued)
    assert issued.text_digest_sha256 not in repr(issued)
    assert verify_transcript_proof(
        SECRET,
        BINDING,
        issued.canonical_text,
        text_digest_sha256=issued.text_digest_sha256,
        transcript_proof=issued.transcript_proof,
        proof_expires_at=issued.proof_expires_at,
        now=NOW + timedelta(seconds=1),
    ) == private_text

    with pytest.raises(TranscriptProofError, match="noncanonical_transcript"):
        verify_transcript_proof(
            SECRET,
            BINDING,
            f" {private_text} ",
            text_digest_sha256=issued.text_digest_sha256,
            transcript_proof=issued.transcript_proof,
            proof_expires_at=issued.proof_expires_at,
            now=NOW + timedelta(seconds=1),
        )


def test_invalid_text_encoding_and_session_scope_are_rejected() -> None:
    with pytest.raises(TranscriptProofError, match="invalid_transcript_text"):
        canonical_transcript("unpaired surrogate \ud800")

    with pytest.raises(TranscriptProofError, match="invalid_worker_identity"):
        TranscriptSessionScope(
            BINDING.session_id,
            BINDING.generation,
            BINDING.assignment_id,
            "worker identity with spaces",
        )
