"""Exercise owner-scoped operation reads while PostgreSQL callers serialize source transitions.
The public read lock keeps completion subscription registration in the caller's transaction.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from test_assignments_postgres import control, create_operation

from astralplane.api import create_repository_catalog
from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.assignments import digest
from tests.integration.test_lifecycle_acceptance import (
    concurrent_database,
    wait_for_blocked_statement,
)


@pytest.mark.parametrize("first", ("read", "cancel"))
@pytest.mark.postgresql_lock_catalog
def test_locked_source_read_serializes_completion_subscription_and_cancel(migrated_clone, first):
    catalog = create_repository_catalog()
    repo = catalog.assignments
    with migrated_clone.database.transaction() as tx:
        source = create_operation(repo, tx)
    entered = Event()
    state = {}

    def register(tx):
        observed = repo.get_operation(
            tx, owner_id="owner", assignment_id=source.assignment_id, lock=True
        )
        if observed.disposition == "queued":
            catalog.completion_wake.register_subscription(
                tx,
                owner_id="owner",
                waiter_operation_id="10000000-0000-4000-8000-000000000001",
                waiter_owner_id="owner",
                source_operation_id=source.assignment_id,
                source_owner_id="owner",
                terminal_condition="cancelled",
                source_revision=source.state_version,
                current_revision_fence=source.state_version + 1,
                created_at=100,
            )
        return observed

    def competing(database):
        with database.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            if first == "read":
                return control(repo, tx, source, "stop")
            return register(tx)

    with (
        concurrent_database(migrated_clone) as database,
        concurrent_database(migrated_clone) as observation,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        with migrated_clone.database.transaction() as holder:
            blocking_pid = holder.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if first == "read":
                assert register(holder).disposition == "queued"
            else:
                control(repo, holder, source, "stop")
            pending = workers.submit(competing, database)
            assert entered.wait(timeout=30)
            wait_for_blocked_statement(observation, state["pid"], blocking_pid)
        result = pending.result(timeout=30)
    if first == "read":
        assert result.assignment.lifecycle == "stopped"
    else:
        assert result.disposition == "cancelled"
    with migrated_clone.database.transaction() as tx:
        current = repo.get_operation(tx, owner_id="owner", assignment_id=source.assignment_id)
        assert current.disposition == "cancelled"
        row = tx.fetch_one("SELECT count(*) AS count FROM completion_subscription")
        assert row["count"] == (1 if first == "read" else 0)


@pytest.mark.parametrize("options", ({}, {"lock": False}))
def test_default_operation_read_does_not_lock_source_mutation(migrated_clone, options):
    repo = create_repository_catalog().assignments
    with migrated_clone.database.transaction() as tx:
        source = create_operation(repo, tx)

    def read(database):
        with database.transaction() as tx:
            return repo.get_operation(
                tx, owner_id="owner", assignment_id=source.assignment_id, **options
            )

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=1) as workers,
        migrated_clone.database.transaction() as holder,
    ):
        control(repo, holder, source, "stop")
        assert workers.submit(read, database).result(timeout=30).disposition == "queued"


def test_locked_operation_read_keeps_owner_predicate(migrated_clone):
    repo = create_repository_catalog().assignments
    with migrated_clone.database.transaction() as tx:
        source = create_operation(repo, tx)
        assert (
            repo.get_operation(
                tx, owner_id="foreign", assignment_id=source.assignment_id, lock=True
            )
            is None
        )


@pytest.mark.parametrize("lock", (0, 1, None, "yes", [], {}))
def test_operation_read_refuses_non_boolean_lock(migrated_clone, lock):
    repo = create_repository_catalog().assignments
    with (
        migrated_clone.database.transaction() as tx,
        pytest.raises(RepositoryValidationError, match="lock must be a boolean"),
    ):
        repo.get_operation(
            tx,
            owner_id="owner",
            assignment_id="10000000-0000-4000-8000-000000000001",
            lock=lock,
        )


@pytest.mark.postgresql_lock_catalog
def test_reciprocal_operation_locking_takes_owner_before_source(migrated_clone):
    repo = create_repository_catalog().assignments
    with migrated_clone.database.transaction() as tx:
        first = create_operation(repo, tx, caller_key="first", command_digest=digest("first"))
        second = create_operation(repo, tx, caller_key="second", command_digest=digest("second"))
    entered = Event()
    source_read = Event()
    state = {}

    def wait_options(target, source):
        return dict(
            owner_id="owner",
            assignment_id=target.assignment_id,
            expected_instruction_revision=target.instruction_revision,
            expected_control_epoch=target.control_epoch,
            expected_state_version=target.state_version,
            submission_id=str(uuid4()),
            submission_digest=digest(("wait", source.assignment_id, target.assignment_id)),
            event_key="completion:" + source.assignment_id,
            source_revision=source.state_version,
        )

    def competing(database):
        with database.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            observed = repo.get_operation(
                tx, owner_id="owner", assignment_id=second.assignment_id, lock=True
            )
            source_read.set()
            result = repo.set_owner_event_wait(tx, **wait_options(first, observed.assignment))
            return observed, result

    with (
        concurrent_database(migrated_clone) as database,
        concurrent_database(migrated_clone) as observation,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        with migrated_clone.database.transaction() as holder:
            blocking_pid = holder.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            observed = repo.get_operation(
                holder, owner_id="owner", assignment_id=first.assignment_id, lock=True
            )
            assert observed.disposition == "queued"
            pending = workers.submit(competing, database)
            assert entered.wait(timeout=30)
            wait_for_blocked_statement(observation, state["pid"], blocking_pid)
            assert not source_read.is_set()
            assert repo.set_owner_event_wait(holder, **wait_options(second, first)).applied
        observed, result = pending.result(timeout=30)
    assert observed.disposition == "awaiting_event" and result.applied
    with migrated_clone.database.transaction() as tx:
        for source, target in ((first, second), (second, first)):
            current = repo.get_operation(tx, owner_id="owner", assignment_id=target.assignment_id)
            assert current.disposition == "awaiting_event"
            assert current.assignment.operation["control"]["wait"]["event_key"] == (
                "completion:" + source.assignment_id
            )


@pytest.mark.parametrize("lock", (False, True))
def test_operation_inspection_remains_available_for_retired_owner(migrated_clone, lock):
    repo = create_repository_catalog().assignments
    with migrated_clone.database.transaction() as tx:
        source = create_operation(repo, tx)
        tx.execute(
            "INSERT INTO astralplane_blob_owner_state(owner_id,state,version,retired_at,updated_at)"
            " VALUES('owner','retired',1,clock_timestamp(),clock_timestamp())"
        )
    with migrated_clone.database.transaction() as tx:
        observed = repo.get_operation(
            tx, owner_id="owner", assignment_id=source.assignment_id, lock=lock
        )
        assert observed.disposition == "queued"
        assert observed.assignment == source
