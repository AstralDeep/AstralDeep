"""Immutable attended approval; no caller arguments or replayed execution."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from persistent_agents.models import (
    ApprovalDecisionRequest,
    AssignmentError,
    CreateAssignmentRequest,
)
from persistent_agents.tests.test_controls import control_payload
from persistent_agents.tests.test_models import create_payload
from persistent_agents.tests.test_service import service as shared_service

service = shared_service


async def proposal(service, state="proposed"):
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    action = SimpleNamespace(action_id=str(uuid4()), state=state, intent=SimpleNamespace(
        request_digest="a" * 64, permission_digest="b" * 64, precondition_digest="c" * 64,
        request={"kind": "tool", "agent_id": "web-research-1", "tool_name": "fetch_page",
                 "arguments": {"url": "https://www.python.org/downloads/"}}))
    original = service.store.call

    async def call(method, **kwargs):
        if method == "get_action":
            return action
        if method == "decide_action":
            assert kwargs["decision"].proposal_digest == action.intent.request_digest
            return SimpleNamespace(action_id=action.action_id, intent=action.intent,
                state="approved" if state == "proposed" and kwargs["decision"].decision == "approve"
                else ("declined" if kwargs["decision"].decision == "decline" else state))
        return await original(method, **kwargs)
    service.store.call = AsyncMock(side_effect=call)
    return record, action


def decision(choice="approve", request_digest="a" * 64):
    return ApprovalDecisionRequest.model_validate({**control_payload(), "request_digest": request_digest,
                                                   "decision": choice})


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction", [None, {"sub": "owner"}, "socket"])
async def test_serialized_or_missing_interaction_cannot_approve(service, interaction):
    record, action = await proposal(service)
    service.approval_executor = AsyncMock()
    with pytest.raises(AssignmentError, match="live_interaction_required"):
        await service.decide("owner", {"sub": "owner"}, record.assignment_id, action.action_id,
                              decision(), interaction=interaction)
    service.approval_executor.assert_not_called()


@pytest.mark.asyncio
async def test_approval_uses_stored_exact_request_and_live_owner(service):
    record, action = await proposal(service)
    socket = object()
    service.orch.ui_sessions[socket] = {"sub": "owner"}
    service.approval_executor = AsyncMock(return_value={"state": "completed"})
    result = await service.decide("owner", {"sub": "owner"}, record.assignment_id, action.action_id,
                                  decision(), interaction=socket)
    assert result == {"state": "completed"}
    assert service.approval_executor.call_args.args[3].intent.request == action.intent.request
    assert service.approval_executor.call_args.args[4] is socket


@pytest.mark.asyncio
async def test_completed_approval_replay_never_dispatches(service):
    record, action = await proposal(service, state="completed")
    socket = object()
    service.orch.ui_sessions[socket] = {"sub": "owner"}
    service.approval_executor = AsyncMock()
    result = await service.decide("owner", {"sub": "owner"}, record.assignment_id, action.action_id,
                                  decision(), interaction=socket)
    assert result.state == "completed"
    service.approval_executor.assert_not_called()


@pytest.mark.asyncio
async def test_decline_works_without_live_execution_or_grant(service):
    record, action = await proposal(service)
    service.orch.offline_grants.is_valid.return_value = False
    result = await service.decide("owner", {"sub": "owner"}, record.assignment_id, action.action_id,
                                  decision("decline"))
    assert result.state == "declined"


@pytest.mark.asyncio
async def test_changed_digest_and_missing_executor_fail_before_decision(service):
    record, action = await proposal(service)
    socket = object()
    service.orch.ui_sessions[socket] = {"sub": "owner"}
    with pytest.raises(AssignmentError, match="proposal_changed"):
        await service.decide("owner", {"sub": "owner"}, record.assignment_id, action.action_id,
                              decision(request_digest="d" * 64), interaction=socket)
    with pytest.raises(AssignmentError, match="executor_unavailable"):
        await service.decide("owner", {"sub": "owner"}, record.assignment_id, action.action_id,
                              decision(), interaction=socket)
    assert all(call.args[0] != "decide_action" for call in service.store.call.call_args_list)


@pytest.mark.asyncio
async def test_action_cannot_widen_reviewed_source(service):
    record, action = await proposal(service)
    action.intent.request["arguments"]["url"] = "https://example.org/other"
    with pytest.raises(AssignmentError, match="source_outside_consent"):
        await service.validate_execution("owner", {"sub": "owner"}, record, action)


@pytest.fixture
def bridge():
    import time

    from persistent_agents.approvals import AssignmentApprovalBridge
    action = SimpleNamespace(action_id=str(uuid4()), assignment_id=str(uuid4()), state="approved",
        instruction_revision=1, control_epoch=1, interactive_proposal_id=None,
        intent=SimpleNamespace(request_digest="a" * 64, permission_digest="b" * 64,
            precondition_digest="c" * 64, request={"kind": "tool", "agent_id": "remote-compute-1",
                "tool_name": "remove_path", "arguments": {"machine_id": "m", "remote_path": "/x"}}))
    record = SimpleNamespace(assignment_id=action.assignment_id, lifecycle="active", instruction_revision=1,
        control_epoch=1, definition=SimpleNamespace(conversation_id="chat-1"))
    row = SimpleNamespace(proposal_id=uuid4().hex, owner_id="owner", status="pending", expires_at=int(time.time())+60,
        summary="Remove /x from the selected machine.", machine_id="m", agent_id="remote-compute-1",
        tool_name="remove_path", conversation_id="chat-1")
    tx = object()
    repository = SimpleNamespace(link_interactive_proposal=Mock(return_value=action),
        observe_interactive_proposal=Mock(return_value=action))
    remote = SimpleNamespace(get=Mock(return_value=row), decide_if_pending=Mock(), expire_if_pending=Mock())
    source = SimpleNamespace(plane_runtime=object(), plane_repositories=SimpleNamespace(
        assignments=repository, remote_operation_proposals=remote))
    orch = SimpleNamespace(plane_repository_source=source, send_ui_render=AsyncMock())
    store = SimpleNamespace(repository=repository, call=AsyncMock(return_value=action), close=Mock())

    async def transaction(callback):
        return callback(tx, repository)
    store.transaction = AsyncMock(side_effect=transaction)
    service = SimpleNamespace(store=store, _interaction=Mock(return_value={"sub": "owner"}),
        validate_execution=AsyncMock(return_value={"permission_digest": "b"*64, "precondition_digest": "c"*64}),
        get=AsyncMock(return_value=record))
    runner = SimpleNamespace(service=service, orch=orch, execute_approved=AsyncMock(return_value=action))
    bridge = AssignmentApprovalBridge(runner)
    service.approval_executor = bridge
    orch.persistent_assignments = service
    return SimpleNamespace(bridge=bridge, runner=runner, service=service, orch=orch, store=store,
                           remote=remote, repository=repository, action=action, record=record, row=row, tx=tx)


@pytest.mark.asyncio
async def test_bridge_links_before_showing_second_review_without_dispatch(bridge, monkeypatch):
    from orchestrator import remote_confirmation as rc
    events = []

    def create(*args, on_created):
        events.append("create")
        on_created(bridge.tx, bridge.row.proposal_id)
        events.append("commit")
        return bridge.row.proposal_id, {}
    monkeypatch.setattr(rc, "_create_proposal", create)
    monkeypatch.setattr(rc, "evaluate", Mock(side_effect=AssertionError("unreserved preflight")))
    result = await bridge.bridge("owner", {"sub": "owner"}, bridge.record, bridge.action, object())
    assert result is bridge.action and events == ["create", "commit"]
    assert bridge.repository.link_interactive_proposal.call_args.args[0] is bridge.tx
    assert bridge.repository.link_interactive_proposal.call_args.kwargs["expected_request_digest"] == "a"*64
    bridge.runner.execute_approved.assert_not_called()
    bridge.orch.send_ui_render.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_link_failure_never_exposes_confirmation(bridge, monkeypatch):
    from orchestrator import remote_confirmation as rc
    bridge.repository.link_interactive_proposal.side_effect = AssignmentError("assignment_approval_stale")
    def create(*args, on_created):
        on_created(bridge.tx, bridge.row.proposal_id)
        raise AssertionError("must fail before commit")
    monkeypatch.setattr(rc, "_create_proposal", create)
    with pytest.raises(AssignmentError, match="approval_stale"):
        await bridge.bridge("owner", {"sub": "owner"}, bridge.record, bridge.action, object())
    bridge.orch.send_ui_render.assert_not_called()
    bridge.runner.execute_approved.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "approved", "declined", "expired", "consumed", "missing"])
async def test_bridge_restart_reloads_durable_link_without_new_proposal(bridge, monkeypatch, state):
    from orchestrator import remote_confirmation as rc
    bridge.action.interactive_proposal_id = bridge.row.proposal_id
    bridge.row.status = state
    if state == "missing":
        bridge.remote.get.return_value = None
    create = Mock(side_effect=AssertionError("duplicate review"))
    monkeypatch.setattr(rc, "_create_proposal", create)
    if state in ("consumed", "missing"):
        with pytest.raises(AssignmentError):
            await bridge.bridge("owner", {"sub": "owner"}, bridge.record, bridge.action, object())
    else:
        await bridge.bridge("owner", {"sub": "owner"}, bridge.record, bridge.action, object())
    create.assert_not_called()
    if state == "approved":
        assert bridge.runner.execute_approved.call_args.kwargs["remote_marker"] == bridge.row.proposal_id
    else:
        bridge.runner.execute_approved.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_linked_remote_decision_changes_exact_proposal_and_continues_only_assignment(bridge, monkeypatch, decision):
    from orchestrator import remote_confirmation as rc
    from persistent_agents.approvals import handle_linked_remote_decision
    monkeypatch.setattr(rc, "_audit_async", AsyncMock())
    def decide(tx, **kwargs):
        assert tx is bridge.tx
        assert kwargs["proposal_id"] == bridge.row.proposal_id
        bridge.row.status = kwargs["decision"]
        return bridge.row
    bridge.remote.decide_if_pending.side_effect = decide
    assert await handle_linked_remote_decision(bridge.orch, object(), "owner", bridge.row, decision)
    if decision == "approve":
        bridge.runner.execute_approved.assert_awaited_once()
    else:
        bridge.runner.execute_approved.assert_not_called()
        bridge.repository.observe_interactive_proposal.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["epoch", "permissions", "unknown_decision", "consumed", "already_decided"])
async def test_linked_approval_failures_cannot_fall_back_to_chat(bridge, failure):
    from persistent_agents.approvals import handle_linked_remote_decision
    decision = "approve"
    if failure == "epoch":
        bridge.record.control_epoch = 2
    elif failure == "permissions":
        bridge.service.validate_execution.return_value["permission_digest"] = "d"*64
    elif failure == "unknown_decision":
        decision = "maybe"
    elif failure == "consumed":
        bridge.row.status = "consumed"
    elif failure == "already_decided":
        bridge.remote.decide_if_pending.return_value = None
    assert await handle_linked_remote_decision(bridge.orch, object(), "owner", bridge.row, decision)
    bridge.runner.execute_approved.assert_not_called()
    bridge.orch.send_ui_render.assert_awaited_once()


@pytest.mark.asyncio
async def test_linked_disabled_service_refuses_and_unlinked_ordinary_review_continues(bridge, monkeypatch):
    from persistent_agents.approvals import handle_linked_remote_decision
    bridge.store.call.return_value = None
    assert not await handle_linked_remote_decision(bridge.orch, object(), "owner", bridge.row, "approve")
    bridge.store.call.return_value = bridge.action
    bridge.orch.persistent_assignments = None
    monkeypatch.setattr("persistent_agents.approvals.AssignmentStore", lambda orch: bridge.store)
    assert await handle_linked_remote_decision(bridge.orch, object(), "owner", bridge.row, "approve")
    bridge.store.close.assert_called_once()
    assert not await handle_linked_remote_decision(SimpleNamespace(), object(), "owner", bridge.row, "approve")
    bridge.orch.plane_repository_source.plane_repositories.assignments = None
    assert not await handle_linked_remote_decision(bridge.orch, object(), "owner", bridge.row, "approve")


def test_persistent_conditional_confirmation_never_stats_remote_target(monkeypatch):
    from orchestrator import remote_confirmation as rc
    monkeypatch.setattr("persistent_agents.dispatch_context.current_dispatch", lambda: object())
    monkeypatch.setattr(rc, "_is_destructive", Mock(side_effect=AssertionError("unreserved stat")))
    monkeypatch.setattr(rc, "_consume_if_valid", lambda *args: True)
    monkeypatch.setattr(rc, "_audit_sync", Mock())
    socket = object()
    orch = SimpleNamespace(ui_sessions={socket: {"sub": "owner"}})
    arguments = {"machine_id": "m", "remote_path": "/x", rc._MARKER: "p"}
    assert rc.evaluate(orch, socket, "remote-compute-1", "upload_file", arguments, "chat", "owner") is None
    assert rc._MARKER not in arguments


def test_real_confirmation_creation_links_in_the_creation_transaction(monkeypatch):
    from orchestrator import remote_confirmation as rc
    from tests.test_remote_confirmation_063 import _FakeDB, _orch
    orch = _orch(_FakeDB())
    policy = SimpleNamespace(summary=lambda *args: "Remove reviewed path.", machine_key="machine_id",
        machine_id=None, card_title="Review operation", card_caption="Approve exact parameters", card_as_result=False)
    monkeypatch.setattr(rc, "policy_for", lambda agent: policy)
    monkeypatch.setattr(rc, "_audit_sync", Mock())
    calls = []
    def link(transaction, proposal_id):
        row = orch.runtime_composition.plane.repositories.remote_operation_proposals.get(
            transaction, owner_id="owner", proposal_id=proposal_id)
        assert row is not None and row.status == "pending"
        assert row.arguments == {"machine_id": "m", "remote_path": "/x"}
        calls.append(proposal_id)
    proposal_id, card = rc._create_proposal(orch, "owner", "chat-1", "remote-compute-1", "remove_path",
        {"machine_id": "m", "remote_path": "/x"}, on_created=link)
    assert calls == [proposal_id] and card["type"] == "card"


@pytest.mark.asyncio
async def test_real_remote_handler_returns_before_ordinary_dispatch_for_a_link(monkeypatch):
    from orchestrator import remote_confirmation as rc
    from tests.test_remote_confirmation_063 import _FakeDB, _orch, _seed
    database = _FakeDB()
    orch = _orch(database)
    _seed(database, "proposal", owner="owner", verb="remove_path",
          args={"machine_id": "m", "remote_path": "/x"}, status="pending")
    linked = AsyncMock(return_value=True)
    monkeypatch.setattr("persistent_agents.approvals.handle_linked_remote_decision", linked)
    await rc.handle_decision(orch, object(), "owner", {"proposal_id": "proposal", "decision": "approve"})
    linked.assert_awaited_once()
    assert not orch.execute_single_tool.calls
    assert database.rows["proposal"]["status"] == "pending"
