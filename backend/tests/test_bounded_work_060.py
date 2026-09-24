"""Tests for orchestrator/bounded_work.py: the blocking executor's worker/queue budget,
lane isolation between generation and maintenance, and context propagation without
blocking the event loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from orchestrator.bounded_work import (
    BoundedWorkExecutor,
    WorkExecutorSaturated,
)


async def test_executor_refuses_above_its_finite_worker_and_queue_budget():
    executor = BoundedWorkExecutor(name="finite_test", max_workers=1, queue_limit=0)
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        assert release.wait(timeout=5)
        return "finished"

    first = asyncio.create_task(executor.run(blocking))
    assert await asyncio.to_thread(started.wait, 5)
    with pytest.raises(WorkExecutorSaturated, match="finite_test_executor_saturated"):
        await executor.run(lambda: "must not run")
    release.set()
    assert await first == "finished"
    assert executor.in_flight == 0


async def test_generation_and_maintenance_lanes_do_not_share_capacity():
    generation = BoundedWorkExecutor(
        name="generation_test", max_workers=1, queue_limit=0
    )
    maintenance = BoundedWorkExecutor(
        name="maintenance_test", max_workers=1, queue_limit=0
    )
    started = threading.Event()
    release = threading.Event()

    def blocking_maintenance():
        started.set()
        assert release.wait(timeout=5)

    maintenance_task = asyncio.create_task(
        maintenance.run(blocking_maintenance)
    )
    assert await asyncio.to_thread(started.wait, 5)
    assert await generation.run(lambda: "generation admitted") == (
        "generation admitted"
    )
    release.set()
    await maintenance_task


async def test_blocking_lane_preserves_context_without_blocking_event_loop():
    executor = BoundedWorkExecutor(name="context_test", max_workers=1, queue_limit=1)
    marker = contextvars.ContextVar("marker", default="missing")
    marker.set("fenced-context")
    release = threading.Event()
    started = threading.Event()

    def blocking():
        started.set()
        assert release.wait(timeout=5)
        return marker.get()

    work = asyncio.create_task(executor.run(blocking))
    assert await asyncio.to_thread(started.wait, 5)
    # Zero-delay probe: blocked event loop couldn't reach this
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    release.set()
    assert await work == "fenced-context"
