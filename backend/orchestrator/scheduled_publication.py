"""In-memory staging for one scheduled chat turn's history mutations, so a long-running
scheduled LLM call never holds a PostgreSQL transaction open; scheduler/store.py
later publishes the frozen batch with its effect-ledger transition atomically.
"""

from __future__ import annotations

import contextvars
import copy
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator


class ScheduledPublicationEscapeError(RuntimeError):
    pass


@dataclass(frozen=True)
class StagedHistoryMessage:
    role: str
    content: str
    title_source: str
    timestamp_ms: int


@dataclass(frozen=True)
class ScheduledHistoryBatch:
    chat_id: str
    user_id: str
    create_chat_if_missing: bool
    agent_id: str | None
    requested_title: str | None
    messages: tuple[StagedHistoryMessage, ...]
    conversation_commit_id: str | None = None
    request_generation: str | None = None
    base_render_revision: int | None = None
    committed_render_revision: int | None = None
    canvas_layouts: tuple[dict[str, Any], ...] = ()


class ScheduledHistoryStage:
    def __init__(
        self,
        *,
        history: Any,
        chat_id: str,
        user_id: str,
        create_chat_if_missing: bool,
        agent_id: str | None,
    ) -> None:
        if not chat_id or not user_id:
            raise ValueError("scheduled history target requires chat_id and user_id")
        self._history = history
        self.chat_id = chat_id
        self.user_id = user_id
        self.create_chat_if_missing = bool(create_chat_if_missing)
        self.agent_id = agent_id
        self.requested_title: str | None = None
        self._messages: list[StagedHistoryMessage] = []
        self._sealed = False

    def matches(self, history: Any, chat_id: str, user_id: str) -> bool:
        return (
            history is self._history
            and str(chat_id) == self.chat_id
            and str(user_id) == self.user_id
        )

    def _assert_write_target(
        self, history: Any, chat_id: str, user_id: str
    ) -> None:
        if self._sealed:
            raise ScheduledPublicationEscapeError(
                "scheduled history stage is already sealed"
            )
        if not self.matches(history, chat_id, user_id):
            raise ScheduledPublicationEscapeError(
                "scheduled history write escaped its owned chat"
            )

    def add_message(
        self,
        history: Any,
        *,
        chat_id: str,
        user_id: str,
        role: str,
        content: Any,
    ) -> None:
        self._assert_write_target(history, chat_id, user_id)
        content_string = content if isinstance(content, str) else json.dumps(content)
        title_source = str(content)
        timestamp_ms = int(time.time() * 1000)
        if self._messages:
            timestamp_ms = max(timestamp_ms, self._messages[-1].timestamp_ms + 1)
        self._messages.append(
            StagedHistoryMessage(
                role=str(role),
                content=content_string,
                title_source=title_source,
                timestamp_ms=timestamp_ms,
            )
        )

    def update_title(
        self,
        history: Any,
        *,
        chat_id: str,
        user_id: str,
        title: str,
    ) -> None:
        self._assert_write_target(history, chat_id, user_id)
        self.requested_title = str(title)

    @property
    def messages(self) -> tuple[StagedHistoryMessage, ...]:
        return tuple(self._messages)

    def seal(self) -> None:
        self._sealed = True

    def batch(
        self,
        *,
        conversation_commit_id: str | None = None,
        request_generation: str | None = None,
        base_render_revision: int | None = None,
        committed_render_revision: int | None = None,
        canvas_layouts: list[dict[str, Any]] | None = None,
    ) -> ScheduledHistoryBatch:
        if not self._sealed:
            raise RuntimeError("scheduled history stage must be sealed first")
        return ScheduledHistoryBatch(
            chat_id=self.chat_id,
            user_id=self.user_id,
            create_chat_if_missing=self.create_chat_if_missing,
            agent_id=self.agent_id,
            requested_title=self.requested_title,
            messages=tuple(self._messages),
            conversation_commit_id=conversation_commit_id,
            request_generation=request_generation,
            base_render_revision=base_render_revision,
            committed_render_revision=committed_render_revision,
            canvas_layouts=tuple(copy.deepcopy(canvas_layouts or [])),
        )


_ACTIVE_STAGE: contextvars.ContextVar[ScheduledHistoryStage | None] = (
    contextvars.ContextVar("scheduled_history_stage", default=None)
)


def current_scheduled_history_stage() -> ScheduledHistoryStage | None:
    return _ACTIVE_STAGE.get()


@contextmanager
def stage_scheduled_history(
    *,
    history: Any,
    chat_id: str,
    user_id: str,
    create_chat_if_missing: bool,
    agent_id: str | None,
) -> Iterator[ScheduledHistoryStage]:
    if _ACTIVE_STAGE.get() is not None:
        raise RuntimeError("nested scheduled history stages are not supported")
    stage = ScheduledHistoryStage(
        history=history,
        chat_id=chat_id,
        user_id=user_id,
        create_chat_if_missing=create_chat_if_missing,
        agent_id=agent_id,
    )
    token = _ACTIVE_STAGE.set(stage)
    try:
        yield stage
    finally:
        stage.seal()
        _ACTIVE_STAGE.reset(token)
