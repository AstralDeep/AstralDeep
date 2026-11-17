"""Tests completion subscriptions and wake receipts against a real migrated PostgreSQL schema.
The assignment test fixtures provide caller-owned transactions for denial, replay and rollback.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_assignments_postgres import database as database

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.completion_wake import (
    accept_wake_receipt,
    delete_subscription,
    register_subscription,
    replay_wake_receipt,
    revoke_subscription,
)


@pytest.fixture()
def tx(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM wake_receipt")
        transaction.execute("DELETE FROM completion_subscription")
        yield transaction


def uid():
    return str(uuid.uuid4())


def sub_kwargs(**over):
    args = dict(
        owner_id="owner-1",
        waiter_operation_id=uid(),
        waiter_owner_id="owner-1",
        source_operation_id=uid(),
        source_owner_id="owner-2",
        terminal_condition="completed",
        source_revision=3,
        current_revision_fence=5,
        created_at=100,
    )
    args.update(over)
    return args


def test_register_and_accept_roundtrip(tx):
    sub = register_subscription(tx, **sub_kwargs())
    assert sub.revoked_at is None
    first = accept_wake_receipt(
        tx,
        owner_id="owner-1",
        subscription_id=sub.subscription_id,
        idempotency_key="k1",
        observed_terminal="completed",
        observed_revision=4,
        accepted_at=200,
    )
    duplicate = accept_wake_receipt(
        tx,
        owner_id="owner-1",
        subscription_id=sub.subscription_id,
        idempotency_key="k1",
        observed_terminal="completed",
        observed_revision=4,
        accepted_at=201,
    )
    assert duplicate.receipt_id == first.receipt_id
    assert duplicate.replay_of is None


def test_any_terminal_covers_all_terminals(tx):
    sub = register_subscription(tx, **sub_kwargs(terminal_condition="any_terminal"))
    for terminal in ("completed", "failed", "cancelled"):
        receipt = accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=sub.subscription_id,
            idempotency_key=f"k-{terminal}",
            observed_terminal=terminal,
            observed_revision=3,
            accepted_at=200,
        )
        assert receipt.observed_terminal == terminal


def test_uncovered_terminal_refused(tx):
    sub = register_subscription(tx, **sub_kwargs(terminal_condition="completed"))
    with pytest.raises(RepositoryConflictError):
        accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=sub.subscription_id,
            idempotency_key="k",
            observed_terminal="failed",
            observed_revision=3,
            accepted_at=200,
        )


def test_revision_fence_enforced(tx):
    sub = register_subscription(tx, **sub_kwargs(source_revision=3, current_revision_fence=5))
    with pytest.raises(RepositoryConflictError):
        accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=sub.subscription_id,
            idempotency_key="low",
            observed_terminal="completed",
            observed_revision=2,
            accepted_at=200,
        )
    with pytest.raises(RepositoryConflictError):
        accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=sub.subscription_id,
            idempotency_key="high",
            observed_terminal="completed",
            observed_revision=6,
            accepted_at=200,
        )
    ok = accept_wake_receipt(
        tx,
        owner_id="owner-1",
        subscription_id=sub.subscription_id,
        idempotency_key="edge",
        observed_terminal="completed",
        observed_revision=5,
        accepted_at=200,
    )
    assert ok.observed_revision == 5


def test_self_subscription_refused(tx):
    op = uid()
    with pytest.raises(RepositoryValidationError):
        register_subscription(
            tx,
            **sub_kwargs(
                waiter_operation_id=op,
                waiter_owner_id="o",
                source_operation_id=op,
                source_owner_id="o",
            ),
        )
    with pytest.raises(RepositoryValidationError):
        register_subscription(tx, **sub_kwargs(source_revision=9, current_revision_fence=3))
    with pytest.raises(RepositoryValidationError):
        register_subscription(tx, **sub_kwargs(terminal_condition="someday"))


def test_malformed_fields_refused(tx):
    with pytest.raises(RepositoryValidationError):
        register_subscription(tx, **sub_kwargs(owner_id=""))
    with pytest.raises(RepositoryValidationError):
        register_subscription(tx, **sub_kwargs(source_operation_id="not-a-uuid"))
    with pytest.raises(RepositoryValidationError):
        accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=uid(),
            idempotency_key="",
            observed_terminal="completed",
            observed_revision=1,
            accepted_at=1,
        )


def test_revoked_subscription_refuses_receipts(tx):
    sub = register_subscription(tx, **sub_kwargs())
    revoke_subscription(tx, owner_id="owner-1", subscription_id=sub.subscription_id, revoked_at=150)
    with pytest.raises(RepositoryConflictError):
        accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=sub.subscription_id,
            idempotency_key="k",
            observed_terminal="completed",
            observed_revision=3,
            accepted_at=200,
        )
    with pytest.raises(RepositoryConflictError):
        revoke_subscription(
            tx, owner_id="owner-1", subscription_id=sub.subscription_id, revoked_at=160
        )
    fresh = register_subscription(tx, **sub_kwargs())
    with pytest.raises(RepositoryDataError):
        revoke_subscription(
            tx,
            owner_id="owner-1",
            subscription_id=fresh.subscription_id,
            revoked_at=50,
        )
    with pytest.raises(RepositoryNotFoundError):
        revoke_subscription(
            tx, owner_id="intruder", subscription_id=sub.subscription_id, revoked_at=160
        )


def test_cross_owner_isolation(tx):
    sub = register_subscription(tx, **sub_kwargs())
    with pytest.raises(RepositoryNotFoundError):
        accept_wake_receipt(
            tx,
            owner_id="intruder",
            subscription_id=sub.subscription_id,
            idempotency_key="k",
            observed_terminal="completed",
            observed_revision=3,
            accepted_at=200,
        )
    with pytest.raises(RepositoryNotFoundError):
        delete_subscription(tx, owner_id="intruder", subscription_id=sub.subscription_id)


def test_delete_cascades_receipts(tx):
    sub = register_subscription(tx, **sub_kwargs())
    accept_wake_receipt(
        tx,
        owner_id="owner-1",
        subscription_id=sub.subscription_id,
        idempotency_key="k",
        observed_terminal="completed",
        observed_revision=3,
        accepted_at=200,
    )
    delete_subscription(tx, owner_id="owner-1", subscription_id=sub.subscription_id)
    row = tx.fetch_one(
        "SELECT count(*) AS n FROM wake_receipt WHERE subscription_id=%s", (sub.subscription_id,)
    )
    assert row["n"] == 0
    with pytest.raises(RepositoryNotFoundError):
        accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=sub.subscription_id,
            idempotency_key="k2",
            observed_terminal="completed",
            observed_revision=3,
            accepted_at=201,
        )


def test_replay_points_at_original(tx):
    sub = register_subscription(tx, **sub_kwargs())
    original = accept_wake_receipt(
        tx,
        owner_id="owner-1",
        subscription_id=sub.subscription_id,
        idempotency_key="first",
        observed_terminal="completed",
        observed_revision=3,
        accepted_at=200,
    )
    replay = replay_wake_receipt(
        tx,
        owner_id="owner-1",
        receipt_id=original.receipt_id,
        idempotency_key="replay-1",
        accepted_at=300,
    )
    assert replay.replay_of == original.receipt_id
    assert replay.receipt_id != original.receipt_id
    again = replay_wake_receipt(
        tx,
        owner_id="owner-1",
        receipt_id=original.receipt_id,
        idempotency_key="replay-1",
        accepted_at=301,
    )
    assert again.receipt_id == replay.receipt_id
    with pytest.raises(RepositoryNotFoundError):
        replay_wake_receipt(
            tx, owner_id="owner-1", receipt_id=uid(), idempotency_key="x", accepted_at=300
        )


def test_registration_race_resolves_to_one_row(database):
    from test_assignments_postgres import independent_database as _ind

    with database.transaction() as setup:
        setup.execute("DELETE FROM wake_receipt")
        setup.execute("DELETE FROM completion_subscription")
        sub_id = register_subscription(setup, **sub_kwargs()).subscription_id
        schema = setup.fetch_one("SELECT current_schema() AS name")["name"]

    def attempt(key):
        with _ind(schema) as db:
            try:
                with db.transaction() as inner:
                    accept_wake_receipt(
                        inner,
                        owner_id="owner-1",
                        subscription_id=sub_id,
                        idempotency_key=key,
                        observed_terminal="completed",
                        observed_revision=3,
                        accepted_at=200,
                    )
                return "accepted"
            except Exception:
                return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, ["race"] * 4))
    assert results.count("accepted") >= 1
    with database.transaction() as check:
        row = check.fetch_one(
            "SELECT count(*) AS n FROM wake_receipt WHERE subscription_id=%s", (sub_id,)
        )
        assert row["n"] == 1


def test_restart_keeps_receipts(database):
    with database.transaction() as first:
        sub = register_subscription(first, **sub_kwargs())
        accept_wake_receipt(
            first,
            owner_id="owner-1",
            subscription_id=sub.subscription_id,
            idempotency_key="k",
            observed_terminal="completed",
            observed_revision=3,
            accepted_at=200,
        )
        saved = sub.subscription_id
    with database.transaction() as second:
        again = accept_wake_receipt(
            second,
            owner_id="owner-1",
            subscription_id=saved,
            idempotency_key="k",
            observed_terminal="completed",
            observed_revision=3,
            accepted_at=900,
        )
        assert again.accepted_at == 200
