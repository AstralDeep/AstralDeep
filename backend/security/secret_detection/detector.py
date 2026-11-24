"""Deterministic secret detector and streaming buffer.

Implements safe, fail-closed secret detection at the redaction boundary.
Strict audit guarantees:
- Never logs checked text, raw secrets, keys, or matched secret substrings.
- Safe on malformed unicode, NUL characters, and oversized input.
- Provides streamed buffer chunk boundary detection for real-time egress.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from security.secret_detection.patterns import (
    CLINICAL_EXEMPTION_WORDS,
    CONTEXTUAL_MIN_ENTROPY,
    CONTEXTUAL_MIN_LENGTH,
    PROVIDER_SECRET_PATTERNS,
    RE_CONTEXTUAL_ASSIGNMENT,
    calculate_shannon_entropy,
    is_clinical_exemption,
)

logger = logging.getLogger("SecretDetector")

MAX_SCAN_TEXT_SIZE = 512 * 1024  # 512 KB upper safety bound
STREAM_BUFFER_OVERLAP = 128       # Overlap window for streamed chunk scanning


@dataclass(frozen=True, slots=True)
class SecretFinding:
    pattern_name: str
    start: int
    end: int
    entropy: float
    replacement: str
    # STRICT COMPLIANCE: Raw secret text is explicitly NOT stored in this object.


@dataclass(frozen=True, slots=True)
class DetectionResult:
    redacted_text: str
    findings: Tuple[SecretFinding, ...]
    has_secrets: bool
    entropy_checked: int


class SecretDetector:
    """Production-grade deterministic secret scanner."""

    def __init__(
        self,
        *,
        enable_entropy_heuristics: bool = True,
        max_scan_size: int = MAX_SCAN_TEXT_SIZE,
    ):
        self._enable_entropy = enable_entropy_heuristics
        self._max_scan_size = max_scan_size

    def scan_and_redact(self, text: Optional[str]) -> DetectionResult:
        """Scan text, identify secret patterns, and redact in-place.
        
        Guarantees:
        - Never raises an unhandled exception (fail-closed fallback).
        - Never outputs or logs secret payloads.
        - Preserves clinical medical terminology and genomic notations.
        """
        if text is None:
            return DetectionResult(redacted_text="", findings=(), has_secrets=False, entropy_checked=0)
        
        if not isinstance(text, str):
            text = str(text)

        # Safety bound check
        if len(text) > self._max_scan_size:
            logger.warning("SecretDetector input exceeded max scan size, enforcing truncation.")
            text = text[:self._max_scan_size]

        try:
            findings: List[SecretFinding] = []
            spans_to_replace: List[Tuple[int, int, str, str, float]] = []

            # 1. Check pinned provider & infrastructure patterns
            for pattern_name, regex, replacement in PROVIDER_SECRET_PATTERNS:
                for match in regex.finditer(text):
                    start, end = match.span()
                    matched_val = match.group(0)
                    
                    # Protect against clinical exemptions
                    if is_clinical_exemption(matched_val):
                        continue

                    # Special handling for database URIs with credentials: only redact password portion
                    if pattern_name == "database_url_credentials":
                        try:
                            pwd_start = match.start("pass")
                            pwd_end = match.end("pass")
                            spans_to_replace.append((pwd_start, pwd_end, pattern_name, "[REDACTED:pwd]", 0.0))
                            continue
                        except (IndexError, AttributeError):
                            pass

                    ent = calculate_shannon_entropy(matched_val)
                    spans_to_replace.append((start, end, pattern_name, replacement, ent))

            # 2. Check contextual assignments if enabled
            entropy_checked_count = 0
            if self._enable_entropy:
                for match in RE_CONTEXTUAL_ASSIGNMENT.finditer(text):
                    key = match.group("key")
                    val = match.group("val")
                    
                    if is_clinical_exemption(key) or is_clinical_exemption(val):
                        continue

                    entropy = calculate_shannon_entropy(val)
                    entropy_checked_count += 1
                    
                    # High-entropy random string or sufficiently long contextual credential
                    if entropy >= CONTEXTUAL_MIN_ENTROPY or len(val) >= CONTEXTUAL_MIN_LENGTH:
                        val_start = match.start("val")
                        val_end = match.end("val")
                        spans_to_replace.append(
                            (val_start, val_end, "contextual_credential", "[REDACTED:credential]", entropy)
                        )

            if not spans_to_replace:
                return DetectionResult(
                    redacted_text=text,
                    findings=(),
                    has_secrets=False,
                    entropy_checked=entropy_checked_count,
                )

            # 3. Sort spans and resolve overlaps deterministically
            spans_to_replace.sort(key=lambda s: (s[0], -(s[1] - s[0])))
            merged_spans: List[Tuple[int, int, str, str, float]] = []
            last_end = -1
            for start, end, name, repl, ent in spans_to_replace:
                if start >= last_end:
                    merged_spans.append((start, end, name, repl, ent))
                    last_end = end
                elif end > last_end and start < last_end:
                    # Partial overlap: prioritize previous broader span
                    continue

            # 4. Construct redacted text without storing or leaking raw secret
            pieces = []
            curr_pos = 0
            for start, end, name, repl, ent in merged_spans:
                pieces.append(text[curr_pos:start])
                pieces.append(repl)
                findings.append(
                    SecretFinding(
                        pattern_name=name,
                        start=start,
                        end=end,
                        entropy=ent,
                        replacement=repl,
                    )
                )
                curr_pos = end
            pieces.append(text[curr_pos:])
            redacted = "".join(pieces)

            return DetectionResult(
                redacted_text=redacted,
                findings=tuple(findings),
                has_secrets=True,
                entropy_checked=entropy_checked_count,
            )

        except Exception as exc:
            # Fail closed: log safe structured error without text/payload
            logger.error("SecretDetector internal error: %s (falling back to sanitized state)", type(exc).__name__)
            return DetectionResult(
                redacted_text="[REDACTED — detector error]",
                findings=(
                    SecretFinding(
                        pattern_name="error_fallback",
                        start=0,
                        end=len(text),
                        entropy=0.0,
                        replacement="[REDACTED — detector error]",
                    ),
                ),
                has_secrets=True,
                entropy_checked=0,
            )


class StreamedSecretBuffer:
    """Sliding-window buffer for detecting secrets across streamed chunk boundaries.
    
    When an LLM streams tokens, an API key like 'sk-proj-abc...' may arrive in
    multiple small chunks. This buffer retains a small overlap to prevent leakage.
    """

    def __init__(self, detector: Optional[SecretDetector] = None, window_size: int = STREAM_BUFFER_OVERLAP):
        self._detector = detector or SecretDetector()
        self._window_size = window_size
        self._buffer = ""

    def append_chunk(self, chunk: str) -> str:
        """Push a newly arrived chunk and emit safe redacted content."""
        if not chunk:
            return ""

        self._buffer += chunk
        
        # If buffer is smaller than overlap window, wait for more data to avoid boundary leak
        if len(self._buffer) <= self._window_size:
            return ""

        # Scan accumulated buffer
        result = self._detector.scan_and_redact(self._buffer)
        scanned = result.redacted_text

        # Retain trailing window for subsequent chunk boundary checking
        emit_len = len(scanned) - self._window_size
        if emit_len > 0:
            emit_part = scanned[:emit_len]
            self._buffer = scanned[emit_len:]
            return emit_part
        else:
            return ""

    def flush(self) -> str:
        """Flush remaining buffer content at stream completion."""
        if not self._buffer:
            return ""
        result = self._detector.scan_and_redact(self._buffer)
        remaining = result.redacted_text
        self._buffer = ""
        return remaining
