"""Real guarded Plane transactions; only external IAM and tool replies are fixtures.

One-shot tests construct an existing durable read action directly through Plane.
They do not enable a one-shot runner, source retention, or framework authority.
"""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentActionIntent,
    AssignmentControl,
    AssignmentOperationAuthority,
    AssignmentOperationSpec,
    AssignmentResourceAmount,
)
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import ActionExecutor
from persistent_agents.models import AssignmentError
from persistent_agents.runner import _episode_lease
from persistent_agents.runtime_values import digest, thaw
from persistent_agents.tests.test_engine_postgres import current
from persistent_agents.tests.test_engine_postgres import engine as engine
from persistent_agents.tests.test_engine_postgres import plane as plane


async def bound_executor(engine, *, one_shot=False):
    host, runner, store, identity = engine
    if one_shot:
        original = await current(store, identity)
        identity = str(uuid4())
        now = datetime.now(UTC)
        limits = {k: v for k, v in original.definition.limits.items()
                  if not k.startswith("daily_") and k not in {"cadence_seconds", "step_timeout_ms"}}
        await store.call("create_operation", owner_id="owner", assignment_id=identity,
            origin_namespace="test", caller_key="bounded-read", command_digest=digest("bounded-read"),
            definition=replace(original.definition, limits=limits),
            operation=AssignmentOperationSpec("chat", AssignmentOperationAuthority(
                "owner", "scheduled", "offline_grant", original.definition.offline_grant_id,
                now + timedelta(minutes=10)), now + timedelta(minutes=5), "operation"))
    method = "claim_operations_for_administration" if one_shot else "claim_due_for_administration"
    claim = (await store.call(method, worker_id=runner.worker_id, lease_seconds=30))[0]
    assert claim.assignment.assignment_id == identity
    operation_fence = await runner._admit(claim)
    executor = ActionExecutor(runner, claim, operation_fence, object())
    await store.call("bind_operation", fence=claim.fence, binding=executor.binding)
    return executor


async def ready_action(executor):
    request = {"kind": "tool", "agent_id": "web-research-1", "tool_name": "fetch_page",
               "arguments": {"url": "https://example.org/releases"}}
    return await executor.store.call("put_action", fence=executor.claim.fence,
        intent=AssignmentActionIntent("read", request, digest(request),
            AssignmentResourceAmount(tool_calls=1, elapsed_ms=30_000),
            digest("permission"), digest("precondition"), boundary="read_only"))


async def change_control(executor, command):
    record = await current(executor.store, executor.record.assignment_id)
    values = {"expected_state_version": record.state_version} if record.execution_profile == "one_shot" else {}
    return await executor.store.call("apply_control", owner_id="owner", assignment_id=record.assignment_id,
        expected_instruction_revision=record.instruction_revision, expected_control_epoch=record.control_epoch,
        submission_id=str(uuid4()), submission_digest=digest(command), control=AssignmentControl(command), **values)


def expire_claim(runtime, identity):
    # Simulate a crashed worker's elapsed lease in this test's isolated schema.
    with runtime.transaction() as tx:
        tx.execute("WITH expired AS (SELECT clock_timestamp()-interval '1 second' AS at) "
            "UPDATE persistent_assignment SET lease_expires_at=expired.at, "
            "data=jsonb_set(data,'{lease_expires_at}',to_jsonb(expired.at)) "
            "FROM expired WHERE id=%s", (identity,))


def revoke_grant(runtime, grant_id):
    with runtime.transaction() as tx:
        runtime.repositories.offline_grants.revoke_grant(tx, owner_id="owner", grant_id=grant_id,
            revoked_at=int(datetime.now(UTC).timestamp() * 1000))


def test_current_one_shot_result_uses_both_fences_and_is_not_redispatched(engine):
    host, _, store, _ = engine

    async def scenario():
        executor = await bound_executor(engine, one_shot=True)
        action = await ready_action(executor)
        before = await current(store, action.assignment_id)
        call = store.call
        store.call = AsyncMock(wraps=call)
        result = await executor.execute(action)
        assert "Release version 2" in result["text"]
        settlement = next(item for item in store.call.call_args_list if item.args[0] == "record_action_outcome")
        assert settlement.kwargs["result_fence"] == executor.claim.fence
        assert settlement.kwargs["result_binding"] == executor.binding
        stored = await store.call("get_action", owner_id="owner", assignment_id=action.assignment_id,
                                  action_id=action.action_id)
        assert stored.state == "succeeded" and stored.result.get("result_available", True) is True
        assert await executor.execute(stored) == result
        assert host.physical_tools == 1
        record = await current(store, action.assignment_id)
        assert record.usage["spent"]["tool_calls"] == 1
        assert record.checkpoint == before.checkpoint and record.tasks == before.tasks

    asyncio.run(scenario())


def test_guarded_finish_rolls_back_checkpoint_when_admission_commit_fails(engine):
    host, runner, store, identity = engine

    async def scenario():
        executor = await bound_executor(engine)
        before = await current(store, identity)
        runner._notify_activity = AsyncMock()
        host.work_admission.terminalize = Mock(side_effect=RuntimeError("synthetic commit failure"))
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            await runner._finish(executor, before, checkpoint={"last_finding": "must roll back"})
        assert await current(store, identity) == before
        assert not _episode_lease(executor).terminal
        runner._notify_activity.assert_not_awaited()
        # The unchanged admission is still selected after the transaction rolled back.
        await asyncio.to_thread(host.work_admission.assert_current_execution, executor.operation_fence)

    asyncio.run(scenario())


@pytest.mark.parametrize("loss", ["stop", "admission", "retirement"])
def test_change_after_remote_refresh_refuses_checkpoint_and_terminal_ack(engine, loss):
    host, runner, store, identity = engine

    async def scenario():
        executor = await bound_executor(engine)
        before = await current(store, identity)
        runner._notify_activity = AsyncMock()
        host.work_admission.terminalize = Mock(wraps=host.work_admission.terminalize)

        async def refresh_then_change():
            if loss == "stop":
                await change_control(executor, "stop")
            elif loss == "admission":
                await asyncio.to_thread(host.work_admission.reselect_execution, executor.operation_fence)
            else:
                await store.call("retire_operations_for_owner", owner_id="owner")

        executor.refresh = AsyncMock(side_effect=refresh_then_change)
        with pytest.raises(AssignmentError):
            await runner._finish(executor, before, checkpoint={"last_finding": "must not publish"})
        executor.refresh.assert_awaited_once()
        assert not _episode_lease(executor).terminal
        runner._notify_activity.assert_not_awaited()
        host.work_admission.terminalize.assert_not_called()
        assert host.physical_tools == 0 and host.physical_models == []
        if loss != "retirement":
            assert (await current(store, identity)).checkpoint == before.checkpoint

    asyncio.run(scenario())


def test_one_shot_failed_physical_read_still_charges_and_never_returns_success(engine):
    host, _, store, _ = engine

    async def scenario():
        executor = await bound_executor(engine, one_shot=True)
        action = await ready_action(executor)
        host.tool_after_send = AsyncMock(side_effect=ConnectionError("synthetic external failure"))
        with pytest.raises(ConnectionError):
            await executor.execute(action)
        stored = await store.call("get_action", owner_id="owner", assignment_id=action.assignment_id,
                                  action_id=action.action_id)
        assert stored.state == "failed" and stored.result["outcome"] == "failed"
        assert host.physical_tools == 1
        record = await current(store, action.assignment_id)
        assert record.usage["spent"]["tool_calls"] == 1
        assert all(amount == 0 for amount in record.usage["outstanding"].values())

    asyncio.run(scenario())


@pytest.mark.parametrize("loss", ["pause", "stop", "admission", "remote", "permission", "precondition", "grant"])
def test_authentic_old_permit_settles_once_but_cannot_return_content(engine, plane, loss):
    host, runner, store, _ = engine

    async def scenario():
        executor = await bound_executor(engine, one_shot=True)
        action = await ready_action(executor)
        before = await current(store, action.assignment_id)

        async def lose_authority():
            if loss in {"pause", "stop"}:
                await change_control(executor, loss)
            elif loss == "admission":
                await asyncio.to_thread(host.work_admission.reselect_execution, executor.operation_fence)
            elif loss == "remote":
                runner.service.validate_execution.side_effect = DispatchDenied("assignment_authorization_required")
            elif loss in {"permission", "precondition"}:
                runner.service.validate_execution.return_value = {
                    "permission_digest": digest("new" if loss == "permission" else "permission"),
                    "precondition_digest": digest("new" if loss == "precondition" else "precondition")}
            else:
                await asyncio.to_thread(revoke_grant, plane, before.definition.offline_grant_id)

        host.tool_after_send = lose_authority
        with pytest.raises(DispatchDenied, match="assignment_result_unavailable"):
            await executor.execute(action)
        stored = await store.call("get_action", owner_id="owner", assignment_id=action.assignment_id,
                                  action_id=action.action_id)
        assert stored.state == "succeeded"
        assert stored.result["result_available"] is False and stored.result["result"] == {}
        assert "Release version 2" not in str(thaw(stored))
        assert len(stored.attempts) == 1
        with pytest.raises(DispatchDenied, match="assignment_result_requires_reconciliation"):
            await executor.execute(stored)
        assert host.physical_tools == 1
        record = await current(store, action.assignment_id)
        assert record.usage["spent"]["tool_calls"] == 1
        assert all(amount == 0 for amount in record.usage["outstanding"].values())
        assert record.checkpoint == before.checkpoint and record.tasks == before.tasks
        assert record.wake_generation == before.wake_generation

    asyncio.run(scenario())


def test_old_plane_guard_cannot_issue_new_permit_and_releases_unbegun_reservation(engine, monkeypatch):
    host, _, store, _ = engine

    async def scenario():
        executor = await bound_executor(engine)
        action = await ready_action(executor)
        monkeypatch.setattr(store.repository, "assert_current_assignment_execution", None)
        with pytest.raises(AssignmentError, match="assignment_repository_contract_unavailable"):
            await executor.execute(action)
        stored = await store.call("get_action", owner_id="owner", assignment_id=action.assignment_id,
                                  action_id=action.action_id)
        assert not stored.ever_started and stored.state == "failed_not_started"
        assert host.physical_tools == 0
        record = await current(store, action.assignment_id)
        assert all(amount == 0 for amount in record.usage["outstanding"].values())

    asyncio.run(scenario())


def test_revoked_grant_hold_refuses_and_expired_claim_recovers_without_false_completion(engine, plane):
    host, runner, store, identity = engine

    async def scenario():
        executor = await bound_executor(engine)
        before = await current(store, identity)
        executor.refresh = AsyncMock(side_effect=DispatchDenied("assignment_authorization_required"))
        runner._notify_activity = AsyncMock()
        host.work_admission.terminalize = Mock(wraps=host.work_admission.terminalize)
        await asyncio.to_thread(revoke_grant, plane, before.definition.offline_grant_id)
        with pytest.raises(AssignmentError, match="assignment_authorization_unavailable"):
            await runner._hold(executor, "assignment_authorization_required")
        executor.refresh.assert_not_awaited()
        assert await current(store, identity) == before
        assert not _episode_lease(executor).terminal
        runner._notify_activity.assert_not_awaited()
        host.work_admission.terminalize.assert_not_called()
        await asyncio.to_thread(expire_claim, plane, identity)
        recovery = await store.call("recover_expired_for_administration")
        recovered = await current(store, identity)
        assert identity in recovery.reclaimed_assignment_ids
        assert recovered.lifecycle == "active" and recovered.phase == "failed"
        assert recovered.safe_error_code == "assignment_interrupted"
        assert recovered.next_wake_at is not None
        assert recovered.checkpoint == before.checkpoint and recovered.tasks == before.tasks
        assert host.physical_tools == 0 and host.physical_models == []
        assert not _episode_lease(executor).terminal

    asyncio.run(scenario())
