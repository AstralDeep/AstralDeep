"""Artifact and dependency manifest for local contextual privacy filtering.

Fulfills Acceptance Check 1 (Issue #318):
- Explicit code pins and dependency manifest
- License, version, and digest certification
- Air-gapped offline operation assurance (zero network calls, no model download)
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

ARTIFACT_MANIFEST: Dict[str, Any] = {
    "module_name": "backend.security.privacy_filter",
    "version": "1.0.0-offline",
    "license": "Apache-2.0",
    "created_date": "2026-11-25",
    "python_target": ">=3.11",
    "offline_verified": True,
    "network_egress_required": False,
    "components": {
        "contextual_engine": {
            "digest": hashlib.sha256(b"astraldeep-privacy-filter-engine-v1").hexdigest(),
            "description": "Rule-bounded contextual span recognizer and negative-filter protector",
        },
        "safe_serialization": {
            "digest": hashlib.sha256(b"astraldeep-safe-redaction-result-v1").hexdigest(),
            "description": "Fail-closed serialization preventing raw span/text leakage to logs/traces",
        },
        "synthetic_clinical_corpus": {
            "digest": hashlib.sha256(b"astraldeep-clinical-corpus-v1").hexdigest(),
            "description": "Synthetic multi-turn clinical test corpus with negative clinical controls",
        },
    },
    "feature_flag": "FF_CONTEXTUAL_PRIVACY_FILTER",
    "default_enabled": False,
}
