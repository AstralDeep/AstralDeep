"""Reports whether FF_REDTEAM_SELFTEST asks for the adversarial gate before a draft agent
goes live. The gate needed a chat turn that no longer exists, so agentic_creation.py
refuses approval with UNAVAILABLE_CODE while the flag is on.
"""

from __future__ import annotations

import os

UNAVAILABLE_CODE = "redteam_unavailable"


def redteam_enabled() -> bool:
    return os.getenv("FF_REDTEAM_SELFTEST", "false").strip().lower() in ("1", "true", "yes", "on")
