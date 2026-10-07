"""Exercises TypeSafe outcome writers against the real Orchestrator and its pinned Plane pool.
Event-held transactions verify that shutdown owns pending writes and refuses new outcome work.
"""

import asyncio
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from llm_config.typesafe_store import key_fingerprint
from orchestrator.typesafe_routing.budget import Outcome
from orchestrator.typesafe_routing.runner import RoutingOutcome


KEY = "typesafe-shutdown-synthetic-key"


@pytest.fixture
async def outcome_writer(orchestrator_factory, monkeypatch, tmp_path):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("PERSONAL_AGENT_ARTIFACT_ROOT", str(tmp_path / "agents"))
    orch = orchestrator_factory()
    store = orch._typesafe_store
    owner = "typesafe-shutdown-" + uuid4().hex
    fingerprint = key_fingerprint(KEY)
    await store.save(owner, KEY)
    await asyncio.to_thread(store.record_outcome_sync, owner, "unavailable", fingerprint)
    pool = orch.runtime_composition.plane.runtime._pool
    state = SimpleNamespace(orch=orch, store=store, owner=owner, fingerprint=fingerprint,
                            pool=pool, entered=threading.Event(), release=threading.Event(),
                            finished=threading.Event(), waiters=[], writers=[],
                            close_borrows=[], fail_writer=False)
    repository = store._repository.repository
    record = repository.record_outcome
    record_sync = store.record_outcome_sync
    close_pool = pool.close

    def held_record(transaction, **fields):
        assert fields["owner_id"] == owner
        assert fields["expected_fingerprint"] == fingerprint
        assert pool.snapshot.borrowed > 0
        state.entered.set()
        assert state.release.wait(timeout=5)
        return record(transaction, **fields)

    def observed_writer(*args):
        try:
            result = record_sync(*args)
            if state.fail_writer:
                raise RuntimeError("outcome writer failed after returning its transaction")
            return result
        finally:
            state.finished.set()

    def observed_close():
        state.close_borrows.append(pool.snapshot.borrowed)
        return close_pool()

    monkeypatch.setattr(repository, "record_outcome", held_record)
    monkeypatch.setattr(store, "record_outcome_sync", observed_writer)
    monkeypatch.setattr(pool, "close", observed_close)
    try:
        yield state
    finally:
        state.release.set()
        await asyncio.wait_for(asyncio.gather(*state.writers, return_exceptions=True), 5)
        await asyncio.wait_for(asyncio.gather(*state.waiters, return_exceptions=True), 5)


def outcome(state):
    return RoutingOutcome(outcome=Outcome.SUCCESS, credential_outcome="valid",
                          fingerprint=state.fingerprint)


async def start_writer(state):
    state.orch._typesafe_record_outcome(state.owner, outcome(state))
    writer, = state.orch._typesafe_outcome_tasks
    state.writers.append(writer)
    assert await asyncio.wait_for(asyncio.to_thread(state.entered.wait, 5), 5)
    assert state.pool.snapshot.borrowed > 0
    return writer


def start_close(state):
    waiter = asyncio.create_task(state.orch._close_started_services())
    state.waiters.append(waiter)
    return waiter


async def assert_close_held(state, *waiters):
    settled, pending = await asyncio.wait(waiters, timeout=0.1)
    for waiter in settled:
        await waiter
    assert pending == set(waiters)
    assert not state.close_borrows
    assert not state.finished.is_set()
    assert state.orch._typesafe_outcomes_closing is True


async def assert_closed(state):
    assert state.finished.is_set()
    assert state.pool.snapshot.borrowed == 0
    assert state.pool.snapshot.closed
    assert state.close_borrows == [0]
    assert not state.orch._typesafe_outcome_tasks


async def test_normal_close_joins_real_borrowed_outcome(outcome_writer, orchestrator_factory):
    state = outcome_writer
    writer = await start_writer(state)
    waiter = start_close(state)
    await assert_close_held(state, waiter)
    state.release.set()
    await asyncio.wait_for(waiter, 5)
    assert await writer is True
    await assert_closed(state)
    reader = orchestrator_factory()
    assert (await reader._typesafe_store.status(state.owner)).name == "active"
    assert await reader._typesafe_store.get_key("foreign-" + uuid4().hex) is None


async def test_concurrent_cancelled_close_waiters_keep_real_writer_owned(outcome_writer):
    state = outcome_writer
    writer = await start_writer(state)
    first = start_close(state)
    await asyncio.sleep(0)
    shared = state.orch._started_services_close_task
    second = start_close(state)
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await assert_close_held(state, first, second)
    assert state.orch._started_services_close_task is shared
    assert not writer.cancelled()
    state.release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    assert await writer is True
    await assert_closed(state)
    await state.orch._close_started_services()
    assert state.orch._started_services_close_task is shared
    assert state.close_borrows == [0]


async def test_failed_writer_returns_borrow_before_normal_close(outcome_writer):
    state = outcome_writer
    state.fail_writer = True
    writer = await start_writer(state)
    waiter = start_close(state)
    await assert_close_held(state, waiter)
    state.release.set()
    await asyncio.wait_for(waiter, 5)
    assert isinstance(writer.exception(), RuntimeError)
    await assert_closed(state)


async def test_outcome_admission_stops_when_shared_close_begins(outcome_writer, monkeypatch):
    state = outcome_writer
    await start_writer(state)
    entered, release = asyncio.Event(), asyncio.Event()
    service = state.orch.generated_agent_publication_service
    close = service.close
    admissions = []
    record = state.store.record_outcome_async

    async def held_close():
        entered.set()
        await release.wait()
        await close()

    def admitted_record(*args):
        admissions.append(args[:2])
        return record(*args)

    monkeypatch.setattr(service, "close", held_close)
    monkeypatch.setattr(state.store, "record_outcome_async", admitted_record)
    waiter = start_close(state)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        state.orch._typesafe_record_outcome(state.owner, outcome(state))
        assert admissions == []
        assert state.orch._typesafe_outcomes_closing is True
    finally:
        state.release.set()
        release.set()
        await asyncio.wait_for(asyncio.gather(waiter, return_exceptions=True), 5)
    await assert_closed(state)
    state.orch._typesafe_record_outcome(state.owner, outcome(state))
    assert admissions == []


async def test_failed_close_keeps_outcome_admission_retired_during_retry(outcome_writer, monkeypatch):
    state = outcome_writer
    writer = await start_writer(state)
    state.release.set()
    assert await asyncio.wait_for(writer, 5) is True
    composition = state.orch.runtime_composition
    close = type(composition).close
    attempts = 0
    admissions = []
    record = state.store.record_outcome_async

    async def failing_close(current):
        nonlocal attempts
        if current is composition:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("runtime close failed before closing Plane")
        await close(current)

    def admitted_record(*args):
        admissions.append(args[:2])
        return record(*args)

    monkeypatch.setattr(type(composition), "close", failing_close)
    monkeypatch.setattr(state.store, "record_outcome_async", admitted_record)
    with pytest.raises(RuntimeError, match="runtime close failed"):
        await state.orch._close_started_services()
    assert state.orch._started_services_close_task is None
    state.orch._typesafe_record_outcome(state.owner, outcome(state))
    assert admissions == []
    assert state.orch._typesafe_outcomes_closing is True
    await state.orch._close_started_services()
    assert attempts == 2
    await assert_closed(state)
