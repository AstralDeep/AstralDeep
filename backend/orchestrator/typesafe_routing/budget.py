"""Turn-scoped attempt scheduling, deadline clipping, and a per-user circuit breaker for
TypeSafe routing calls; runner.py drives it, keeping every retry and backoff within
the turn's budget and isolating one user's failures from another's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import random
import time
from typing import Callable, Optional

logger = logging.getLogger("Orchestrator.TypeSafe.Budget")

TURN_BUDGET_SECONDS = 1.5

MAX_ATTEMPTS = 3

BACKOFF_MS = (100, 200)
BACKOFF_JITTER = 0.2

CIRCUIT_FAILURE_THRESHOLD = 3

CIRCUIT_COOLDOWN_SECONDS = 300.0

CIRCUIT_IDLE_EVICTION_SECONDS = 3600.0

MAX_HONORED_RETRY_AFTER_SECONDS = 1.0


class Outcome(str, Enum):
    SUCCESS = "success"
    FALLBACK_TRANSIENT = "fallback_transient"
    FALLBACK_NONTRANSIENT = "fallback_nontransient"
    FALLBACK_DEADLINE = "fallback_deadline"
    SKIPPED_CIRCUIT = "skipped_circuit"
    SKIPPED_NO_KEY = "skipped_no_key"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_UNAVAILABLE = "skipped_unavailable"
    CANCELLED = "cancelled"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


TRANSIENT_ERROR_NAMES = frozenset(
    {
        "TypeSafeAPITimeoutError",
        "TypeSafeAPIConnectionError",
        "TypeSafeRateLimitError",
        "TypeSafeInternalServerError",
    }
)

NONTRANSIENT_ERROR_NAMES = frozenset(
    {
        "TypeSafeAuthenticationError",
        "TypeSafePermissionDeniedError",
        "TypeSafeUnprocessableEntityError",
        "TypeSafeAPIResponseValidationError",
        "TypeSafeBadRequestError",
        "TypeSafeNotFoundError",
    }
)

AUTH_ERROR_NAMES = frozenset(
    {"TypeSafeAuthenticationError", "TypeSafePermissionDeniedError"}
)

TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})


def _error_name(error: BaseException) -> str:
    return type(error).__name__


def _error_status(error: BaseException) -> Optional[int]:
    for attribute in ("status_code", "status", "http_status"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


# Never classify by message text — it can contain the user's request
def is_transient(error: BaseException) -> bool:
    name = _error_name(error)
    if name in TRANSIENT_ERROR_NAMES:
        return True
    if name in NONTRANSIENT_ERROR_NAMES:
        return False
    status = _error_status(error)
    if status is not None:
        return status in TRANSIENT_STATUSES or status >= 500
    return False


def is_auth_failure(error: BaseException) -> bool:
    if _error_name(error) in AUTH_ERROR_NAMES:
        return True
    return _error_status(error) in (401, 403)


def retry_after_seconds(error: BaseException) -> Optional[float]:
    value = getattr(error, "retry_after", None)
    if value is None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                value = headers.get("retry-after")
            except Exception:  # pragma: no cover
                value = None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    # NaN is the only value that compares unequal to itself
    if seconds != seconds:
        return None
    if seconds <= 0 or seconds > MAX_HONORED_RETRY_AFTER_SECONDS:
        return None
    return seconds


@dataclass
class Deadline:
    total: float = TURN_BUDGET_SECONDS
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.clock()

    @property
    def expires_at(self) -> float:
        return self.started_at + self.total

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def elapsed(self) -> float:
        return self.clock() - self.started_at


def attempt_timeout(deadline: Deadline, attempt_timeout_ms: int) -> float:
    return min(attempt_timeout_ms / 1000.0, deadline.remaining())


def backoff_delay(
    attempt: int,
    deadline: Deadline,
    *,
    retry_after: Optional[float] = None,
    jitter: Optional[Callable[[float, float], float]] = None,
) -> float:
    if attempt <= 1:
        return 0.0
    index = min(attempt - 2, len(BACKOFF_MS) - 1)
    base = BACKOFF_MS[index] / 1000.0
    if retry_after is not None:
        delay = retry_after
    else:
        spread = base * BACKOFF_JITTER
        rand = jitter or random.uniform
        delay = base + rand(-spread, spread)
    return max(0.0, min(delay, deadline.remaining()))


@dataclass
class CircuitEntry:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    last_seen: float = 0.0
    auth_blocked_fingerprint: Optional[str] = None


class UserCircuit:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        cooldown: float = CIRCUIT_COOLDOWN_SECONDS,
        idle_eviction: float = CIRCUIT_IDLE_EVICTION_SECONDS,
    ) -> None:
        self._clock = clock
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._idle_eviction = idle_eviction
        self._entries: dict[str, CircuitEntry] = {}

    def _entry(self, user_id: str) -> CircuitEntry:
        entry = self._entries.get(user_id)
        if entry is None:
            entry = CircuitEntry(last_seen=self._clock())
            self._entries[user_id] = entry
        return entry

    def _evict_idle(self) -> None:
        now = self._clock()
        stale = [
            user_id
            for user_id, entry in self._entries.items()
            if now - entry.last_seen > self._idle_eviction
        ]
        for user_id in stale:
            self._entries.pop(user_id, None)

    def allows(self, user_id: str, *, fingerprint: Optional[str] = None) -> bool:
        self._evict_idle()
        entry = self._entry(user_id)
        entry.last_seen = self._clock()

        if entry.auth_blocked_fingerprint is not None:
            if fingerprint is not None and fingerprint != entry.auth_blocked_fingerprint:
                entry.auth_blocked_fingerprint = None
                entry.state = CircuitState.CLOSED
                entry.consecutive_failures = 0
                return True
            return False

        if entry.state is CircuitState.CLOSED:
            return True
        if entry.state is CircuitState.HALF_OPEN:
            return False
        if self._clock() - entry.opened_at >= self._cooldown:
            entry.state = CircuitState.HALF_OPEN
            return True
        return False

    def state_of(self, user_id: str) -> CircuitState:
        entry = self._entries.get(user_id)
        return CircuitState.CLOSED if entry is None else entry.state

    def record_success(self, user_id: str) -> None:
        entry = self._entry(user_id)
        entry.state = CircuitState.CLOSED
        entry.consecutive_failures = 0
        entry.opened_at = 0.0
        entry.auth_blocked_fingerprint = None
        entry.last_seen = self._clock()

    def record_failure(self, user_id: str) -> CircuitState:
        entry = self._entry(user_id)
        entry.last_seen = self._clock()
        if entry.state is CircuitState.HALF_OPEN:
            entry.state = CircuitState.OPEN
            entry.opened_at = self._clock()
            return entry.state
        entry.consecutive_failures += 1
        if entry.consecutive_failures >= self._failure_threshold:
            entry.state = CircuitState.OPEN
            entry.opened_at = self._clock()
        return entry.state

    def record_auth_failure(self, user_id: str, fingerprint: Optional[str]) -> None:
        entry = self._entry(user_id)
        entry.last_seen = self._clock()
        entry.state = CircuitState.OPEN
        entry.opened_at = self._clock()
        if fingerprint is None:
            entry.consecutive_failures = self._failure_threshold
            return
        entry.auth_blocked_fingerprint = fingerprint

    def reset(self, user_id: str) -> None:
        self._entries.pop(user_id, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:  # pragma: no cover
        return len(self._entries)


_circuit: Optional[UserCircuit] = None


def circuit() -> UserCircuit:
    global _circuit
    if _circuit is None:
        _circuit = UserCircuit()
    return _circuit


def set_circuit(replacement: Optional[UserCircuit]) -> None:
    global _circuit
    _circuit = replacement


__all__ = (
    "AUTH_ERROR_NAMES",
    "BACKOFF_MS",
    "CIRCUIT_COOLDOWN_SECONDS",
    "CIRCUIT_FAILURE_THRESHOLD",
    "MAX_ATTEMPTS",
    "MAX_HONORED_RETRY_AFTER_SECONDS",
    "NONTRANSIENT_ERROR_NAMES",
    "TRANSIENT_ERROR_NAMES",
    "TRANSIENT_STATUSES",
    "TURN_BUDGET_SECONDS",
    "CircuitEntry",
    "CircuitState",
    "Deadline",
    "Outcome",
    "UserCircuit",
    "attempt_timeout",
    "backoff_delay",
    "circuit",
    "is_auth_failure",
    "is_transient",
    "retry_after_seconds",
    "set_circuit",
)
