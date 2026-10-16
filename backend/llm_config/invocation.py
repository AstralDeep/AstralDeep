"""Captures request-local model dispatch accounting without changing the runner's tuple API.
The standard harness consumes typed results while provider calls retain their existing guards and audits.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Awaitable, Callable, Mapping


class InvocationContractError(ValueError):
    pass


def usage_tokens(usage: Any) -> int | None:
    value = (
        usage.get("total_tokens")
        if isinstance(usage, Mapping)
        else getattr(usage, "total_tokens", None)
    )
    return value if type(value) is int and value >= 0 else None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    outcome: str
    attempts: int
    message: Any = field(default=None, repr=False)
    usage: Any = field(default=None, repr=False)
    total_tokens: int | None = None
    usage_complete: bool = False
    credential_outcome: str | None = None
    error_class: str | None = None

    def __post_init__(self):
        if (
            type(self.outcome) is not str
            or self.outcome not in {"succeeded", "denied", "failed", "cancelled"}
            or type(self.attempts) is not int
            or self.attempts < 0
            or type(self.usage_complete) is not bool
            or (
                self.total_tokens is not None
                and (type(self.total_tokens) is not int or self.total_tokens < 0)
            )
            or (
                self.credential_outcome is not None
                and (
                    type(self.credential_outcome) is not str
                    or self.credential_outcome not in {"unavailable", "rejected"}
                )
            )
            or (
                self.error_class is not None
                and (
                    type(self.error_class) is not str
                    or not self.error_class.isidentifier()
                    or len(self.error_class) > 128
                )
            )
        ):
            raise InvocationContractError("invalid typed runner result")
        if (
            (
                self.outcome == "succeeded"
                and (self.message is None or self.attempts == 0)
            )
            or (self.outcome != "succeeded" and self.message is not None)
            or (self.outcome == "denied" and self.attempts != 0)
            or (self.outcome == "succeeded" and self.credential_outcome is not None)
            or (self.attempts == 0 and self.total_tokens not in (None, 0))
            or (self.usage_complete and self.attempts > 0 and self.total_tokens is None)
        ):
            raise InvocationContractError("inconsistent typed runner outcome")


@dataclass(slots=True)
class InvocationState:
    started: bool = False
    attempts: int = 0
    total_tokens: int | None = None
    pending: int = 0
    unknown_usage: bool = False
    credential_outcome: str | None = None
    error_class: str | None = None
    failed: bool = False

    @property
    def usage_complete(self):
        return self.pending == 0 and not self.unknown_usage

    def record_usage(self, usage):
        tokens = usage_tokens(usage)
        if tokens is None:
            self.unknown_usage = True
        else:
            self.total_tokens = (self.total_tokens or 0) + tokens

    def finish(self, value):
        if type(value) is InvocationResult:
            return value
        if not self.started or type(value) is not tuple or len(value) != 2:
            raise InvocationContractError(
                "runner must return an explicit accounted result"
            )
        message, usage = value
        if self.failed:
            message = None
        return self.result(
            "denied"
            if self.credential_outcome == "unavailable"
            else "failed"
            if message is None
            else "succeeded",
            message=message,
            usage=usage,
        )

    def result(self, outcome, *, message=None, usage=None):
        return InvocationResult(
            outcome,
            self.attempts,
            message,
            usage,
            self.total_tokens,
            self.usage_complete,
            self.credential_outcome,
            self.error_class if outcome == "failed" else None,
        )


_CURRENT: ContextVar[InvocationState | None] = ContextVar(
    "llm_invocation_accounting", default=None
)


@dataclass(slots=True)
class ProviderAttempt:
    state: InvocationState | None = field(repr=False)
    closed: bool = False
    reported_tokens: int | None = None
    lock: Any = field(default_factory=Lock, repr=False)


_ACTIVE_ATTEMPT: ContextVar[ProviderAttempt | None] = ContextVar(
    "llm_provider_attempt", default=None
)


def provider_reported_usage(usage):
    attempt = _ACTIVE_ATTEMPT.get()
    tokens = usage_tokens(usage)
    if attempt is not None and tokens is not None:
        with attempt.lock:
            if not attempt.closed:
                attempt.reported_tokens = tokens


def provider_started():
    state = _CURRENT.get()
    if state is not None:
        state.attempts += 1
        state.pending += 1
    return ProviderAttempt(state)


def provider_finished(attempt, usage=None, *, sent=True):
    if attempt is None:
        return
    with attempt.lock:
        if attempt.closed:
            return
        attempt.closed = True
        if attempt.state is None:
            return
        attempt.state.pending -= 1
        if sent:
            attempt.state.record_usage(usage)
            if usage_tokens(usage) is None and attempt.reported_tokens is not None:
                attempt.state.record_usage({"total_tokens": attempt.reported_tokens})
        else:
            attempt.state.attempts -= 1


def runner_started():
    if (state := _CURRENT.get()) is not None:
        state.started = True


def runner_unavailable():
    if (state := _CURRENT.get()) is not None:
        state.credential_outcome = "unavailable"


def runner_failed(error_class, *, credential_rejected=False):
    if (state := _CURRENT.get()) is not None:
        state.failed = True
        state.error_class = error_class
        if credential_rejected:
            state.credential_outcome = "rejected"


async def observe_provider(invoke: Callable[[], Awaitable[Any]]):
    state = _CURRENT.get()
    if state is None:
        return await invoke()
    attempt = provider_started()
    token = _ACTIVE_ATTEMPT.set(attempt)
    try:
        response = await invoke()
    except BaseException as error:
        provider_finished(attempt, getattr(error, "usage", None))
        state.error_class = type(error).__name__
        raise
    finally:
        _ACTIVE_ATTEMPT.reset(token)
    provider_finished(attempt, getattr(response, "usage", None))
    return response


async def invoke_accounted(
    invoke: Callable[[], Awaitable[Any]], state: InvocationState
):
    token = _CURRENT.set(state)
    try:
        return state.finish(await invoke())
    except (asyncio.CancelledError, InvocationContractError):
        raise
    except Exception as error:
        if not state.started:
            raise InvocationContractError("unaccounted runner failure") from None
        state.error_class = type(error).__name__
        return state.result("failed")
    finally:
        _CURRENT.reset(token)
