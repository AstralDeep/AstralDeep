"""Real-PostgreSQL tests for astralplane.repositories.assignments and scheduler:
policy-allowance admission, one-outstanding-episode enforcement, terminal Stop
history, and the policy-absent legacy-recurrence regression.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import PlaneError
from astralplane.repositories.assignments import AssignmentRepository
from astralplane.repositories.scheduler import OccurrenceState, ScheduledJob, SchedulerRepository
from astralplane.repositories.scheduler_models import ScheduledJobPolicy
from tests.fixtures.migrated_template import bound_clone
from tests.repositories.test_assignments_postgres import Pool, control, create

OWNER = "owner"
OTHER_OWNER = "other-owner"


@pytest.fixture(scope="module")
def plane(migrated_template):
    with bound_clone(migrated_template) as clone:
        pool = ConnectionPool(Pool(clone.connection))
        try:
            yield PlaneDatabase(pool), clone.schema, clone.dsn
        finally:
            pool.close()


@pytest.fixture
def database(plane):
    db = plane[0]
    with db.transaction() as tx:
        tx.execute("DELETE FROM scheduled_occurrence_assignment")
        tx.execute("DELETE FROM job_run")
        tx.execute("DELETE FROM scheduled_occurrence")
        tx.execute("DELETE FROM scheduled_job_policy")
        tx.execute("DELETE FROM scheduled_job")
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        tx.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id=%s", (OWNER,))
    return db


@pytest.fixture
def repo():
    return SchedulerRepository()


def second_database(plane):
    import psycopg2

    if plane[2].startswith("sqlite:"):
        from astralplane.database.sqlite import create_sqlite_driver_pool

        connection = create_sqlite_driver_pool(plane[2]).getconn()
        pool = ConnectionPool(Pool(connection))
        return PlaneDatabase(pool), pool, connection
    connection = psycopg2.connect(plane[2])
    with connection.cursor() as cursor:
        cursor.execute(f'SET search_path TO "{plane[1]}",pg_catalog')
        cursor.execute("SET statement_timeout = '8000ms'")
    connection.commit()
    pool = ConnectionPool(Pool(connection))
    return PlaneDatabase(pool), pool, connection


def uid():
    return str(uuid.uuid4())


_OFFSETS = itertools.count(1)


def now_ms():
    return int(time.time() * 1000)


def job(tx, repo, *, owner_id=OWNER, kind="cron", status="active", next_run_at=None):
    expression = {"cron": "0 9 * * *", "interval": "PT1H", "one_shot": "at"}[kind]
    value = ScheduledJob(
        job_id=uid(),
        owner_id=owner_id,
        name=f"Policy {kind}",
        instruction="Read a synthetic source",
        schedule_kind=kind,
        schedule_expression=expression,
        timezone="UTC",
        status=status,
        next_run_at=now_ms() + 60_000 if next_run_at is None else next_run_at,
        created_at=1,
        updated_at=1,
    )
    return repo.create_job_definition(tx, value)


def policy(job_id, **changes):
    values = dict(
        job_id=job_id,
        owner_id=OWNER,
        version=1,
        max_runs=3,
        admitted_runs=0,
        per_episode_limits={"model_calls": 4},
        max_outstanding_episodes=1,
        monitor_changes=True,
        definition_revision=1,
        terminal_stop=False,
        last_assignment_id=None,
        updated_at=5,
    )
    values.update(changes)
    return ScheduledJobPolicy(**values)


def claimed(tx, repo, definition, *, worker="worker-1", owner_id=OWNER):
    now = datetime.now(UTC)
    occurrence_id = uid()
    repo.create_occurrence(
        tx,
        occurrence_id=occurrence_id,
        job_id=definition.job_id,
        owner_id=owner_id,
        scheduled_for=now - timedelta(seconds=1, microseconds=next(_OFFSETS)),
    )
    return repo.claim_occurrence(
        tx,
        owner_id=owner_id,
        occurrence_id=occurrence_id,
        worker_id=worker,
        lease_token=uid(),
        now=now,
        lease_expires_at=now + timedelta(seconds=60),
    )


def pending(tx, repo, definition):
    occurrence_id = uid()
    repo.create_occurrence(
        tx,
        occurrence_id=occurrence_id,
        job_id=definition.job_id,
        owner_id=OWNER,
        scheduled_for=datetime.now(UTC) + timedelta(seconds=30, microseconds=next(_OFFSETS)),
    )
    return occurrence_id


def started(tx, occurrence):
    tx.execute(
        "UPDATE scheduled_occurrence SET state='running', started_at=clock_timestamp() "
        "WHERE occurrence_id=%s AND claim_generation=%s AND lease_token=%s",
        (occurrence.occurrence_id, occurrence.claim_generation, occurrence.lease_token),
    )


def admit(tx, repo, definition, occurrence, assignment_id, **changes):
    values = dict(
        owner_id=OWNER,
        job_id=definition.job_id,
        occurrence_id=occurrence.occurrence_id,
        claim_generation=occurrence.claim_generation,
        lease_token=occurrence.lease_token,
        lease_owner=occurrence.lease_owner,
        assignment_id=assignment_id,
        admitted_at=now_ms(),
    )
    values.update(changes)
    return repo.admit_assignment_episode(tx, **values)


def episode(tx):
    return create(AssignmentRepository(), tx).assignment_id


def resolve(tx, assignment_id):
    repo = AssignmentRepository()
    record = repo.get_assignment(tx, owner_id=OWNER, assignment_id=assignment_id)
    stopped = control(repo, tx, record, "stop").assignment
    assert stopped.lifecycle == "stopped"


def bindings(tx, job_id):
    return tx.fetch_all(
        "SELECT occurrence_id, assignment_id, spend FROM scheduled_occurrence_assignment "
        "WHERE job_id=%s ORDER BY admitted_at, assignment_id",
        (job_id,),
    )


def rows(tx, table, order):
    return tuple(dict(row) for row in tx.fetch_all(f"SELECT * FROM {table} ORDER BY {order}"))


def test_current_scheduler_policy_structure_digest(database):
    from astralplane.database import migrations as m

    with database.transaction() as tx:
        if tx.dialect.is_sqlite:
            from astralplane.database.sqlite_migrations import (
                SQLITE_SCHEMA_STRUCTURE_DIGEST,
                read_structure_digest,
            )

            assert read_structure_digest(tx) == SQLITE_SCHEMA_STRUCTURE_DIGEST
            return
        assert (
            m._schema_structure_digest(tx.fetch_all(m.CURRENT_SCHEMA_STRUCTURE_QUERY))
            == m.CURRENT_SCHEMA_STRUCTURE_DIGEST
        )


@pytest.mark.parametrize("kind", ["cron", "interval", "one_shot"])
def test_policy_absent_keeps_legacy_recurrence_semantics(database, repo, kind):
    with database.transaction() as tx:
        definition = job(tx, repo, kind=kind, next_run_at=10)
        assert repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id) is None
        assert repo.list_due_jobs_for_administration(tx, due_at_ms=10) == (definition,)
        assert repo.list_due_jobs_for_administration(tx, due_at_ms=9) == ()
        run_now = repo.materialize_run_now(
            tx, owner_id=OWNER, job_id=definition.job_id, submission_id=uid()
        )
        assert run_now.created and run_now.state is OccurrenceState.PENDING
        assert repo.update_job_after_run_for_administration(
            tx,
            job_id=definition.job_id,
            last_run_at=20,
            next_run_at=None if kind == "one_shot" else 30,
            completed=kind == "one_shot",
            updated_at=21,
        )
        after_run = repo.get_job(tx, owner_id=OWNER, job_id=definition.job_id)
        assert after_run == replace(
            definition,
            status="completed" if kind == "one_shot" else "active",
            last_run_at=20,
            next_run_at=None if kind == "one_shot" else 30,
            updated_at=21,
        )
        occurrence = claimed(tx, repo, definition)
        assignment_id = episode(tx)
        before = (
            rows(tx, "scheduled_job", "id"),
            rows(tx, "scheduled_occurrence", "occurrence_id"),
        )
        admission = admit(tx, repo, definition, occurrence, assignment_id)
        assert not admission.admitted and admission.reason == "policy_missing"
        assert admission.policy is None and not admission.created
        assert bindings(tx, definition.job_id) == ()
        assert repo.list_outstanding_episodes(tx, owner_id=OWNER, job_id=definition.job_id) == ()
        assert (
            rows(tx, "scheduled_job", "id"),
            rows(tx, "scheduled_occurrence", "occurrence_id"),
        ) == before
        unstarted = repo.transition_job_and_list_unstarted(
            tx, owner_id=OWNER, job_id=definition.job_id, status="paused"
        )
        assert {item.occurrence_id for item in unstarted} == {
            run_now.occurrence_id,
            occurrence.occurrence_id,
        }
        for item in unstarted:
            assert repo.cancel_unstarted_occurrence(
                tx,
                owner_id=OWNER,
                occurrence_id=item.occurrence_id,
                expected_operation_id=item.operation_id,
                terminal_code="cancelled_job_paused",
            )
        assert repo.get_job(tx, owner_id=OWNER, job_id=definition.job_id).status == "paused"
        assert tx.fetch_one("SELECT count(*) AS n FROM scheduled_job_policy")["n"] == 0


def test_put_policy_is_version_cas_owner_scoped_and_validates_before_sql(database, repo):
    with database.transaction() as tx:
        definition = job(tx, repo)
        created = repo.put_job_policy(tx, policy=policy(definition.job_id), expected_version=0)
        assert created == policy(definition.job_id)
        assert created.per_episode_limits == (("model_calls", 4),)
        assert repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id) == created
        assert repo.get_job_policy(tx, owner_id=OTHER_OWNER, job_id=definition.job_id) is None
        with pytest.raises(PlaneError) as replay:
            repo.put_job_policy(tx, policy=policy(definition.job_id), expected_version=0)
        assert replay.value.code == "scheduled_job_policy_version_conflict"
        updated = repo.put_job_policy(
            tx,
            policy=policy(definition.job_id, version=2, max_runs=5, updated_at=6),
            expected_version=1,
        )
        assert (updated.version, updated.max_runs, updated.admitted_runs) == (2, 5, 0)
        with pytest.raises(PlaneError) as stale:
            repo.put_job_policy(
                tx, policy=policy(definition.job_id, version=2, max_runs=9), expected_version=1
            )
        assert stale.value.code == "scheduled_job_policy_version_conflict"
        assert dict(stale.value.metadata)["observed_version"] == "2"
        with pytest.raises(PlaneError) as rewrite:
            repo.put_job_policy(
                tx,
                policy=policy(definition.job_id, version=3, terminal_stop=True),
                expected_version=2,
            )
        assert rewrite.value.code == "scheduled_job_policy_charge_rewrite"
        with pytest.raises(PlaneError) as foreign:
            repo.put_job_policy(
                tx,
                policy=policy(definition.job_id, owner_id=OTHER_OWNER),
                expected_version=0,
            )
        assert foreign.value.code == "scheduled_job_missing"
        with pytest.raises(PlaneError) as missing:
            repo.put_job_policy(
                tx,
                policy=policy(job(tx, repo).job_id, version=2),
                expected_version=1,
            )
        assert missing.value.code == "scheduled_job_policy_missing"
        assert repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id) == updated
    for changes in (
        {"max_runs": 0},
        {"max_runs": 1_000_001},
        {"admitted_runs": -1},
        {"admitted_runs": 4},
        {"max_outstanding_episodes": 0},
        {"max_outstanding_episodes": 65},
        {"per_episode_limits": {"model_calls": -1}},
        {"per_episode_limits": {"Model": 1}},
        {"per_episode_limits": {f"k{i}": 1 for i in range(33)}},
        {"definition_revision": 0},
        {"version": 0},
        {"monitor_changes": 1},
        {"last_assignment_id": "not-a-uuid"},
    ):
        with pytest.raises(ValueError):
            policy(uid(), **changes)


def test_allowance_admits_until_exhausted_then_refuses_without_binding(database, repo):
    with database.transaction() as tx:
        definition = job(tx, repo)
        repo.put_job_policy(
            tx, policy=policy(definition.job_id, max_runs=2, max_outstanding_episodes=4),
            expected_version=0,
        )
        first, second, third = (claimed(tx, repo, definition) for _ in range(3))
        episodes = [episode(tx) for _ in range(3)]
        one = admit(tx, repo, definition, first, episodes[0])
        assert (one.admitted, one.created, one.reason, one.spend) == (True, True, "admitted", 1)
        assert (one.policy.admitted_runs, one.policy.version) == (1, 2)
        assert one.policy.last_assignment_id == episodes[0]
        two = admit(tx, repo, definition, second, episodes[1])
        assert two.admitted and two.policy.remaining_runs == 0
        assert two.policy.last_assignment_id == episodes[1]
        exhausted = admit(tx, repo, definition, third, episodes[2])
        assert not exhausted.admitted and exhausted.reason == "allowance_exhausted"
        assert exhausted.policy == two.policy
        replay = admit(tx, repo, definition, first, episodes[0])
        assert (replay.admitted, replay.created, replay.reason) == (True, False, "replayed")
        assert replay.policy == two.policy
        bound = [str(row["assignment_id"]) for row in bindings(tx, definition.job_id)]
        assert bound == episodes[:2]
        current = repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id)
        assert (current.admitted_runs, current.max_runs, current.version) == (2, 2, 3)
        with pytest.raises(ValueError, match="admitted_runs"):
            replace(current, version=4, max_runs=1)
        raised = repo.put_job_policy(
            tx, policy=replace(current, version=4, max_runs=3), expected_version=3
        )
        assert raised.remaining_runs == 1
        late = admit(tx, repo, definition, third, episodes[2], spend=2)
        assert not late.admitted and late.reason == "allowance_exhausted"
        assert admit(tx, repo, definition, third, episodes[2]).admitted
        with pytest.raises(ValueError):
            admit(tx, repo, definition, third, episodes[2], spend=-1)


def test_second_admission_while_an_episode_is_outstanding_consumes_nothing(database, repo):
    with database.transaction() as tx:
        definition = job(tx, repo)
        repo.put_job_policy(tx, policy=policy(definition.job_id, max_runs=5), expected_version=0)
        first, second = claimed(tx, repo, definition), claimed(tx, repo, definition)
        a, b = episode(tx), episode(tx)
        assert admit(tx, repo, definition, first, a).admitted
        assert repo.list_outstanding_episodes(tx, owner_id=OWNER, job_id=definition.job_id) == (a,)
        refused = admit(tx, repo, definition, second, b)
        assert not refused.admitted and refused.reason == "episode_outstanding"
        assert refused.policy.admitted_runs == 1 and refused.policy.last_assignment_id == a
        assert len(bindings(tx, definition.job_id)) == 1
        resolve(tx, a)
        assert repo.list_outstanding_episodes(tx, owner_id=OWNER, job_id=definition.job_id) == ()
        admitted = admit(tx, repo, definition, second, b)
        assert admitted.admitted and admitted.policy.admitted_runs == 2
        assert repo.list_outstanding_episodes(tx, owner_id=OWNER, job_id=definition.job_id) == (b,)
        assert [str(row["assignment_id"]) for row in bindings(tx, definition.job_id)] == [a, b]
        assert (
            repo.list_outstanding_episodes(tx, owner_id=OTHER_OWNER, job_id=definition.job_id)
            == ()
        )


def test_stop_is_terminal_cancels_unstarted_and_keeps_history_and_charges(database, repo):
    with database.transaction() as tx:
        definition = job(tx, repo, next_run_at=1)
        repo.put_job_policy(tx, policy=policy(definition.job_id, max_runs=5), expected_version=0)
        running = claimed(tx, repo, definition)
        a = episode(tx)
        assert admit(tx, repo, definition, running, a).admitted
        started(tx, running)
        waiting = pending(tx, repo, definition)
        held = claimed(tx, repo, definition, worker="worker-2")
        before_bindings = bindings(tx, definition.job_id)
        with pytest.raises(PlaneError) as stale:
            repo.stop_assignment_job(
                tx, owner_id=OWNER, job_id=definition.job_id, expected_version=1, stopped_at=50
            )
        assert stale.value.code == "scheduled_job_policy_version_conflict"
        outcome = repo.stop_assignment_job(
            tx, owner_id=OWNER, job_id=definition.job_id, expected_version=2, stopped_at=50
        )
        assert outcome.stopped and outcome.policy.terminal_stop
        assert (outcome.policy.version, outcome.policy.admitted_runs) == (3, 1)
        assert set(outcome.cancelled_occurrence_ids) == {waiting, held.occurrence_id}
        assert outcome.cancelled_operation_ids == ()
        assert outcome.outstanding_assignment_ids == (a,)
        stopped_job = repo.get_job(tx, owner_id=OWNER, job_id=definition.job_id)
        assert (stopped_job.status, stopped_job.next_run_at, stopped_job.updated_at) == (
            "completed",
            None,
            50,
        )
        assert repo.list_due_jobs_for_administration(tx, due_at_ms=now_ms()) == ()
        states = {
            str(row["occurrence_id"]): (str(row["state"]), row["result_code"])
            for row in tx.fetch_all("SELECT * FROM scheduled_occurrence WHERE job_id=%s", (
                definition.job_id,
            ))
        }
        assert states[running.occurrence_id] == ("running", None)
        assert states[waiting] == ("cancelled", "cancelled_job_stopped")
        assert states[held.occurrence_id] == ("cancelled", "cancelled_job_stopped")
        assert bindings(tx, definition.job_id) == before_bindings
        refused = admit(tx, repo, definition, running, a)
        assert not refused.admitted and refused.reason == "terminal_stop"
        fresh = claimed(tx, repo, definition, worker="worker-3")
        again = admit(tx, repo, definition, fresh, episode(tx))
        assert not again.admitted and again.reason == "terminal_stop"
        assert bindings(tx, definition.job_id) == before_bindings
        repeat = repo.stop_assignment_job(
            tx, owner_id=OWNER, job_id=definition.job_id, expected_version=3, stopped_at=51
        )
        assert not repeat.stopped and repeat.policy == outcome.policy
        assert repeat.outstanding_assignment_ids == (a,)
        assert repeat.cancelled_occurrence_ids == ()
        resolve(tx, a)
        assert repo.stop_assignment_job(
            tx, owner_id=OWNER, job_id=definition.job_id, expected_version=3, stopped_at=52
        ).outstanding_assignment_ids == ()
        with pytest.raises(PlaneError) as revive:
            repo.put_job_policy(
                tx,
                policy=replace(outcome.policy, version=4, terminal_stop=False),
                expected_version=3,
            )
        assert revive.value.code == "scheduled_job_policy_charge_rewrite"
        assert repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id) == outcome.policy
        with pytest.raises(PlaneError) as no_policy:
            repo.stop_assignment_job(
                tx, owner_id=OWNER, job_id=job(tx, repo).job_id, expected_version=1, stopped_at=1
            )
        assert no_policy.value.code == "scheduled_job_policy_missing"
        with pytest.raises(PlaneError) as foreign:
            repo.stop_assignment_job(
                tx, owner_id=OTHER_OWNER, job_id=definition.job_id, expected_version=3, stopped_at=1
            )
        assert foreign.value.code == "scheduled_job_missing"


def test_pause_suspends_triggers_only_and_last_run_projection_is_unchanged(database, repo):
    with database.transaction() as tx:
        definition = job(tx, repo, next_run_at=10)
        created = repo.put_job_policy(tx, policy=policy(definition.job_id), expected_version=0)
        unstarted = repo.transition_job_and_list_unstarted(
            tx, owner_id=OWNER, job_id=definition.job_id, status="paused"
        )
        assert unstarted == ()
        assert repo.get_job(tx, owner_id=OWNER, job_id=definition.job_id).status == "paused"
        assert repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id) == created
        assert repo.list_due_jobs_for_administration(tx, due_at_ms=10) == ()
        assert repo.set_job_status(
            tx, owner_id=OWNER, job_id=definition.job_id, status="active", updated_at=11
        )
        assert repo.list_due_jobs_for_administration(tx, due_at_ms=10) == (
            replace(definition, updated_at=11),
        )
        occurrence = claimed(tx, repo, definition)
        a = episode(tx)
        admission = admit(tx, repo, definition, occurrence, a, admitted_at=12)
        assert admission.admitted and admission.policy.last_assignment_id == a
        assert admission.policy.updated_at == 12
        assert repo.update_job_after_run_for_administration(
            tx,
            job_id=definition.job_id,
            last_run_at=20,
            next_run_at=30,
            completed=False,
            updated_at=21,
        )
        projected = repo.get_job(tx, owner_id=OWNER, job_id=definition.job_id)
        assert (projected.last_run_at, projected.next_run_at) == (20, 30)
        assert projected.status == "active"
        assert repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id) == admission.policy


def test_admission_fails_closed_on_stale_claim_foreign_or_resolved_episode(database, repo):
    with database.transaction() as tx:
        definition = job(tx, repo)
        other = job(tx, repo)
        repo.put_job_policy(
            tx, policy=policy(definition.job_id, max_outstanding_episodes=4), expected_version=0
        )
        occurrence = claimed(tx, repo, definition)
        a, b = episode(tx), episode(tx)
        with pytest.raises(PlaneError) as stale:
            admit(tx, repo, definition, occurrence, a, lease_token=uid())
        assert stale.value.code == "stale_occurrence_claim"
        with pytest.raises(PlaneError) as missing:
            admit(tx, repo, definition, occurrence, uid())
        assert missing.value.code == "scheduled_episode_assignment_missing"
        resolve(tx, b)
        with pytest.raises(PlaneError) as resolved:
            admit(tx, repo, definition, occurrence, b)
        assert resolved.value.code == "scheduled_episode_assignment_resolved"
        assert admit(tx, repo, other, occurrence, a).reason == "policy_missing"
        repo.put_job_policy(tx, policy=policy(other.job_id), expected_version=0)
        with pytest.raises(PlaneError) as wrong_job:
            admit(tx, repo, other, occurrence, a)
        assert wrong_job.value.code == "scheduled_occurrence_job_mismatch"
        assert bindings(tx, definition.job_id) == () and bindings(tx, other.job_id) == ()
        assert admit(tx, repo, definition, occurrence, a).admitted
        with pytest.raises(PlaneError) as conflict:
            admit(tx, repo, definition, occurrence, episode(tx))
        assert conflict.value.code == "scheduled_occurrence_binding_conflict"
        later = claimed(tx, repo, definition)
        repo.transition_job_and_list_unstarted(
            tx, owner_id=OWNER, job_id=definition.job_id, status="paused"
        )
        with pytest.raises(PlaneError) as paused:
            admit(tx, repo, definition, later, episode(tx))
        assert paused.value.code == "stale_occurrence_claim"
        assert dict(paused.value.metadata)["terminal_code"] == "cancelled_job_paused"
        assert len(bindings(tx, definition.job_id)) == 1


def test_concurrent_admission_two_workers_one_allowance_one_admission(database, repo, plane):
    with database.transaction() as tx:
        definition = job(tx, repo)
        repo.put_job_policy(
            tx, policy=policy(definition.job_id, max_runs=1, max_outstanding_episodes=2),
            expected_version=0,
        )
        first = claimed(tx, repo, definition, worker="worker-1")
        second = claimed(tx, repo, definition, worker="worker-2")
        a, b = episode(tx), episode(tx)
    other, pool, connection = second_database(plane)
    results: dict[str, object] = {}
    started = threading.Event()

    def worker_b():
        started.set()
        try:
            with other.transaction() as tx:
                results["b"] = admit(tx, repo, definition, second, b)
        except BaseException as error:
            results["b"] = error

    thread = threading.Thread(target=worker_b)
    try:
        with database.transaction() as tx:
            results["a"] = admit(tx, repo, definition, first, a)
            thread.start()
            started.wait(5)
            time.sleep(0.5)
            assert "b" not in results
        thread.join(10)
    finally:
        pool.close()
        connection.close()
    assert not thread.is_alive()
    assert results["a"].admitted and results["a"].created
    assert not isinstance(results["b"], BaseException), results["b"]
    assert not results["b"].admitted and results["b"].reason == "allowance_exhausted"
    with database.transaction() as tx:
        assert [str(row["assignment_id"]) for row in bindings(tx, definition.job_id)] == [a]
        current = repo.get_job_policy(tx, owner_id=OWNER, job_id=definition.job_id)
        assert (current.admitted_runs, current.max_runs, current.version) == (1, 1, 2)
