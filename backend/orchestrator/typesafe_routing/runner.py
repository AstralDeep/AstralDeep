"""The turn-facing entry points: start a routing task, await it, give up (feature 089).

This is where the pieces meet. :func:`start_routing` returns immediately with a
task so the call overlaps prompt preparation -- the overlap is most of why a
routing call can fit inside a turn at all. :func:`await_decision` collects it
against what is left of the budget and cancels it when the budget is gone.

The contract both halves keep is that a turn is never harmed by routing:

* Neither function raises. Every failure path produces ``None``.
* The task is cancellable, and a cancelled task produces ``None``.
* Awaiting costs at most the remaining budget, and an open circuit costs
  essentially nothing.
* The user hears at most one "retrying" notice and one "standard routing"
  notice per turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Optional, Protocol

from .budget import (
    MAX_ATTEMPTS,
    TURN_BUDGET_SECONDS,
    CircuitState,
    Deadline,
    Outcome,
    UserCircuit,
    attempt_timeout,
    backoff_delay,
    circuit as default_circuit,
    is_auth_failure,
    is_transient,
    retry_after_seconds,
)
from .client import (
    ATTEMPT_TIMEOUT_MS,
    TypeSafeAdapterClient,
    TypeSafeUnavailable,
    adapter_client as default_adapter_client,
)
from .decision import RoutingDecision, parse_decision
from .questions import (
    ROUTING_MAX_REQUEST_CHARS,
    QuestionSet,
    RoutingRequest,
    build_questions,
)

logger = logging.getLogger("Orchestrator.TypeSafe.Runner")

#: Operations kill switch. Default ON. When OFF every user takes the no-key
#: path with zero network calls. It is a switch, not a rollout mechanism: there
#: is no percentage and no per-user targeting, because a routing decision that
#: depends on which bucket a user landed in is not reproducible.
FEATURE_FLAG = "typesafe_routing"

NOTICE_RETRYING = "Smart routing is slow to respond — retrying…"
NOTICE_FALLBACK = "Using standard routing for this request."


class RoutingNotifier(Protocol):
    """How the adapter talks to the user. The only channel it has."""

    async def __call__(self, status: str, message: str) -> None: ...  # pragma: no cover


class _NullNotifier:
    """Used when a caller has no socket, such as a background turn."""

    async def __call__(self, status: str, message: str) -> None:
        return None


@dataclass
class RoutingOutcome:
    """The result of one turn's routing, decision plus bookkeeping.

    The decision is what the turn uses; everything else is what the metrics and
    the credential store need. It carries no request text and no key.
    """

    decision: Optional[RoutingDecision] = None
    outcome: Outcome = Outcome.SKIPPED_NO_KEY
    attempts: int = 0
    elapsed_ms: int = 0
    credential_outcome: Optional[str] = None
    fingerprint: Optional[str] = None


def routing_enabled(flags: Any = None) -> bool:
    """True when the kill switch allows routing."""
    if flags is None:
        try:
            from shared.feature_flags import flags as global_flags
        except ImportError:  # pragma: no cover - the module is always present
            return True
        flags = global_flags
    try:
        return bool(flags.is_enabled(FEATURE_FLAG))
    except Exception:  # pragma: no cover - a broken flag must not block a turn
        return True


@dataclass
class _Attempt:
    """One pass over the client, kept separate so the loop reads as a schedule."""

    client: TypeSafeAdapterClient
    api_key: str = field(repr=False)
    request: RoutingRequest
    question_set: QuestionSet
    attempt_timeout_ms: int = ATTEMPT_TIMEOUT_MS

    async def run(self, deadline: Deadline) -> Any:
        timeout = attempt_timeout(deadline, self.attempt_timeout_ms)
        if timeout <= 0:
            raise asyncio.TimeoutError
        return await self.client.system_one(
            api_key=self.api_key,
            state=self.request.state(),
            questions=self.question_set.questions,
            timeout=timeout,
        )


async def _route(
    *,
    user_id: str,
    api_key: str,
    fingerprint: Optional[str],
    request: RoutingRequest,
    notifier: RoutingNotifier,
    client: TypeSafeAdapterClient,
    circuit: UserCircuit,
    deadline: Deadline,
    attempt_timeout_ms: int,
    sleep: Callable[[float], Any],
) -> RoutingOutcome:
    """Run the attempt schedule. Never raises except :class:`asyncio.CancelledError`."""
    try:
        from .client import load_sdk

        sdk = client._surface()  # noqa: SLF001 - the surface is the adapter's own
        del load_sdk
    except TypeSafeUnavailable:
        return RoutingOutcome(outcome=Outcome.SKIPPED_UNAVAILABLE)
    except Exception:  # pragma: no cover - an unexpected SDK shape
        logger.debug("TypeSafe SDK surface unavailable", exc_info=True)
        return RoutingOutcome(outcome=Outcome.SKIPPED_UNAVAILABLE)

    try:
        question_set = build_questions(request, sdk)
    except Exception:
        logger.debug("building the routing question set failed", exc_info=True)
        return RoutingOutcome(outcome=Outcome.SKIPPED_UNAVAILABLE)

    attempt_runner = _Attempt(
        client=client,
        api_key=api_key,
        request=request,
        question_set=question_set,
        attempt_timeout_ms=attempt_timeout_ms,
    )

    notified_retry = False
    attempts = 0
    last_error: Optional[BaseException] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if deadline.expired:
            break
        attempts = attempt
        try:
            response = await attempt_runner.run(deadline)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - classification happens below
            last_error = error
            if is_auth_failure(error):
                circuit.record_auth_failure(user_id, fingerprint)
                await _notify_once(notifier, "info", NOTICE_FALLBACK)
                return RoutingOutcome(
                    outcome=Outcome.FALLBACK_NONTRANSIENT,
                    attempts=attempts,
                    elapsed_ms=int(deadline.elapsed() * 1000),
                    credential_outcome="rejected",
                    fingerprint=fingerprint,
                )
            if not is_transient(error):
                await _notify_once(notifier, "info", NOTICE_FALLBACK)
                return RoutingOutcome(
                    outcome=Outcome.FALLBACK_NONTRANSIENT,
                    attempts=attempts,
                    elapsed_ms=int(deadline.elapsed() * 1000),
                    fingerprint=fingerprint,
                )
            if attempt < MAX_ATTEMPTS and not deadline.expired:
                if not notified_retry:
                    notified_retry = True
                    await _notify_once(notifier, "retrying", NOTICE_RETRYING)
                delay = backoff_delay(
                    attempt + 1, deadline, retry_after=retry_after_seconds(error)
                )
                if delay > 0:
                    await sleep(delay)
                continue
            break
        else:
            decision = parse_decision(response, question_set)
            return RoutingOutcome(
                decision=decision,
                outcome=Outcome.SUCCESS,
                attempts=attempts,
                elapsed_ms=int(deadline.elapsed() * 1000),
                credential_outcome="valid",
                fingerprint=fingerprint,
            )

    await _notify_once(notifier, "info", NOTICE_FALLBACK)
    expired = deadline.expired and last_error is None
    return RoutingOutcome(
        outcome=Outcome.FALLBACK_DEADLINE if expired else Outcome.FALLBACK_TRANSIENT,
        attempts=attempts,
        elapsed_ms=int(deadline.elapsed() * 1000),
        credential_outcome="unavailable",
        fingerprint=fingerprint,
    )


async def _notify_once(notifier: RoutingNotifier, status: str, message: str) -> None:
    """Send one notice, swallowing a dead socket."""
    try:
        await notifier(status, message)
    except Exception:  # pragma: no cover - a closed socket is not an error here
        logger.debug("routing notice delivery failed (non-fatal)", exc_info=True)


async def start_routing(
    *,
    user_id: str,
    request: RoutingRequest,
    api_key: Optional[str] = None,
    fingerprint: Optional[str] = None,
    notifier: Optional[RoutingNotifier] = None,
    client: Optional[TypeSafeAdapterClient] = None,
    circuit: Optional[UserCircuit] = None,
    flags: Any = None,
    budget_seconds: float = TURN_BUDGET_SECONDS,
    attempt_timeout_ms: int = ATTEMPT_TIMEOUT_MS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Optional[Callable[[float], Any]] = None,
) -> asyncio.Task:
    """Create the routing task and return immediately.

    The returned task always resolves to a :class:`RoutingOutcome`. It is
    created even for the skip paths, so the caller has exactly one shape to
    handle and one thing to cancel.
    """
    notifier = notifier or _NullNotifier()
    breaker = circuit if circuit is not None else default_circuit()

    def _skip(outcome: Outcome) -> asyncio.Task:
        async def _resolved() -> RoutingOutcome:
            return RoutingOutcome(outcome=outcome)

        return asyncio.ensure_future(_resolved())

    if not routing_enabled(flags):
        return _skip(Outcome.SKIPPED_DISABLED)
    if not api_key:
        return _skip(Outcome.SKIPPED_NO_KEY)
    if request.is_empty:
        return _skip(Outcome.SKIPPED_NO_KEY)
    if not breaker.allows(user_id, fingerprint=fingerprint):
        return _skip(Outcome.SKIPPED_CIRCUIT)

    deadline = Deadline(total=budget_seconds, clock=clock)
    runner = _route(
        user_id=user_id,
        api_key=api_key,
        fingerprint=fingerprint,
        request=request,
        notifier=notifier,
        client=client if client is not None else default_adapter_client(),
        circuit=breaker,
        deadline=deadline,
        attempt_timeout_ms=attempt_timeout_ms,
        sleep=sleep or asyncio.sleep,
    )
    task = asyncio.ensure_future(runner)
    task.astral_routing_deadline = deadline  # type: ignore[attr-defined]
    return task


async def await_decision(
    task: Optional[asyncio.Task],
    *,
    deadline: Optional[Deadline] = None,
    circuit: Optional[UserCircuit] = None,
    user_id: Optional[str] = None,
) -> RoutingOutcome:
    """Collect ``task`` within the remaining budget.

    On expiry the task is cancelled and the turn proceeds unnarrowed. The
    circuit is updated here rather than inside the task, because what the
    circuit counts is turn-level outcomes and only this function sees them all.
    """
    if task is None:
        return RoutingOutcome(outcome=Outcome.SKIPPED_NO_KEY)

    budget = deadline or getattr(task, "astral_routing_deadline", None)
    remaining = budget.remaining() if budget is not None else TURN_BUDGET_SECONDS

    try:
        if remaining <= 0 and not task.done():
            raise asyncio.TimeoutError
        outcome = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
    except (asyncio.TimeoutError, TimeoutError):
        task.cancel()
        outcome = RoutingOutcome(
            outcome=Outcome.FALLBACK_DEADLINE,
            elapsed_ms=int(budget.elapsed() * 1000) if budget is not None else 0,
            credential_outcome="unavailable",
        )
    except asyncio.CancelledError:
        # The turn was cancelled, not the budget. Propagate.
        raise
    except Exception:  # pragma: no cover - the task itself never raises
        logger.debug("routing task failed unexpectedly", exc_info=True)
        outcome = RoutingOutcome(outcome=Outcome.FALLBACK_NONTRANSIENT)

    breaker = circuit if circuit is not None else default_circuit()
    owner = user_id
    if owner is not None:
        if outcome.outcome is Outcome.SUCCESS:
            breaker.record_success(owner)
        elif outcome.outcome in (
            Outcome.FALLBACK_TRANSIENT,
            Outcome.FALLBACK_DEADLINE,
            Outcome.FALLBACK_NONTRANSIENT,
        ):
            breaker.record_failure(owner)

    return outcome


async def screen_instruction(
    *,
    user_id: str,
    text: str,
    api_key: Optional[str],
    fingerprint: Optional[str] = None,
    client: Optional[TypeSafeAdapterClient] = None,
    circuit: Optional[UserCircuit] = None,
    flags: Any = None,
    timeout_seconds: float = TURN_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
):
    """Security-only screen for a path that has no interactive turn (seam I7).

    The HTTP Work ``kind="chat"`` submission does not run through
    ``handle_chat_message``: it hands an instruction to a background executor
    with nobody watching. So it asks the three security questions and nothing
    else -- there is no round one to narrow and no canvas to arrange, and
    paying for the routing half would be spending the user's quota on answers
    nobody reads.

    Returns a :class:`Verdict`. Every failure path returns
    :attr:`Verdict.PASS`, because a screen that cannot reach its service must
    not become a way to block work.
    """
    from .decision import parse_security
    from .questions import build_security_question_set
    from .security_policy import Verdict, verdict_for

    if not api_key or not text or not routing_enabled(flags):
        return Verdict.PASS
    breaker = circuit if circuit is not None else default_circuit()
    if not breaker.allows(user_id, fingerprint=fingerprint):
        return Verdict.PASS

    adapter = client if client is not None else default_adapter_client()
    try:
        sdk = adapter._surface()  # noqa: SLF001 - the adapter's own surface
        question_set = build_security_question_set(sdk)
        deadline = Deadline(total=timeout_seconds, clock=clock)
        response = await adapter.system_one(
            api_key=api_key,
            state={"current_request": text[:ROUTING_MAX_REQUEST_CHARS]},
            questions=question_set.questions,
            timeout=attempt_timeout(deadline, ATTEMPT_TIMEOUT_MS),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("security-only screen unavailable (non-fatal)", exc_info=True)
        breaker.record_failure(user_id)
        return Verdict.PASS

    breaker.record_success(user_id)
    return verdict_for(parse_security(response))


def cancel_routing(task: Optional[asyncio.Task]) -> None:
    """Cancel a routing task. Safe with ``None`` and with a finished task."""
    if task is not None and not task.done():
        task.cancel()


__all__ = (
    "FEATURE_FLAG",
    "NOTICE_FALLBACK",
    "NOTICE_RETRYING",
    "CircuitState",
    "RoutingNotifier",
    "RoutingOutcome",
    "await_decision",
    "cancel_routing",
    "screen_instruction",
    "routing_enabled",
    "start_routing",
)
