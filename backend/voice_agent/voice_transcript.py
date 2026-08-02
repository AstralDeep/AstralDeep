"""Development-tree bridge to the canonical transcript-proof implementation.

The isolated worker image overwrites this bridge with the audited
``backend/shared/voice_transcript.py`` source at build time.  Keeping the
import inside ``voice_agent`` prevents the product ``shared`` package from
crossing the worker boundary while preserving one canonical implementation in
the repository and in ordinary backend test runs.
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
