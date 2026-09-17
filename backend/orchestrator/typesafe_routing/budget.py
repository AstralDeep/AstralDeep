"""Attempt scheduling, the turn deadline, and the per-user circuit (feature 089).

The owner's requirement for a misbehaving TypeSafe is a sequence, not a
tolerance: notice, up to three attempts inside 1.5 seconds, fall back to
standard routing, and stop making the user wait for a service that is not
answering. This module is that sequence.

Two ideas do most of the work.

**The deadline is monotonic and belongs to the turn.** Every attempt's timeout
is clipped to what is left of it, and so is every backoff sleep. That is what
makes the worst case bounded: a slow first attempt cannot buy a third attempt
extra time, and a backoff can never push the total past the deadline.

**The circuit is per user, not per process.** A rate limit on one user's key
says nothing about another user's, and a shared circuit would let one
misconfigured account degrade everyone's routing. Auth failures get their own
treatment: they are blocked by key fingerprint until the fingerprint changes,
because retrying a rejected key on a timer only wastes turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import random
import time
from typing import Callable, Optional

logger = logging.getLogger("Orchestrator.TypeSafe.Budget")

#: Total wall clock a turn may spend on routing, in seconds.
TURN_BUDGET_SECONDS = 1.5

#: Attempts per turn, including the first.
MAX_ATTEMPTS = 3

#: Base backoff before attempt 2 and attempt 3, in milliseconds. Jitter is
#: +/-20%, and the result is clipped to the remaining budget.
BACKOFF_MS = (100, 200)
BACKOFF_JITTER = 0.2

#: Consecutive turn-level fallbacks that open a user's circuit.
CIRCUIT_FAILURE_THRESHOLD = 3

#: How long the circuit stays open before a half-open trial, in seconds.
CIRCUIT_COOLDOWN_SECONDS = 300.0

#: A user's circuit state is dropped after this long without a turn, so an
#: idle process does not accumulate one entry per user who ever chatted.
CIRCUIT_IDLE_EVICTION_SECONDS = 3600.0

#: A bounded `Retry-After` is honored only when it fits the remaining budget.
#: A server asking for 30 seconds is telling us to fall back, not to wait.
MAX_HONORED_RETRY_AFTER_SECONDS = 1.0


class Outcome(str, Enum):
    """How a turn's routing ended. Recorded as a metric label; carries no content."""

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


# Error class names, matched by name so this module never imports the SDK.
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

#: HTTP statuses that make a generic ``TypeSafeAPIError`` transient.
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


def is_transient(error: BaseException) -> bool:
    """True when another attempt could plausibly succeed.

    Classification is by class name plus HTTP status, never by message text: a
    message can contain the user's request, and matching on it would make the
    retry policy depend on content.
    """
    name = _error_name(error)
    if name in TRANSIENT_ERROR_NAMES:
        return True
    if name in NONTRANSIENT_ERROR_NAMES:
        return False
    status = _error_status(error)
    if status is not None:
        return status in TRANSIENT_STATUSES or status >= 500
    # An unrecognized error is treated as non-transient. Retrying something we
    # do not understand spends the user's latency budget on a guess.
    return False


def is_auth_failure(error: BaseException) -> bool:
    """True for 401/403-class failures, which block by key fingerprint."""
    if _error_name(error) in AUTH_ERROR_NAMES:
        return True
    return _error_status(error) in (401, 403)


def retry_after_seconds(error: BaseException) -> Optional[float]:
    """Return a bounded ``Retry-After``, or ``None``.

    A value larger than :data:`MAX_HONORED_RETRY_AFTER_SECONDS` is ignored
    rather than clamped: the server is saying "not soon", and the honest answer
    is to fall back to standard routing now.
    """
    value = getattr(error, "retry_after", None)
    if value is None:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                value = headers.get("retry-after")
            except Exception:  # pragma: no cover - exotic header mapping
                value = None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds:  # NaN compares false against every bound below
        return None
    if seconds <= 0 or seconds > MAX_HONORED_RETRY_AFTER_SECONDS:
        return None
    return seconds


@dataclass
class Deadline:
    """A monotonic turn budget that every attempt and sleep is clipped to."""

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
    """The timeout for the next attempt: the per-attempt cap or what is left."""
    return min(attempt_timeout_ms / 1000.0, deadline.remaining())


def backoff_delay(
    attempt: int,
    deadline: Deadline,
    *,
    retry_after: Optional[float] = None,
    jitter: Optional[Callable[[float, float], float]] = None,
) -> float:
    """Return the sleep before ``attempt`` (1-based), clipped to the budget.

    ``retry_after`` wins when the server sent a bounded one, because the server
    knows something we do not. Otherwise the schedule is 100 ms then 200 ms,
    jittered so a wave of turns does not retry in lockstep.
    """
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
    """One user's circuit. Holds no key and no request content."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    last_seen: float = 0.0
    auth_blocked_fingerprint: Optional[str] = None


class UserCircuit:
    """Per-user circuit breaker over turn-level routing outcomes.

    Only turn-level results move it. A single retried attempt inside a turn
    that ultimately succeeded is not a failure: the user got their narrowed
    round, which is the thing the circuit exists to protect.
    """

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
        """True when this turn may attempt routing.

        A half-open trial is allowed exactly once per cool-down: the entry moves
        to ``HALF_OPEN`` here, and the next call sees it as open again unless an
        outcome resolved it.
        """
        self._evict_idle()
        entry = self._entry(user_id)
        entry.last_seen = self._clock()

        if entry.auth_blocked_fingerprint is not None:
            if fingerprint is not None and fingerprint != entry.auth_blocked_fingerprint:
                # A different key. The block was about the old one.
                entry.auth_blocked_fingerprint = None
                entry.state = CircuitState.CLOSED
                entry.consecutive_failures = 0
                return True
            return False

        if entry.state is CircuitState.CLOSED:
            return True
        if entry.state is CircuitState.HALF_OPEN:
            # A trial is already in flight for this cool-down.
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
        """Count one turn-level fallback; open the circuit at the threshold."""
        entry = self._entry(user_id)
        entry.last_seen = self._clock()
        if entry.state is CircuitState.HALF_OPEN:
            # The trial failed: straight back to open with a fresh cool-down.
            entry.state = CircuitState.OPEN
            entry.opened_at = self._clock()
            return entry.state
        entry.consecutive_failures += 1
        if entry.consecutive_failures >= self._failure_threshold:
            entry.state = CircuitState.OPEN
            entry.opened_at = self._clock()
        return entry.state

    def record_auth_failure(self, user_id: str, fingerprint: Optional[str]) -> None:
        """Block this key until the fingerprint changes.

        Without a fingerprint there is nothing to unblock against, so the entry
        falls back to an ordinary open circuit that the cool-down will retry.
        """
        entry = self._entry(user_id)
        entry.last_seen = self._clock()
        entry.state = CircuitState.OPEN
        entry.opened_at = self._clock()
        if fingerprint is None:
            entry.consecutive_failures = self._failure_threshold
            return
        entry.auth_blocked_fingerprint = fingerprint

    def reset(self, user_id: str) -> None:
        """Forget a user's circuit. Saving or clearing a key calls this."""
        self._entries.pop(user_id, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:  # pragma: no cover - diagnostic
        return len(self._entries)


_circuit: Optional[UserCircuit] = None


def circuit() -> UserCircuit:
    """The process-wide per-user circuit."""
    global _circuit
    if _circuit is None:
        _circuit = UserCircuit()
    return _circuit


def set_circuit(replacement: Optional[UserCircuit]) -> None:
    """Install a circuit (or clear it). Tests use this."""
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
