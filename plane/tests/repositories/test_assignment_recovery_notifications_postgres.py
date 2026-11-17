"""Checks detached terminal recovery records and owner serialization on PostgreSQL.
Builds on the assignment fixtures used by host completion delivery.
"""

from datetime import timedelta

import pytest
from test_assignments_postgres import create_operation, independent_database, session_observation
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_terminal_postgres import current, expire_authority

from astralplane.repositories.assignment_models import (
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
)
from astralplane.repositories.assignments import digest


class ObservedTransaction:
    def __init__(self, transaction):
        self.transaction = transaction
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.transaction, name)

    def fetch_one(self, statement, parameters=()):
        if "pg_try_advisory_xact_lock" in statement:
            self.calls.append(("owner", parameters[0]))
        return self.transaction.fetch_one(statement, parameters)

    def fetch_all(self, statement, parameters=()):
        if (
            statement.startswith("SELECT id,owner_user_id FROM persistent_assignment ")
            and "FOR UPDATE" in statement
        ):
            self.calls.append(("rows", None))
        return self.transaction.fetch_all(statement, parameters)


def expired_owner_operation(repo, transaction, owner_id, index):
    observed = session_observation(
        transaction, owner_id=owner_id, session_id="recovery-" + owner_id
    )
    now = transaction.fetch_one("SELECT clock_timestamp() AS now")["now"]
    assignment_id = f"00000000-0000-4000-8000-{index:012d}"
    record = create_operation(
        repo, transaction, owner_id=owner_id, authority=observed,
        assignment_id=assignment_id, caller_key=assignment_id,
        command_digest=digest(assignment_id),
        operation=AssignmentOperationSpec(
            kind="chat", deadline_at=now + timedelta(minutes=5), source_retention="none",
            authority=AssignmentOperationAuthority(
                owner_id=owner_id, origin="interactive", reference_kind="session_incarnation",
                reference_id=observed.credential.incarnation_id,
                expires_at=now + timedelta(minutes=10),
            ),
        ),
    )
    expire_authority(transaction, record, "deadline")
    return record


def test_terminal_recovery_returns_exact_saved_record(tx, repo):
    source = create_operation(repo, tx)
    expire_authority(tx, source, "deadline")
    result = repo.recover_expired_operations_for_administration(tx)
    saved = current(repo, tx, source)
    assert result.terminal_assignments == (saved,)
    assert saved.lifecycle == "completed"
    assert saved.operation["terminal_outcome"] == "failed"
    with pytest.raises(TypeError):
        saved.operation["terminal_outcome"] = "completed"
    assert repo.recover_expired_operations_for_administration(tx).terminal_assignments == ()


def test_recovery_skips_locked_owner_before_changing_assignment(database, repo):
    with database.transaction() as setup:
        setup.execute("DELETE FROM assignment_operation_receipt")
        setup.execute("DELETE FROM persistent_assignment")
        setup.execute("DELETE FROM astralplane_blob_owner_state WHERE owner_id='owner'")
        source = create_operation(repo, setup)
        expire_authority(setup, source, "deadline")
        schema = setup.fetch_one("SELECT current_schema() AS name")["name"]
    try:
        with database.transaction() as owner:
            owner.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", ("owner",))
            with independent_database(schema) as other, other.transaction() as recovery:
                before = current(repo, recovery, source)
                result = repo.recover_expired_operations_for_administration(recovery)
                assert result.reclaimed_assignment_ids == ()
                assert current(repo, recovery, source) == before
        with database.transaction() as recovery:
            result = repo.recover_expired_operations_for_administration(recovery)
            assert result.reclaimed_assignment_ids == (source.assignment_id,)
            assert result.terminal_assignments == (current(repo, recovery, source),)
    finally:
        with database.transaction() as cleanup:
            cleanup.execute("DELETE FROM assignment_operation_receipt")
            cleanup.execute("DELETE FROM persistent_assignment")


@pytest.mark.parametrize("blocked_count", [1, 3])
def test_locked_owner_prefix_does_not_hide_an_available_owner(database, repo, blocked_count):
    with database.transaction() as setup:
        setup.execute("DELETE FROM assignment_operation_receipt")
        setup.execute("DELETE FROM persistent_assignment")
        for index in range(1, blocked_count + 1):
            expired_owner_operation(repo, setup, "held-owner", index)
        available = expired_owner_operation(repo, setup, "available-owner", blocked_count + 1)
        schema = setup.fetch_one("SELECT current_schema() AS name")["name"]
    with database.transaction() as held:
        held.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", ("held-owner",))
        with independent_database(schema) as other, other.transaction() as recovery:
            observed = ObservedTransaction(recovery)
            result = repo.recover_expired_operations_for_administration(observed, limit=1)
            if recovery.dialect.is_sqlite:
                assert result.reclaimed_assignment_ids == ()
                assert repo.get_assignment(
                    recovery, owner_id="available-owner", assignment_id=available.assignment_id
                ).lifecycle == "active"
            else:
                assert result.reclaimed_assignment_ids == (available.assignment_id,)
                assert [identity for kind, identity in observed.calls if kind == "owner"] == [
                    "held-owner",
                    "available-owner",
                ]
                assert observed.calls[-1] == ("rows", None)
                assert sum(kind == "rows" for kind, _ in observed.calls) == 1
    if recovery.dialect.is_sqlite:
        with database.transaction() as retry:
            result = repo.recover_expired_operations_for_administration(
                retry, limit=blocked_count + 1
            )
            assert available.assignment_id in result.reclaimed_assignment_ids
            assert len(result.reclaimed_assignment_ids) == blocked_count + 1


def test_retired_owner_prefix_is_preserved_while_another_owner_recovers(tx, repo):
    retired = expired_owner_operation(repo, tx, "owner", 1)
    expire_authority(tx, retired, "retired")
    available = expired_owner_operation(repo, tx, "available-owner", 2)
    before = current(repo, tx, retired)

    result = repo.recover_expired_operations_for_administration(tx, limit=1)

    assert result.reclaimed_assignment_ids == (available.assignment_id,)
    assert current(repo, tx, retired) == before


def test_recovery_bounds_distinct_owner_attempts(database, repo):
    with database.transaction() as setup:
        setup.execute("DELETE FROM assignment_operation_receipt")
        setup.execute("DELETE FROM persistent_assignment")
        for index in range(1, 102):
            expired_owner_operation(repo, setup, f"budget-owner-{index:03d}", index)
        schema = setup.fetch_one("SELECT current_schema() AS name")["name"]
    with database.transaction() as held:
        for index in range(1, 101):
            held.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,79))",
                (f"budget-owner-{index:03d}",),
            )
        with independent_database(schema) as other, other.transaction() as recovery:
            observed = ObservedTransaction(recovery)
            result = repo.recover_expired_operations_for_administration(observed, limit=1)
            attempts = [identity for kind, identity in observed.calls if kind == "owner"]
            assert len(attempts) == len(set(attempts)) == 100
            assert result.reclaimed_assignment_ids == ()
            assert not any(kind == "rows" for kind, _ in observed.calls)
            assert recovery.fetch_one(
                "SELECT COUNT(*) AS active FROM persistent_assignment WHERE lifecycle='active'"
            )["active"] == 101
