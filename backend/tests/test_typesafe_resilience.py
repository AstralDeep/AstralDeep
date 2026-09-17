"""Feature 089 (T029): the owner's failure sequence, measured.

The requirement is a sequence, not a tolerance: notice, up to three attempts
inside 1.5 s, fall back to standard routing, and stop making the user wait for
a service that is not answering.

These tests run the real runner against the deterministic fake with a fake
clock, so "the total can never exceed the budget" is checked as arithmetic
rather than as wall time. A timing test that sleeps is a timing test that is
flaky on a loaded machine.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.typesafe_routing.budget import (  # noqa: E402
    CIRCUIT_COOLDOWN_SECONDS,
    CIRCUIT_FAILURE_THRESHOLD,
    TURN_BUDGET_SECONDS,
    CircuitState,
    Outcome,
    UserCircuit,
)
from orchestrator.typesafe_routing.questions import (  # noqa: E402
    AgentOption,
    RoutingRequest,
    ToolOption,
)
from orchestrator.typesafe_routing.runner import (  # noqa: E402
    NOTICE_FALLBACK,
    NOTICE_RETRYING,
    await_decision,
    cancel_routing,
    start_routing,
)
from tests.fakes.typesafe_fake import (  # noqa: E402
    FakeTypeSafeClient,
    TypeSafeAPIConnectionError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeInternalServerError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
    high_confidence,
)

KEY = "ts_live_CANARY0000NOTAREALKEY000000"
FINGERPRINT = "0123456789ab"
USER = "resilience-user"
AGENT = "weather-1"
TOOL = "weather-1__get_current_weather"


class Clock:
    """A monotonic clock the test advances, including through sleeps."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Notices:
    """Collects everything the adapter said to the user."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, status: str, message: str) -> None:
        self.sent.append((status, message))

    @property
    def statuses(self) -> list[str]:
        return [status for status, _ in self.sent]


def _request() -> RoutingRequest:
    return RoutingRequest.build(
        current_request="What's the weather in Lexington right now?",
        agents=[AgentOption(AGENT, "Weather", "Live conditions")],
        tools_by_agent={AGENT: [ToolOption(TOOL, "Current conditions")]},
    )


async def _run(
    *,
    faults=(),
    latency: float = 0.0,
    circuit: UserCircuit | None = None,
    clock: Clock | None = None,
    notices: Notices | None = None,
    api_key: str | None = KEY,
    flags=None,
):
    clock = clock or Clock()
    notices = notices or Notices()
    circuit = circuit if circuit is not None else UserCircuit(clock=clock)
    fake = FakeTypeSafeClient(
        default=high_confidence(AGENT, TOOL),
        faults=list(faults),
        latency=latency,
        sleep=clock.sleep,
    )
    task = await start_routing(
        user_id=USER,
        request=_request(),
        api_key=api_key,
        fingerprint=FINGERPRINT,
        notifier=notices,
        client=fake,
        circuit=circuit,
        clock=clock,
        sleep=clock.sleep,
        flags=flags,
    )
    outcome = await await_decision(task, user_id=USER, circuit=circuit)
    return outcome, fake, notices, clock, circuit


# -- the happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clean_call_succeeds_with_no_notices() -> None:
    outcome, fake, notices, _clock, circuit = await _run()
    assert outcome.outcome is Outcome.SUCCESS
    assert outcome.decision is not None
    assert fake.call_count == 1
    assert notices.sent == []
    assert circuit.state_of(USER) is CircuitState.CLOSED


# -- transient failures ---------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        TypeSafeAPITimeoutError,
        TypeSafeAPIConnectionError,
        TypeSafeRateLimitError,
        TypeSafeInternalServerError,
    ],
)
@pytest.mark.asyncio
async def test_every_transient_class_is_retried_up_to_three_attempts(error) -> None:
    outcome, fake, notices, _clock, _circuit = await _run(faults=[error, error, error])
    assert fake.call_count == 3
    assert outcome.outcome in (Outcome.FALLBACK_TRANSIENT, Outcome.FALLBACK_DEADLINE)
    assert outcome.decision is None


@pytest.mark.asyncio
async def test_a_transient_failure_that_then_succeeds_needs_no_fallback() -> None:
    outcome, fake, notices, _clock, _circuit = await _run(
        faults=[TypeSafeAPITimeoutError, None]
    )
    assert fake.call_count == 2
    assert outcome.outcome is Outcome.SUCCESS
    assert NOTICE_RETRYING in [m for _s, m in notices.sent]
    assert NOTICE_FALLBACK not in [m for _s, m in notices.sent]


@pytest.mark.asyncio
async def test_the_backoff_schedule_is_100ms_then_200ms() -> None:
    _outcome, _fake, _notices, clock, _circuit = await _run(
        faults=[TypeSafeAPITimeoutError, TypeSafeAPITimeoutError, TypeSafeAPITimeoutError]
    )
    assert len(clock.sleeps) == 2
    assert 0.08 <= clock.sleeps[0] <= 0.12
    assert 0.16 <= clock.sleeps[1] <= 0.24


# -- non-transient failures -----------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        TypeSafeUnprocessableEntityError,
        TypeSafeAPIResponseValidationError,
    ],
)
@pytest.mark.asyncio
async def test_non_transient_classes_are_not_retried(error) -> None:
    outcome, fake, _notices, _clock, _circuit = await _run(faults=[error, None, None])
    assert fake.call_count == 1
    assert outcome.outcome is Outcome.FALLBACK_NONTRANSIENT


@pytest.mark.parametrize(
    "error", [TypeSafeAuthenticationError, TypeSafePermissionDeniedError]
)
@pytest.mark.asyncio
async def test_an_auth_failure_is_not_retried_and_blocks_the_key(error) -> None:
    outcome, fake, _notices, _clock, circuit = await _run(faults=[error, None, None])
    assert fake.call_count == 1
    assert outcome.outcome is Outcome.FALLBACK_NONTRANSIENT
    assert outcome.credential_outcome == "rejected"
    # Blocked by fingerprint: a cool-down will not unblock it, a new key will.
    assert circuit.allows(USER, fingerprint=FINGERPRINT) is False
    assert circuit.allows(USER, fingerprint="ffffffffffff") is True


# -- notices --------------------------------------------------------------


@pytest.mark.asyncio
async def test_exactly_one_retrying_notice_and_one_fallback_notice() -> None:
    _outcome, _fake, notices, _clock, _circuit = await _run(
        faults=[TypeSafeAPITimeoutError, TypeSafeAPITimeoutError, TypeSafeAPITimeoutError]
    )
    messages = [m for _s, m in notices.sent]
    assert messages.count(NOTICE_RETRYING) == 1
    assert messages.count(NOTICE_FALLBACK) == 1
    assert notices.statuses == ["retrying", "info"]


@pytest.mark.asyncio
async def test_a_non_transient_failure_sends_only_the_fallback_notice() -> None:
    _outcome, _fake, notices, _clock, _circuit = await _run(
        faults=[TypeSafeUnprocessableEntityError]
    )
    assert [m for _s, m in notices.sent] == [NOTICE_FALLBACK]


@pytest.mark.asyncio
async def test_notices_carry_no_request_text_or_error_detail() -> None:
    _outcome, _fake, notices, _clock, _circuit = await _run(
        faults=[TypeSafeAPITimeoutError("Lexington weather blew up")]
    )
    for _status, message in notices.sent:
        assert "Lexington" not in message
        assert "blew up" not in message


@pytest.mark.asyncio
async def test_a_dead_socket_does_not_break_the_turn() -> None:
    class Dead:
        async def __call__(self, status: str, message: str) -> None:
            raise RuntimeError("socket closed")

    clock = Clock()
    fake = FakeTypeSafeClient(
        default=high_confidence(AGENT, TOOL),
        faults=[TypeSafeAPITimeoutError, None],
        sleep=clock.sleep,
    )
    task = await start_routing(
        user_id=USER,
        request=_request(),
        api_key=KEY,
        fingerprint=FINGERPRINT,
        notifier=Dead(),
        client=fake,
        circuit=UserCircuit(clock=clock),
        clock=clock,
        sleep=clock.sleep,
    )
    outcome = await await_decision(task, user_id=USER)
    assert outcome.outcome is Outcome.SUCCESS


# -- the budget -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_total_never_exceeds_the_turn_budget() -> None:
    clock = Clock()
    start = clock.now
    # Every attempt burns its full per-attempt timeout before failing.
    await _run(
        faults=[0.4, 0.4, 0.4],
        clock=clock,
    )
    assert clock.now - start <= TURN_BUDGET_SECONDS + 1e-6


@pytest.mark.asyncio
async def test_a_slow_attempt_is_abandoned_and_retried() -> None:
    """An attempt that outruns its per-attempt timeout does not get to finish.

    The first call would take 1.6 s, well past the whole turn budget. It is cut
    at the per-attempt timeout, and the retry answers immediately, so the user
    still gets a narrowed round instead of paying for a stalled call.
    """
    clock = Clock()
    start = clock.now
    outcome, fake, _notices, _clock, _circuit = await _run(faults=[1.6], clock=clock)

    assert fake.call_count == 2
    assert outcome.outcome is Outcome.SUCCESS
    assert clock.now - start <= TURN_BUDGET_SECONDS + 1e-6


@pytest.mark.asyncio
async def test_every_attempt_timing_out_ends_in_fallback_inside_the_budget() -> None:
    clock = Clock()
    start = clock.now
    outcome, fake, notices, _clock, _circuit = await _run(
        faults=[1.6, 1.6, 1.6], clock=clock
    )

    assert fake.call_count == 3
    assert outcome.decision is None
    assert outcome.outcome in (Outcome.FALLBACK_TRANSIENT, Outcome.FALLBACK_DEADLINE)
    assert NOTICE_FALLBACK in [m for _s, m in notices.sent]
    assert clock.now - start <= TURN_BUDGET_SECONDS + 1e-6


# -- the circuit ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_circuit_opens_after_three_consecutive_fallback_turns() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        await _run(faults=[TypeSafeUnprocessableEntityError], circuit=circuit, clock=clock)
    assert circuit.state_of(USER) is CircuitState.OPEN


@pytest.mark.asyncio
async def test_an_open_circuit_skips_the_call_entirely() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        await _run(faults=[TypeSafeUnprocessableEntityError], circuit=circuit, clock=clock)

    before = clock.now
    outcome, fake, _notices, _clock, _circuit = await _run(circuit=circuit, clock=clock)

    assert outcome.outcome is Outcome.SKIPPED_CIRCUIT
    assert fake.call_count == 0
    # SC-004: an open circuit costs essentially nothing.
    assert clock.now == before


@pytest.mark.asyncio
async def test_the_circuit_half_opens_after_the_cooldown() -> None:
    clock = Clock()
    circuit = UserCircuit(clock=clock)
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        await _run(faults=[TypeSafeUnprocessableEntityError], circuit=circuit, clock=clock)
    clock.now += CIRCUIT_COOLDOWN_SECONDS

    outcome, fake, _notices, _clock, _circuit = await _run(circuit=circuit, clock=clock)

    assert fake.call_count == 1
    assert outcome.outcome is Outcome.SUCCESS
    assert circuit.state_of(USER) is CircuitState.CLOSED


# -- the skip paths -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_key_makes_no_call() -> None:
    outcome, fake, notices, _clock, _circuit = await _run(api_key=None)
    assert outcome.outcome is Outcome.SKIPPED_NO_KEY
    assert fake.call_count == 0
    assert notices.sent == []


@pytest.mark.asyncio
async def test_the_kill_switch_makes_no_call() -> None:
    class Off:
        @staticmethod
        def is_enabled(name: str) -> bool:
            return False

    outcome, fake, notices, _clock, _circuit = await _run(flags=Off())
    assert outcome.outcome is Outcome.SKIPPED_DISABLED
    assert fake.call_count == 0
    assert notices.sent == []


@pytest.mark.asyncio
async def test_an_empty_request_makes_no_call() -> None:
    clock = Clock()
    fake = FakeTypeSafeClient(default=high_confidence(AGENT, TOOL), sleep=clock.sleep)
    task = await start_routing(
        user_id=USER,
        request=RoutingRequest.build(current_request="hi"),
        api_key=KEY,
        fingerprint=FINGERPRINT,
        client=fake,
        circuit=UserCircuit(clock=clock),
        clock=clock,
        sleep=clock.sleep,
    )
    outcome = await await_decision(task, user_id=USER)
    assert outcome.outcome is Outcome.SKIPPED_NO_KEY
    assert fake.call_count == 0


# -- cancellation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_a_routing_task_is_safe() -> None:
    clock = Clock()
    fake = FakeTypeSafeClient(
        default=high_confidence(AGENT, TOOL), faults=[5.0], sleep=asyncio.sleep
    )
    task = await start_routing(
        user_id=USER,
        request=_request(),
        api_key=KEY,
        fingerprint=FINGERPRINT,
        client=fake,
        circuit=UserCircuit(clock=clock),
        clock=clock,
    )
    cancel_routing(task)
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    # Cancelling twice, or cancelling a finished task, is not an error.
    cancel_routing(task)
    cancel_routing(None)


@pytest.mark.asyncio
async def test_await_decision_on_a_missing_task_is_a_skip() -> None:
    outcome = await await_decision(None, user_id=USER)
    assert outcome.outcome is Outcome.SKIPPED_NO_KEY
    assert outcome.decision is None


# -- nothing ever raises --------------------------------------------------


@pytest.mark.asyncio
async def test_an_unexpected_error_class_still_produces_an_outcome() -> None:
    class Bizarre(Exception):
        pass

    outcome, _fake, _notices, _clock, _circuit = await _run(faults=[Bizarre])
    assert outcome.outcome is Outcome.FALLBACK_NONTRANSIENT
    assert outcome.decision is None


@pytest.mark.asyncio
async def test_a_malformed_response_produces_no_decision_rather_than_an_error() -> None:
    clock = Clock()
    fake = FakeTypeSafeClient(answers={}, default=None, sleep=clock.sleep)
    task = await start_routing(
        user_id=USER,
        request=_request(),
        api_key=KEY,
        fingerprint=FINGERPRINT,
        client=fake,
        circuit=UserCircuit(clock=clock),
        clock=clock,
        sleep=clock.sleep,
    )
    outcome = await await_decision(task, user_id=USER)
    assert outcome.decision is None
    assert outcome.outcome is not Outcome.SUCCESS
