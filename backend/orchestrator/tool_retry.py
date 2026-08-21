"""Retry semantics for LETS-governed physical effects.

The existing orchestrator retry loop predates protected-executor receipts.  A
single receipt is authority for one possible physical effect, so it must never
be reused for a second invocation.  This module deliberately separates:

* transport retries while obtaining one authorization (same operation id,
  nonce, and canonical request); and
* physical retries after an actuator returned a known retryable result (new
  operation id, nonce, and receipt).

An actuator whose non-idempotent result may have been lost is not retried.  It
is reported as ``outcome_uncertain`` for reconciliation or compensation.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar


T = TypeVar("T")
P = TypeVar("P")


class EffectSemantics(StrEnum):
    """Recovery posture declared by the host for one tool effect."""

    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"
    NON_IDEMPOTENT = "non_idempotent"


class ProtectedRetryError(RuntimeError):
    """Stable, content-free base error for the protected retry boundary."""

    code = "protected_retry_failed"

    def __init__(self, code: str | None = None) -> None:
        self.code = self.code if code is None else code
        super().__init__(self.code)


class PreResponseTransportError(ProtectedRetryError):
    """The authorization transport failed before a response was known."""

    code = "authorization_transport_unavailable"


class EffectResultLostError(ProtectedRetryError):
    """The actuator may have run, but its result is not known."""

    code = "effect_result_lost"


@dataclass(frozen=True, slots=True)
class PhysicalAttempt:
    """Stable identity for exactly one possible physical invocation."""

    operation_id: str
    nonce: str
    ordinal: int

    @classmethod
    def create(cls, ordinal: int) -> "PhysicalAttempt":
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError("physical attempt ordinal must be positive")
        return cls(
            operation_id=f"effect-{uuid.uuid4()}",
            nonce=secrets.token_hex(16),
            ordinal=ordinal,
        )


@dataclass(frozen=True, slots=True)
class ActuatorResult(Generic[T]):
    """Known response from one physical attempt."""

    value: T | None = None
    error_code: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or not self.error_code
        ):
            raise ValueError("actuator error code must be a nonempty string")
        if self.error_code is None and self.retryable:
            raise ValueError("a successful actuator result cannot be retryable")

    @property
    def succeeded(self) -> bool:
        return self.error_code is None


class ProtectedOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNCERTAIN = "outcome_uncertain"


@dataclass(frozen=True, slots=True)
class ProtectedOutcome(Generic[T]):
    """Terminal result of the bounded protected retry operation."""

    status: ProtectedOutcomeStatus
    attempt: PhysicalAttempt
    value: T | None = None
    error_code: str | None = None


Authorize = Callable[[PhysicalAttempt], P | Awaitable[P]]
Invoke = Callable[[PhysicalAttempt, P], ActuatorResult[T] | Awaitable[ActuatorResult[T]]]
Observe = Callable[[str, PhysicalAttempt], object | Awaitable[object]]


async def _resolve(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


class ProtectedToolRetrier(Generic[P, T]):
    """Run one governed effect with receipt-safe bounded retry semantics."""

    def __init__(
        self,
        *,
        physical_attempts: int,
        authorization_transport_attempts: int,
        backoff_seconds: tuple[float, ...] = (0.1, 0.25, 0.5),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if type(physical_attempts) is not int or not 1 <= physical_attempts <= 10:
            raise ValueError("physical attempts must be between 1 and 10")
        if (
            type(authorization_transport_attempts) is not int
            or not 1 <= authorization_transport_attempts <= 10
        ):
            raise ValueError("authorization transport attempts must be between 1 and 10")
        if not backoff_seconds or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            for value in backoff_seconds
        ):
            raise ValueError("retry backoff must contain non-negative delays")
        self._physical_attempts = physical_attempts
        self._transport_attempts = authorization_transport_attempts
        self._backoff = tuple(float(value) for value in backoff_seconds)
        self._sleep = sleep

    async def execute(
        self,
        *,
        semantics: EffectSemantics,
        authorize: Authorize[P],
        invoke: Invoke[P, T],
        observe: Observe | None = None,
    ) -> ProtectedOutcome[T]:
        """Authorize and invoke, never reusing a permit across actuations."""

        if not isinstance(semantics, EffectSemantics):
            raise TypeError("effect semantics must be declared")

        last: ProtectedOutcome[T] | None = None
        for ordinal in range(1, self._physical_attempts + 1):
            attempt = PhysicalAttempt.create(ordinal)
            if observe is not None:
                await _resolve(observe("attempt_created", attempt))

            permit = await self._authorize_same_intent(
                attempt=attempt,
                authorize=authorize,
                observe=observe,
            )
            if observe is not None:
                await _resolve(observe("authorization_committed", attempt))

            try:
                result = await _resolve(invoke(attempt, permit))
            except EffectResultLostError as exc:
                if observe is not None:
                    await _resolve(observe("outcome_uncertain", attempt))
                if semantics is EffectSemantics.IDEMPOTENT and ordinal < self._physical_attempts:
                    # Idempotency is a domain assertion.  The next possible
                    # invocation still receives new external authority.
                    await self._wait(ordinal)
                    continue
                return ProtectedOutcome(
                    status=ProtectedOutcomeStatus.OUTCOME_UNCERTAIN,
                    attempt=attempt,
                    error_code=exc.code,
                )

            if not isinstance(result, ActuatorResult):
                raise TypeError("actuator must return ActuatorResult")
            if result.succeeded:
                if observe is not None:
                    await _resolve(observe("effect_succeeded", attempt))
                return ProtectedOutcome(
                    status=ProtectedOutcomeStatus.SUCCEEDED,
                    attempt=attempt,
                    value=result.value,
                )

            last = ProtectedOutcome(
                status=ProtectedOutcomeStatus.FAILED,
                attempt=attempt,
                error_code=result.error_code,
            )
            if observe is not None:
                await _resolve(observe("effect_failed", attempt))
            if not result.retryable or ordinal >= self._physical_attempts:
                return last
            await self._wait(ordinal)

        if last is None:  # Defensive: constructor prevents a zero-attempt loop.
            raise ProtectedRetryError("protected_retry_exhausted")
        return last

    async def _authorize_same_intent(
        self,
        *,
        attempt: PhysicalAttempt,
        authorize: Authorize[P],
        observe: Observe | None,
    ) -> P:
        """Retry a no-response authorization with byte-equivalent identity."""

        for transport_ordinal in range(1, self._transport_attempts + 1):
            try:
                return await _resolve(authorize(attempt))
            except PreResponseTransportError:
                if observe is not None:
                    await _resolve(observe("authorization_transport_retry", attempt))
                if transport_ordinal >= self._transport_attempts:
                    raise
                await self._wait(transport_ordinal)
        raise ProtectedRetryError("authorization_transport_exhausted")

    async def _wait(self, ordinal: int) -> None:
        delay = self._backoff[min(ordinal - 1, len(self._backoff) - 1)]
        await self._sleep(delay)


__all__ = (
    "ActuatorResult",
    "EffectResultLostError",
    "EffectSemantics",
    "PhysicalAttempt",
    "PreResponseTransportError",
    "ProtectedOutcome",
    "ProtectedOutcomeStatus",
    "ProtectedRetryError",
    "ProtectedToolRetrier",
)
