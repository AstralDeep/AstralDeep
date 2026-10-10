"""
Compaction utilities for AstralDeep.

This module is responsible for trimming the request payload to fit within the
model's context window. Historically it used a very simple heuristic
(`len(text) / 4`) and ignored tool‑call metadata. The new implementation
integrates the provider‑aware token estimator defined in
``backend.orchestrator.context_token`` and performs a *pre‑flight* budget
check before any trimming occurs.

Only the public function ``compact_payload`` is used by the rest of the code‑base.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping

from .context_token import estimate_request_tokens

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration constants – these can be tuned via environment variables if
# required, but the defaults are safe for production.
# --------------------------------------------------------------------------- #

DEFAULT_MAX_TRIM_ITERATIONS = 5
TRIM_FRACTION = 0.25  # Remove roughly 1/4 of the oldest messages each iteration.

# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #

def _trim_messages(messages: List[Dict[str, Any]], fraction: float) -> List[Dict[str, Any]]:
    """
    Return a new list of messages with the oldest ``fraction`` removed.
    The function never mutates the original list.
    """
    if not messages:
        return messages

    cut_index = int(len(messages) * (1 - fraction))
    # Keep the newest messages up to ``cut_index
