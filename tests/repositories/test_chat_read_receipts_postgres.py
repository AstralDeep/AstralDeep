"""Exercises owner- and chat-scoped chat read receipts against real PostgreSQL: the
monotonic one-statement mark, the revision fence, owner isolation, the batched read
that covers chats without receipts, and cascade retirement on chat deletion. Caller
rollback and device-conflict determinism are public facade contracts.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from psycopg2.extensions import make_dsn

from astralplane import create_chat_read_receipt_repository
from astralplane.api import ChatReadReceiptRecord
from astralplane.database.pool import ConnectionPool
from astralplane.database.postgres import create_postgres_driver_pool
from astralplane.database.transaction import PlaneDatabase
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.history import ConversationRepository

OWNER = "receipt-owner"
OTHER = "receipt-other"
CHAT = "receipt-chat"
SECOND_CHAT = "receipt-second-chat"
OTHER_CHAT = "receipt-other-chat"


def receipt_record(owner_id, chat_id, last_seen_position, revision):
    return ChatReadReceiptRecord(
        owner_id=owner_id,
        chat_id=chat_id,
        last_seen_position=last_seen_position,
        revision=revision,
    )


def chat(tx, chat_id=CHAT, owner_id=OWNER, created_at=10):
    return ConversationRepository().create(
        tx,
        owner_id=owner_id,
        conversation_id=chat_id,
        title="Synthetic receipt chat",
        agent_id=None,
        created_at=created_at,
    )


def mark(tx, position=5, chat_id=CHAT, owner_id=OWNER, expected_revision=0):
    return create_chat_read_receipt_repository().mark_seen(
        tx,
        owner_id=owner_id,
        chat_id=chat_id,
        last_seen_position=position,
        expected_revision=expected_revision,
    )


@contextmanager
def concurrent_database(clone):
    if clone.dsn.startswith("sqlite:"):
        from astralplane.database.sqlite import create_sqlite_driver_pool

        pool = ConnectionPool(create_sqlite_driver_pool(clone.dsn))
        try:
            yield PlaneDatabase(pool)
        finally:
            pool.close()
        return
    configured = make_dsn(clone.dsn, options=f"-csearch_path={clone.schema},pg_catalog")
    pool = ConnectionPool(
        create_postgres_driver_pool(configured, minimum_connections=1, maximum_connections=2)
    )
    try:
        yield PlaneDatabase(pool)
    finally:
        pool.close()


def test_mark_seen_creates_a_receipt_and_reads_are_owner_scoped(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        chat(tx, chat_id=SECOND_CHAT, created_at=11)
        chat(tx, chat_id=OTHER_CHAT, owner_id=OTHER)
        created = mark(tx)
        assert created == receipt_record(OWNER, CHAT, 5, 1)
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) == created
        assert repo.get(tx, owner_id=OWNER, chat_id=SECOND_CHAT) is None
        assert repo.get(tx, owner_id=OTHER, chat_id=CHAT) is None
        assert repo.get(tx, owner_id=OWNER, chat_id=OTHER_CHAT) is None
        with pytest.raises(RepositoryNotFoundError):
            mark(tx, position=1, chat_id=OTHER_CHAT)
        assert repo.get(tx, owner_id=OTHER, chat_id=OTHER_CHAT) is None


def test_mark_seen_is_monotonic_and_older_positions_are_no_ops(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        first = mark(tx, position=5)
        assert first.revision == 1 and first.last_seen_position == 5
        assert mark(tx, position=3, expected_revision=first.revision) == first
        assert mark(tx, position=5, expected_revision=first.revision) == first
        advanced = mark(tx, position=7, expected_revision=first.revision)
        assert advanced == receipt_record(OWNER, CHAT, 7, 2)
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) == advanced


def test_stale_revision_conflicts_and_changes_nothing(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        first = mark(tx, position=5)
        advanced = mark(tx, position=6, expected_revision=first.revision)
        assert advanced.revision == 2
        with pytest.raises(RepositoryConflictError):
            mark(tx, position=9, expected_revision=first.revision)
        with pytest.raises(RepositoryConflictError):
            mark(tx, position=2, expected_revision=99)
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) == advanced


def test_another_owner_cannot_read_or_move_a_receipt(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        chat(tx, chat_id=OTHER_CHAT, owner_id=OTHER)
        stored = mark(tx, position=5)
        assert repo.get(tx, owner_id=OTHER, chat_id=CHAT) is None
        with pytest.raises(RepositoryNotFoundError):
            mark(tx, position=6, chat_id=CHAT, owner_id=OTHER, expected_revision=1)
        with pytest.raises(RepositoryNotFoundError):
            mark(
                tx,
                position=6,
                chat_id=CHAT,
                owner_id=OTHER,
                expected_revision=stored.revision,
            )
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) == stored
        states = repo.list_receipts(tx, owner_id=OTHER)
        assert [(state.conversation_id, state.receipt) for state in states] == [
            (OTHER_CHAT, None)
        ]


def test_batched_read_returns_every_chat_including_chats_without_receipts(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx, created_at=12)
        chat(tx, chat_id=SECOND_CHAT, created_at=11)
        chat(tx, chat_id=OTHER_CHAT, owner_id=OTHER, created_at=13)
        marked = mark(tx, position=4)
        states = repo.list_receipts(tx, owner_id=OWNER)
        assert [(state.conversation_id, state.owner_id) for state in states] == [
            (CHAT, OWNER),
            (SECOND_CHAT, OWNER),
        ]
        assert states[0].receipt == marked
        assert states[1].receipt is None
        other_states = repo.list_receipts(tx, owner_id=OTHER)
        assert [(state.conversation_id, state.receipt) for state in other_states] == [
            (OTHER_CHAT, None)
        ]
        ConversationRepository().create(
            tx,
            owner_id=OWNER,
            conversation_id="receipt-third-chat",
            title="Synthetic third chat",
            agent_id=None,
            created_at=14,
        )
        assert [
            state.conversation_id for state in repo.list_receipts(tx, owner_id=OWNER)
        ] == ["receipt-third-chat", CHAT, SECOND_CHAT]
        with pytest.raises(RepositoryValidationError):
            repo.list_receipts(tx, owner_id=OWNER, limit=0)


def test_deleting_the_chat_retires_its_receipt_and_recreation_does_not_inherit(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        stored = mark(tx, position=5)
        assert ConversationRepository().delete(tx, owner_id=OWNER, conversation_id=CHAT)
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) is None
        assert list(repo.list_receipts(tx, owner_id=OWNER)) == []
        chat(tx)
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) is None
        recreated = mark(tx, position=2)
        assert recreated == receipt_record(OWNER, CHAT, 2, 1)
        assert stored.last_seen_position != recreated.last_seen_position


def test_mark_seen_validates_its_inputs(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        with pytest.raises(RepositoryValidationError):
            mark(tx, position=-1)
        with pytest.raises(RepositoryValidationError):
            mark(tx, position=9007199254740992)
        with pytest.raises(RepositoryValidationError):
            mark(tx, position=True)
        with pytest.raises(RepositoryValidationError):
            mark(tx, expected_revision=-1)
        with pytest.raises(RepositoryValidationError):
            mark(tx, expected_revision=True)
        with pytest.raises(RepositoryValidationError):
            mark(tx, chat_id="")
        with pytest.raises(RepositoryValidationError):
            repo.get(tx, owner_id=OWNER, chat_id="")
        with pytest.raises(RepositoryValidationError):
            repo.get(tx, owner_id="", chat_id=CHAT)


def test_caller_rollback_removes_uncommitted_receipt_mutations(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with (
        pytest.raises(RuntimeError, match="receipt rollback"),
        migrated_clone.database.transaction() as tx,
    ):
        chat(tx)
        mark(tx, position=5)
        raise RuntimeError("receipt rollback")
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) is None
        assert [state.receipt for state in repo.list_receipts(tx, owner_id=OWNER)] == [None]


def test_concurrent_mark_seen_resolves_device_conflicts_deterministically(migrated_clone):
    repo = create_chat_read_receipt_repository()
    with migrated_clone.database.transaction() as tx:
        chat(tx)
        mark(tx, position=5)
    start = Barrier(2, timeout=30)

    def competitor(database):
        start.wait()
        with database.transaction() as tx:
            try:
                record = repo.mark_seen(
                    tx,
                    owner_id=OWNER,
                    chat_id=CHAT,
                    last_seen_position=9,
                    expected_revision=1,
                )
            except RepositoryConflictError:
                record = "conflict"
        return record

    with (
        concurrent_database(migrated_clone) as first,
        concurrent_database(migrated_clone) as second,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = list(workers.map(competitor, (first, second)))
    winners = [result for result in results if result != "conflict"]
    assert len(winners) == 1 and winners[0].last_seen_position == 9
    with migrated_clone.database.transaction() as tx:
        assert repo.get(tx, owner_id=OWNER, chat_id=CHAT) == receipt_record(OWNER, CHAT, 9, 2)


def test_public_chat_read_receipt_contract_behaviors():
    from tests.fixtures.migrated_template import bound_clone, current_lineages

    for check in (
        test_another_owner_cannot_read_or_move_a_receipt,
        test_mark_seen_creates_a_receipt_and_reads_are_owner_scoped,
        test_stale_revision_conflicts_and_changes_nothing,
        test_mark_seen_is_monotonic_and_older_positions_are_no_ops,
    ):
        with bound_clone(current_lineages().current()) as clone:
            check(clone)
