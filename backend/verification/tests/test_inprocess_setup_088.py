"""Tests for the in-process driver's setup/teardown
(backend/verification/drivers/in_process.py, fixture_identity.py): off-loop
construction that still owns its runtime graph, and rollback on constructor or start
failure.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from verification.drivers.in_process import InProcessDriver, _run_sync


def _driver(tmp_path):
    return InProcessDriver(
        SimpleNamespace(run_dir=str(tmp_path), run_id="__verif__setup", mode="in_process")
    )


@pytest.fixture(autouse=True)
def fixture_identity_lifetime(monkeypatch):
    from verification.drivers.fixture_identity import FixtureIdentity

    monkeypatch.setenv("ASTRAL_ENV", "development")
    yield
    if FixtureIdentity._active is not None:
        FixtureIdentity._active.close()


def test_setup_constructs_off_loop_but_starts_on_callers_loop(tmp_path, monkeypatch):
    from orchestrator import orchestrator as product

    driver = _driver(tmp_path)
    events = []

    async def exercise():
        loop = asyncio.get_running_loop()
        caller = threading.get_ident()

        def start():
            assert asyncio.get_running_loop() is loop
            assert threading.get_ident() == caller
            events.append("start")

        graph = SimpleNamespace(runtime_composition=SimpleNamespace(start=start))

        def construct():
            assert threading.get_ident() != caller
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            events.append("construct")
            return graph

        def register():
            assert asyncio.get_running_loop() is loop
            assert driver.orch is graph
            events.append("register")

        monkeypatch.setattr(product, "Orchestrator", construct)
        monkeypatch.setattr(driver, "_register_general_agent", register)
        await driver.setup()
        assert driver.orch is graph

    asyncio.run(exercise())
    assert events == ["construct", "register", "start"]


@pytest.mark.parametrize("cancel", [False, True])
def test_setup_observes_constructor_failure(tmp_path, monkeypatch, cancel):
    from orchestrator import orchestrator as product

    driver = _driver(tmp_path)

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        def construct():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=3)
            raise ValueError("construction rolled back")

        monkeypatch.setattr(product, "Orchestrator", construct)
        pending = asyncio.create_task(driver.setup())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            if cancel:
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done()
            release.set()
            with pytest.raises(ValueError, match="construction rolled back"):
                await pending
            assert driver.orch is None
        finally:
            release.set()

    asyncio.run(exercise())


def test_cancelled_setup_joins_construction_and_closes_graph(tmp_path, monkeypatch):
    from orchestrator import orchestrator as product

    driver = _driver(tmp_path)
    events = []

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        closing = asyncio.Event()
        finish_close = asyncio.Event()

        async def close():
            assert asyncio.get_running_loop() is loop
            events.append("close")
            closing.set()
            await finish_close.wait()
            events.append("closed")

        graph = SimpleNamespace(_close_started_services=close)

        def construct():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=3)
            return graph

        monkeypatch.setattr(product, "Orchestrator", construct)
        monkeypatch.setattr(
            driver, "_register_general_agent", lambda: events.append("register")
        )
        pending = asyncio.create_task(driver.setup())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            await asyncio.wait_for(closing.wait(), timeout=2)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            assert driver.orch is None
            finish_close.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
            finish_close.set()

    asyncio.run(exercise())
    assert events == ["close", "closed"]


@pytest.mark.parametrize("phase", ["register", "start"])
def test_setup_rolls_back_registration_or_start_failure(tmp_path, monkeypatch, phase):
    from orchestrator import orchestrator as product

    driver = _driver(tmp_path)
    events = []

    def step(name):
        events.append(name)
        if phase == name:
            raise ValueError(f"{name} failed")

    async def close():
        events.append("close")

    graph = SimpleNamespace(
        runtime_composition=SimpleNamespace(start=lambda: step("start")),
        _close_started_services=close,
    )
    monkeypatch.setattr(product, "Orchestrator", lambda: graph)
    monkeypatch.setattr(driver, "_register_general_agent", lambda: step("register"))
    with pytest.raises(ValueError, match=f"{phase} failed"):
        asyncio.run(driver.setup())
    assert driver.orch is None
    assert events == (["register"] if phase == "register" else ["register", "start"]) + ["close"]


@pytest.mark.parametrize("cancel", [False, True])
def test_durable_call_settles_before_cancellation_can_close_runtime(cancel):
    events = []

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        def write(value, *, expected):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            assert value == expected
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=3)
            events.append("write_settled")
            return value

        async def caller():
            try:
                return await _run_sync(write, "owned", expected="owned")
            finally:
                events.append("caller_cleanup")

        pending = asyncio.create_task(caller())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            if cancel:
                pending.cancel()
                await asyncio.sleep(0)
                pending.cancel()
                await asyncio.sleep(0)
                assert not pending.done()
                assert not events
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                assert await pending == "owned"
        finally:
            release.set()

    asyncio.run(exercise())
    assert events == ["write_settled", "caller_cleanup"]


def test_durable_call_propagates_failure():
    def failed():
        raise ValueError("durable call failed")

    with pytest.raises(ValueError, match="durable call failed"):
        asyncio.run(_run_sync(failed))
