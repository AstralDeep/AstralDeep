"""Types and safe serialization contracts for privacy filter.

Enforces Acceptance Check 3:
Only validated redacted text and safe aggregate counts may leave the filter boundary.
Raw original text and span text are strictly shielded from logs, traces, audit
payloads, and durable records.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FilterStatus(str, Enum):
    CLEAN = "CLEAN"
    REDACTED = "REDACTED"
    FAIL_CLOSED = "FAIL_CLOSED"
    BYPASSED = "BYPASSED"


@dataclass(frozen=True)
class PrivacySpan:
    """Represents a detected sensitive span boundary.

    Internal to the engine during processing.
    Notice: span text is hashed, NEVER kept plaintext in durable structures.
    """
    start: int
    end: int
    entity_type: str
    confidence: float = 1.0
    text_digest: str = ""

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"Invalid span boundaries: [{self.start}, {self.end}]")

    @classmethod
    def create(cls, start: int, end: int, entity_type: str, raw_span_text: str = "", confidence: float = 1.0) -> PrivacySpan:
        digest = hashlib.sha256(raw_span_text.encode("utf-8")).hexdigest()[:16] if raw_span_text else ""
        return cls(start=start, end=end, entity_type=entity_type, confidence=confidence, text_digest=digest)


@dataclass(frozen=True)
class SafeRedactionResult:
    """Safe, immutable result boundary for contextual privacy filtering.

    STRICT GUARANTEE:
    This object and its serialized dictionary representation NEVER include
    the original unredacted text or raw extracted span values.
    Only redacted_text, safe aggregate category counts, span count, status,
    and performance metrics are exported.
    """
    redacted_text: str
    status: FilterStatus
    safe_counts: Dict[str, int] = field(default_factory=dict)
    spans_count: int = 0
    latency_ms: float = 0.0
    error_detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Safe dictionary representation for audit logging and serialization.

        Guaranteed not to contain raw original text or raw span values.
        """
        data: Dict[str, Any] = {
            "status": self.status.value,
            "spans_count": self.spans_count,
            "safe_counts": dict(self.safe_counts),
            "latency_ms": round(self.latency_ms, 3),
        }
        if self.error_detail:
            # Error detail is sanitized, containing only error category, never raw payload
            data["error_category"] = self.error_detail
        return data

    def __repr__(self) -> str:
        return (
            f"SafeRedactionResult(status={self.status.value}, "
            f"spans_count={self.spans_count}, safe_counts={self.safe_counts}, "
            f"latency_ms={self.latency_ms:.2f})"
        )
