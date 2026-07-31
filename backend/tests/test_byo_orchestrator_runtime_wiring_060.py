"""Focused production-adapter tests for the feature-060 BYO runtime.

The PostgreSQL state machines have their own fault-injection suites.  These
tests pin the orchestration ordering at the boundary where durable transitions
are projected onto live sockets, so a future refactor cannot accidentally make
an in-memory map or a WebSocket acknowledgement authoritative.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import orchestrator.orchestrator as runtime
import orchestrator.user_agents as user_agents
from orchestrator.orchestrator import Orchestrator
from orchestrator.user_agents import (
    AgentTombstone,
    HostInventoryAction,
    HostInventoryReconciliation,
    HostInventorySelectedDelivery,
    HostSessionRecord,
)
from orchestrator.work_admission import ExecutionFence
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
        supported_runtime_contract_versions=(2,),
        runtime_contract_version=2,
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
        supported_runtime_contract_versions=(2,),
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
        runtime_contract_version=2,
        release_lock_digest="a" * 64,
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
            "runtime_contract_version": 2,
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
        def load(self, path, *, expected_digest):
            assert path == revision.artifact_relative_path
            assert expected_digest == revision.artifact_digest
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
            "runtime_contract_version": 2,
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
        runtime_contract_version=2,
        release_lock_digest="d" * 64,
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
                "runtime_contract_version": 2,
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
        runtime_contract_version=2,
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
                    runtime_contract_version=2,
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
            assert events == [
                "operation_claimed",
                "durable_reconciliation",
                "response_sent",
            ]
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
        assert events == ["operation_claimed", "durable_reconciliation"]
        sent.append(json.loads(payload))
        events.append("response_sent")
        return True

    async def emit_lifecycle(owner_user_id, runtime, *, state, reason_code=None):
        assert owner_user_id == host.owner_user_id
        assert runtime is starting_runtime
        assert state == "starting"
        assert reason_code is None
        assert events[-1] == "runtime_loaded"
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
                "runtime_contract_version": 2,
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
                "runtime_contract_version": 2,
                "required_runtime_lock_sha256": "a" * 64,
                "bundle_sha256": digest,
            },
        }
    ]
    assert events == [
        "operation_claimed",
        "durable_reconciliation",
        "response_sent",
        "runtime_loaded",
        "lifecycle_sent",
    ]


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

    class Database:
        def fetch_all(self, query, params):
            assert "state IN ('delivering', 'starting')" in query
            assert params == (
                runtime.PERSONAL_AGENT_STARTUP_TIMEOUT_SECONDS,
                runtime.PERSONAL_AGENT_HEARTBEAT_TIMEOUT_SECONDS,
            )
            return [
                {
                    "runtime_instance_id": delivering.fence.runtime_instance_id,
                    "state": "delivering",
                },
                {
                    "runtime_instance_id": online.fence.runtime_instance_id,
                    "state": "online",
                },
            ]

    class Repository:
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
    orchestrator.history = SimpleNamespace(db=Database())
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
    orchestrator.history = SimpleNamespace(db=object())
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
