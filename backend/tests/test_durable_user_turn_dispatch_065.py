"""Authenticated durable user-turn dispatcher proofs for Feature 065."""

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
    _CONNECTION_OPERATION_CONTEXT,
    _VOICE_REQUEST_CANCELLED_MESSAGE,
    _VOICE_REQUEST_FAILED_MESSAGE,
    _VOICE_REQUEST_SUCCEEDED_MESSAGE,
    _VoiceDispatchContext,
)
from orchestrator.tests.test_voice_bootstrap_065 import (
    _runner_services,
    _voice_turn,
)
from orchestrator.task_state import TaskState
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from shared.feature_flags import flags


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
        return (
            SimpleNamespace(
                operation=SimpleNamespace(
                    operation_id=work.operation_id,
                    chat_id=work.frame.chat_id,
                    request_generation=work.frame.request_generation,
                ),
                fence=SimpleNamespace(
                    operation_id=work.operation_id,
                    execution_generation=1,
                    execution_lease_token=uuid.uuid4(),
                ),
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


def _voice_outcome_coordinator() -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=8,
                queue_limit=0,
                max_wait_ms=0,
                config_revision="voice-outcome-065",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=8,
                queue_limit=8,
                max_wait_ms=5_000,
                config_revision="voice-outcome-065",
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.VOICE_INTERACTIVE,
                parent_class_name=AdmissionClass.INTERACTIVE,
                active_limit=4,
                queue_limit=0,
                max_wait_ms=0,
                config_revision="voice-outcome-065",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        operation_retention=timedelta(hours=24),
    )


def _claim_voice_outcome(
    coordinator: WorkAdmissionCoordinator,
    *,
    chat_id: str,
    connection_generation: uuid.UUID,
    request_generation: uuid.UUID,
) -> tuple[OperationOwner, Any]:
    owner = OperationOwner(OwnerScope.USER, USER_ID, None)
    submission_id = uuid.uuid4()
    admitted = coordinator.submit(
        OperationRequest(
            operation_kind="voice_chat_message",
            admission_class=AdmissionClass.VOICE_INTERACTIVE,
            owner=owner,
            submission_id=submission_id,
            idempotency_namespace="voice_chat_message",
            idempotency_key=str(submission_id),
            normalized_input_digest="b" * 64,
            chat_id=chat_id,
            parent_operation_id=None,
            connection_generation=connection_generation,
            request_generation=request_generation,
        )
    )
    assert admitted.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.VOICE_INTERACTIVE,
        admitted.operation_id,
    )
    assert claim is not None
    return owner, claim


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


def test_failed_voice_operation_summary_is_explicit_and_actionable() -> None:
    context: dict[str, Any] = {"operation_kind": "voice_chat_message"}
    token = _CONNECTION_OPERATION_CONTEXT.set(context)
    try:
        assert Orchestrator._remember_voice_operation_terminal_intent(
            TaskState.FAILED
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    intent = context["voice_terminal_intent"]
    assert intent.state is OperationState.FAILED
    assert intent.terminal_code == "operation_failed"
    assert intent.safe_summary == (
        "Voice request failed. This request did not complete. Review the "
        "error in the conversation, then try again; typed chat is still "
        "available."
    )


@pytest.mark.asyncio
async def test_voice_dispatch_retains_exact_authority_then_scrubs() -> None:
    origin = _Origin()
    runtime = _runtime(origin)
    work = _work("voice_chat_message")
    context = _context(origin)
    completed: list[uuid.UUID] = []
    observed: dict[str, Any] = {}

    class _Store:
        async def get(self, user_id: str) -> str:
            return f"user-config:{user_id}"

        async def get_system(self) -> str:
            return "system-config"

    class _Permissions:
        @staticmethod
        def is_tool_allowed(
            user_id: str, agent_id: str, tool_id: str
        ) -> bool:
            return (user_id, agent_id, tool_id) == (
                USER_ID,
                "agent-065",
                "tool-065",
            )

        @staticmethod
        def get_enabled_scope_names(
            user_id: str, agent_id: str
        ) -> list[str]:
            assert (user_id, agent_id) == (USER_ID, "agent-065")
            return ["tools:read"]

    class _Delegation:
        async def exchange_token_for_agent(
            self,
            raw_token: str,
            agent_id: str,
            allowed_tools: list[str],
            user_id: str,
            scopes: list[str],
        ) -> dict[str, str]:
            observed["exchange"] = (
                raw_token,
                agent_id,
                allowed_tools,
                user_id,
                scopes,
            )
            return {"access_token": "attenuated-token"}

    runtime._llm_store = _Store()
    runtime._CredentialSource = SimpleNamespace(USER="user", SYSTEM="system")
    runtime._build_llm_client = lambda config, source: (config, source)

    async def drain_notes() -> None:
        return None

    runtime._drain_llm_discard_notes = drain_notes
    runtime.agent_cards = {
        "agent-065": SimpleNamespace(
            skills=[SimpleNamespace(id="tool-065")]
        )
    }
    runtime.security_flags = {}
    runtime.tool_permissions = _Permissions()
    runtime.delegation = _Delegation()

    async def execute(
        _context: Any,
        accepted_work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        assert isinstance(websocket, DurableUserTurnWebSocket)
        assert websocket is accepted_work.runtime_websocket
        assert websocket is not origin
        assert runtime.ui_sessions[websocket] == accepted_work.auth_claims
        assert runtime.ui_sessions[websocket] is not accepted_work.auth_claims
        observed["socket"] = websocket
        observed["user_llm"] = await Orchestrator._resolve_llm_client_for(
            runtime, websocket
        )
        observed["system_llm"] = await Orchestrator._resolve_llm_client_for(
            runtime, None
        )
        observed["delegation"] = await Orchestrator._get_delegation_token(
            runtime,
            websocket,
            "agent-065",
            USER_ID,
        )

    async def complete(
        _context: Any, accepted_work: _ConnectionOperation
    ) -> None:
        completed.append(accepted_work.operation_id)

    async def unexpected_terminal(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("successful dispatch must not fail terminalization")

    runtime._run_connection_ui_operation = execute
    runtime._complete_connection_operation = complete
    runtime._terminalize_connection_operation = unexpected_terminal

    await Orchestrator._run_connection_operation(runtime, context, work)

    socket = observed["socket"]
    assert observed["user_llm"] == (
        f"user-config:{USER_ID}",
        "user",
    )
    assert observed["system_llm"] == ("system-config", "system")
    assert observed["delegation"] == "attenuated-token"
    assert observed["exchange"] == (
        RAW_TOKEN,
        "agent-065",
        ["tool-065"],
        USER_ID,
        ["tools:read"],
    )
    assert completed == [work.operation_id]
    assert work.auth_claims == {}
    assert work.runtime_websocket is None
    assert socket.llm_context_user_id is None
    assert socket not in runtime.ui_sessions
    assert socket not in runtime.rote._profiles
    assert RAW_TOKEN not in repr(work)
    assert RAW_TOKEN not in repr(socket)


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
    """Voice rejection bookkeeping must not shadow credential-save state."""

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


@pytest.mark.asyncio
async def test_committed_error_snapshot_uses_failed_operation_after_media_end() -> None:
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    peer_chat_id = str(uuid.uuid4())
    peer_connection_generation = uuid.uuid4()
    peer_request_generation = uuid.uuid4()
    peer_owner, peer_claim = _claim_voice_outcome(
        coordinator,
        chat_id=peer_chat_id,
        connection_generation=peer_connection_generation,
        request_generation=peer_request_generation,
    )
    coordinator.terminalize(
        claim.fence,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary="Task failed",
        retry_after_ms=None,
    )
    coordinator.terminalize(
        peer_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Conversation committed",
        retry_after_ms=None,
    )

    session_id = str(uuid.uuid4())
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=session_id,
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    peer = replace(
        _voice_turn(
            session_id=session_id,
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(peer_request_generation),
            result_commit_id=str(uuid.uuid4()),
        ),
        user_id=USER_ID,
        chat_id=peer_chat_id,
        operation_id=str(peer_claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn, peer)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    class Commits:
        def committed_assistant_content(self, **_kwargs: Any) -> Any:
            raise AssertionError(
                "a failed operation must not infer its outcome from error text"
            )

    runtime = object.__new__(Orchestrator)
    runtime.voice_services = services
    runtime.work_admission = coordinator
    runtime.conversation_commits = Commits()
    terminal_notices: list[tuple[Any, str]] = []

    async def publish_terminal_notice(
        terminal_turn: Any,
        *,
        message: str,
    ) -> None:
        terminal_notices.append((terminal_turn, message))

    runtime._broadcast_voice_turn_state = publish_terminal_notice
    stage = SimpleNamespace(
        sealed=True,
        committed=True,
        commit_id=result_commit_id,
        operation_fence=claim.fence,
        summary_text="This committed error text is not an outcome signal.",
    )
    voice_dispatch = _VoiceDispatchContext(
        admission=SimpleNamespace(turn=turn),
        connection_generation=str(connection_generation),
        origin=object(),
    )
    token = _CONNECTION_OPERATION_CONTEXT.set(
        {
            "operation": claim.operation,
            "owner": owner,
            "execution_fence": claim.fence,
        }
    )
    try:
        await Orchestrator._finish_voice_chat_dispatch(
            runtime,
            voice_dispatch=voice_dispatch,
            user_id=USER_ID,
            chat_id=chat_id,
            stage=stage,
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "failed"
    assert terminal.result_commit_id == result_commit_id
    assert terminal.recap_source == "terminal_status"
    assert terminal.terminal_at is not None
    assert terminal_notices == [
        (
            terminal,
            "Voice request failed. This request did not complete. Review the "
            "error in the conversation, then try again; typed chat is still "
            "available.",
        )
    ]
    assert repository.turns[peer.turn_id] == peer
    assert coordinator.query_operation(
        owner=peer_owner,
        operation_id=peer_claim.operation.operation_id,
    ).state is OperationState.COMPLETED
    assert media.calls == []


@pytest.mark.asyncio
async def test_committed_success_requires_completed_exact_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Conversation committed",
        retry_after_ms=None,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    class Commits:
        def committed_assistant_content(self, **kwargs: Any) -> Any:
            assert kwargs == {
                "commit_id": result_commit_id,
                "owner_user_id": USER_ID,
            }
            return [{"type": "text", "content": "The report is ready."}]

    monkeypatch.setattr(
        "personalization.phi_gate.get_phi_gate",
        lambda: SimpleNamespace(detect_for_notice=lambda _text: False),
    )
    runtime = object.__new__(Orchestrator)
    runtime.voice_services = services
    runtime.work_admission = coordinator
    runtime.conversation_commits = Commits()
    stage = SimpleNamespace(
        sealed=True,
        committed=True,
        commit_id=result_commit_id,
        operation_fence=claim.fence,
        summary_text="The report is ready.",
    )
    voice_dispatch = _VoiceDispatchContext(
        admission=SimpleNamespace(turn=turn),
        connection_generation=str(connection_generation),
        origin=object(),
    )
    token = _CONNECTION_OPERATION_CONTEXT.set(
        {
            "operation": claim.operation,
            "owner": owner,
            "execution_fence": claim.fence,
        }
    )
    try:
        await Orchestrator._finish_voice_chat_dispatch(
            runtime,
            voice_dispatch=voice_dispatch,
            user_id=USER_ID,
            chat_id=chat_id,
            stage=stage,
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "succeeded"
    assert terminal.result_commit_id == result_commit_id
    assert terminal.recap_source == "authoritative_summary"
    assert terminal.terminal_at is not None
    assert media.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_failure", "expected_voice_state"),
    ((False, "succeeded"), (True, "failed")),
)
async def test_connection_runner_finalizes_operation_before_voice_outcome(
    monkeypatch: pytest.MonkeyPatch,
    execution_failure: bool,
    expected_voice_state: str,
) -> None:
    origin = _Origin()
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    stage = SimpleNamespace(
        sealed=True,
        committed=True,
        commit_id=result_commit_id,
        operation_fence=claim.fence,
        summary_text="The requested report is ready.",
    )
    voice_dispatch = _VoiceDispatchContext(
        admission=SimpleNamespace(turn=turn),
        connection_generation=str(connection_generation),
        origin=object(),
    )
    events: list[str] = []
    content_reads: list[str] = []

    class Commits:
        def committed_assistant_content(self, **_kwargs: Any) -> Any:
            content_reads.append(result_commit_id)
            return [
                {"type": "text", "content": "The requested report is ready."}
            ]

    runtime = _runtime(origin)
    runtime.work_admission = coordinator
    runtime.voice_services = services
    runtime.conversation_commits = Commits()
    work = _work("voice_chat_message")
    work.owner = owner
    work.operation_id = claim.operation.operation_id
    work.frame = replace(
        work.frame,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    context = _context(origin)
    context.connection_generation = connection_generation

    async def claim_operation(
        _context: ConnectionContext,
        accepted_work: _ConnectionOperation,
    ) -> tuple[Any, None]:
        assert accepted_work.operation_id == claim.operation.operation_id
        return claim, None

    async def execute(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        assert isinstance(websocket, DurableUserTurnWebSocket)
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=voice_dispatch,
            user_id=USER_ID,
            chat_id=chat_id,
            stage=stage,
        )
        events.append("voice_deferred")
        if execution_failure:
            coordinator.terminalize(
                claim.fence,
                state=OperationState.FAILED,
                terminal_code="operation_failed",
                safe_summary="Task failed",
                retry_after_ms=None,
            )

    async def complete_operation(
        _context: ConnectionContext,
        accepted_work: _ConnectionOperation,
    ) -> Any:
        projection = coordinator.query_operation(
            owner=owner,
            operation_id=accepted_work.operation_id,
        )
        if projection.state is OperationState.RUNNING:
            projection = coordinator.terminalize(
                claim.fence,
                state=OperationState.COMPLETED,
                terminal_code=None,
                safe_summary="Completed",
                retry_after_ms=None,
            )
        events.append(f"operation_{projection.state.value}")
        assert repository.turns[turn.turn_id].state == "processing"
        return projection

    async def unexpected_terminal(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the normal runner must reconcile its exact operation")

    original_finish = services.finish_turn_announcements

    async def observe_finish(
        service: Any,
        turn_arg: Any,
        **kwargs: Any,
    ) -> Any:
        assert service is services
        projection = coordinator.query_operation(
            owner=owner,
            operation_id=claim.operation.operation_id,
        )
        assert projection.state in {
            OperationState.COMPLETED,
            OperationState.FAILED,
        }
        events.append(f"voice_{kwargs['terminal_kind']}")
        return await original_finish(turn_arg, **kwargs)

    monkeypatch.setattr(
        "personalization.phi_gate.get_phi_gate",
        lambda: SimpleNamespace(detect_for_notice=lambda _text: False),
    )
    runtime._claim_connection_operation = claim_operation
    runtime._run_connection_ui_operation = execute
    runtime._complete_connection_operation = complete_operation
    runtime._terminalize_connection_operation = unexpected_terminal
    monkeypatch.setattr(
        type(services),
        "finish_turn_announcements",
        observe_finish,
    )

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert events == [
        "voice_deferred",
        f"operation_{'failed' if execution_failure else 'completed'}",
        f"voice_{expected_voice_state}",
    ]
    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == expected_voice_state
    assert terminal.result_commit_id == result_commit_id
    assert terminal.terminal_at is not None
    assert content_reads == ([] if execution_failure else [result_commit_id])
    assert media.calls == []


@pytest.mark.asyncio
async def test_peer_terminal_projection_cannot_complete_another_voice_turn() -> None:
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    peer_chat_id = str(uuid.uuid4())
    peer_connection_generation = uuid.uuid4()
    peer_request_generation = uuid.uuid4()
    peer_owner, peer_claim = _claim_voice_outcome(
        coordinator,
        chat_id=peer_chat_id,
        connection_generation=peer_connection_generation,
        request_generation=peer_request_generation,
    )
    own_terminal = coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Conversation committed",
        retry_after_ms=None,
    )
    peer_terminal = coordinator.terminalize(
        peer_claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Conversation committed",
        retry_after_ms=None,
    )
    own_result = str(uuid.uuid4())
    peer_result = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=session_id,
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=own_result,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    peer = replace(
        _voice_turn(
            session_id=session_id,
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(peer_request_generation),
            result_commit_id=peer_result,
        ),
        user_id=USER_ID,
        chat_id=peer_chat_id,
        operation_id=str(peer_claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn, peer)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    class Commits:
        def committed_assistant_content(self, **_kwargs: Any) -> Any:
            raise AssertionError("a peer projection cannot authorize recap content")

    runtime = object.__new__(Orchestrator)
    runtime.voice_services = services
    runtime.work_admission = coordinator
    runtime.conversation_commits = Commits()
    stage = SimpleNamespace(
        sealed=True,
        committed=True,
        commit_id=own_result,
        operation_fence=claim.fence,
        summary_text="Own committed result",
    )
    context = {
        "operation": claim.operation,
        "owner": owner,
        "execution_fence": claim.fence,
        "operation_kind": "voice_chat_message",
    }
    token = _CONNECTION_OPERATION_CONTEXT.set(context)
    try:
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=_VoiceDispatchContext(
                admission=SimpleNamespace(turn=turn),
                connection_generation=str(connection_generation),
                origin=object(),
            ),
            user_id=USER_ID,
            chat_id=chat_id,
            stage=stage,
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    await runtime._finish_pending_voice_dispatch(context, peer_terminal)

    assert repository.turns[turn.turn_id] == turn
    assert turn.state == "processing"
    assert turn.terminal_at is None
    assert repository.turns[peer.turn_id] == peer
    assert own_terminal.operation_id != peer_terminal.operation_id
    assert coordinator.query_operation(
        owner=peer_owner,
        operation_id=peer_claim.operation.operation_id,
    ).state is OperationState.COMPLETED
    assert media.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provided_projection", (False, True))
async def test_nonterminal_projection_exhausts_bounded_retry_without_outcome(
    provided_projection: bool,
) -> None:
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    runtime = object.__new__(Orchestrator)
    runtime.voice_services = services
    runtime.work_admission = coordinator
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: []
    )
    stage = SimpleNamespace(
        sealed=True,
        committed=True,
        commit_id=result_commit_id,
        operation_fence=claim.fence,
        summary_text=None,
    )
    context = {
        "operation": claim.operation,
        "owner": owner,
        "execution_fence": claim.fence,
        "operation_kind": "voice_chat_message",
    }
    token = _CONNECTION_OPERATION_CONTEXT.set(context)
    try:
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=_VoiceDispatchContext(
                admission=SimpleNamespace(turn=turn),
                connection_generation=str(connection_generation),
                origin=object(),
            ),
            user_id=USER_ID,
            chat_id=chat_id,
            stage=stage,
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    original_call = runtime._call_work_admission
    query_calls = 0

    async def count_queries(method: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal query_calls
        if getattr(method, "__name__", "") == "query_operation":
            query_calls += 1
        return await original_call(method, *args, **kwargs)

    runtime._call_work_admission = count_queries
    await runtime._finish_pending_voice_dispatch(
        context,
        claim.operation if provided_projection else None,
    )

    assert query_calls == 2
    assert repository.turns[turn.turn_id] == turn
    assert turn.state == "processing"
    assert turn.terminal_at is None
    assert "voice_finalization" not in context
    assert media.calls == []


@pytest.mark.asyncio
async def test_transient_query_retries_exact_terminal_and_finalizes_once() -> None:
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    runtime = object.__new__(Orchestrator)
    runtime.work_admission = coordinator
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: []
    )
    stage = SimpleNamespace(
        sealed=False,
        committed=False,
        commit_id=result_commit_id,
        operation_fence=claim.fence,
        summary_text=None,
    )
    context = {
        "operation": claim.operation,
        "owner": owner,
        "execution_fence": claim.fence,
        "operation_kind": "voice_chat_message",
    }
    token = _CONNECTION_OPERATION_CONTEXT.set(context)
    try:
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=_VoiceDispatchContext(
                admission=SimpleNamespace(turn=turn),
                connection_generation=str(connection_generation),
                origin=object(),
            ),
            user_id=USER_ID,
            chat_id=chat_id,
            stage=stage,
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    coordinator.terminalize(
        claim.fence,
        state=OperationState.FAILED,
        terminal_code="operation_failed",
        safe_summary="Voice request failed",
        retry_after_ms=None,
    )
    original_call = runtime._call_work_admission
    failed_once = False
    query_calls = 0

    async def transient_failure(method: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failed_once, query_calls
        if getattr(method, "__name__", "") == "query_operation":
            query_calls += 1
            if not failed_once:
                failed_once = True
                raise RuntimeError("temporary operation authority outage")
        return await original_call(method, *args, **kwargs)

    finish_calls = 0

    class CountingVoiceServices:
        repository = services.repository

        async def finish_turn_announcements(
            self,
            voice_turn: Any,
            **kwargs: Any,
        ) -> Any:
            nonlocal finish_calls
            finish_calls += 1
            return await services.finish_turn_announcements(
                voice_turn,
                **kwargs,
            )

    runtime.voice_services = CountingVoiceServices()
    runtime._call_work_admission = transient_failure
    await runtime._finish_pending_voice_dispatch(context, None)

    assert failed_once is True
    assert query_calls == 2
    assert finish_calls == 1
    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "failed"
    assert terminal.terminal_at is not None
    assert "voice_finalization" not in context
    assert media.calls == []


@pytest.mark.asyncio
async def test_post_acceptance_delivery_error_still_finalizes_voice_turn() -> None:
    from orchestrator.conversation_publication import (
        ConversationPublicationStage,
        activate_conversation_publication,
        current_conversation_publication,
    )

    origin = _Origin()
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    stage = ConversationPublicationStage(
        history=object(),
        commit_id=result_commit_id,
        chat_id=chat_id,
        user_id=USER_ID,
        base_render_revision=0,
        next_render_revision=1,
        operation_fence=claim.fence,
        publication_role="assistant_result",
    )
    aborted: list[str] = []

    class Commits:
        def abort_commit(self, **kwargs: Any) -> None:
            assert kwargs == {
                "commit_id": result_commit_id,
                "owner_user_id": USER_ID,
            }
            aborted.append(result_commit_id)

    runtime = _runtime(origin)
    runtime.work_admission = coordinator
    runtime.voice_services = services
    runtime.conversation_commits = Commits()
    runtime._workspace_locks = {}
    runtime._LLMUnavailable = RuntimeError
    work = _work("voice_chat_message")
    work.owner = owner
    work.operation_id = claim.operation.operation_id
    work.frame = replace(
        work.frame,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    context = _context(origin)
    context.connection_generation = connection_generation
    voice_dispatch = _VoiceDispatchContext(
        admission=SimpleNamespace(turn=turn),
        connection_generation=str(connection_generation),
        origin=object(),
    )
    delivery_attempted = asyncio.Event()

    async def claim_operation(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
    ) -> tuple[Any, None]:
        return claim, None

    async def resolve_llm(_websocket: Any) -> tuple[object, object, object]:
        return object(), object(), object()

    async def begin_voice_publication(
        _websocket: Any,
        **_kwargs: Any,
    ) -> tuple[Any, ...]:
        token = activate_conversation_publication(stage)  # type: ignore[arg-type]
        return (
            stage,
            token,
            str(request_generation),
            SimpleNamespace(),
            {},
            turn,
            1,
        )

    async def fail_acceptance_delivery(*_args: Any, **_kwargs: Any) -> Any:
        delivery_attempted.set()
        raise RuntimeError("accepted snapshot delivery failed")

    async def unexpected_impl(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("model execution must not start after delivery failure")

    async def execute(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        await Orchestrator.handle_chat_message(
            runtime,
            websocket,
            "accepted voice request",
            chat_id,
            user_id=USER_ID,
            operation_context=_CONNECTION_OPERATION_CONTEXT.get(),
            voice_dispatch=voice_dispatch,
        )

    async def terminalize_operation(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str,
        retry_after_ms: int | None = None,
    ) -> Any:
        assert state is OperationState.FAILED
        return coordinator.terminalize(
            claim.fence,
            state=state,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
        )

    async def unexpected_complete(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("failed acceptance delivery cannot complete")

    runtime._claim_connection_operation = claim_operation
    runtime._resolve_llm_client_for = resolve_llm
    runtime._begin_voice_conversation_publication = begin_voice_publication
    runtime._deliver_accepted_voice_turn = fail_acceptance_delivery
    runtime._handle_chat_message_impl = unexpected_impl
    runtime._run_connection_ui_operation = execute
    runtime._terminalize_connection_operation = terminalize_operation
    runtime._complete_connection_operation = unexpected_complete

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert delivery_attempted.is_set()
    assert aborted == [result_commit_id]
    assert stage.sealed is True and stage.committed is False
    assert current_conversation_publication() is None
    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "failed"
    assert terminal.result_commit_id is None
    assert terminal.terminal_at is not None
    assert media.calls == []


@pytest.mark.asyncio
async def test_wrong_connection_generation_cannot_finalize_voice_turn() -> None:
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    terminal_operation = coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    wrong_generation = replace(
        terminal_operation,
        connection_generation=uuid.uuid4(),
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    runtime = object.__new__(Orchestrator)
    runtime.voice_services = services
    runtime.work_admission = coordinator
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("wrong connection identity cannot authorize recap")
        )
    )
    operation_context = {
        "operation": claim.operation,
        "owner": owner,
        "execution_fence": claim.fence,
        "operation_kind": "voice_chat_message",
    }
    await runtime._finish_voice_chat_dispatch(
        voice_dispatch=_VoiceDispatchContext(
            admission=SimpleNamespace(turn=turn),
            connection_generation=str(connection_generation),
            origin=object(),
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        stage=SimpleNamespace(
            sealed=True,
            committed=True,
            commit_id=result_commit_id,
            operation_fence=claim.fence,
            summary_text="Completed",
        ),
        operation_context=operation_context,
        operation_projection=wrong_generation,
    )

    assert repository.turns[turn.turn_id] == turn
    assert turn.state == "processing"
    assert turn.terminal_at is None
    assert media.calls == []


@pytest.mark.asyncio
async def test_uncommitted_cancelled_operation_closes_voice_as_cancelled() -> None:
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    terminal_operation = coordinator.terminalize(
        claim.fence,
        state=OperationState.CANCELLED,
        terminal_code="cancelled_by_user",
        safe_summary="Cancelled",
        retry_after_ms=None,
    )
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=str(uuid.uuid4()),
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    runtime = object.__new__(Orchestrator)
    runtime.voice_services = services
    runtime.work_admission = coordinator
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancelled work cannot authorize recap content")
        )
    )
    await runtime._finish_voice_chat_dispatch(
        voice_dispatch=_VoiceDispatchContext(
            admission=SimpleNamespace(turn=turn),
            connection_generation=str(connection_generation),
            origin=object(),
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        stage=SimpleNamespace(
            sealed=True,
            committed=False,
            commit_id=turn.result_commit_id,
            operation_fence=claim.fence,
            summary_text=None,
        ),
        operation_context={
            "operation": claim.operation,
            "owner": owner,
            "execution_fence": claim.fence,
        },
        operation_projection=terminal_operation,
    )

    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "cancelled"
    assert terminal.result_commit_id is None
    assert terminal.terminal_at is not None
    assert media.calls == []


@pytest.mark.asyncio
async def test_runner_terminalizes_exact_running_voice_operation_on_exit() -> None:
    origin = _Origin()
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=str(uuid.uuid4()),
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    runtime = _runtime(origin)
    runtime.work_admission = coordinator
    runtime.voice_services = services
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("incomplete work cannot authorize recap content")
        )
    )
    work = _work("voice_chat_message")
    work.owner = owner
    work.operation_id = claim.operation.operation_id
    work.frame = replace(
        work.frame,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    context = _context(origin)
    context.connection_generation = connection_generation
    terminalizations: list[tuple[OperationState, str | None]] = []

    async def claim_operation(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
    ) -> tuple[Any, None]:
        return claim, None

    async def execute(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        assert isinstance(websocket, DurableUserTurnWebSocket)
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=_VoiceDispatchContext(
                admission=SimpleNamespace(turn=turn),
                connection_generation=str(connection_generation),
                origin=object(),
            ),
            user_id=USER_ID,
            chat_id=chat_id,
            stage=SimpleNamespace(
                sealed=True,
                committed=False,
                commit_id=turn.result_commit_id,
                operation_fence=claim.fence,
                summary_text=None,
            ),
        )

    async def leave_running(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
    ) -> Any:
        return coordinator.query_operation(
            owner=owner,
            operation_id=claim.operation.operation_id,
        )

    async def terminalize(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str,
        retry_after_ms: int | None = None,
    ) -> Any:
        terminalizations.append((state, terminal_code))
        return coordinator.terminalize(
            claim.fence,
            state=state,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
        )

    runtime._claim_connection_operation = claim_operation
    runtime._run_connection_ui_operation = execute
    runtime._complete_connection_operation = leave_running
    runtime._terminalize_connection_operation = terminalize

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert terminalizations == [
        (OperationState.FAILED, "voice_dispatch_incomplete")
    ]
    assert coordinator.query_operation(
        owner=owner,
        operation_id=claim.operation.operation_id,
    ).state is OperationState.FAILED
    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "failed"
    assert terminal.result_commit_id is None
    assert media.calls == []


@pytest.mark.asyncio
async def test_lease_renewal_cannot_cancel_terminal_voice_recap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _Origin()
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    runtime = _runtime(origin)
    runtime.work_admission = coordinator
    runtime.voice_services = services
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: [
            {"type": "text", "content": "The report is ready."}
        ]
    )
    work = _work("voice_chat_message")
    work.owner = owner
    work.operation_id = claim.operation.operation_id
    work.frame = replace(
        work.frame,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    context = _context(origin)
    context.connection_generation = connection_generation
    recap_started = asyncio.Event()

    async def claim_operation(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
    ) -> tuple[Any, None]:
        return claim, None

    async def renewal_that_would_cancel_recap(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        _stop: asyncio.Event,
        worker: asyncio.Task[Any],
    ) -> None:
        await recap_started.wait()
        worker.cancel()

    async def execute(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        assert isinstance(websocket, DurableUserTurnWebSocket)
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=_VoiceDispatchContext(
                admission=SimpleNamespace(turn=turn),
                connection_generation=str(connection_generation),
                origin=object(),
            ),
            user_id=USER_ID,
            chat_id=chat_id,
            stage=SimpleNamespace(
                sealed=True,
                committed=True,
                commit_id=result_commit_id,
                operation_fence=claim.fence,
                summary_text="The report is ready.",
            ),
        )

    async def complete(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
    ) -> Any:
        return coordinator.terminalize(
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Completed",
            retry_after_ms=None,
        )

    original_finish = services.finish_turn_announcements

    async def slow_finish(
        service: Any,
        turn_arg: Any,
        **kwargs: Any,
    ) -> Any:
        assert service is services
        recap_started.set()
        await asyncio.sleep(0.01)
        return await original_finish(turn_arg, **kwargs)

    monkeypatch.setattr(
        "personalization.phi_gate.get_phi_gate",
        lambda: SimpleNamespace(detect_for_notice=lambda _text: False),
    )
    monkeypatch.setattr(
        type(services),
        "finish_turn_announcements",
        slow_finish,
    )
    runtime._claim_connection_operation = claim_operation
    runtime._renew_connection_lease = renewal_that_would_cancel_recap
    runtime._run_connection_ui_operation = execute
    runtime._complete_connection_operation = complete

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert recap_started.is_set()
    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "succeeded"
    assert terminal.result_commit_id == result_commit_id
    assert media.calls == []


@pytest.mark.asyncio
async def test_llm_none_terminalizes_voice_when_task_state_machine_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = _Origin()
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    class HistoryDb:
        @staticmethod
        def get_chat_agent(_chat_id: str) -> None:
            return None

        @staticmethod
        def get_user_disabled_agents(_user_id: str) -> list[str]:
            return []

    class History:
        db = HistoryDb()

        @staticmethod
        def get_file_mappings(
            _chat_id: str,
            *,
            user_id: str,
        ) -> list[Any]:
            assert user_id == USER_ID
            return []

        @staticmethod
        def get_chat(
            _chat_id: str,
            *,
            user_id: str,
        ) -> dict[str, Any]:
            assert user_id == USER_ID
            return {
                "messages": [
                    {"role": "user", "content": "Earlier request"},
                    {"role": "assistant", "content": "Earlier result"},
                ]
            }

    class Heartbeat:
        def cancel(self) -> None:
            return None

    runtime = _runtime(origin)
    runtime.work_admission = coordinator
    runtime.voice_services = services
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("LLM failure cannot authorize recap content")
        )
    )
    runtime.history = History()
    runtime.workspace = SimpleNamespace(
        alive_rows=lambda *_args, **_kwargs: asyncio.sleep(0, result=[])
    )
    runtime.agent_cards = {}
    runtime.agents = {}
    runtime.local_agents = {}
    runtime.security_flags = {}
    runtime._is_draft_agent = lambda _agent_id: False
    runtime.tool_permissions = SimpleNamespace(
        is_tool_allowed=lambda *_args, **_kwargs: False
    )
    runtime.personalization_service = SimpleNamespace(
        build_prompt_fragment=lambda *_args, **_kwargs: ""
    )
    runtime._chat_recorders = {}
    runtime._chain_budgets = {}
    runtime._active_request = {}
    runtime.cancelled_sessions = {}
    renders: list[list[dict[str, Any]]] = []

    async def safe_send(_websocket: Any, _payload: str) -> None:
        return None

    async def append_message(*_args: Any, **_kwargs: Any) -> int:
        return 1

    async def notify_phi(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def start_heartbeat(_websocket: Any) -> Heartbeat:
        return Heartbeat()

    async def call_llm(*_args: Any, **_kwargs: Any) -> tuple[None, None]:
        return None, None

    async def render(
        _websocket: Any,
        components: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> None:
        renders.append(components)

    runtime._safe_send = safe_send
    runtime._append_conversation_message = append_message
    runtime._notify_phi_if_detected = notify_phi
    runtime._start_heartbeat = start_heartbeat
    runtime._call_llm = call_llm
    runtime.send_ui_render = render
    work = _work("voice_chat_message")
    work.owner = owner
    work.operation_id = claim.operation.operation_id
    work.frame = replace(
        work.frame,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    context = _context(origin)
    context.connection_generation = connection_generation
    stage = SimpleNamespace(
        sealed=False,
        committed=False,
        commit_id=result_commit_id,
        user_id=USER_ID,
        operation_fence=claim.fence,
        publication_role="assistant_result",
        summary_text=None,
    )
    voice_dispatch = _VoiceDispatchContext(
        admission=SimpleNamespace(turn=turn),
        connection_generation=str(connection_generation),
        origin=object(),
    )
    terminalizations: list[tuple[OperationState, str | None]] = []

    async def claim_operation(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
    ) -> tuple[Any, None]:
        return claim, None

    async def execute(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=voice_dispatch,
            user_id=USER_ID,
            chat_id=chat_id,
            stage=stage,
        )
        await Orchestrator._handle_chat_message_impl(
            runtime,
            websocket,
            "voice request with empty provider response",
            chat_id,
            user_id=USER_ID,
            operation_context=_CONNECTION_OPERATION_CONTEXT.get(),
            voice_dispatch=voice_dispatch,
            conversation_stage=stage,
            conversation_request_generation=str(request_generation),
            conversation_server_initiated=True,
            voice_acceptance={"message_id": 1, "turn": turn},
            llm_preflight_complete=True,
        )

    async def terminalize(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str,
        retry_after_ms: int | None = None,
    ) -> Any:
        terminalizations.append((state, terminal_code))
        return coordinator.terminalize(
            claim.fence,
            state=state,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
        )

    async def unexpected_complete(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an empty LLM response cannot complete")

    monkeypatch.setitem(flags._flags, "task_state_machine", False)
    monkeypatch.setitem(flags._flags, "message_compaction", False)
    runtime._claim_connection_operation = claim_operation
    runtime._run_connection_ui_operation = execute
    runtime._terminalize_connection_operation = terminalize
    runtime._complete_connection_operation = unexpected_complete

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert terminalizations == [(OperationState.FAILED, "operation_failed")]
    assert coordinator.query_operation(
        owner=owner,
        operation_id=claim.operation.operation_id,
    ).state is OperationState.FAILED
    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "failed"
    assert terminal.result_commit_id is None
    assert any("Failed to get a response" in item[0]["message"] for item in renders)
    assert media.calls == []


@pytest.mark.asyncio
async def test_cancelled_terminal_intent_wins_without_legacy_task() -> None:
    origin = _Origin()
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=str(uuid.uuid4()),
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )
    runtime = _runtime(origin)
    runtime.work_admission = coordinator
    runtime.voice_services = services
    runtime.conversation_commits = SimpleNamespace()
    work = _work("voice_chat_message")
    work.owner = owner
    work.operation_id = claim.operation.operation_id
    work.frame = replace(
        work.frame,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    context = _context(origin)
    context.connection_generation = connection_generation

    async def claim_operation(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
    ) -> tuple[Any, None]:
        return claim, None

    async def execute(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        websocket: Any,
    ) -> None:
        assert isinstance(websocket, DurableUserTurnWebSocket)
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=_VoiceDispatchContext(
                admission=SimpleNamespace(turn=turn),
                connection_generation=str(connection_generation),
                origin=object(),
            ),
            user_id=USER_ID,
            chat_id=chat_id,
            stage=SimpleNamespace(
                sealed=False,
                committed=False,
                commit_id=turn.result_commit_id,
                operation_fence=claim.fence,
                summary_text=None,
            ),
        )
        assert runtime._remember_voice_operation_terminal_intent(
            TaskState.CANCELLED
        )

    async def terminalize(
        _context: ConnectionContext,
        _work: _ConnectionOperation,
        *,
        state: OperationState,
        terminal_code: str | None,
        safe_summary: str,
        retry_after_ms: int | None = None,
    ) -> Any:
        assert state is OperationState.CANCELLED
        assert terminal_code == "cancelled_by_user"
        assert safe_summary == (
            "Voice request cancelled. No completed result was produced. You "
            "can try again or keep using typed chat."
        )
        return coordinator.terminalize(
            claim.fence,
            state=state,
            terminal_code=terminal_code,
            safe_summary=safe_summary,
            retry_after_ms=retry_after_ms,
        )

    async def unexpected_complete(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("cancelled intent cannot complete")

    runtime._claim_connection_operation = claim_operation
    runtime._run_connection_ui_operation = execute
    runtime._terminalize_connection_operation = terminalize
    runtime._complete_connection_operation = unexpected_complete

    await Orchestrator._run_connection_operation(runtime, context, work)

    assert coordinator.query_operation(
        owner=owner,
        operation_id=claim.operation.operation_id,
    ).state is OperationState.CANCELLED
    terminal = repository.turns[turn.turn_id]
    assert terminal.state == terminal.terminal_kind == "cancelled"
    assert terminal.result_commit_id is None
    assert media.calls == []


@pytest.mark.asyncio
async def test_stale_runner_cannot_terminalize_or_announce_running_successor() -> None:
    origin = _Origin()
    coordinator = _voice_outcome_coordinator()
    chat_id = str(uuid.uuid4())
    connection_generation = uuid.uuid4()
    request_generation = uuid.uuid4()
    owner, claim = _claim_voice_outcome(
        coordinator,
        chat_id=chat_id,
        connection_generation=connection_generation,
        request_generation=request_generation,
    )
    successor = replace(
        claim.operation,
        execution_generation=claim.operation.execution_generation + 1,
        execution_lease_token=uuid.uuid4(),
        state_revision=claim.operation.state_revision + 1,
    )
    result_commit_id = str(uuid.uuid4())
    turn = replace(
        _voice_turn(
            session_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            client_turn_id=str(uuid.uuid4()),
            submission_id=str(uuid.uuid4()),
            request_generation=str(request_generation),
            result_commit_id=result_commit_id,
        ),
        user_id=USER_ID,
        chat_id=chat_id,
        operation_id=str(claim.operation.operation_id),
    )
    services, _clock, repository, media = _runner_services(turn)
    await services.handle_runtime_session_end(
        SimpleNamespace(session_id=turn.session_id, generation=1),  # type: ignore[arg-type]
        "worker_media_ended",
    )

    class SuccessorAuthority:
        @staticmethod
        def query_operation(**_kwargs: Any) -> Any:
            return successor

        @staticmethod
        def assert_current_execution(_fence: Any) -> None:
            raise StaleExecutionFenceError("old runner fence is stale")

    runtime = _runtime(origin)
    runtime.work_admission = SuccessorAuthority()
    runtime.voice_services = services
    runtime.conversation_commits = SimpleNamespace(
        committed_assistant_content=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale runner cannot authorize recap content")
        )
    )
    work = _work("voice_chat_message")
    work.owner = owner
    work.operation_id = claim.operation.operation_id
    work.fence = claim.fence
    work.frame = replace(
        work.frame,
        chat_id=chat_id,
        request_generation=request_generation,
    )
    connection_context = _context(origin)
    connection_context.connection_generation = connection_generation
    operation_context = {
        "operation": claim.operation,
        "owner": owner,
        "execution_fence": claim.fence,
        "operation_kind": "voice_chat_message",
        "connection_generation": connection_generation,
        "request_generation": request_generation,
    }
    token = _CONNECTION_OPERATION_CONTEXT.set(operation_context)
    try:
        assert runtime._defer_voice_chat_dispatch(
            voice_dispatch=_VoiceDispatchContext(
                admission=SimpleNamespace(turn=turn),
                connection_generation=str(connection_generation),
                origin=object(),
            ),
            user_id=USER_ID,
            chat_id=chat_id,
            stage=SimpleNamespace(
                sealed=True,
                committed=True,
                commit_id=result_commit_id,
                operation_fence=claim.fence,
                summary_text="Old runner result",
            ),
        )
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    async def unexpected_terminalize(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("stale runner cannot terminalize its successor")

    runtime._terminalize_connection_operation = unexpected_terminalize
    resolved = await runtime._reconcile_pending_voice_operation(
        operation_context,
        connection_context,
        work,
        None,
    )
    assert resolved is None
    await runtime._finish_pending_voice_dispatch(operation_context, resolved)

    assert repository.turns[turn.turn_id] == turn
    assert turn.state == "processing"
    assert turn.terminal_at is None
    assert "voice_finalization" not in operation_context
    assert media.calls == []
