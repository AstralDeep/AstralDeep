"""Feature-065 contract tests for voice-turn work admission."""

from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from orchestrator.orchestrator import (
    ConnectionContext,
    Orchestrator,
    _ConnectionOperation,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    AdmissionConfigurationError,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    PostgresWorkAdmissionRepository,
    RefusedAdmission,
    WorkAdmissionCoordinator,
)


@dataclass
class _Clock:
    current: datetime = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


def _classes(*, shared_active_limit: int = 20) -> tuple[AdmissionClassConfig, ...]:
    return (
        AdmissionClassConfig(
            class_name=AdmissionClass.GLOBAL,
            parent_class_name=None,
            active_limit=shared_active_limit,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="test-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.INTERACTIVE,
            parent_class_name=AdmissionClass.GLOBAL,
            active_limit=shared_active_limit,
            queue_limit=100,
            max_wait_ms=5_000,
            config_revision="test-065",
        ),
        AdmissionClassConfig(
            class_name=AdmissionClass.VOICE_INTERACTIVE,
            parent_class_name=AdmissionClass.INTERACTIVE,
            active_limit=10,
            queue_limit=0,
            max_wait_ms=0,
            config_revision="test-065",
        ),
    )


def _coordinator(*, shared_active_limit: int = 20) -> WorkAdmissionCoordinator:
    return WorkAdmissionCoordinator(
        admission_classes=_classes(shared_active_limit=shared_active_limit),
        repository=InMemoryWorkAdmissionRepository(),
        clock=_Clock(),
        operation_retention=timedelta(hours=24),
    )


class _UnusedDatabase:
    def _get_connection(self) -> None:
        raise AssertionError("capacity refusal must not borrow a real connection")


class _RunningCountCursor:
    def __init__(self, running_count: int) -> None:
        self.running_count = running_count
        self.query = ""
        self.params: tuple[Any, ...] = ()

    def execute(self, query: str, params: tuple[Any, ...]) -> None:
        self.query = query
        self.params = params

    def fetchone(self) -> dict[str, int]:
        return {"running_count": self.running_count}


class _PostgresVoiceCapacityHarness(PostgresWorkAdmissionRepository):
    """Exercise the production per-user refusal branch without schema setup."""

    def __init__(self, running_count: int) -> None:
        super().__init__(_UnusedDatabase())
        self.cursor = _RunningCountCursor(running_count)
        self.refusal: dict[str, Any] | None = None
        self._configs = {config.class_name: config for config in _classes()}

    @contextmanager
    def _transaction(self) -> Iterator[_RunningCountCursor]:
        yield self.cursor

    @classmethod
    def _lock_request_identities(cls, cursor: Any, request: OperationRequest) -> None:
        del cursor, request

    def _submission_row(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    def _existing_idempotent_operation(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None

    def _lock_class_chain(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    @staticmethod
    def _expire_queued_locked(*args: Any, **kwargs: Any) -> tuple[()]:
        del args, kwargs
        return ()

    def _expire_execution_leases_locked(self, *args: Any, **kwargs: Any) -> tuple[()]:
        del args, kwargs
        return ()

    def _insert_submission(self, *args: Any, **kwargs: Any) -> None:
        del args
        self.refusal = kwargs

    def _select_free_slots(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("capacity must be refused before slot selection")


def _request(label: str, *, owner_user_id: str = "user-a") -> OperationRequest:
    submission_id = uuid.uuid4()
    return OperationRequest(
        operation_kind="voice_turn",
        admission_class=AdmissionClass.VOICE_INTERACTIVE,
        owner=OperationOwner(
            owner_scope=OwnerScope.USER,
            owner_user_id=owner_user_id,
            connection_scope_id=None,
        ),
        submission_id=submission_id,
        idempotency_namespace="voice_turn",
        idempotency_key=str(submission_id),
        normalized_input_digest=hashlib.sha256(label.encode()).hexdigest(),
        chat_id=f"chat-{label}",
        parent_operation_id=None,
        connection_generation=uuid.uuid4(),
        request_generation=uuid.uuid4(),
    )


def _runtime_context(
    websocket: object,
    connection_generation: str,
) -> ConnectionContext:
    return ConnectionContext(
        websocket=websocket,
        connection_scope_id=uuid.uuid4(),
        registration_deadline=999_999.0,
        connection_generation=uuid.UUID(connection_generation),
        registered=True,
    )


def _runtime_voice_frame(connection_generation: str) -> dict[str, Any]:
    submission_id = str(uuid.uuid4())
    request_generation = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    return {
        "type": "ui_event",
        "action": "chat_message",
        "session_id": chat_id,
        "submission_id": submission_id,
        "request_generation": request_generation,
        "connection_generation": connection_generation,
        "payload": {
            "chat_id": chat_id,
            "message": "Run the admitted request",
            "submission_id": submission_id,
            "request_generation": request_generation,
            "connection_generation": connection_generation,
            "voice_origin": {
                "schema_version": "1",
                "session_id": str(uuid.uuid4()),
                "generation": 1,
                "media_grant_revision": 1,
                "turn_id": str(uuid.uuid4()),
                "client_turn_id": str(uuid.uuid4()),
                "chat_context_revision": 1,
                "source_participant_identity": (
                    f"worker-{uuid.uuid4().hex}"
                ),
                "detected_language": "en",
                "text_digest_sha256": "a" * 64,
                "transcript_proof": "b" * 64,
                "proof_expires_at": (
                    datetime.now(UTC) + timedelta(minutes=1)
                ).isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
            },
        },
    }


def _runtime_orchestrator(
    websocket: object,
    *,
    user_id: str = "voice-owner",
) -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.work_admission = _coordinator()
    orchestrator.ui_sessions = {websocket: {"sub": user_id}}
    orchestrator._ws_active_chat = {}
    orchestrator.runtime_observability = None
    return orchestrator


def test_voice_class_is_a_no_queue_child_of_normal_interactive_capacity() -> None:
    coordinator = _coordinator()

    status = coordinator.inspect_admission_class(AdmissionClass.VOICE_INTERACTIVE)

    assert status.parent_class_name is AdmissionClass.INTERACTIVE
    assert status.active_limit == 10
    assert status.queue_limit == 0
    assert status.max_wait_ms == 0
    with pytest.raises(AdmissionConfigurationError, match="must not queue"):
        AdmissionClassConfig(
            class_name=AdmissionClass.VOICE_INTERACTIVE,
            parent_class_name=AdmissionClass.INTERACTIVE,
            active_limit=10,
            queue_limit=1,
            max_wait_ms=5_000,
            config_revision="invalid-065",
        )
    invalid_parent = list(_classes())
    invalid_parent[-1] = AdmissionClassConfig(
        class_name=AdmissionClass.VOICE_INTERACTIVE,
        parent_class_name=AdmissionClass.GLOBAL,
        active_limit=10,
        queue_limit=0,
        max_wait_ms=0,
        config_revision="invalid-parent-065",
    )
    with pytest.raises(AdmissionConfigurationError, match="child of interactive"):
        WorkAdmissionCoordinator(
            admission_classes=invalid_parent,
            repository=InMemoryWorkAdmissionRepository(),
            clock=_Clock(),
        )


def test_runtime_routes_only_voice_chat_to_voice_interactive_admission() -> None:
    websocket = object()
    connection_generation = str(uuid.uuid4())
    context = _runtime_context(websocket, connection_generation)
    orchestrator = _runtime_orchestrator(websocket)
    voice_raw = _runtime_voice_frame(connection_generation)
    voice_frame = orchestrator._connection_frame(
        context,
        json.dumps(voice_raw),
        voice_raw,
    )
    assert voice_frame is not None

    voice_result = orchestrator._submit_connection_batch(
        context,
        [voice_frame],
    )[0]

    assert voice_result[2].accepted is True
    assert voice_result[2].state is OperationState.RUNNING
    assert voice_result[3].admission_class is AdmissionClass.VOICE_INTERACTIVE
    assert voice_result[1].owner_scope is OwnerScope.USER
    assert voice_result[1].owner_user_id == "voice-owner"

    typed_raw = _runtime_voice_frame(connection_generation)
    typed_raw["payload"].pop("voice_origin")
    typed_frame = orchestrator._connection_frame(
        context,
        json.dumps(typed_raw),
        typed_raw,
    )
    assert typed_frame is not None

    typed_result = orchestrator._submit_connection_batch(
        context,
        [typed_frame],
    )[0]

    assert typed_result[2].accepted is True
    assert typed_result[3].admission_class is AdmissionClass.INTERACTIVE
    assert typed_result[1].owner_scope is OwnerScope.CONNECTION


@pytest.mark.asyncio
async def test_runtime_claims_voice_operation_from_voice_class() -> None:
    websocket = object()
    connection_generation = str(uuid.uuid4())
    context = _runtime_context(websocket, connection_generation)
    orchestrator = _runtime_orchestrator(websocket)
    raw = _runtime_voice_frame(connection_generation)
    frame = orchestrator._connection_frame(
        context,
        json.dumps(raw),
        raw,
    )
    assert frame is not None
    _frame, owner, admission, _projection = (
        orchestrator._submit_connection_batch(context, [frame])[0]
    )
    work = _ConnectionOperation(
        frame=frame,
        owner=owner,
        operation_id=admission.operation_id,
    )

    claim, terminal = await orchestrator._claim_connection_operation(
        context,
        work,
    )

    assert terminal is None
    assert claim is not None
    assert claim.operation.admission_class is AdmissionClass.VOICE_INTERACTIVE


@pytest.mark.asyncio
async def test_runtime_capacity_refusal_is_voice_correlated_before_acceptance(
) -> None:
    websocket = object()
    connection_generation = str(uuid.uuid4())
    context = _runtime_context(websocket, connection_generation)
    orchestrator = _runtime_orchestrator(websocket)
    sent: list[dict[str, Any]] = []
    persisted: list[dict[str, Any]] = []
    worker_rejections: list[dict[str, Any]] = []
    audible_rejections: list[dict[str, Any]] = []
    delivery_order: list[str] = []

    class Repository:
        def reject_transcript(self, **kwargs: Any) -> Any:
            persisted.append(kwargs)
            return SimpleNamespace(
                turn=SimpleNamespace(turn_id=kwargs["turn_id"])
            )

    class VoiceCoordinator:
        async def emit_transcript_rejected(
            self,
            turn: Any,
            **kwargs: Any,
        ) -> None:
            delivery_order.append("worker_clear")
            worker_rejections.append(
                {"turn_id": turn.turn_id, **kwargs}
            )

    def schedule_preacceptance_rejection(
        turn: Any,
        *,
        reason: str,
    ) -> None:
        delivery_order.append("audible_guidance")
        audible_rejections.append(
            {"turn_id": turn.turn_id, "reason": reason}
        )

    async def safe_send(_websocket: object, payload: str) -> bool:
        sent.append(json.loads(payload))
        return True

    orchestrator.voice_services = SimpleNamespace(
        repository=Repository(),
        coordinator=VoiceCoordinator(),
        schedule_preacceptance_rejection=schedule_preacceptance_rejection,
    )
    orchestrator._safe_send = safe_send

    for _index in range(2):
        raw = _runtime_voice_frame(connection_generation)
        frame = orchestrator._connection_frame(
            context,
            json.dumps(raw),
            raw,
        )
        assert frame is not None
        accepted = orchestrator._submit_connection_batch(
            context,
            [frame],
        )[0][2]
        assert accepted.accepted is True
        assert accepted.state is OperationState.RUNNING

    refused_raw = _runtime_voice_frame(connection_generation)
    refused_frame = orchestrator._connection_frame(
        context,
        json.dumps(refused_raw),
        refused_raw,
    )
    assert refused_frame is not None
    context.ingress.append(refused_frame)

    await orchestrator._connection_admission_pump(context)

    assert [item["type"] for item in sent] == [
        "voice_submission_rejected"
    ]
    rejection = sent[0]
    assert rejection["reason"] == "capacity_exhausted"
    assert rejection["retry_policy"] == "explicit_user_retry"
    assert rejection["turn_id"] == (
        refused_raw["payload"]["voice_origin"]["turn_id"]
    )
    assert rejection["submission_id"] == refused_raw["submission_id"]
    assert rejection["request_generation"] == (
        refused_raw["request_generation"]
    )
    assert rejection["chat_id"] == refused_raw["session_id"]
    assert all(item["type"] != "user_message_acked" for item in sent)
    assert persisted[0]["reason"] == "capacity_exhausted"
    assert persisted[0]["retry_policy"] == "explicit_user_retry"
    assert worker_rejections == [
        {
            "turn_id": rejection["turn_id"],
            "reason": "capacity_exhausted",
            "retry_policy": "explicit_user_retry",
        }
    ]
    assert audible_rejections == [
        {
            "turn_id": rejection["turn_id"],
            "reason": "capacity_exhausted",
        }
    ]
    assert delivery_order == ["worker_clear", "audible_guidance"]
    status = orchestrator.work_admission.inspect_admission_class(
        AdmissionClass.VOICE_INTERACTIVE
    )
    assert status.active_count == 2
    assert status.queued_count == 0


@pytest.mark.asyncio
async def test_runtime_identity_denial_uses_terminal_voice_disposition() -> None:
    websocket = object()
    connection_generation = str(uuid.uuid4())
    context = _runtime_context(websocket, connection_generation)
    orchestrator = _runtime_orchestrator(websocket)
    raw = _runtime_voice_frame(connection_generation)
    frame = orchestrator._connection_frame(
        context,
        json.dumps(raw),
        raw,
    )
    assert frame is not None
    rejected: list[dict[str, Any]] = []
    generic: list[dict[str, Any]] = []

    async def reject(_websocket: object, **kwargs: Any) -> None:
        rejected.append(kwargs)

    async def generic_refusal(
        _websocket: object,
        **kwargs: Any,
    ) -> None:
        generic.append(kwargs)

    orchestrator._reject_voice_submission = reject
    orchestrator._send_admission_refusal = generic_refusal

    correlated = await orchestrator._send_connection_admission_refusal(
        context,
        frame,
        code="idempotency_conflict",
        retryable=False,
    )

    assert correlated is True
    assert generic == []
    assert len(rejected) == 1
    assert rejected[0]["user_id"] == "voice-owner"
    assert rejected[0]["origin"].turn_id == (
        raw["payload"]["voice_origin"]["turn_id"]
    )
    assert rejected[0]["submission_id"] == raw["submission_id"]
    assert rejected[0]["request_generation"] == raw["request_generation"]
    assert rejected[0]["chat_id"] == raw["session_id"]
    assert rejected[0]["connection_generation"] == connection_generation
    assert rejected[0]["reason"] == "invalid_binding"
    assert rejected[0]["retry_policy"] == "none"


def test_third_running_voice_turn_for_one_user_is_refused_before_acceptance() -> None:
    coordinator = _coordinator()
    first = coordinator.submit(_request("first"))
    second = coordinator.submit(_request("second"))
    third_request = _request("third")

    third = coordinator.submit(third_request)

    assert first.accepted is True
    assert first.state is OperationState.RUNNING
    assert first.queue_position is None
    assert second.accepted is True
    assert second.state is OperationState.RUNNING
    assert second.queue_position is None
    assert isinstance(third, RefusedAdmission)
    assert third == RefusedAdmission(
        accepted=False,
        code="capacity_exceeded",
        retryable=True,
        retry_after_ms=1_000,
    )
    assert not hasattr(third, "operation_id")
    status = coordinator.inspect_admission_class(AdmissionClass.VOICE_INTERACTIVE)
    assert status.active_count == 2
    assert status.queued_count == 0
    assert (
        coordinator.reconcile_submission(
            owner=third_request.owner,
            submission_id=third_request.submission_id,
        )
        == third
    )


def test_voice_shared_capacity_exhaustion_is_refused_instead_of_queued() -> None:
    coordinator = _coordinator(shared_active_limit=1)
    first = coordinator.submit(_request("first", owner_user_id="user-a"))

    second = coordinator.submit(_request("second", owner_user_id="user-b"))

    assert first.accepted is True
    assert second == RefusedAdmission(False, "capacity_exceeded", True, 1_000)
    status = coordinator.inspect_admission_class(AdmissionClass.VOICE_INTERACTIVE)
    assert status.active_count == 1
    assert status.queued_count == 0


def test_postgres_refuses_per_user_capacity_before_selecting_or_inserting_work() -> (
    None
):
    repository = _PostgresVoiceCapacityHarness(running_count=2)
    request = _request("postgres-capacity")
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

    result = repository.submit(
        request,
        now=now,
        retention=timedelta(hours=24),
        slot_lease=timedelta(seconds=30),
    )

    assert result == RefusedAdmission(False, "capacity_exceeded", True, 1_000)
    assert "owner_user_id = %s" in repository.cursor.query
    assert "state = 'running'" in repository.cursor.query
    assert repository.cursor.params == (
        AdmissionClass.VOICE_INTERACTIVE.value,
        OwnerScope.USER.value,
        request.owner.owner_user_id,
    )
    assert repository.refusal == {
        "current_time": now,
        "retention": timedelta(hours=24),
        "refusal_code": "capacity_exceeded",
        "retryable": True,
        "retry_after_ms": 1_000,
    }


def test_voice_running_limit_is_partitioned_by_authenticated_user() -> None:
    coordinator = _coordinator()

    results = [
        coordinator.submit(_request(f"{user}-{index}", owner_user_id=user))
        for user in ("user-a", "user-b")
        for index in range(2)
    ]

    assert all(result.accepted for result in results)
    assert all(result.state is OperationState.RUNNING for result in results)
    assert (
        coordinator.inspect_admission_class(
            AdmissionClass.VOICE_INTERACTIVE
        ).active_count
        == 4
    )
    assert coordinator.submit(_request("a-third", owner_user_id="user-a")) == (
        RefusedAdmission(False, "capacity_exceeded", True, 1_000)
    )
    assert coordinator.submit(_request("b-third", owner_user_id="user-b")) == (
        RefusedAdmission(False, "capacity_exceeded", True, 1_000)
    )


def test_terminal_voice_turn_releases_one_user_capacity_without_a_queue() -> None:
    coordinator = _coordinator()
    first = coordinator.submit(_request("first"))
    assert first.accepted
    second = coordinator.submit(_request("second"))
    assert second.accepted
    refused_request = _request("refused")
    refused = coordinator.submit(refused_request)
    assert not refused.accepted
    claim = coordinator.claim_operation(
        AdmissionClass.VOICE_INTERACTIVE, first.operation_id
    )
    assert claim is not None

    coordinator.terminalize(
        claim.fence,
        state=OperationState.COMPLETED,
        terminal_code=None,
        safe_summary="Completed",
        retry_after_ms=None,
    )
    assert coordinator.submit(refused_request) == refused
    replacement = coordinator.submit(_request("replacement"))

    assert replacement.accepted is True
    assert replacement.state is OperationState.RUNNING
    status = coordinator.inspect_admission_class(AdmissionClass.VOICE_INTERACTIVE)
    assert status.active_count == 2
    assert status.queued_count == 0


def test_concurrent_voice_submissions_cannot_oversubscribe_one_user() -> None:
    coordinator = _coordinator()
    requests = tuple(_request(f"race-{index}") for index in range(12))

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        results = tuple(executor.map(coordinator.submit, requests))

    accepted = tuple(result for result in results if result.accepted)
    refused = tuple(result for result in results if not result.accepted)
    assert len(accepted) == 2
    assert all(result.state is OperationState.RUNNING for result in accepted)
    assert len(refused) == 10
    assert all(result.code == "capacity_exceeded" for result in refused)
    status = coordinator.inspect_admission_class(AdmissionClass.VOICE_INTERACTIVE)
    assert status.active_count == 2
    assert status.queued_count == 0


def test_voice_admission_requires_authenticated_user_ownership() -> None:
    request = _request("invalid-owner")
    with pytest.raises(ValueError, match="user ownership"):
        OperationRequest(
            operation_kind=request.operation_kind,
            admission_class=request.admission_class,
            owner=OperationOwner(
                owner_scope=OwnerScope.SYSTEM,
                owner_user_id=None,
                connection_scope_id=None,
            ),
            submission_id=request.submission_id,
            idempotency_namespace=request.idempotency_namespace,
            idempotency_key=request.idempotency_key,
            normalized_input_digest=request.normalized_input_digest,
            chat_id=request.chat_id,
            parent_operation_id=request.parent_operation_id,
            connection_generation=request.connection_generation,
            request_generation=request.request_generation,
        )
