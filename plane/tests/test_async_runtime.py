"""Tests for src/astralplane/async_runtime.py: AsyncPlaneRuntime's bounded worker
admission, cancellation, and event-driven shutdown, with a PostgreSQL pool-close check.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import CoroutineType, SimpleNamespace

import pytest

from astralplane.async_runtime import (
    AsyncPlaneCallbackTypeError,
    AsyncPlaneCapacityError,
    AsyncPlaneClosedError,
    AsyncPlaneRuntime,
)
from astralplane.contracts import IsolationLevel
from astralplane.database.bootstrap import BootStatus
from astralplane.errors import PoolInUseError
from tests.fixtures.migrated_template import MigratedDatabase


async def _async_function(_transaction: object) -> str:
    return "never"


class _AsyncCallObject:
    async def __call__(self, _transaction: object) -> str:
        return "never"


class _HiddenCallObject:
    async def __call__(self, _transaction: object) -> str:
        return "never"

    def __getattribute__(self, name: str) -> object:
        if name == "__call__":
            raise AttributeError("__call__ is withheld")
        return object.__getattribute__(self, name)


class _AsyncMethodHost:
    async def method(self, _transaction: object) -> str:
        return "never"


class _AwaitableResult:
    def __await__(self) -> Iterator[None]:
        if False:
            yield None


class RuntimeStub:
    def __init__(self) -> None:
        self.repositories = object()
        self.isolations: list[IsolationLevel | None] = []
        self.threads: list[int] = []
        self.transactions_entered = 0
        self.outcomes: list[str] = []

    @contextmanager
    def transaction(self, *, isolation: IsolationLevel | None = None):
        self.transactions_entered += 1
        self.isolations.append(isolation)
        self.threads.append(threading.get_ident())
        try:
            yield SimpleNamespace(marker="transaction")
        except BaseException:
            self.outcomes.append("rolled_back")
            raise
        else:
            self.outcomes.append("committed")


def test_transaction_runs_off_loop_with_exact_isolation_and_catalog() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(runtime, maximum_concurrency=2)  # type: ignore[arg-type]
    loop_thread = threading.get_ident()

    async def run() -> str:
        return await adapter.run_in_transaction(
            lambda transaction: transaction.marker,
            isolation=IsolationLevel.SERIALIZABLE,
        )

    assert asyncio.run(run()) == "transaction"
    assert runtime.isolations == [IsolationLevel.SERIALIZABLE]
    assert runtime.threads[0] != loop_thread
    assert adapter.repositories is runtime.repositories
    assert adapter.snapshot().active == 0


def test_callback_failure_propagates_and_releases_capacity() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(runtime)  # type: ignore[arg-type]

    async def run() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            await adapter.run_in_transaction(
                lambda _transaction: (_ for _ in ()).throw(RuntimeError("boom"))
            )
        assert adapter.snapshot().active == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "build_callback",
    [
        pytest.param(lambda: _async_function, id="coroutine_function"),
        pytest.param(
            lambda: functools.partial(_async_function),
            id="partial_of_coroutine_function",
        ),
        pytest.param(_AsyncCallObject, id="callable_object_with_async_call"),
        pytest.param(lambda: _AsyncMethodHost().method, id="bound_async_method"),
    ],
)
def test_coroutine_callback_kinds_are_rejected_before_any_transaction(
    build_callback: Callable[[], object],
) -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(runtime)  # type: ignore[arg-type]

    with pytest.raises(AsyncPlaneCallbackTypeError) as caught:
        asyncio.run(adapter.run_in_transaction(build_callback()))  # type: ignore[arg-type]
    assert caught.value.code == "async_plane_callback_type"
    assert caught.value.metadata == (("reason", "coroutine_callback"),)
    assert runtime.transactions_entered == 0
    assert runtime.outcomes == []
    assert runtime.isolations == []
    assert adapter.snapshot().active == 0
    assert asyncio.run(adapter.run_in_transaction(lambda _transaction: "sync")) == "sync"
    assert runtime.outcomes == ["committed"]


def test_callable_with_unreadable_call_attribute_falls_through_to_result_rejection() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(runtime)  # type: ignore[arg-type]
    callback = _HiddenCallObject()
    assert callable(callback)

    async def run() -> None:
        with pytest.raises(AsyncPlaneCallbackTypeError) as raised:
            await adapter.run_in_transaction(callback)
        assert raised.value.metadata == (("reason", "awaitable_result"),)
        assert adapter.snapshot().active == 0

    asyncio.run(run())
    assert runtime.transactions_entered == 1
    assert runtime.outcomes == ["rolled_back"]


def test_sync_callback_returning_coroutine_rolls_back_without_leaking() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(runtime)  # type: ignore[arg-type]
    produced: list[object] = []

    def returns_coroutine(_transaction: object) -> object:
        coroutine = _async_function(_transaction)
        produced.append(coroutine)
        return coroutine

    async def run() -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(AsyncPlaneCallbackTypeError) as raised:
                await adapter.run_in_transaction(returns_coroutine)
        assert raised.value.code == "async_plane_callback_type"
        assert raised.value.metadata == (("reason", "awaitable_result"),)
        assert not [item for item in caught if issubclass(item.category, RuntimeWarning)]
        assert adapter.snapshot().active == 0
        assert await adapter.run_in_transaction(lambda _transaction: "after") == "after"

    asyncio.run(run())
    assert runtime.transactions_entered == 2
    assert runtime.outcomes == ["rolled_back", "committed"]
    assert isinstance(produced[0], CoroutineType)
    assert produced[0].cr_frame is None


def test_sync_callback_returning_non_coroutine_awaitable_rolls_back() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(runtime)  # type: ignore[arg-type]

    def returns_awaitable(_transaction: object) -> object:
        return _AwaitableResult()

    async def run() -> None:
        with pytest.raises(AsyncPlaneCallbackTypeError) as raised:
            await adapter.run_in_transaction(returns_awaitable)
        assert raised.value.code == "async_plane_callback_type"
        assert raised.value.metadata == (("reason", "awaitable_result"),)
        assert adapter.snapshot().active == 0

    asyncio.run(run())
    assert runtime.transactions_entered == 1
    assert runtime.outcomes == ["rolled_back"]


def test_cancelled_requester_keeps_slot_until_worker_rejects_awaitable() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(  # type: ignore[arg-type]
        runtime,
        maximum_concurrency=1,
        admission_timeout_seconds=0.02,
    )
    entered = threading.Event()
    release = threading.Event()
    produced: list[object] = []

    def returns_coroutine(_transaction: object) -> object:
        entered.set()
        assert release.wait(timeout=2)
        coroutine = _async_function(_transaction)
        produced.append(coroutine)
        return coroutine

    async def run() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[object] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        requester = asyncio.create_task(adapter.run_in_transaction(returns_coroutine))
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            requester.cancel()
            with pytest.raises(asyncio.CancelledError):
                await requester
            assert adapter.snapshot().active == 1
            with pytest.raises(AsyncPlaneCapacityError):
                await adapter.run_in_transaction(lambda _transaction: "never")
            release.set()
            while adapter.snapshot().active:
                await asyncio.sleep(0.001)
            assert isinstance(produced[0], CoroutineType)
            assert produced[0].cr_frame is None
            assert await adapter.run_in_transaction(lambda _transaction: 7) == 7
            assert not unhandled
        finally:
            release.set()
            await asyncio.gather(requester, return_exceptions=True)

    asyncio.run(run())
    assert runtime.transactions_entered == 2
    assert runtime.outcomes == ["rolled_back", "committed"]


def test_admission_timeout_bounds_worker_backlog() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(  # type: ignore[arg-type]
        runtime,
        maximum_concurrency=1,
        admission_timeout_seconds=0.02,
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking(_transaction: object) -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "done"

    async def run() -> None:
        first = asyncio.create_task(adapter.run_in_transaction(blocking))
        while not entered.is_set():
            await asyncio.sleep(0)
        with pytest.raises(AsyncPlaneCapacityError) as caught:
            await adapter.run_in_transaction(lambda _transaction: "never")
        assert caught.value.code == "async_plane_capacity_unavailable"
        assert adapter.snapshot().active == 1
        release.set()
        assert await first == "done"

    asyncio.run(run())


def test_cancellation_retains_slot_until_the_thread_finishes() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(  # type: ignore[arg-type]
        runtime,
        maximum_concurrency=1,
        admission_timeout_seconds=0.02,
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking(_transaction: object) -> None:
        entered.set()
        release.wait(timeout=2)

    async def run() -> None:
        task = asyncio.create_task(adapter.run_in_transaction(blocking))
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(AsyncPlaneCapacityError):
            await adapter.run_in_transaction(lambda _transaction: None)
        release.set()
        while adapter.snapshot().active:
            await asyncio.sleep(0.001)
        assert await adapter.run_in_transaction(lambda _transaction: 7) == 7

    asyncio.run(run())


def test_close_rejects_new_work_and_cross_loop_use_fails_closed() -> None:
    runtime = RuntimeStub()
    adapter = AsyncPlaneRuntime(runtime)  # type: ignore[arg-type]
    asyncio.run(adapter.run_in_transaction(lambda _transaction: None))

    with pytest.raises(RuntimeError, match="event loops"):
        asyncio.run(adapter.run_in_transaction(lambda _transaction: None))

    fresh = AsyncPlaneRuntime(runtime)  # type: ignore[arg-type]
    fresh.close()
    assert fresh.snapshot().closed
    with pytest.raises(AsyncPlaneClosedError):
        asyncio.run(fresh.run_in_transaction(lambda _transaction: None))


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"maximum_concurrency": True}, TypeError),
        ({"maximum_concurrency": 0}, ValueError),
        ({"maximum_concurrency": 65}, ValueError),
        ({"admission_timeout_seconds": True}, TypeError),
        ({"admission_timeout_seconds": 0}, ValueError),
        ({"admission_timeout_seconds": 301}, ValueError),
    ],
)
def test_constructor_bounds(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        AsyncPlaneRuntime(RuntimeStub(), **kwargs)  # type: ignore[arg-type]


def test_non_callable_callback_is_rejected_before_loop_binding() -> None:
    adapter = AsyncPlaneRuntime(RuntimeStub())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="callable"):
        asyncio.run(adapter.run_in_transaction(None))  # type: ignore[arg-type]


def test_idle_drain_has_a_bounded_default_and_repeated_close_is_safe() -> None:
    adapter = AsyncPlaneRuntime(RuntimeStub())  # type: ignore[arg-type]

    async def run() -> None:
        assert await adapter.drain()
        adapter.close()
        adapter.close()
        assert await adapter.drain(timeout=300)
        assert adapter.snapshot().closed
        with pytest.raises(AsyncPlaneClosedError):
            await adapter.run_in_transaction(lambda _transaction: None)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("timeout", "error"),
    [
        (None, TypeError),
        (True, TypeError),
        ("1", TypeError),
        (0, ValueError),
        (-1, ValueError),
        (0.009, ValueError),
        (300.001, ValueError),
        (10**1000, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (float("-inf"), ValueError),
    ],
)
def test_drain_rejects_invalid_timeout_without_closing_or_binding(
    timeout: object, error: type[Exception]
) -> None:
    adapter = AsyncPlaneRuntime(RuntimeStub())  # type: ignore[arg-type]
    with pytest.raises(error):
        asyncio.run(adapter.drain(timeout=timeout))  # type: ignore[arg-type]
    assert not adapter.snapshot().closed
    assert asyncio.run(adapter.run_in_transaction(lambda _transaction: 7)) == 7


def test_cross_loop_drain_does_not_close_the_adapter() -> None:
    adapter = AsyncPlaneRuntime(RuntimeStub())  # type: ignore[arg-type]
    asyncio.run(adapter.run_in_transaction(lambda _transaction: None))
    with pytest.raises(RuntimeError, match="event loops"):
        asyncio.run(adapter.drain())
    assert not adapter.snapshot().closed


def test_drain_waits_for_worker_and_rejects_queued_admissions() -> None:
    adapter = AsyncPlaneRuntime(RuntimeStub(), maximum_concurrency=1)  # type: ignore[arg-type]
    release = threading.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def blocking(_transaction: object) -> str:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return "done"

        worker = asyncio.create_task(adapter.run_in_transaction(blocking))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            queued = asyncio.create_task(adapter.run_in_transaction(lambda _transaction: "never"))
            await asyncio.sleep(0)
            draining = asyncio.create_task(adapter.drain(timeout=300))
            await asyncio.sleep(0)
            assert adapter.snapshot().closed
            assert adapter.snapshot().active == 1
            assert not draining.done()
            with pytest.raises(AsyncPlaneClosedError):
                await adapter.run_in_transaction(lambda _transaction: "never")
            release.set()
            assert await worker == "done"
            assert await draining
            with pytest.raises(AsyncPlaneClosedError):
                await queued
            assert adapter.snapshot().active == 0
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("worker_fails", [False, True])
def test_multiple_drains_wait_after_requester_cancellation_and_consume_worker_failure(
    worker_fails: bool,
) -> None:
    adapter = AsyncPlaneRuntime(RuntimeStub())  # type: ignore[arg-type]
    release = threading.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[object] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        entered = asyncio.Event()

        def blocking(_transaction: object) -> None:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            if worker_fails:
                raise RuntimeError("synthetic worker failure")

        requester = asyncio.create_task(adapter.run_in_transaction(blocking))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            requester.cancel()
            with pytest.raises(asyncio.CancelledError):
                await requester
            first = asyncio.create_task(adapter.drain(timeout=300))
            second = asyncio.create_task(adapter.drain(timeout=300))
            await asyncio.sleep(0)
            assert adapter.snapshot().active == 1
            assert not first.done() and not second.done()
            release.set()
            assert await asyncio.gather(first, second) == [True, True]
            assert adapter.snapshot().active == 0
            assert not unhandled
        finally:
            release.set()
            await asyncio.gather(requester, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("cancel_drain", [False, True])
def test_timeout_or_cancelled_drain_keeps_worker_running_and_can_be_retried(
    cancel_drain: bool,
) -> None:
    adapter = AsyncPlaneRuntime(RuntimeStub())  # type: ignore[arg-type]
    release = threading.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def blocking(_transaction: object) -> str:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return "done"

        worker = asyncio.create_task(adapter.run_in_transaction(blocking))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            if cancel_drain:
                draining = asyncio.create_task(adapter.drain(timeout=300))
                await asyncio.sleep(0)
                draining.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await draining
            else:
                assert not await adapter.drain(timeout=0.01)
            assert adapter.snapshot().closed
            assert adapter.snapshot().active == 1
            assert not worker.done()
            with pytest.raises(AsyncPlaneClosedError):
                await adapter.run_in_transaction(lambda _transaction: "never")
            release.set()
            assert await worker == "done"
            assert await adapter.drain(timeout=300)
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_drain_allows_final_safe_runtime_close_on_postgresql(
    migrated_clone: MigratedDatabase,
) -> None:
    from astralplane.api import PlaneRuntime

    runtime = PlaneRuntime(
        pool=migrated_clone.pool,
        database=migrated_clone.database,
        initializer=SimpleNamespace(status=BootStatus.READY),  # type: ignore[arg-type]
        reconciler=SimpleNamespace(),  # type: ignore[arg-type]
    )
    adapter = AsyncPlaneRuntime(runtime)
    release = threading.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def blocking(transaction: object) -> None:
            transaction.fetch_one("SELECT 1 AS synthetic_value")  # type: ignore[attr-defined]
            loop.call_soon_threadsafe(entered.set)
            release.wait()

        worker = asyncio.create_task(adapter.run_in_transaction(blocking))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            with pytest.raises(PoolInUseError):
                runtime.close()
            assert not await adapter.drain(timeout=0.01)
            with pytest.raises(PoolInUseError):
                runtime.close()
            release.set()
            await worker
            assert await adapter.drain(timeout=300)
            assert not runtime.health().pool.closed
            runtime.close()
            runtime.close()
            assert runtime.health().pool.closed
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())
