"""Policy for recovering from a failed model call: how long to wait and which configured model to try next.
Orchestrator._call_llm_guarded consults it after each failure and pins a fallback model for the rest of a chat turn.
"""

from __future__ import annotations

import contextvars
import math
import os
from typing import List, Optional

MAX_FALLBACKS = 3
RETRY_AFTER_CAP_SECONDS = 60.0
FALLBACK_ENV = "LLM_FALLBACK_MODELS"

TURN_MODEL: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "astral_llm_turn_model", default=None)

_ELIGIBLE_CLASSES = frozenset({"transport_error", "rate_limit", "model_not_found"})


def reset_turn() -> None:
    TURN_MODEL.set(None)


def fallback_models(primary: str) -> List[str]:
    chain: List[str] = []
    for name in os.environ.get(FALLBACK_ENV, "").split(","):
        name = name.strip()
        if name and name != primary and name not in chain:
            chain.append(name)
    return chain[:MAX_FALLBACKS]


def fallback_eligible(status_code: Optional[int], upstream_error_class: str) -> bool:
    if upstream_error_class in _ELIGIBLE_CLASSES:
        return True
    return status_code is not None and 500 <= status_code <= 599


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def retry_delay(attempt: int, retry_after: Optional[float], jitter: float) -> float:
    if retry_after is not None:
        return min(retry_after, RETRY_AFTER_CAP_SECONDS)
    return min(2 ** (attempt - 1), 8) * jitter


def fallback_notice(primary: str, fallback: str) -> str:
    return f"{primary} did not respond. This turn is using {fallback}."
