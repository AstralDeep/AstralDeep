"""Contextual Privacy Filter Package.

Qualifies local contextual privacy filtering for free-form tool text (Issue #318).
Provides fail-closed, offline, union-of-detections redaction while preserving
clinical meaning and preventing sensitive raw text/span leakage into audit logs.
"""

from __future__ import annotations

from .adapter import LocalPrivacyFilterAdapter, is_contextual_privacy_filter_enabled
from .engine import ContextualPrivacyEngine
from .manifest import ARTIFACT_MANIFEST
from .types import FilterStatus, PrivacySpan, SafeRedactionResult

__all__ = [
    "ARTIFACT_MANIFEST",
    "ContextualPrivacyEngine",
    "FilterStatus",
    "LocalPrivacyFilterAdapter",
    "PrivacySpan",
    "SafeRedactionResult",
    "is_contextual_privacy_filter_enabled",
]
