"""Owner-scoped storage for conversation-step records: create, get, list, and a
compare-and-set finish. Used by astralplane.api and AstralDeep's plane-repository
adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _positive_int,
    _required_id,
    _row_value,
    _single_returned,
)


class ChatStepStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ERRORED = "errored"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self is not ChatStepStatus.IN_PROGRESS


@dataclass(frozen=True, slots=True)
class ChatStepRecord:
    step_id: str
    conversation_id: str
    owner_id: str
    turn_message_id: int | None
    kind: str
    name: str
    status: ChatStepStatus
    args_truncated: str | None = field(repr=False)
    args_was_truncated: bool
    result_summary: str | None = field(repr=False)
    result_was_truncated: bool
    error_message: str | None = field(repr=False)
    started_at: int
    ended_at: int | None

    @property
    def terminal(self) -> bool:
        return self.status.terminal


class ChatStepRepository:
    _FIELDS = (
        "id, chat_id, user_id, turn_message_id, kind, name, status, "
        "args_truncated, args_was_truncated, result_summary, "
        "result_was_truncated, error_message, started_at, ended_at"
    )

    def create_step(
        self,
        transaction: Transaction,
        *,
        step_id: str,
        owner_id: str,
        conversation_id: str,
        turn_message_id: int | None,
        kind: str,
        name: str,
        args_truncated: str | None,
        args_was_truncated: bool,
        started_at: int,
    ) -> ChatStepRecord:
        identity = _required_id(step_id, "step_id", maximum=128)
        owner = _required_id(owner_id, "owner_id")
        conversation = _required_id(conversation_id, "conversation_id")
        turn = _optional_positive_int(turn_message_id, "turn_message_id")
        step_kind = _bounded_text(kind, "kind", maximum=128)
        step_name = _bounded_text(name, "name", maximum=1024)
        args = _optional_text(args_truncated, "args_truncated", 100_000)
        args_truncated_flag = _strict_bool(args_was_truncated, "args_was_truncated")
        started = _non_negative_int(started_at, "started_at")
        result = transaction.execute(
            f"""
            INSERT INTO chat_steps (
                id, chat_id, user_id, turn_message_id, kind, name, status,
                args_truncated, args_was_truncated, started_at
            )
            SELECT %s, chat.id, chat.user_id, %s, %s, %s, 'in_progress', %s, %s, %s
              FROM chats AS chat
             WHERE chat.id = %s AND chat.user_id = %s
               AND (
                    %s::integer IS NULL
                    OR EXISTS (
                        SELECT 1 FROM messages AS message
                         WHERE message.id = %s
                           AND message.chat_id = chat.id
                           AND message.user_id = chat.user_id
                    )
               )
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                identity,
                turn,
                step_kind,
                step_name,
                args,
                args_truncated_flag,
                started,
                conversation,
                owner,
                turn,
                turn,
            ),
        )
        returned = getattr(result, "returned_records", ())
        if returned:
            record = _step(_single_returned(result, "chat_step.create"))
            if turn is not None:
                bumped = transaction.execute(
                    """
                    UPDATE messages SET step_count = step_count + 1
                     WHERE id = %s AND chat_id = %s AND user_id = %s
                    """,
                    (turn, conversation, owner),
                )
                if bumped.rowcount != 1:
                    raise RepositoryDataError(
                        "step turn-message counter update lost its owner scope",
                        metadata={"operation": "chat_step.create"},
                    )
            return record

        existing = self.get_step(transaction, owner_id=owner, step_id=identity)
        if existing is None:
            raise RepositoryConflictError(
                "step identity, conversation owner, or turn-message scope is unavailable",
                metadata={"operation": "chat_step.create"},
            )
        expected = (
            conversation,
            turn,
            step_kind,
            step_name,
            args,
            args_truncated_flag,
            started,
        )
        observed = (
            existing.conversation_id,
            existing.turn_message_id,
            existing.kind,
            existing.name,
            existing.args_truncated,
            existing.args_was_truncated,
            existing.started_at,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "step replay changed immutable semantics",
                metadata={"operation": "chat_step.create"},
            )
        return existing

    def get_step(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        step_id: str,
    ) -> ChatStepRecord | None:
        owner = _required_id(owner_id, "owner_id")
        identity = _required_id(step_id, "step_id", maximum=128)
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM chat_steps WHERE id = %s AND user_id = %s",
            (identity, owner),
        )
        return None if row is None else _step(row)

    def list_steps(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        limit: int = 1000,
    ) -> tuple[ChatStepRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        conversation = _required_id(conversation_id, "conversation_id")
        limit = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS} FROM chat_steps
             WHERE chat_id = %s AND user_id = %s
             ORDER BY started_at ASC, id ASC
             LIMIT %s
            """,
            (conversation, owner, limit),
        )
        return tuple(_step(row) for row in rows)

    def finish_step(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        step_id: str,
        expected_status: ChatStepStatus | str,
        status: ChatStepStatus | str,
        ended_at: int,
        result_summary: str | None = None,
        result_was_truncated: bool = False,
        error_message: str | None = None,
    ) -> ChatStepRecord:
        owner = _required_id(owner_id, "owner_id")
        identity = _required_id(step_id, "step_id", maximum=128)
        expected = _status(expected_status, "expected_status")
        terminal = _status(status, "status")
        if expected.terminal or not terminal.terminal:
            raise RepositoryValidationError(
                "finish_step requires a live expected status and terminal result status"
            )
        ended = _non_negative_int(ended_at, "ended_at")
        summary = _optional_text(result_summary, "result_summary", 100_000)
        result_truncated = _strict_bool(result_was_truncated, "result_was_truncated")
        error = _optional_text(error_message, "error_message", 100_000)
        result = transaction.execute(
            f"""
            UPDATE chat_steps
               SET status = %s, ended_at = %s, result_summary = %s,
                   result_was_truncated = %s, error_message = %s
             WHERE id = %s AND user_id = %s AND status = %s AND ended_at IS NULL
               AND started_at <= %s
            RETURNING {self._FIELDS}
            """,
            (
                terminal.value,
                ended,
                summary,
                result_truncated,
                error,
                identity,
                owner,
                expected.value,
                ended,
            ),
        )
        returned = getattr(result, "returned_records", ())
        if returned:
            return _step(_single_returned(result, "chat_step.finish"))
        existing = self.get_step(transaction, owner_id=owner, step_id=identity)
        if existing is None:
            raise RepositoryNotFoundError(
                "owner-scoped step was not found",
                metadata={"operation": "chat_step.finish"},
            )
        raise RepositoryConflictError(
            "step status or timestamp fence is stale",
            metadata={"operation": "chat_step.finish"},
        )


def _status(value: object, field: str) -> ChatStepStatus:
    try:
        return ChatStepStatus(str(value))
    except ValueError as exc:
        raise RepositoryValidationError(f"{field} is unsupported") from exc


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise RepositoryValidationError(f"{field} must be a boolean")
    return value


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _optional_positive_int(value: object, field: str) -> int | None:
    return None if value is None else _positive_int(value, field)


def _stored_status(value: object) -> ChatStepStatus:
    try:
        return _status(value, "status")
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted step status is unsupported") from exc


def _stored_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise RepositoryDataError(
            "persisted step truncation flag is invalid", metadata={"field": field}
        )
    return value


def _stored_non_negative(value: object, field: str) -> int:
    try:
        return _non_negative_int(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted step timestamp is invalid", metadata={"field": field}
        ) from exc


def _optional_stored_non_negative(value: object, field: str) -> int | None:
    return None if value is None else _stored_non_negative(value, field)


def _stored_required_text(value: object, field: str, maximum: int) -> str:
    try:
        return _required_id(value, field, maximum=maximum)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted step text identity is invalid", metadata={"field": field}
        ) from exc


def _stored_optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    try:
        return _bounded_text(value, field, maximum=100_000, allow_empty=True)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted redacted step text is invalid", metadata={"field": field}
        ) from exc


def _step(row: Mapping[str, Any]) -> ChatStepRecord:
    status = _stored_status(_row_value(row, "status"))
    started = _stored_non_negative(_row_value(row, "started_at"), "started_at")
    ended = _optional_stored_non_negative(row.get("ended_at"), "ended_at")
    if status.terminal != (ended is not None) or (ended is not None and ended < started):
        raise RepositoryDataError("persisted step lifecycle timestamps are inconsistent")
    turn = row.get("turn_message_id")
    try:
        turn_message_id = None if turn is None else _positive_int(turn, "turn_message_id")
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted step turn-message identity is invalid") from exc
    return ChatStepRecord(
        step_id=_stored_required_text(_row_value(row, "id"), "step_id", 128),
        conversation_id=_stored_required_text(
            _row_value(row, "chat_id"), "conversation_id", 512
        ),
        owner_id=_stored_required_text(_row_value(row, "user_id"), "owner_id", 512),
        turn_message_id=turn_message_id,
        kind=_stored_required_text(_row_value(row, "kind"), "kind", 128),
        name=_stored_required_text(_row_value(row, "name"), "name", 1024),
        status=status,
        args_truncated=_stored_optional_text(row.get("args_truncated"), "args_truncated"),
        args_was_truncated=_stored_bool(
            _row_value(row, "args_was_truncated"), "args_was_truncated"
        ),
        result_summary=_stored_optional_text(row.get("result_summary"), "result_summary"),
        result_was_truncated=_stored_bool(
            _row_value(row, "result_was_truncated"), "result_was_truncated"
        ),
        error_message=_stored_optional_text(row.get("error_message"), "error_message"),
        started_at=started,
        ended_at=ended,
    )


__all__ = ("ChatStepRecord", "ChatStepRepository", "ChatStepStatus")
