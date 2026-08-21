"""Focused production-adapter tests for the feature-060 BYO runtime.

The PostgreSQL state machines have their own fault-injection suites.  These
tests pin the orchestration ordering at the boundary where durable transitions
are projected onto live sockets, so a future refactor cannot accidentally make
an in-memory map or a WebSocket acknowledgement authoritative.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astralplane import canonical_generated_agent_manifest_digest

import orchestrator.orchestrator as runtime
import orchestrator.user_agents as user_agents
from orchestrator.agent_constitution import USER_AGENT_POLICY_REVISION
from orchestrator.agent_generator import BYO_RUNTIME_CONTRACT_VERSION
from orchestrator.agent_lifecycle import (
    AgentRevisionActivator,
    CandidateAgentMetadata,
    PhysicalStopReceipt,
    RecoveryPlan,
    RevisionActivationError,
    RevisionActivationRecoveryPendingError,
    RevisionRecoveryStatus,
)
from orchestrator.orchestrator import Orchestrator
from orchestrator.user_agents import (
    AgentTombstone,
    HostInventoryAction,
    HostInventoryReconciliation,
    HostInventorySelectedDelivery,
    HostSessionRecord,
    StaleRuntimeGenerationError,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    ExecutionFence,
    InMemoryWorkAdmissionRepository,
    OperationState,
    StaleExecutionFenceError,
    WorkAdmissionCoordinator,
)
from shared.protocol import AgentHostRegistration, ProtocolValidationError, RuntimeFence
from shared.protocol import CandidateCapabilityMap


def _uuid() -> str:
    return str(uuid.uuid4())


def _host_record() -> HostSessionRecord:
    now = datetime.now(UTC)
    return HostSessionRecord(
        host_session_id=_uuid(),
        host_id=_uuid(),
        owner_user_id="owner-060",
        connection_scope_id=_uuid(),
        platform="windows",
        client_version="0.4.0",
        host_generation=1,
        supersedes_session_id=None,
        supported_runtime_contract_versions=(BYO_RUNTIME_CONTRACT_VERSION,),
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        release_lock_digest="a" * 64,
        state="connected",
        inventory_state="pending",
        eligible_since=now,
        accepted_at=now,
        last_seen_at=now,
        disconnected_at=None,
        inventory_reconciled_at=None,
        failure_code=None,
    )


def _candidate_metadata(*, draft_id: str | None = None) -> CandidateAgentMetadata:
    return CandidateAgentMetadata(
        draft_id=draft_id or _uuid(),
        draft_state_revision=7,
        display_name="Recovery Test Agent",
        constitution_version="constitution-test-v1",
        validated_policy_revision=USER_AGENT_POLICY_REVISION,
        declared_tools=(),
        declared_scopes=(),
        declared_egress=(),
    )


def _revision_delivery_args(
    *,
    owner_id: str,
    agent_id: str,
    revision_id: str,
    agent_metadata: CandidateAgentMetadata | None = None,
) -> dict:
    bundle_sha256 = "b" * 64
    return {
        "owner_sub": owner_id,
        "agent_id": agent_id,
        "files": {"agent_main.py": ""},
        "runtime_manifest": {
            "agent_id": agent_id,
            "revision_id": revision_id,
            "bundle_sha256": bundle_sha256,
        },
        "bundle_sha256": bundle_sha256,
        "revision_id": revision_id,
        "artifact_relative_path": f"revisions/{agent_id}/{revision_id}",
        "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
        "required_runtime_lock_sha256": "a" * 64,
        "agent_metadata": agent_metadata or _candidate_metadata(),
    }


def _online_runtime(
    *,
    agent_id: str,
    revision_id: str,
    runtime_instance_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        fence=RuntimeFence(
            agent_id=agent_id,
            host_id=_uuid(),
            host_session_id=_uuid(),
            delivery_id=_uuid(),
            revision_id=revision_id,
            runtime_instance_id=runtime_instance_id or _uuid(),
            process_id=_uuid(),
            lifecycle_generation=11,
        )
    )


class _RecordingExitWaiters(dict):
    """Remember exact waiter entries even when production removes them."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[object] = []

    def __setitem__(self, key: str, value: object) -> None:
        self.recorded.append(value)
        super().__setitem__(key, value)


def _stopping_runtime_orchestrator() -> tuple[
    Orchestrator,
    object,
    SimpleNamespace,
]:
    record = _host_record()
    fence = RuntimeFence(
        agent_id="agent-stop-060",
        host_id=record.host_id,
        host_session_id=record.host_session_id,
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=17,
    )
    instance = SimpleNamespace(
        fence=fence,
        state="stopping",
        failure_code=None,
    )

    class Repository:
        def __init__(self):
            self.orchestrator = None
            self.physical_exit_calls: list[tuple[RuntimeFence, str]] = []
            self.settled_request_ids: tuple[str, ...] = ()

        def get_runtime_instance(self, runtime_instance_id):
            assert runtime_instance_id == fence.runtime_instance_id
            return instance

        def record_runtime_physical_exit(self, recorded_fence, *, proof_code):
            assert recorded_fence == fence
            waiter = self.orchestrator._personal_agent_exit_waiters.get(
                fence.runtime_instance_id
            )
            if waiter is not None:
                assert not waiter.acknowledged.done()
            self.physical_exit_calls.append((recorded_fence, proof_code))
            instance.state = "offline"
            instance.failure_code = proof_code
            return SimpleNamespace(
                instance=instance,
                settled_request_ids=self.settled_request_ids,
                settlement_code=None,
            )

    websocket = object()
    orchestrator = Orchestrator.__new__(Orchestrator)
    repository = Repository()
    repository.orchestrator = orchestrator
    orchestrator.personal_agent_runtime = repository
    orchestrator._personal_agent_host_sessions = {id(websocket): record}
    orchestrator._personal_agent_session_sockets = {
        record.host_session_id: websocket
    }
    orchestrator._personal_agent_exit_waiters = _RecordingExitWaiters()
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_request_waiters = {}
    orchestrator._personal_agent_request_runtime_fences = {}
    orchestrator._personal_agent_runtime_sockets = {}
    orchestrator._personal_agent_runtime_authorities = {}
    orchestrator.agents = {}
    orchestrator._terminalize_personal_agent_runtime = AsyncMock()
    orchestrator._safe_send = AsyncMock(return_value=True)
    return orchestrator, websocket, instance


def _runtime_exit_frame(
    fence: RuntimeFence,
    *,
    exit_kind: str = "explicit_stop",
) -> dict:
    return {
        "type": "agent_runtime_exit",
        "fence": fence.to_dict(),
        "exit_kind": exit_kind,
        "exit_code": 0 if exit_kind == "process_exit" else None,
    }


async def _wait_for_exit_waiter(
    orchestrator: Orchestrator,
    runtime_instance_id: str,
) -> object:
    async def _wait() -> object:
        while runtime_instance_id not in orchestrator._personal_agent_exit_waiters:
            await asyncio.sleep(0)
        return orchestrator._personal_agent_exit_waiters[runtime_instance_id]

    return await asyncio.wait_for(_wait(), timeout=1.0)


def _revision_delivery_orchestrator(
    *,
    runtime_repository: object,
    revision_store: object,
) -> Orchestrator:
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    operation_owner = SimpleNamespace(owner_user_id="owner-060")
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._personal_agent_activation_locks = {}
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_session_sockets = {}
    orchestrator.personal_agent_runtime = runtime_repository
    orchestrator.personal_agent_revisions = revision_store
    orchestrator.work_admission = SimpleNamespace(
        expire_execution_leases=object(),
        terminalize=object(),
        query_operation=object(),
    )

    async def call_work_admission(callback, *args, **kwargs):
        if callback is orchestrator.work_admission.expire_execution_leases:
            return None
        if callback is orchestrator.work_admission.terminalize:
            return SimpleNamespace(
                operation_id=operation_fence.operation_id,
                state=kwargs["state"],
                terminal_code=kwargs["terminal_code"],
            )
        raise AssertionError("unexpected owner-scoped operation query")

    orchestrator._call_work_admission = AsyncMock(side_effect=call_work_admission)
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(
            operation_owner,
            SimpleNamespace(fence=operation_fence),
        )
    )
    orchestrator._admit_personal_agent_runtime_authority = AsyncMock()
    orchestrator._publish_personal_agent_runtime = AsyncMock()
    orchestrator._audit_user_agent = AsyncMock()
    orchestrator.agents = {}
    return orchestrator


def _personal_agent_operation_orchestrator() -> tuple[
    Orchestrator,
    WorkAdmissionCoordinator,
]:
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.BACKGROUND,
                parent_class_name=None,
                active_limit=1,
                queue_limit=2,
                max_wait_ms=5_000,
                config_revision="personal-agent-retry-test",
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
        operation_retention=timedelta(hours=1),
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.work_admission = coordinator

    async def call(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    orchestrator._call_work_admission = call
    return orchestrator, coordinator


def _personal_agent_operation_kwargs() -> dict:
    return {
        "owner_user_id": "owner-060",
        "operation_kind": "agent_runtime_delivery",
        "idempotency_namespace": "personal_agent_revision_delivery",
        "idempotency_key": "agent-060:revision-060",
        "normalized_identity": {
            "agent_id": "agent-060",
            "revision_id": "revision-060",
            "bundle_sha256": "b" * 64,
            "runtime_manifest_sha256": "c" * 64,
            "agent_metadata_sha256": "d" * 64,
        },
        "wait_seconds": 0.01,
        "retry_terminal": True,
    }


@pytest.mark.asyncio
async def test_retryable_delivery_allocates_deterministic_child_attempts():
    orchestrator, coordinator = _personal_agent_operation_orchestrator()
    kwargs = _personal_agent_operation_kwargs()

    owner, first = await orchestrator._claim_personal_agent_operation(**kwargs)
    coordinator.terminalize(
        first.fence,
        state=OperationState.RETRYABLE,
        terminal_code="capacity_exceeded",
        safe_summary="Retry this exact immutable delivery",
        retry_after_ms=0,
    )
    _owner, second = await orchestrator._claim_personal_agent_operation(**kwargs)
    second_record = coordinator.repository.get_operation_for_administration(
        second.fence.operation_id
    )

    assert second.fence.operation_id != first.fence.operation_id
    assert second_record is not None
    assert second_record.parent_operation_id == first.fence.operation_id
    assert second_record.idempotency_key == f"retry:{first.fence.operation_id}"
    assert second_record.normalized_input_digest == (
        coordinator.repository.get_operation_for_administration(
            first.fence.operation_id
        ).normalized_input_digest
    )

    coordinator.terminalize(
        second.fence,
        state=OperationState.RETRYABLE,
        terminal_code="revision_promotion_recovery_pending",
        safe_summary="Retry the child attempt",
        retry_after_ms=0,
    )
    _owner, third = await orchestrator._claim_personal_agent_operation(**kwargs)
    third_record = coordinator.repository.get_operation_for_administration(
        third.fence.operation_id
    )
    assert third_record is not None
    assert third_record.parent_operation_id == second.fence.operation_id
    assert third_record.idempotency_key == f"retry:{second.fence.operation_id}"


@pytest.mark.asyncio
async def test_failed_delivery_replay_preserves_terminal_disposition():
    orchestrator, coordinator = _personal_agent_operation_orchestrator()
    kwargs = _personal_agent_operation_kwargs()
    _owner, first = await orchestrator._claim_personal_agent_operation(**kwargs)
    coordinator.terminalize(
        first.fence,
        state=OperationState.FAILED,
        terminal_code="host_inventory_pending",
        safe_summary="The selected host inventory was not authoritative",
        retry_after_ms=None,
    )

    with pytest.raises(runtime._PersonalAgentOperationTerminal) as captured:
        await orchestrator._claim_personal_agent_operation(**kwargs)

    assert captured.value.state is OperationState.FAILED
    assert captured.value.terminal_code == "host_inventory_pending"
    assert coordinator.repository.get_operation_for_administration(
        first.fence.operation_id
    ).state is OperationState.FAILED


@pytest.mark.asyncio
async def test_running_delivery_replay_is_pending_not_terminalized_unselected():
    orchestrator, coordinator = _personal_agent_operation_orchestrator()
    kwargs = _personal_agent_operation_kwargs()
    owner, first = await orchestrator._claim_personal_agent_operation(**kwargs)

    with pytest.raises(
        runtime._PersonalAgentOperationRetryPending,
        match="already running",
    ):
        await orchestrator._claim_personal_agent_operation(
            **{**kwargs, "wait_seconds": 0.0}
        )

    projection = coordinator.query_operation(
        owner=owner,
        operation_id=first.fence.operation_id,
    )
    assert projection.state is OperationState.RUNNING


@pytest.mark.asyncio
async def test_delivery_retry_identity_change_fails_before_child_allocation():
    orchestrator, _coordinator = _personal_agent_operation_orchestrator()
    kwargs = _personal_agent_operation_kwargs()
    await orchestrator._claim_personal_agent_operation(**kwargs)

    with pytest.raises(
        runtime._PersonalAgentOperationIdentityConflict,
        match="changed semantics",
    ):
        await orchestrator._claim_personal_agent_operation(
            **{
                **kwargs,
                "normalized_identity": {
                    **kwargs["normalized_identity"],
                    "bundle_sha256": "e" * 64,
                },
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("poison_at", ("query", "claim"))
async def test_delivery_retry_parent_lineage_is_validated_before_use(poison_at):
    orchestrator, coordinator = _personal_agent_operation_orchestrator()
    poisoned_parent = uuid.uuid4()

    async def call(callback, *args, **kwargs):
        result = callback(*args, **kwargs)
        if poison_at == "query" and callback == coordinator.query_operation:
            return replace(result, parent_operation_id=poisoned_parent)
        if (
            poison_at == "claim"
            and callback == coordinator.claim_operation
            and result is not None
        ):
            return replace(
                result,
                operation=replace(
                    result.operation,
                    parent_operation_id=poisoned_parent,
                ),
            )
        return result

    orchestrator._call_work_admission = call

    with pytest.raises(
        runtime._PersonalAgentOperationIdentityConflict,
        match="lineage changed semantics",
    ):
        await orchestrator._claim_personal_agent_operation(
            **_personal_agent_operation_kwargs()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    (
        OperationState.RETRYABLE,
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.COMPLETED,
    ),
)
async def test_delivery_deadline_observes_first_terminal_winner(terminal_state):
    orchestrator, coordinator = _personal_agent_operation_orchestrator()

    async def call(callback, *args, **kwargs):
        if callback == coordinator.claim_operation:
            return None
        if callback == coordinator.terminalize_unselected:
            return SimpleNamespace(
                state=terminal_state,
                terminal_code="concurrent_terminal",
            )
        return callback(*args, **kwargs)

    orchestrator._call_work_admission = call
    kwargs = {
        **_personal_agent_operation_kwargs(),
        "wait_seconds": 0.0,
    }
    if terminal_state is OperationState.RETRYABLE:
        assert await orchestrator._claim_personal_agent_operation(**kwargs) is None
    else:
        with pytest.raises(runtime._PersonalAgentOperationTerminal) as captured:
            await orchestrator._claim_personal_agent_operation(**kwargs)
        assert captured.value.state is terminal_state
        assert captured.value.terminal_code == "concurrent_terminal"


def test_capability_getter_is_shared_and_returns_detached_payloads():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_capabilities = CandidateCapabilityMap()

    first = orchestrator.get_personal_agent_capabilities()
    second = orchestrator.get_personal_agent_capabilities()

    assert first == second
    assert first is not second
    first["personal_agent_host"]["macos"]["supported"] = True
    assert orchestrator.get_personal_agent_capabilities()[
        "personal_agent_host"
    ]["macos"]["supported"] is False


@pytest.mark.asyncio
async def test_clean_authority_absence_reaches_fresh_host_selection():
    owner_id = "owner-060"
    agent_id = "agent-clean-fresh"
    revision_id = _uuid()
    events: list[str] = []

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **kwargs):
            assert kwargs == {
                "owner_user_id": owner_id,
                "agent_id": agent_id,
            }
            events.append("authority_absent")
            return None

        def select_host_for_agent(self, **kwargs):
            assert kwargs == {
                "owner_user_id": owner_id,
                "agent_id": agent_id,
            }
            events.append("fresh_selection")
            return SimpleNamespace(session=None)

    class RevisionStore:
        def inspect_recovery_status(self, owner, agent, revision):
            assert (owner, agent, revision) == (owner_id, agent_id, revision_id)
            events.append("no_prepared_revision")
            return None

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    delivered = await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
    )

    assert delivered == 0
    assert events == [
        "authority_absent",
        "no_prepared_revision",
        "fresh_selection",
    ]
    orchestrator._claim_personal_agent_operation.assert_awaited_once()
    assert orchestrator._call_work_admission.await_count == 1
    terminal_call = orchestrator._call_work_admission.await_args
    assert terminal_call.args[0] is orchestrator.work_admission.terminalize
    assert terminal_call.kwargs == {
        "state": OperationState.RETRYABLE,
        "terminal_code": "revision_host_unavailable",
        "safe_summary": "No reconciled personal-agent host was available",
        "retry_after_ms": 1000,
    }


@pytest.mark.asyncio
async def test_replayed_no_host_delivery_allocates_exact_child_attempt():
    owner_id = "owner-060"
    agent_id = "agent-no-host-child"
    revision_id = _uuid()
    _unused, coordinator = _personal_agent_operation_orchestrator()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            return SimpleNamespace(session=None)

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator.work_admission = coordinator
    orchestrator._claim_personal_agent_operation = (
        Orchestrator._claim_personal_agent_operation.__get__(
            orchestrator,
            Orchestrator,
        )
    )
    settled_fences: list[ExecutionFence] = []

    async def call(callback, *args, **kwargs):
        result = callback(*args, **kwargs)
        if callback == coordinator.terminalize:
            settled_fences.append(args[0])
        return result

    orchestrator._call_work_admission = call
    delivery_args = _revision_delivery_args(
        owner_id=owner_id,
        agent_id=agent_id,
        revision_id=revision_id,
    )

    assert await orchestrator._deliver_personal_agent_revision(**delivery_args) == 0
    assert await orchestrator._deliver_personal_agent_revision(**delivery_args) == 0

    first = coordinator.repository.get_operation_for_administration(
        settled_fences[0].operation_id
    )
    second = coordinator.repository.get_operation_for_administration(
        settled_fences[1].operation_id
    )
    assert first is not None and second is not None
    assert first.state is OperationState.RETRYABLE
    assert second.state is OperationState.RETRYABLE
    assert first.terminal_code == "revision_host_unavailable"
    assert second.terminal_code == "revision_host_unavailable"
    assert second.parent_operation_id == first.operation_id
    assert second.idempotency_key == f"retry:{first.operation_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_error", "expected_code"),
    (
        (
            runtime._PersonalAgentOperationIdentityConflict("poisoned identity"),
            "revision_delivery_identity_conflict",
        ),
        (RuntimeError("admission acknowledgement lost"), "revision_delivery_admission_pending"),
    ),
)
async def test_delivery_admission_failure_precedes_host_selection(
    claim_error,
    expected_code,
):
    owner_id = "owner-060"
    agent_id = "agent-admission-first"
    revision_id = _uuid()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("admission ambiguity must precede host selection")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator._claim_personal_agent_operation = AsyncMock(
        side_effect=claim_error
    )

    expected_error = (
        RevisionActivationError
        if isinstance(claim_error, runtime._PersonalAgentOperationIdentityConflict)
        else RevisionActivationRecoveryPendingError
    )
    with pytest.raises(expected_error, match=expected_code):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_point", ("submit", "claim_operation"))
async def test_delivery_admission_commit_ack_loss_is_typed_pending(ack_point):
    owner_id = "owner-060"
    agent_id = f"agent-{ack_point}-ack-loss"
    revision_id = _uuid()
    _unused, coordinator = _personal_agent_operation_orchestrator()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("ambiguous admission must not select a host")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator.work_admission = coordinator
    orchestrator._claim_personal_agent_operation = (
        Orchestrator._claim_personal_agent_operation.__get__(
            orchestrator,
            Orchestrator,
        )
    )
    injected = False

    async def call(callback, *args, **kwargs):
        nonlocal injected
        result = callback(*args, **kwargs)
        if not injected and callback.__name__ == ack_point:
            injected = True
            raise RuntimeError("durable admission acknowledgement lost")
        return result

    orchestrator._call_work_admission = call

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_delivery_admission_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )
    assert injected


@pytest.mark.asyncio
async def test_no_host_settlement_ack_loss_queries_exact_retryable_result():
    owner_id = "owner-060"
    agent_id = "agent-no-host-ack-loss"
    revision_id = _uuid()
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            return SimpleNamespace(session=None)

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(
            SimpleNamespace(owner_user_id=owner_id),
            SimpleNamespace(fence=operation_fence),
        )
    )
    callbacks: list[object] = []

    async def call(callback, *args, **kwargs):
        callbacks.append(callback)
        if callback is orchestrator.work_admission.terminalize:
            raise RuntimeError("commit acknowledgement lost")
        assert callback is orchestrator.work_admission.query_operation
        assert kwargs["operation_id"] == operation_fence.operation_id
        return SimpleNamespace(
            operation_id=operation_fence.operation_id,
            state=OperationState.RETRYABLE,
            terminal_code="revision_host_unavailable",
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=call)

    assert await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
    ) == 0
    assert callbacks == [
        orchestrator.work_admission.terminalize,
        orchestrator.work_admission.query_operation,
    ]


@pytest.mark.asyncio
async def test_host_selection_failure_settles_retryable_before_pending():
    owner_id = "owner-060"
    agent_id = "agent-host-selection-pending"
    revision_id = _uuid()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise RuntimeError("Plane selection unavailable")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_host_selection_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    terminal_call = orchestrator._call_work_admission.await_args
    assert terminal_call.args[0] is orchestrator.work_admission.terminalize
    assert terminal_call.kwargs["state"] is OperationState.RETRYABLE
    assert terminal_call.kwargs["terminal_code"] == (
        "revision_host_selection_pending"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    (OperationState.FAILED, OperationState.CANCELLED),
)
async def test_no_host_settlement_preserves_concurrent_terminal_winner(
    terminal_state,
):
    owner_id = "owner-060"
    agent_id = "agent-no-host-terminal-race"
    revision_id = _uuid()
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            return SimpleNamespace(session=None)

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(SimpleNamespace(), SimpleNamespace(fence=operation_fence))
    )

    async def call(callback, *_args, **_kwargs):
        assert callback is orchestrator.work_admission.terminalize
        return SimpleNamespace(
            operation_id=operation_fence.operation_id,
            state=terminal_state,
            terminal_code="concurrent_cancellation",
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=call)

    with pytest.raises(RevisionActivationError, match="concurrent_cancellation"):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )


@pytest.mark.asyncio
async def test_completed_delivery_replay_projects_authority_before_host_selection():
    owner_id = "owner-060"
    agent_id = "agent-completed-replay"
    revision_id = _uuid()
    online = _online_runtime(agent_id=agent_id, revision_id=revision_id)
    authority_reads = 0

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            nonlocal authority_reads
            authority_reads += 1
            return None if authority_reads == 1 else online

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("completed delivery must not select a host")

    class RevisionStore:
        def inspect_recovery_status(self, *_args):
            return None

        def assert_active_replay(self, replay):
            assert replay.runtime_instance_id == online.fence.runtime_instance_id

        def recovery_plan(self, owner, agent):
            return RecoveryPlan(
                owner_user_id=owner,
                agent_id=agent,
                active_revision_id=revision_id,
                authoritative_runtime_instance_id=online.fence.runtime_instance_id,
                start_revision_id=None,
                stop_runtime_instance_ids=(),
            )

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )
    orchestrator._claim_personal_agent_operation = AsyncMock(
        side_effect=runtime._PersonalAgentOperationTerminal(
            OperationState.COMPLETED,
            None,
        )
    )

    assert await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
    ) == 1
    assert authority_reads == 2
    orchestrator._publish_personal_agent_runtime.assert_awaited_once_with(online)


@pytest.mark.asyncio
async def test_authority_lookup_failure_is_recovery_pending():
    owner_id = "owner-060"
    agent_id = "agent-authority-lookup"
    revision_id = _uuid()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            raise RuntimeError("Plane is unavailable")

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("host selection must not follow lookup ambiguity")

    class RevisionStore:
        def inspect_recovery_status(self, *_args):
            raise AssertionError("recovery inspection requires a clean absence")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_authority_lookup_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )


@pytest.mark.asyncio
async def test_recovery_status_lookup_failure_is_recovery_pending():
    owner_id = "owner-060"
    agent_id = "agent-recovery-lookup"
    revision_id = _uuid()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("host selection must not follow lookup ambiguity")

    class RevisionStore:
        def inspect_recovery_status(self, *_args):
            raise RuntimeError("revision journal is unavailable")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_recovery_lookup_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )


@pytest.mark.asyncio
async def test_exact_active_revision_replay_validates_before_projection():
    owner_id = "owner-060"
    agent_id = "agent-exact-replay"
    revision_id = _uuid()
    metadata = _candidate_metadata()
    online = _online_runtime(agent_id=agent_id, revision_id=revision_id)
    events: list[str] = []

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **kwargs):
            assert kwargs == {
                "owner_user_id": owner_id,
                "agent_id": agent_id,
            }
            events.append("authority")
            return online

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("exact replay must not select a host")

    class RevisionStore:
        def assert_active_replay(self, replay):
            assert replay.owner_user_id == owner_id
            assert replay.agent_id == agent_id
            assert replay.revision_id == revision_id
            assert replay.runtime_instance_id == online.fence.runtime_instance_id
            assert replay.agent_metadata == metadata
            events.append("durable_identity")

        def recovery_plan(self, owner, agent):
            assert (owner, agent) == (owner_id, agent_id)
            events.append("cleanup_reconciled")
            return RecoveryPlan(
                owner_user_id=owner,
                agent_id=agent,
                active_revision_id=revision_id,
                authoritative_runtime_instance_id=(
                    online.fence.runtime_instance_id
                ),
                start_revision_id=None,
                stop_runtime_instance_ids=(),
            )

        def inspect_recovery_status(self, *_args):
            raise AssertionError("exact replay must not enter candidate recovery")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    async def admit(**kwargs):
        assert kwargs["owner_user_id"] == owner_id
        assert kwargs["runtime"] is online
        assert kwargs["declared_scopes"] == ()
        assert events == [
            "authority",
            "durable_identity",
            "cleanup_reconciled",
        ]
        events.append("lets_admission")

    async def publish(projected):
        assert projected is online
        assert events == [
            "authority",
            "durable_identity",
            "cleanup_reconciled",
            "lets_admission",
        ]
        events.append("route_projection")

    orchestrator._admit_personal_agent_runtime_authority = AsyncMock(
        side_effect=admit
    )
    orchestrator._publish_personal_agent_runtime = AsyncMock(side_effect=publish)

    delivered = await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
            agent_metadata=metadata,
        )
    )

    assert delivered == 1
    assert events == [
        "authority",
        "durable_identity",
        "cleanup_reconciled",
        "lets_admission",
        "route_projection",
    ]
    orchestrator._claim_personal_agent_operation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_error",
    [
        StaleRuntimeGenerationError("immutable revision identity is stale"),
        ValueError("candidate metadata is stale"),
    ],
    ids=("immutable-identity", "candidate-metadata"),
)
async def test_exact_active_identity_mismatch_is_terminal(identity_error):
    owner_id = "owner-060"
    agent_id = "agent-replay-conflict"
    revision_id = _uuid()
    online = _online_runtime(agent_id=agent_id, revision_id=revision_id)

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return online

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("identity conflicts must not select a host")

    class RevisionStore:
        def assert_active_replay(self, _replay):
            raise identity_error

        def inspect_recovery_status(self, *_args):
            raise AssertionError("identity conflicts are terminal")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    with pytest.raises(
        RevisionActivationError,
        match="revision_replay_identity_conflict",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    orchestrator._admit_personal_agent_runtime_authority.assert_not_awaited()
    orchestrator._publish_personal_agent_runtime.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_projection", ("lets", "route"))
async def test_exact_active_projection_error_is_recovery_pending(failed_projection):
    owner_id = "owner-060"
    agent_id = "agent-projection-retry"
    revision_id = _uuid()
    online = _online_runtime(agent_id=agent_id, revision_id=revision_id)
    replay_checks: list[str] = []

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return online

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("committed replay must not select a host")

    class RevisionStore:
        def assert_active_replay(self, _replay):
            replay_checks.append("validated")

        def recovery_plan(self, owner, agent):
            assert (owner, agent) == (owner_id, agent_id)
            replay_checks.append("cleanup_reconciled")
            return RecoveryPlan(
                owner_user_id=owner,
                agent_id=agent,
                active_revision_id=revision_id,
                authoritative_runtime_instance_id=(
                    online.fence.runtime_instance_id
                ),
                start_revision_id=None,
                stop_runtime_instance_ids=(),
            )

        def inspect_recovery_status(self, *_args):
            raise AssertionError("committed replay must not recover a candidate")

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )
    if failed_projection == "lets":
        orchestrator._admit_personal_agent_runtime_authority.side_effect = (
            RuntimeError("LETS authority unavailable")
        )
    else:
        orchestrator._publish_personal_agent_runtime.side_effect = RuntimeError(
            "route projection unavailable"
        )

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_projection_recovery_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert replay_checks == ["validated", "cleanup_reconciled"]
    orchestrator._admit_personal_agent_runtime_authority.assert_awaited_once()
    if failed_projection == "lets":
        orchestrator._publish_personal_agent_runtime.assert_not_awaited()
    else:
        orchestrator._publish_personal_agent_runtime.assert_awaited_once_with(
            online
        )


@pytest.mark.asyncio
async def test_prepared_revision_without_runtime_remains_a_fresh_delivery(monkeypatch):
    owner_id = "owner-060"
    agent_id = "agent-prepared-only"
    revision_id = _uuid()
    selection_calls: list[tuple[str, str]] = []

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **kwargs):
            selection_calls.append(
                (kwargs["owner_user_id"], kwargs["agent_id"])
            )
            return SimpleNamespace(session=None)

    class RevisionStore:
        def inspect_recovery_status(self, owner, agent, revision):
            return RevisionRecoveryStatus(
                owner_user_id=owner,
                agent_id=agent,
                revision_id=revision,
                revision_state="prepared",
                runtime_instance_id=None,
                runtime_failure_code=None,
                operation_state=None,
                operation_terminal_code=None,
            )

    async def unexpected_recovery(*_args):
        raise AssertionError("a prepared revision without a runtime is fresh")

    monkeypatch.setattr(
        AgentRevisionActivator,
        "reconcile_after_crash",
        unexpected_recovery,
    )
    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    delivered = await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
    )

    assert delivered == 0
    assert selection_calls == [(owner_id, agent_id)]


@pytest.mark.asyncio
async def test_prepared_revision_replay_preserves_failed_delivery_result():
    owner_id = "owner-060"
    agent_id = "agent-terminal-delivery"
    revision_id = _uuid()
    host_session_id = _uuid()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("terminal delivery must not select a host")

    class RevisionStore:
        def inspect_recovery_status(self, owner, agent, revision):
            return RevisionRecoveryStatus(
                owner_user_id=owner,
                agent_id=agent,
                revision_id=revision,
                revision_state="prepared",
                runtime_instance_id=None,
                runtime_failure_code=None,
                operation_state=None,
                operation_terminal_code=None,
            )

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}
    orchestrator._claim_personal_agent_operation = AsyncMock(
        side_effect=runtime._PersonalAgentOperationTerminal(
            OperationState.FAILED,
            "host_inventory_pending",
        )
    )

    with pytest.raises(RevisionActivationError, match="host_inventory_pending"):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )


@pytest.mark.asyncio
async def test_attempted_candidate_with_live_operation_remains_pending(monkeypatch):
    owner_id = "owner-060"
    agent_id = "agent-live-attempt"
    revision_id = _uuid()
    runtime_instance_id = _uuid()
    inspection_count = 0

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("a live attempt must not select another host")

    class RevisionStore:
        def inspect_recovery_status(self, owner, agent, revision):
            nonlocal inspection_count
            inspection_count += 1
            return RevisionRecoveryStatus(
                owner_user_id=owner,
                agent_id=agent,
                revision_id=revision,
                revision_state="starting",
                runtime_instance_id=runtime_instance_id,
                runtime_failure_code=None,
                operation_state=OperationState.RUNNING,
                operation_terminal_code=None,
            )

    async def unexpected_activation(*_args):
        raise AssertionError("a live attempt must not activate a second runtime")

    monkeypatch.setattr(AgentRevisionActivator, "activate", unexpected_activation)
    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_activation_in_progress",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert inspection_count == 2
    orchestrator._call_work_admission.assert_awaited_once_with(
        orchestrator.work_admission.expire_execution_leases
    )
    orchestrator._claim_personal_agent_operation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ("lease_expiration", "status_reread"))
async def test_running_candidate_recovery_failures_are_typed(failure_at):
    owner_id = "owner-060"
    agent_id = "agent-running-recovery-failure"
    revision_id = _uuid()
    runtime_instance_id = _uuid()
    inspections = 0

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("ambiguous recovery must not select a host")

    class RevisionStore:
        def inspect_recovery_status(self, owner, agent, revision):
            nonlocal inspections
            inspections += 1
            if failure_at == "status_reread" and inspections == 2:
                raise RuntimeError("recovery status reread unavailable")
            return RevisionRecoveryStatus(
                owner_user_id=owner,
                agent_id=agent,
                revision_id=revision,
                revision_state="starting",
                runtime_instance_id=runtime_instance_id,
                runtime_failure_code=None,
                operation_state=OperationState.RUNNING,
                operation_terminal_code=None,
            )

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    async def expire(callback, *_args, **_kwargs):
        assert callback is orchestrator.work_admission.expire_execution_leases
        if failure_at == "lease_expiration":
            raise RuntimeError("lease expiration unavailable")
        return None

    orchestrator._call_work_admission = AsyncMock(side_effect=expire)

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_recovery_lookup_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert inspections == (1 if failure_at == "lease_expiration" else 2)
    orchestrator._claim_personal_agent_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_attempt_resets_old_runtime_then_activates_child_attempt(
    monkeypatch,
):
    owner_id = "owner-060"
    agent_id = "agent-expired-winner"
    revision_id = _uuid()
    stale_runtime_instance_id = _uuid()
    runtime_instance_id = _uuid()
    online = _online_runtime(
        agent_id=agent_id,
        revision_id=revision_id,
        runtime_instance_id=runtime_instance_id,
    )
    statuses = [OperationState.RUNNING, OperationState.RETRYABLE, None]
    events: list[str] = []
    host_session_id = _uuid()
    host = SimpleNamespace(
        host_session_id=host_session_id,
        inventory_state="reconciled",
    )
    child_fence = ExecutionFence(uuid.uuid4(), 2, uuid.uuid4())

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            events.append("authority_absent")
            return None

        def get_runtime_instance(self, selected_runtime_instance_id):
            assert selected_runtime_instance_id == runtime_instance_id
            events.append("active_runtime")
            return online

        def select_host_for_agent(self, **kwargs):
            assert kwargs == {
                "owner_user_id": owner_id,
                "agent_id": agent_id,
            }
            events.append("fresh_host_selection")
            return SimpleNamespace(session=host)

    class RevisionStore:
        def inspect_recovery_status(self, owner, agent, revision):
            state = statuses.pop(0)
            events.append(
                "operation_prepared"
                if state is None
                else f"operation_{state.value}"
            )
            return RevisionRecoveryStatus(
                owner_user_id=owner,
                agent_id=agent,
                revision_id=revision,
                revision_state=("prepared" if state is None else "starting"),
                runtime_instance_id=(
                    None if state is None else stale_runtime_instance_id
                ),
                runtime_failure_code=None,
                operation_state=state,
                operation_terminal_code=(
                    "lease_expired"
                    if state is OperationState.RETRYABLE
                    else None
                ),
            )

        def stage_retryable_candidate_reset(self, owner, agent, revision):
            assert (owner, agent, revision) == (
                owner_id,
                agent_id,
                revision_id,
            )
            events.append("retryable_runtime_staged")
            return stale_runtime_instance_id

        def finalize_retryable_candidate_reset(
            self,
            owner,
            agent,
            revision,
            selected_runtime_instance_id,
        ):
            assert (owner, agent, revision, selected_runtime_instance_id) == (
                owner_id,
                agent_id,
                revision_id,
                stale_runtime_instance_id,
            )
            events.append("retryable_runtime_finalized")

    async def activate(_self, preparation):
        assert preparation.owner_user_id == owner_id
        assert preparation.agent_id == agent_id
        assert preparation.revision_id == revision_id
        assert preparation.operation_fence == child_fence
        events.append("child_activation")
        return SimpleNamespace(
            commit=SimpleNamespace(runtime_instance_id=runtime_instance_id),
            cleanup_pending=False,
        )

    monkeypatch.setattr(AgentRevisionActivator, "activate", activate)
    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}

    async def stop_stale_runtime(selected_runtime_instance_id):
        assert selected_runtime_instance_id == stale_runtime_instance_id
        events.append("exact_stale_stop")
        return PhysicalStopReceipt(
            runtime_instance_id=stale_runtime_instance_id,
            release=lambda: events.append("stale_stop_receipt_released"),
        )

    async def claim(**kwargs):
        assert kwargs["owner_user_id"] == owner_id
        assert kwargs["retry_terminal"] is True
        events.append("child_operation_claimed")
        return SimpleNamespace(), SimpleNamespace(fence=child_fence)

    async def call_work_admission(callback, *args, **kwargs):
        if callback is orchestrator.work_admission.expire_execution_leases:
            assert args == ()
            assert kwargs == {}
            events.append("expired_parent_lease")
            return None
        assert callback is orchestrator.work_admission.terminalize
        assert args == (child_fence,)
        assert kwargs["state"] is OperationState.COMPLETED
        events.append("child_operation_completed")
        return SimpleNamespace(
            operation_id=child_fence.operation_id,
            state=OperationState.COMPLETED,
            terminal_code=None,
        )

    async def admit(**kwargs):
        assert kwargs["owner_user_id"] == owner_id
        assert kwargs["runtime"] is online
        events.append("lets_admission")

    async def publish(projected):
        assert projected is online
        events.append("route_projection")

    orchestrator._stop_personal_agent_revision_process = AsyncMock(
        side_effect=stop_stale_runtime
    )
    orchestrator._claim_personal_agent_operation = AsyncMock(side_effect=claim)
    orchestrator._call_work_admission = AsyncMock(
        side_effect=call_work_admission
    )
    orchestrator._admit_personal_agent_runtime_authority = AsyncMock(
        side_effect=admit
    )
    orchestrator._publish_personal_agent_runtime = AsyncMock(side_effect=publish)

    delivered = await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
    )

    assert delivered == 1
    assert statuses == []
    assert events == [
        "authority_absent",
        "operation_running",
        "expired_parent_lease",
        "operation_retryable",
        "retryable_runtime_staged",
        "exact_stale_stop",
        "retryable_runtime_finalized",
        "stale_stop_receipt_released",
        "operation_prepared",
        "child_operation_claimed",
        "fresh_host_selection",
        "child_activation",
        "active_runtime",
        "lets_admission",
        "child_operation_completed",
        "route_projection",
    ]
    orchestrator._stop_personal_agent_revision_process.assert_awaited_once_with(
        stale_runtime_instance_id
    )
    assert orchestrator._call_work_admission.await_count == 2
    orchestrator._claim_personal_agent_operation.assert_awaited_once()
    orchestrator._publish_personal_agent_runtime.assert_awaited_once_with(online)


@pytest.mark.asyncio
async def test_terminal_attempt_recovers_old_winner_without_second_activation(
    monkeypatch,
):
    owner_id = "owner-060"
    agent_id = "agent-terminal-loser"
    revision_id = _uuid()
    runtime_instance_id = _uuid()
    reconcile_calls: list[tuple[str, str]] = []

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def get_current_online_authority(self, **_kwargs):
            raise AssertionError("the losing candidate is not authoritative")

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("recovery must not select a second host")

    class RevisionStore:
        def inspect_recovery_status(self, owner, agent, revision):
            return RevisionRecoveryStatus(
                owner_user_id=owner,
                agent_id=agent,
                revision_id=revision,
                revision_state="starting",
                runtime_instance_id=runtime_instance_id,
                runtime_failure_code="revision_promotion_failed",
                operation_state=OperationState.FAILED,
                operation_terminal_code="revision_promotion_failed",
            )

    async def unexpected_activation(*_args):
        raise AssertionError("recovery must not activate a second runtime")

    async def reconcile(_self, owner, agent):
        reconcile_calls.append((owner, agent))
        return SimpleNamespace(
            active_revision_id=_uuid(),
            authoritative_runtime_instance_id=_uuid(),
        )

    monkeypatch.setattr(AgentRevisionActivator, "activate", unexpected_activation)
    monkeypatch.setattr(AgentRevisionActivator, "reconcile_after_crash", reconcile)
    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    with pytest.raises(
        RevisionActivationError,
        match="revision_promotion_failed",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert reconcile_calls == [(owner_id, agent_id)]
    orchestrator._call_work_admission.assert_not_awaited()
    orchestrator._claim_personal_agent_operation.assert_not_awaited()
    orchestrator._publish_personal_agent_runtime.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_code", "operation_code", "expected_code"),
    (
        ("bundle_install_failed", None, "bundle_install_failed"),
        ("child_start_failed", None, "child_start_failed"),
        (
            "child_registration_timeout",
            None,
            "child_registration_timeout",
        ),
        ("revision_promotion_failed", None, "revision_promotion_failed"),
        (None, "revision_cancelled_by_owner", "revision_cancelled_by_owner"),
    ),
)
async def test_failed_revision_replay_preserves_exact_durable_code(
    runtime_code,
    operation_code,
    expected_code,
):
    owner_id = "owner-060"
    agent_id = "agent-failed-replay-code"
    revision_id = _uuid()

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            raise AssertionError("a terminal revision must not select a host")

    class RevisionStore:
        def inspect_recovery_status(self, *_args):
            return RevisionRecoveryStatus(
                owner_user_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
                revision_state="failed",
                runtime_instance_id=None,
                runtime_failure_code=runtime_code,
                operation_state=(
                    None if operation_code is None else OperationState.CANCELLED
                ),
                operation_terminal_code=operation_code,
            )

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=RevisionStore(),
    )

    with pytest.raises(RevisionActivationError, match=expected_code):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    orchestrator._claim_personal_agent_operation.assert_not_awaited()
    orchestrator._publish_personal_agent_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_revision_promotion_terminalizes_delivery_retryable(
    monkeypatch,
):
    owner_id = "owner-060"
    agent_id = "agent-recovery-pending"
    revision_id = _uuid()
    host_session_id = _uuid()
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    host = SimpleNamespace(
        host_session_id=host_session_id,
        inventory_state="reconciled",
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._personal_agent_activation_locks = {}
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}
    orchestrator.personal_agent_runtime = SimpleNamespace(
        get_current_online_authority_if_present=lambda **_kwargs: None,
        select_host_for_agent=lambda **_kwargs: SimpleNamespace(session=host),
    )
    orchestrator.personal_agent_revisions = SimpleNamespace(
        inspect_recovery_status=lambda *_args: None
    )
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(
            SimpleNamespace(),
            SimpleNamespace(fence=operation_fence),
        )
    )
    orchestrator.work_admission = SimpleNamespace(
        terminalize=object(),
        query_operation=object(),
    )

    async def settle(callback, *args, **kwargs):
        assert callback is orchestrator.work_admission.terminalize
        assert args == (operation_fence,)
        return SimpleNamespace(
            operation_id=operation_fence.operation_id,
            state=kwargs["state"],
            terminal_code=kwargs["terminal_code"],
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=settle)

    async def pending(_self, _preparation):
        raise RevisionActivationRecoveryPendingError(
            "revision_promotion_recovery_pending"
        )

    monkeypatch.setattr(AgentRevisionActivator, "activate", pending)

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_promotion_recovery_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert orchestrator._call_work_admission.await_count == 1
    _, kwargs = orchestrator._call_work_admission.await_args
    assert kwargs == {
        "state": OperationState.RETRYABLE,
        "terminal_code": "revision_promotion_recovery_pending",
        "safe_summary": "Personal-agent revision promotion requires recovery",
        "retry_after_ms": 1000,
    }


@pytest.mark.asyncio
async def test_ambiguous_promotion_reconciles_committed_winner(monkeypatch):
    owner_id = "owner-060"
    agent_id = "agent-reconciled-winner"
    revision_id = _uuid()
    runtime_instance_id = _uuid()
    host_session_id = _uuid()
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    host = SimpleNamespace(
        host_session_id=host_session_id,
        inventory_state="reconciled",
    )
    online = _online_runtime(
        agent_id=agent_id,
        revision_id=revision_id,
        runtime_instance_id=runtime_instance_id,
    )
    authority_visible = False
    activation_calls = 0
    events: list[str] = []

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **kwargs):
            assert kwargs == {
                "owner_user_id": owner_id,
                "agent_id": agent_id,
            }
            events.append(
                "committed_authority" if authority_visible else "authority_absent"
            )
            return online if authority_visible else None

        def select_host_for_agent(self, **_kwargs):
            events.append("host_selected")
            return SimpleNamespace(session=host)

    class RevisionStore:
        def inspect_recovery_status(self, *_args):
            events.append("fresh_revision")
            return None

        def assert_active_replay(self, replay):
            assert replay.runtime_instance_id == runtime_instance_id
            events.append("durable_identity")

        def recovery_plan(self, owner, agent):
            assert (owner, agent) == (owner_id, agent_id)
            events.append("cleanup_reconciled")
            return RecoveryPlan(
                owner_user_id=owner,
                agent_id=agent,
                active_revision_id=revision_id,
                authoritative_runtime_instance_id=runtime_instance_id,
                start_revision_id=None,
                stop_runtime_instance_ids=(),
            )

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._personal_agent_activation_locks = {}
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}
    orchestrator.personal_agent_runtime = RuntimeRepository()
    orchestrator.personal_agent_revisions = RevisionStore()
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(
            SimpleNamespace(),
            SimpleNamespace(fence=operation_fence),
        )
    )
    orchestrator.work_admission = SimpleNamespace(
        terminalize=object(),
        query_operation=object(),
    )

    async def settle(callback, *args, **kwargs):
        assert callback is orchestrator.work_admission.terminalize
        assert args == (operation_fence,)
        return SimpleNamespace(
            operation_id=operation_fence.operation_id,
            state=kwargs["state"],
            terminal_code=kwargs["terminal_code"],
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=settle)
    orchestrator._admit_personal_agent_runtime_authority = AsyncMock()
    orchestrator._publish_personal_agent_runtime = AsyncMock()
    orchestrator._audit_user_agent = AsyncMock()
    orchestrator.agents = {}

    async def pending(_self, _preparation):
        nonlocal activation_calls
        activation_calls += 1
        events.append("promotion_ambiguous")
        raise RevisionActivationRecoveryPendingError(
            "revision_promotion_recovery_pending"
        )

    async def reconcile(_self, reconciled_owner, reconciled_agent):
        assert (reconciled_owner, reconciled_agent) == (owner_id, agent_id)
        events.append("cleanup_reconciled")
        return SimpleNamespace(
            active_revision_id=revision_id,
            authoritative_runtime_instance_id=runtime_instance_id,
        )

    monkeypatch.setattr(AgentRevisionActivator, "activate", pending)
    monkeypatch.setattr(AgentRevisionActivator, "reconcile_after_crash", reconcile)

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_promotion_recovery_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert activation_calls == 1
    assert orchestrator._call_work_admission.await_count == 1
    _, retry_kwargs = orchestrator._call_work_admission.await_args
    assert retry_kwargs["state"] is OperationState.RETRYABLE
    orchestrator._publish_personal_agent_runtime.assert_not_awaited()

    # The ambiguous transaction becomes visible only on the explicit replay.
    # That replay validates the exact immutable identity, reconciles cleanup,
    # and projects the already-committed winner without activating again.
    authority_visible = True
    delivered = await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
    )

    assert delivered == 1
    assert activation_calls == 1
    assert events == [
        "authority_absent",
        "fresh_revision",
        "host_selected",
        "promotion_ambiguous",
        "committed_authority",
        "durable_identity",
        "cleanup_reconciled",
    ]
    assert orchestrator._call_work_admission.await_count == 1
    orchestrator._claim_personal_agent_operation.assert_awaited_once()
    orchestrator._publish_personal_agent_runtime.assert_awaited_once_with(online)


@pytest.mark.asyncio
async def test_postcommit_route_failure_never_relabels_activation_failed(monkeypatch):
    owner_id = "owner-060"
    agent_id = "agent-route-pending"
    revision_id = _uuid()
    runtime_instance_id = _uuid()
    host_session_id = _uuid()
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    host = SimpleNamespace(
        host_session_id=host_session_id,
        inventory_state="reconciled",
    )
    online = SimpleNamespace(runtime_instance_id=runtime_instance_id)
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._personal_agent_activation_locks = {}
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}
    orchestrator.personal_agent_runtime = SimpleNamespace(
        get_current_online_authority_if_present=lambda **_kwargs: None,
        select_host_for_agent=lambda **_kwargs: SimpleNamespace(session=host),
        get_runtime_instance=lambda _runtime_id: online,
    )
    orchestrator.personal_agent_revisions = SimpleNamespace(
        inspect_recovery_status=lambda *_args: None
    )
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(
            SimpleNamespace(),
            SimpleNamespace(fence=operation_fence),
        )
    )
    orchestrator.work_admission = SimpleNamespace(
        terminalize=object(),
        query_operation=object(),
    )

    async def settle(callback, *args, **kwargs):
        assert callback is orchestrator.work_admission.terminalize
        assert args == (operation_fence,)
        return SimpleNamespace(
            operation_id=operation_fence.operation_id,
            state=kwargs["state"],
            terminal_code=kwargs["terminal_code"],
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=settle)
    orchestrator._admit_personal_agent_runtime_authority = AsyncMock()
    orchestrator._publish_personal_agent_runtime = AsyncMock(
        side_effect=RuntimeError("route projection unavailable")
    )

    async def committed(_self, _preparation):
        return SimpleNamespace(
            commit=SimpleNamespace(runtime_instance_id=runtime_instance_id),
            cleanup_pending=False,
        )

    monkeypatch.setattr(AgentRevisionActivator, "activate", committed)

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_projection_recovery_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert orchestrator._call_work_admission.await_count == 1
    _, kwargs = orchestrator._call_work_admission.await_args
    assert kwargs["state"] is OperationState.COMPLETED
    assert kwargs["terminal_code"] is None
    orchestrator._publish_personal_agent_runtime.assert_awaited_once_with(online)


@pytest.mark.asyncio
async def test_revision_activation_renews_lease_through_durable_projection(monkeypatch):
    owner_id = "owner-060"
    agent_id = "agent-renewed-activation"
    revision_id = _uuid()
    runtime_instance_id = _uuid()
    host_session_id = _uuid()
    host = SimpleNamespace(
        host_session_id=host_session_id,
        inventory_state="reconciled",
    )
    online = _online_runtime(
        agent_id=agent_id,
        revision_id=revision_id,
        runtime_instance_id=runtime_instance_id,
    )

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            return SimpleNamespace(session=host)

        def get_runtime_instance(self, selected_runtime_instance_id):
            assert selected_runtime_instance_id == runtime_instance_id
            return online

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}
    orchestrator.work_admission.renew_execution_lease = object()
    renewed = asyncio.Event()
    renewal_fences: list[ExecutionFence] = []

    async def call(callback, *args, **kwargs):
        if callback is orchestrator.work_admission.renew_execution_lease:
            renewal_fences.append(args[0])
            renewed.set()
            return SimpleNamespace()
        assert callback is orchestrator.work_admission.terminalize
        return SimpleNamespace(
            operation_id=args[0].operation_id,
            state=kwargs["state"],
            terminal_code=kwargs["terminal_code"],
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=call)
    monkeypatch.setattr(runtime, "CONNECTION_LEASE_RENEW_SECONDS", 0.001)

    async def delayed_activation(_self, preparation):
        await asyncio.wait_for(renewed.wait(), timeout=1.0)
        assert preparation.operation_fence == renewal_fences[0]
        return SimpleNamespace(
            commit=SimpleNamespace(runtime_instance_id=runtime_instance_id),
            cleanup_pending=False,
        )

    monkeypatch.setattr(AgentRevisionActivator, "activate", delayed_activation)

    async def publish_while_renewed(projected):
        assert projected is online
        assert any(
            task.get_name().startswith("personal-agent-revision-lease-")
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

    orchestrator._publish_personal_agent_runtime.side_effect = (
        publish_while_renewed
    )

    delivered = await orchestrator._deliver_personal_agent_revision(
        **_revision_delivery_args(
            owner_id=owner_id,
            agent_id=agent_id,
            revision_id=revision_id,
        )
    )

    assert delivered == 1
    assert renewal_fences
    assert renewal_fences[0] == (
        orchestrator._claim_personal_agent_operation.return_value[1].fence
    )
    orchestrator._publish_personal_agent_runtime.assert_awaited_once_with(online)
    await asyncio.sleep(0)
    assert not any(
        task.get_name().startswith("personal-agent-revision-lease-")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_cancelled_delivery_joins_activation_before_stopping_renewal(monkeypatch):
    owner_id = "owner-060"
    agent_id = "agent-cancelled-renewal"
    revision_id = _uuid()
    runtime_instance_id = _uuid()
    host_session_id = _uuid()
    host = SimpleNamespace(
        host_session_id=host_session_id,
        inventory_state="reconciled",
    )
    online = _online_runtime(
        agent_id=agent_id,
        revision_id=revision_id,
        runtime_instance_id=runtime_instance_id,
    )

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            return SimpleNamespace(session=host)

        def get_runtime_instance(self, selected_runtime_instance_id):
            assert selected_runtime_instance_id == runtime_instance_id
            return online

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}
    orchestrator.work_admission.renew_execution_lease = object()
    first_renewal = asyncio.Event()
    second_renewal = asyncio.Event()
    activation_may_finish = asyncio.Event()
    renewals = 0

    async def call(callback, *args, **kwargs):
        nonlocal renewals
        if callback is orchestrator.work_admission.renew_execution_lease:
            renewals += 1
            first_renewal.set()
            if renewals >= 2:
                second_renewal.set()
            return SimpleNamespace()
        assert callback is orchestrator.work_admission.terminalize
        return SimpleNamespace(
            operation_id=args[0].operation_id,
            state=kwargs["state"],
            terminal_code=kwargs["terminal_code"],
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=call)
    monkeypatch.setattr(runtime, "CONNECTION_LEASE_RENEW_SECONDS", 0.001)

    async def delayed_activation(_self, _preparation):
        await activation_may_finish.wait()
        return SimpleNamespace(
            commit=SimpleNamespace(runtime_instance_id=runtime_instance_id),
            cleanup_pending=False,
        )

    monkeypatch.setattr(AgentRevisionActivator, "activate", delayed_activation)
    delivery = asyncio.create_task(
        orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )
    )
    await asyncio.wait_for(first_renewal.wait(), timeout=1.0)
    delivery.cancel()
    await asyncio.wait_for(second_renewal.wait(), timeout=1.0)
    delivery.cancel()
    await asyncio.sleep(0)
    assert not delivery.done()

    activation_may_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await delivery

    assert renewals >= 2
    orchestrator._publish_personal_agent_runtime.assert_awaited_once_with(online)
    await asyncio.sleep(0)
    assert not any(
        task.get_name().startswith("personal-agent-revision-lease-")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
async def test_stale_revision_lease_first_terminal_winner_blocks_projection(
    monkeypatch,
):
    owner_id = "owner-060"
    agent_id = "agent-stale-activation-lease"
    revision_id = _uuid()
    host_session_id = _uuid()
    host = SimpleNamespace(
        host_session_id=host_session_id,
        inventory_state="reconciled",
    )

    class RuntimeRepository:
        def get_current_online_authority_if_present(self, **_kwargs):
            return None

        def select_host_for_agent(self, **_kwargs):
            return SimpleNamespace(session=host)

    orchestrator = _revision_delivery_orchestrator(
        runtime_repository=RuntimeRepository(),
        revision_store=SimpleNamespace(
            inspect_recovery_status=lambda *_args: None
        ),
    )
    orchestrator._personal_agent_session_sockets = {host_session_id: object()}
    orchestrator.work_admission.renew_execution_lease = object()
    renewal_lost = asyncio.Event()
    terminal_requests: list[OperationState] = []

    async def call(callback, *_args, **kwargs):
        if callback is orchestrator.work_admission.renew_execution_lease:
            renewal_lost.set()
            raise StaleExecutionFenceError("delivery lease is stale")
        assert callback is orchestrator.work_admission.terminalize
        terminal_requests.append(kwargs["state"])
        operation_fence = (
            orchestrator._claim_personal_agent_operation.return_value[1].fence
        )
        return SimpleNamespace(
            operation_id=operation_fence.operation_id,
            state=OperationState.RETRYABLE,
            terminal_code="execution_lease_expired",
        )

    orchestrator._call_work_admission = AsyncMock(side_effect=call)
    monkeypatch.setattr(runtime, "CONNECTION_LEASE_RENEW_SECONDS", 0.001)

    async def stale_activation(_self, _preparation):
        await asyncio.wait_for(renewal_lost.wait(), timeout=1.0)
        raise RevisionActivationError("stale_runtime_generation")

    monkeypatch.setattr(AgentRevisionActivator, "activate", stale_activation)

    with pytest.raises(
        RevisionActivationRecoveryPendingError,
        match="revision_delivery_settlement_pending",
    ):
        await orchestrator._deliver_personal_agent_revision(
            **_revision_delivery_args(
                owner_id=owner_id,
                agent_id=agent_id,
                revision_id=revision_id,
            )
        )

    assert terminal_requests == [OperationState.FAILED]
    orchestrator._admit_personal_agent_runtime_authority.assert_not_awaited()
    orchestrator._publish_personal_agent_runtime.assert_not_awaited()
    await asyncio.sleep(0)
    assert not any(
        task.get_name().startswith("personal-agent-revision-lease-")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exit_kind",
    ("explicit_stop", "process_exit", "protocol_eof"),
)
@pytest.mark.parametrize("runtime_state", ("stopping", "failed"))
async def test_exact_runtime_exit_acknowledges_registered_stop_waiter(
    exit_kind,
    runtime_state,
):
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    instance.state = runtime_state
    send_entered = asyncio.Event()
    send_may_return = asyncio.Event()

    async def send(target, payload):
        assert target is websocket
        assert json.loads(payload) == {
            "type": "agent_stop",
            "fence": instance.fence.to_dict(),
        }
        waiter = orchestrator._personal_agent_exit_waiters.get(
            instance.fence.runtime_instance_id
        )
        assert waiter is not None
        assert waiter.fence == instance.fence
        assert not waiter.acknowledged.done()
        send_entered.set()
        await send_may_return.wait()
        return True

    orchestrator._safe_send = send
    stop_task = asyncio.create_task(
        orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        )
    )
    await asyncio.wait_for(send_entered.wait(), timeout=1.0)

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind=exit_kind),
    )
    send_may_return.set()
    receipt = await asyncio.wait_for(stop_task, timeout=1.0)

    assert receipt.runtime_instance_id == instance.fence.runtime_instance_id
    waiter = orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ]
    assert waiter.acknowledged.result() == instance.fence
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    expected_proof = (
        "agent_offline" if exit_kind == "explicit_stop" else "child_exited"
    )
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, expected_proof)
    ]

    instance.state = "stopped"
    assert receipt.release() is None
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_exit_waiters
    )


@pytest.mark.asyncio
async def test_physical_exit_commit_ack_loss_replays_before_waiter_resolution():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    waiter = runtime._PersonalAgentExitWaiter(
        fence=instance.fence,
        acknowledged=asyncio.get_running_loop().create_future(),
    )
    orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ] = waiter
    repository = orchestrator.personal_agent_runtime
    record = repository.record_runtime_physical_exit
    calls = 0

    def commit_then_raise(fence, *, proof_code):
        nonlocal calls
        calls += 1
        result = record(fence, proof_code=proof_code)
        if calls == 1:
            raise RuntimeError("physical-exit commit acknowledgement lost")
        return result

    repository.record_runtime_physical_exit = commit_then_raise
    frame = _runtime_exit_frame(instance.fence, exit_kind="process_exit")

    await orchestrator._handle_personal_agent_host_frame(websocket, frame)
    assert not waiter.acknowledged.done()
    assert instance.failure_code == "child_exited"

    await orchestrator._handle_personal_agent_host_frame(websocket, frame)
    assert waiter.acknowledged.result() == instance.fence
    assert calls == 2


@pytest.mark.asyncio
async def test_durable_exit_proof_resolves_pending_waiter_without_duplicate_frame():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    waiter = runtime._PersonalAgentExitWaiter(
        fence=instance.fence,
        acknowledged=asyncio.get_running_loop().create_future(),
    )
    orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ] = waiter
    repository = orchestrator.personal_agent_runtime
    record = repository.record_runtime_physical_exit

    def commit_then_raise(fence, *, proof_code):
        record(fence, proof_code=proof_code)
        raise RuntimeError("physical-exit commit acknowledgement lost")

    repository.record_runtime_physical_exit = commit_then_raise
    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind="process_exit"),
    )
    assert not waiter.acknowledged.done()
    assert instance.failure_code == "child_exited"

    receipt = await asyncio.wait_for(
        orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        ),
        timeout=1.0,
    )

    assert waiter.acknowledged.result() == instance.fence
    orchestrator._safe_send.assert_not_awaited()
    assert receipt.release() is None


@pytest.mark.asyncio
async def test_unowned_stop_replays_ack_lost_settlement_and_projects_once(
    monkeypatch,
):
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    request_id = _uuid()
    request_waiter = asyncio.get_running_loop().create_future()
    orchestrator._personal_agent_request_waiters[request_id] = request_waiter
    orchestrator._personal_agent_request_runtime_fences[request_id] = instance.fence
    successor_request_id = _uuid()
    successor_request_waiter = asyncio.get_running_loop().create_future()
    successor_fence = replace(
        instance.fence,
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=instance.fence.lifecycle_generation + 1,
    )
    orchestrator._personal_agent_request_waiters[
        successor_request_id
    ] = successor_request_waiter
    orchestrator._personal_agent_request_runtime_fences[
        successor_request_id
    ] = successor_fence
    repository = orchestrator.personal_agent_runtime
    repository.settled_request_ids = (request_id,)
    record = repository.record_runtime_physical_exit
    calls = 0

    def commit_then_raise(fence, *, proof_code):
        nonlocal calls
        calls += 1
        settlement = record(fence, proof_code=proof_code)
        if calls == 1:
            repository.settled_request_ids = ()
            raise RuntimeError("physical-exit commit acknowledgement lost")
        return settlement

    repository.record_runtime_physical_exit = commit_then_raise
    projected_socket = SimpleNamespace(owner_sub="owner-060")
    orchestrator._personal_agent_runtime_sockets[
        instance.fence.runtime_instance_id
    ] = projected_socket
    orchestrator.agents[instance.fence.agent_id] = projected_socket
    orchestrator._personal_agent_runtime_authorities[
        instance.fence.runtime_instance_id
    ] = object()
    orchestrator._retire_personal_agent_runtime_authority = AsyncMock()
    orchestrator._emit_personal_agent_lifecycle = AsyncMock()
    forgotten: list[tuple[str, str]] = []
    orchestrator.unregister_governed_dispatch_runtime = (
        lambda agent_id, *, runtime_id: forgotten.append((agent_id, runtime_id))
    )
    monkeypatch.setattr(runtime, "PERSONAL_AGENT_STOP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(runtime, "PERSONAL_AGENT_STOP_RETRY_SECONDS", 0.001)

    driver = asyncio.create_task(
        orchestrator._drive_personal_agent_runtime_stop(
            instance.fence.runtime_instance_id,
            lifecycle_owned=False,
        )
    )
    await _wait_for_exit_waiter(
        orchestrator,
        instance.fence.runtime_instance_id,
    )
    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind="process_exit"),
    )
    await asyncio.wait_for(driver, timeout=1.0)

    assert calls == 2
    assert request_waiter.result().error == {
        "message": "child_exited",
        "retryable": True,
        "code": "child_exited",
    }
    assert not successor_request_waiter.done()
    assert orchestrator._personal_agent_exit_waiters == {}
    assert orchestrator._personal_agent_runtime_sockets == {}
    assert instance.fence.agent_id not in orchestrator.agents
    assert orchestrator._personal_agent_runtime_authorities == {}
    assert forgotten == [
        (instance.fence.agent_id, instance.fence.runtime_instance_id)
    ]
    orchestrator._retire_personal_agent_runtime_authority.assert_awaited_once()
    orchestrator._emit_personal_agent_lifecycle.assert_awaited_once()
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    successor_request_waiter.cancel()


@pytest.mark.asyncio
async def test_unowned_projection_retry_retains_receipt_and_repeats_only_lets(
    monkeypatch,
):
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    request_id = _uuid()
    request_waiter = asyncio.get_running_loop().create_future()
    orchestrator._personal_agent_request_waiters[request_id] = request_waiter
    orchestrator._personal_agent_request_runtime_fences[request_id] = instance.fence
    orchestrator.personal_agent_runtime.settled_request_ids = (request_id,)
    projected_socket = SimpleNamespace(owner_sub="owner-060")
    successor_socket = SimpleNamespace(owner_sub="owner-060")
    orchestrator._personal_agent_runtime_sockets[
        instance.fence.runtime_instance_id
    ] = projected_socket
    orchestrator.agents[instance.fence.agent_id] = projected_socket
    orchestrator._personal_agent_runtime_authorities[
        instance.fence.runtime_instance_id
    ] = object()
    first_retirement_failed = asyncio.Event()
    second_retirement_entered = asyncio.Event()
    retry_may_finish = asyncio.Event()
    retire_calls = 0

    async def retire(**_kwargs):
        nonlocal retire_calls
        retire_calls += 1
        if retire_calls == 1:
            orchestrator.agents[instance.fence.agent_id] = successor_socket
            first_retirement_failed.set()
            raise RuntimeError("LETS retirement temporarily unavailable")
        second_retirement_entered.set()
        await retry_may_finish.wait()

    orchestrator._retire_personal_agent_runtime_authority = retire
    orchestrator._emit_personal_agent_lifecycle = AsyncMock()
    forgotten: list[tuple[str, str]] = []
    orchestrator.unregister_governed_dispatch_runtime = (
        lambda agent_id, *, runtime_id: forgotten.append((agent_id, runtime_id))
    )
    monkeypatch.setattr(runtime, "PERSONAL_AGENT_STOP_RETRY_SECONDS", 0.001)

    driver = asyncio.create_task(
        orchestrator._drive_personal_agent_runtime_stop(
            instance.fence.runtime_instance_id,
            lifecycle_owned=False,
        )
    )
    stop_waiter = await _wait_for_exit_waiter(
        orchestrator,
        instance.fence.runtime_instance_id,
    )
    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind="process_exit"),
    )
    await asyncio.wait_for(first_retirement_failed.wait(), timeout=1.0)
    await asyncio.wait_for(second_retirement_entered.wait(), timeout=1.0)

    assert orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ] is stop_waiter
    assert request_waiter.done()
    assert orchestrator._personal_agent_runtime_sockets == {}
    assert orchestrator.agents[instance.fence.agent_id] is successor_socket
    assert forgotten == [
        (instance.fence.agent_id, instance.fence.runtime_instance_id)
    ]
    orchestrator._emit_personal_agent_lifecycle.assert_awaited_once()

    retry_may_finish.set()
    await asyncio.wait_for(driver, timeout=1.0)

    assert retire_calls == 2
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, "child_exited")
    ]
    assert orchestrator._personal_agent_exit_waiters == {}
    assert orchestrator.agents[instance.fence.agent_id] is successor_socket
    assert forgotten == [
        (instance.fence.agent_id, instance.fence.runtime_instance_id)
    ]
    orchestrator._emit_personal_agent_lifecycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_physical_exit_rereads_waiter_registered_during_plane_commit():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    repository = orchestrator.personal_agent_runtime
    record = repository.record_runtime_physical_exit
    commit_entered = threading.Event()
    commit_may_finish = threading.Event()

    def blocked_record(fence, *, proof_code):
        commit_entered.set()
        assert commit_may_finish.wait(timeout=1.0)
        return record(fence, proof_code=proof_code)

    repository.record_runtime_physical_exit = blocked_record
    exit_task = asyncio.create_task(
        orchestrator._handle_personal_agent_host_frame(
            websocket,
            _runtime_exit_frame(instance.fence, exit_kind="process_exit"),
        )
    )
    assert await asyncio.to_thread(commit_entered.wait, 1.0)

    stop_task = asyncio.create_task(
        orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        )
    )
    registered = await _wait_for_exit_waiter(
        orchestrator,
        instance.fence.runtime_instance_id,
    )
    assert not registered.acknowledged.done()
    commit_may_finish.set()

    await asyncio.wait_for(exit_task, timeout=1.0)
    receipt = await asyncio.wait_for(stop_task, timeout=1.0)

    assert len(orchestrator._personal_agent_exit_waiters.recorded) == 1
    assert registered.acknowledged.result() == instance.fence
    assert registered.settlement is not None
    assert registered.settlement.result().instance is instance
    assert receipt.release() is None


@pytest.mark.asyncio
async def test_physical_exit_racing_waiter_registration_is_retained_for_finalizer():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind="process_exit"),
    )

    waiter = orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ]
    assert waiter.acknowledged.result() == instance.fence
    assert waiter.settlement is not None
    assert waiter.settlement.result().instance is instance
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, "child_exited")
    ]
    receipt = await orchestrator._stop_personal_agent_revision_process(
        instance.fence.runtime_instance_id
    )
    orchestrator._safe_send.assert_not_awaited()
    assert receipt.release() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_kind", "exit_code"),
    (
        ("protocol_eof", 0),
        ("protocol_eof", False),
        ("explicit_stop", "0"),
        ("explicit_stop", {}),
        ("process_exit", None),
        ("process_exit", True),
    ),
)
async def test_malformed_runtime_exit_code_is_a_noop(exit_kind, exit_code):
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    frame = _runtime_exit_frame(instance.fence, exit_kind=exit_kind)
    frame["exit_code"] = exit_code

    await orchestrator._handle_personal_agent_host_frame(websocket, frame)

    assert orchestrator.personal_agent_runtime.physical_exit_calls == []
    assert orchestrator._personal_agent_exit_waiters == {}
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_state_frame_preserves_staged_stop_until_exact_exit():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    bundle_sha256 = "a" * 64
    orchestrator._personal_agent_revision_metadata = AsyncMock(
        return_value=SimpleNamespace(
            runtime_contract_version=3,
            artifact_digest=bundle_sha256,
        )
    )

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        {
            "type": "agent_runtime_state",
            "fence": instance.fence.to_dict(),
            "state": "failed",
            "runtime_contract_version": 3,
            "bundle_sha256": bundle_sha256,
            "observed_at": "2026-08-15T12:00:00Z",
            "reason_code": "child_registration_timeout",
        },
    )

    assert instance.state == "stopping"
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    waiter = await _wait_for_exit_waiter(
        orchestrator,
        instance.fence.runtime_instance_id,
    )
    assert not waiter.acknowledged.done()
    orchestrator._safe_send.assert_awaited_once()

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind="process_exit"),
    )
    await asyncio.gather(*orchestrator._startup_background_tasks)
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, "child_exited")
    ]
    assert instance.fence.runtime_instance_id in (
        orchestrator._personal_agent_exit_waiters
    )
    receipt = await orchestrator._stop_personal_agent_revision_process(
        instance.fence.runtime_instance_id
    )
    assert receipt.release() is None


@pytest.mark.asyncio
async def test_candidate_failed_state_stages_stop_and_wakes_activation_owner():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    instance.state = "starting"
    instance.failure_code = None
    bundle_sha256 = "b" * 64
    orchestrator._personal_agent_revision_metadata = AsyncMock(
        return_value=SimpleNamespace(
            runtime_contract_version=3,
            artifact_digest=bundle_sha256,
        )
    )
    ready_waiter = asyncio.get_running_loop().create_future()
    orchestrator._personal_agent_ready_waiters = {
        instance.fence.runtime_instance_id: ready_waiter
    }
    orchestrator._retire_personal_agent_runtime_authority = AsyncMock()
    orchestrator._emit_personal_agent_lifecycle = AsyncMock()

    def stage_runtime_failure(fence, *, failure_code):
        assert fence == instance.fence
        assert failure_code == "child_registration_timeout"
        instance.state = "stopping"
        instance.failure_code = failure_code
        return instance

    orchestrator.personal_agent_runtime.stage_runtime_failure = stage_runtime_failure

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        {
            "type": "agent_runtime_state",
            "fence": instance.fence.to_dict(),
            "state": "failed",
            "runtime_contract_version": 3,
            "bundle_sha256": bundle_sha256,
            "observed_at": "2026-08-15T12:00:00Z",
            "reason_code": "child_registration_timeout",
        },
    )

    with pytest.raises(RuntimeError, match="child_registration_timeout"):
        await ready_waiter
    stop_waiter = await _wait_for_exit_waiter(
        orchestrator,
        instance.fence.runtime_instance_id,
    )
    assert stop_waiter.failure_code is None
    assert stop_waiter.terminalize_on_ack is False
    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        {
            "type": "agent_runtime_state",
            "fence": instance.fence.to_dict(),
            "state": "failed",
            "runtime_contract_version": 3,
            "bundle_sha256": bundle_sha256,
            "observed_at": "2026-08-15T12:00:01Z",
            "reason_code": "child_registration_timeout",
        },
    )
    orchestrator._safe_send.assert_awaited_once()
    lifecycle_finalizer_stop = asyncio.create_task(
        orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        )
    )

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind="process_exit"),
    )
    receipt = await asyncio.wait_for(lifecycle_finalizer_stop, timeout=1.0)
    assert stop_waiter.acknowledged.result() == instance.fence
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, "child_exited")
    ]
    orchestrator._safe_send.assert_awaited_once()
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    orchestrator._retire_personal_agent_runtime_authority.assert_not_awaited()
    orchestrator._emit_personal_agent_lifecycle.assert_not_awaited()
    assert receipt.release() is None


@pytest.mark.asyncio
async def test_unowned_failed_state_terminalizes_only_after_exact_exit():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    instance.state = "online"
    instance.failure_code = None
    bundle_sha256 = "c" * 64
    orchestrator._personal_agent_revision_metadata = AsyncMock(
        return_value=SimpleNamespace(
            runtime_contract_version=3,
            artifact_digest=bundle_sha256,
        )
    )
    orchestrator._personal_agent_ready_waiters = {}
    request_id = _uuid()
    request_waiter = asyncio.get_running_loop().create_future()
    orchestrator._personal_agent_request_waiters[request_id] = request_waiter
    orchestrator._personal_agent_request_runtime_fences[request_id] = instance.fence
    orchestrator.personal_agent_runtime.settled_request_ids = (request_id,)
    projected_socket = SimpleNamespace(owner_sub="owner-060")
    orchestrator._personal_agent_runtime_sockets[
        instance.fence.runtime_instance_id
    ] = projected_socket
    orchestrator.agents[instance.fence.agent_id] = projected_socket
    orchestrator._personal_agent_runtime_authorities[
        instance.fence.runtime_instance_id
    ] = object()
    orchestrator._retire_personal_agent_runtime_authority = AsyncMock()
    orchestrator._emit_personal_agent_lifecycle = AsyncMock()
    forgotten: list[tuple[str, str]] = []
    orchestrator.unregister_governed_dispatch_runtime = (
        lambda agent_id, *, runtime_id: forgotten.append((agent_id, runtime_id))
    )

    def stage_runtime_failure(fence, *, failure_code):
        assert fence == instance.fence
        instance.state = "stopping"
        instance.failure_code = failure_code
        return instance

    orchestrator.personal_agent_runtime.stage_runtime_failure = stage_runtime_failure
    record_physical_exit = (
        orchestrator.personal_agent_runtime.record_runtime_physical_exit
    )

    def record_after_host_disconnect(fence, *, proof_code):
        settlement = record_physical_exit(fence, proof_code=proof_code)
        orchestrator._personal_agent_host_sessions.clear()
        return settlement

    orchestrator.personal_agent_runtime.record_runtime_physical_exit = (
        record_after_host_disconnect
    )

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        {
            "type": "agent_runtime_state",
            "fence": instance.fence.to_dict(),
            "state": "offline",
            "runtime_contract_version": 3,
            "bundle_sha256": bundle_sha256,
            "observed_at": "2026-08-15T12:00:00Z",
            "reason_code": "agent_offline",
        },
    )
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    stop_waiter = await _wait_for_exit_waiter(
        orchestrator,
        instance.fence.runtime_instance_id,
    )
    assert stop_waiter.terminalize_on_ack is False

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence, exit_kind="protocol_eof"),
    )

    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, "child_exited")
    ]
    await asyncio.gather(*orchestrator._startup_background_tasks)
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_exit_waiters
    )
    assert request_waiter.result().error == {
        "message": "child_exited",
        "retryable": True,
        "code": "child_exited",
    }
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_runtime_sockets
    )
    assert instance.fence.agent_id not in orchestrator.agents
    assert orchestrator._personal_agent_runtime_authorities == {}
    assert forgotten == [
        (instance.fence.agent_id, instance.fence.runtime_instance_id)
    ]
    orchestrator._retire_personal_agent_runtime_authority.assert_awaited_once_with(
        owner_user_id="owner-060",
        fence=instance.fence,
    )
    orchestrator._emit_personal_agent_lifecycle.assert_awaited_once_with(
        "owner-060",
        instance,
        state="failed",
        reason_code="child_exited",
    )


@pytest.mark.asyncio
async def test_staged_stop_driver_retries_network_and_missing_exit(
    monkeypatch,
):
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    instance.state = "online"
    bundle_sha256 = "d" * 64
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_revision_metadata = AsyncMock(
        return_value=SimpleNamespace(
            runtime_contract_version=3,
            artifact_digest=bundle_sha256,
        )
    )

    def stage_runtime_failure(fence, *, failure_code):
        assert fence == instance.fence
        assert failure_code == "agent_offline"
        instance.state = "stopping"
        instance.failure_code = failure_code
        return instance

    orchestrator.personal_agent_runtime.stage_runtime_failure = stage_runtime_failure
    monkeypatch.setattr(runtime, "PERSONAL_AGENT_STOP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(runtime, "PERSONAL_AGENT_STOP_RETRY_SECONDS", 0.001)
    sends = 0

    async def send(_target, _payload):
        nonlocal sends
        sends += 1
        if sends == 3:
            await orchestrator._handle_personal_agent_host_frame(
                websocket,
                _runtime_exit_frame(instance.fence),
            )
        return sends != 1

    orchestrator._safe_send = send
    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        {
            "type": "agent_runtime_state",
            "fence": instance.fence.to_dict(),
            "state": "offline",
            "runtime_contract_version": 3,
            "bundle_sha256": bundle_sha256,
            "observed_at": "2026-08-15T12:00:00Z",
            "reason_code": "agent_offline",
        },
    )
    tasks = tuple(orchestrator._startup_background_tasks)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)

    assert sends == 3
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, "agent_offline")
    ]
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_exit_waiters
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_field", ("host_id", "process_id"))
async def test_wrong_runtime_exit_fence_cannot_resolve_exact_waiter(stale_field):
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    acknowledged = asyncio.get_running_loop().create_future()
    waiter = runtime._PersonalAgentExitWaiter(
        fence=instance.fence,
        acknowledged=acknowledged,
    )
    orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ] = waiter
    stale_fence = replace(instance.fence, **{stale_field: _uuid()})

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(stale_fence),
    )

    assert not acknowledged.done()
    assert (
        orchestrator._personal_agent_exit_waiters[
            instance.fence.runtime_instance_id
        ]
        is waiter
    )
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    acknowledged.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "error_pattern"),
    (
        ("missing_socket", "host socket is unavailable"),
        ("send_false", "stop delivery failed"),
        ("send_exception", "stop transport failed"),
        ("timeout", "stop acknowledgement timed out"),
    ),
)
async def test_stop_failure_cancels_and_removes_only_its_waiter(
    monkeypatch,
    failure_mode,
    error_pattern,
):
    orchestrator, _websocket, instance = _stopping_runtime_orchestrator()
    if failure_mode == "missing_socket":
        orchestrator._personal_agent_session_sockets = {}
    elif failure_mode == "send_false":
        orchestrator._safe_send = AsyncMock(return_value=False)
    elif failure_mode == "send_exception":
        orchestrator._safe_send = AsyncMock(
            side_effect=RuntimeError("stop transport failed")
        )
    else:
        monkeypatch.setattr(runtime, "PERSONAL_AGENT_STOP_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(RuntimeError, match=error_pattern):
        await orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        )

    assert len(orchestrator._personal_agent_exit_waiters.recorded) == 1
    recorded = orchestrator._personal_agent_exit_waiters.recorded[0]
    assert recorded.fence == instance.fence
    assert recorded.acknowledged.cancelled()
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_exit_waiters
    )
    assert instance.state == "stopping"
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ("send_false", "send_exception", "timeout"))
async def test_stop_failure_compare_pop_preserves_newer_waiter(
    monkeypatch,
    failure_mode,
):
    orchestrator, _websocket, instance = _stopping_runtime_orchestrator()
    replacement = runtime._PersonalAgentExitWaiter(
        fence=instance.fence,
        acknowledged=asyncio.get_running_loop().create_future(),
    )

    async def send(_target, _payload):
        orchestrator._personal_agent_exit_waiters[
            instance.fence.runtime_instance_id
        ] = replacement
        if failure_mode == "send_exception":
            raise RuntimeError("stop transport failed")
        return failure_mode != "send_false"

    orchestrator._safe_send = send
    if failure_mode == "timeout":
        monkeypatch.setattr(runtime, "PERSONAL_AGENT_STOP_TIMEOUT_SECONDS", 0.001)

    with pytest.raises(RuntimeError):
        await orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        )

    original = orchestrator._personal_agent_exit_waiters.recorded[0]
    assert original.acknowledged.cancelled()
    assert (
        orchestrator._personal_agent_exit_waiters[
            instance.fence.runtime_instance_id
        ]
        is replacement
    )
    assert not replacement.acknowledged.done()
    replacement.acknowledged.cancel()


@pytest.mark.asyncio
async def test_cancellation_after_stop_send_removes_waiter_for_resend():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    sends = 0
    sent = asyncio.Event()

    async def send(_target, _payload):
        nonlocal sends
        sends += 1
        sent.set()
        return True

    orchestrator._safe_send = send
    first = asyncio.create_task(
        orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        )
    )
    await asyncio.wait_for(sent.wait(), timeout=1.0)
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    first_waiter = orchestrator._personal_agent_exit_waiters.recorded[0]
    assert first_waiter.acknowledged.cancelled()
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_exit_waiters
    )

    sent.clear()
    second = asyncio.create_task(
        orchestrator._stop_personal_agent_revision_process(
            instance.fence.runtime_instance_id
        )
    )
    await asyncio.wait_for(sent.wait(), timeout=1.0)
    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence),
    )
    receipt = await asyncio.wait_for(second, timeout=1.0)

    assert sends == 2
    assert receipt.release() is None


@pytest.mark.asyncio
async def test_completed_stop_receipt_is_reusable_and_duplicate_safe_until_release():
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    acknowledged = asyncio.get_running_loop().create_future()
    acknowledged.set_result(instance.fence)
    waiter = runtime._PersonalAgentExitWaiter(
        fence=instance.fence,
        acknowledged=acknowledged,
    )
    orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ] = waiter

    first = await orchestrator._stop_personal_agent_revision_process(
        instance.fence.runtime_instance_id
    )
    second = await orchestrator._stop_personal_agent_revision_process(
        instance.fence.runtime_instance_id
    )
    orchestrator._safe_send.assert_not_awaited()

    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence),
    )
    assert (
        orchestrator._personal_agent_exit_waiters[
            instance.fence.runtime_instance_id
        ]
        is waiter
    )
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()

    # Plane's exact finalizer commits before the lifecycle owner releases the
    # acknowledgement. A later duplicate is then terminal-state idempotent.
    instance.state = "stopped"
    assert first.release() is None
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_exit_waiters
    )
    await orchestrator._handle_personal_agent_host_frame(
        websocket,
        _runtime_exit_frame(instance.fence),
    )
    assert instance.fence.runtime_instance_id not in (
        orchestrator._personal_agent_exit_waiters
    )
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    assert orchestrator.personal_agent_runtime.physical_exit_calls == [
        (instance.fence, "agent_offline")
    ]
    assert instance.failure_code == "agent_offline"
    with pytest.raises(RuntimeError, match="receipt is stale"):
        second.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_code", ("child_exited", "agent_offline"))
async def test_terminal_exact_exit_proof_never_demands_a_second_stop(proof_code):
    orchestrator, _websocket, instance = _stopping_runtime_orchestrator()
    instance.state = "offline"
    instance.failure_code = proof_code

    receipt = await orchestrator._stop_personal_agent_revision_process(
        instance.fence.runtime_instance_id
    )

    assert receipt is None
    orchestrator._safe_send.assert_not_awaited()
    assert orchestrator._personal_agent_exit_waiters == {}


@pytest.mark.asyncio
async def test_stop_receipt_release_compares_the_exact_waiter_entry():
    orchestrator, _websocket, instance = _stopping_runtime_orchestrator()
    acknowledged = asyncio.get_running_loop().create_future()
    acknowledged.set_result(instance.fence)
    original = runtime._PersonalAgentExitWaiter(
        fence=instance.fence,
        acknowledged=acknowledged,
    )
    orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ] = original
    receipt = await orchestrator._stop_personal_agent_revision_process(
        instance.fence.runtime_instance_id
    )
    replacement = runtime._PersonalAgentExitWaiter(
        fence=instance.fence,
        acknowledged=asyncio.get_running_loop().create_future(),
    )
    orchestrator._personal_agent_exit_waiters[
        instance.fence.runtime_instance_id
    ] = replacement

    with pytest.raises(RuntimeError, match="receipt is stale"):
        receipt.release()

    assert (
        orchestrator._personal_agent_exit_waiters[
            instance.fence.runtime_instance_id
        ]
        is replacement
    )
    replacement.acknowledged.cancel()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_exit_waiters_and_clears_receipts(monkeypatch):
    orchestrator = Orchestrator.__new__(Orchestrator)
    pending = asyncio.get_running_loop().create_future()
    pending_settlement = asyncio.get_running_loop().create_future()
    completed = asyncio.get_running_loop().create_future()
    first_fence = RuntimeFence(
        agent_id="agent-stop-060",
        host_id=_uuid(),
        host_session_id=_uuid(),
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=1,
    )
    second_fence = replace(
        first_fence,
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=2,
    )
    completed.set_result(second_fence)
    orchestrator._personal_agent_exit_waiters = {
        first_fence.runtime_instance_id: runtime._PersonalAgentExitWaiter(
            fence=first_fence,
            acknowledged=pending,
            settlement=pending_settlement,
        ),
        second_fence.runtime_instance_id: runtime._PersonalAgentExitWaiter(
            fence=second_fence,
            acknowledged=completed,
        ),
    }
    orchestrator._startup_background_tasks = set()
    orchestrator.async_task_manager = SimpleNamespace(
        drain=AsyncMock(return_value=0),
        stop_retention_sweep=AsyncMock(),
    )
    monkeypatch.setattr(
        runtime,
        "_unbind_orchestrator_process_consumers",
        lambda _orchestrator: None,
    )

    await orchestrator._close_started_services_once()

    assert pending.cancelled()
    assert pending_settlement.cancelled()
    assert completed.result() == second_fence
    assert orchestrator._personal_agent_exit_waiters == {}
    orchestrator.async_task_manager.drain.assert_awaited_once_with(
        timeout_seconds=5.0
    )
    orchestrator.async_task_manager.stop_retention_sweep.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_near_supervisor_budget_exit_ack_fits_seven_second_bound(
    monkeypatch,
):
    orchestrator, websocket, instance = _stopping_runtime_orchestrator()
    observed_timeouts: list[float] = []
    simulated_ack_seconds = 4.999

    class SimulatedTimeout:
        def __init__(self, seconds):
            observed_timeouts.append(seconds)

        async def __aenter__(self):
            await orchestrator._handle_personal_agent_host_frame(
                websocket,
                _runtime_exit_frame(instance.fence),
            )
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

    monkeypatch.setattr(runtime.asyncio, "timeout", SimulatedTimeout)

    receipt = await orchestrator._stop_personal_agent_revision_process(
        instance.fence.runtime_instance_id
    )

    assert simulated_ack_seconds < observed_timeouts[0]
    assert observed_timeouts == [7.0]
    orchestrator._terminalize_personal_agent_runtime.assert_not_awaited()
    instance.state = "stopped"
    assert receipt.release() is None


@pytest.mark.asyncio
async def test_reconnect_replays_latest_durable_lifecycle_without_live_socket():
    now = datetime.now(UTC)
    fence = RuntimeFence(
        agent_id="agent-replay",
        host_id=_uuid(),
        host_session_id=_uuid(),
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=9,
    )
    terminal = SimpleNamespace(
        fence=fence,
        state="failed",
        state_revision=4,
        failure_code="child_exited",
        created_at=now,
        last_liveness_at=now,
        terminal_at=now,
    )

    class Repository:
        def list_latest_runtime_instances(self, *, owner_user_id):
            assert owner_user_id == "owner-060"
            return (terminal,)

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    websocket = object()
    sent: list[dict] = []

    async def send(target, payload):
        assert target is websocket
        sent.append(json.loads(payload))
        return True

    orchestrator._safe_send = send

    assert await orchestrator._replay_personal_agent_lifecycles(
        websocket, "owner-060"
    ) == 1
    assert sent == [
        {
            "type": "agent_lifecycle",
            "agent_id": fence.agent_id,
            "revision_id": fence.revision_id,
            "runtime_instance_id": fence.runtime_instance_id,
            "lifecycle_generation": 9,
            "state_revision": 4,
            "state": "failed",
            "reason_code": "child_exited",
            "label": "Failed",
            "updated_at": sent[0]["updated_at"],
        }
    ]


@pytest.mark.asyncio
async def test_reconnect_never_projects_host_ready_as_public_online():
    now = datetime.now(UTC)
    existing_revision = _uuid()
    existing_authority = _uuid()
    fresh_fence = RuntimeFence(
        agent_id="agent-first-start",
        host_id=_uuid(),
        host_session_id=_uuid(),
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=1,
    )
    update_fence = RuntimeFence(
        agent_id="agent-update",
        host_id=_uuid(),
        host_session_id=_uuid(),
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=2,
    )
    ready_runtimes = (
        SimpleNamespace(
            fence=fresh_fence,
            state="ready",
            state_revision=3,
            active_revision_id=None,
            authoritative_instance_id=None,
            failure_code=None,
            created_at=now,
            last_liveness_at=now,
            terminal_at=None,
        ),
        SimpleNamespace(
            fence=update_fence,
            state="ready",
            state_revision=6,
            active_revision_id=existing_revision,
            authoritative_instance_id=existing_authority,
            failure_code=None,
            created_at=now,
            last_liveness_at=now,
            terminal_at=None,
        ),
    )

    class Repository:
        def list_latest_runtime_instances(self, *, owner_user_id):
            assert owner_user_id == "owner-060"
            return ready_runtimes

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    websocket = object()
    sent: list[dict] = []

    async def send(target, payload):
        assert target is websocket
        sent.append(json.loads(payload))
        return True

    orchestrator._safe_send = send

    assert await orchestrator._replay_personal_agent_lifecycles(
        websocket, "owner-060"
    ) == 2
    assert [(frame["agent_id"], frame["state"]) for frame in sent] == [
        ("agent-first-start", "starting"),
        ("agent-update", "updating"),
    ]
    assert all(frame["state"] != "online" for frame in sent)


@pytest.mark.asyncio
async def test_structured_host_ack_is_emitted_only_after_durable_registration():
    websocket = object()
    record = _host_record()
    events: list[str] = []

    class Repository:
        def register_host_session(self, **kwargs):
            assert kwargs["owner_user_id"] == record.owner_user_id
            assert kwargs["host_id"] == record.host_id
            events.append("durable_registration")
            return record

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    orchestrator._connection_contexts = {}
    orchestrator._personal_agent_host_sessions = {}
    orchestrator._personal_agent_session_sockets = {}
    orchestrator._agent_host_sockets = {}

    async def send(target, payload):
        assert target is websocket
        assert events == ["durable_registration"]
        events.append("ack")
        sent.append(json.loads(payload))

    sent: list[dict] = []
    orchestrator._safe_send = send
    registration = AgentHostRegistration(
        host_id=record.host_id,
        supported_runtime_contract_versions=(BYO_RUNTIME_CONTRACT_VERSION,),
        runtime_lock_sha256="a" * 64,
        platform="windows",
        client_version="0.4.0",
    )

    accepted = await orchestrator._register_personal_agent_host(
        websocket,
        owner_user_id=record.owner_user_id,
        registration=registration,
    )

    assert accepted == record
    assert events == ["durable_registration", "ack"]
    assert sent == [
        {
            "type": "agent_host_registered",
            "host_id": record.host_id,
            "host_session_id": record.host_session_id,
            "inventory_required": True,
            "accepted_at": Orchestrator._rfc3339(record.accepted_at),
        }
    ]
    assert orchestrator._personal_agent_host_sessions[id(websocket)] == record
    assert (
        orchestrator._personal_agent_session_sockets[record.host_session_id]
        is websocket
    )


@pytest.mark.asyncio
async def test_disconnect_commits_before_removing_socket_projections():
    websocket = object()
    record = _host_record()
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._personal_agent_host_sessions = {id(websocket): record}
    orchestrator._personal_agent_session_sockets = {
        record.host_session_id: websocket
    }
    orchestrator._agent_host_sockets = {id(websocket): record.host_session_id}
    orchestrator._personal_agent_runtime_sockets = {}
    orchestrator.agents = {}
    orchestrator._fail_personal_agent_waiters = AsyncMock()

    class Repository:
        def disconnect_host_session(self, fence, *, failure_code):
            assert fence == record.fence
            assert failure_code == "host_lost"
            assert orchestrator._personal_agent_host_sessions[id(websocket)] == record
            assert (
                orchestrator._personal_agent_session_sockets[
                    record.host_session_id
                ]
                is websocket
            )
            return SimpleNamespace(
                settled_request_ids=("request-1",), selected_sessions={}
            )

    orchestrator.personal_agent_runtime = Repository()
    result = await orchestrator._disconnect_personal_agent_host(websocket)

    assert result.settled_request_ids == ("request-1",)
    assert id(websocket) not in orchestrator._personal_agent_host_sessions
    assert record.host_session_id not in orchestrator._personal_agent_session_sockets
    assert id(websocket) not in orchestrator._agent_host_sockets
    orchestrator._fail_personal_agent_waiters.assert_awaited_once_with(
        ("request-1",), code="host_lost"
    )


@pytest.mark.asyncio
async def test_disconnect_database_failure_preserves_socket_projections():
    websocket = object()
    record = _host_record()
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._personal_agent_host_sessions = {id(websocket): record}
    orchestrator._personal_agent_session_sockets = {
        record.host_session_id: websocket
    }
    orchestrator._agent_host_sockets = {id(websocket): record.host_session_id}

    class Repository:
        def disconnect_host_session(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    orchestrator.personal_agent_runtime = Repository()
    assert await orchestrator._disconnect_personal_agent_host(websocket) is None
    assert orchestrator._personal_agent_host_sessions[id(websocket)] == record
    assert (
        orchestrator._personal_agent_session_sockets[record.host_session_id]
        is websocket
    )
    assert orchestrator._agent_host_sockets[id(websocket)] == record.host_session_id


@pytest.mark.asyncio
async def test_disconnect_delivers_rehashed_artifact_only_to_selected_standby():
    lost_socket = object()
    standby_socket = object()
    lost = _host_record()
    selected_session_id = _uuid()
    revision_id = _uuid()
    runtime_fence = RuntimeFence(
        agent_id="agent-recovery",
        host_id=_uuid(),
        host_session_id=selected_session_id,
        delivery_id=_uuid(),
        revision_id=revision_id,
        runtime_instance_id=_uuid(),
        process_id=None,
        lifecycle_generation=7,
    )
    operation_fence = ExecutionFence(uuid.uuid4(), 2, uuid.uuid4())
    revision = SimpleNamespace(
        revision_id=revision_id,
        artifact_relative_path=(
            f"revisions/agent-recovery/{revision_id}"
        ),
        artifact_digest="b" * 64,
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        release_lock_digest="a" * 64,
        manifest={
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": "a" * 64,
        },
    )
    recovery = SimpleNamespace(
        host=SimpleNamespace(host_session_id=selected_session_id),
        revision=revision,
        instance=SimpleNamespace(fence=runtime_fence),
    )
    artifact = SimpleNamespace(
        bundle_sha256="b" * 64,
        files={
            "agent_card.py": "CARD",
            "mcp_tools.py": "TOOLS",
            "agent_main.py": "MAIN",
        },
        manifest={
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": "a" * 64,
        },
    )
    events: list[str] = []

    class Repository:
        def disconnect_host_session(self, fence, *, failure_code):
            assert fence == lost.fence
            assert failure_code == "host_lost"
            events.append("disconnect_commit")
            return SimpleNamespace(
                settled_request_ids=(),
                selected_sessions={"agent-recovery": selected_session_id},
            )

        def create_selected_recovery_instance(self, **kwargs):
            assert kwargs == {
                "owner_user_id": lost.owner_user_id,
                "agent_id": "agent-recovery",
                "operation_fence": operation_fence,
            }
            assert lost.host_session_id not in (
                orchestrator._personal_agent_session_sockets
            )
            events.append("recovery_allocated")
            return recovery

    class Artifacts:
        def load(
            self,
            path,
            *,
            expected_digest,
            expected_manifest_digest,
        ):
            assert path == revision.artifact_relative_path
            assert expected_digest == revision.artifact_digest
            assert expected_manifest_digest == (
                canonical_generated_agent_manifest_digest(revision.manifest)
            )
            events.append("artifact_rehashed")
            return artifact

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    orchestrator.personal_agent_artifacts = Artifacts()
    orchestrator._personal_agent_host_sessions = {id(lost_socket): lost}
    orchestrator._personal_agent_session_sockets = {
        lost.host_session_id: lost_socket,
        selected_session_id: standby_socket,
    }
    orchestrator._agent_host_sockets = {id(lost_socket): lost.host_session_id}
    orchestrator._personal_agent_runtime_sockets = {}
    orchestrator.agents = {}
    orchestrator._fail_personal_agent_waiters = AsyncMock()

    async def claim(**kwargs):
        assert kwargs["owner_user_id"] == lost.owner_user_id
        assert kwargs["operation_kind"] == "agent_runtime_delivery"
        assert kwargs["idempotency_namespace"] == (
            "personal_agent_standby_recovery"
        )
        assert len(kwargs["idempotency_key"]) == 64
        events.append("operation_claimed")
        return SimpleNamespace(), SimpleNamespace(fence=operation_fence)

    sent: list[dict] = []

    async def send(target, payload):
        assert target is standby_socket
        assert events == [
            "disconnect_commit",
            "operation_claimed",
            "recovery_allocated",
            "artifact_rehashed",
        ]
        events.append("selected_send")
        sent.append(json.loads(payload))
        return True

    orchestrator._claim_personal_agent_operation = claim
    orchestrator._safe_send = send

    result = await orchestrator._disconnect_personal_agent_host(lost_socket)

    assert result.selected_sessions == {
        "agent-recovery": selected_session_id
    }
    assert events == [
        "disconnect_commit",
        "operation_claimed",
        "recovery_allocated",
        "artifact_rehashed",
        "selected_send",
    ]
    assert sent == [
        {
            "type": "agent_bundle_deliver",
            "fence": runtime_fence.to_dict(),
            "authority": None,
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": "a" * 64,
            "bundle_sha256": "b" * 64,
            "files": artifact.files,
        }
    ]
    assert lost.host_session_id not in orchestrator._personal_agent_session_sockets
    assert orchestrator._personal_agent_session_sockets[selected_session_id] is (
        standby_socket
    )


@pytest.mark.asyncio
async def test_selected_standby_send_failure_terminalizes_allocated_runtime():
    selected_session_id = _uuid()
    revision_id = _uuid()
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    runtime_fence = RuntimeFence(
        agent_id="agent-recovery",
        host_id=_uuid(),
        host_session_id=selected_session_id,
        delivery_id=_uuid(),
        revision_id=revision_id,
        runtime_instance_id=_uuid(),
        process_id=None,
        lifecycle_generation=9,
    )
    revision = SimpleNamespace(
        artifact_relative_path=f"revisions/agent-recovery/{revision_id}",
        artifact_digest="c" * 64,
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        release_lock_digest="d" * 64,
        manifest={
            "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
            "required_runtime_lock_sha256": "d" * 64,
        },
    )
    recovery = SimpleNamespace(
        host=SimpleNamespace(host_session_id=selected_session_id),
        revision=revision,
        instance=SimpleNamespace(fence=runtime_fence),
    )

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = SimpleNamespace(
        create_selected_recovery_instance=lambda **_kwargs: recovery
    )
    orchestrator.personal_agent_artifacts = SimpleNamespace(
        load=lambda *_args, **_kwargs: SimpleNamespace(
            bundle_sha256="c" * 64,
            files={"agent_card.py": "", "mcp_tools.py": "", "agent_main.py": ""},
            manifest={
                "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
                "required_runtime_lock_sha256": "d" * 64,
            },
        )
    )
    orchestrator._personal_agent_session_sockets = {
        selected_session_id: object()
    }
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(
            SimpleNamespace(),
            SimpleNamespace(fence=operation_fence),
        )
    )
    orchestrator._safe_send = AsyncMock(return_value=False)
    orchestrator._terminalize_personal_agent_runtime = AsyncMock()

    assert not await orchestrator._recover_personal_agent_on_selected_standby(
        owner_user_id="owner-060",
        agent_id="agent-recovery",
        lost_host_session_id=_uuid(),
        selected_host_session_id=selected_session_id,
    )
    orchestrator._terminalize_personal_agent_runtime.assert_awaited_once_with(
        runtime_fence,
        failure_code="standby_recovery_failed",
    )


@pytest.mark.asyncio
async def test_inventory_is_committed_before_the_complete_action_frame_is_sent():
    websocket = object()
    host = _host_record()
    revision_id = _uuid()
    inventory_id = _uuid()
    digest = "b" * 64
    operation_fence = ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())
    delivery = HostInventorySelectedDelivery(
        delivery_id=_uuid(),
        runtime_instance_id=_uuid(),
        lifecycle_generation=9,
        runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
        required_runtime_lock_sha256="a" * 64,
        bundle_sha256=digest,
    )
    reconciled_host = SimpleNamespace(
        host_id=host.host_id,
        host_session_id=host.host_session_id,
    )
    reconciliation = HostInventoryReconciliation(
        host=reconciled_host,
        inventory_id=inventory_id,
        actions=(
            HostInventoryAction(
                agent_id="agent-060",
                revision_id=revision_id,
                action="start",
                reason_code=None,
                selected_delivery=delivery,
            ),
        ),
        reconciled_at=datetime.now(UTC),
    )
    starting_runtime = SimpleNamespace(
        fence=SimpleNamespace(runtime_instance_id=delivery.runtime_instance_id)
    )
    sent: list[dict] = []
    events: list[str] = []

    class Repository:
        def get_selected_session_revision(self, fence, *, agent_id):
            assert fence == host.fence
            return SimpleNamespace(
                revision=SimpleNamespace(
                    revision_id=revision_id,
                    artifact_digest=digest,
                    runtime_contract_version=BYO_RUNTIME_CONTRACT_VERSION,
                    release_lock_digest="a" * 64,
                    state="active",
                )
            )

        def reconcile_host_inventory(self, fence, **kwargs):
            assert sent == []
            assert events == ["operation_claimed"]
            assert fence == host.fence
            assert kwargs["delivery_operation_fences"] == {
                ("agent-060", revision_id): operation_fence
            }
            events.append("durable_reconciliation")
            return reconciliation

        def get_runtime_instance(self, runtime_instance_id):
            assert runtime_instance_id == delivery.runtime_instance_id
            assert events == ["operation_claimed", "durable_reconciliation"]
            events.append("runtime_loaded")
            return starting_runtime

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    orchestrator._personal_agent_host_sessions = {id(websocket): host}
    orchestrator._personal_agent_session_sockets = {
        host.host_session_id: websocket
    }

    async def claim(**_kwargs):
        events.append("operation_claimed")
        return SimpleNamespace(), SimpleNamespace(fence=operation_fence)

    async def send(target, payload):
        assert target is websocket
        assert events == [
            "operation_claimed",
            "durable_reconciliation",
            "runtime_loaded",
        ]
        sent.append(json.loads(payload))
        events.append("response_sent")
        return True

    async def emit_lifecycle(owner_user_id, runtime, *, state, reason_code=None):
        assert owner_user_id == host.owner_user_id
        assert runtime is starting_runtime
        assert state == "starting"
        assert reason_code is None
        assert events[-1] == "response_sent"
        events.append("lifecycle_sent")

    orchestrator._claim_personal_agent_operation = claim
    orchestrator._safe_send = send
    orchestrator._emit_personal_agent_lifecycle = emit_lifecycle
    frame = {
        "type": "agent_host_inventory",
        "host_id": host.host_id,
        "host_session_id": host.host_session_id,
        "inventory_id": inventory_id,
        "entries": [
            {
                "agent_id": "agent-060",
                "revision_id": revision_id,
                "bundle_sha256": digest,
                "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
                "required_runtime_lock_sha256": "a" * 64,
            }
        ],
    }

    await orchestrator._reconcile_personal_agent_inventory(websocket, frame)

    assert sent[0]["type"] == "agent_host_inventory_reconciled"
    assert sent[0]["actions"] == [
        {
            "agent_id": "agent-060",
            "revision_id": revision_id,
            "action": "start",
            "reason_code": None,
            "selected_delivery": {
                "delivery_id": delivery.delivery_id,
                "runtime_instance_id": delivery.runtime_instance_id,
                "lifecycle_generation": 9,
                "authority": None,
                "runtime_contract_version": BYO_RUNTIME_CONTRACT_VERSION,
                "required_runtime_lock_sha256": "a" * 64,
                "bundle_sha256": digest,
            },
        }
    ]
    assert events == [
        "operation_claimed",
        "durable_reconciliation",
        "runtime_loaded",
        "response_sent",
        "lifecycle_sent",
    ]


@pytest.mark.asyncio
async def test_late_request_waiter_observes_exit_that_projected_before_registration():
    fence = RuntimeFence(
        agent_id="agent-request-race",
        host_id=_uuid(),
        host_session_id=_uuid(),
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=41,
    )
    request_id = _uuid()
    request_generation = _uuid()
    authority = SimpleNamespace(fence=fence)
    request = SimpleNamespace(
        fence=SimpleNamespace(
            request_id=request_id,
            request_generation=request_generation,
        )
    )
    terminal = SimpleNamespace(
        fence=fence,
        state="offline",
        is_authoritative=False,
        failure_code="child_exited",
    )
    events: list[str] = []

    class Repository:
        def get_current_online_authority(self, **_kwargs):
            return authority

        def assign_request(self, assigned_fence, **_kwargs):
            assert assigned_fence == fence
            events.append("request_assigned")
            return request

        def get_runtime_instance(self, runtime_instance_id):
            assert runtime_instance_id == fence.runtime_instance_id
            assert events == ["request_assigned"]
            events.append("terminal_rechecked")
            return terminal

    socket = SimpleNamespace(
        owner_sub="owner-060",
        agent_id=fence.agent_id,
        runtime_fence=fence,
        send_fenced=AsyncMock(),
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    orchestrator._claim_personal_agent_operation = AsyncMock(
        return_value=(
            SimpleNamespace(owner_user_id="owner-060"),
            SimpleNamespace(fence=ExecutionFence(uuid.uuid4(), 1, uuid.uuid4())),
        )
    )
    orchestrator._renew_personal_agent_operation_lease = AsyncMock()
    orchestrator._personal_agent_request_waiters = {}
    orchestrator._personal_agent_request_runtime_fences = {}
    orchestrator._pending_request_agent = {}
    orchestrator._dispatch_context = {}
    orchestrator._register_dispatch_context = lambda *_args: None

    response = await orchestrator._execute_via_personal_runtime(
        socket,
        "race_tool",
        {},
        timeout=1.0,
        ui_websocket=None,
    )

    assert events == ["request_assigned", "terminal_rechecked"]
    assert response.request_id == request_id
    assert response.error == {
        "message": "child_exited",
        "retryable": True,
        "code": "child_exited",
    }
    socket.send_fenced.assert_not_awaited()
    assert orchestrator._personal_agent_request_waiters == {}
    assert orchestrator._personal_agent_request_runtime_fences == {}
    assert orchestrator._pending_request_agent == {}


@pytest.mark.asyncio
async def test_fenced_result_settles_durably_before_waking_the_caller():
    websocket = object()
    host = _host_record()
    runtime_fence = RuntimeFence(
        agent_id="agent-060",
        host_id=host.host_id,
        host_session_id=host.host_session_id,
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=12,
    )
    request_id = _uuid()
    request_generation = _uuid()
    request_fence = SimpleNamespace(
        runtime=runtime_fence,
        request_id=request_id,
        request_generation=request_generation,
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._personal_agent_host_sessions = {id(websocket): host}
    orchestrator._personal_agent_session_sockets = {
        host.host_session_id: websocket
    }
    waiter = asyncio.get_running_loop().create_future()
    orchestrator._personal_agent_request_waiters = {request_id: waiter}
    events: list[str] = []

    class Repository:
        def get_runtime_request(self, value):
            assert value == request_id
            return SimpleNamespace(fence=request_fence)

        def settle_request(self, fence, **kwargs):
            assert fence is request_fence
            assert not waiter.done()
            assert kwargs["state"] == "completed"
            events.append("durable_settlement")

    orchestrator.personal_agent_runtime = Repository()
    frame = {
        "type": "mcp_response",
        "request_id": request_id,
        "request_generation": request_generation,
        "fence": runtime_fence.to_dict(),
        "result": {"ok": True},
        "result_type": "complete",
        "responder_info": {"name": "agent-060", "version": "1.0.0"},
    }

    await orchestrator._handle_personal_agent_result(websocket, frame)

    assert events == ["durable_settlement"]
    assert waiter.done()
    assert waiter.result().result == {"ok": True}
    assert waiter.result().result_type == "complete"
    assert waiter.result().responder_info == {
        "name": "agent-060",
        "version": "1.0.0",
    }


@pytest.mark.asyncio
async def test_fenced_result_refuses_unknown_field_before_repository_access():
    orchestrator = Orchestrator.__new__(Orchestrator)
    frame = {
        "type": "mcp_response",
        "request_id": _uuid(),
        "request_generation": _uuid(),
        "fence": {},
        "future_field": True,
    }
    with pytest.raises(ProtocolValidationError, match="response fields are invalid"):
        await orchestrator._handle_personal_agent_result(object(), frame)


@pytest.mark.asyncio
async def test_terminal_runtime_publishes_owner_lifecycle_after_commit():
    host = _host_record()
    owner_socket = object()
    other_socket = object()
    fence = RuntimeFence(
        agent_id="agent-lifecycle",
        host_id=host.host_id,
        host_session_id=host.host_session_id,
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=18,
    )
    terminal = SimpleNamespace(
        fence=fence,
        state_revision=5,
        failure_code="child_exited",
    )
    events: list[str] = []

    class Repository:
        def terminalize_runtime(self, value, *, failure_code):
            assert value == fence
            assert failure_code == "child_exited"
            events.append("durable_terminal")
            return SimpleNamespace(
                instance=terminal,
                settled_request_ids=(),
            )

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    orchestrator._personal_agent_host_sessions = {1: host}
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_request_waiters = {}
    orchestrator._personal_agent_runtime_sockets = {}
    orchestrator.agents = {}
    orchestrator.ui_clients = {owner_socket, other_socket}
    orchestrator.ui_sessions = {
        owner_socket: {"sub": host.owner_user_id},
        other_socket: {"sub": "other-owner"},
    }
    sent: list[tuple[object, dict]] = []

    async def send(target, payload):
        assert events == ["durable_terminal"]
        sent.append((target, json.loads(payload)))
        return True

    orchestrator._safe_send = send

    await orchestrator._terminalize_personal_agent_runtime(
        fence,
        failure_code="child_exited",
    )

    assert sent == [
        (
            owner_socket,
            {
                "type": "agent_lifecycle",
                "agent_id": fence.agent_id,
                "revision_id": fence.revision_id,
                "runtime_instance_id": fence.runtime_instance_id,
                "lifecycle_generation": 18,
                "state_revision": 5,
                "state": "failed",
                "reason_code": "child_exited",
                "label": "Failed",
                "updated_at": sent[0][1]["updated_at"],
            },
        )
    ]


@pytest.mark.asyncio
async def test_one_second_generic_phase_is_durable_and_canonical(monkeypatch):
    monkeypatch.setattr(runtime, "OPERATION_PROGRESS_PHASE_SECONDS", 0.001)
    operation_id = uuid.uuid4()
    operation_fence = ExecutionFence(operation_id, 3, uuid.uuid4())
    context = SimpleNamespace(
        websocket=object(),
        connection_generation=uuid.uuid4(),
    )
    frame = SimpleNamespace(
        operation_kind="chat_message",
        action="chat_message",
        surface="chat",
        chat_id=None,
        request_generation=uuid.uuid4(),
    )
    work = SimpleNamespace(
        frame=frame,
        fence=operation_fence,
        owner=SimpleNamespace(),
        operation_id=operation_id,
        subscribers={},
    )
    events: list[str] = []

    class Admission:
        def update_phase(self, fence, phase):
            assert fence == operation_fence
            assert phase == "running"
            events.append("durable_phase")
            return SimpleNamespace(
                operation_id=operation_id,
                state_revision=4,
                updated_at=datetime.now(UTC),
            )

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.work_admission = Admission()

    async def call(method, *args, **kwargs):
        return method(*args, **kwargs)

    sent: list[dict] = []

    async def send(target, payload):
        assert target is context.websocket
        assert events == ["durable_phase"]
        sent.append(json.loads(payload))
        return True

    orchestrator._call_work_admission = call
    orchestrator._safe_send = send

    await orchestrator._emit_long_running_operation_phase(context, work)

    assert sent[0] == {
        "type": "operation_status",
        "operation_id": str(operation_id),
        "action": "chat_message",
        "surface": "chat",
        "chat_id": None,
        "connection_generation": str(context.connection_generation),
        "request_generation": str(frame.request_generation),
        "sequence": 4,
        "state": "running",
        "phase": "running",
        "label": "Working…",
        "terminal": False,
        "retryable": False,
        "error": None,
        "retry_after_ms": None,
        "updated_at": sent[0]["updated_at"],
    }


@pytest.mark.asyncio
async def test_personal_agent_call_lease_is_renewed_until_stop(monkeypatch):
    monkeypatch.setattr(runtime, "CONNECTION_LEASE_RENEW_SECONDS", 0.001)
    fence = ExecutionFence(uuid.uuid4(), 4, uuid.uuid4())
    renewals: list[ExecutionFence] = []

    class Admission:
        _repository = object()

        def renew_execution_lease(self, value):
            renewals.append(value)

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.work_admission = Admission()
    stop = asyncio.Event()
    task = asyncio.create_task(
        orchestrator._renew_personal_agent_operation_lease(fence, stop)
    )
    for _ in range(100):
        if len(renewals) >= 2:
            break
        await asyncio.sleep(0.001)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert len(renewals) >= 2
    assert set(renewals) == {fence}


@pytest.mark.asyncio
async def test_watchdog_uses_atomic_database_deadline_transitions():
    host = _host_record()

    def fenced(state: str) -> SimpleNamespace:
        return SimpleNamespace(
            state=state,
            fence=RuntimeFence(
                agent_id=f"agent-{state}",
                host_id=host.host_id,
                host_session_id=host.host_session_id,
                delivery_id=_uuid(),
                revision_id=_uuid(),
                runtime_instance_id=_uuid(),
                process_id=(None if state == "delivering" else _uuid()),
                lifecycle_generation=20,
            ),
        )

    delivering = fenced("delivering")
    online = fenced("online")
    instances = {
        delivering.fence.runtime_instance_id: delivering,
        online.fence.runtime_instance_id: online,
    }
    events: list[tuple] = []

    class Repository:
        def list_expired_runtime_candidates(
            self,
            *,
            startup_timeout_seconds,
            liveness_timeout_seconds,
        ):
            assert (
                startup_timeout_seconds
                == runtime.PERSONAL_AGENT_STARTUP_TIMEOUT_SECONDS
            )
            assert (
                liveness_timeout_seconds
                == runtime.PERSONAL_AGENT_HEARTBEAT_TIMEOUT_SECONDS
            )
            return (
                SimpleNamespace(
                    runtime_instance_id=delivering.fence.runtime_instance_id,
                    owner_id=host.owner_user_id,
                    state="delivering",
                    reason="startup",
                ),
                SimpleNamespace(
                    runtime_instance_id=online.fence.runtime_instance_id,
                    owner_id=host.owner_user_id,
                    state="online",
                    reason="liveness",
                ),
            )

        def get_runtime_instance(self, runtime_id):
            return instances[runtime_id]

        def terminalize_expired_startup(self, fence, *, timeout_seconds):
            events.append(("startup", fence.runtime_instance_id, timeout_seconds))
            return SimpleNamespace(
                instance=SimpleNamespace(
                    fence=fence, failure_code="child_registration_timeout"
                ),
                settled_request_ids=("startup-request",),
            )

        def terminalize_expired_liveness(self, fence, *, timeout_seconds):
            events.append(("liveness", fence.runtime_instance_id, timeout_seconds))
            return SimpleNamespace(
                instance=SimpleNamespace(fence=fence, failure_code="child_hung"),
                settled_request_ids=("hung-request",),
            )

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.personal_agent_runtime = Repository()
    orchestrator._personal_agent_session_sockets = {
        host.host_session_id: object()
    }
    orchestrator._personal_agent_ready_waiters = {}
    orchestrator._personal_agent_runtime_sockets = {}
    orchestrator.agents = {}
    failed: list[tuple] = []
    sent: list[dict] = []

    async def fail_waiters(request_ids, *, code):
        failed.append((tuple(request_ids), code))

    async def send(_socket, payload):
        sent.append(json.loads(payload))

    orchestrator._fail_personal_agent_waiters = fail_waiters
    orchestrator._safe_send = send

    assert await orchestrator._personal_agent_watchdog_once() == 2
    assert events == [
        (
            "startup",
            delivering.fence.runtime_instance_id,
            runtime.PERSONAL_AGENT_STARTUP_TIMEOUT_SECONDS,
        ),
        (
            "liveness",
            online.fence.runtime_instance_id,
            runtime.PERSONAL_AGENT_HEARTBEAT_TIMEOUT_SECONDS,
        ),
    ]
    assert failed == [
        (("startup-request",), "child_registration_timeout"),
        (("hung-request",), "child_hung"),
    ]
    # A never-launched delivery has no process to stop; the hung child does.
    assert sent == [{"type": "agent_stop", "fence": online.fence.to_dict()}]


@pytest.mark.asyncio
async def test_delete_cleans_exact_tombstone_before_routes_or_stops(
    monkeypatch,
):
    owner = "owner-060"
    agent_id = "agent-delete"
    websocket = SimpleNamespace()
    fence = RuntimeFence(
        agent_id=agent_id,
        host_id=_uuid(),
        host_session_id=_uuid(),
        delivery_id=_uuid(),
        revision_id=_uuid(),
        runtime_instance_id=_uuid(),
        process_id=_uuid(),
        lifecycle_generation=31,
    )
    projected = SimpleNamespace(
        owner_sub=owner,
        agent_id=agent_id,
        runtime_fence=fence,
        ui_websocket=websocket,
    )
    tombstone = AgentTombstone(
        agent_id=agent_id,
        owner_user_id=owner,
        lifecycle_generation=32,
        state_revision=8,
        deleted_at=1_700_000_000_000,
    )
    cleanup = SimpleNamespace(
        settlements=(
            SimpleNamespace(instance=SimpleNamespace(fence=fence)),
        ),
        settled_request_ids=("request-delete",),
    )
    events: list[str] = []

    monkeypatch.setattr(
        user_agents,
        "get_user_agent",
        lambda _db, value: (
            {
                "agent_id": agent_id,
                "owner_user_id": owner,
                "state_revision": 7,
            }
            if value == agent_id
            else None
        ),
    )

    class Repository:
        def tombstone_agent(self, **kwargs):
            assert kwargs == {
                "owner_user_id": owner,
                "agent_id": agent_id,
                "expected_state_revision": 7,
            }
            assert orchestrator.agents[agent_id] is projected
            events.append("tombstone")
            return tombstone

        def cleanup_tombstoned_agent(self, value):
            assert value == tombstone
            assert events == ["tombstone"]
            assert orchestrator.agents[agent_id] is projected
            events.append("cleanup")
            return cleanup

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.user_agent_registry = object()
    orchestrator.personal_agent_runtime = Repository()
    orchestrator._personal_agent_runtime_sockets = {
        fence.runtime_instance_id: projected
    }
    orchestrator._personal_agent_session_sockets = {
        fence.host_session_id: websocket
    }
    orchestrator._tunnel_sockets = {}
    orchestrator.agents = {agent_id: projected}
    orchestrator.agent_cards = {agent_id: object()}
    orchestrator.ui_clients = set()

    async def fail_waiters(request_ids, *, code):
        assert events == ["tombstone", "cleanup"]
        assert tuple(request_ids) == ("request-delete",)
        assert code == "agent_deleted"
        events.append("wake")

    async def send(target, payload):
        assert target is websocket
        assert agent_id not in orchestrator.agents
        assert events == ["tombstone", "cleanup", "wake"]
        assert json.loads(payload) == {
            "type": "agent_stop",
            "fence": fence.to_dict(),
        }
        events.append("stop")

    orchestrator._fail_personal_agent_waiters = fail_waiters
    orchestrator._safe_send = send
    orchestrator._audit_user_agent = AsyncMock()

    assert await orchestrator.delete_user_agent(owner, agent_id) is True
    assert events == ["tombstone", "cleanup", "wake", "stop"]
    assert agent_id not in orchestrator.agents
    assert agent_id not in orchestrator.agent_cards
    assert fence.runtime_instance_id not in orchestrator._personal_agent_runtime_sockets
