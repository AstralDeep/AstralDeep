"""Scripted failure-path coverage for the chat queue repository: capacity, identity,
fence, permutation, and persisted-shape denials fail closed with typed errors before any
caller transaction state is touched.
"""

from __future__ import annotations

import dataclasses

import pytest

from astralplane import create_chat_queue_repository
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.chat_queue import QueuedFollowUpRecord
from tests.repositories._support import Result, ScriptedTransaction

ITEM = "10000000-0000-4000-8000-000000000001"
OTHER_ITEM = "10000000-0000-4000-8000-000000000002"


def row(**changes):
    values = dict(
        item_id=ITEM,
        owner_id="queue-owner",
        chat_id="queue-chat",
        text="queued",
        position=0,
        revision=1,
        created_at=10,
        updated_at=10,
    )
    values.update(changes)
    return values


def depth(value):
    return {"depth": value}


def test_enqueue_at_capacity_conflicts():
    transaction = ScriptedTransaction(
        one=[{"pg_advisory_xact_lock": None}, {"present": 1}, depth(100)]
    )
    with pytest.raises(RepositoryConflictError, match="capacity"):
        create_chat_queue_repository().enqueue(
            transaction,
            owner_id="queue-owner",
            chat_id="queue-chat",
            text="queued",
            created_at=10,
        )
    assert not transaction.calls[-1][1].startswith("INSERT")


def test_enqueue_replay_without_identity_conflicts():
    transaction = ScriptedTransaction(
        one=[{"pg_advisory_xact_lock": None}, {"present": 1}, depth(1), None, row()]
    )
    with pytest.raises(RepositoryConflictError, match="identity already exists"):
        create_chat_queue_repository().enqueue(
            transaction,
            owner_id="queue-owner",
            chat_id="queue-chat",
            text="queued",
            created_at=10,
            item_id=ITEM,
        )


def test_update_and_delete_fences_fail_closed():
    transaction = ScriptedTransaction(one=[row()])
    with pytest.raises(RepositoryConflictError, match="update fence rejected"):
        create_chat_queue_repository().update_text(
            transaction,
            owner_id="queue-owner",
            chat_id="queue-chat",
            item_id=ITEM,
            text="edited",
            expected_revision=1,
            updated_at=20,
        )
    transaction = ScriptedTransaction(
        one=[row()], execute=[Result(rowcount=0, returned_records=())]
    )
    with pytest.raises(RepositoryConflictError, match="deletion fence rejected"):
        create_chat_queue_repository().delete(
            transaction,
            owner_id="queue-owner",
            chat_id="queue-chat",
            item_id=ITEM,
            expected_revision=1,
        )


def test_exhausted_revision_conflicts_before_writing():
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryConflictError, match="exhausted"):
        create_chat_queue_repository().update_text(
            transaction,
            owner_id="queue-owner",
            chat_id="queue-chat",
            item_id=ITEM,
            text="edited",
            expected_revision=9007199254740991,
            updated_at=20,
        )
    assert transaction.calls == []


def test_reorder_input_shapes_fail_closed():
    repo = create_chat_queue_repository()
    with pytest.raises(RepositoryValidationError, match="sequence"):
        repo.reorder(
            ScriptedTransaction(),
            owner_id="o",
            chat_id="c",
            ordered_item_ids=ITEM,
            expected_revisions={ITEM: 1},
            reordered_at=1,
        )
    with pytest.raises(RepositoryValidationError, match="mapping"):
        repo.reorder(
            ScriptedTransaction(),
            owner_id="o",
            chat_id="c",
            ordered_item_ids=[ITEM],
            expected_revisions=[1],
            reordered_at=1,
        )
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError, match="fence every reordered item"):
        repo.reorder(
            transaction,
            owner_id="o",
            chat_id="c",
            ordered_item_ids=[ITEM],
            expected_revisions={OTHER_ITEM: 1},
            reordered_at=1,
        )
    assert transaction.calls == []
    with pytest.raises(RepositoryValidationError, match="repeat an item"):
        repo.reorder(
            ScriptedTransaction(),
            owner_id="o",
            chat_id="c",
            ordered_item_ids=[ITEM, ITEM],
            expected_revisions={ITEM: 1},
            reordered_at=1,
        )


def test_oversized_reorder_is_refused_without_touching_the_queue():
    items = [f"10000000-0000-4000-8000-{index:012x}" for index in range(101)]
    transaction = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError, match="capacity"):
        create_chat_queue_repository().reorder(
            transaction,
            owner_id="o",
            chat_id="c",
            ordered_item_ids=items,
            expected_revisions={item: 1 for item in items},
            reordered_at=1,
        )
    assert transaction.calls == []


def test_reorder_with_a_short_write_conflicts():
    transaction = ScriptedTransaction(
        one=[{"pg_advisory_xact_lock": None}],
        all_rows=[(row(),)],
        execute=[Result(), Result(rowcount=0, returned_records=())],
    )
    with pytest.raises(RepositoryConflictError, match="fence rejected"):
        create_chat_queue_repository().reorder(
            transaction,
            owner_id="queue-owner",
            chat_id="queue-chat",
            ordered_item_ids=[ITEM],
            expected_revisions={ITEM: 1},
            reordered_at=20,
        )


def test_reorder_returning_an_unexpected_permutation_fails_closed():
    transaction = ScriptedTransaction(
        one=[{"pg_advisory_xact_lock": None}],
        all_rows=[(row(), row(item_id=OTHER_ITEM, position=1))],
        execute=[
            Result(),
            Result(
                rowcount=1,
                returned_records=(
                    row(item_id=ITEM, position=1),
                    row(item_id=OTHER_ITEM, position=0),
                ),
            ),
        ],
    )
    with pytest.raises(RepositoryDataError, match="unexpected permutation"):
        create_chat_queue_repository().reorder(
            transaction,
            owner_id="queue-owner",
            chat_id="queue-chat",
            ordered_item_ids=[ITEM, OTHER_ITEM],
            expected_revisions={ITEM: 1, OTHER_ITEM: 1},
            reordered_at=20,
        )


def test_claim_returning_multiple_records_fails_closed():
    transaction = ScriptedTransaction(execute=[Result(returned_records=(row(), row()))])
    with pytest.raises(RepositoryDataError, match="multiple detached records"):
        create_chat_queue_repository().claim_next(
            transaction, owner_id="queue-owner", chat_id="queue-chat"
        )


def test_claim_returning_a_non_mapping_record_fails_closed():
    transaction = ScriptedTransaction(execute=[Result(returned_records=("corrupt",))])
    with pytest.raises(RepositoryDataError, match="non-mapping record"):
        create_chat_queue_repository().claim_next(
            transaction, owner_id="queue-owner", chat_id="queue-chat"
        )


def test_corrupt_persisted_rows_fail_closed():
    transaction = ScriptedTransaction(one=[{"item_id": ITEM}])
    with pytest.raises(RepositoryDataError, match="missing a required field"):
        create_chat_queue_repository().get(
            transaction, owner_id="queue-owner", chat_id="queue-chat", item_id=ITEM
        )
    transaction = ScriptedTransaction(all_rows=[(row(revision=0),)])
    with pytest.raises(RepositoryValidationError, match="bounded integer"):
        create_chat_queue_repository().list(
            transaction, owner_id="queue-owner", chat_id="queue-chat"
        )
    transaction = ScriptedTransaction(one=[row(updated_at=5)])
    with pytest.raises(RepositoryDataError, match="regressed its retention clock"):
        create_chat_queue_repository().get(
            transaction, owner_id="queue-owner", chat_id="queue-chat", item_id=ITEM
        )


def test_malformed_item_identifiers_fail_closed():
    repo = create_chat_queue_repository()
    with pytest.raises(RepositoryValidationError, match="UUID string"):
        repo.get(ScriptedTransaction(), owner_id="o", chat_id="c", item_id="not-a-uuid")
    with pytest.raises(RepositoryValidationError, match="bounded integer"):
        repo.update_text(
            ScriptedTransaction(),
            owner_id="o",
            chat_id="c",
            item_id=ITEM,
            text="edited",
            expected_revision=True,
            updated_at=20,
        )


def test_record_is_a_frozen_detached_value():

    record = QueuedFollowUpRecord(
        item_id=ITEM,
        owner_id="queue-owner",
        chat_id="queue-chat",
        text="queued",
        position=0,
        revision=1,
        created_at=10,
        updated_at=10,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.text = "mutated"
