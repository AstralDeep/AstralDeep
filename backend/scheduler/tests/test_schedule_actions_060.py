"""Feature-060 run-now and job-lifecycle action contracts.

These are real-PostgreSQL tests because owner-scoped idempotency, job/occurrence
locking, and operation cancellation are database concurrency properties.  The
public API and Chrome adapters have separate focused fake-backed tests.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    WorkAdmissionCoordinator,
)
from scheduler.tests.plane_runtime import (
    ensure_plane_runtime,
    scheduled_job_store as ScheduledJobStore,
    work_admission_repository,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    """Create one isolated database initialized only by AstralPlane."""

    with isolated_plane_runtime("schedule_actions") as runtime:
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


def _coordinator(db: PlaneTestRuntime) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=8,
                queue_limit=0,
                max_wait_ms=None,
                config_revision="schedule-actions-060-test",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.SCHEDULED,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=4,
                queue_limit=20,
                max_wait_ms=30_000,
                config_revision="schedule-actions-060-test",
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
    due: bool = False,
) -> dict[str, Any]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return store.create_job(
        owner,
        name=f"Job {label}",
        instruction=f"perform deterministic task {label}",
        schedule_kind="interval",
        schedule_expr="1h",
        timezone="UTC",
        consented_scopes=[],
        agent_id=None,
        target_chat_id=f"chat-{label}",
        next_run_at=now_ms - 1_000 if due else now_ms + 3_600_000,
        offline_grant_id=None,
    )


def _value(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return value[field]
    return getattr(value, field)


def _occurrence_value(result: Any, field: str) -> Any:
    occurrence = (
        result.get("occurrence", result)
        if isinstance(result, dict)
        else getattr(result, "occurrence", result)
    )
    return _value(occurrence, field)


def _created(result: Any) -> bool:
    if isinstance(result, dict) and "created" in result:
        return bool(result["created"])
    if hasattr(result, "created"):
        return bool(result.created)
    return not bool(_value(result, "duplicate"))


def _run_now(
    store: ScheduledJobStore,
    *,
    owner: str,
    job_id: str,
    submission_id: uuid.UUID,
) -> Any:
    return store.materialize_run_now(
        user_id=owner,
        job_id=job_id,
        submission_id=submission_id,
        eligibility=lambda _job_record: True,
    )


def _assert_code(exc: BaseException, expected: str) -> None:
    assert getattr(exc, "code", None) == expected or expected in str(exc)


def test_schema_has_owner_scoped_run_now_submission_identity(
    postgres_database: PlaneTestRuntime,
) -> None:
    column = postgres_database.fetch_one(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'scheduled_occurrence'
          AND column_name = 'run_now_submission_id'
        """
    )
    assert column is not None
    assert dict(column) == {"data_type": "uuid", "is_nullable": "YES"}

    indexes = postgres_database.fetch_all(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'scheduled_occurrence'
          AND indexdef ILIKE '%run_now_submission_id%'
        """
    )
    assert len(indexes) == 1
    indexdef = str(indexes[0]["indexdef"]).lower()
    assert "unique index" in indexdef
    assert "owner_user_id" in indexdef
    assert "run_now_submission_id" in indexdef
    assert "where" in indexdef and "is not null" in indexdef


def test_concurrent_and_replayed_run_now_resolve_one_occurrence_without_cadence_change(
    clean_database: PlaneTestRuntime,
) -> None:
    coordinator = _coordinator(clean_database)
    stores = (
        ScheduledJobStore(clean_database, coordinator=coordinator),
        ScheduledJobStore(clean_database, coordinator=coordinator),
    )
    owner = "owner-run-now-replay"
    job = _job(stores[0], owner=owner, label="run-now-replay")
    cadence_before = int(job["next_run_at"])
    submission_id = uuid.uuid4()
    barrier = threading.Barrier(2)

    def submit(store: ScheduledJobStore) -> Any:
        barrier.wait(timeout=5)
        return _run_now(
            store,
            owner=owner,
            job_id=str(job["id"]),
            submission_id=submission_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(submit, stores))

    occurrence_ids = {
        uuid.UUID(str(_occurrence_value(result, "occurrence_id"))) for result in results
    }
    assert len(occurrence_ids) == 1
    occurrence_id = next(iter(occurrence_ids))
    assert occurrence_id.version == 4
    assert sorted(_created(result) for result in results) == [False, True]

    replay = _run_now(
        stores[0],
        owner=owner,
        job_id=str(job["id"]),
        submission_id=submission_id,
    )
    assert uuid.UUID(str(_occurrence_value(replay, "occurrence_id"))) == occurrence_id
    assert _created(replay) is False

    occurrence_rows = clean_database.fetch_all(
        """
        SELECT occurrence_id, state
        FROM scheduled_occurrence
        WHERE owner_user_id = ? AND run_now_submission_id = ?
        """,
        (owner, str(submission_id)),
    )
    assert len(occurrence_rows) == 1
    assert uuid.UUID(str(occurrence_rows[0]["occurrence_id"])) == occurrence_id
    assert occurrence_rows[0]["state"] == "pending"
    persisted_job = stores[0].get_job(owner, str(job["id"]))
    assert persisted_job is not None
    assert int(persisted_job["next_run_at"]) == cadence_before


def test_run_now_submission_cannot_be_rebound_to_another_job(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database, coordinator=_coordinator(clean_database))
    owner = "owner-run-now-conflict"
    first_job = _job(store, owner=owner, label="conflict-a")
    second_job = _job(store, owner=owner, label="conflict-b")
    submission_id = uuid.uuid4()

    first = _run_now(
        store,
        owner=owner,
        job_id=str(first_job["id"]),
        submission_id=submission_id,
    )
    with pytest.raises(Exception) as caught:
        _run_now(
            store,
            owner=owner,
            job_id=str(second_job["id"]),
            submission_id=submission_id,
        )

    _assert_code(caught.value, "idempotency_conflict")
    rows = clean_database.fetch_all(
        "SELECT occurrence_id, job_id FROM scheduled_occurrence "
        "WHERE owner_user_id = ? AND run_now_submission_id = ?",
        (owner, str(submission_id)),
    )
    assert len(rows) == 1
    assert uuid.UUID(str(rows[0]["occurrence_id"])) == uuid.UUID(
        str(_occurrence_value(first, "occurrence_id"))
    )
    assert uuid.UUID(str(rows[0]["job_id"])) == uuid.UUID(str(first_job["id"]))


def test_run_now_owner_scope_is_enforced_without_cross_owner_disclosure(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database, coordinator=_coordinator(clean_database))
    submission_id = uuid.uuid4()
    owner_a = "owner-scope-a"
    owner_b = "owner-scope-b"
    job_a = _job(store, owner=owner_a, label="scope-a")

    _run_now(
        store,
        owner=owner_a,
        job_id=str(job_a["id"]),
        submission_id=submission_id,
    )
    with pytest.raises(Exception) as caught:
        _run_now(
            store,
            owner=owner_b,
            job_id=str(job_a["id"]),
            submission_id=submission_id,
        )
    _assert_code(caught.value, "job_not_found")

    job_b = _job(store, owner=owner_b, label="scope-b")
    second_owner = _run_now(
        store,
        owner=owner_b,
        job_id=str(job_b["id"]),
        submission_id=submission_id,
    )
    assert _created(second_owner) is True
    assert (
        int(
            clean_database.fetch_one(
                "SELECT COUNT(*) AS n FROM scheduled_occurrence "
                "WHERE run_now_submission_id = ?",
                (str(submission_id),),
            )["n"]
        )
        == 2
    )


def test_claim_scan_does_not_claim_pending_work_for_paused_job(
    clean_database: PlaneTestRuntime,
) -> None:
    store = ScheduledJobStore(clean_database, coordinator=_coordinator(clean_database))
    owner = "owner-paused-claim-guard"
    job = _job(store, owner=owner, label="paused-claim-guard")
    occurrence_id = uuid.uuid4()
    clean_database.execute(
        """
        INSERT INTO scheduled_occurrence (
            occurrence_id, job_id, owner_user_id, scheduled_for, state,
            first_eligible_at, created_at, updated_at
        ) VALUES (
            ?, ?, ?, clock_timestamp() - INTERVAL '1 second', 'pending',
            clock_timestamp() - INTERVAL '1 second', clock_timestamp(),
            clock_timestamp()
        )
        """,
        (str(occurrence_id), str(job["id"]), owner),
    )
    assert store.set_status(owner, str(job["id"]), "paused")

    claims = store.materialize_and_claim_due(
        "paused-claim-guard",
        limit=5,
        lease_seconds=15,
        eligibility=lambda _job_record: True,
    )

    assert claims == ()
    assert (
        clean_database.fetch_one(
            "SELECT state FROM scheduled_occurrence WHERE occurrence_id = ?",
            (str(occurrence_id),),
        )["state"]
        == "pending"
    )


@pytest.mark.parametrize(
    ("status", "terminal_code"),
    (
        ("paused", "cancelled_job_paused"),
        ("disabled", "cancelled_job_deleted"),
    ),
)
def test_pause_or_delete_cancels_pending_run_now_occurrence(
    clean_database: PlaneTestRuntime,
    status: str,
    terminal_code: str,
) -> None:
    store = ScheduledJobStore(clean_database, coordinator=_coordinator(clean_database))
    owner = f"owner-pending-{status}"
    job = _job(store, owner=owner, label=f"pending-{status}")
    result = _run_now(
        store,
        owner=owner,
        job_id=str(job["id"]),
        submission_id=uuid.uuid4(),
    )

    changed = store.set_status_and_cancel_unstarted(
        user_id=owner,
        job_id=str(job["id"]),
        status=status,
        terminal_code=terminal_code,
    )

    assert changed
    occurrence = clean_database.fetch_one(
        "SELECT state, result_code, terminal_at, lease_token, lease_owner, "
        "lease_expires_at, next_attempt_at FROM scheduled_occurrence "
        "WHERE occurrence_id = ?",
        (str(_occurrence_value(result, "occurrence_id")),),
    )
    assert occurrence["state"] == "cancelled"
    assert occurrence["result_code"] == terminal_code
    assert occurrence["terminal_at"] is not None
    assert occurrence["lease_token"] is None
    assert occurrence["lease_owner"] is None
    assert occurrence["lease_expires_at"] is None
    assert occurrence["next_attempt_at"] is None
    assert store.get_job(owner, str(job["id"]))["status"] == status


@pytest.mark.parametrize(
    ("status", "terminal_code"),
    (
        ("paused", "cancelled_job_paused"),
        ("disabled", "cancelled_job_deleted"),
    ),
)
def test_pause_or_delete_cancels_claimed_but_not_running_attempt(
    clean_database: PlaneTestRuntime,
    status: str,
    terminal_code: str,
) -> None:
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = f"owner-claimed-{status}"
    job = _job(store, owner=owner, label=f"claimed-{status}", due=True)
    claim = store.materialize_and_claim_due(
        f"actions-{status}", limit=1, lease_seconds=15, eligibility=lambda _job: True
    )[0]
    attempt = store.allocate_attempt(claim)
    assert attempt.execution_fence is not None
    assert (
        clean_database.fetch_one(
            "SELECT state FROM scheduled_occurrence WHERE occurrence_id = ?",
            (str(claim.occurrence_id),),
        )["state"]
        == "claimed"
    )

    changed = store.set_status_and_cancel_unstarted(
        user_id=owner,
        job_id=str(job["id"]),
        status=status,
        terminal_code=terminal_code,
    )

    assert changed
    occurrence = clean_database.fetch_one(
        "SELECT state, result_code, terminal_at FROM scheduled_occurrence "
        "WHERE occurrence_id = ?",
        (str(claim.occurrence_id),),
    )
    operation = clean_database.fetch_one(
        "SELECT state, terminal_code, cancel_requested_at "
        "FROM operation_record WHERE operation_id = ?",
        (str(attempt.operation_id),),
    )
    assert occurrence["state"] == "cancelled"
    assert occurrence["result_code"] == terminal_code
    assert occurrence["terminal_at"] is not None
    assert operation["state"] == "cancelled"
    assert operation["terminal_code"] == terminal_code
    assert (
        int(
            clean_database.fetch_one(
                "SELECT COUNT(*) AS n FROM operation_admission_slot "
                "WHERE operation_id = ?",
                (str(attempt.operation_id),),
            )["n"]
        )
        == 0
    )


@pytest.mark.parametrize(
    ("status", "terminal_code"),
    (
        ("paused", "cancelled_job_paused"),
        ("disabled", "cancelled_job_deleted"),
    ),
)
def test_pause_or_delete_leaves_already_running_occurrence_and_operation_untouched(
    clean_database: PlaneTestRuntime,
    status: str,
    terminal_code: str,
) -> None:
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    owner = f"owner-running-{status}"
    job = _job(store, owner=owner, label=f"running-{status}", due=True)
    claim = store.materialize_and_claim_due(
        f"actions-running-{status}",
        limit=1,
        lease_seconds=15,
        eligibility=lambda _job: True,
    )[0]
    attempt = store.start_attempt(store.allocate_attempt(claim))

    changed = store.set_status_and_cancel_unstarted(
        user_id=owner,
        job_id=str(job["id"]),
        status=status,
        terminal_code=terminal_code,
    )

    assert changed
    occurrence = clean_database.fetch_one(
        "SELECT state, result_code, terminal_at FROM scheduled_occurrence "
        "WHERE occurrence_id = ?",
        (str(claim.occurrence_id),),
    )
    operation = clean_database.fetch_one(
        "SELECT state, terminal_code, cancel_requested_at "
        "FROM operation_record WHERE operation_id = ?",
        (str(attempt.operation_id),),
    )
    assert store.get_job(owner, str(job["id"]))["status"] == status
    assert occurrence["state"] == "running"
    assert occurrence["result_code"] is None
    assert occurrence["terminal_at"] is None
    assert operation["state"] == "running"
    assert operation["terminal_code"] is None
    assert operation["cancel_requested_at"] is None
