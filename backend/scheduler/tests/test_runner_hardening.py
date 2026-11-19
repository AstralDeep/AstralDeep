"""Tests for backend/scheduler/runner.py: environment-tunable claim limits and the
turn_engine_retired refusal of every non-dreaming job in JobRunner.run_occurrence and
run_job, while dreaming jobs still reach their own execution paths.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scheduler.runner import JobRunner, claim_loop_ceiling, max_attempts
from scheduler.tests.test_occurrence_claims_060 import _dummy_attempt


def test_knobs_are_read_per_call_with_floors(monkeypatch):
    monkeypatch.delenv("SCHEDULER_MAX_ATTEMPTS", raising=False)
    assert max_attempts() == 3

    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "5")
    assert max_attempts() == 5

    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "0")
    assert max_attempts() == 1
    assert claim_loop_ceiling() == 10
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "3")
    assert claim_loop_ceiling() == 30


def _runner():
    store = MagicMock()
    orch = SimpleNamespace(
        runtime_observability=MagicMock(),
        notify_user=AsyncMock(),
        run_scheduled_turn=AsyncMock(),
    )
    return JobRunner(orch, store), orch, store


def _job(**over):
    job = {
        "id": "job-1",
        "user_id": "u1",
        "name": "Digest",
        "instruction": "summarize recent findings",
        "agent_id": "web-research-1",
        "schedule_kind": "interval",
        "schedule_expr": "1d",
        "timezone": "UTC",
        "offline_grant_id": "grant-1",
        "target_chat_id": "c1",
        "consented_scopes": ["tools:read"],
    }
    job.update(over)
    return job


@pytest.mark.asyncio
async def test_run_occurrence_refuses_a_chat_job_without_effects_or_publication():
    runner, orch, store = _runner()
    attempt = _dummy_attempt("retired-occurrence")

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())

    assert result.outcome == "failure"
    assert result.result_code == "turn_engine_retired"
    assert result.retryable is False
    assert result.summary == "Scheduled turns are retired. Turns run on the Kernel engine."
    assert result.auth_ref == str(attempt.operation_id)
    orch.runtime_observability.record_scheduler.assert_called_once_with(
        "terminal", job_type="scheduled_chat", result_code="turn_engine_retired"
    )
    assert store.method_calls == []
    orch.notify_user.assert_not_awaited()
    orch.run_scheduled_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_occurrence_dispatches_dreaming_jobs_unchanged(monkeypatch):
    runner, orch, _store = _runner()
    attempt = _dummy_attempt("dreaming-occurrence")
    attempt.claim.job["agent_id"] = "__dreaming__"
    sentinel = object()
    dreaming = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(runner, "_run_dreaming_occurrence", dreaming)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())

    assert result is sentinel
    dreaming.assert_awaited_once_with(attempt)
    orch.runtime_observability.record_scheduler.assert_not_called()


@pytest.mark.asyncio
async def test_run_job_refuses_a_chat_job_without_touching_the_store():
    runner, orch, store = _runner()

    assert await runner.run_job(_job()) == "failure"

    orch.runtime_observability.record_scheduler.assert_called_once_with(
        "terminal", job_type="scheduled_chat", result_code="turn_engine_retired"
    )
    assert store.method_calls == []
    orch.notify_user.assert_not_awaited()
    orch.run_scheduled_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_job_dispatches_dreaming_jobs_unchanged(monkeypatch):
    runner, orch, _store = _runner()
    dreaming = AsyncMock(return_value="success")
    monkeypatch.setattr(runner, "_run_dreaming", dreaming)

    assert await runner.run_job(_job(agent_id="__dreaming__")) == "success"

    assert dreaming.await_count == 1
    orch.runtime_observability.record_scheduler.assert_not_called()
