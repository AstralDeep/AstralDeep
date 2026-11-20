"""Unit-proofs the chat read receipt store's fail-closed guards with scripted driver
doubles: multi-row write corruption, stale expectations without a durable receipt, and
a foreign owner row inside the batched read. PostgreSQL contract tests cover the
statement-level monotonic and owner-scoped behavior.
"""

import pytest

from astralplane.api import ChatReadReceiptRecord
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
)
from astralplane.repositories.chat_read_receipts import ChatReadReceiptRepository
from tests.repositories._support import Result, ScriptedTransaction


def _receipt_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "owner_id": "receipt-owner",
        "chat_id": "receipt-chat",
        "last_seen_position": 5,
        "revision": 1,
    }
    row.update(overrides)
    return row


def test_mark_seen_rejects_a_write_that_returns_multiple_records() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_receipt_row(), _receipt_row(revision=2)))]
    )
    with pytest.raises(RepositoryDataError, match="multiple detached records"):
        ChatReadReceiptRepository().mark_seen(
            transaction,  # type: ignore[arg-type]
            owner_id="receipt-owner",
            chat_id="receipt-chat",
            last_seen_position=5,
            expected_revision=0,
        )


def test_mark_seen_distinguishes_missing_chats_from_stale_expectations() -> None:
    repository = ChatReadReceiptRepository()
    missing = ScriptedTransaction(execute=[Result()], one=[None, None])
    with pytest.raises(RepositoryNotFoundError, match="chat was not found"):
        repository.mark_seen(
            missing,  # type: ignore[arg-type]
            owner_id="receipt-owner",
            chat_id="receipt-chat",
            last_seen_position=5,
            expected_revision=3,
        )
    stale = ScriptedTransaction(execute=[Result()], one=[None, {"present": 1}])
    with pytest.raises(RepositoryConflictError, match="stale"):
        repository.mark_seen(
            stale,  # type: ignore[arg-type]
            owner_id="receipt-owner",
            chat_id="receipt-chat",
            last_seen_position=5,
            expected_revision=3,
        )
    assert "chats.id = %s AND chats.user_id = %s" in missing.calls[0][1]


def test_batched_read_fails_closed_on_a_foreign_owner_row() -> None:
    transaction = ScriptedTransaction(
        all_rows=[(_receipt_row(owner_id="receipt-other"),)]
    )
    with pytest.raises(RepositoryDataError, match="owner boundary"):
        ChatReadReceiptRepository().list_receipts(
            transaction,  # type: ignore[arg-type]
            owner_id="receipt-owner",
        )


def test_list_receipts_maps_missing_receipts_to_none() -> None:
    transaction = ScriptedTransaction(
        all_rows=[
            (
                _receipt_row(),
                _receipt_row(
                    chat_id="receipt-second-chat",
                    last_seen_position=None,
                    revision=None,
                ),
            ),
        ]
    )
    states = ChatReadReceiptRepository().list_receipts(
        transaction,  # type: ignore[arg-type]
        owner_id="receipt-owner",
    )
    assert [
        (state.conversation_id, state.receipt is not None) for state in states
    ] == [
        ("receipt-chat", True),
        ("receipt-second-chat", False),
    ]
    assert states[0].receipt == ChatReadReceiptRecord(
        owner_id="receipt-owner",
        chat_id="receipt-chat",
        last_seen_position=5,
        revision=1,
    )
