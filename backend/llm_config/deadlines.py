"""Separate connect, first-token, idle and attempt deadlines for a model call, chosen by endpoint locality.
client_factory builds the HTTP timeout from it and the orchestrator's stream reader uses stream_wait to bound each wait.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Optional, Tuple

import httpx

from llm_config.local_endpoint import is_local_endpoint

WRITE_TIMEOUT_SECONDS = 30.0
MIN_OVERRIDE_SECONDS = 1.0
MAX_OVERRIDE_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class LLMDeadlines:
    connect: float
    first_token: float
    idle: float
    attempt: float

    def http_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect, read=self.idle, write=WRITE_TIMEOUT_SECONDS, pool=self.connect)


REMOTE = LLMDeadlines(10.0, 60.0, 120.0, 600.0)
LOCAL = LLMDeadlines(10.0, 180.0, 300.0, 600.0)

_ENV_FIELDS = (
    ("connect", "LLM_CONNECT_TIMEOUT_SECONDS"),
    ("first_token", "LLM_FIRST_TOKEN_TIMEOUT_SECONDS"),
    ("idle", "LLM_IDLE_TIMEOUT_SECONDS"),
    ("attempt", "LLM_ATTEMPT_TIMEOUT_SECONDS"),
)


def _override(name: str) -> Optional[float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value:
        return None
    return min(max(value, MIN_OVERRIDE_SECONDS), MAX_OVERRIDE_SECONDS)


def deadlines_for(base_url: Optional[str]) -> LLMDeadlines:
    base = LOCAL if base_url and is_local_endpoint(base_url) else REMOTE
    changes = {}
    for field, env_name in _ENV_FIELDS:
        value = _override(env_name)
        if value is not None:
            changes[field] = value
    return replace(base, **changes) if changes else base


def stream_wait(deadlines: LLMDeadlines, *, now: float, started: float,
                last_progress: float, progressed: bool) -> Tuple[float, str]:
    phase_name = "idle" if progressed else "first_token"
    phase_limit = deadlines.idle if progressed else deadlines.first_token
    phase_left = phase_limit - (now - last_progress)
    attempt_left = deadlines.attempt - (now - started)
    if attempt_left < phase_left:
        return max(attempt_left, 0.0), "attempt"
    return max(phase_left, 0.0), phase_name
