"""Local Privacy Filter Adapter and Dispatch Integration.

Fulfills Acceptance Checks 2, 3, 5, and 6 (Issue #318):
- Default-off feature flag control (FF_CONTEXTUAL_PRIVACY_FILTER)
- Union-of-detections execution merging pattern-based redaction with contextual filtering
- Strict sanitization of all results before returning to callers or audit pipelines
- Safe fallback and fail-closed behavior on unexpected adapter errors
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

from .engine import ContextualPrivacyEngine
from .types import FilterStatus, SafeRedactionResult

logger = logging.getLogger("ContextualPrivacyFilter")

FEATURE_FLAG_ENV = "FF_CONTEXTUAL_PRIVACY_FILTER"


def is_contextual_privacy_filter_enabled() -> bool:
    """Return whether the contextual privacy filter is enabled via environment flag.

    Defaults to False (disabled) per acceptance requirements.
    """
    val = os.getenv(FEATURE_FLAG_ENV, "").strip().lower()
    return val in ("1", "true", "yes", "on", "enabled")


class LocalPrivacyFilterAdapter:
    """Adapter bridging the tool execution boundary with the contextual privacy filter."""

    def __init__(self, engine: Optional[ContextualPrivacyEngine] = None) -> None:
        self.engine = engine or ContextualPrivacyEngine()

    def filter_tool_text(self, text: str, force_enable: bool = False) -> SafeRedactionResult:
        """Filter free-form tool text using contextual privacy filtering.

        If the feature flag is disabled and force_enable is False, returns
        the input text with status BYPASSED.
        If enabled, applies the contextual privacy filter.
        """
        if not force_enable and not is_contextual_privacy_filter_enabled():
            return SafeRedactionResult(
                redacted_text=text,
                status=FilterStatus.BYPASSED,
                safe_counts={},
                spans_count=0,
                latency_ms=0.0,
            )

        try:
            result = self.engine.filter_text(text)
            if result.status == FilterStatus.REDACTED:
                logger.debug(
                    "Contextual privacy filter applied: %d spans redacted across categories: %s",
                    result.spans_count,
                    list(result.safe_counts.keys()),
                )
            return result
        except Exception as e:
            logger.error("Contextual privacy filter failed unexpectedly: %s", type(e).__name__)
            # Fail closed: Never leak unredacted text on unexpected error
            return SafeRedactionResult(
                redacted_text="[FAIL_CLOSED_PHI_REDACTED]",
                status=FilterStatus.FAIL_CLOSED,
                safe_counts={"UNEXPECTED_ERROR": 1},
                spans_count=1,
                error_detail=type(e).__name__,
            )

    def redact_union(self, text: str, baseline_redactor_fn: Any) -> Tuple[str, Dict[str, Any]]:
        """Execute union-of-detections: baseline pattern redactor + contextual filter.

        First passes text through baseline redactor (SSN, emails, phone numbers, pattern-based PHI).
        Then passes result through contextual privacy filter (names, addresses, MRNs, contextual dates).
        Returns the combined redacted string and sanitized telemetry metadata.
        """
        # Step 1: Baseline pattern redaction
        baseline_redacted, baseline_modified = baseline_redactor_fn(text)

        # Step 2: Contextual privacy filter
        contextual_res = self.filter_tool_text(baseline_redacted)

        final_text = contextual_res.redacted_text
        combined_meta = {
            "baseline_modified": baseline_modified,
            "contextual_status": contextual_res.status.value,
            "contextual_spans_count": contextual_res.spans_count,
            "contextual_safe_counts": contextual_res.safe_counts,
            "contextual_latency_ms": contextual_res.latency_ms,
        }

        return final_text, combined_meta
