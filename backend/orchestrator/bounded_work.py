"""Small bounded thread-pool executors (GENERATION_EXECUTOR, MAINTENANCE_EXECUTOR)
isolating slow generation/maintenance calls from the default asyncio executor shared
with interactive requests; used by agentic_creation.py and knowledge_synthesis.py.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
import functools
import os
import threading
from typing import Any, Callable, TypeVar


_T = TypeVar("_T")


class WorkExecutorSaturated(RuntimeError):
    pass


class BoundedWorkExecutor:
    def __init__(self, *, name: str, max_workers: int, queue_limit: int) -> None:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("executor name must be a bounded identifier")
        if type(max_workers) is not int or max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if type(queue_limit) is not int or queue_limit < 0:
            raise ValueError("queue_limit cannot be negative")
        self.name = name
        self.max_workers = max_workers
        self.queue_limit = queue_limit
        self._capacity = max_workers + queue_limit
        self._lock = threading.Lock()
        self._in_flight = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"astral-{name}",
        )

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    async def run(
        self, function: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> _T:
        if not callable(function):
            raise TypeError("function must be callable")
        with self._lock:
            if self._in_flight >= self._capacity:
                raise WorkExecutorSaturated(f"{self.name}_executor_saturated")
            self._in_flight += 1
        context = contextvars.copy_context()
        call = functools.partial(function, *args, **kwargs)
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._executor, context.run, call
            )
        finally:
            with self._lock:
                self._in_flight -= 1


def _bounded_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


GENERATION_EXECUTOR = BoundedWorkExecutor(
    name="generation",
    max_workers=_bounded_env(
        "GENERATION_EXECUTOR_WORKERS", 2, minimum=1, maximum=16
    ),
    queue_limit=_bounded_env(
        "GENERATION_EXECUTOR_QUEUE", 8, minimum=0, maximum=128
    ),
)

MAINTENANCE_EXECUTOR = BoundedWorkExecutor(
    name="maintenance",
    max_workers=_bounded_env(
        "MAINTENANCE_EXECUTOR_WORKERS", 2, minimum=1, maximum=16
    ),
    queue_limit=_bounded_env(
        "MAINTENANCE_EXECUTOR_QUEUE", 16, minimum=0, maximum=128
    ),
)


async def run_generation(
    function: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> _T:
    return await GENERATION_EXECUTOR.run(function, *args, **kwargs)


async def run_maintenance(
    function: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> _T:
    return await MAINTENANCE_EXECUTOR.run(function, *args, **kwargs)
