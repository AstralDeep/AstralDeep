"""Tests for backend/scheduler/runner.py's runner-side guards against real PostgreSQL:
bounded retry counting across re-claims, the stale-occurrence backlog skip, and the
claim-loop ceiling.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator.chain_authority import AuthoritySkip, MachineTurnAuthority
from orchestrator.offline_grant import TokenEndpointUnconfigured
from scheduler.runner import (
    _SKIP_BODY,
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
    scheduled_job_store as ScheduledJobStore,
)
from scheduler.tests.test_occurrence_claims_060 import (
    _coordinator,
    _due_job,
    _dummy_attempt,
    _expire_claim,
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


@pytest.mark.asyncio
async def test_retryable_failures_are_capped_then_pause_and_notify_once(
    clean_database, monkeypatch
):
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "2")
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    job = _due_job(store, "retry-cap")
    runner, calls = _failing_runner(store)

    attempt = _claim_and_start(store, "scheduler-cap")
    assert attempt.claim.attempt_number == 1
    first = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert (first.retryable, first.result_code) == (True, "operation_failed")
    store.finish_attempt(
        attempt,
        outcome=first.outcome,
        summary=first.summary,
        retryable=True,
        result_code=first.result_code,
        retry_after_seconds=0,
    )
    assert calls["notifications"] == []
    assert (
        clean_database.fetch_one(
            "SELECT status FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "active"
    )

    attempt = _claim_and_start(store, "scheduler-cap")
    assert attempt.claim.attempt_number == 2
    second = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert second.retryable is False
    assert second.outcome == "failure"
    assert second.result_code == "attempts_exhausted"
    assert "Scheduled turn failed" in (second.summary or "")
    store.finish_attempt(
        attempt,
        outcome=second.outcome,
        summary=second.summary,
        retryable=second.retryable,
        result_code=second.result_code,
    )

    occurrence = clean_database.fetch_one(
        "SELECT state, attempt_count, last_error_code, next_attempt_at "
        "FROM scheduled_occurrence WHERE job_id = ?",
        (job["id"],),
    )
    assert occurrence["state"] == "failed"
    assert occurrence["attempt_count"] == 2
    assert occurrence["last_error_code"] == "attempts_exhausted"
    assert occurrence["next_attempt_at"] is None
    assert (
        clean_database.fetch_one(
            "SELECT status FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "paused"
    )
    assert len(calls["notifications"]) == 1
    user_id, payload = calls["notifications"][0]
    assert user_id == job["user_id"]
    assert payload["level"] == "warning"
    assert payload["title"] == f"Scheduled job paused: {job['name']}"
    assert "2 times" in payload["body"]
    assert store.materialize_and_claim_due("scheduler-cap", limit=5, lease_seconds=15) == ()


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


@pytest.mark.asyncio
async def test_reclaims_past_the_cap_still_run_a_healthy_job(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "3")
    orchestrator, calls = _recording_orchestrator()
    store = _PauseStore()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    attempt = _attempt_number(_dummy_attempt("reclaimed-healthy"), 4)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert len(calls["turns"]) == 1
    assert store.paused == []
    attempt = _attempt_number(_dummy_attempt("reclaimed-healthy-2"), 29)
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert store.paused == []


@pytest.mark.asyncio
async def test_claim_loop_ceiling_is_a_distinct_non_blaming_terminal(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "3")
    orchestrator, calls = _recording_orchestrator()
    store = _PauseStore()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    attempt = _attempt_number(_dummy_attempt("claim-loop"), claim_loop_ceiling() + 1)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "claim_loop_exhausted"
    assert result.retryable is False
    assert result.outcome == "failure"
    assert calls["turns"] == []
    assert store.paused == [(attempt.job["user_id"], attempt.job["id"], "paused")]
    assert len(calls["notifications"]) == 1
    body = calls["notifications"][0][1]["body"]
    assert "did not fail" in body
    assert "failed" not in body.replace("did not fail", "")
    assert "in a row" not in body

    again = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert again.result_code == "claim_loop_exhausted"
    assert len(calls["notifications"]) == 1


@pytest.mark.asyncio
async def test_genuine_failures_are_counted_across_reclaims(clean_database, monkeypatch):
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "3")
    coordinator = _coordinator(clean_database, scheduled_active=8)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    job = _due_job(store, "reclaims")
    orchestrator, calls = _recording_orchestrator()
    healthy_turn = orchestrator.run_scheduled_turn
    outcomes = iter([RuntimeError("boom")])

    async def flaky_turn(**kwargs):
        try:
            raise next(outcomes)
        except StopIteration:
            return await healthy_turn(**kwargs)

    orchestrator.run_scheduled_turn = flaky_turn
    runner = JobRunner(orchestrator, store, _ValidGrants())

    attempt = _claim_and_start(store, "scheduler-reclaims")
    first = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert (first.result_code, first.retryable) == ("operation_failed", True)
    store.finish_attempt(
        attempt, outcome=first.outcome, summary=first.summary,
        retryable=True, result_code=first.result_code, retry_after_seconds=0,
    )
    for _ in range(3):
        claim = store.materialize_and_claim_due(
            "scheduler-reclaims", limit=1, lease_seconds=15
        )[0]
        _expire_claim(clean_database, claim.occurrence_id)
    attempt = _claim_and_start(store, "scheduler-reclaims")
    assert attempt.claim.attempt_number == 5
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert len(calls["turns"]) == 1
    assert [n[1]["level"] for n in calls["notifications"]] == ["success"]
    store.finish_attempt(
        attempt, outcome=result.outcome, summary=result.summary,
        retryable=False, result_code=result.result_code,
    )
    assert (
        clean_database.fetch_one(
            "SELECT status FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "active"
    )


@pytest.mark.asyncio
async def test_resumed_job_that_exhausts_again_notifies_again(clean_database, monkeypatch):
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "1")
    coordinator = _coordinator(clean_database, scheduled_active=8)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    job = _due_job(store, "resume-again")
    runner, calls = _failing_runner(store)

    attempt = _claim_and_start(store, "scheduler-resume")
    first = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert first.result_code == "attempts_exhausted"
    store.finish_attempt(
        attempt, outcome=first.outcome, summary=first.summary,
        retryable=False, result_code=first.result_code,
    )
    assert len(calls["notifications"]) == 1
    assert "1 time " in calls["notifications"][0][1]["body"]

    assert store.set_status(job["user_id"], job["id"], "active") is True
    due_ms = int(datetime.now(UTC).timestamp() * 1000) - 1_000
    clean_database.execute(
        "UPDATE scheduled_job SET next_run_at = ? WHERE id = ?", (due_ms, job["id"])
    )
    attempt = _claim_and_start(store, "scheduler-resume")
    second = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert second.result_code == "attempts_exhausted"
    assert len(calls["notifications"]) == 2
    assert (
        clean_database.fetch_one(
            "SELECT status FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "paused"
    )


@pytest.mark.asyncio
async def test_dreaming_loop_ceiling_pauses_without_owner_notification(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "3")
    orchestrator, calls = _recording_orchestrator()
    store = _PauseStore()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    attempt = _attempt_number(_dummy_attempt("dreaming-loop"), claim_loop_ceiling() + 1)
    attempt = replace(
        attempt, claim=replace(attempt.claim, job={**attempt.job, "agent_id": "__dreaming__"})
    )
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "claim_loop_exhausted"
    assert store.paused == [(attempt.job["user_id"], attempt.job["id"], "paused")]
    assert calls["notifications"] == []


@pytest.mark.asyncio
async def test_default_cap_is_three(clean_database, monkeypatch):
    monkeypatch.delenv("SCHEDULER_MAX_ATTEMPTS", raising=False)
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "default-cap")
    runner, calls = _failing_runner(store)
    codes = []
    for _ in range(3):
        attempt = _claim_and_start(store, "scheduler-default")
        result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
        codes.append((result.result_code, result.retryable))
        store.finish_attempt(
            attempt,
            outcome=result.outcome,
            summary=result.summary,
            retryable=result.retryable,
            result_code=result.result_code,
            retry_after_seconds=0,
        )
    assert codes == [
        ("operation_failed", True),
        ("operation_failed", True),
        ("attempts_exhausted", False),
    ]
    assert len(calls["notifications"]) == 1


@pytest.mark.asyncio
async def test_stale_backlog_is_skipped_without_authority_or_turn(
    clean_database, monkeypatch
):
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    coordinator = _coordinator(clean_database, scheduled_active=8)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    three_days_ago = int((datetime.now(UTC) - timedelta(days=3)).timestamp() * 1000)
    job = _due_job(store, "stale", due_ms=three_days_ago)

    class _ExplodingGrants(_ValidGrants):
        def latest_valid_for(self, user_id, agent_id):
            raise AssertionError("authority must not be derived for a stale occurrence")

        async def mint_access_token(self, grant_id, *, user_id):
            raise AssertionError("no IdP mint for a stale occurrence")

    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ExplodingGrants())

    attempt = _claim_and_start(store, "scheduler-stale")
    assert attempt.claim.scheduled_for <= datetime.now(UTC) - timedelta(days=2)
    advanced = clean_database.fetch_one(
        "SELECT next_run_at FROM scheduled_job WHERE id = ?", (job["id"],)
    )["next_run_at"]
    assert advanced == three_days_ago + 3600 * 1000
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "skipped_stale"
    assert result.retryable is False
    assert "Skipped" in (result.summary or "")
    assert calls["turns"] == []
    assert len(calls["notifications"]) == 1
    notice = calls["notifications"][0][1]
    assert notice["level"] == "warning"
    assert notice["job_id"] == job["id"]
    assert "Skipped 7" in notice["body"]
    assert "missed runs" in notice["body"]

    store.finish_attempt(
        attempt,
        outcome=result.outcome,
        summary=result.summary,
        auth_ref=result.auth_ref,
        retryable=result.retryable,
        result_code=result.result_code,
    )
    occurrence = clean_database.fetch_one(
        "SELECT state, last_error_code FROM scheduled_occurrence WHERE job_id = ?",
        (job["id"],),
    )
    assert dict(occurrence) == {"state": "failed", "last_error_code": "skipped_stale"}
    run = clean_database.fetch_one(
        "SELECT outcome, summary FROM job_run WHERE job_id = ?", (job["id"],)
    )
    assert run["outcome"] == "failure"
    assert "Skipped" in run["summary"]
    assert (
        clean_database.fetch_one(
            "SELECT status, next_run_at FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "active"
    )
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM effect_ledger WHERE occurrence_id = ? "
            "AND effect_kind <> 'notification'",
            (str(attempt.claim.occurrence_id),),
        )["n"]
        == 0
    )

    attempt = _claim_and_start(store, "scheduler-stale")
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "skipped_stale"
    assert len(calls["notifications"]) == 1


@pytest.mark.asyncio
async def test_last_catch_up_of_a_stale_backlog_runs(clean_database, monkeypatch):
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    five_hours_ago = int((datetime.now(UTC) - timedelta(hours=5)).timestamp() * 1000)
    job = store.create_job(
        "owner-daily",
        name="Job daily",
        instruction="daily digest",
        schedule_kind="interval",
        schedule_expr="1d",
        timezone="UTC",
        consented_scopes=[],
        agent_id=None,
        target_chat_id="chat-daily",
        next_run_at=five_hours_ago,
        offline_grant_id=None,
    )
    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    attempt = _claim_and_start(store, "scheduler-daily")
    assert occurrence_is_stale(attempt.claim.scheduled_for)
    advanced = clean_database.fetch_one(
        "SELECT next_run_at FROM scheduled_job WHERE id = ?", (job["id"],)
    )["next_run_at"]
    assert advanced > int(datetime.now(UTC).timestamp() * 1000)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert len(calls["turns"]) == 1
    assert [n[1]["level"] for n in calls["notifications"]] == ["success"]


@pytest.mark.asyncio
async def test_one_shot_is_never_stale_skipped(clean_database, monkeypatch):
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    when = (datetime.now(UTC) - timedelta(days=3)).replace(microsecond=0)
    job = store.create_job(
        "owner-oneshot",
        name="Job oneshot",
        instruction="single deliberate task",
        schedule_kind="one_shot",
        schedule_expr=when.isoformat(),
        timezone="UTC",
        consented_scopes=[],
        agent_id=None,
        target_chat_id="chat-oneshot",
        next_run_at=int(when.timestamp() * 1000),
        offline_grant_id=None,
    )
    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    attempt = _claim_and_start(store, "scheduler-oneshot")
    assert (
        clean_database.fetch_one(
            "SELECT status FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "completed"
    )
    assert occurrence_is_stale(attempt.claim.scheduled_for)
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert len(calls["turns"]) == 1


@pytest.mark.asyncio
async def test_stale_policy_matrix_with_fake_store(monkeypatch):
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    orchestrator, calls = _recording_orchestrator()
    past_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    future_ms = int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000)
    store = _PauseStore(next_run_at=past_ms)
    runner = JobRunner(orchestrator, store, _ValidGrants())

    def stale_attempt(label, *, naive=False, **job_overrides):
        attempt = _dummy_attempt(label)
        when = datetime.now(UTC) - timedelta(days=40)
        if naive:
            when = when.replace(tzinfo=None)
        claim = replace(
            attempt.claim,
            scheduled_for=when,
            job={**attempt.job, **job_overrides},
        )
        return replace(attempt, claim=claim)

    result = await runner.run_occurrence(
        stale_attempt("matrix", naive=True), claim_lost=asyncio.Event()
    )
    assert result.result_code == "skipped_stale"
    assert calls["turns"] == []
    assert len(calls["notifications"]) == 1
    result = await runner.run_occurrence(stale_attempt("matrix"), claim_lost=asyncio.Event())
    assert result.result_code == "skipped_stale"
    assert len(calls["notifications"]) == 1

    store.next_run_at = future_ms
    result = await runner.run_occurrence(stale_attempt("matrix"), claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert len(calls["turns"]) == 1
    store.next_run_at = past_ms
    result = await runner.run_occurrence(stale_attempt("matrix"), claim_lost=asyncio.Event())
    assert result.result_code == "skipped_stale"
    assert [n[1]["level"] for n in calls["notifications"]] == [
        "warning", "success", "warning"
    ]

    result = await runner.run_occurrence(
        stale_attempt("matrix-oneshot", schedule_kind="one_shot"),
        claim_lost=asyncio.Event(),
    )
    assert result.result_code == "success"
    assert len(calls["turns"]) == 2


@pytest.mark.asyncio
async def test_fresh_occurrence_inside_grace_still_runs(clean_database, monkeypatch):
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "3600")
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    thirty_minutes_ago = int(
        (datetime.now(UTC) - timedelta(minutes=30)).timestamp() * 1000
    )
    _due_job(store, "fresh", due_ms=thirty_minutes_ago)
    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    attempt = _claim_and_start(store, "scheduler-fresh")
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert len(calls["turns"]) == 1


@pytest.mark.asyncio
async def test_stale_guard_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "0")
    orchestrator, calls = _recording_orchestrator()

    class _Store:
        def reserve_atomic_chat_effect(self, attempt, **kwargs):
            return SimpleNamespace(state="reserved", ambiguous=False, created=True)

        def reserve_effect(self, attempt, **kwargs):
            return SimpleNamespace(state="reserved", ambiguous=False, created=True)

        def publish_effect(self, attempt, **kwargs):
            return None

    runner = JobRunner(orchestrator, _Store(), _ValidGrants())
    attempt = _dummy_attempt("stale-disabled")
    attempt = replace(
        attempt,
        claim=replace(
            attempt.claim, scheduled_for=datetime.now(UTC) - timedelta(days=40)
        ),
    )
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "success"
    assert len(calls["turns"]) == 1


@pytest.mark.asyncio
async def test_unconfigured_token_endpoint_is_a_named_skip_reason():
    class _Grants(_ValidGrants):
        async def mint_access_token(self, grant_id, *, user_id):
            raise TokenEndpointUnconfigured("no IdP token endpoint configured")

    orch = SimpleNamespace(
        tool_permissions=SimpleNamespace(get_enabled_scope_names=lambda *_: [])
    )
    skip = await MachineTurnAuthority(orch, _Grants()).derive(
        user_id="u1",
        agent_id=None,
        consented_scopes=[],
        grant_id="g1",
        turn_class="scheduled_job",
    )
    assert isinstance(skip, AuthoritySkip)
    assert skip.reason == "token_endpoint_unconfigured"
    assert "KEYCLOAK_AUTHORITY" in _SKIP_BODY[skip.reason]
    assert "administrator" in _SKIP_BODY[skip.reason]


@pytest.mark.asyncio
async def test_unconfigured_token_endpoint_pauses_with_operator_copy(clean_database):
    class _Grants(_ValidGrants):
        async def mint_access_token(self, grant_id, *, user_id):
            raise TokenEndpointUnconfigured("no IdP token endpoint configured")

    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    job = _due_job(store, "endpoint-unconfigured")
    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _Grants())
    attempt = _claim_and_start(store, "scheduler-endpoint")
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.outcome == "skipped_auth"
    assert result.summary == "identity provider token endpoint not configured"
    assert calls["turns"] == []
    assert len(calls["notifications"]) == 1
    assert "KEYCLOAK_AUTHORITY" in calls["notifications"][0][1]["body"]
    assert (
        clean_database.fetch_one(
            "SELECT status FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "paused"
    )
