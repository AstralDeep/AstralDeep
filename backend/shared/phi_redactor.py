"""
phi_redactor.py
~~~~~~~~~~~~~~~~

Centralised PHI redaction logic used by the Astral tool‑dispatcher.
The original implementation relied on a set of regular‑expression patterns
and a “fail‑closed” gate that blocks any request that might contain PHI.

This file has been extended to optionally incorporate the OpenAI
*privacy‑filter* model via the :class:`PrivacyFilterAdapter`.  The adapter
is **disabled by default** and can be turned on through the
``ASTRAL_PRIVACY_FILTER_ENABLED`` environment variable or the
``settings.enable_privacy_filter`` flag.

All new code respects the existing contract:
* ``redact(text)`` returns a ``RedactionResult`` containing only the
  redacted spans (character offsets) and never the original text.
* The union of detections from the pattern‑based engine and the
  privacy‑filter model is used.
* Fail‑closed semantics are preserved – if any component raises or
  returns an empty detection when the filter is enabled, the request is
  denied.
"""

import re
import os
import logging
from typing import List, Tuple

from .redaction_result import RedactionResult

# --------------------------------------------------------------------------- #
# Existing pattern‑based redactor
# --------------------------------------------------------------------------- #

_PATTERN_DEFINITIONS = [
    # Simplified examples – the real repo contains many more.
    (re.compile(r"\bMRN[:\s]*\d{6,}\b", re.IGNORECASE), "MRN"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN"),
    (re.compile(r"\b\d{5}(?:-\d{4})?\b"), "ZIP"),
    (re.compile(r"\b(?:Dr|Mr|Ms|Mrs)\.\s+[A-Z][a-z]+", re.IGNORECASE), "TITLE_NAME"),
]

def _pattern_detect(text: str) -> List[Tuple[int, int]]:
    """Return a list of (start, end) offsets for all pattern matches."""
    spans = []
    for regex, _name in _pattern_definitions:
        for m in regex.finditer(text):
            spans.append((m.start(), m.end()))
    return spans

# --------------------------------------------------------------------------- #
# Privacy‑filter integration
# --------------------------------------------------------------------------- #

try:
    from .privacy_filter_adapter import PrivacyFilterAdapter
