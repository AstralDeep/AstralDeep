"""Tests for orchestrator/concurrency_cap.py: acquire/release semantics, idempotent
double-acquire, per-(user, agent) isolation, and behavior under concurrent acquires.
"""

import asyncio

import pytest

from orchestrator.concurrency_cap import ConcurrencyCap


@pytest.mark.asyncio
async def test_acquire_under_cap_returns_true() -> None:
    cap = ConcurrencyCap(max_per_user_agent=3)
    assert await cap.acquire("u", "classify-1", "job-1") is True
    assert await cap.acquire("u", "classify-1", "job-2") is True
    assert await cap.acquire("u", "classify-1", "job-3") is True
    assert cap.inflight_count("u", "classify-1") == 3


@pytest.mark.asyncio
async def test_acquire_at_cap_returns_false() -> None:
    cap = ConcurrencyCap(max_per_user_agent=3)
    for jid in ("a", "b", "c"):
        assert await cap.acquire("u", "classify-1", jid)
    assert await cap.acquire("u", "classify-1", "fourth") is False


@pytest.mark.asyncio
async def test_acquire_same_job_twice_is_idempotent() -> None:
    cap = ConcurrencyCap(max_per_user_agent=3)
    assert await cap.acquire("u", "classify-1", "job-1") is True
    assert await cap.acquire("u", "classify-1", "job-1") is True
    assert cap.inflight_count("u", "classify-1") == 1


@pytest.mark.asyncio
async def test_release_frees_a_slot() -> None:
    cap = ConcurrencyCap(max_per_user_agent=3)
    for jid in ("a", "b", "c"):
        await cap.acquire("u", "classify-1", jid)
    assert await cap.acquire("u", "classify-1", "d") is False
    await cap.release("u", "classify-1", "b")
    assert await cap.acquire("u", "classify-1", "d") is True
    assert "b" not in cap.inflight_jobs("u", "classify-1")
    assert "d" in cap.inflight_jobs("u", "classify-1")


@pytest.mark.asyncio
async def test_release_unknown_job_is_noop() -> None:
    cap = ConcurrencyCap(max_per_user_agent=3)
    await cap.release("u", "classify-1", "never-existed")
    assert cap.inflight_count("u", "classify-1") == 0


@pytest.mark.asyncio
async def test_distinct_user_agent_pairs_are_isolated() -> None:
    cap = ConcurrencyCap(max_per_user_agent=3)
    for jid in ("a", "b", "c"):
        await cap.acquire("alice", "classify-1", jid)
    assert await cap.acquire("bob", "classify-1", "x") is True
    assert await cap.acquire("alice", "forecaster-1", "y") is True


@pytest.mark.asyncio
async def test_inflight_jobs_lists_active_set() -> None:
    cap = ConcurrencyCap()
    for jid in ("c", "a", "b"):
        await cap.acquire("u", "ag", jid)
    assert cap.inflight_jobs("u", "ag") == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_concurrent_acquires_respect_cap() -> None:
    cap = ConcurrencyCap(max_per_user_agent=3)
    results = await asyncio.gather(*[
        cap.acquire("u", "classify-1", f"j{i}") for i in range(10)
    ])
    assert sum(results) == 3
    assert cap.inflight_count("u", "classify-1") == 3
