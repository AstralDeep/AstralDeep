"""Real-PostgreSQL tests for astralplane.repositories.assignments, guidance, and
history: a selected-input cutoff is bound by one final database-clock observation
taken after any owner-lock wait, not the caller's own clock.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from threading import Event

import pytest
from test_assignment_execution_guard_postgres import _wait_for_lock
from test_assignments_postgres import create_operation, independent_database, session_observation
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_guidance_storage_postgres import during_owner_wait
from test_selected_input_postgres import bind_selected, counters, envelope, selected_agent, snapshot

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.history import SessionExecutionObservation, SessionRepository


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("compare", [False, True])
@pytest.mark.postgresql_lock_catalog
def test_original_cutoff_crossed_while_final_selected_query_waits(
    database, repo, selected, compare
):
    with database.transaction() as tx:
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        record = create_operation(repo, tx)
        if selected:
            _, agent = selected_agent(tx)
            record = bind_selected(repo, tx, record, envelope(agent))
        expected = snapshot(repo, tx, record)
        schema = tx.fetch_one("SELECT current_schema() AS s")["s"]
    ready, state = Event(), {}

    def read():
        with independent_database(schema) as db, db.transaction() as tx:
            observed = session_observation(tx)
            original = SessionExecutionObservation(
                observed.credential, observed.started_at, observed.started_at + timedelta(seconds=1)
            )
            SessionRepository().assert_current_execution(tx, observation=original)
            state.update(
                pid=tx.fetch_one("SELECT pg_backend_pid() AS p")["p"], cutoff=original.valid_until
            )
            ready.set()
            args = counters(record)
            args["authority_valid_until"] = original.valid_until
            if compare:
                return repo.assert_selected_input_current(tx, **args, expected=expected)
            return repo.assert_guidance_current(tx, **args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as tx:
            blocker = tx.fetch_one("SELECT pg_backend_pid() AS p")["p"]
            tx.execute("LOCK TABLE assignment_selected_agent IN ACCESS EXCLUSIVE MODE")
            future = pool.submit(read)
            assert ready.wait(5)
            _wait_for_lock(tx, state["pid"], blocker)
            tx.execute("SELECT pg_sleep(1.1)")
            assert tx.fetch_one("SELECT clock_timestamp() AS now")["now"] >= state["cutoff"]
        with pytest.raises(RepositoryConflictError, match="guidance_changed"):
            future.result(5)


@pytest.mark.parametrize("method", ["assert_guidance_current", "assert_selected_input_current"])
@pytest.mark.parametrize("cutoff", [True, 1, 1.0, "tomorrow", datetime(2026, 1, 1), object()])
def test_invalid_cutoff_is_refused_before_any_database_wait(repo, method, cutoff):
    args = dict(
        owner_id="owner",
        assignment_id="unused",
        expected_instruction_revision=1,
        expected_control_epoch=1,
        expected_state_version=1,
        authority_valid_until=cutoff,
    )
    if method == "assert_selected_input_current":
        args["expected"] = None
    with pytest.raises(RepositoryValidationError):
        getattr(repo, method)(object(), **args)


def test_cutoff_normalizes_offset_and_accepts_completion_counters(tx, repo):
    from test_assignments_postgres import control

    record = create_operation(repo, tx)
    record = control(repo, tx, record, "stop").assignment
    now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    offset = (now + timedelta(seconds=10)).astimezone(timezone(timedelta(hours=3)))
    assert (
        repo.assert_guidance_current(tx, **counters(record), authority_valid_until=offset) == record
    )
    with pytest.raises(RepositoryConflictError):
        repo.assert_guidance_current(tx, **counters(record), authority_valid_until=now)


@pytest.mark.parametrize("database_timezone", ["UTC", "America/New_York"])
@pytest.mark.postgresql_lock_catalog
def test_custom_timezone_is_detached_before_owner_lock(database, repo, database_timezone):
    class MutableZone(tzinfo):
        offset = timedelta(0)

        def utcoffset(self, dt):
            return self.offset

        def dst(self, dt):
            return timedelta(0)

    with database.transaction() as tx:
        tx.execute("SET LOCAL TIME ZONE %s", (database_timezone,))
        tx.execute("DELETE FROM assignment_operation_receipt")
        tx.execute("DELETE FROM persistent_assignment")
        record = create_operation(repo, tx)
        now = tx.fetch_one("SELECT clock_timestamp() AS now")["now"]
    zone = MutableZone()
    cutoff = (now.astimezone(UTC) + timedelta(hours=1)).replace(tzinfo=zone)
    result = during_owner_wait(
        database,
        "owner",
        lambda tx: repo.assert_guidance_current(
            tx, **counters(record), authority_valid_until=cutoff
        ),
        lambda: setattr(zone, "offset", timedelta(hours=2)),
    )
    assert result == record


@pytest.mark.parametrize("compare", [False, True])
@pytest.mark.postgresql_lock_catalog
def test_same_final_clock_refuses_elapsed_note_with_live_original_cutoff(
    tx, repo, monkeypatch, compare
):
    from test_guidance_storage_postgres import note

    from astralplane.repositories import assignments, guidance
    from astralplane.repositories.guidance_models import GuidanceReference

    n = note(tx, expires_at=guidance._clock(tx) + 1500)
    record = bind_selected(
        repo,
        tx,
        create_operation(repo, tx),
        envelope(refs=(GuidanceReference("note", n.note_id, 1),)),
    )
    expected = snapshot(repo, tx, record)
    args = {
        **counters(record),
        "authority_valid_until": assignments._now(tx) + timedelta(seconds=10),
    }
    method = repo.assert_selected_input_current if compare else repo.assert_guidance_current
    if compare:
        args["expected"] = expected
    assert method(tx, **args) == record
    original_now = assignments._now

    def delayed_clock(transaction):
        transaction.execute("SELECT pg_sleep(1.6)")
        return original_now(transaction)

    monkeypatch.setattr(assignments, "_now", delayed_clock)
    with pytest.raises(RepositoryConflictError, match="guidance_changed"):
        method(tx, **args)


def test_datetime_subclass_is_not_a_cutoff(repo):
    class DateSubclass(datetime):
        pass

    with pytest.raises(RepositoryValidationError):
        repo.assert_guidance_current(
            object(),
            owner_id="owner",
            assignment_id="unused",
            expected_instruction_revision=1,
            expected_control_epoch=1,
            expected_state_version=1,
            authority_valid_until=DateSubclass(2026, 1, 1, tzinfo=UTC),
        )
