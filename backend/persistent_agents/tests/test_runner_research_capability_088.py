"""Tests for persistent_agents/runner.py's fixed research capability against real Plane,
session and claim boundaries: unsupported profiles stay untouched and never starve
supported research, and scope changes refuse a locked claim.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentDefinition,
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
)
from astralplane.repositories.history import SessionExecutionObservation
from orchestrator.work_admission import WorkAdmissionCoordinator
from persistent_agents.config import RunnerConfig
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import ActionExecutor
from persistent_agents.research_episode import run_research_episode
from persistent_agents.runner import AssignmentRunner, OneShotLifecycle
from persistent_agents.runtime_values import digest
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import signing_key as signing_key

runtime = plane


@pytest.fixture
def capability(runtime, fixture):
    store = AssignmentStore(plane_runtime=runtime)
    host = SimpleNamespace(
        work_admission=WorkAdmissionCoordinator.from_plane(plane_runtime=runtime),
        tool_permissions=SimpleNamespace(get_tool_scope=Mock(return_value="tools:read")),
        _unbind_machine_turn=lambda _: None,
    )
    service = AssignmentService(host, store, enabled=True, phi_gate=SimpleNamespace())
    runner = AssignmentRunner(
        host, service, config=RunnerConfig(concurrency=2, lease_seconds=30),
        one_shot=OneShotLifecycle(fixture[0], run_research_episode),
    )
    value = SimpleNamespace(runtime=runtime, session_fixture=fixture, store=store,
                            service=service, host=host, runner=runner)
    try:
        yield value
    finally:
        store.close()


def create(capability, *, kind="research", retention="operation", source=None,
           tools=("web-research-1:fetch_page",), scopes=("tools:read",), limits=None):
    op = capability
    sessions, owner, sid, _, _ = op.session_fixture
    state = sessions.capture_execution_reference(owner_id=owner, session_id=sid).state
    hard = datetime.fromtimestamp(state.credential.hard_expires_at, UTC)
    observation = SessionExecutionObservation(
        credential=state.credential, started_at=state.observed_at,
        valid_until=min(state.observed_at + timedelta(seconds=15), hard),
    )
    identity = str(uuid4())
    definition = AssignmentDefinition(
        name="Synthetic research selection", instructions="Read one public page.",
        source=source if source is not None else {
            "profile": "public_page", "agent_id": "web-research-1", "tool_name": "fetch_page",
            "arguments": {"url": "https://example.test/page"}, "linked_document_urls": [],
        },
        allowed_tools=tools, consented_scopes=scopes, offline_grant_id=None,
        limits={"max_retries": 1, "max_concurrent_tasks": 1, "max_depth": 1,
                "max_tasks": 2, "model_calls": 1, "tool_calls": 1,
                "tokens": 129024, "elapsed_ms": 95000, **(limits or {})},
    )
    with op.runtime.transaction() as tx:
        op.runtime.repositories.history.sessions.bound_request_execution_waits(tx)
        return op.runtime.repositories.assignments.create_operation(
            tx, owner_id=owner, assignment_id=identity, origin_namespace="fixture",
            caller_key=identity, command_digest=digest(identity), authority=observation,
            definition=definition, operation=AssignmentOperationSpec(
                kind, AssignmentOperationAuthority(owner, "interactive", "session_incarnation",
                    state.credential.incarnation_id, min(state.observed_at + timedelta(seconds=300), hard)),
                state.observed_at + timedelta(seconds=300), retention,
            ),
        )


async def current(op, record):
    return (await op.store.call("get_operation", owner_id=record.owner_id,
                               assignment_id=record.assignment_id)).assignment


@pytest.mark.asyncio
async def test_unsupported_chat_is_not_claimed_or_refreshed(capability, monkeypatch):
    op = capability
    record = await asyncio.to_thread(create, op, kind="chat", retention="none")
    before = await current(op, record)
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert started == []
    assert await current(op, record) == before
    assert op.session_fixture[-1] == []


@pytest.mark.asyncio
async def test_unsupported_prefix_does_not_starve_supported_research(capability, monkeypatch):
    op = capability
    unsupported = await asyncio.to_thread(create, op, kind="chat", retention="none")
    supported = await asyncio.to_thread(create, op)
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert [claim.assignment.assignment_id for claim in started] == [supported.assignment_id]
    assert await current(op, unsupported) == unsupported
    assert len(op.session_fixture[-1]) == 1
    assert (await current(op, supported)).phase == "checking"


@pytest.mark.asyncio
async def test_nonretained_fixed_research_is_claimed_through_ordinary_authority(capability, monkeypatch):
    op = capability
    record = await asyncio.to_thread(create, op, retention="none")
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert [claim.assignment.assignment_id for claim in started] == [record.assignment_id]
    assert len(op.session_fixture[-1]) == 1
    assert (await current(op, record)).phase == "checking"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"tools": ("web-research-1:fetch_page", "other:read")},
    {"tools": ("other:read",)},
    {"tools": ()},
    {"scopes": ("tools:search",)},
    {"scopes": ("tools:read", "tools:search")},
    {"limits": {"tokens": 129023}},
    {"limits": {"elapsed_ms": 94999}},
    {"limits": {"tool_calls": 0}},
    {"source": {"profile": "registered_reader", "agent_id": "web-research-1",
                "tool_name": "fetch_page", "arguments": {"url": "https://example.test/page"}}},
    {"source": {"profile": "public_page", "agent_id": "web-research-1",
                "tool_name": "fetch_page", "arguments": {"url": "https://example.test/page"},
                "linked_document_urls": ["https://example.test/second"]}},
])
async def test_unsupported_declared_profile_stays_untouched(capability, monkeypatch, changes):
    op = capability
    record = await asyncio.to_thread(create, op, **changes)
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert started == [] and op.session_fixture[-1] == []
    assert await current(op, record) == record


@pytest.mark.parametrize("version", [None, True, 1, 3, "2"])
def test_unrecognized_record_version_is_not_a_fixed_capability(capability, version):
    record = create(capability)
    forged = replace(record, operation={**record.operation, "version": version})
    with pytest.raises(DispatchDenied, match="^assignment_operation_profile_unavailable$"):
        capability.runner._assert_operation_capability(forged)
    assert capability.session_fixture[-1] == []


@pytest.mark.parametrize("limits", [{"tokens": True}, {"tokens": 129024.0},
                                   {"tokens": "129024"}, {"elapsed_ms": None},
                                   {"model_calls": 0}])
def test_non_integer_counter_cannot_claim_profile_support(capability, limits):
    record = create(capability)
    forged = replace(record, definition=replace(record.definition,
        limits={**record.definition.limits, **limits}))
    with pytest.raises(DispatchDenied, match="^assignment_operation_profile_unavailable$"):
        capability.runner._assert_operation_capability(forged)


def test_readiness_is_exact_composition_and_never_auth_io(capability):
    op = capability
    sessions = op.session_fixture[0]
    assert op.runner.fixed_research_ready(service=op.service, sessions=sessions)
    assert not op.runner.fixed_research_ready(service=object(), sessions=sessions)
    assert not op.runner.fixed_research_ready(service=op.service, sessions=object())
    another = AssignmentService(op.host, op.store, enabled=True, phi_gate=SimpleNamespace())
    assert not op.runner.fixed_research_ready(service=another, sessions=sessions)
    original = op.runner.one_shot
    op.runner.one_shot = OneShotLifecycle(sessions, AsyncMock())
    assert not op.runner.fixed_research_ready(service=op.service, sessions=sessions)
    op.runner.one_shot = None
    assert not op.runner.fixed_research_ready(service=op.service, sessions=sessions)
    op.runner.one_shot = original
    op.runner._stopping = True
    assert not op.runner.fixed_research_ready(service=op.service, sessions=sessions)
    assert op.session_fixture[-1] == []


def test_wrong_repository_runtime_refuses_readiness_and_selection(capability, monkeypatch):
    op = capability
    record = create(op)
    monkeypatch.setattr(op.store, "repository", object())
    assert not op.runner.fixed_research_ready(service=op.service, sessions=op.session_fixture[0])
    with pytest.raises(DispatchDenied, match="^assignment_operation_profile_unavailable$"):
        op.runner._assert_operation_capability(record)


def test_programming_failure_never_becomes_supported(capability):
    op = capability
    record = create(op)
    op.host.tool_permissions.get_tool_scope.side_effect = RuntimeError("synthetic policy failure")
    with pytest.raises(RuntimeError, match="synthetic policy failure"):
        op.runner._assert_operation_capability(record)


@pytest.mark.asyncio
async def test_scope_change_during_authority_refresh_refuses_locked_claim(capability, monkeypatch):
    op = capability
    record = await asyncio.to_thread(create, op)
    original = op.runner._operation_authority
    async def change(current_record):
        authority = await original(current_record)
        op.host.tool_permissions.get_tool_scope.return_value = "tools:write"
        return authority
    monkeypatch.setattr(op.runner, "_operation_authority", change)
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert started == [] and len(op.session_fixture[-1]) == 1
    assert await current(op, record) == record


async def claim(op, record):
    authority = await op.runner._operation_authority(record)
    return await op.store.operation_lifecycle_transaction(
        authority=authority, callback=lambda tx, repo, current_record:
        repo.claim_operation_for_administration(tx, owner_id=current_record.owner_id,
            assignment_id=current_record.assignment_id, expected_state_version=current_record.state_version,
            worker_id=op.runner.worker_id, authority=authority.observation, lease_seconds=30),
    )


@pytest.mark.asyncio
async def test_preexisting_unsupported_claim_cannot_dispatch_or_admit(capability, monkeypatch):
    op = capability
    record = await asyncio.to_thread(create, op, kind="chat", retention="none")
    queued = await claim(op, record)
    before = await current(op, record)
    admit = AsyncMock(side_effect=AssertionError("unsupported admission"))
    monkeypatch.setattr(op.runner, "_admit", admit)
    seen = len(op.session_fixture[-1])
    with pytest.raises(DispatchDenied, match="^assignment_operation_profile_unavailable$"):
        await op.runner.run_claim(queued)
    assert await current(op, record) == before
    assert len(op.session_fixture[-1]) == seen
    admit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["authority", "bind", "after_bind"])
async def test_scope_change_before_dispatch_never_calls_source(capability, monkeypatch, stage):
    op = capability
    record = await asyncio.to_thread(create, op)
    queued = await claim(op, record)
    source = AsyncMock(side_effect=AssertionError("source dispatched after capability loss"))
    monkeypatch.setattr(ActionExecutor, "action", source)
    if stage == "authority":
        original = op.runner._operation_authority
        async def changed_authority(current_record):
            authority = await original(current_record)
            op.host.tool_permissions.get_tool_scope.return_value = "tools:write"
            return authority
        monkeypatch.setattr(op.runner, "_operation_authority", changed_authority)
        admit = AsyncMock(side_effect=AssertionError("admitted after capability loss"))
        monkeypatch.setattr(op.runner, "_admit", admit)
    else:
        original = op.store.operation_lifecycle_transaction
        async def changed_bind(**kwargs):
            if stage == "bind":
                op.host.tool_permissions.get_tool_scope.return_value = "tools:write"
            result = await original(**kwargs)
            op.host.tool_permissions.get_tool_scope.return_value = "tools:write"
            return result
        monkeypatch.setattr(op.store, "operation_lifecycle_transaction", changed_bind)
    await op.runner.run_claim(queued)
    source.assert_not_called()
    if stage == "authority":
        admit.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_explicit_handler_keeps_its_existing_claim_contract(capability, monkeypatch):
    op = capability
    record = await asyncio.to_thread(create, op, kind="chat", retention="none")
    handler = AsyncMock()
    op.runner.one_shot = OneShotLifecycle(op.session_fixture[0], handler)
    assert not op.runner.fixed_research_ready(service=op.service, sessions=op.session_fixture[0])
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert [item.assignment.assignment_id for item in started] == [record.assignment_id]
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_full_unsupported_page_advances_to_later_supported_work(capability, monkeypatch):
    op = capability
    earlier = [await asyncio.to_thread(create, op, kind="chat", retention="none") for _ in range(20)]
    later = await asyncio.to_thread(create, op)
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert started == [] and op.session_fixture[-1] == []
    await op.runner._tick_operations()
    assert [item.assignment.assignment_id for item in started] == [later.assignment_id]
    assert all([await current(op, record) == record for record in earlier])


@pytest.mark.asyncio
async def test_source_replacement_after_selection_cannot_use_old_authority(capability, monkeypatch):
    op = capability
    record = await asyncio.to_thread(create, op)
    original = op.runner._operation_authority
    async def replace_source(current_record):
        authority = await original(current_record)
        def replace_stored_source():
            with op.runtime.transaction() as tx:
                tx.execute(
                    "UPDATE persistent_assignment SET data=jsonb_set(data,'{definition,source,profile}',"
                    "'\"registered_reader\"'::jsonb) WHERE id=%s", (record.assignment_id,))
        await asyncio.to_thread(replace_stored_source)
        assert (await current(op, record)).definition.source["profile"] == "registered_reader"
        return authority
    monkeypatch.setattr(op.runner, "_operation_authority", replace_source)
    started = []
    monkeypatch.setattr(op.runner, "_start_claim", started.append)
    await op.runner._tick_operations()
    assert started == []
    final = await current(op, record)
    assert final.phase == "waiting" and final.state_version == record.state_version
    assert final.definition.source["profile"] == "registered_reader"


@pytest.mark.asyncio
async def test_supported_dispatch_reaches_existing_source_boundary_once(capability, monkeypatch):
    op = capability
    record = await asyncio.to_thread(create, op)
    queued = await claim(op, record)
    source = AsyncMock(side_effect=DispatchDenied("assignment_fixture_source_unavailable"))
    monkeypatch.setattr(ActionExecutor, "action", source)
    await op.runner.run_claim(queued)
    from persistent_agents.research_episode import research_action_keys
    source.assert_awaited_once_with(research_action_keys(record)[0], {
        "kind": "tool", "agent_id": "web-research-1", "tool_name": "fetch_page",
        "arguments": {"url": "https://example.test/page"},
    })
    assert "research_result" not in (await current(op, record)).checkpoint
