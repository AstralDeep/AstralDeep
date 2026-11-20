"""Tests for astralplane.repositories.chat_steps: step creation proves chat/turn
ownership, exact replay after a terminal transition, owner-scoped reads, and
status/timestamp-fenced finish transitions.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.chat_steps import (
    ChatStepRepository,
    ChatStepStatus,
)
from tests.repositories._support import Result, ScriptedTransaction


def _step_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "step-1",
        "chat_id": "chat-1",
        "user_id": "owner-1",
        "turn_message_id": 17,
        "kind": "tool_call",
        "name": "search",
        "status": "in_progress",
        "args_truncated": "redacted args",
        "args_was_truncated": False,
        "result_summary": None,
        "result_was_truncated": False,
        "error_message": None,
        "started_at": 100,
        "ended_at": None,
    }
    row.update(overrides)
    return row


def test_create_step_proves_chat_and_turn_ownership_and_bumps_counter_once() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_step_row(),)), Result(rowcount=1)]
    )

    record = ChatStepRepository().create_step(
        transaction,  # type: ignore[arg-type]
        step_id="step-1",
        owner_id="owner-1",
        conversation_id="chat-1",
        turn_message_id=17,
        kind="tool_call",
        name="search",
        args_truncated="redacted args",
        args_was_truncated=False,
        started_at=100,
    )

    assert record.status is ChatStepStatus.IN_PROGRESS and not record.terminal
    assert "redacted args" not in repr(record)
    assert "FROM chats AS chat" in transaction.calls[0][1]
    assert "message.user_id = chat.user_id" in transaction.calls[0][1]
    assert transaction.calls[0][2][-4:] == ("chat-1", "owner-1", 17, 17)  # type: ignore[index]
    assert transaction.calls[1][2] == (17, "chat-1", "owner-1")


def test_create_step_without_turn_does_not_touch_message_counter() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_step_row(turn_message_id=None),))]
    )

    record = ChatStepRepository().create_step(
        transaction,  # type: ignore[arg-type]
        step_id="step-1",
        owner_id="owner-1",
        conversation_id="chat-1",
        turn_message_id=None,
        kind="phase",
        name="planning",
        args_truncated=None,
        args_was_truncated=False,
        started_at=100,
    )

    assert record.turn_message_id is None
    assert len(transaction.calls) == 1


def test_create_step_accepts_exact_replay_after_terminal_transition() -> None:
    terminal = _step_row(
        status="completed",
        result_summary="redacted result",
        ended_at=200,
    )
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[terminal])

    record = ChatStepRepository().create_step(
        transaction,  # type: ignore[arg-type]
        step_id="step-1",
        owner_id="owner-1",
        conversation_id="chat-1",
        turn_message_id=17,
        kind="tool_call",
        name="search",
        args_truncated="redacted args",
        args_was_truncated=False,
        started_at=100,
    )

    assert record.status is ChatStepStatus.COMPLETED and record.terminal
    assert len(transaction.calls) == 2
    assert all("step_count" not in call[1] for call in transaction.calls)


@pytest.mark.parametrize(
    "existing",
    [None, _step_row(name="different")],
)
def test_create_step_rejects_unavailable_scope_or_changed_replay(
    existing: dict[str, object] | None,
) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[existing])

    with pytest.raises(RepositoryConflictError):
        ChatStepRepository().create_step(
            transaction,  # type: ignore[arg-type]
            step_id="step-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            turn_message_id=17,
            kind="tool_call",
            name="search",
            args_truncated="redacted args",
            args_was_truncated=False,
            started_at=100,
        )


def test_create_rolls_back_when_turn_counter_scope_is_lost() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_step_row(),)), Result(rowcount=0)]
    )

    with pytest.raises(RepositoryDataError, match="counter"):
        ChatStepRepository().create_step(
            transaction,  # type: ignore[arg-type]
            step_id="step-1",
            owner_id="owner-1",
            conversation_id="chat-1",
            turn_message_id=17,
            kind="tool_call",
            name="search",
            args_truncated=None,
            args_was_truncated=False,
            started_at=100,
        )


def test_get_and_list_are_owner_scoped_and_chronological() -> None:
    transaction = ScriptedTransaction(
        one=[_step_row()],
        all_rows=[(_step_row(id="step-1"), _step_row(id="step-2", started_at=200))],
    )
    repository = ChatStepRepository()

    loaded = repository.get_step(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        step_id="step-1",
    )
    listed = repository.list_steps(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="chat-1",
        limit=2,
    )

    assert loaded is not None and loaded.step_id == "step-1"
    assert [step.step_id for step in listed] == ["step-1", "step-2"]
    assert transaction.calls[0][2] == ("step-1", "owner-1")
    assert transaction.calls[1][2] == ("chat-1", "owner-1", 2)
    assert "ORDER BY started_at ASC, id ASC" in transaction.calls[1][1]


def test_absent_step_read_is_explicit() -> None:
    transaction = ScriptedTransaction(one=[None])
    assert (
        ChatStepRepository().get_step(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            step_id="missing",
        )
        is None
    )


def test_finish_step_uses_owner_status_and_timestamp_fences() -> None:
    completed = _step_row(
        status="completed",
        result_summary="redacted result",
        result_was_truncated=True,
        ended_at=200,
    )
    transaction = ScriptedTransaction(execute=[Result(returned_records=(completed,))])

    record = ChatStepRepository().finish_step(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        step_id="step-1",
        expected_status=ChatStepStatus.IN_PROGRESS,
        status=ChatStepStatus.COMPLETED,
        ended_at=200,
        result_summary="redacted result",
        result_was_truncated=True,
    )

    assert record.terminal and record.ended_at == 200
    assert "status = %s AND ended_at IS NULL" in transaction.fetch_sql()
    assert "started_at <= %s" in transaction.fetch_sql()
    assert transaction.calls[0][2] == (
        "completed",
        200,
        "redacted result",
        True,
        None,
        "step-1",
        "owner-1",
        "in_progress",
        200,
    )


@pytest.mark.parametrize(
    ("existing", "error_type"),
    [
        (None, RepositoryNotFoundError),
        (_step_row(status="cancelled", ended_at=150), RepositoryConflictError),
    ],
)
def test_finish_step_reports_missing_and_stale_state(
    existing: dict[str, object] | None,
    error_type: type[Exception],
) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[existing])

    with pytest.raises(error_type):
        ChatStepRepository().finish_step(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            step_id="step-1",
            expected_status="in_progress",
            status="cancelled",
            ended_at=200,
        )


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("step_id", ""),
        ("owner_id", ""),
        ("conversation_id", ""),
        ("turn_message_id", 0),
        ("kind", ""),
        ("name", ""),
        ("args_was_truncated", 1),
        ("started_at", -1),
    ],
)
def test_create_rejects_invalid_values(argument: str, value: object) -> None:
    arguments: dict[str, object] = {
        "step_id": "step-1",
        "owner_id": "owner-1",
        "conversation_id": "chat-1",
        "turn_message_id": 17,
        "kind": "tool_call",
        "name": "search",
        "args_truncated": None,
        "args_was_truncated": False,
        "started_at": 100,
    }
    arguments[argument] = value
    with pytest.raises(RepositoryValidationError):
        ChatStepRepository().create_step(  # type: ignore[arg-type]
            ScriptedTransaction(),  # type: ignore[arg-type]
            **arguments,
        )


@pytest.mark.parametrize(
    ("expected", "status"),
    [
        ("completed", "cancelled"),
        ("in_progress", "in_progress"),
        ("unknown", "completed"),
    ],
)
def test_finish_requires_supported_live_to_terminal_transition(
    expected: str,
    status: str,
) -> None:
    with pytest.raises(RepositoryValidationError):
        ChatStepRepository().finish_step(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            step_id="step-1",
            expected_status=expected,
            status=status,
            ended_at=200,
        )


@pytest.mark.parametrize(
    "row",
    [
        _step_row(status="unknown"),
        _step_row(status="completed", ended_at=None),
        _step_row(status="in_progress", ended_at=200),
        _step_row(status="completed", ended_at=50),
        _step_row(turn_message_id=0),
        _step_row(args_was_truncated=1),
        _step_row(name=None),
        _step_row(result_summary=3),
    ],
)
def test_corrupt_persisted_lifecycle_fails_closed(row: dict[str, object]) -> None:
    transaction = ScriptedTransaction(one=[row])
    with pytest.raises(RepositoryDataError):
        ChatStepRepository().get_step(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            step_id="step-1",
        )
