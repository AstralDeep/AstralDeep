"""Tests for the authenticated durable user-turn dispatcher
(orchestrator/async_tasks.py, conversation_publication.py): voice-turn notices,
terminal reconciliation, and dispatch scrubbing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.async_tasks import DurableUserTurnWebSocket
from orchestrator.orchestrator import (
    ConnectionContext,
    Orchestrator,
    _ConnectionIngressFrame,
    _ConnectionOperation,
    _VOICE_REQUEST_CANCELLED_MESSAGE,
    _VOICE_REQUEST_FAILED_MESSAGE,
    _VOICE_REQUEST_SUCCEEDED_MESSAGE,
)
from orchestrator.tests.test_voice_bootstrap_065 import (
    _voice_turn,
)
from orchestrator.work_admission import (
    OperationOwner,
    OperationState,
    OwnerScope,
)


USER_ID = "durable-user-065"
RAW_TOKEN = "memory-only-delegation-token"


class _Origin:
    client = ("test", 65)


class _Rote:
    def __init__(self, origin: Any) -> None:
        self._profiles = {origin: object()}
        self.cleaned: list[Any] = []

    def get_profile(self, websocket: Any) -> Any:
        return self._profiles[websocket]

    def cleanup(self, websocket: Any) -> None:
        self.cleaned.append(websocket)
        self._profiles.pop(websocket, None)


def _frame(operation_kind: str) -> _ConnectionIngressFrame:
    chat_id = str(uuid.uuid4())
    request_generation = uuid.uuid4()
    return _ConnectionIngressFrame(
        raw="{}",
        parsed={"type": "ui_event", "action": "chat_message"},
        action="chat_message",
        surface=None,
        chat_id=chat_id,
        submission_id=uuid.uuid4(),
        request_generation=request_generation,
        normalized_digest="a" * 64,
        read_only=False,
        operation_kind=operation_kind,
        deadline_at_monotonic=None,
        deadline_at_utc=None,
    )


def _context(origin: Any) -> ConnectionContext:
    return ConnectionContext(
        websocket=origin,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.uuid4(),
        registered=True,
    )


def _work(operation_kind: str) -> _ConnectionOperation:
    return _ConnectionOperation(
        frame=_frame(operation_kind),
        owner=OperationOwner(OwnerScope.USER, USER_ID, None),
        operation_id=uuid.uuid4(),
        auth_claims={
            "sub": USER_ID,
            "preferred_username": "durable-user",
            "_raw_token": RAW_TOKEN,
        },
    )


def _runtime(origin: Any) -> Orchestrator:
    runtime = object.__new__(Orchestrator)
    runtime.ui_sessions = {origin: {"sub": USER_ID}}
    runtime.rote = _Rote(origin)

    async def progress(_context: Any, _work: Any) -> None:
        return None

    async def claim(_context: Any, work: _ConnectionOperation):
        fence = SimpleNamespace(
            operation_id=work.operation_id,
            execution_generation=1,
            execution_lease_token=uuid.uuid4(),
        )
        operation = SimpleNamespace(
            operation_id=work.operation_id,
            chat_id=work.frame.chat_id,
            request_generation=work.frame.request_generation,
            owner=work.owner,
            fence=fence,
            lease_lost=False,
        )
        _context.operations[work.operation_id] = operation
        return (
            SimpleNamespace(
                operation=operation,
                fence=fence,
            ),
            None,
        )

    async def renew(
        _context: Any,
        _work: Any,
        stop: asyncio.Event,
        _worker: asyncio.Task[Any],
    ) -> None:
        await stop.wait()

    runtime._emit_long_running_operation_phase = progress
    runtime._claim_connection_operation = claim
    runtime._renew_connection_lease = renew
    return runtime






@pytest.mark.asyncio
async def test_terminal_voice_turn_notice_is_connection_scoped_and_content_safe() -> None:
    first_socket = object()
    second_socket = object()
    other_user_socket = object()
    first_context = _context(first_socket)
    second_context = _context(second_socket)
    other_context = _context(other_user_socket)
    runtime = object.__new__(Orchestrator)
    runtime.ui_sessions = {
        first_socket: {"sub": USER_ID},
        second_socket: {"sub": USER_ID},
        other_user_socket: {"sub": "another-user"},
    }
    runtime._connection_contexts = {
        id(first_socket): first_context,
        id(second_socket): second_context,
        id(other_user_socket): other_context,
    }
    delivered: list[tuple[Any, dict[str, Any]]] = []

    async def safe_send(websocket: Any, raw: str) -> bool:
        delivered.append((websocket, json.loads(raw)))
        return True

    runtime._safe_send = safe_send
    turn = replace(
        _voice_turn(
            state="failed",
            announcement_sequence=3,
            result_commit_id=str(uuid.uuid4()),
        ),
        user_id=USER_ID,
        output_reason="ready",
    )

    await runtime._broadcast_voice_turn_state(
        turn,
        message=(
            "Voice request failed. This request did not complete. Review the "
            "error in the conversation, then try again; typed chat is still "
            "available."
        ),
    )

    assert {item[0] for item in delivered} == {first_socket, second_socket}
    generations = {
        item[0]: item[1]["connection_generation"] for item in delivered
    }
    assert generations == {
        first_socket: str(first_context.connection_generation),
        second_socket: str(second_context.connection_generation),
    }
    for _, frame in delivered:
        assert frame["type"] == "voice_turn_state"
        assert frame["state"] == "failed"
        assert frame["sequence"] == 3
        assert frame["message"].startswith("Voice request failed.")
        assert frame["message"].endswith("typed chat is still available.")
        assert frame["detected_language"] == "en"
        assert frame["spoken_output_policy"] == "full_recap"
        assert frame["output_reason"] == "ready"
        assert frame["sensitive_result_pending"] is False
        assert "summary" not in frame
        assert "error_body" not in frame


def test_succeeded_voice_turn_notice_preserves_result_bound_consent_identity() -> None:
    result_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            state="succeeded",
            announcement_sequence=4,
            result_commit_id=result_id,
            sensitivity="sensitive",
        ),
        output_reason="ready",
    )

    frame = Orchestrator._voice_turn_state_frame(
        turn,
        connection_generation=str(uuid.uuid4()),
        message="Request completed. The text result is available in the conversation.",
    )

    assert frame["state"] == "succeeded"
    assert frame["result_id"] == result_id
    assert frame["sensitive_result_pending"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_message"),
    [
        ("succeeded", _VOICE_REQUEST_SUCCEEDED_MESSAGE),
        ("cancelled", _VOICE_REQUEST_CANCELLED_MESSAGE),
        ("failed", _VOICE_REQUEST_FAILED_MESSAGE),
    ],
)
async def test_reconciled_terminal_turn_is_projected_to_current_clients(
    state: str,
    expected_message: str,
) -> None:
    runtime = object.__new__(Orchestrator)
    delivered: list[tuple[Any, str]] = []

    async def broadcast(turn: Any, *, message: str) -> None:
        delivered.append((turn, message))

    runtime._broadcast_voice_turn_state = broadcast
    turn = _voice_turn(state=state)

    await runtime._notify_reconciled_voice_terminal_turn(turn)

    assert delivered == [(turn, expected_message)]


@pytest.mark.asyncio
async def test_reconciled_nonterminal_turn_is_not_projected() -> None:
    runtime = object.__new__(Orchestrator)

    async def broadcast(_turn: Any, *, message: str) -> None:
        raise AssertionError(f"unexpected lifecycle projection: {message}")

    runtime._broadcast_voice_turn_state = broadcast

    await runtime._notify_reconciled_voice_terminal_turn(
        _voice_turn(state="processing")
    )


@pytest.mark.asyncio
async def test_voice_dispatch_cancellation_terminalizes_and_scrubs() -> None:
    origin = _Origin()
    runtime = _runtime(origin)
    work = _work("voice_chat_message")
    context = _context(origin)
    entered = asyncio.Event()
    terminal: list[tuple[OperationState, str | None]] = []
    observed_socket: list[DurableUserTurnWebSocket] = []

    async def execute(
        _context: Any,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        observed_socket.append(websocket)
        entered.set()
        await asyncio.Future()

    async def complete(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("cancelled dispatch must not complete")

    async def terminalize(
        _context: Any,
        _work: _ConnectionOperation,
        *,
        state: OperationState,
        terminal_code: str | None,
        **_kwargs: Any,
    ) -> None:
        terminal.append((state, terminal_code))

    runtime._run_connection_ui_operation = execute
    runtime._complete_connection_operation = complete
    runtime._terminalize_connection_operation = terminalize

    task = asyncio.create_task(
        Orchestrator._run_connection_operation(runtime, context, work)
    )
    await entered.wait()
    task.cancel()
    await task

    assert terminal == [
        (OperationState.CANCELLED, "cancelled_by_user")
    ]
    assert work.auth_claims == {}
    assert work.runtime_websocket is None
    assert observed_socket[0].llm_context_user_id is None
    assert observed_socket[0] not in runtime.ui_sessions
    assert observed_socket[0] not in runtime.rote._profiles


@pytest.mark.asyncio
async def test_voice_preacceptance_rejection_never_completes_operation() -> None:
    origin = _Origin()
    runtime = _runtime(origin)
    work = _work("voice_chat_message")
    context = _context(origin)
    sent: list[dict[str, Any]] = []
    terminal: list[tuple[OperationState, str | None, str | None]] = []
    scheduled: list[tuple[Any, str]] = []
    voice_origin = SimpleNamespace(
        session_id=str(uuid.uuid4()),
        generation=1,
        media_grant_revision=1,
        turn_id=str(uuid.uuid4()),
        client_turn_id=str(uuid.uuid4()),
    )

    async def safe_send(_websocket: Any, payload: str) -> bool:
        sent.append(json.loads(payload))
        return True

    async def execute(
        _context: Any,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        await Orchestrator._reject_voice_submission(
            runtime,
            websocket,
            user_id=USER_ID,
            origin=voice_origin,
            submission_id=str(work.frame.submission_id),
            request_generation=str(work.frame.request_generation),
            chat_id=str(work.frame.chat_id),
            connection_generation=str(context.connection_generation),
            reason="permission_denied",
            retry_policy="none",
        )

    async def unexpected_complete(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a rejected spoken turn must not complete")

    async def terminalize(
        _context: Any,
        _work: _ConnectionOperation,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str | None = None,
        **_kwargs: Any,
    ) -> None:
        terminal.append((state, terminal_code, safe_summary))

    rejected_turn = SimpleNamespace(turn_id=voice_origin.turn_id)

    class Repository:
        def reject_transcript(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(turn=rejected_turn)

    class Coordinator:
        async def emit_transcript_rejected(
            self,
            turn: Any,
            **_kwargs: Any,
        ) -> None:
            assert turn is rejected_turn

    def schedule_guidance(turn: Any, *, reason: str) -> None:
        scheduled.append((turn, reason))

    runtime.voice_services = SimpleNamespace(
        repository=Repository(),
        coordinator=Coordinator(),
        schedule_preacceptance_rejection=schedule_guidance,
    )
    runtime._safe_send = safe_send
    runtime._run_connection_ui_operation = execute
    runtime._complete_connection_operation = unexpected_complete
    runtime._terminalize_connection_operation = terminalize

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert sent[0]["type"] == "voice_submission_rejected"
    assert sent[0]["reason"] == "permission_denied"
    assert terminal == [
        (
            OperationState.FAILED,
            "permission_denied",
            "Voice request did not start because it is not authorized.",
        )
    ]
    assert scheduled == [(rejected_turn, "permission_denied")]


@pytest.mark.asyncio
async def test_llm_credential_operation_keeps_its_typed_context_separate() -> None:
    from llm_config import ws_handlers

    origin = _Origin()
    runtime = _runtime(origin)
    work = _work("llm_credential_save")
    work.frame = replace(
        work.frame,
        deadline_at_monotonic=float("inf"),
        deadline_at_utc=datetime.now(UTC) + timedelta(minutes=1),
    )
    context = _context(origin)
    completed_operation = SimpleNamespace(state=OperationState.COMPLETED)
    completed: list[uuid.UUID] = []

    runtime.work_admission = SimpleNamespace()

    async def handle_credential_operation(
        _context: Any,
        _work: _ConnectionOperation,
    ) -> None:
        active = ws_handlers._ACTIVE_LLM_CONFIG_OPERATION.get()
        assert active is not None
        active.completed_operation = completed_operation

    async def complete(
        _context: Any, accepted_work: _ConnectionOperation
    ) -> None:
        assert accepted_work.committed_operation is completed_operation
        completed.append(accepted_work.operation_id)

    async def unexpected_terminal(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("successful credential save must complete")

    runtime._handle_llm_credential_operation = handle_credential_operation
    runtime._complete_connection_operation = complete
    runtime._terminalize_connection_operation = unexpected_terminal

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert completed == [work.operation_id]
    assert work.auth_claims == {}
    assert ws_handlers._ACTIVE_LLM_CONFIG_OPERATION.get() is None


@pytest.mark.asyncio
async def test_typed_turn_uses_original_socket_without_durable_adapter() -> None:
    origin = _Origin()
    runtime = _runtime(origin)
    work = _work("connection_frame")
    work.auth_claims.clear()
    context = _context(origin)
    completed: list[uuid.UUID] = []

    async def execute(
        _context: Any,
        accepted_work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        assert websocket is origin
        assert accepted_work.runtime_websocket is None

    async def complete(
        _context: Any, accepted_work: _ConnectionOperation
    ) -> None:
        completed.append(accepted_work.operation_id)

    async def unexpected_terminal(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("typed compatibility path must complete")

    runtime._run_connection_ui_operation = execute
    runtime._complete_connection_operation = complete
    runtime._terminalize_connection_operation = unexpected_terminal

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert completed == [work.operation_id]
    assert runtime.ui_sessions == {origin: {"sub": USER_ID}}
    assert runtime.rote.cleaned == []









