"""Tests tracked-job selection and polling against the real owner-serialized Plane contract.
The assignment fixtures provide independent caller-owned PostgreSQL transactions.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, uid
from test_secrets_lock_postgres import wait_for_lock

from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from astralplane.repositories.tracked_jobs import TrackedJobRecord, TrackedJobRepository


def seed(database):
    owner = uid()
    record = TrackedJobRecord(tracked_job_id=uid(), owner_id=owner, machine_id=uid(),
                              scheduler_job_id="42", created_at=100)
    with database.transaction() as tx:
        original = TrackedJobRepository().create(tx, record)
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    return original, schema


def poll(repository, tx, record):
    return repository.apply_poll(
        tx, owner_id=record.owner_id, tracked_job_id=record.tracked_job_id,
        expected_fail_count=0, expected_last_polled_at=None, state="COMPLETED",
        exit_code="0:0", terminal=True, fail_count=0, polled_at=101)


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.postgresql_lock_catalog
def test_locked_job_selection_serializes_terminal_poll_and_caller_rollback(database, rollback):
    record, schema = seed(database)
    repository = TrackedJobRepository()
    entered, identities = Event(), {}

    def writer():
        with independent_database(schema) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout='5s'")
            identities["writer"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            return poll(repository, tx, record)

    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            with database.transaction() as tx:
                assert repository.get(tx, owner_id=record.owner_id,
                                      tracked_job_id=record.tracked_job_id, lock=True) == record
                blocking = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
                future = worker.submit(writer)
                assert entered.wait(3)
                wait_for_lock(tx, identities["writer"], blocking)
                assert not future.done()
                if rollback:
                    raise RuntimeError("intentional caller rollback")
        except RuntimeError as error:
            assert rollback and str(error) == "intentional caller rollback"
        changed = future.result(timeout=5)
    with database.transaction() as tx:
        assert repository.get(tx, owner_id=record.owner_id,
                              tracked_job_id=record.tracked_job_id) == changed
        assert changed.terminal and changed.finished_at == 101


@pytest.mark.parametrize("operation", ["selection", "poll"])
def test_retired_owner_cannot_select_for_wait_or_apply_terminal_poll(database, operation):
    record, _ = seed(database)
    repository = TrackedJobRepository()
    with database.transaction() as tx:
        tx.execute("INSERT INTO astralplane_blob_owner_state(owner_id,state,retired_at) "
                   "VALUES(%s,'retired',clock_timestamp())",
                   (record.owner_id,))
    with (
        pytest.raises(RepositoryConflictError, match="owner_retired"),
        database.transaction() as tx,
    ):
        if operation == "selection":
            repository.get(tx, owner_id=record.owner_id,
                           tracked_job_id=record.tracked_job_id, lock=True)
        else:
            poll(repository, tx, record)
    with database.transaction() as tx:
        assert repository.get(tx, owner_id=record.owner_id,
                              tracked_job_id=record.tracked_job_id) == record


def test_locked_job_read_remains_owner_scoped_and_lock_argument_is_strict(database):
    record, _ = seed(database)
    repository = TrackedJobRepository()
    with database.transaction() as tx:
        assert repository.get(tx, owner_id=uid(), tracked_job_id=record.tracked_job_id,
                              lock=True) is None
        for invalid in (1, "true", None):
            with pytest.raises(RepositoryValidationError):
                repository.get(tx, owner_id=record.owner_id,
                               tracked_job_id=record.tracked_job_id, lock=invalid)
