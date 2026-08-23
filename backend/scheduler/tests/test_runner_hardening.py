"""Deep-side scheduler hardening ahead of FF_SCHEDULER_EXECUTION=on.

Three runner-side guards, all against the real Plane PostgreSQL runtime:

1. Bounded retries — ``SCHEDULER_MAX_ATTEMPTS`` (default 3) caps retryable
   attempts per occurrence. The last permitted attempt that fails settles the
   occurrence ``failed``/``attempts_exhausted`` (NOT retryable), pauses the
   job, and notifies the owner exactly once. Pre-fix the occurrence was
   re-claimed every tick forever.
2. Stale-occurrence guard — an occurrence whose ``scheduled_for`` is older
   than ``SCHEDULER_STALE_GRACE_SECONDS`` (default 2h) is completed as
   ``skipped_stale`` with no authority derivation, no LLM turn, and no
   notification, so a backlog burns cheaply.
3. The token endpoint resolution fail-closed reason
   (``token_endpoint_unconfigured``) reaches the owner with actionable copy.
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
    max_attempts,
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


# ---------------------------------------------------------------------------
# Knobs are read per call
# ---------------------------------------------------------------------------


def test_knobs_are_read_per_call_with_floors(monkeypatch):
    monkeypatch.delenv("SCHEDULER_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    assert max_attempts() == 3
    assert stale_grace_seconds() == 7200

    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "60")
    assert max_attempts() == 5
    assert stale_grace_seconds() == 60

    # Floors: the cap can never be 0 (nothing would ever run); garbage falls
    # back to the default rather than crashing the loop.
    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "nope")
    assert max_attempts() == 1
    assert stale_grace_seconds() == 7200


def test_occurrence_is_stale_respects_grace_and_disable(monkeypatch):
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    assert occurrence_is_stale(now - timedelta(hours=3), now=now) is True
    assert occurrence_is_stale(now - timedelta(hours=1), now=now) is False
    # Naive timestamps are treated as UTC, never as "always stale".
    assert occurrence_is_stale(now.replace(tzinfo=None), now=now) is False
    monkeypatch.setenv("SCHEDULER_STALE_GRACE_SECONDS", "0")
    assert occurrence_is_stale(now - timedelta(days=30), now=now) is False


# ---------------------------------------------------------------------------
# Bounded retries
# ---------------------------------------------------------------------------


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

    # Attempt 1: ordinary retryable failure.
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

    # Attempt 2 == cap: same failure now exhausts the occurrence.
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
    # Exactly one owner notification, mirroring the auth-skip shape.
    assert len(calls["notifications"]) == 1
    user_id, payload = calls["notifications"][0]
    assert user_id == job["user_id"]
    assert payload["level"] == "warning"
    assert payload["title"] == f"Scheduled job paused: {job['name']}"
    assert "2 times" in payload["body"]
    # A failed occurrence is NEVER re-claimed by a later poll.
    assert store.materialize_and_claim_due("scheduler-cap", limit=5, lease_seconds=15) == ()


@pytest.mark.asyncio
async def test_attempt_beyond_cap_is_exhausted_before_any_dispatch(monkeypatch):
    """Lease-loss re-claims also bump attempt_count; past the cap nothing runs."""

    monkeypatch.setenv("SCHEDULER_MAX_ATTEMPTS", "3")
    orchestrator, calls = _recording_orchestrator()
    paused = []
    notified = []

    class _Store:
        def set_status(self, user_id, job_id, status):
            paused.append((user_id, job_id, status))
            return True

        def reserve_effect(self, attempt, **kwargs):
            return SimpleNamespace(state="reserved", ambiguous=False, created=True)

        def publish_effect(self, attempt, **kwargs):
            notified.append(kwargs["effect_key"])

    runner = JobRunner(orchestrator, _Store(), _ValidGrants())
    attempt = _dummy_attempt("beyond-cap")
    attempt = replace(attempt, claim=replace(attempt.claim, attempt_number=4))

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "attempts_exhausted"
    assert result.retryable is False
    assert calls["turns"] == []  # no LLM turn, no authority derivation
    assert paused == [(attempt.job["user_id"], attempt.job["id"], "paused")]
    assert len(calls["notifications"]) == 1

    # Settling the same occurrence again does not re-notify.
    again = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert again.result_code == "attempts_exhausted"
    assert len(calls["notifications"]) == 1


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


# ---------------------------------------------------------------------------
# Stale-occurrence guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_backlog_is_skipped_without_authority_or_turn(
    clean_database, monkeypatch
):
    monkeypatch.delenv("SCHEDULER_STALE_GRACE_SECONDS", raising=False)
    coordinator = _coordinator(clean_database)
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
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == "skipped_stale"
    assert result.retryable is False
    assert "Skipped" in (result.summary or "")
    assert calls["turns"] == []
    assert calls["notifications"] == []

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
    # Plane's job_run.outcome CHECK constraint has no 'skipped_stale' member;
    # the honest marker rides result_code + summary.
    assert run["outcome"] == "failure"
    assert "Skipped" in run["summary"]
    # The job itself is NOT paused: the materializer advanced next_run_at and
    # the next fresh occurrence runs normally.
    assert (
        clean_database.fetch_one(
            "SELECT status, next_run_at FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "active"
    )
    # Effect ledger untouched: nothing was reserved for the stale occurrence.
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM effect_ledger WHERE occurrence_id = ?",
            (str(attempt.claim.occurrence_id),),
        )["n"]
        == 0
    )


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


# ---------------------------------------------------------------------------
# Token endpoint fail-closed reason reaches the runner
# ---------------------------------------------------------------------------


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
