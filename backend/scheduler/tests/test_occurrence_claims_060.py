"""Real-PostgreSQL scheduler occurrence, lease, and effect contracts (T023).

The tests intentionally exercise the product store and two independent Plane
runtime consumers. PostgreSQL time, row locks, unique indexes,
and the feature-060 operation coordinator are part of the assertions; a fake
repository cannot satisfy this suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Iterator

import pytest

from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    ExecutionFence,
    OperationState,
    RefusedAdmission,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
import scheduler.loop as scheduler_loop_module
import scheduler.store as scheduler_store_module
from scheduler.loop import ClaimLeaseKeeper, SchedulerLoop
from scheduler.runner import (
    HandlerIdempotencyBoundary,
    JobRunner,
    OccurrenceRunResult,
    ScheduledHandlerDeclaration,
)
from scheduler.tests.plane_runtime import (
    ensure_plane_runtime,
    scheduled_job_store as ScheduledJobStore,
    work_admission_repository,
)
from scheduler.store import (
    EffectReservation,
    EffectIdempotencyConflictError,
    OccurrenceClaim,
    ScheduledAdmissionRefusedError,
    ScheduledAttempt,
    StaleOccurrenceClaimError,
)
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _classes(*, scheduled_active: int = 1, scheduled_queue: int = 20):
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=max(8, scheduled_active),
            queue_limit=0,
            max_wait_ms=None,
            config_revision="scheduler-060-test",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.SCHEDULED,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=scheduled_active,
            queue_limit=scheduled_queue,
            max_wait_ms=120_000,
            config_revision="scheduler-060-test",
        ),
    )


@pytest.fixture(scope="module")
def postgres_database() -> Iterator[PlaneTestRuntime]:
    """Create one isolated database initialized only by AstralPlane."""

    with isolated_plane_runtime("scheduler_occurrence") as runtime:
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


def _coordinator(
    db: PlaneTestRuntime, *, scheduled_active: int = 1, scheduled_queue: int = 20
) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(
            scheduled_active=scheduled_active,
            scheduled_queue=scheduled_queue,
        ),
        repository=work_admission_repository(db),
        operation_retention=timedelta(hours=24),
        slot_lease=timedelta(seconds=90),
    )


def _due_job(store: ScheduledJobStore, label: str, *, due_ms: int | None = None):
    if due_ms is None:
        due_ms = int(datetime.now(UTC).timestamp() * 1000) - 1_000
    return store.create_job(
        f"owner-{label}",
        name=f"Job {label}",
        instruction=f"perform deterministic task {label}",
        schedule_kind="interval",
        schedule_expr="1h",
        timezone="UTC",
        consented_scopes=[],
        agent_id=None,
        target_chat_id=f"chat-{label}",
        next_run_at=due_ms,
        offline_grant_id=None,
    )


def _expire_claim(db: PlaneTestRuntime, occurrence_id: uuid.UUID) -> None:
    db.execute(
        "UPDATE scheduled_occurrence SET lease_expires_at = "
        "clock_timestamp() - INTERVAL '1 second' WHERE occurrence_id = ?",
        (str(occurrence_id),),
    )


def _rotate_claim(db: PlaneTestRuntime, occurrence_id: uuid.UUID) -> None:
    db.execute(
        "UPDATE scheduled_occurrence SET claim_generation = claim_generation + 1, "
        "lease_token = ?, lease_owner = 'replacement', "
        "lease_expires_at = clock_timestamp() + INTERVAL '15 seconds', "
        "updated_at = clock_timestamp() WHERE occurrence_id = ?",
        (str(uuid.uuid4()), str(occurrence_id)),
    )


class _ValidGrants:
    def latest_valid_for(self, user_id, agent_id):
        return "grant-scheduler-060"

    def is_valid(self, grant_id, *, user_id):
        return True

    async def mint_access_token(self, grant_id, *, user_id):
        return "test-access-token"


def _recording_orchestrator():
    calls = {"turns": [], "notifications": []}

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

    orchestrator = SimpleNamespace(
        run_scheduled_turn=run_scheduled_turn,
        notify_user=notify_user,
        tool_permissions=SimpleNamespace(get_agent_scopes=lambda *_: {}),
    )
    return orchestrator, calls


def _dummy_claim(label: str = "dummy", *, attempt_number: int = 1) -> OccurrenceClaim:
    occurrence_id = uuid.uuid5(uuid.NAMESPACE_URL, f"scheduler-test:{label}")
    operation_id = uuid.uuid5(uuid.NAMESPACE_OID, f"scheduler-parent:{label}")
    return OccurrenceClaim(
        occurrence_id=occurrence_id,
        job={
            "id": f"job-{label}",
            "user_id": f"owner-{label}",
            "name": f"Job {label}",
            "instruction": f"perform {label}",
            "schedule_kind": "interval",
            "schedule_expr": "1h",
            "timezone": "UTC",
            "consented_scopes": [],
            "agent_id": None,
            "target_chat_id": f"chat-{label}",
            "offline_grant_id": None,
        },
        # Just-due, not a fixed past date: the runner now completes
        # occurrences older than SCHEDULER_STALE_GRACE_SECONDS as
        # ``skipped_stale`` without dispatching.
        scheduled_for=datetime.now(UTC) - timedelta(seconds=1),
        claim_generation=1,
        lease_token=uuid.uuid5(uuid.NAMESPACE_X500, f"scheduler-lease:{label}"),
        lease_owner="scheduler-test",
        lease_expires_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        attempt_number=attempt_number,
        parent_operation_id=operation_id if attempt_number > 1 else None,
    )


def _dummy_attempt(
    label: str = "dummy",
    *,
    selected: bool = True,
    started: bool = True,
) -> ScheduledAttempt:
    operation_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"scheduler-operation:{label}")
    fence = (
        ExecutionFence(
            operation_id=operation_id,
            execution_generation=1,
            execution_lease_token=uuid.uuid5(
                uuid.NAMESPACE_DNS, f"scheduler-execution:{label}"
            ),
        )
        if selected
        else None
    )
    return ScheduledAttempt(
        claim=_dummy_claim(label),
        operation_id=operation_id,
        operation_state=(OperationState.RUNNING if selected else OperationState.QUEUED),
        execution_fence=fence,
        parent_operation_id=None,
        run_id=(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"scheduler-run:{label}")
            if selected and started
            else None
        ),
    )


def test_repeated_polls_materialize_once_and_advance_job_atomically(clean_database):
    store = ScheduledJobStore(clean_database)
    job = _due_job(store, "repeat")
    original_next_run = int(job["next_run_at"])

    first = store.materialize_and_claim_due("scheduler-a", limit=10, lease_seconds=15)
    second = ScheduledJobStore(clean_database).materialize_and_claim_due(
        "scheduler-b", limit=10, lease_seconds=15
    )

    assert len(first) == 1
    assert second == ()
    assert first[0].job["id"] == job["id"]
    assert first[0].attempt_number == 1
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM scheduled_occurrence WHERE job_id = ?",
            (job["id"],),
        )["n"]
        == 1
    )
    advanced = clean_database.fetch_one(
        "SELECT next_run_at FROM scheduled_job WHERE id = ?", (job["id"],)
    )
    assert int(advanced["next_run_at"]) > original_next_run


def test_materialization_and_next_run_advance_roll_back_together(clean_database):
    store = ScheduledJobStore(clean_database)
    job = _due_job(store, "rollback")
    clean_database.execute(
        """
        CREATE OR REPLACE FUNCTION scheduler_060_fail_advance()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.next_run_at IS DISTINCT FROM NEW.next_run_at THEN
                RAISE EXCEPTION 'scheduler-060 injected crash';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    clean_database.execute(
        "CREATE TRIGGER scheduler_060_fail_advance_trigger "
        "BEFORE UPDATE ON scheduled_job FOR EACH ROW "
        "EXECUTE FUNCTION scheduler_060_fail_advance()"
    )
    try:
        with pytest.raises(Exception, match="scheduler-060 injected crash"):
            store.materialize_and_claim_due("scheduler-a", limit=10, lease_seconds=15)
    finally:
        clean_database.execute(
            "DROP TRIGGER IF EXISTS scheduler_060_fail_advance_trigger ON scheduled_job"
        )
        clean_database.execute("DROP FUNCTION IF EXISTS scheduler_060_fail_advance()")

    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM scheduled_occurrence WHERE job_id = ?",
            (job["id"],),
        )["n"]
        == 0
    )
    row = clean_database.fetch_one(
        "SELECT next_run_at FROM scheduled_job WHERE id = ?", (job["id"],)
    )
    assert int(row["next_run_at"]) == int(job["next_run_at"])


def test_two_instances_claim_one_authoritative_occurrence(clean_database):
    _due_job(ScheduledJobStore(clean_database), "two-instance")

    def poll(instance: str):
        return ScheduledJobStore(clean_database).materialize_and_claim_due(
            instance, limit=10, lease_seconds=15
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(poll, ("scheduler-a", "scheduler-b")))

    claims = [claim for batch in results for claim in batch]
    assert len(claims) == 1
    assert len({claim.occurrence_id for claim in claims}) == 1
    row = clean_database.fetch_one(
        "SELECT state, claim_generation, lease_owner FROM scheduled_occurrence"
    )
    assert row == {
        "state": "claimed",
        "claim_generation": 1,
        "lease_owner": claims[0].lease_owner,
    }


def test_lease_expiry_recovers_same_occurrence_with_new_generation(clean_database):
    store = ScheduledJobStore(clean_database)
    _due_job(store, "recovery")
    original = store.materialize_and_claim_due(
        "scheduler-a", limit=1, lease_seconds=15
    )[0]
    _expire_claim(clean_database, original.occurrence_id)

    recovered = ScheduledJobStore(clean_database).materialize_and_claim_due(
        "scheduler-b", limit=1, lease_seconds=15
    )[0]

    assert recovered.occurrence_id == original.occurrence_id
    assert recovered.attempt_number == original.attempt_number + 1
    assert recovered.claim_generation == original.claim_generation + 1
    assert recovered.lease_token != original.lease_token
    assert recovered.lease_owner == "scheduler-b"


def test_recovery_preserves_occurrence_but_allocates_distinct_attempt_operations(
    clean_database,
):
    coordinator = _coordinator(clean_database, scheduled_active=2)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "attempts")
    claim_one = store.materialize_and_claim_due(
        "scheduler-a", limit=1, lease_seconds=15
    )[0]
    attempt_one = store.allocate_attempt(claim_one)
    assert attempt_one.operation_id is not None
    assert attempt_one.execution_fence is not None

    _expire_claim(clean_database, claim_one.occurrence_id)
    claim_two = ScheduledJobStore(
        clean_database, coordinator=coordinator
    ).materialize_and_claim_due("scheduler-b", limit=1, lease_seconds=15)[0]
    attempt_two = store.allocate_attempt(claim_two)

    assert claim_two.occurrence_id == claim_one.occurrence_id
    assert attempt_two.operation_id != attempt_one.operation_id
    assert attempt_two.parent_operation_id == attempt_one.operation_id
    old_operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(attempt_one.operation_id),),
    )
    assert old_operation == {"state": "retryable", "terminal_code": "claim_lost"}
    rows = clean_database.fetch_all(
        "SELECT operation_id, idempotency_key FROM operation_record "
        "WHERE operation_kind = 'scheduled_occurrence' ORDER BY accepted_at"
    )
    assert [row["idempotency_key"] for row in rows] == [
        f"{claim_one.occurrence_id}:1",
        f"{claim_one.occurrence_id}:2",
    ]


@pytest.mark.asyncio
async def test_claim_renews_while_operation_is_queued_for_two_full_leases(
    clean_database,
):
    """A real 31-second queue residence proves the default 15-second lease."""

    coordinator = _coordinator(clean_database, scheduled_active=1)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "saturated-a")
    _due_job(store, "saturated-b")
    claims = store.materialize_and_claim_due("scheduler-a", limit=2, lease_seconds=15)
    assert len(claims) == 2
    first = store.allocate_attempt(claims[0])
    queued = store.allocate_attempt(claims[1])
    assert first.execution_fence is not None
    assert queued.execution_fence is None
    assert queued.operation_state is OperationState.QUEUED

    keeper = ClaimLeaseKeeper(store, claims[1], lease_seconds=15)
    keeper.start()
    try:
        await asyncio.sleep(31.0)
        assert keeper.lost.is_set() is False
        assert keeper.successful_renewals >= 6
        competing = ScheduledJobStore(clean_database).materialize_and_claim_due(
            "scheduler-b", limit=10, lease_seconds=15
        )
        assert all(
            claim.occurrence_id != claims[1].occurrence_id for claim in competing
        )

        coordinator.terminalize(
            first.execution_fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="released scheduled test slot",
            retry_after_ms=None,
        )
        selected = store.claim_attempt_execution(queued)
        assert selected is not None
        assert selected.execution_fence is not None
    finally:
        await keeper.stop()


@pytest.mark.asyncio
async def test_lost_renewal_refuses_queued_start_and_any_effect(clean_database):
    coordinator = _coordinator(clean_database, scheduled_active=1)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "lost-a")
    _due_job(store, "lost-b")
    claims = store.materialize_and_claim_due("scheduler-a", limit=2, lease_seconds=5)
    first = store.allocate_attempt(claims[0])
    queued = store.allocate_attempt(claims[1])
    assert first.execution_fence is not None
    assert queued.execution_fence is None

    keeper = ClaimLeaseKeeper(store, claims[1], lease_seconds=5)
    keeper.start()
    _rotate_claim(clean_database, claims[1].occurrence_id)
    await asyncio.wait_for(keeper.lost.wait(), timeout=2.5)
    await keeper.stop()

    coordinator.terminalize(
        first.execution_fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="released scheduled test slot",
        retry_after_ms=None,
    )
    assert store.claim_attempt_execution(queued) is None
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM job_run WHERE occurrence_id = ?",
            (str(claims[1].occurrence_id),),
        )["n"]
        == 0
    )
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM effect_ledger WHERE occurrence_id = ?",
            (str(claims[1].occurrence_id),),
        )["n"]
        == 0
    )


def test_stale_claim_cannot_start_or_create_job_run(clean_database):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "stale-start")
    claim = store.materialize_and_claim_due("scheduler-a", limit=1, lease_seconds=15)[0]
    attempt = store.allocate_attempt(claim)
    assert attempt.execution_fence is not None
    _rotate_claim(clean_database, claim.occurrence_id)

    with pytest.raises(StaleOccurrenceClaimError):
        store.start_attempt(attempt, lease_seconds=15)
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM job_run WHERE occurrence_id = ?",
            (str(claim.occurrence_id),),
        )["n"]
        == 0
    )


def test_effect_ledger_same_digest_replays_and_different_digest_conflicts(
    clean_database,
):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "effects")
    claim = store.materialize_and_claim_due("scheduler-a", limit=1, lease_seconds=15)[0]
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)
    digest = _sha256("normalized-visible-effect")

    first = store.reserve_effect(
        attempt,
        effect_kind="chat_history",
        effect_key="chat-effects",
        payload_digest=digest,
    )
    replay = store.reserve_effect(
        attempt,
        effect_kind="chat_history",
        effect_key="chat-effects",
        payload_digest=digest,
    )
    assert first.created is True and first.state == "reserved"
    assert replay.created is False and replay.state == "reserved"

    published = store.publish_effect(
        attempt,
        effect_kind="chat_history",
        effect_key="chat-effects",
        payload_digest=digest,
    )
    after_publish = store.reserve_effect(
        attempt,
        effect_kind="chat_history",
        effect_key="chat-effects",
        payload_digest=digest,
    )
    assert published.state == "published"
    assert after_publish.created is False and after_publish.state == "published"

    with pytest.raises(EffectIdempotencyConflictError):
        store.reserve_effect(
            attempt,
            effect_kind="chat_history",
            effect_key="chat-effects",
            payload_digest=_sha256("different-visible-effect"),
        )


def test_stale_claim_cannot_reserve_publish_or_complete_effect(clean_database):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "stale-effect")
    claim = store.materialize_and_claim_due("scheduler-a", limit=1, lease_seconds=15)[0]
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)
    stale = replace(
        attempt,
        claim=replace(
            attempt.claim,
            claim_generation=attempt.claim.claim_generation + 1,
            lease_token=uuid.uuid4(),
        ),
    )
    digest = _sha256("stale")

    for mutation in (
        lambda: store.reserve_effect(
            stale,
            effect_kind="chat_history",
            effect_key="chat-stale",
            payload_digest=digest,
        ),
        lambda: store.publish_effect(
            stale,
            effect_kind="chat_history",
            effect_key="chat-stale",
            payload_digest=digest,
        ),
        lambda: store.finish_attempt(
            stale,
            outcome="success",
            summary="must not commit",
        ),
    ):
        with pytest.raises(StaleOccurrenceClaimError):
            mutation()

    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM effect_ledger WHERE occurrence_id = ?",
            (str(claim.occurrence_id),),
        )["n"]
        == 0
    )
    assert (
        clean_database.fetch_one(
            "SELECT state FROM scheduled_occurrence WHERE occurrence_id = ?",
            (str(claim.occurrence_id),),
        )["state"]
        == "running"
    )


def test_reserved_crash_replay_is_ambiguous_and_never_reemits(clean_database):
    coordinator = _coordinator(clean_database, scheduled_active=2)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "ambiguous")
    first_claim = store.materialize_and_claim_due(
        "scheduler-a", limit=1, lease_seconds=15
    )[0]
    first_attempt = store.start_attempt(
        store.allocate_attempt(first_claim), lease_seconds=15
    )
    digest = _sha256("ambiguous-payload")
    reservation = store.reserve_effect(
        first_attempt,
        effect_kind="chat_history",
        effect_key="chat-ambiguous",
        payload_digest=digest,
    )
    assert reservation.created is True

    _expire_claim(clean_database, first_claim.occurrence_id)
    recovered_claim = store.materialize_and_claim_due(
        "scheduler-b", limit=1, lease_seconds=15
    )[0]
    recovered_attempt = store.start_attempt(
        store.allocate_attempt(recovered_claim), lease_seconds=15
    )
    reconciled = store.reserve_effect(
        recovered_attempt,
        effect_kind="chat_history",
        effect_key="chat-ambiguous",
        payload_digest=digest,
    )
    assert reconciled.created is False
    assert reconciled.state == "reserved"
    assert reconciled.ambiguous is True


@pytest.mark.asyncio
async def test_runner_replays_published_effect_without_reinvoking_handler(
    clean_database,
):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "runner-replay")
    claim = store.materialize_and_claim_due("scheduler-a", limit=1, lease_seconds=15)[0]
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)
    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    runner.bind_execution_context(coordinator=coordinator, store=store)
    claim_lost = asyncio.Event()

    first = await runner.run_occurrence(attempt, claim_lost=claim_lost)
    replay = await runner.run_occurrence(attempt, claim_lost=claim_lost)

    assert first.outcome == replay.outcome == "success"
    assert len(calls["turns"]) == 1
    assert len(calls["notifications"]) == 1
    effects = clean_database.fetch_all(
        "SELECT effect_kind, state FROM effect_ledger "
        "WHERE occurrence_id = ? ORDER BY effect_kind",
        (str(claim.occurrence_id),),
    )
    assert [dict(effect) for effect in effects] == [
        {"effect_kind": "chat_history", "state": "published"},
        {"effect_kind": "notification", "state": "published"},
    ]


@pytest.mark.asyncio
async def test_scheduler_loop_completes_one_fenced_attempt_end_to_end(clean_database):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    job = _due_job(store, "loop-e2e")
    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, _ValidGrants())
    loop = SchedulerLoop(
        store,
        runner,
        coordinator=coordinator,
        instance_id="scheduler-e2e",
        claim_lease_seconds=5,
    )

    await loop._tick()
    pending = tuple(loop._dispatch_tasks)
    assert len(pending) == 1
    await asyncio.gather(*pending)

    occurrence = clean_database.fetch_one(
        "SELECT occurrence_id, state, attempt_count, current_operation_id "
        "FROM scheduled_occurrence WHERE job_id = ?",
        (job["id"],),
    )
    assert occurrence["state"] == "completed"
    assert occurrence["attempt_count"] == 1
    operation = clean_database.fetch_one(
        "SELECT state, execution_generation FROM operation_record "
        "WHERE operation_id = ?",
        (str(occurrence["current_operation_id"]),),
    )
    assert operation == {"state": "completed", "execution_generation": 1}
    run = clean_database.fetch_one(
        "SELECT outcome, attempt_number, operation_id, "
        "operation_execution_generation, occurrence_claim_generation "
        "FROM job_run WHERE occurrence_id = ?",
        (str(occurrence["occurrence_id"]),),
    )
    assert run == {
        "outcome": "success",
        "attempt_number": 1,
        "operation_id": occurrence["current_operation_id"],
        "operation_execution_generation": 1,
        "occurrence_claim_generation": 1,
    }
    assert len(calls["turns"]) == 1
    assert len(calls["notifications"]) == 1


def test_deterministic_10000_interleavings_publish_one_visible_effect(
    clean_database,
):
    coordinator = _coordinator(clean_database)
    stores = (
        ScheduledJobStore(clean_database, coordinator=coordinator),
        ScheduledJobStore(clean_database, coordinator=coordinator),
    )
    _due_job(stores[0], "ten-thousand")
    claim = stores[0].materialize_and_claim_due(
        "scheduler-a", limit=1, lease_seconds=60
    )[0]
    attempt = stores[0].start_attempt(
        stores[0].allocate_attempt(claim), lease_seconds=60
    )
    digest = _sha256("one-canonical-effect")
    visible_effects: list[str] = []

    for index in range(10_000):
        # The loop performs ~11k serial DB round trips; on a loaded CI runner
        # that exceeds the max 60 s lease, and the store correctly refuses the
        # now-stale claim. This test exercises effect deduplication, not lease
        # expiry, so renew the running claim well within the lease window (the
        # same renewal a real long-running attempt performs per lease/3).
        if index and index % 250 == 0:
            stores[0].renew_claim(claim, lease_seconds=60)
        store = stores[index % len(stores)]
        reservation = store.reserve_effect(
            attempt,
            effect_kind="chat_history",
            effect_key="chat-ten-thousand",
            payload_digest=digest,
        )
        if reservation.created:
            visible_effects.append(f"effect-{index}")
            store.publish_effect(
                attempt,
                effect_kind="chat_history",
                effect_key="chat-ten-thousand",
                payload_digest=digest,
            )
        elif index % 7 == 0:
            replay = store.reserve_effect(
                attempt,
                effect_kind="chat_history",
                effect_key="chat-ten-thousand",
                payload_digest=digest,
            )
            assert replay.created is False

    assert visible_effects == ["effect-0"]
    assert clean_database.fetch_one(
        "SELECT state, COUNT(*) AS n FROM effect_ledger "
        "WHERE occurrence_id = ? GROUP BY state",
        (str(claim.occurrence_id),),
    ) == {"state": "published", "n": 1}


def test_scheduler_configuration_and_binding_fail_closed(monkeypatch):
    class BoundStore:
        def __init__(self):
            self.bound = None

        def bind_coordinator(self, coordinator):
            self.bound = coordinator

        def reconcile_interrupted(self):
            return 0

    class BoundRunner:
        def __init__(self):
            self.bound = None

        def bind_execution_context(self, *, coordinator, store):
            self.bound = (coordinator, store)

    coordinator = object()
    store = BoundStore()
    runner = BoundRunner()
    task_manager = SimpleNamespace(_require_coordinator=lambda: coordinator)
    loop = SchedulerLoop(
        store,
        runner,
        task_manager,
        claim_lease_seconds=5,
    )
    assert loop.coordinator is coordinator
    assert store.bound is coordinator
    assert runner.bound == (coordinator, store)

    def unavailable_coordinator():
        raise RuntimeError("foundation not initialized")

    legacy = SchedulerLoop(
        BoundStore(),
        BoundRunner(),
        SimpleNamespace(_require_coordinator=unavailable_coordinator),
        claim_lease_seconds=5,
    )
    assert legacy.coordinator is None

    monkeypatch.setenv("SCHEDULED_CLAIM_LEASE_SECONDS", "5")
    assert SchedulerLoop(BoundStore(), BoundRunner()).claim_lease_seconds == 5
    with pytest.raises(ValueError, match="between 5 and 60"):
        ClaimLeaseKeeper(BoundStore(), _dummy_claim(), lease_seconds=4)
    with pytest.raises(ValueError, match="SCHEDULED_CLAIM_LEASE_SECONDS"):
        SchedulerLoop(BoundStore(), BoundRunner(), claim_lease_seconds=61)
    with pytest.raises(ValueError, match="claim_limit"):
        SchedulerLoop(BoundStore(), BoundRunner(), claim_lease_seconds=5, claim_limit=0)

    database_store = object.__new__(scheduler_store_module.ScheduledJobStore)
    database_store._coordinator = None
    with pytest.raises(RuntimeError, match="requires a WorkAdmissionCoordinator"):
        database_store._require_coordinator()
    database_store.bind_coordinator(coordinator)
    with pytest.raises(RuntimeError, match="cannot replace"):
        database_store.bind_coordinator(object())

    refusal = RefusedAdmission(False, "capacity_exhausted", True, 1_000)
    error = ScheduledAdmissionRefusedError(refusal)
    assert (error.code, error.retryable, error.retry_after_ms) == (
        "capacity_exhausted",
        True,
        1_000,
    )
    assert scheduler_store_module._utc(datetime(2026, 1, 1)).tzinfo is UTC

    with pytest.raises(ValueError, match="effect_kinds"):
        ScheduledHandlerDeclaration(
            supports_unattended=True,
            idempotency_boundary=HandlerIdempotencyBoundary.ASTRALDEEP_TRANSACTION,
            effect_kinds=(),
        )

    runner_one = JobRunner(SimpleNamespace(), object(), object())
    runner_two_store = object()
    runner_one.bind_execution_context(coordinator=coordinator, store=runner_one.store)
    with pytest.raises(RuntimeError, match="cannot replace"):
        runner_one.bind_execution_context(coordinator=object(), store=runner_one.store)
    with pytest.raises(RuntimeError, match="binding mismatch"):
        runner_one.bind_execution_context(
            coordinator=coordinator,
            store=runner_two_store,
        )


def test_store_rejects_invalid_claim_and_effect_inputs(clean_database):
    store = ScheduledJobStore(clean_database)
    for instance_id, limit, lease_seconds, message in (
        ("contains a space", 1, 15, "instance_id"),
        ("scheduler-a", 0, 15, "claim limit"),
        ("scheduler-a", 1, 4, "claim lease"),
    ):
        with pytest.raises(ValueError, match=message):
            store.materialize_and_claim_due(
                instance_id,
                limit=limit,
                lease_seconds=lease_seconds,
            )

    for kwargs, message in (
        (
            {"effect_kind": "Bad", "effect_key": "key", "payload_digest": "0" * 64},
            "effect_kind",
        ),
        (
            {
                "effect_kind": "chat_history",
                "effect_key": "",
                "payload_digest": "0" * 64,
            },
            "effect_key",
        ),
        (
            {
                "effect_kind": "chat_history",
                "effect_key": "key",
                "payload_digest": "bad",
            },
            "payload_digest",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            store._validate_effect_identity(**kwargs)

    with pytest.raises(ValueError, match="error_code"):
        store.mark_claim_retryable(_dummy_claim(), error_code="Bad")
    with pytest.raises(ValueError, match="retry_after_seconds"):
        store.mark_claim_retryable(
            _dummy_claim(), error_code="operation_failed", retry_after_seconds=-1
        )

    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "validation")
    claim = store.materialize_and_claim_due(
        "scheduler-validation", limit=1, lease_seconds=15
    )[0]
    selected = store.allocate_attempt(claim)
    unselected = replace(selected, execution_fence=None)
    with pytest.raises(StaleOccurrenceClaimError, match="selected execution"):
        store.start_attempt(unselected)
    with pytest.raises(StaleOccurrenceClaimError, match="execution fence"):
        store.reserve_effect(
            unselected,
            effect_kind="chat_history",
            effect_key="validation",
            payload_digest=_sha256("validation"),
        )
    with pytest.raises(StaleOccurrenceClaimError, match="not started"):
        store.reserve_effect(
            selected,
            effect_kind="chat_history",
            effect_key="validation",
            payload_digest=_sha256("validation"),
        )
    with pytest.raises(StaleOccurrenceClaimError, match="not started"):
        store.finish_attempt(selected, outcome="success")

    started = store.start_attempt(selected)
    with pytest.raises(ValueError, match="unsupported"):
        store.finish_attempt(started, outcome="unknown")
    with pytest.raises(ValueError, match="result_code"):
        store.finish_attempt(started, outcome="failure", result_code="Bad")
    with pytest.raises(ValueError, match="retry_after_seconds"):
        store.finish_attempt(started, outcome="failure", retry_after_seconds=86_401)
    with pytest.raises(ValueError, match="downstream_receipt_digest"):
        store.publish_effect(
            started,
            effect_kind="chat_history",
            effect_key="missing",
            payload_digest=_sha256("missing"),
            downstream_receipt_digest="bad",
        )
    with pytest.raises(ValueError, match="failure_code"):
        store.fail_effect(
            started,
            effect_kind="chat_history",
            effect_key="missing",
            payload_digest=_sha256("missing"),
            failure_code="Bad",
        )
    with pytest.raises(StaleOccurrenceClaimError, match="not reserved"):
        store.publish_effect(
            started,
            effect_kind="chat_history",
            effect_key="missing",
            payload_digest=_sha256("missing"),
        )


def test_ineligible_jobs_are_never_materialized_or_reclaimed(clean_database):
    store = ScheduledJobStore(clean_database)
    job = _due_job(store, "ineligible-new")
    original_next_run = int(job["next_run_at"])
    decision = SimpleNamespace(eligible=False)
    assert (
        store.materialize_and_claim_due(
            "scheduler-a",
            limit=10,
            lease_seconds=15,
            eligibility=lambda _job: decision,
        )
        == ()
    )
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM scheduled_occurrence WHERE job_id = ?",
            (job["id"],),
        )["n"]
        == 0
    )
    assert (
        int(
            clean_database.fetch_one(
                "SELECT next_run_at FROM scheduled_job WHERE id = ?", (job["id"],)
            )["next_run_at"]
        )
        == original_next_run
    )
    assert store.set_status(job["user_id"], job["id"], "paused") is True

    recovery_job = _due_job(store, "ineligible-recovery")
    claim = store.materialize_and_claim_due("scheduler-a", limit=1, lease_seconds=15)[0]
    assert claim.job["id"] == recovery_job["id"]
    _expire_claim(clean_database, claim.occurrence_id)
    assert (
        store.materialize_and_claim_due(
            "scheduler-b",
            limit=1,
            lease_seconds=15,
            eligibility=lambda _job: False,
        )
        == ()
    )


def test_admission_refusal_releases_occurrence_for_retry(clean_database):
    coordinator = _coordinator(
        clean_database,
        scheduled_active=1,
        scheduled_queue=0,
    )
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "refusal-a")
    _due_job(store, "refusal-b")
    claims = store.materialize_and_claim_due(
        "scheduler-refusal", limit=2, lease_seconds=15
    )
    running = store.allocate_attempt(claims[0])
    assert running.execution_fence is not None
    with pytest.raises(ScheduledAdmissionRefusedError) as refused:
        store.allocate_attempt(claims[1])
    assert refused.value.code == "capacity_exceeded"
    occurrence = clean_database.fetch_one(
        "SELECT state, last_error_code, lease_token FROM scheduled_occurrence "
        "WHERE occurrence_id = ?",
        (str(claims[1].occurrence_id),),
    )
    assert occurrence == {
        "state": "retryable",
        "last_error_code": "capacity_exceeded",
        "lease_token": None,
    }


def test_attach_race_settles_preselected_operation(clean_database, monkeypatch):
    coordinator = _coordinator(clean_database, scheduled_active=2)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "attach-blocker")
    _due_job(store, "attach-race")
    claims = store.materialize_and_claim_due(
        "scheduler-attach", limit=2, lease_seconds=15
    )
    blocker = store.allocate_attempt(claims[0])
    assert blocker.execution_fence is not None
    original_submit = coordinator.submit
    admitted_operation_id: uuid.UUID | None = None

    def submit_with_conflicting_attachment(request):
        nonlocal admitted_operation_id
        result = original_submit(request)
        admitted_operation_id = result.operation_id
        clean_database.execute(
            "UPDATE scheduled_occurrence SET current_operation_id = ? "
            "WHERE occurrence_id = ?",
            (str(blocker.operation_id), str(claims[1].occurrence_id)),
        )
        return result

    monkeypatch.setattr(coordinator, "submit", submit_with_conflicting_attachment)
    with pytest.raises(StaleOccurrenceClaimError):
        store.allocate_attempt(claims[1])
    assert admitted_operation_id is not None
    operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(admitted_operation_id),),
    )
    assert operation == {"state": "retryable", "terminal_code": "claim_lost"}


def test_recovered_terminalization_tolerates_a_stale_operation_fence(
    clean_database,
):
    class StaleCoordinator:
        operation_retention = timedelta(hours=1)

        def terminalize(self, *args, **kwargs):
            raise StaleExecutionFenceError("already replaced")

        def terminalize_unselected(self, *args, **kwargs):
            return None

    store = ScheduledJobStore(clean_database, coordinator=StaleCoordinator())
    store._terminalize_recovered_attempt(
        operation_id=uuid.uuid4(),
        execution_generation=1,
        execution_lease_token=uuid.uuid4(),
        state="running",
    )
    store._terminalize_recovered_attempt(
        operation_id=uuid.uuid4(),
        execution_generation=0,
        execution_lease_token=uuid.UUID(int=0),
        state="completed",
    )


def test_claim_selection_race_terminalizes_the_selected_operation(
    clean_database,
    monkeypatch,
):
    coordinator = _coordinator(clean_database, scheduled_active=1)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "selection-blocker")
    _due_job(store, "selection-queued")
    claims = store.materialize_and_claim_due(
        "scheduler-selection", limit=2, lease_seconds=15
    )
    blocker = store.allocate_attempt(claims[0])
    queued = store.allocate_attempt(claims[1])
    assert blocker.execution_fence is not None
    assert queued.execution_fence is None
    coordinator.terminalize(
        blocker.execution_fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="release",
        retry_after_ms=None,
    )
    original_claim = coordinator.claim_operation

    def claim_then_rotate(class_name, operation_id):
        selected = original_claim(class_name, operation_id)
        _rotate_claim(clean_database, claims[1].occurrence_id)
        return selected

    monkeypatch.setattr(coordinator, "claim_operation", claim_then_rotate)
    assert store.claim_attempt_execution(queued) is None
    operation = clean_database.fetch_one(
        "SELECT state, terminal_code FROM operation_record WHERE operation_id = ?",
        (str(queued.operation_id),),
    )
    assert operation == {"state": "retryable", "terminal_code": "claim_lost"}


def test_start_is_idempotent_for_one_attempt_and_rejects_run_conflicts(
    clean_database,
):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "start-replay")
    claim = store.materialize_and_claim_due(
        "scheduler-start", limit=1, lease_seconds=15
    )[0]
    selected = store.allocate_attempt(claim)
    started = store.start_attempt(selected)
    clean_database.execute(
        "UPDATE scheduled_occurrence SET state = 'claimed' WHERE occurrence_id = ?",
        (str(claim.occurrence_id),),
    )
    replay = store.start_attempt(selected)
    assert replay.run_id == started.run_id

    clean_database.execute(
        "UPDATE job_run SET operation_execution_generation = "
        "operation_execution_generation + 1 WHERE id = ?",
        (str(started.run_id),),
    )
    clean_database.execute(
        "UPDATE scheduled_occurrence SET state = 'claimed' WHERE occurrence_id = ?",
        (str(claim.occurrence_id),),
    )
    with pytest.raises(StaleOccurrenceClaimError, match="job_run fence conflict"):
        store.start_attempt(selected)


def test_start_refuses_when_claimed_to_running_cas_loses(clean_database):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "start-cas")
    claim = store.materialize_and_claim_due(
        "scheduler-start", limit=1, lease_seconds=15
    )[0]
    selected = store.allocate_attempt(claim)
    clean_database.execute(
        """
        CREATE OR REPLACE FUNCTION scheduler_060_drop_running_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.state = 'running' THEN
                RETURN NULL;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    clean_database.execute(
        "CREATE TRIGGER scheduler_060_drop_running_update_trigger "
        "BEFORE UPDATE ON scheduled_occurrence FOR EACH ROW "
        "EXECUTE FUNCTION scheduler_060_drop_running_update()"
    )
    try:
        with pytest.raises(StaleOccurrenceClaimError):
            store.start_attempt(selected)
    finally:
        clean_database.execute(
            "DROP TRIGGER IF EXISTS scheduler_060_drop_running_update_trigger "
            "ON scheduled_occurrence"
        )
        clean_database.execute(
            "DROP FUNCTION IF EXISTS scheduler_060_drop_running_update()"
        )
    assert (
        clean_database.fetch_one(
            "SELECT COUNT(*) AS n FROM job_run WHERE occurrence_id = ?",
            (str(claim.occurrence_id),),
        )["n"]
        == 0
    )


def test_retryable_running_claim_settles_its_exact_job_run(clean_database):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "retryable-running-run")
    claim = store.materialize_and_claim_due(
        "scheduler-retryable", limit=1, lease_seconds=15
    )[0]
    attempt = store.start_attempt(store.allocate_attempt(claim), lease_seconds=15)

    store.mark_claim_retryable(
        claim,
        error_code="operation_lease_lost",
        retry_after_seconds=1,
    )

    assert clean_database.fetch_one(
        "SELECT outcome, ended_at IS NOT NULL AS ended FROM job_run WHERE id = ?",
        (str(attempt.run_id),),
    ) == {"outcome": "interrupted", "ended": True}
    assert clean_database.fetch_one(
        "SELECT state, last_error_code FROM scheduled_occurrence "
        "WHERE occurrence_id = ?",
        (str(claim.occurrence_id),),
    ) == {
        "state": "retryable",
        "last_error_code": "operation_lease_lost",
    }


def test_effect_failure_retry_receipt_and_attempt_ownership(clean_database):
    coordinator = _coordinator(clean_database, scheduled_active=2)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "effect-retry")
    claim = store.materialize_and_claim_due(
        "scheduler-effect", limit=1, lease_seconds=15
    )[0]
    first = store.start_attempt(store.allocate_attempt(claim))
    digest = _sha256("effect-retry")
    store.reserve_effect(
        first,
        effect_kind="chat_history",
        effect_key="effect-retry",
        payload_digest=digest,
    )
    failed = store.fail_effect(
        first,
        effect_kind="chat_history",
        effect_key="effect-retry",
        payload_digest=digest,
        failure_code="pre_publish_failed",
    )
    assert failed.state == "failed"
    retried = store.reserve_effect(
        first,
        effect_kind="chat_history",
        effect_key="effect-retry",
        payload_digest=digest,
    )
    assert retried == EffectReservation("reserved", True, False)
    published = store.publish_effect(
        first,
        effect_kind="chat_history",
        effect_key="effect-retry",
        payload_digest=digest,
        downstream_receipt_digest=_sha256("receipt"),
    )
    assert published.state == "published"
    with pytest.raises(StaleOccurrenceClaimError):
        store.fail_effect(
            first,
            effect_kind="chat_history",
            effect_key="effect-retry",
            payload_digest=digest,
            failure_code="already_published",
        )

    second_digest = _sha256("owned-by-first-attempt")
    store.reserve_effect(
        first,
        effect_kind="chat_history",
        effect_key="attempt-owned",
        payload_digest=second_digest,
    )
    _expire_claim(clean_database, claim.occurrence_id)
    recovered_claim = store.materialize_and_claim_due(
        "scheduler-effect-recovery", limit=1, lease_seconds=15
    )[0]
    second = store.start_attempt(store.allocate_attempt(recovered_claim))
    with pytest.raises(StaleOccurrenceClaimError, match="another attempt"):
        store.publish_effect(
            second,
            effect_kind="chat_history",
            effect_key="attempt-owned",
            payload_digest=second_digest,
        )


def test_effect_authority_and_digest_conflicts_fail_closed(clean_database):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "effect-authority")
    claim = store.materialize_and_claim_due(
        "scheduler-effect", limit=1, lease_seconds=15
    )[0]
    started = store.start_attempt(store.allocate_attempt(claim))
    digest = _sha256("effect-authority")
    store.reserve_effect(
        started,
        effect_kind="chat_history",
        effect_key="digest-conflict",
        payload_digest=digest,
    )
    with pytest.raises(EffectIdempotencyConflictError):
        store.publish_effect(
            started,
            effect_kind="chat_history",
            effect_key="digest-conflict",
            payload_digest=_sha256("different"),
        )
    clean_database.execute(
        "UPDATE scheduled_occurrence SET operation_execution_generation = "
        "operation_execution_generation + 1 WHERE occurrence_id = ?",
        (str(claim.occurrence_id),),
    )
    with pytest.raises(StaleOccurrenceClaimError):
        store.reserve_effect(
            started,
            effect_kind="notification",
            effect_key="mismatched-operation",
            payload_digest=digest,
        )


def test_finish_attempt_outcomes_truncation_and_replay_fence(clean_database):
    coordinator = _coordinator(clean_database, scheduled_active=3)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)

    _due_job(store, "finish-retryable")
    retry_claim = store.materialize_and_claim_due(
        "scheduler-finish", limit=1, lease_seconds=15
    )[0]
    retry_attempt = store.start_attempt(store.allocate_attempt(retry_claim))
    retry_row = store.finish_attempt(
        retry_attempt,
        outcome="success",
        retryable=True,
        retry_after_seconds=0,
    )
    assert retry_row["state"] == "retryable"
    assert (
        clean_database.fetch_one(
            "SELECT outcome FROM job_run WHERE id = ?", (str(retry_attempt.run_id),)
        )["outcome"]
        == "failure"
    )

    _due_job(store, "finish-failure")
    failure_claim = store.materialize_and_claim_due(
        "scheduler-finish", limit=1, lease_seconds=15
    )[0]
    failure_attempt = store.start_attempt(store.allocate_attempt(failure_claim))
    failure_row = store.finish_attempt(failure_attempt, outcome="skipped_auth")
    assert failure_row["state"] == "failed"
    assert failure_row["last_error_code"] == "authorization_unavailable"

    _due_job(store, "finish-summary")
    summary_claim = store.materialize_and_claim_due(
        "scheduler-finish", limit=1, lease_seconds=15
    )[0]
    summary_attempt = store.start_attempt(store.allocate_attempt(summary_claim))
    store.finish_attempt(summary_attempt, outcome="success", summary="x" * 2_100)
    assert (
        len(
            clean_database.fetch_one(
                "SELECT summary FROM job_run WHERE id = ?",
                (str(summary_attempt.run_id),),
            )["summary"]
        )
        == 2_000
    )

    clean_database.execute(
        "UPDATE scheduled_occurrence SET state = 'running', lease_token = ?, "
        "lease_owner = ?, lease_expires_at = clock_timestamp() + INTERVAL '15 seconds', "
        "current_operation_id = ?, operation_execution_generation = ? "
        "WHERE occurrence_id = ?",
        (
            str(summary_attempt.claim.lease_token),
            summary_attempt.claim.lease_owner,
            str(summary_attempt.operation_id),
            summary_attempt.execution_fence.execution_generation,
            str(summary_attempt.claim.occurrence_id),
        ),
    )
    with pytest.raises(StaleOccurrenceClaimError, match="job_run is no longer running"):
        store.finish_attempt(summary_attempt, outcome="success")


def test_finish_attempt_rolls_back_if_occurrence_cas_loses(clean_database):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "finish-cas")
    claim = store.materialize_and_claim_due(
        "scheduler-finish", limit=1, lease_seconds=15
    )[0]
    attempt = store.start_attempt(store.allocate_attempt(claim))
    clean_database.execute(
        """
        CREATE OR REPLACE FUNCTION scheduler_060_rotate_on_run_finish()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.outcome = 'running' AND NEW.outcome <> 'running' THEN
                UPDATE scheduled_occurrence
                SET claim_generation = claim_generation + 1
                WHERE occurrence_id = NEW.occurrence_id;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    clean_database.execute(
        "CREATE TRIGGER scheduler_060_rotate_on_run_finish_trigger "
        "AFTER UPDATE ON job_run FOR EACH ROW "
        "EXECUTE FUNCTION scheduler_060_rotate_on_run_finish()"
    )
    try:
        with pytest.raises(StaleOccurrenceClaimError):
            store.finish_attempt(attempt, outcome="success")
    finally:
        clean_database.execute(
            "DROP TRIGGER IF EXISTS scheduler_060_rotate_on_run_finish_trigger ON job_run"
        )
        clean_database.execute(
            "DROP FUNCTION IF EXISTS scheduler_060_rotate_on_run_finish()"
        )
    assert (
        clean_database.fetch_one(
            "SELECT outcome FROM job_run WHERE id = ?", (str(attempt.run_id),)
        )["outcome"]
        == "running"
    )


@pytest.mark.asyncio
async def test_runner_refuses_ineligible_and_pre_lost_claims():
    orchestrator, _ = _recording_orchestrator()
    runner = JobRunner(orchestrator, object(), _ValidGrants())
    attempt = _dummy_attempt("runner-refusal")
    ineligible = replace(
        attempt,
        claim=replace(
            attempt.claim,
            job={**attempt.job, "handler_kind": "legacy_best_effort"},
        ),
    )
    result = await runner.run_occurrence(ineligible, claim_lost=asyncio.Event())
    assert result.result_code == "handler_not_idempotent"

    lost = asyncio.Event()
    lost.set()
    with pytest.raises(StaleOccurrenceClaimError):
        await runner.run_occurrence(attempt, claim_lost=lost)


@pytest.mark.asyncio
async def test_runner_authority_skip_pauses_and_notifies_once(clean_database):
    class MissingGrants:
        def latest_valid_for(self, user_id, agent_id):
            return None

    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    job = _due_job(store, "authority-skip")
    claim = store.materialize_and_claim_due(
        "scheduler-auth", limit=1, lease_seconds=15
    )[0]
    attempt = store.start_attempt(store.allocate_attempt(claim))
    orchestrator, calls = _recording_orchestrator()
    runner = JobRunner(orchestrator, store, MissingGrants())

    first = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    second = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert first.outcome == second.outcome == "skipped_auth"
    assert first.result_code == "authorization_unavailable"
    assert len(calls["notifications"]) == 1
    assert (
        clean_database.fetch_one(
            "SELECT status FROM scheduled_job WHERE id = ?", (job["id"],)
        )["status"]
        == "paused"
    )


class _EffectScriptStore:
    def __init__(self, reservation: EffectReservation):
        self.reservation = reservation
        self.published: list[dict] = []
        self.failed: list[dict] = []
        self.statuses: list[tuple] = []

    def reserve_effect(self, attempt, **kwargs):
        return self.reservation

    def reserve_atomic_chat_effect(self, attempt, **kwargs):
        return self.reservation

    def publish_effect(self, attempt, **kwargs):
        self.published.append(kwargs)
        return EffectReservation("published", False, False)

    def fail_effect(self, attempt, **kwargs):
        self.failed.append(kwargs)
        return EffectReservation("failed", False, False)

    def set_status(self, *args):
        self.statuses.append(args)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reservation", "expected_code"),
    (
        (EffectReservation("published", False, False), "success"),
        (EffectReservation("reserved", False, True), "effect_outcome_ambiguous"),
    ),
)
async def test_dreaming_occurrence_reconciles_without_repeating(
    reservation,
    expected_code,
):
    attempt = _dummy_attempt("dreaming-reconcile")
    attempt = replace(
        attempt,
        claim=replace(
            attempt.claim,
            job={**attempt.job, "agent_id": "__dreaming__"},
        ),
    )
    store = _EffectScriptStore(reservation)
    runner = JobRunner(SimpleNamespace(), store, _ValidGrants())
    result = await runner._run_dreaming_occurrence(attempt)
    assert result.result_code == expected_code
    assert store.published == []


@pytest.mark.asyncio
async def test_dreaming_occurrence_disabled_success_and_failure(monkeypatch):
    import dreaming.consolidation as consolidation
    import personalization.phi_gate as phi_gate

    attempt = _dummy_attempt("dreaming-paths")
    attempt = replace(
        attempt,
        claim=replace(
            attempt.claim,
            job={**attempt.job, "agent_id": "__dreaming__"},
        ),
    )
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: object())

    disabled_store = _EffectScriptStore(EffectReservation("reserved", True, False))
    disabled_orch = SimpleNamespace(
        personalization_service=SimpleNamespace(
            repo=SimpleNamespace(
                get_profile=lambda _user_id: {"dreaming_enabled": False}
            )
        )
    )
    disabled = await JobRunner(
        disabled_orch, disabled_store, _ValidGrants()
    )._run_dreaming_occurrence(attempt)
    assert disabled.result_code == "dreaming_disabled"
    assert len(disabled_store.failed) == 1
    assert disabled_store.statuses == [
        (attempt.job["user_id"], attempt.job["id"], "paused")
    ]

    enabled_repo = SimpleNamespace(
        get_profile=lambda _user_id: {"dreaming_enabled": True}
    )
    enabled_orch = SimpleNamespace(
        personalization_service=SimpleNamespace(repo=enabled_repo)
    )
    monkeypatch.setattr(
        consolidation,
        "run_sweep",
        lambda *args, **kwargs: {
            "promoted_count": 2,
            "candidates_considered": 3,
        },
    )
    success_store = _EffectScriptStore(EffectReservation("reserved", True, False))
    success = await JobRunner(
        enabled_orch, success_store, _ValidGrants()
    )._run_dreaming_occurrence(attempt)
    assert success.outcome == "success"
    assert success.summary == "Consolidated 2 of 3 signals."
    assert len(success_store.published) == 1

    def fail_sweep(*args, **kwargs):
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(consolidation, "run_sweep", fail_sweep)
    failure_store = _EffectScriptStore(EffectReservation("reserved", True, False))
    failure = await JobRunner(
        enabled_orch, failure_store, _ValidGrants()
    )._run_dreaming_occurrence(attempt)
    assert failure.result_code == "operation_failed"
    assert failure_store.published == []


@pytest.mark.asyncio
async def test_run_occurrence_routes_dreaming_handler(monkeypatch):
    attempt = _dummy_attempt("dreaming-route")
    attempt = replace(
        attempt,
        claim=replace(
            attempt.claim,
            job={**attempt.job, "agent_id": "__dreaming__"},
        ),
    )
    runner = JobRunner(SimpleNamespace(), object(), _ValidGrants())
    expected = OccurrenceRunResult(
        "success", "dreamed", str(attempt.operation_id), False, "success"
    )

    async def dreaming_result(_attempt):
        return expected

    monkeypatch.setattr(runner, "_run_dreaming_occurrence", dreaming_result)
    assert (
        await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    ) == expected


@pytest.mark.asyncio
async def test_notification_replay_and_ambiguity_do_not_redeliver():
    orchestrator, calls = _recording_orchestrator()
    attempt = _dummy_attempt("notification-reconcile")
    for reservation in (
        EffectReservation("published", False, False),
        EffectReservation("reserved", False, True),
    ):
        store = _EffectScriptStore(reservation)
        runner = JobRunner(orchestrator, store, _ValidGrants())
        await runner._notify_occurrence(
            attempt,
            level="success",
            title="ready",
            body="complete",
        )
        assert store.published == []
    assert calls["notifications"] == []


@pytest.mark.asyncio
async def test_runner_refuses_ambiguous_chat_effect():
    orchestrator, calls = _recording_orchestrator()
    store = _EffectScriptStore(EffectReservation("reserved", False, True))
    runner = JobRunner(orchestrator, store, _ValidGrants())
    result = await runner.run_occurrence(
        _dummy_attempt("chat-ambiguous"),
        claim_lost=asyncio.Event(),
    )
    assert result.result_code == "effect_outcome_ambiguous"
    assert calls["turns"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "expected_code"),
    (
        (lambda: RuntimeError("handler failed"), "operation_failed"),
        (
            lambda: __import__(
                "llm_config", fromlist=["LLMUnavailable"]
            ).LLMUnavailable("not configured"),
            "llm_unavailable",
        ),
    ),
)
async def test_runner_reports_handler_failures(
    clean_database,
    error_factory,
    expected_code,
):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, f"handler-{expected_code}")
    claim = store.materialize_and_claim_due(
        "scheduler-handler", limit=1, lease_seconds=15
    )[0]
    attempt = store.start_attempt(store.allocate_attempt(claim))

    async def fail_turn(**kwargs):
        raise error_factory()

    orchestrator, _ = _recording_orchestrator()
    orchestrator.run_scheduled_turn = fail_turn
    runner = JobRunner(orchestrator, store, _ValidGrants())
    result = await runner.run_occurrence(attempt, claim_lost=asyncio.Event())
    assert result.result_code == expected_code
    assert result.outcome == "failure"


@pytest.mark.asyncio
async def test_runner_refuses_result_when_claim_is_lost_after_handler(
    clean_database,
):
    coordinator = _coordinator(clean_database)
    store = ScheduledJobStore(clean_database, coordinator=coordinator)
    _due_job(store, "post-handler-loss")
    claim = store.materialize_and_claim_due(
        "scheduler-handler", limit=1, lease_seconds=15
    )[0]
    attempt = store.start_attempt(store.allocate_attempt(claim))
    claim_lost = asyncio.Event()
    orchestrator, _ = _recording_orchestrator()

    async def lose_after_turn(**kwargs):
        claim_lost.set()
        return "must not publish"

    orchestrator.run_scheduled_turn = lose_after_turn
    runner = JobRunner(orchestrator, store, _ValidGrants())
    with pytest.raises(StaleOccurrenceClaimError):
        await runner.run_occurrence(attempt, claim_lost=claim_lost)
    assert (
        clean_database.fetch_one(
            "SELECT state FROM effect_ledger WHERE occurrence_id = ? "
            "AND effect_kind = 'chat_history'",
            (str(claim.occurrence_id),),
        )["state"]
        == "reserved"
    )


@pytest.mark.asyncio
async def test_claim_lease_keeper_marks_exceptions_as_authority_loss():
    class FailingStore:
        def renew_claim(self, claim, *, lease_seconds):
            raise RuntimeError("database unavailable")

    keeper = ClaimLeaseKeeper(
        FailingStore(), _dummy_claim("renew-error"), lease_seconds=5
    )
    keeper.interval_seconds = 0.001
    keeper.start()
    keeper.start()
    await asyncio.wait_for(keeper.lost.wait(), timeout=1)
    await keeper.stop()
    assert keeper.successful_renewals == 0


@pytest.mark.asyncio
async def test_legacy_scheduler_lifecycle_dispatch_and_shutdown():
    class LegacyStore:
        def __init__(self):
            self.reconciled = 0

        def reconcile_interrupted(self):
            self.reconciled += 1
            return 2

        def list_due(self, now_ms):
            return [
                {
                    "id": "legacy-job",
                    "user_id": "legacy-owner",
                    "target_chat_id": None,
                }
            ]

    class LegacyRunner:
        async def run_job(self, job):
            return f"ran:{job['id']}"

    class LegacyTaskManager:
        def __init__(self):
            self.submissions = []

        async def submit(self, *args):
            self.submissions.append(args)

    store = LegacyStore()
    runner = LegacyRunner()
    task_manager = LegacyTaskManager()
    loop = SchedulerLoop(
        store,
        runner,
        task_manager,
        claim_lease_seconds=5,
    )
    loop.start()
    loop.start()
    await asyncio.sleep(0.02)
    await loop.stop()
    assert store.reconciled == 1
    assert len(task_manager.submissions) == 1
    assert await loop._job_coro(None, {"id": "legacy-job"}) == "ran:legacy-job"

    pending = asyncio.create_task(asyncio.Event().wait())
    loop._dispatch_tasks.add(pending)
    await loop.stop()
    assert pending.cancelled()


class _LoopCoordinator:
    def __init__(
        self,
        *,
        stale_terminalization: bool = False,
        renewal_error: Exception | None = None,
    ):
        self.terminal = []
        self.stale_terminalization = stale_terminalization
        self.slot_lease = timedelta(milliseconds=20)
        self.renewal_error = renewal_error
        self.execution_renewals = []

    def renew_execution_lease(self, fence):
        self.execution_renewals.append(fence)
        if self.renewal_error is not None:
            raise self.renewal_error

    def terminalize(self, fence, **kwargs):
        if self.stale_terminalization:
            raise StaleExecutionFenceError("already terminal")
        self.terminal.append((fence, kwargs))


class _LoopStore:
    def __init__(self, attempt: ScheduledAttempt):
        self.attempt = attempt
        self.claim_results: list[ScheduledAttempt | None] = []
        self.allocate_error: Exception | None = None
        self.start_error: Exception | None = None
        self.finish_error: Exception | None = None
        self.finished = []
        self.marked = []
        self.bound = None

    def bind_coordinator(self, coordinator):
        self.bound = coordinator

    def renew_claim(self, claim, *, lease_seconds):
        return datetime.now(UTC) + timedelta(seconds=lease_seconds)

    def allocate_attempt(self, claim):
        if self.allocate_error is not None:
            raise self.allocate_error
        return self.attempt

    def claim_attempt_execution(self, attempt):
        if self.claim_results:
            return self.claim_results.pop(0)
        return None

    def start_attempt(self, attempt, *, lease_seconds):
        if self.start_error is not None:
            raise self.start_error
        return replace(attempt, run_id=attempt.run_id or uuid.uuid4())

    def finish_attempt(self, attempt, **kwargs):
        if self.finish_error is not None:
            raise self.finish_error
        self.finished.append((attempt, kwargs))

    def mark_claim_retryable(self, claim, **kwargs):
        self.marked.append((claim, kwargs))


class _LoopRunner:
    def __init__(self, run):
        self._run = run
        self.bound = None

    def bind_execution_context(self, *, coordinator, store):
        self.bound = (coordinator, store)

    async def run_occurrence(self, attempt, *, claim_lost):
        return await self._run(attempt, claim_lost)


class _ScriptKeeper:
    instances = []
    lose_on_start = False
    lose_soon = False

    def __init__(self, store, claim, *, lease_seconds):
        self.lost = asyncio.Event()
        self.stopped = False
        type(self).instances.append(self)

    def start(self):
        if type(self).lose_on_start:
            self.lost.set()
        elif type(self).lose_soon:
            asyncio.get_running_loop().call_soon(self.lost.set)

    async def stop(self):
        self.stopped = True


def _script_loop(store, runner, coordinator):
    return SchedulerLoop(
        store,
        runner,
        coordinator=coordinator,
        instance_id="scheduler-script",
        claim_lease_seconds=5,
    )


@pytest.mark.asyncio
async def test_dispatch_polls_exact_queued_attempt_until_selected(monkeypatch):
    queued = _dummy_attempt("queue-select", selected=False, started=False)
    selected = replace(
        queued,
        operation_state=OperationState.RUNNING,
        execution_fence=_dummy_attempt("queue-select").execution_fence,
    )
    store = _LoopStore(queued)
    store.claim_results = [None, selected]
    coordinator = _LoopCoordinator()

    async def retryable_result(attempt, claim_lost):
        return OccurrenceRunResult(
            "failure", "retry later", str(attempt.operation_id), True, "transient"
        )

    runner = _LoopRunner(retryable_result)
    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    await _script_loop(store, runner, coordinator)._dispatch_claim(queued.claim)
    assert len(store.finished) == 1
    assert coordinator.terminal[0][1]["state"] is OperationState.RETRYABLE


@pytest.mark.asyncio
async def test_dispatch_refuses_queue_when_service_is_stopping(monkeypatch):
    queued = _dummy_attempt("queue-stop", selected=False, started=False)
    store = _LoopStore(queued)

    async def unused_runner(attempt, claim_lost):
        raise AssertionError("queued operation must not start")

    loop = _script_loop(store, _LoopRunner(unused_runner), _LoopCoordinator())
    loop._stop.set()
    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    await loop._dispatch_claim(queued.claim)
    assert store.claim_results == []


@pytest.mark.asyncio
async def test_dispatch_refuses_start_when_keeper_already_lost(monkeypatch):
    attempt = _dummy_attempt("lost-before-start", started=False)
    store = _LoopStore(attempt)
    coordinator = _LoopCoordinator()

    async def unused_runner(attempt, claim_lost):
        raise AssertionError("lost claim must not run")

    class LostKeeper(_ScriptKeeper):
        lose_on_start = True

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", LostKeeper)
    await _script_loop(store, _LoopRunner(unused_runner), coordinator)._dispatch_claim(
        attempt.claim
    )
    assert coordinator.terminal[0][1]["terminal_code"] == "claim_lost"


@pytest.mark.asyncio
async def test_dispatch_cancels_handler_immediately_on_renewal_loss(monkeypatch):
    attempt = _dummy_attempt("lost-during-run", started=False)
    store = _LoopStore(attempt)
    coordinator = _LoopCoordinator()
    cancelled = asyncio.Event()

    async def blocking_runner(attempt, claim_lost):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class SoonLostKeeper(_ScriptKeeper):
        def start(self):
            asyncio.get_running_loop().call_later(0.01, self.lost.set)

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", SoonLostKeeper)
    await _script_loop(
        store, _LoopRunner(blocking_runner), coordinator
    )._dispatch_claim(attempt.claim)
    assert cancelled.is_set()
    assert coordinator.terminal[0][1]["terminal_code"] == "claim_lost"


@pytest.mark.asyncio
async def test_dispatch_renews_operation_execution_lease_while_handler_runs(
    monkeypatch,
):
    attempt = _dummy_attempt("operation-lease-renewed", started=False)
    store = _LoopStore(attempt)
    coordinator = _LoopCoordinator()

    async def long_runner(attempt, claim_lost):
        await asyncio.sleep(0.04)
        return OccurrenceRunResult(
            "success", "renewed", str(attempt.operation_id), False, None
        )

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    await _script_loop(store, _LoopRunner(long_runner), coordinator)._dispatch_claim(
        attempt.claim
    )
    assert coordinator.execution_renewals
    assert all(
        fence == attempt.execution_fence for fence in coordinator.execution_renewals
    )


@pytest.mark.asyncio
async def test_dispatch_revokes_handler_on_operation_lease_loss(monkeypatch):
    attempt = _dummy_attempt("operation-lease-lost", started=False)
    store = _LoopStore(attempt)
    coordinator = _LoopCoordinator(
        renewal_error=StaleExecutionFenceError("operation lease rotated")
    )
    cancelled = asyncio.Event()

    async def resistant_until_cancelled(attempt, claim_lost):
        try:
            await asyncio.sleep(0.05)
            return OccurrenceRunResult(
                "success", "must not publish", str(attempt.operation_id), False, None
            )
        finally:
            cancelled.set()

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    await _script_loop(
        store, _LoopRunner(resistant_until_cancelled), coordinator
    )._dispatch_claim(attempt.claim)
    assert cancelled.is_set()
    assert not store.finished
    assert coordinator.terminal[0][1]["state"] is OperationState.RETRYABLE
    assert coordinator.terminal[0][1]["terminal_code"] == "operation_lease_lost"


@pytest.mark.asyncio
async def test_dispatch_bounds_cancellation_resistant_handler_after_lease_loss(
    monkeypatch,
):
    attempt = _dummy_attempt("operation-lease-resistant", started=False)
    store = _LoopStore(attempt)
    coordinator = _LoopCoordinator(
        renewal_error=StaleExecutionFenceError("operation lease rotated")
    )
    release = asyncio.Event()
    resisted = asyncio.Event()

    async def cancellation_resistant_runner(attempt, claim_lost):
        try:
            await release.wait()
        except asyncio.CancelledError:
            resisted.set()
            await release.wait()
        return OccurrenceRunResult(
            "success", "fenced stale result", str(attempt.operation_id), False, None
        )

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    loop = _script_loop(store, _LoopRunner(cancellation_resistant_runner), coordinator)
    dispatch = asyncio.create_task(loop._dispatch_claim(attempt.claim))
    try:
        done, _ = await asyncio.wait({dispatch}, timeout=0.75)
        assert dispatch in done
        assert resisted.is_set()
        assert store.marked[0][1]["error_code"] == "operation_lease_lost"
        assert len(loop._handler_remainders) == 1
    finally:
        release.set()
        await asyncio.wait_for(dispatch, timeout=1)
        if loop._handler_remainders:
            await asyncio.wait_for(
                asyncio.gather(*tuple(loop._handler_remainders)), timeout=1
            )


@pytest.mark.asyncio
async def test_dispatch_treats_finish_fence_loss_as_retryable_authority_loss(
    monkeypatch,
):
    """A renewal/finish race must not misreport stale work as a handler failure."""

    attempt = _dummy_attempt("operation-lease-lost-at-finish", started=False)
    store = _LoopStore(attempt)
    store.finish_error = StaleExecutionFenceError("operation lease rotated")
    coordinator = _LoopCoordinator()

    async def completed_runner(attempt, claim_lost):
        return OccurrenceRunResult(
            "success", "stale result", str(attempt.operation_id), False, None
        )

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    await _script_loop(
        store, _LoopRunner(completed_runner), coordinator
    )._dispatch_claim(attempt.claim)

    assert store.marked[0][1]["error_code"] == "operation_lease_lost"
    assert coordinator.terminal[0][1]["state"] is OperationState.RETRYABLE
    assert coordinator.terminal[0][1]["terminal_code"] == "operation_lease_lost"


@pytest.mark.asyncio
async def test_dispatch_handles_admission_and_stale_claim_failures(monkeypatch):
    attempt = _dummy_attempt("dispatch-errors", started=False)

    async def unused_runner(attempt, claim_lost):
        raise AssertionError("failed start must not run")

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    refusal_store = _LoopStore(attempt)
    refusal_store.allocate_error = ScheduledAdmissionRefusedError(
        RefusedAdmission(False, "capacity_exhausted", True, 1_000)
    )
    await _script_loop(
        refusal_store,
        _LoopRunner(unused_runner),
        _LoopCoordinator(),
    )._dispatch_claim(attempt.claim)

    stale_store = _LoopStore(attempt)
    stale_store.start_error = StaleOccurrenceClaimError("rotated")
    coordinator = _LoopCoordinator()
    await _script_loop(
        stale_store,
        _LoopRunner(unused_runner),
        coordinator,
    )._dispatch_claim(attempt.claim)
    assert coordinator.terminal[0][1]["terminal_code"] == "claim_lost"


@pytest.mark.asyncio
async def test_dispatch_cancellation_marks_retryable_and_propagates(monkeypatch):
    attempt = _dummy_attempt("dispatch-cancel", started=False)
    store = _LoopStore(attempt)
    coordinator = _LoopCoordinator()
    entered = asyncio.Event()

    async def blocking_runner(attempt, claim_lost):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    dispatch = asyncio.create_task(
        _script_loop(store, _LoopRunner(blocking_runner), coordinator)._dispatch_claim(
            attempt.claim
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert store.marked[0][1]["error_code"] == "service_draining"
    assert coordinator.terminal[0][1]["terminal_code"] == "service_draining"


@pytest.mark.asyncio
async def test_dispatch_failure_records_run_and_terminalizes(monkeypatch):
    attempt = _dummy_attempt("dispatch-failure", started=False)
    store = _LoopStore(attempt)
    store.finish_error = RuntimeError("finish fence lost")
    coordinator = _LoopCoordinator()

    async def failing_runner(attempt, claim_lost):
        raise RuntimeError("handler failed")

    monkeypatch.setattr(scheduler_loop_module, "ClaimLeaseKeeper", _ScriptKeeper)
    await _script_loop(store, _LoopRunner(failing_runner), coordinator)._dispatch_claim(
        attempt.claim
    )
    assert coordinator.terminal[0][1]["state"] is OperationState.FAILED


@pytest.mark.asyncio
async def test_terminalization_is_noop_without_fence_and_tolerates_stale_fence():
    attempt = _dummy_attempt("terminal-noop", selected=False, started=False)
    store = _LoopStore(attempt)

    async def unused_runner(attempt, claim_lost):
        raise AssertionError

    no_coordinator = SchedulerLoop(
        store,
        _LoopRunner(unused_runner),
        claim_lease_seconds=5,
    )
    await no_coordinator._terminalize_attempt(
        attempt,
        state=OperationState.RETRYABLE,
        code="claim_lost",
        summary=None,
    )

    selected = _dummy_attempt("terminal-stale")
    stale_coordinator = _LoopCoordinator(stale_terminalization=True)
    loop = _script_loop(store, _LoopRunner(unused_runner), stale_coordinator)
    await loop._terminalize_attempt(
        selected,
        state=OperationState.RETRYABLE,
        code="claim_lost",
        summary=None,
    )
