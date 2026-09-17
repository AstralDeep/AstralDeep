"""Feature 089 (T011): the attempt schedule, the deadline, and the circuit.

Everything here runs on a fake clock. A timing test that sleeps is a timing
test that is flaky on a loaded machine, and the property being checked --
"the total can never exceed the budget" -- is about arithmetic, not about
wall clock.
"""

from __future__ import annotations

import pytest

from orchestrator.typesafe_routing.budget import (
    BACKOFF_MS,
    CIRCUIT_COOLDOWN_SECONDS,
    CIRCUIT_FAILURE_THRESHOLD,
    MAX_ATTEMPTS,
    MAX_HONORED_RETRY_AFTER_SECONDS,
    TURN_BUDGET_SECONDS,
    CircuitState,
    Deadline,
    Outcome,
    UserCircuit,
    attempt_timeout,
    backoff_delay,
    is_auth_failure,
    is_transient,
    retry_after_seconds,
)
from tests.fakes.typesafe_fake import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeInternalServerError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
)

FINGERPRINT = "0123456789ab"
OTHER_FINGERPRINT = "ba9876543210"


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- error classification ------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        TypeSafeAPITimeoutError(),
        TypeSafeAPIConnectionError(),
        TypeSafeRateLimitError(),
        TypeSafeInternalServerError(),
        TypeSafeAPIError(status_code=408),
        TypeSafeAPIError(status_code=503),
        TypeSafeAPIError(status_code=529),
    ],
)
def test_transient_errors_are_retried(error: BaseException) -> None:
    assert is_transient(error) is True


@pytest.mark.parametrize(
    "error",
    [
        TypeSafeAuthenticationError(),
        TypeSafePermissionDeniedError(),
        TypeSafeUnprocessableEntityError(),
        TypeSafeAPIResponseValidationError(),
        TypeSafeAPIError(status_code=400),
        TypeSafeAPIError(status_code=404),
        ValueError("something unrecognized"),
    ],
)
def test_non_transient_errors_are_not_retried(error: BaseException) -> None:
    assert is_transient(error) is False


def test_an_unrecognized_error_is_treated_as_non_transient() -> None:
    """Retrying something we do not understand spends the user's latency on a guess."""

    class Mystery(Exception):
        pass

    assert is_transient(Mystery()) is False


@pytest.mark.parametrize(
    "error", [TypeSafeAuthenticationError(), TypeSafePermissionDeniedError()]
)
def test_auth_failures_are_recognized(error: BaseException) -> None:
    assert is_auth_failure(error) is True


@pytest.mark.parametrize(
    "error", [TypeSafeAPITimeoutError(), TypeSafeRateLimitError(), TypeSafeAPIError(status_code=500)]
)
def test_other_failures_are_not_auth_failures(error: BaseException) -> None:
    assert is_auth_failure(error) is False


def test_classification_never_reads_the_message() -> None:
    """A message can contain the user's request; the policy must not depend on it."""
    benign = TypeSafeAPIError("timeout connection rate limit", status_code=400)
    assert is_transient(benign) is False


# -- Retry-After ---------------------------------------------------------


def test_a_short_retry_after_is_honored() -> None:
    assert retry_after_seconds(TypeSafeRateLimitError(retry_after=0.25)) == 0.25


def test_a_long_retry_after_is_ignored_rather_than_clamped() -> None:
    """A server asking for 30 seconds is telling us to fall back, not to wait."""
    assert retry_after_seconds(TypeSafeRateLimitError(retry_after=30.0)) is None


@pytest.mark.parametrize("value", [None, 0, -1, "soon", float("nan")])
def test_a_missing_or_nonsense_retry_after_is_ignored(value: object) -> None:
    assert retry_after_seconds(TypeSafeRateLimitError(retry_after=value)) is None


def test_the_retry_after_bound_is_inside_the_turn_budget() -> None:
    assert MAX_HONORED_RETRY_AFTER_SECONDS < TURN_BUDGET_SECONDS


# -- deadline ------------------------------------------------------------


def test_the_deadline_counts_down_monotonically() -> None:
    clock = Clock()
    deadline = Deadline(total=1.5, clock=clock)
    assert deadline.remaining() == pytest.approx(1.5)
    clock.advance(0.6)
    assert deadline.remaining() == pytest.approx(0.9)
    assert deadline.elapsed() == pytest.approx(0.6)
    assert deadline.expired is False
    clock.advance(1.0)
    assert deadline.remaining() == 0.0
    assert deadline.expired is True


def test_an_attempt_timeout_is_clipped_to_the_remaining_budget() -> None:
    clock = Clock()
    deadline = Deadline(total=1.5, clock=clock)
    assert attempt_timeout(deadline, 600) == pytest.approx(0.6)
    clock.advance(1.3)
    # Only 0.2s left: the per-attempt cap does not get to exceed it.
    assert attempt_timeout(deadline, 600) == pytest.approx(0.2)
    clock.advance(0.5)
    assert attempt_timeout(deadline, 600) == 0.0


def test_a_slow_first_attempt_cannot_buy_the_third_extra_time() -> None:
    clock = Clock()
    deadline = Deadline(total=1.5, clock=clock)
    spent = 0.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        timeout = attempt_timeout(deadline, 600)
        clock.advance(timeout)
        spent += timeout
        delay = backoff_delay(attempt + 1, deadline, jitter=lambda lo, hi: 0.0)
        clock.advance(delay)
        spent += delay
    assert spent <= TURN_BUDGET_SECONDS + 1e-9
    assert deadline.remaining() == 0.0


# -- backoff -------------------------------------------------------------


def test_the_first_attempt_has_no_backoff() -> None:
    assert backoff_delay(1, Deadline(clock=Clock())) == 0.0


def test_the_backoff_schedule_is_100ms_then_200ms() -> None:
    deadline = Deadline(clock=Clock())

    def zero(low: float, high: float) -> float:
        return 0.0

    assert backoff_delay(2, deadline, jitter=zero) == pytest.approx(BACKOFF_MS[0] / 1000)
    assert backoff_delay(3, deadline, jitter=zero) == pytest.approx(BACKOFF_MS[1] / 1000)


def test_backoff_jitter_stays_within_twenty_percent() -> None:
    deadline = Deadline(clock=Clock())
    for _ in range(200):
        delay = backoff_delay(2, deadline)
        assert 0.08 - 1e-9 <= delay <= 0.12 + 1e-9


def test_backoff_is_clipped_to_the_remaining_budget() -> None:
    clock = Clock()
    deadline = Deadline(total=1.5, clock=clock)
    clock.advance(1.45)
    assert backoff_delay(3, deadline, jitter=lambda lo, hi: 0.0) == pytest.approx(0.05)
    clock.advance(0.1)
    assert backoff_delay(3, deadline) == 0.0


def test_a_bounded_retry_after_overrides_the_schedule() -> None:
    deadline = Deadline(clock=Clock())
    assert backoff_delay(2, deadline, retry_after=0.4) == pytest.approx(0.4)


# -- circuit -------------------------------------------------------------


def test_a_fresh_circuit_allows_routing() -> None:
    circuit = UserCircuit(clock=Clock())
    assert circuit.allows("u1") is True
    assert circuit.state_of("u1") is CircuitState.CLOSED


def test_the_circuit_opens_after_three_consecutive_fallbacks() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD - 1):
        circuit.record_failure("u1")
        assert circuit.allows("u1") is True
    circuit.record_failure("u1")
    assert circuit.state_of("u1") is CircuitState.OPEN
    assert circuit.allows("u1") is False


def test_a_success_resets_the_failure_count() -> None:
    circuit = UserCircuit(clock=Clock())
    circuit.record_failure("u1")
    circuit.record_failure("u1")
    circuit.record_success("u1")
    circuit.record_failure("u1")
    assert circuit.allows("u1") is True


def test_the_circuit_half_opens_after_the_cooldown() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        circuit.record_failure("u1")
    assert circuit.allows("u1") is False

    clock.advance(CIRCUIT_COOLDOWN_SECONDS)
    assert circuit.allows("u1") is True
    assert circuit.state_of("u1") is CircuitState.HALF_OPEN
    # Only one trial per cool-down.
    assert circuit.allows("u1") is False


def test_a_failed_half_open_trial_reopens_with_a_fresh_cooldown() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        circuit.record_failure("u1")
    clock.advance(CIRCUIT_COOLDOWN_SECONDS)
    circuit.allows("u1")

    circuit.record_failure("u1")

    assert circuit.state_of("u1") is CircuitState.OPEN
    clock.advance(CIRCUIT_COOLDOWN_SECONDS - 1)
    assert circuit.allows("u1") is False


def test_a_successful_half_open_trial_closes_the_circuit() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        circuit.record_failure("u1")
    clock.advance(CIRCUIT_COOLDOWN_SECONDS)
    circuit.allows("u1")

    circuit.record_success("u1")

    assert circuit.state_of("u1") is CircuitState.CLOSED
    assert circuit.allows("u1") is True


def test_the_circuit_is_per_user() -> None:
    circuit = UserCircuit(clock=Clock())
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        circuit.record_failure("noisy")
    assert circuit.allows("noisy") is False
    assert circuit.allows("quiet") is True


def test_an_auth_failure_blocks_until_the_fingerprint_changes() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    circuit.record_auth_failure("u1", FINGERPRINT)

    assert circuit.allows("u1", fingerprint=FINGERPRINT) is False
    # A cool-down does not help: the key itself is rejected.
    clock.advance(CIRCUIT_COOLDOWN_SECONDS * 10)
    assert circuit.allows("u1", fingerprint=FINGERPRINT) is False

    assert circuit.allows("u1", fingerprint=OTHER_FINGERPRINT) is True
    assert circuit.state_of("u1") is CircuitState.CLOSED


def test_an_auth_failure_without_a_fingerprint_falls_back_to_a_cooldown() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    circuit.record_auth_failure("u1", None)
    assert circuit.allows("u1") is False
    clock.advance(CIRCUIT_COOLDOWN_SECONDS)
    assert circuit.allows("u1") is True


def test_resetting_clears_one_users_circuit() -> None:
    circuit = UserCircuit(clock=Clock())
    circuit.record_auth_failure("u1", FINGERPRINT)
    circuit.reset("u1")
    assert circuit.allows("u1", fingerprint=FINGERPRINT) is True


def test_idle_users_are_evicted() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock, idle_eviction=60.0)
    circuit.record_failure("u1")
    assert len(circuit) == 1
    clock.advance(61.0)
    circuit.allows("u2")
    assert "u1" not in circuit._entries  # noqa: SLF001 - eviction is the subject


def test_circuit_entries_hold_no_key_material() -> None:
    circuit = UserCircuit(clock=Clock())
    circuit.record_auth_failure("u1", FINGERPRINT)
    rendered = repr(circuit._entries["u1"])  # noqa: SLF001 - the subject
    # The fingerprint is not key material, but nothing else may be there.
    assert "ts_" not in rendered
    assert "api_key" not in rendered


def test_every_outcome_label_is_a_plain_identifier() -> None:
    """Outcomes become metric labels, so they must carry no content."""
    for outcome in Outcome:
        assert outcome.value.replace("_", "").isalnum()
