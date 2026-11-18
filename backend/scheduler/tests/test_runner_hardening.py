"""Tests for backend/scheduler/runner.py's runner-side guards against real PostgreSQL:
bounded retry counting across re-claims, the stale-occurrence backlog skip, and the
claim-loop ceiling.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from scheduler.runner import (
    JobRunner,
    claim_loop_ceiling,
    estimate_missed_runs,
    max_attempts,
    occurrence_age_seconds,
    occurrence_is_stale,
    stale_grace_seconds,
)
from scheduler.tests.plane_runtime import (
    ensure_plane_runtime,
)
from scheduler.tests.test_occurrence_claims_060 import (
    _recording_orchestrator,
    _ValidGrants,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@pytest.fixture(scope="module")
def postgres_database():
    with isolated_plane_runtime("scheduler_hardening") as runtime:
        yield runtime


@pytest.fixture
def clean_database(postgres_database: PlaneTestRuntime) -> PlaneTestRuntime:
    db = postgres_database
    ensure_plane_runtime(db)
    db.execute("DELETE FROM effect_ledger")
    db.execute("DELETE FROM job_run")
    db.execute("DELETE FROM scheduled_occurrence")
    db.execute("DELETE FROM scheduled_job")
    db.execute("DELETE FROM operation_submission_result")
    db.execute(
        "UPDATE operation_admission_slot SET operation_id = NULL, "
        "lease_token = NULL, lease_expires_at = NULL"
    )
    db.execute("DELETE FROM operation_record")
    return db


def test_knobs_are_read_per_call_with_floors(monkeypatch):
    monkeypatch.delenv("SCHEDULER_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    assert max_attempts() == 3
    assert stale_grace_seconds() == 7200

    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "60")
    assert max_attempts() == 5
    assert stale_grace_seconds() == 60

    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "nope")
    assert max_attempts() == 1
    assert stale_grace_seconds() == 7200
    assert claim_loop_ceiling() == 10
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "3")
    assert claim_loop_ceiling() == 30


def test_occurrence_is_stale_respects_grace_and_disable(monkeypatch):
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    assert occurrence_is_stale(now - timedelta(hours=3), now=now) is True
    assert occurrence_is_stale(now - timedelta(hours=1), now=now) is False
    assert occurrence_is_stale(now.replace(tzinfo=None), now=now) is False
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "0")
    assert occurrence_is_stale(now - timedelta(days=30), now=now) is False


def test_occurrence_age_is_aware_for_naive_and_aware_inputs():
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    aware = now - timedelta(hours=3)
    naive = aware.replace(tzinfo=None)
    assert occurrence_age_seconds(aware, now=now) == 3 * 3600
    assert occurrence_age_seconds(naive, now=now) == 3 * 3600
    assert occurrence_age_seconds(aware, now=now.replace(tzinfo=None)) == 3 * 3600


def test_estimate_missed_runs_walks_the_cadence():
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    now_ms = int(now.timestamp() * 1000)
    job = {"schedule_kind": "interval", "schedule_expr": "1h", "timezone": "UTC"}
    assert estimate_missed_runs(job, now - timedelta(hours=5), now_ms) == 5
    assert estimate_missed_runs(job, now - timedelta(minutes=30), now_ms) == 1
    bad = {"schedule_kind": "interval", "schedule_expr": "???", "timezone": "UTC"}
    assert estimate_missed_runs(bad, now - timedelta(days=2), now_ms) == 1


def _claim_and_start(store, instance: str):
    claim = store.materialize_and_claim_due(instance, limit=1, lease_seconds=15)[0]
    return store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)


def _failing_runner(store):
    async def fail_turn(**kwargs):
        raise RuntimeError("handler failed")

    orchestrator, calls = _recording_orchestrator()
    orchestrator.run_scheduled_turn = fail_turn
    return JobRunner(orchestrator, store, _ValidGrants()), calls


class _PauseStore:
    def __init__(self, *, status="active", next_run_at=None):
        self.paused = []
        self.published = []
        self.status = status
        self.next_run_at = next_run_at

    def set_status(self, user_id, job_id, status):
        self.paused.append((user_id, job_id, status))
        self.status = status
        return True

    def get_job(self, user_id, job_id):
        return {"id": job_id, "user_id": user_id, "status": self.status,
                "next_run_at": self.next_run_at}

    def reserve_effect(self, attempt, **kwargs):
        return SimpleNamespace(state="reserved", ambiguous=False, created=True)

    def reserve_atomic_chat_effect(self, attempt, **kwargs):
        return SimpleNamespace(state="reserved", ambiguous=False, created=True)

    def publish_effect(self, attempt, **kwargs):
        self.published.append(kwargs["effect_key"])


def _attempt_number(attempt, number):
    return replace(attempt, claim=replace(attempt.claim, attempt_number=number))

