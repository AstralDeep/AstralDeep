"""Feature-088 T039/T040 (Deep half): scheduler policy, admission, and Stop.

Real-PostgreSQL tests: owner-scoped CAS, occurrence-to-assignment binding, and
the finite allowance are database concurrency/consistency properties over the
Plane 088.007 ``SchedulerRepository`` contract (``get_job_policy``/
``put_job_policy``/``admit_assignment_episode``/``stop_assignment_job``). These
exercise the Deep-side wrapper in ``scheduler.store`` and the admission gate
wired into ``scheduler.runner.JobRunner.run_occurrence`` — the actual
minting/continuation of a monitoring assignment for a FIRST-ever policy job is
a separate, injectable ``monitoring_dispatcher`` seam (see
``scheduler.runner.default_monitoring_dispatcher``), exercised here with a
test double standing in for that not-yet-wired creation path.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    WorkAdmissionCoordinator,
)
from scheduler.runner import JobRunner, default_monitoring_dispatcher
from scheduler.store import ScheduleActionError
from scheduler.tests.plane_runtime import (
    ensure_plane_runtime,
    scheduled_job_store as ScheduledJobStore,
    work_admission_repository,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    """Create one isolated database initialized only by AstralPlane."""

    with isolated_plane_runtime("policy_allowance_088") as runtime:
        yield runtime


@pytest.fixture
def clean_database(postgres_database: PlaneTestRuntime) -> PlaneTestRuntime:
    db = postgres_database
    ensure_plane_runtime(db)
    db.execute("DELETE FROM scheduled_occurrence_assignment")
    db.execute("DELETE FROM scheduled_job_policy")
    db.execute("DELETE FROM effect_ledger")
    db.execute("DELETE FROM job_run")
    db.execute("DELETE FROM scheduled_occurrence")
    db.execute("DELETE FROM scheduled_job")
    db.execute("DELETE FROM persistent_assignment_event")
    db.execute("DELETE FROM persistent_assignment_action")
    db.execute("DELETE FROM persistent_assignment")
    db.execute("DELETE FROM operation_submission_result")
    db.execute(
        "UPDATE operation_admission_slot SET operation_id = NULL, "
        "lease_token = NULL, lease_expires_at = NULL"
    )
    db.execute("DELETE FROM operation_record")
    return db


def _coordinator(db: PlaneTestRuntime) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=8,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="policy-allowance-088-test",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.SCHEDULED,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=4,
                queue_limit=20,
                max_wait_ms=30_000,
                config_revision="policy-allowance-088-test",
            ),
        ),
        repository=work_admission_repository(db),
        operation_retention=timedelta(hours=24),
        slot_lease=timedelta(seconds=90),
    )


def _job(
    store: ScheduledJobStore,
    *,
    owner: str,
    label: str,
    due: bool = True,
) -> dict[str, Any]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return store.create_job(
        owner,
        name=f"Job {label}",
        instruction=f"perform deterministic monitoring check {label}",
        schedule_kind="interval",
        schedule_expr="1h",
        timezone="UTC",
        consented_scopes=[],
        agent_id=None,
        target_chat_id=f"chat-{label}",
        next_run_at=now_ms - 1_000 if due else now_ms + 3_600_000,
        offline_grant_id=None,
    )


def _assignment(db: PlaneTestRuntime, *, owner: str, lifecycle: str = "active") -> str:
    """Insert a minimal, valid ``persistent_assignment`` row for admission tests."""

    assignment_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO persistent_assignment (
            id, owner_user_id, submission_id, submission_digest, lifecycle,
            state_version, data
        ) VALUES (?, ?, ?, ?, ?, 1, ?::jsonb)
        """,
        (
            assignment_id,
            owner,
            str(uuid.uuid4()),
            "a" * 64,
            lifecycle,
            f'{{"lifecycle": "{lifecycle}", "state_version": 1}}',
        ),
    )
    return assignment_id


def _set_lifecycle(db: PlaneTestRuntime, assignment_id: str, lifecycle: str) -> None:
    db.execute(
        "UPDATE persistent_assignment SET lifecycle = ?, "
        "data = jsonb_set(data, '{lifecycle}', to_jsonb(?::text)) WHERE id = ?",
        (lifecycle, lifecycle, assignment_id),
    )


def _claim_one(store: ScheduledJobStore, instance: str, *, job_id: str):
    """Materialize+claim due work and return the one claim for ``job_id``."""

    claims = store.materialize_and_claim_due(instance, limit=10)
    matches = [claim for claim in claims if str(claim.job["id"]) == job_id]
    assert len(matches) == 1, f"expected exactly one claim for {job_id}, got {claims}"
    return matches[0]


def _run_now(store: ScheduledJobStore, *, owner: str, job_id: str) -> None:
    store.materialize_run_now(
        user_id=owner,
        job_id=job_id,
        submission_id=uuid.uuid4(),
        eligibility=lambda _job: True,
    )


# ---------------------------------------------------------------------------
# Store-level: policy CRUD
# ---------------------------------------------------------------------------


def test_get_job_policy_is_none_for_a_legacy_job(clean_database: PlaneTestRuntime) -> None:
    store = ScheduledJobStore(clean_database)
    job = _job(store, owner="owner-legacy", label="legacy")
    assert store.get_job_policy("owner-legacy", str(job["id"])) is None


def test_set_job_policy_creates_then_updates_under_version_cas(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database)
    job = _job(store, owner="owner-policy-cas", label="policy-cas")
    job_id = str(job["id"])

    created = store.set_job_policy(
        "owner-policy-cas", job_id, max_runs=5, monitor_changes=False, expected_version=0,
    )
    assert created == {
        "job_id": job_id, "owner_id": "owner-policy-cas", "version": 1,
        "max_runs": 5, "admitted_runs": 0, "per_episode_limits": {},
        "max_outstanding_episodes": 1, "monitor_changes": False,
        "definition_revision": 1, "terminal_stop": False,
        "last_assignment_id": None, "updated_at": created["updated_at"],
    }

    updated = store.set_job_policy(
        "owner-policy-cas", job_id, max_runs=10, monitor_changes=True, expected_version=1,
    )
    assert updated["version"] == 2
    assert updated["max_runs"] == 10
    assert updated["monitor_changes"] is True
    # Scheduler-owned fields are untouched by the owner-editable form.
    assert updated["admitted_runs"] == 0
    assert updated["terminal_stop"] is False
    assert updated["last_assignment_id"] is None

    with pytest.raises(ScheduleActionError) as excinfo:
        store.set_job_policy(
            "owner-policy-cas", job_id, max_runs=1, monitor_changes=False, expected_version=1,
        )
    assert excinfo.value.code == "schedule_policy_version_conflict"

    with pytest.raises(ScheduleActionError) as excinfo:
        store.set_job_policy(
            "owner-policy-cas", job_id, max_runs=1, monitor_changes=False, expected_version=0,
        )
    assert excinfo.value.code == "schedule_policy_version_conflict"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_runs": 0, "monitor_changes": False, "expected_version": 0},
        {"max_runs": -1, "monitor_changes": False, "expected_version": 0},
        {"max_runs": 1_000_001, "monitor_changes": False, "expected_version": 0},
        {"max_runs": None, "monitor_changes": "yes", "expected_version": 0},
        {"max_runs": None, "monitor_changes": False, "expected_version": -1},
    ],
)
def test_set_job_policy_validates_bounds_before_any_sql(
    clean_database: PlaneTestRuntime, kwargs: dict,
) -> None:
    store = ScheduledJobStore(clean_database)
    job = _job(store, owner="owner-policy-bounds", label="policy-bounds")
    with pytest.raises(ValueError):
        store.set_job_policy("owner-policy-bounds", str(job["id"]), **kwargs)
    assert store.get_job_policy("owner-policy-bounds", str(job["id"])) is None


# ---------------------------------------------------------------------------
# Store-level: admission (finite allowance, outstanding cap, terminal stop)
# ---------------------------------------------------------------------------


def test_admit_episode_admits_then_replays_idempotently(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database)
    owner = "owner-admit-replay"
    job = _job(store, owner=owner, label="admit-replay")
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    assignment_id = _assignment(clean_database, owner=owner)
    claim = _claim_one(store, "admit-replay", job_id=job_id)

    admitted = store.admit_episode(claim, assignment_id=assignment_id)
    assert admitted.admitted is True
    assert admitted.created is True
    assert admitted.reason == "admitted"
    assert str(admitted.assignment_id) == assignment_id
    assert admitted.policy["admitted_runs"] == 1

    replay = store.admit_episode(claim, assignment_id=assignment_id)
    assert replay.admitted is True
    assert replay.created is False
    assert replay.reason == "replayed"
    assert replay.policy["admitted_runs"] == 1

    binding = clean_database.fetch_one(
        "SELECT assignment_id FROM scheduled_occurrence_assignment WHERE occurrence_id = ?",
        (str(claim.occurrence_id),),
    )
    assert str(binding["assignment_id"]) == assignment_id


def test_admit_episode_refuses_a_second_outstanding_episode_and_charges_nothing(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database)
    owner = "owner-outstanding"
    job = _job(store, owner=owner, label="outstanding", due=False)
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    assignment_a = _assignment(clean_database, owner=owner)
    assignment_b = _assignment(clean_database, owner=owner)

    _run_now(store, owner=owner, job_id=job_id)
    _run_now(store, owner=owner, job_id=job_id)
    claims = store.materialize_and_claim_due("outstanding", limit=10)
    assert len(claims) == 2
    claim_one, claim_two = claims

    first = store.admit_episode(claim_one, assignment_id=assignment_a)
    assert first.admitted is True

    refused = store.admit_episode(claim_two, assignment_id=assignment_b)
    assert refused.admitted is False
    assert refused.reason == "episode_outstanding"
    assert refused.policy["admitted_runs"] == 1  # nothing charged

    # Once A resolves it no longer counts as outstanding, so B can admit.
    _set_lifecycle(clean_database, assignment_a, "completed")
    second = store.admit_episode(claim_two, assignment_id=assignment_b)
    assert second.admitted is True
    assert second.reason == "admitted"
    assert second.policy["admitted_runs"] == 2


def test_admit_episode_refuses_when_allowance_is_exhausted(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database)
    owner = "owner-exhausted"
    job = _job(store, owner=owner, label="exhausted", due=False)
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=1, monitor_changes=False, expected_version=0)
    assignment_a = _assignment(clean_database, owner=owner)
    assignment_b = _assignment(clean_database, owner=owner)

    _run_now(store, owner=owner, job_id=job_id)
    claim_one = _claim_one(store, "exhausted-1", job_id=job_id)
    admitted = store.admit_episode(claim_one, assignment_id=assignment_a)
    assert admitted.admitted is True
    _set_lifecycle(clean_database, assignment_a, "completed")

    _run_now(store, owner=owner, job_id=job_id)
    claim_two = _claim_one(store, "exhausted-2", job_id=job_id)
    refused = store.admit_episode(claim_two, assignment_id=assignment_b)
    assert refused.admitted is False
    assert refused.reason == "allowance_exhausted"
    assert refused.policy["admitted_runs"] == 1


def test_admit_episode_refuses_after_terminal_stop_of_a_running_claim(
    clean_database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = "owner-stop-admit"
    job = _job(store, owner=owner, label="stop-admit")
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    assignment_id = _assignment(clean_database, owner=owner)
    claim = _claim_one(store, "stop-admit", job_id=job_id)
    # Started (running) occurrences are NOT cancelled by Stop, unlike
    # pending/claimed ones — the in-flight run is allowed to settle.
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)

    outcome = store.stop_job(owner, job_id, expected_version=1)
    assert outcome.stopped is True

    refused = store.admit_episode(attempt.claim, assignment_id=assignment_id)
    assert refused.admitted is False
    assert refused.reason == "terminal_stop"
    assert refused.policy["admitted_runs"] == 0


# ---------------------------------------------------------------------------
# Store-level: Stop
# ---------------------------------------------------------------------------


def test_stop_job_cancels_unstarted_occurrence_and_blocks_the_next_scan(
    clean_database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = "owner-stop-scan"
    job = _job(store, owner=owner, label="stop-scan")
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    claim = _claim_one(store, "stop-scan", job_id=job_id)  # claimed, not started

    outcome = store.stop_job(owner, job_id, expected_version=1)
    assert outcome.stopped is True
    assert str(claim.occurrence_id) in {str(v) for v in outcome.cancelled_occurrence_ids}
    assert outcome.outstanding_assignment_ids == ()

    job_row = store.get_job(owner, job_id)
    assert job_row is not None
    assert job_row["status"] == "completed"

    # A repeat scan admits nothing new for this job — the terminal Stop is
    # honoured by the ordinary due-scan eligibility path, no special casing.
    assert store.materialize_and_claim_due("stop-scan-again", limit=50) == ()


def test_stop_job_is_idempotent_and_repeats_the_same_outstanding_families(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database)
    owner = "owner-stop-idempotent"
    job = _job(store, owner=owner, label="stop-idempotent")
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    assignment_id = _assignment(clean_database, owner=owner)
    claim = _claim_one(store, "stop-idempotent", job_id=job_id)
    admitted = store.admit_episode(claim, assignment_id=assignment_id)

    first = store.stop_job(owner, job_id, expected_version=admitted.policy["version"])
    assert first.stopped is True
    assert {str(v) for v in first.outstanding_assignment_ids} == {assignment_id}

    second = store.stop_job(owner, job_id, expected_version=first.policy["version"])
    assert second.stopped is False
    assert {str(v) for v in second.outstanding_assignment_ids} == {assignment_id}
    # History/charges are retained across the repeat Stop.
    assert second.policy["admitted_runs"] == 1


def test_stop_job_refuses_stale_version_and_missing_policy(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database)
    owner = "owner-stop-refused"
    job = _job(store, owner=owner, label="stop-refused")
    job_id = str(job["id"])

    with pytest.raises(ScheduleActionError) as excinfo:
        store.stop_job(owner, job_id, expected_version=1)
    assert excinfo.value.code == "schedule_policy_missing"

    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    with pytest.raises(ScheduleActionError) as excinfo:
        store.stop_job(owner, job_id, expected_version=99)
    assert excinfo.value.code == "schedule_policy_version_conflict"


# ---------------------------------------------------------------------------
# JobRunner-level: the admission gate wired into run_occurrence
# ---------------------------------------------------------------------------


def _recording_orchestrator():
    calls: dict[str, list] = {"turns": [], "notifications": []}

    async def run_scheduled_turn(**kwargs):
        calls["turns"].append(kwargs)
        scheduled_store = kwargs.get("scheduled_store")
        if scheduled_store is not None:
            scheduled_store.publish_effect(
                kwargs["scheduled_attempt"],
                effect_kind=kwargs["effect_kind"],
                effect_key=kwargs["effect_key"],
                payload_digest=kwargs["payload_digest"],
            )
        return "durable scheduled result"

    async def notify_user(user_id, payload):
        calls["notifications"].append((user_id, payload))

    async def get_system():
        return {"provider": "test"}

    from types import SimpleNamespace

    orchestrator = SimpleNamespace(
        run_scheduled_turn=run_scheduled_turn,
        notify_user=notify_user,
        tool_permissions=SimpleNamespace(get_agent_scopes=lambda *_: {}),
        _llm_store=SimpleNamespace(get_system=get_system),
    )
    return orchestrator, calls


class _ValidGrants:
    def latest_valid_for(self, user_id, agent_id):
        return "grant-policy-088"

    def is_valid(self, grant_id, *, user_id):
        return True

    async def mint_access_token(self, grant_id, *, user_id):
        return "test-access-token"


@pytest.mark.asyncio
async def test_run_occurrence_pauses_and_notifies_once_when_unbound(
    clean_database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = "owner-run-unbound"
    job = _job(store, owner=owner, label="run-unbound")
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    claim = _claim_one(store, "run-unbound", job_id=job_id)
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)

    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    runner.bind_execution_context(coordinator=coordinator, store=store)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())

    assert result.outcome == "failure"
    assert result.result_code == "monitoring_assignment_unbound"
    assert result.retryable is False
    assert calls["turns"] == []
    assert len(calls["notifications"]) == 1
    assert store.get_job(owner, job_id)["status"] == "paused"


@pytest.mark.asyncio
async def test_run_occurrence_admits_and_dispatches_with_an_injected_dispatcher(
    clean_database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = "owner-run-admit"
    job = _job(store, owner=owner, label="run-admit")
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=None, monitor_changes=False, expected_version=0)
    assignment_id = _assignment(clean_database, owner=owner)
    claim = _claim_one(store, "run-admit", job_id=job_id)
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)

    def dispatcher(transaction, job_row, prior_assignment_id):
        assert job_row["id"] == job_id
        assert prior_assignment_id is None  # first-ever episode for this policy
        return assignment_id

    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants(), monitoring_dispatcher=dispatcher)
    runner.bind_execution_context(coordinator=coordinator, store=store)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())

    assert result.outcome == "success"
    assert len(calls["turns"]) == 1
    policy = store.get_job_policy(owner, job_id)
    assert policy["admitted_runs"] == 1
    assert policy["last_assignment_id"] == assignment_id


@pytest.mark.asyncio
async def test_run_occurrence_pauses_and_notifies_once_when_allowance_exhausted(
    clean_database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = "owner-run-exhausted"
    job = _job(store, owner=owner, label="run-exhausted", due=False)
    job_id = str(job["id"])
    store.set_job_policy(owner, job_id, max_runs=1, monitor_changes=False, expected_version=0)
    assignment_a = _assignment(clean_database, owner=owner)
    assignment_b = _assignment(clean_database, owner=owner)

    # Pre-charge the allowance directly, then resolve A so it stops counting
    # as an outstanding episode — isolating the allowance check from the
    # outstanding-episode-cap check exercised in the store-level tests above.
    _run_now(store, owner=owner, job_id=job_id)
    pre_claim = _claim_one(store, "run-exhausted-pre", job_id=job_id)
    assert store.admit_episode(pre_claim, assignment_id=assignment_a).admitted is True
    _set_lifecycle(clean_database, assignment_a, "completed")

    _run_now(store, owner=owner, job_id=job_id)
    claim = _claim_one(store, "run-exhausted", job_id=job_id)
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)

    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(
        orchestrator, store, _ValidGrants(),
        monitoring_dispatcher=lambda transaction, job_row, prior: assignment_b,
    )
    runner.bind_execution_context(coordinator=coordinator, store=store)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())

    assert result.outcome == "failure"
    assert result.result_code == "allowance_exhausted"
    assert result.retryable is False
    assert calls["turns"] == []
    assert len(calls["notifications"]) == 1
    assert store.get_job(owner, job_id)["status"] == "paused"
    assert store.get_job_policy(owner, job_id)["admitted_runs"] == 1


@pytest.mark.asyncio
async def test_run_occurrence_is_unaffected_for_a_legacy_job_without_a_policy(
    clean_database: PlaneTestRuntime,
) -> None:
    """Regression (FR-008): cron/interval/one-shot cadence and dispatch are
    byte-identical when a job carries no policy row at all."""

    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = "owner-run-legacy"
    job = _job(store, owner=owner, label="run-legacy")
    job_id = str(job["id"])
    assert store.get_job_policy(owner, job_id) is None
    claim = _claim_one(store, "run-legacy", job_id=job_id)
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)

    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    runner.bind_execution_context(coordinator=coordinator, store=store)

    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())

    assert result.outcome == "success"
    assert len(calls["turns"]) == 1
    assert store.get_job_policy(owner, job_id) is None


def test_default_monitoring_dispatcher_reuses_the_bound_assignment_and_refuses_when_unbound() -> None:
    assert default_monitoring_dispatcher(object(), {"id": "job-1"}, "assignment-1") == "assignment-1"
    with pytest.raises(ScheduleActionError) as excinfo:
        default_monitoring_dispatcher(object(), {"id": "job-1"}, None)
    assert excinfo.value.code == "monitoring_assignment_unbound"
