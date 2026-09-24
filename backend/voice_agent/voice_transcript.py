"""Development-tree re-export of backend/shared/voice_transcript.py; the worker image
overwrites it at build time so shared code never crosses the boundary, while
session.py and its tests import one canonical implementation.
"""

from shared.voice_transcript import (  # noqa: F401
    IssuedTranscriptProof,
    TranscriptProofBinding,
    TranscriptProofError,
    TranscriptSessionScope,
    canonical_transcript,
    derive_session_proof_key,
    issue_transcript_proof,
    issue_transcript_proof_with_key,
    verify_transcript_proof,
)

__all__ = (
    "IssuedTranscriptProof",
    "TranscriptProofBinding",
    "TranscriptProofError",
    "TranscriptSessionScope",
    "canonical_transcript",
    "derive_session_proof_key",
    "issue_transcript_proof",
    "issue_transcript_proof_with_key",
    "verify_transcript_proof",
)
