"""Bounded event-loop adapter moving one complete caller-owned Plane transaction from
api.py's synchronous runtime onto a worker thread; admission is bounded so event-loop
callers cannot create an unbounded executor backlog.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from astralplane.contracts import IsolationLevel, Transaction
from astralplane.errors import PlaneError

if TYPE_CHECKING:
    from astralplane.api import PlaneRuntime

T = TypeVar("T")


class AsyncPlaneCapacityError(PlaneError):
    default_code = "async_plane_capacity_unavailable"


class AsyncPlaneClosedError(PlaneError):
    default_code = "async_plane_closed"


class AsyncPlaneCallbackTypeError(PlaneError):
    default_code = "async_plane_callback_type"


def _callback_is_coroutine(callback: object) -> bool:
    candidate: object = callback
    while isinstance(candidate, functools.partial):
        candidate = candidate.func
    if inspect.iscoroutinefunction(candidate):
        return True
    try:
        return inspect.iscoroutinefunction(candidate.__call__)
    except AttributeError:
        return False


@dataclass(frozen=True, slots=True)
class AsyncPlaneSnapshot:
    maximum_concurrency: int
    active: int
    closed: bool


class AsyncPlaneRuntime:
    def __init__(
        self,
        runtime: PlaneRuntime,
        *,
        maximum_concurrency: int = 8,
        admission_timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(maximum_concurrency, bool) or not isinstance(
            maximum_concurrency, int
        ):
            raise TypeError("maximum_concurrency must be an integer")
        if not 1 <= maximum_concurrency <= 64:
            raise ValueError("maximum_concurrency must be between 1 and 64")
        if isinstance(admission_timeout_seconds, bool) or not isinstance(
            admission_timeout_seconds, (int, float)
        ):
            raise TypeError("admission_timeout_seconds must be numeric")
        timeout = float(admission_timeout_seconds)
        if not 0.01 <= timeout <= 300.0:
            raise ValueError("admission_timeout_seconds must be between 0.01 and 300")
        if runtime is None:
            raise TypeError("runtime is required")
        self._runtime = runtime
        self._maximum_concurrency = maximum_concurrency
        self._admission_timeout_seconds = timeout
        self._semaphore = asyncio.Semaphore(maximum_concurrency)
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def repositories(self) -> object:
        return self._runtime.repositories

    def snapshot(self) -> AsyncPlaneSnapshot:
        return AsyncPlaneSnapshot(
            maximum_concurrency=self._maximum_concurrency,
            active=self._active,
            closed=self._closed,
        )

    async def run_in_transaction(
        self,
        callback: Callable[[Transaction], T],
        *,
        isolation: IsolationLevel | None = None,
    ) -> T:
        if not callable(callback):
            raise TypeError("callback must be callable")
        if _callback_is_coroutine(callback):
            raise AsyncPlaneCallbackTypeError(
                "AsyncPlaneRuntime callbacks must complete synchronously; an awaitable"
                " callback would leave the transaction without its result",
                metadata={"reason": "coroutine_callback"},
            )
        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        if self._closed:
            raise AsyncPlaneClosedError("AstralPlane async adapter is closed")
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._admission_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AsyncPlaneCapacityError(
                "AstralPlane async transaction capacity is unavailable",
                metadata={"maximum_concurrency": self._maximum_concurrency},
            ) from exc
        if self._closed:
            self._semaphore.release()
            raise AsyncPlaneClosedError("AstralPlane async adapter is closed")

        self._active += 1
        self._idle.clear()
        worker = asyncio.create_task(
            asyncio.to_thread(self._run_sync, callback, isolation),
            name="astralplane-transaction",
        )
        released = False

        def release_slot(completed: asyncio.Task[T]) -> None:
            nonlocal released
            if released:
                return
            released = True
            self._active -= 1
            self._semaphore.release()
            if self._active == 0:
                self._idle.set()
            if completed.cancelled():
                return
            completed.exception()

        worker.add_done_callback(release_slot)
        return await asyncio.shield(worker)

    def close(self) -> None:
        self._closed = True

    async def drain(self, timeout: float = 5.0) -> bool:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be numeric")
        if not 0.01 <= timeout <= 300.0:
            raise ValueError("timeout must be between 0.01 and 300 seconds")
        self._bind_loop(asyncio.get_running_loop())
        self.close()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=float(timeout))
            return True
        except TimeoutError:
            return False

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("AsyncPlaneRuntime cannot be shared across event loops")

    def _run_sync(
        self,
        callback: Callable[[Transaction], T],
        isolation: IsolationLevel | None,
    ) -> T:
        with self._runtime.transaction(isolation=isolation) as transaction:
            result = callback(transaction)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise AsyncPlaneCallbackTypeError(
                    "AsyncPlaneRuntime callbacks must return a synchronous result;"
                    " the transaction rolled back instead of committing an awaitable",
                    metadata={"reason": "awaitable_result"},
                )
            return result


__all__ = (
    "AsyncPlaneCallbackTypeError",
    "AsyncPlaneCapacityError",
    "AsyncPlaneClosedError",
    "AsyncPlaneRuntime",
    "AsyncPlaneSnapshot",
)
