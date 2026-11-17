"""Exercise completion receipt acceptance against PostgreSQL subscription and receipt locks.
Exact blocked process identities make revocation ordering and duplicate claims deterministic.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from astralplane.api import create_repository_catalog
from astralplane.repositories import RepositoryConflictError
from tests.integration.test_lifecycle_acceptance import (
    concurrent_database,
    wait_for_blocked_statement,
)


def test_completion_accept_rechecks_revocation_after_unique_wait(migrated_clone):
    repo = create_repository_catalog().completion_wake
    with migrated_clone.database.transaction() as tx:
        subscription = repo.register_subscription(
            tx,
            owner_id="review-owner",
            waiter_operation_id="10000000-0000-4000-8000-000000000001",
            waiter_owner_id="review-owner",
            source_operation_id="10000000-0000-4000-8000-000000000002",
            source_owner_id="review-owner",
            terminal_condition="completed",
            source_revision=3,
            current_revision_fence=5,
            created_at=100,
        )
    entered = Event()
    state = {}
    receipt_args = dict(
        owner_id="review-owner",
        subscription_id=subscription.subscription_id,
        idempotency_key="review-key",
        observed_terminal="completed",
        observed_revision=4,
        accepted_at=200,
    )

    def competing(database):
        try:
            with database.transaction() as tx:
                state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
                entered.set()
                return repo.accept_wake_receipt(tx, **receipt_args)
        except RepositoryConflictError:
            return None

    with (
        concurrent_database(migrated_clone) as database,
        concurrent_database(migrated_clone) as observation,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        with (
            pytest.raises(RuntimeError, match="discard blocker"),
            migrated_clone.database.transaction() as blocker,
        ):
            blocking_pid = blocker.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            blocker.execute(
                "INSERT INTO wake_receipt (receipt_id, subscription_id, owner_id,"
                " idempotency_key, observed_terminal, observed_revision, accepted_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    "10000000-0000-4000-8000-000000000003",
                    subscription.subscription_id,
                    "review-owner",
                    "review-key",
                    "completed",
                    4,
                    200,
                ),
            )
            pending = workers.submit(competing, database)
            assert entered.wait(timeout=30)
            wait_for_blocked_statement(observation, state["pid"], blocking_pid)
            with observation.transaction() as tx:
                revoked = repo.revoke_subscription(
                    tx,
                    owner_id="review-owner",
                    subscription_id=subscription.subscription_id,
                    revoked_at=300,
                )
                assert revoked.revoked_at == 300
            raise RuntimeError("discard blocker")
        accepted = pending.result(timeout=30)
    with migrated_clone.database.transaction() as tx:
        stored = tx.fetch_one(
            "SELECT revoked_at FROM completion_subscription WHERE subscription_id=%s",
            (subscription.subscription_id,),
        )
        assert stored["revoked_at"] == 300
        count = tx.fetch_one("SELECT count(*) AS count FROM wake_receipt")["count"]
    assert accepted is None and count == 0


@pytest.mark.parametrize("first", ("revoke", "accept"))
def test_completion_accept_serializes_with_subscription_revoke(migrated_clone, first):
    repo = create_repository_catalog().completion_wake
    with migrated_clone.database.transaction() as tx:
        subscription = repo.register_subscription(
            tx,
            owner_id="race-owner",
            waiter_operation_id="10000000-0000-4000-8000-000000000001",
            waiter_owner_id="race-owner",
            source_operation_id="10000000-0000-4000-8000-000000000002",
            source_owner_id="race-owner",
            terminal_condition="completed",
            source_revision=3,
            current_revision_fence=5,
            created_at=100,
        )
    entered = Event()
    state = {}
    acceptance = dict(
        owner_id="race-owner",
        subscription_id=subscription.subscription_id,
        idempotency_key="race-key",
        observed_terminal="completed",
        observed_revision=4,
        accepted_at=200,
    )
    revocation = dict(
        owner_id="race-owner", subscription_id=subscription.subscription_id, revoked_at=300
    )

    def competing(database):
        try:
            with database.transaction() as tx:
                state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
                entered.set()
                if first == "revoke":
                    return repo.accept_wake_receipt(tx, **acceptance)
                return repo.revoke_subscription(tx, **revocation)
        except RepositoryConflictError:
            return None

    with (
        concurrent_database(migrated_clone) as database,
        concurrent_database(migrated_clone) as observation,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        with migrated_clone.database.transaction() as blocker:
            blocking_pid = blocker.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            if first == "revoke":
                repo.revoke_subscription(blocker, **revocation)
            else:
                accepted = repo.accept_wake_receipt(blocker, **acceptance)
            pending = workers.submit(competing, database)
            assert entered.wait(timeout=30)
            wait_for_blocked_statement(observation, state["pid"], blocking_pid)
        result = pending.result(timeout=30)
    if first == "revoke":
        assert result is None
    else:
        assert result.revoked_at == 300
    with migrated_clone.database.transaction() as tx:
        stored = tx.fetch_one(
            "SELECT revoked_at FROM completion_subscription WHERE subscription_id=%s",
            (subscription.subscription_id,),
        )
        assert stored["revoked_at"] == 300
        rows = tx.fetch_all("SELECT receipt_id FROM wake_receipt")
        assert tuple(row["receipt_id"] for row in rows) == (
            () if first == "revoke" else (accepted.receipt_id,)
        )
