"""Exercises owner- and chat-scoped queued follow-ups against real PostgreSQL: owner
isolation, revision-fenced mutations, full-permutation reordering, and an atomic head
claim that hands each item to exactly one dispatcher. Caller rollback, replay, and chat
cascade retention are public facade contracts.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from psycopg2.extensions import make_dsn

from astralplane import create_chat_queue_repository
from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import PlaneDatabase
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import ConversationRepository

OWNER = "queue-owner"
OTHER = "queue-other"
CHAT = "queue-chat"
OTHER_CHAT = "queue-other-chat"
ITEM_1 = "10000000-0000-4000-8000-000000000001"
ITEM_2 = "10000000-0000-4000-8000-000000000002"
ITEM_3 = "10000000-0000-4000-8000-000000000003"


def chat(tx, chat_id=CHAT, owner_id=OWNER, created_at=10):
    return ConversationRepository().create(
        tx,
        owner_id=owner_id,
        conversation_id=chat_id,
        title="Synthetic queue chat",
        agent_id=None,
        created_at=created_at,
    )


def enqueue(tx, item_id=ITEM_1, chat_id=CHAT, owner_id=OWNER, text="first", created_at=20):
    return create_chat_queue_repository().enqueue(
        tx,
        owner_id=owner_id,
        chat_id=chat_id,
        text=text,
        created_at=created_at,
        item_id=item_id,
    )


@contextmanager
def concurrent_database(clone):
    if clone.dsn.startswith("sqlite:"):
        from test_assignments_postgres import independent_database

        with independent_database(clone.schema) as database:
            yield database
        return
    configured = make_dsn(clone.dsn, options=f"-csearch_path={clone.schema},pg_catalog")
    pool = ConnectionPool(
        create_postgres_driver_pool(configured, minimum_connections=1, maximum_connections=2)
    )
    try:
        yield PlaneDatabase(pool)
    finally:
        pool.close()


def test_enqueue_replay_and_owner_scoped_reads(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        chat(tx, chat_id=OTHER_CHAT, owner_id=OTHER)
        original = enqueue(tx)
        assert original.position == 0 and original.revision == 1
        assert enqueue(tx) == original
        with pytest.raises(RepositoryConflictError):
            enqueue(tx, created_at=30)
        second = enqueue(tx, item_id=ITEM_2, text="second", created_at=21)
        assert second.position == 1
        assert [item.item_id for item in repo.list(tx, owner_id=OWNER, chat_id=CHAT)] == [
            ITEM_1,
            ITEM_2,
        ]
        assert repo.list(tx, owner_id=OTHER, chat_id=CHAT) == ()
        assert repo.get(tx, owner_id=OTHER, chat_id=CHAT, item_id=ITEM_1) is None
        assert repo.get(tx, owner_id=OWNER, chat_id=OTHER_CHAT, item_id=ITEM_1) is None
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT, item_id=ITEM_3) is None
        with pytest.raises(RepositoryConflictError):
            enqueue(tx, text="different")
        with pytest.raises(RepositoryNotFoundError):
            enqueue(tx, item_id=ITEM_3, chat_id="missing-chat")
        with pytest.raises(RepositoryNotFoundError):
            enqueue(tx, item_id=ITEM_3, chat_id=OTHER_CHAT)
        with pytest.raises(RepositoryValidationError):
            enqueue(tx, item_id=ITEM_3, text="")
        with pytest.raises(RepositoryValidationError):
            enqueue(tx, item_id=ITEM_3, text="x" * 16385)


def test_update_and_delete_are_owner_scoped_revision_fenced_mutations(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        enqueue(tx)
        updated = repo.update_text(
            tx,
            owner_id=OWNER,
            chat_id=CHAT,
            item_id=ITEM_1,
            text="edited",
            expected_revision=1,
            updated_at=30,
        )
        assert updated.text == "edited" and updated.revision == 2 and updated.updated_at == 30
        with pytest.raises(RepositoryConflictError):
            repo.update_text(
                tx,
                owner_id=OWNER,
                chat_id=CHAT,
                item_id=ITEM_1,
                text="stale",
                expected_revision=1,
                updated_at=40,
            )
        with pytest.raises(RepositoryNotFoundError):
            repo.update_text(
                tx,
                owner_id=OTHER,
                chat_id=CHAT,
                item_id=ITEM_1,
                text="denied",
                expected_revision=2,
                updated_at=40,
            )
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT, item_id=ITEM_1).text == "edited"
        with pytest.raises(RepositoryConflictError):
            repo.delete(tx, owner_id=OWNER, chat_id=CHAT, item_id=ITEM_1, expected_revision=1)
        assert repo.delete(tx, owner_id=OWNER, chat_id=CHAT, item_id=ITEM_1, expected_revision=2)
        assert repo.list(tx, owner_id=OWNER, chat_id=CHAT) == ()
        with pytest.raises(RepositoryNotFoundError):
            repo.delete(tx, owner_id=OWNER, chat_id=CHAT, item_id=ITEM_1, expected_revision=2)


def test_stale_revision_reorder_conflicts_and_changes_nothing(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        first = enqueue(tx)
        second = enqueue(tx, item_id=ITEM_2, text="second", created_at=21)
        revisions = {ITEM_1: first.revision, ITEM_2: second.revision + 1}
        with pytest.raises(RepositoryConflictError):
            repo.reorder(
                tx,
                owner_id=OWNER,
                chat_id=CHAT,
                ordered_item_ids=[ITEM_2, ITEM_1],
                expected_revisions=revisions,
                reordered_at=40,
            )
        assert [item.item_id for item in repo.list(tx, owner_id=OWNER, chat_id=CHAT)] == [
            ITEM_1,
            ITEM_2,
        ]
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT, item_id=ITEM_2).revision == (
            second.revision
        )


def test_reorder_requires_a_full_permutation_and_updates_positions(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        first = enqueue(tx)
        second = enqueue(tx, item_id=ITEM_2, text="second", created_at=21)
        third = enqueue(tx, item_id=ITEM_3, text="third", created_at=22)
        revisions = {
            first.item_id: first.revision,
            second.item_id: second.revision,
            third.item_id: third.revision,
        }
        with pytest.raises(RepositoryValidationError):
            repo.reorder(
                tx,
                owner_id=OWNER,
                chat_id=CHAT,
                ordered_item_ids=[ITEM_1, ITEM_2],
                expected_revisions=revisions,
                reordered_at=40,
            )
        with pytest.raises(RepositoryValidationError):
            repo.reorder(
                tx,
                owner_id=OWNER,
                chat_id=CHAT,
                ordered_item_ids=[ITEM_1, ITEM_2, ITEM_3, ITEM_3],
                expected_revisions=revisions,
                reordered_at=40,
            )
        with pytest.raises(RepositoryValidationError):
            repo.reorder(
                tx,
                owner_id=OWNER,
                chat_id=CHAT,
                ordered_item_ids=[ITEM_1, ITEM_2, "99999999-0000-4000-8000-000000000009"],
                expected_revisions=revisions,
                reordered_at=40,
            )
        reordered = repo.reorder(
            tx,
            owner_id=OWNER,
            chat_id=CHAT,
            ordered_item_ids=[ITEM_3, ITEM_1, ITEM_2],
            expected_revisions=revisions,
            reordered_at=40,
        )
        assert [item.item_id for item in reordered] == [ITEM_3, ITEM_1, ITEM_2]
        assert [item.position for item in reordered] == [0, 1, 2]
        assert all(item.revision == 2 for item in reordered)
        assert [item.item_id for item in repo.list(tx, owner_id=OWNER, chat_id=CHAT)] == [
            ITEM_3,
            ITEM_1,
            ITEM_2,
        ]
        assert repo.reorder(
            tx,
            owner_id=OWNER,
            chat_id=CHAT,
            ordered_item_ids=[],
            expected_revisions={},
            reordered_at=41,
        ) == ()
        other = repo.list(tx, owner_id=OTHER, chat_id=CHAT)
        assert other == ()


def test_claim_next_dispatches_in_order_and_requires_the_owner(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        enqueue(tx)
        enqueue(tx, item_id=ITEM_2, text="second", created_at=21)
        assert repo.claim_next(tx, owner_id=OTHER, chat_id=CHAT) is None
        first = repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT)
        assert first.item_id == ITEM_1 and first.text == "first"
        second = repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT)
        assert second.item_id == ITEM_2
        assert repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT) is None
        assert repo.list(tx, owner_id=OWNER, chat_id=CHAT) == ()
        enqueue(tx, item_id=ITEM_3, text="third", created_at=22)
        with pytest.raises(RepositoryNotFoundError):
            enqueue(tx, item_id=ITEM_1, chat_id=OTHER_CHAT)


def test_owner_isolation_covers_list_edit_reorder_and_claim(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        chat(tx, chat_id=OTHER_CHAT, owner_id=OTHER)
        enqueue(tx)
        enqueue(tx, item_id=ITEM_2, text="second", created_at=21)
        assert repo.list(tx, owner_id=OTHER, chat_id=CHAT) == ()
        assert repo.get(tx, owner_id=OTHER, chat_id=CHAT, item_id=ITEM_1) is None
        with pytest.raises(RepositoryNotFoundError):
            repo.update_text(
                tx,
                owner_id=OTHER,
                chat_id=CHAT,
                item_id=ITEM_1,
                text="denied",
                expected_revision=1,
                updated_at=30,
            )
        with pytest.raises(RepositoryValidationError):
            repo.reorder(
                tx,
                owner_id=OTHER,
                chat_id=CHAT,
                ordered_item_ids=[ITEM_1, ITEM_2],
                expected_revisions={ITEM_1: 1, ITEM_2: 1},
                reordered_at=30,
            )
        assert repo.claim_next(tx, owner_id=OTHER, chat_id=CHAT) is None
        with pytest.raises(RepositoryNotFoundError):
            repo.delete(tx, owner_id=OTHER, chat_id=CHAT, item_id=ITEM_1, expected_revision=1)
        assert [item.item_id for item in repo.list(tx, owner_id=OWNER, chat_id=CHAT)] == [
            ITEM_1,
            ITEM_2,
        ]


def test_deleting_the_chat_retires_its_queued_items(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        enqueue(tx)
        enqueue(tx, item_id=ITEM_2, text="second", created_at=21)
        ConversationRepository().delete(tx, owner_id=OWNER, conversation_id=CHAT)
        assert repo.list(tx, owner_id=OWNER, chat_id=CHAT) == ()
        assert repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT) is None


def test_caller_rollback_removes_uncommitted_queue_mutations(migrated_clone):
    repo = create_chat_queue_repository()
    with (
        pytest.raises(RuntimeError, match="queue rollback"),
        migrated_clone.database.transaction() as tx,
    ):
        chat(tx)
        enqueue(tx)
        repo.update_text(
            tx,
            owner_id=OWNER,
            chat_id=CHAT,
            item_id=ITEM_1,
            text="edited",
            expected_revision=1,
            updated_at=30,
        )
        raise RuntimeError("queue rollback")
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        assert repo.list(tx, owner_id=OWNER, chat_id=CHAT) == ()
        assert repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT) is None


def test_concurrent_claim_next_hands_the_head_to_exactly_one_caller(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        enqueue(tx)
        enqueue(tx, item_id=ITEM_2, text="second", created_at=21)
    outcomes = {}
    start = Barrier(2, timeout=30)

    def competitor(database):
        start.wait()
        with database.transaction() as tx:
            try:
                claimed = repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT)
            except RepositoryConflictError:
                claimed = "conflict"
        outcomes[id(database)] = claimed
        return claimed

    with (
        concurrent_database(migrated_clone) as first,
        concurrent_database(migrated_clone) as second,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = list(workers.map(competitor, (first, second)))
    winners = [result for result in results if result not in (None, "conflict")]
    identifiers = [result.item_id for result in winners]
    assert ITEM_1 in identifiers
    assert len(identifiers) == len(set(identifiers))
    with migrated_clone.database.transaction() as tx:
        remaining = repo.list(tx, owner_id=OWNER, chat_id=CHAT)
        assert [item.item_id for item in remaining] == ([] if ITEM_2 in identifiers else [ITEM_2])
        assert len(winners) + len(remaining) == 2
        if remaining:
            assert repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT).item_id == ITEM_2
        assert repo.claim_next(tx, owner_id=OWNER, chat_id=CHAT) is None


def test_public_chat_queue_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_owner_isolation_covers_list_edit_reorder_and_claim,
        test_enqueue_replay_and_owner_scoped_reads,
        test_concurrent_claim_next_hands_the_head_to_exactly_one_caller,
        test_stale_revision_reorder_conflicts_and_changes_nothing,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)


def test_concurrent_enqueue_serializes_positions_without_conflict(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        chat(tx)
    start = Barrier(2, timeout=30)

    def competitor(database):
        start.wait()
        with database.transaction() as tx:
            return create_chat_queue_repository().enqueue(
                tx,
                owner_id=OWNER,
                chat_id=CHAT,
                text=f"queued-{id(database)}",
                created_at=20,
            )

    with (
        concurrent_database(migrated_clone) as first,
        concurrent_database(migrated_clone) as second,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = list(workers.map(competitor, (first, second)))
    assert sorted(item.position for item in results) == [0, 1]
    with migrated_clone.database.transaction() as tx:
        assert len(create_chat_queue_repository().list(tx, owner_id=OWNER, chat_id=CHAT)) == 2
