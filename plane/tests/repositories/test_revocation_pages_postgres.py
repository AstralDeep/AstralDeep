"""Real-PostgreSQL tests for astralplane.repositories.revocations: a held row prefix
never hides later eligible work, deleted cursors don't restart a cycle, and
concurrent enqueues can't extend a captured cycle.
"""

import pytest
from test_assignments_postgres import database as database

from astralplane.repositories import RepositoryDataError, RepositoryNotFoundError
from astralplane.repositories.revocations import RevocationQueueRepository


@pytest.fixture
def tx(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM auth_revocation_queue")
        yield transaction


def enqueue(tx, timestamp, *, bound=True, owner="held-owner"):
    return RevocationQueueRepository().enqueue(
        tx,
        owner_id=owner,
        refresh_token_ciphertext="opaque-private-qualification-only",
        enqueued_at=timestamp,
        client_id="astral-mobile" if bound else None,
        issuing_issuer="https://iam.example.test/realms/Astral" if bound else None,
    )


def test_held_twenty_row_prefix_does_not_hide_later_eligible_work(tx):
    repo = RevocationQueueRepository()
    held = [enqueue(tx, 100) for _ in range(24)]
    eligible = enqueue(tx, 100, bound=False, owner="eligible-owner")
    first = repo.page_for_administration(tx)
    assert first.records == tuple(held[:20])
    assert first.ceiling == eligible.queue_id
    for record in first.records:
        changed = repo.bump_attempt(
            tx, owner_id=record.owner_id, queue_id=record.queue_id, expected_attempts=0
        )
        assert changed.attempts == 1
        assert (changed.issuing_issuer, changed.client_id) == (
            record.issuing_issuer,
            record.client_id,
        )
    second = repo.page_for_administration(tx, after=first.next_cursor, ceiling=first.ceiling)
    assert second.records == (*held[20:], eligible) and second.next_cursor is None
    assert not repo.resolve(tx, owner_id="held-owner", queue_id=eligible.queue_id)
    with pytest.raises(RepositoryNotFoundError):
        repo.bump_attempt(
            tx, owner_id="held-owner", queue_id=eligible.queue_id, expected_attempts=0
        )
    assert repo.resolve(tx, owner_id="eligible-owner", queue_id=eligible.queue_id)
    retry = repo.page_for_administration(tx)
    assert [r.queue_id for r in retry.records] == [r.queue_id for r in held[:20]]
    assert all(r.attempts == 1 for r in retry.records)


@pytest.mark.parametrize("timestamp", [0, 100, 200])
def test_continuous_enqueues_cannot_extend_captured_cycle(tx, timestamp):
    repo = RevocationQueueRepository()
    initial = [enqueue(tx, 100) for _ in range(10)]
    page = repo.page_for_administration(tx, limit=3)
    ceiling = page.ceiling
    visited = []
    newcomers = []
    for _ in range(4):
        visited.extend(r.queue_id for r in page.records)
        newcomers.extend(enqueue(tx, timestamp) for _ in range(4))
        if page.next_cursor is None:
            break
        page = repo.page_for_administration(tx, limit=3, after=page.next_cursor, ceiling=ceiling)
        assert page.ceiling == ceiling
    assert page.next_cursor is None
    assert visited == [r.queue_id for r in initial]
    restart = repo.page_for_administration(tx, limit=200)
    expected = sorted((*initial, *newcomers), key=lambda r: (r.enqueued_at, r.queue_id))
    assert restart.records == tuple(expected) and restart.next_cursor is None
    assert restart.ceiling == newcomers[-1].queue_id


def test_deleted_cursor_and_ceiling_do_not_restart_or_relabel_a_cycle(tx):
    repo = RevocationQueueRepository()
    records = [enqueue(tx, time) for time in [300, 100, 100, 200, 250]]
    first = repo.page_for_administration(tx, limit=2)
    assert [r.queue_id for r in first.records] == [records[1].queue_id, records[2].queue_id]
    assert repo.resolve(tx, owner_id="held-owner", queue_id=first.next_cursor.queue_id)
    assert repo.resolve(tx, owner_id="held-owner", queue_id=first.ceiling)
    newer = enqueue(tx, 250)
    final = repo.page_for_administration(
        tx, limit=20, after=first.next_cursor, ceiling=first.ceiling
    )
    assert final.records == (records[3], records[0])
    assert final.ceiling == first.ceiling and final.next_cursor is None
    assert newer.queue_id not in {r.queue_id for r in final.records}
    for record in final.records:
        assert repo.resolve(tx, owner_id=record.owner_id, queue_id=record.queue_id)
    empty = repo.page_for_administration(tx, after=first.next_cursor, ceiling=first.ceiling)
    assert empty.records == () and empty.next_cursor is None
    assert empty.ceiling == first.ceiling


def test_empty_cycle_replay_and_legacy_peek_are_read_only(tx):
    repo = RevocationQueueRepository()
    assert repo.page_for_administration(tx).ceiling is None
    rows = [enqueue(tx, i, bound=bool(i % 2)) for i in range(5)]
    first = repo.page_for_administration(tx, limit=2)
    assert first.records == repo.pending_for_administration(tx, limit=2)
    arguments = {"after": first.next_cursor, "ceiling": first.ceiling, "limit": 2}
    assert repo.page_for_administration(tx, **arguments) == repo.page_for_administration(
        tx, **arguments
    )
    assert repo.pending_for_administration(tx, limit=200) == tuple(rows)
    assert "opaque-private-qualification-only" not in repr(first)


def test_read_page_does_not_change_rollback_or_attempt_fences(tx):
    repo = RevocationQueueRepository()
    original = enqueue(tx, 0)
    with pytest.raises(RuntimeError, match="interrupt"), tx.savepoint("queue_page_rollback"):
        assert repo.page_for_administration(tx).records == (original,)
        assert repo.resolve(tx, owner_id=original.owner_id, queue_id=original.queue_id)
        raise RuntimeError("interrupt")
    assert repo.page_for_administration(tx).records == (original,)
    changed = repo.bump_attempt(
        tx, owner_id=original.owner_id, queue_id=original.queue_id, expected_attempts=0
    )
    with pytest.raises(RepositoryNotFoundError):
        repo.bump_attempt(
            tx, owner_id=original.owner_id, queue_id=original.queue_id, expected_attempts=0
        )
    assert repo.page_for_administration(tx).records == (changed,)


def test_corrupt_negative_timestamp_never_becomes_a_page_cursor(tx):
    original = enqueue(tx, 0)
    tx.execute("UPDATE auth_revocation_queue SET enqueued_at=-1 WHERE id=%s", (original.queue_id,))
    with pytest.raises(RepositoryDataError, match="page is invalid"):
        RevocationQueueRepository().page_for_administration(tx)


def test_committed_changes_between_pages_keep_original_cycle(database):
    repo = RevocationQueueRepository()
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM auth_revocation_queue")
        original = [enqueue(transaction, 100) for _ in range(3)]
        first = repo.page_for_administration(transaction, limit=1)
    with database.transaction() as transaction:
        later = enqueue(transaction, 0)
        assert repo.resolve(
            transaction, owner_id=original[0].owner_id, queue_id=original[0].queue_id
        )
    with database.transaction() as transaction:
        final = repo.page_for_administration(
            transaction, after=first.next_cursor, ceiling=first.ceiling
        )
        assert final.records == tuple(original[1:]) and final.next_cursor is None
        restart = repo.page_for_administration(transaction)
        assert restart.records == (later, *original[1:])
