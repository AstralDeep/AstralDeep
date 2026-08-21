"""Persistent step recorder for in-chat progress notifications.

Feature 014-progress-notifications, US2 / FR-007 through FR-013, FR-020/021.

The recorder is created once per active chat turn and given the WebSocket the
turn arrived on. It exposes a small lifecycle API:

* :meth:`start` — register a step (tool call / agent hand-off / orchestrator
  phase), persist an ``in_progress`` row, emit a ``chat_step`` event, and
  return a stable ``step_id`` the caller uses for completion/error.
* :meth:`complete` — mark a step ``completed`` with its truncated result.
* :meth:`error` — mark a step ``errored`` with a redacted message.
* :meth:`cancel_all_in_flight` — invoked by the cancel_task handler; marks
  every in-progress step ``cancelled`` (FR-020/021).
* :meth:`is_terminal` — used by the orchestrator to drop late-arriving
  results from cancelled steps (R6 best-effort discard policy).

All payloads pass through :func:`shared.phi_redactor.redact` before being
persisted or transmitted (FR-009b, defense-in-depth at the write boundary).
The recorder never raises into the caller — failures are structured-logged
and the caller's lifecycle continues unaffected.

See also:

* contracts/chat_step_event.md for the wire shape.
* data-model.md for the ``chat_steps`` schema.
* research.md R1, R4, R5, R6 for design rationale.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from shared.phi_redactor import redact

logger = logging.getLogger("Orchestrator.ChatSteps")

KIND_TOOL_CALL = "tool_call"
KIND_AGENT_HANDOFF = "agent_handoff"
KIND_PHASE = "phase"

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_ERRORED = "errored"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

_TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_ERRORED,
    STATUS_CANCELLED,
    STATUS_INTERRUPTED,
})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _record_to_step(record: Any) -> Dict[str, Any]:
    """Normalise a detached Plane record into the wire shape consumers expect."""
    return {
        "id": record.step_id,
        "chat_id": record.conversation_id,
        "turn_message_id": record.turn_message_id,
        "kind": record.kind,
        "name": record.name,
        "status": record.status.value,
        "args_truncated": record.args_truncated,
        "args_was_truncated": record.args_was_truncated,
        "result_summary": record.result_summary,
        "result_was_truncated": record.result_was_truncated,
        "error_message": record.error_message,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
    }


class ChatStepRecorder:
    """Records lifecycle events for one chat turn's persistent step trail.

    A new recorder is constructed per active turn so it can hold per-turn
    state (the in-flight set used by ``cancel_all_in_flight``) without a
    shared registry. The recorder is safe to construct with ``websocket=None``
    or ``safe_send=None`` — persistence still happens; the WebSocket emit is
    skipped silently.
    """

    def __init__(
        self,
        *,
        db=None,
        websocket=None,
        safe_send=None,
        chat_id: str,
        user_id: str,
        turn_message_id: Optional[int] = None,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
    ):
        self.websocket = websocket
        self.safe_send = safe_send
        self.chat_id = chat_id
        self.user_id = user_id
        self.turn_message_id = turn_message_id
        self._in_flight: Dict[str, str] = {}
        # Cache the terminal status for late-arriving result discard checks.
        self._statuses: Dict[str, str] = {}
        repository, runtime = repository_from(
            "chat_steps",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._steps = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )

    # ------------------------------------------------------------------
    # Lifecycle entry points
    # ------------------------------------------------------------------
    async def start(self, kind: str, name: str, args: Any = None) -> str:
        """Register a new in-progress step. Returns its ``step_id``."""
        step_id = uuid.uuid4().hex
        args_text, args_trunc = redact(args, kind="args")
        started = _now_ms()
        record = None
        try:
            record = await self._steps.call_async(
                self._steps.repository.create_step,
                step_id=step_id,
                owner_id=self.user_id,
                conversation_id=self.chat_id,
                turn_message_id=self.turn_message_id,
                kind=kind,
                name=name,
                args_truncated=args_text,
                args_was_truncated=args_trunc,
                started_at=started,
            )
        except Exception as exc:  # pragma: no cover — defensive
            # "step_name", not "name": `name` is a reserved LogRecord attribute
            # and putting it in `extra` makes the logging call itself raise.
            logger.error(
                "chat_steps.start_persist_failed",
                extra={"chat_id": self.chat_id, "kind": kind, "step_name": name, "error": str(exc)},
            )

        self._in_flight[step_id] = name
        self._statuses[step_id] = STATUS_IN_PROGRESS

        await self._emit(
            _record_to_step(record)
            if record is not None
            else {
                "id": step_id,
                "chat_id": self.chat_id,
                "turn_message_id": self.turn_message_id,
                "kind": kind,
                "name": name,
                "status": STATUS_IN_PROGRESS,
                "args_truncated": args_text,
                "args_was_truncated": args_trunc,
                "result_summary": None,
                "result_was_truncated": False,
                "error_message": None,
                "started_at": started,
                "ended_at": None,
            }
        )
        # "step_name", not "name": `name` is a reserved LogRecord attribute and
        # `extra` may not overwrite it — logging raises KeyError at INFO level,
        # which the caller's defensive except turned into step_id=None, leaving
        # every step permanently in_progress (no complete/error ever recorded).
        logger.info(
            "chat_steps.started",
            extra={"step_id": step_id, "chat_id": self.chat_id, "kind": kind, "step_name": name},
        )
        return step_id

    async def complete(self, step_id: str, result: Any = None) -> None:
        """Mark a step ``completed`` with its truncated result summary."""
        if self._statuses.get(step_id) in _TERMINAL_STATUSES:
            # Late completion after cancel — drop per R6.
            logger.info(
                "chat_steps.late_complete_dropped",
                extra={"step_id": step_id, "chat_id": self.chat_id},
            )
            return
        result_text, result_trunc = redact(result, kind="result")
        await self._terminate(
            step_id,
            status=STATUS_COMPLETED,
            result_summary=result_text,
            result_was_truncated=result_trunc,
            error_message=None,
        )

    async def error(self, step_id: str, exc) -> None:
        """Mark a step ``errored``. ``exc`` may be an Exception or a string."""
        if self._statuses.get(step_id) in _TERMINAL_STATUSES:
            logger.info(
                "chat_steps.late_error_dropped",
                extra={"step_id": step_id, "chat_id": self.chat_id},
            )
            return
        msg = str(exc) if exc is not None else "Unknown error"
        err_text, _ = redact(msg, kind="error")
        await self._terminate(
            step_id,
            status=STATUS_ERRORED,
            result_summary=None,
            result_was_truncated=False,
            error_message=err_text,
        )

    async def cancel_all_in_flight(self) -> None:
        """Mark every still-in-progress step as ``cancelled`` (FR-020/021).

        Defensive: each candidate is re-checked against the DB before being
        flipped to ``cancelled``. A row that already reached a terminal
        state in the DB (e.g. ``complete()`` raced ahead and updated the
        row before this code observed it) is skipped — we never overwrite
        a real terminal state with a cancellation marker.
        """
        # Snapshot first — _terminate mutates _in_flight.
        snapshot = list(self._in_flight.keys())
        for step_id in snapshot:
            # Re-check the DB. If the row is already in a terminal state,
            # respect that and clear the in-memory entry without emitting
            # a contradictory cancelled event.
            try:
                record = await self._steps.call_async(
                    self._steps.repository.get_step,
                    owner_id=self.user_id,
                    step_id=step_id,
                )
                status = None if record is None else record.status.value
                if status in _TERMINAL_STATUSES:
                    self._in_flight.pop(step_id, None)
                    self._statuses[step_id] = status
                    logger.info(
                        "chat_steps.cancel_skipped_already_terminal",
                        extra={"step_id": step_id, "status": status},
                    )
                    continue
            except Exception:  # pragma: no cover — defensive
                pass
            await self._terminate(
                step_id,
                status=STATUS_CANCELLED,
                result_summary=None,
                result_was_truncated=False,
                error_message=None,
            )

    def is_terminal(self, step_id: str) -> bool:
        """True once a step has reached any terminal state.

        Used by the orchestrator before integrating a tool result so
        late-arriving responses from cancelled steps are dropped per R6.
        """
        return self._statuses.get(step_id) in _TERMINAL_STATUSES

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _terminate(
        self,
        step_id: str,
        *,
        status: str,
        result_summary: Optional[str],
        result_was_truncated: bool,
        error_message: Optional[str],
    ) -> None:
        ended = _now_ms()
        record = None
        step_name = self._in_flight.get(step_id, "unknown")
        try:
            record = await self._steps.call_async(
                self._steps.repository.finish_step,
                owner_id=self.user_id,
                step_id=step_id,
                expected_status=STATUS_IN_PROGRESS,
                status=status,
                ended_at=ended,
                result_summary=result_summary,
                result_was_truncated=result_was_truncated,
                error_message=error_message,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.error(
                "chat_steps.terminate_persist_failed",
                extra={"step_id": step_id, "status": status, "error": str(exc)},
            )

        self._in_flight.pop(step_id, None)
        self._statuses[step_id] = status

        # The Plane mutation returns the canonical detached state.  Persistence
        # failures remain non-fatal to the caller and emit the bounded fallback
        # shape retained by the original progress-notification contract.
        payload = (
            _record_to_step(record)
            if record is not None
            else {
                "id": step_id,
                "chat_id": self.chat_id,
                "turn_message_id": self.turn_message_id,
                "kind": KIND_TOOL_CALL,
                "name": step_name,
                "status": status,
                "args_truncated": None,
                "args_was_truncated": False,
                "result_summary": result_summary,
                "result_was_truncated": result_was_truncated,
                "error_message": error_message,
                "started_at": ended,
                "ended_at": ended,
            }
        )

        await self._emit(payload)
        logger.info(
            "chat_steps.terminated",
            extra={"step_id": step_id, "status": status, "chat_id": self.chat_id},
        )

    async def _emit(self, step_payload: Dict[str, Any]) -> None:
        if self.websocket is None or self.safe_send is None:
            return
        try:
            envelope = {
                "type": "chat_step",
                "chat_id": self.chat_id,
                "step": step_payload,
            }
            sent = self.safe_send(self.websocket, json.dumps(envelope, default=str))
            if asyncio.iscoroutine(sent):
                await sent
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "chat_steps.emit_failed",
                extra={"chat_id": self.chat_id, "error": str(exc)},
            )
