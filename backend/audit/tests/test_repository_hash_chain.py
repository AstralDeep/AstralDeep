"""Hash-chain integrity tests for ``AuditRepository``.

These tests integrate against an isolated PostgreSQL database initialized by
AstralPlane; each test uses a unique ``actor_user_id`` so the chain is isolated.
"""
from __future__ import annotations

import threading


def test_genesis_row_uses_zero_prev_hash(repo, make_event, unique_user, database):
    ev = make_event(actor_user_id=unique_user, auth_principal=unique_user)
    dto = repo.insert(ev)
    assert dto.event_id

    # The internal column should equal 32 zero bytes
    with database.transaction() as transaction:
        row = transaction.fetch_one(
            "SELECT prev_hash FROM audit_events WHERE event_id = %s",
            (dto.event_id,),
        )
    assert bytes(row["prev_hash"]) == bytes(32)


def test_second_row_links_to_genesis(repo, make_event, unique_user, database):
    a = repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, action_type="auth.first"))
    b = repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, action_type="auth.second"))

    with database.transaction() as transaction:
        row_a = transaction.fetch_one(
            "SELECT entry_hash FROM audit_events WHERE event_id = %s",
            (a.event_id,),
        )
        row_b = transaction.fetch_one(
            "SELECT prev_hash FROM audit_events WHERE event_id = %s",
            (b.event_id,),
        )
    assert bytes(row_b["prev_hash"]) == bytes(row_a["entry_hash"])


def test_verify_chain_returns_none_for_clean_log(repo, make_event, unique_user):
    for i in range(3):
        repo.insert(make_event(
            actor_user_id=unique_user, auth_principal=unique_user,
            action_type=f"auth.attempt_{i}",
        ))
    assert repo.verify_chain(unique_user) is None


def test_verify_chain_flags_tampered_row(repo, make_event, unique_user, database):
    repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, action_type="auth.first"))
    target = repo.insert(make_event(
        actor_user_id=unique_user, auth_principal=unique_user, action_type="auth.target",
    ))
    repo.insert(make_event(actor_user_id=unique_user, auth_principal=unique_user, action_type="auth.third"))

    # Mutate the description directly (bypass the trigger via the GUC)
    with database.transaction() as transaction:
        transaction.execute("SET LOCAL audit.allow_purge = 'true'")
        transaction.execute(
            "UPDATE audit_events SET description = %s WHERE event_id = %s",
            ("tampered description", target.event_id),
        )

    bad_id = repo.verify_chain(unique_user)
    assert bad_id == target.event_id


def test_concurrent_inserts_serialize_through_for_update(repo, make_event, unique_user):
    """Two threads inserting for the same user must produce a linear chain."""
    barrier = threading.Barrier(2)
    results = []

    def insert_one(action: str) -> None:
        barrier.wait()
        results.append(repo.insert(make_event(
            actor_user_id=unique_user, auth_principal=unique_user, action_type=action,
        )))

    t1 = threading.Thread(target=insert_one, args=("auth.t1",))
    t2 = threading.Thread(target=insert_one, args=("auth.t2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    # Chain integrity: verify the user's chain is still well-formed
    assert repo.verify_chain(unique_user) is None
