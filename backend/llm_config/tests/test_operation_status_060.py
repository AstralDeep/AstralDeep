"""Feature 060 US5: durable, bounded LLM credential-save operations.

These tests intentionally exercise the backend-owned half of the Apple
first-login flow.  The client may render a local ``submitting`` projection,
but only this admission path may create ``accepted`` and terminal states.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astralplane.repositories.secrets import EncryptedLLMConfigRecord

import llm_config.probe as probe_module
import llm_config.ws_handlers as handlers
from orchestrator import llm_gate
from orchestrator.orchestrator import (
    ConnectionContext,
    Orchestrator,
    _ConnectionOperation,
)
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
from llm_config.user_store import LLMConfigCommitDeadlineExceeded


USER_ID = "first-login-owner"
API_KEY = "sk-operation-060-secret"


def _coordinator(*, active_limit: int = 2) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=None,
                active_limit=active_limit,
                queue_limit=8,
                max_wait_ms=30_000,
                config_revision="llm-operation-060",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
        operation_retention=timedelta(hours=24),
    )


def _user_owner(connection_scope_id: uuid.UUID | None = None) -> OperationOwner:
    return OperationOwner(
        owner_scope=OwnerScope.USER,
        owner_user_id=USER_ID,
        connection_scope_id=connection_scope_id,
    )


def _accepted_claim(
    coordinator: WorkAdmissionCoordinator,
    *,
    submission_id: uuid.UUID | None = None,
) -> tuple[OperationOwner, object, object]:
    owner = _user_owner(uuid.uuid4())
    submission_id = submission_id or uuid.uuid4()
    accepted = coordinator.submit(
        OperationRequest(
            operation_kind="llm_credential_save",
            admission_class=AdmissionClass.INTERACTIVE,
            owner=owner,
            submission_id=submission_id,
            idempotency_namespace="llm_credential_save",
            idempotency_key=str(submission_id),
            normalized_input_digest=("ab" * 32),
            chat_id=None,
            parent_operation_id=None,
            connection_generation=uuid.uuid4(),
            request_generation=uuid.uuid4(),
        )
    )
    assert accepted.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, accepted.operation_id
    )
    assert claim is not None
    return owner, accepted, claim


def _operation_runtime(**kwargs):
    runtime_type = getattr(handlers, "LLMConfigOperationContext", None)
    assert runtime_type is not None, (
        "EXPECTED RED (T073): ws_handlers must expose the credential-save "
        "operation context"
    )
    return runtime_type(**kwargs)


def _activate_runtime(runtime):
    active = getattr(handlers, "active_llm_config_operation", None)
    assert active is not None, (
        "EXPECTED RED (T073): credential Save needs one task-local operation "
        "identity"
    )
    return active(runtime)


def _config() -> dict[str, str]:
    return {
        "provider": "openai",
        "base_url": "https://ignored.example/v1",
        "model": "gpt-4o-mini",
        "api_key": API_KEY,
    }


def _ui_save(
    *,
    submission_id: uuid.UUID,
    request_generation: uuid.UUID,
) -> str:
    return json.dumps(
        {
            "type": "ui_event",
            "action": "chrome_llm_save",
            "payload": {
                "surface": "llm_settings",
                "submission_id": str(submission_id),
                "request_generation": str(request_generation),
                "fields": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "api_key": API_KEY,
                },
            },
        }
    )


def _context(websocket: object) -> ConnectionContext:
    return ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=time.monotonic() + 5,
        connection_generation=uuid.uuid4(),
        registered=True,
    )


def test_contract_uses_exact_eight_and_ten_second_server_bounds() -> None:
    assert probe_module.PROBE_TIMEOUT_SECONDS == 8.0
    assert getattr(handlers, "LLM_CREDENTIAL_ATTEMPT_TIMEOUT_SECONDS", None) == 10.0


@pytest.mark.asyncio
async def test_admission_refusal_uses_manifested_error_without_operation_id() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.runtime_observability = None
    orch._safe_send = AsyncMock(return_value=True)
    submission_id = uuid.uuid4()

    await orch._send_admission_refusal(
        object(),
        submission_id=submission_id,
        code="capacity_exceeded",
        retryable=True,
        retry_after_ms=750,
    )

    payload = json.loads(orch._safe_send.await_args.args[1])
    assert payload == {
        "type": "error",
        "submission_id": str(submission_id),
        "accepted": False,
        "code": "capacity_exceeded",
        "message": "The request could not be accepted right now.",
        "retryable": True,
        "retry_after_ms": 750,
    }
    assert "operation_id" not in payload


@pytest.mark.asyncio
async def test_provider_probe_has_a_hard_async_timeout(monkeypatch) -> None:
    class _Completions:
        @staticmethod
        def create(**_kwargs):
            time.sleep(0.25)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    class _OpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(probe_module, "OpenAI", _OpenAI)
    started = time.monotonic()

    ok, error_class, _message = await probe_module.probe_chat_completion(
        api_key=API_KEY,
        base_url="https://provider.example/v1",
        model="m",
        timeout=0.02,
    )

    assert time.monotonic() - started < 0.15
    assert (ok, error_class) == (False, "transport_error")


def test_same_submission_is_user_owned_and_reconciles_one_operation() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.work_admission = _coordinator()
    first_ws, second_ws = object(), object()
    first = _context(first_ws)
    second = _context(second_ws)
    orch.ui_sessions = {
        first_ws: {"sub": USER_ID},
        second_ws: {"sub": USER_ID},
    }
    submission_id = uuid.uuid4()
    request_generation = uuid.uuid4()
    raw = _ui_save(
        submission_id=submission_id,
        request_generation=request_generation,
    )
    first_frame = orch._connection_frame(first, raw, json.loads(raw))
    second_frame = orch._connection_frame(second, raw, json.loads(raw))
    assert first_frame is not None and second_frame is not None

    first_result = orch._submit_connection_batch(first, [first_frame])[0]
    second_result = orch._submit_connection_batch(second, [second_frame])[0]

    assert first_result[1].owner_scope is OwnerScope.USER
    assert first_result[1].owner_user_id == USER_ID
    assert first_result[1].connection_scope_id == first.connection_scope_id
    assert first_result[2].operation_id == second_result[2].operation_id
    projection = orch.work_admission.query_operation(
        owner=_user_owner(), operation_id=first_result[2].operation_id
    )
    assert projection.operation_kind == "llm_credential_save"
    assert projection.owner_scope is OwnerScope.USER


@pytest.mark.asyncio
async def test_same_submission_schedules_exactly_one_process_worker() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.work_admission = _coordinator()
    orch.runtime_observability = None
    orch._safe_send = AsyncMock(return_value=True)
    orch._interactive_capacity_event = None
    orch._interactive_capacity_revision = 0
    orch._reconnectable_operations = {}
    orch._reconnectable_operation_tasks = set()
    first_ws, second_ws = object(), object()
    first = _context(first_ws)
    second = _context(second_ws)
    orch.ui_sessions = {
        first_ws: {"sub": USER_ID},
        second_ws: {"sub": USER_ID},
    }
    submission_id = uuid.uuid4()
    request_generation = uuid.uuid4()
    raw = _ui_save(
        submission_id=submission_id,
        request_generation=request_generation,
    )
    for context in (first, second):
        frame = orch._connection_frame(context, raw, json.loads(raw))
        assert frame is not None
        context.ingress.append(frame)

    release = asyncio.Event()
    starts = 0

    async def _worker(_context, _work) -> None:
        nonlocal starts
        starts += 1
        await release.wait()

    orch._run_connection_operation = _worker
    await orch._connection_admission_pump(first)
    await asyncio.sleep(0)
    await orch._connection_admission_pump(second)
    await asyncio.sleep(0)

    assert starts == 1
    assert set(first.operations) == set(second.operations)
    assert len(orch._reconnectable_operations) == 1
    release.set()
    await asyncio.gather(
        *(tuple(first.operation_tasks) + tuple(second.operation_tasks)),
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_accepted_status_is_immediate_and_canonical() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.work_admission = _coordinator()
    orch.runtime_observability = None
    orch._safe_send = AsyncMock(return_value=True)
    websocket = object()
    context = _context(websocket)
    orch.ui_sessions = {websocket: {"sub": USER_ID}}
    submission_id = uuid.uuid4()
    raw = _ui_save(
        submission_id=submission_id,
        request_generation=uuid.uuid4(),
    )
    frame = orch._connection_frame(context, raw, json.loads(raw))
    assert frame is not None
    _frame, _owner, admission, _projection = orch._submit_connection_batch(
        context, [frame]
    )[0]

    await orch._send_operation_accepted(context, frame, admission)

    payload = json.loads(orch._safe_send.await_args.args[1])
    assert payload["type"] == "operation_status"
    assert payload["operation_id"] == str(admission.operation_id)
    assert payload["state"] == payload["phase"] == "accepted"
    assert payload["surface"] == "llm_settings"
    assert payload["terminal"] is payload["retryable"] is False
    assert API_KEY not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_result", "state", "code"),
    [
        ((False, "auth_failed", "HTTP 401"), OperationState.FAILED, "validation_failed"),
        ((False, "model_not_found", "HTTP 404"), OperationState.FAILED, "validation_failed"),
        (
            (False, "transport_error", "DNS resolution failed"),
            OperationState.RETRYABLE,
            "network_unavailable",
        ),
        (
            (False, "provider_unavailable", "HTTP 503"),
            OperationState.RETRYABLE,
            "provider_unavailable",
        ),
    ],
)
async def test_probe_failures_map_to_corrective_or_retryable_terminal(
    monkeypatch,
    store,
    fake_db,
    fake_recorder,
    safe_send,
    probe_result,
    state,
    code,
) -> None:
    async def _probe(**_kwargs):
        return probe_result

    monkeypatch.setattr(handlers, "probe_chat_completion", _probe)
    coordinator = _coordinator()
    _owner, _accepted, claim = _accepted_claim(coordinator)
    runtime = _operation_runtime(
        coordinator=coordinator,
        fence=claim.fence,
        deadline_at_monotonic=time.monotonic() + 10,
        deadline_at_utc=datetime.now(UTC) + timedelta(seconds=10),
        emit_phase=AsyncMock(),
        unlock_after_save=AsyncMock(),
    )
    failure_type = getattr(handlers, "LLMConfigOperationFailure", None)
    assert failure_type is not None

    with _activate_runtime(runtime), pytest.raises(failure_type) as captured:
        await handlers.handle_llm_config_set(
            safe_send=safe_send,
            websocket=object(),
            config=_config(),
            actor_user_id=USER_ID,
            auth_principal=USER_ID,
            store=store,
            recorder=fake_recorder,
        )

    assert captured.value.state is state
    assert captured.value.code == code
    assert fake_db.users == {}
    runtime.unlock_after_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_phases_then_fenced_persistence_and_unlock(
    monkeypatch,
    store,
    fake_db,
    fake_recorder,
    safe_send,
) -> None:
    async def _probe(**_kwargs):
        return True, None, None

    monkeypatch.setattr(handlers, "probe_chat_completion", _probe)
    coordinator = _coordinator()
    _owner, _accepted, claim = _accepted_claim(coordinator)
    phases: list[tuple[str, str, str]] = []

    async def _phase(state: str, phase: str, label: str) -> None:
        phases.append((state, phase, label))

    unlock = AsyncMock(return_value=True)
    runtime = _operation_runtime(
        coordinator=coordinator,
        fence=claim.fence,
        deadline_at_monotonic=time.monotonic() + 10,
        deadline_at_utc=datetime.now(UTC) + timedelta(seconds=10),
        emit_phase=_phase,
        unlock_after_save=unlock,
    )

    with _activate_runtime(runtime):
        saved = await handlers.handle_llm_config_set(
            safe_send=safe_send,
            websocket=object(),
            config=_config(),
            actor_user_id=USER_ID,
            auth_principal=USER_ID,
            store=store,
            recorder=fake_recorder,
        )

    assert saved is True
    assert [(state, phase) for state, phase, _label in phases] == [
        ("validating", "validating_credentials"),
        ("persisting", "saving_credentials"),
    ]
    assert fake_db.users[USER_ID]["api_key_enc"] != API_KEY
    projection = coordinator.query_operation(
        owner=_user_owner(), operation_id=claim.fence.operation_id
    )
    assert projection.state is OperationState.COMPLETED
    # UI projection belongs to the outer wrapper after it observes the durable
    # winner; the persistence handler cannot unlock or ack beforehand.
    unlock.assert_not_awaited()
    safe_send.assert_not_awaited()


def test_deadline_after_insert_rolls_back_before_completed_cas(store) -> None:
    """The final DB-time check and COMPLETED CAS share the insert transaction."""

    deadline = datetime.now(UTC) + timedelta(seconds=10)

    class _Transaction:
        def __init__(self) -> None:
            self.staged = None
            self.committed = None

        def fetch_one(self, *_args, **_kwargs):
            """Identify the production caller-owned Plane transaction seam."""

            return None

    transaction = _Transaction()

    class _Repository:
        @staticmethod
        def upsert_user_before_deadline(
            observed_transaction,
            *,
            owner_id,
            provider,
            base_url,
            model,
            api_key_ciphertext,
            deadline_at,
        ):
            assert observed_transaction is transaction
            transaction.staged = {
                "owner_id": owner_id,
                "provider": provider,
                "base_url": base_url,
                "model": model,
                "api_key_ciphertext": api_key_ciphertext,
            }
            # The deadline-fenced upsert won immediately before its bound, but
            # the detached Plane record proves database time crossed the bound
            # before the completed operation compare-and-set.
            completed_at = deadline_at + timedelta(microseconds=1)
            return EncryptedLLMConfigRecord(
                scope="user",
                owner_id=owner_id,
                provider=provider,
                base_url=base_url,
                model=model,
                api_key_ciphertext=api_key_ciphertext,
                updated_by=None,
                created_at=completed_at,
                updated_at=completed_at,
            )

    store._repository.repository = _Repository()

    class _Coordinator:
        terminalize_calls = 0

        @contextmanager
        def fenced_transaction(self, _fence):
            try:
                yield transaction
            except Exception:
                transaction.staged = None
                raise
            else:
                transaction.committed = transaction.staged

        def terminalize(self, *_args, **_kwargs):
            self.terminalize_calls += 1
            raise AssertionError("deadline loss must precede completed CAS")

    coordinator = _Coordinator()

    with pytest.raises(LLMConfigCommitDeadlineExceeded):
        store.set_fenced_sync(
            USER_ID,
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key=API_KEY,
            coordinator=coordinator,
            fence=object(),
            deadline_at_monotonic=time.monotonic() + 10,
            deadline_at_utc=deadline,
        )

    assert transaction.staged is None
    assert transaction.committed is None
    assert coordinator.terminalize_calls == 0


@pytest.mark.asyncio
async def test_terminal_fence_blocks_late_persistence_and_unlock(
    monkeypatch,
    store,
    fake_db,
    fake_recorder,
    safe_send,
) -> None:
    probe_finished = asyncio.Event()

    async def _probe(**_kwargs):
        await asyncio.sleep(0.03)
        probe_finished.set()
        return True, None, None

    monkeypatch.setattr(handlers, "probe_chat_completion", _probe)
    coordinator = _coordinator()
    _owner, _accepted, claim = _accepted_claim(coordinator)
    unlock = AsyncMock()
    runtime = _operation_runtime(
        coordinator=coordinator,
        fence=claim.fence,
        deadline_at_monotonic=time.monotonic() + 0.01,
        deadline_at_utc=datetime.now(UTC) + timedelta(seconds=0.01),
        emit_phase=AsyncMock(),
        unlock_after_save=unlock,
    )
    failure_type = getattr(handlers, "LLMConfigOperationFailure", None)
    assert failure_type is not None

    with _activate_runtime(runtime), pytest.raises(failure_type) as captured:
        await handlers.handle_llm_config_set(
            safe_send=safe_send,
            websocket=object(),
            config=_config(),
            actor_user_id=USER_ID,
            auth_principal=USER_ID,
            store=store,
            recorder=fake_recorder,
        )

    assert probe_finished.is_set()
    assert captured.value.state is OperationState.RETRYABLE
    assert captured.value.code == "deadline_exceeded"
    assert fake_db.users == {}
    unlock.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_rejects_a_worker_after_first_terminal(store, fake_db) -> None:
    coordinator = _coordinator()
    _owner, _accepted, claim = _accepted_claim(coordinator)
    coordinator.terminalize(
        claim.fence,
        state=OperationState.RETRYABLE,
        terminal_code="deadline_exceeded",
        safe_summary="Credential save timed out",
        retry_after_ms=None,
    )
    fenced_set = getattr(store, "set_fenced", None)
    assert fenced_set is not None, (
        "EXPECTED RED (T074): credential persistence must commit under the "
        "operation fence"
    )

    with pytest.raises(StaleExecutionFenceError):
        await fenced_set(
            USER_ID,
            provider="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key=API_KEY,
            coordinator=coordinator,
            fence=claim.fence,
            deadline_at_monotonic=time.monotonic() + 10,
            deadline_at_utc=datetime.now(UTC) + timedelta(seconds=10),
        )

    assert fake_db.users == {}


@pytest.mark.asyncio
async def test_disconnect_detaches_viewer_but_does_not_cancel_user_save() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.work_admission = _coordinator()
    orch.runtime_observability = None
    orch._interactive_capacity_event = None
    orch._interactive_capacity_revision = 0
    orch._connection_contexts = {}
    websocket = object()
    context = _context(websocket)
    orch._connection_contexts[id(websocket)] = context
    owner, accepted, _claim = _accepted_claim(orch.work_admission)
    release = asyncio.Event()

    async def _survivor() -> None:
        await release.wait()

    task = asyncio.create_task(_survivor())
    work = _ConnectionOperation(
        frame=SimpleNamespace(action="chrome_llm_save", surface="llm_settings"),
        owner=owner,
        operation_id=accepted.operation_id,
        task=task,
    )
    context.operations[accepted.operation_id] = work
    context.tracked_tasks.add(task)
    context.operation_tasks.add(task)

    await orch._drain_connection_context(context)

    assert task.cancelled() is False
    assert task.done() is False
    projection = orch.work_admission.query_operation(
        owner=_user_owner(), operation_id=accepted.operation_id
    )
    assert projection.state is OperationState.RUNNING
    release.set()
    await task


@pytest.mark.asyncio
async def test_disconnected_save_finishes_from_captured_user_authority(
    monkeypatch,
    store,
    fake_db,
    fake_recorder,
) -> None:
    async def _probe(**_kwargs):
        return True, None, None

    monkeypatch.setattr(handlers, "probe_chat_completion", _probe)
    orch = Orchestrator.__new__(Orchestrator)
    orch.work_admission = _coordinator()
    orch.runtime_observability = None
    orch._interactive_capacity_event = None
    orch._interactive_capacity_revision = 0
    orch._safe_send = AsyncMock(return_value=False)
    orch._llm_store = store
    orch.audit_recorder = fake_recorder
    orch._ws_llm_gated = {}
    websocket = object()
    context = _context(websocket)
    orch.ui_sessions = {
        websocket: {"sub": USER_ID, "preferred_username": "owner@example.test"}
    }
    raw = _ui_save(
        submission_id=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )
    frame = orch._connection_frame(context, raw, json.loads(raw))
    assert frame is not None
    _frame, owner, accepted, _projection = orch._submit_connection_batch(
        context, [frame]
    )[0]
    work = _ConnectionOperation(
        frame=frame,
        owner=owner,
        operation_id=accepted.operation_id,
        auth_principal="owner@example.test",
    )

    # The socket/session disappears after admission but before the worker's
    # first turn. The durable USER owner, not the connection, authorizes it.
    context.closing = True
    orch.ui_sessions.clear()
    await orch._run_connection_operation(context, work)

    projection = orch.work_admission.query_operation(
        owner=_user_owner(), operation_id=accepted.operation_id
    )
    assert projection.state is OperationState.COMPLETED
    assert fake_db.users[USER_ID]["model"] == "gpt-4o-mini"
    assert fake_db.users[USER_ID]["api_key_enc"] != API_KEY


@pytest.mark.asyncio
async def test_completed_cas_precedes_unlock_and_legacy_ack(
    monkeypatch,
    store,
    fake_db,
    fake_recorder,
) -> None:
    async def _probe(**_kwargs):
        return True, None, None

    monkeypatch.setattr(handlers, "probe_chat_completion", _probe)
    orch = Orchestrator.__new__(Orchestrator)
    orch.work_admission = _coordinator()
    orch.runtime_observability = None
    orch._interactive_capacity_event = None
    orch._interactive_capacity_revision = 0
    orch._llm_store = store
    orch.audit_recorder = fake_recorder
    orch._ws_llm_gated = {}
    websocket = object()
    context = _context(websocket)
    orch.ui_sessions = {websocket: {"sub": USER_ID}}
    events: list[str] = []

    async def _send(_websocket, raw: str) -> bool:
        payload = json.loads(raw)
        if payload.get("type") == "operation_status" and payload.get("terminal"):
            events.append(f"terminal:{payload['state']}")
        elif payload.get("type") == "llm_config_ack":
            events.append("ack")
        return True

    orch._safe_send = _send
    raw = _ui_save(
        submission_id=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )
    frame = orch._connection_frame(context, raw, json.loads(raw))
    assert frame is not None
    _frame, owner, accepted, _projection = orch._submit_connection_batch(
        context, [frame]
    )[0]
    work = _ConnectionOperation(
        frame=frame,
        owner=owner,
        operation_id=accepted.operation_id,
        auth_principal=USER_ID,
    )

    async def _unlock(_orch, _user_id, **_kwargs):
        projection = orch.work_admission.query_operation(
            owner=_user_owner(), operation_id=accepted.operation_id
        )
        events.append(f"unlock:{projection.state.value}")
        # Simulate the old deadline/watchdog path trying to win in the former
        # gap between unlock authorization and the completed CAS.
        late = orch.work_admission.terminalize(
            work.fence,
            state=OperationState.RETRYABLE,
            terminal_code="deadline_exceeded",
            safe_summary="Credential save timed out",
            retry_after_ms=None,
        )
        assert late.state is OperationState.COMPLETED
        return False

    monkeypatch.setattr(llm_gate, "unlock_after_save", _unlock)

    await orch._run_connection_operation(context, work)

    projection = orch.work_admission.query_operation(
        owner=_user_owner(), operation_id=accepted.operation_id
    )
    assert projection.state is OperationState.COMPLETED
    assert fake_db.users[USER_ID]["model"] == "gpt-4o-mini"
    assert events == ["terminal:completed", "unlock:completed", "ack"]


@pytest.mark.asyncio
async def test_whole_attempt_deadline_is_retryable_and_has_no_late_success() -> None:
    orch = Orchestrator.__new__(Orchestrator)
    orch.work_admission = _coordinator()
    orch.runtime_observability = None
    orch._interactive_capacity_event = None
    orch._interactive_capacity_revision = 0
    orch._safe_send = AsyncMock(return_value=True)
    websocket = object()
    context = _context(websocket)
    orch.ui_sessions = {websocket: {"sub": USER_ID}}
    completed_handler = False

    async def _slow_handler(_context, _work) -> None:
        nonlocal completed_handler
        await asyncio.sleep(0.2)
        completed_handler = True

    orch._handle_llm_credential_operation = _slow_handler
    owner, accepted, _projection = orch._submit_connection_batch(
        context,
        [
            SimpleNamespace(
                raw="{}",
                parsed={"payload": {}},
                action="chrome_llm_save",
                surface="llm_settings",
                chat_id=None,
                submission_id=uuid.uuid4(),
                request_generation=uuid.uuid4(),
                normalized_digest="ab" * 32,
                read_only=False,
                operation_kind="llm_credential_save",
                deadline_at_monotonic=time.monotonic() + 0.02,
                deadline_at_utc=datetime.now(UTC) + timedelta(seconds=0.02),
            )
        ],
    )[0][1:4]
    work = _ConnectionOperation(
        frame=SimpleNamespace(
            raw="{}",
            parsed={"payload": {}},
            action="chrome_llm_save",
            surface="llm_settings",
            chat_id=None,
            request_generation=uuid.uuid4(),
            deadline_at_monotonic=time.monotonic() + 0.02,
            deadline_at_utc=datetime.now(UTC) + timedelta(seconds=0.02),
            operation_kind="llm_credential_save",
        ),
        owner=owner,
        operation_id=accepted.operation_id,
    )

    await orch._run_connection_operation(context, work)
    await asyncio.sleep(0.03)

    projection = orch.work_admission.query_operation(
        owner=_user_owner(), operation_id=accepted.operation_id
    )
    assert projection.state is OperationState.RETRYABLE
    assert projection.terminal_code == "deadline_exceeded"
    assert completed_handler is False
    states = [
        json.loads(call.args[1]).get("state")
        for call in orch._safe_send.await_args_list
        if json.loads(call.args[1]).get("type") == "operation_status"
    ]
    assert states == ["retryable"]


@pytest.mark.asyncio
async def test_fenced_unlock_rejects_expired_or_terminal_execution() -> None:
    coordinator = _coordinator()
    _owner, _accepted, claim = _accepted_claim(coordinator)
    websocket = object()
    orch = SimpleNamespace(
        ui_sessions={websocket: {"sub": USER_ID}},
        _ws_llm_gated={id(websocket): True},
        _ws_active_chat={},
        _ws_welcome={},
        _safe_send=AsyncMock(return_value=True),
    )
    coordinator.terminalize(
        claim.fence,
        state=OperationState.RETRYABLE,
        terminal_code="deadline_exceeded",
        safe_summary="Credential save timed out",
        retry_after_ms=None,
    )

    with pytest.raises(StaleExecutionFenceError):
        await llm_gate.unlock_after_save(
            orch,
            USER_ID,
            coordinator=coordinator,
            fence=claim.fence,
            deadline_at_monotonic=time.monotonic() + 10,
        )

    assert orch._ws_llm_gated[id(websocket)] is True
    orch._safe_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_deadline_after_gate_pop_restores_marker_before_frames(
    monkeypatch,
) -> None:
    coordinator = _coordinator()
    owner, accepted, claim = _accepted_claim(coordinator)
    coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )

    class _Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = _Clock()

    class _DeadlineMap(dict):
        def pop(self, key, default=None):
            value = super().pop(key, default)
            clock.now = 2.0
            return value

    monkeypatch.setattr(llm_gate.time, "monotonic", clock.monotonic)
    websocket = object()
    gated = _DeadlineMap({id(websocket): True})
    orch = SimpleNamespace(
        ui_sessions={websocket: {"sub": USER_ID}},
        _ws_llm_gated=gated,
        _ws_active_chat={},
        _ws_welcome={},
        _safe_send=AsyncMock(return_value=True),
    )

    with pytest.raises(TimeoutError):
        await llm_gate.unlock_after_save(
            orch,
            USER_ID,
            coordinator=coordinator,
            completed_owner=owner,
            completed_operation_id=accepted.operation_id,
            deadline_at_monotonic=1.0,
        )

    assert gated[id(websocket)] is True
    orch._safe_send.assert_not_awaited()
