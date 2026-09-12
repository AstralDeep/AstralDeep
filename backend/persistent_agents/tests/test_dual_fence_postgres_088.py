"""Real guarded Plane transactions; only external IAM and tool replies are fixtures.

One-shot fixtures use the real stored interactive incarnation, normal JWT
resolver and ordinary delegated dispatch. They activate no runner or ingress.
"""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentActionIntent,
    AssignmentControl,
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
from persistent_agents.tests.test_operation_reader_postgres_088 import (
    actions, control, prepare,
    current as operation_current,
    operation as operation,
    runtime as runtime,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    signing_key as signing_key,
)
from orchestrator import session_authority
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record


async def bound_executor(engine):
    host, runner, store, identity = engine
    claim = (await store.call("claim_due_for_administration",
                             worker_id=runner.worker_id, lease_seconds=30))[0]
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


@pytest.mark.asyncio
async def test_current_one_shot_result_uses_both_fences_and_is_not_redispatched(operation, monkeypatch):
    op = operation
    executor, store = op.executor, op.executor.store
    action = await prepare(op)
    before = await operation_current(op)
    call = AsyncMock(wraps=store.call_for_operation)
    monkeypatch.setattr(store, "call_for_operation", call)
    result = await executor.execute(action)
    assert "Public release 088" in result["text"]
    settlement = next(item for item in call.call_args_list if item.args[0] == "record_action_outcome")
    assert settlement.kwargs["result_fence"] == executor.claim.fence
    assert settlement.kwargs["result_binding"] == executor.binding
    assert settlement.kwargs["result_authority"].credential.incarnation_id == (
        executor.record.operation["authority"]["reference_id"])
    [stored] = await actions(op)
    assert stored.state == "succeeded" and stored.result.get("result_available", True) is True
    assert await executor.execute(stored) == result
    assert len(op.physical) == 1
    record = await operation_current(op)
    assert record.usage["spent"]["tool_calls"] == 1
    assert record.checkpoint == before.checkpoint and record.tasks == before.tasks


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


@pytest.mark.asyncio
async def test_one_shot_failed_physical_read_still_charges_and_never_returns_success(operation):
    op = operation
    action = await prepare(op)
    op.hooks.after = AsyncMock(side_effect=ConnectionError("synthetic external failure"))
    with pytest.raises((ConnectionError, DispatchDenied)):
        await op.executor.execute(action)
    [stored] = await actions(op)
    assert stored.state == "failed" and stored.result["outcome"] == "failed"
    assert len(op.physical) == 1
    record = await operation_current(op)
    assert record.usage["spent"]["tool_calls"] == 1
    assert all(amount == 0 for amount in record.usage["outstanding"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["pause", "stop", "admission", "remote", "permission", "precondition", "session"])
async def test_authentic_old_permit_settles_once_but_cannot_return_content(operation, monkeypatch, loss):
    op = operation
    action = await prepare(op)
    before = await operation_current(op)

    async def lose_authority():
        if loss in {"pause", "stop"}:
            await control(op, loss)
        elif loss == "admission":
            await asyncio.to_thread(op.executor.orch.work_admission.reselect_execution,
                                    op.executor.operation_fence)
        elif loss == "remote":
            # External refresh failure; the actual local/JWT resolver remains.
            monkeypatch.setattr(session_authority.web_auth, "_exchange_session_refresh",
                                AsyncMock(side_effect=ConnectionError("synthetic IAM unavailable")))
        elif loss == "permission":
            await asyncio.to_thread(op.executor.orch.tool_permissions.set_agent_scopes,
                                    op.owner, "web-research-1", {"tools:read": False})
        elif loss == "precondition":
            validate = op.executor.service.validate_execution
            async def changed(*args, **kwargs):
                result = await validate(*args, **kwargs)
                return {**result, "precondition_digest": digest("synthetic changed precondition")}
            monkeypatch.setattr(op.executor.service, "validate_execution", changed)
        else:
            old = await asyncio.to_thread(get_session_record, op.runtime, op.sid)
            await asyncio.to_thread(replace_session_record, op.runtime, old)

    op.hooks.after = lose_authority
    with pytest.raises(DispatchDenied, match="assignment_result_unavailable"):
        await op.executor.execute(action)
    [stored] = await actions(op)
    assert stored.state == "succeeded"
    assert stored.result["result_available"] is False and stored.result["result"] == {}
    assert "Public release 088" not in str(thaw(stored))
    assert len(stored.attempts) == 1
    with pytest.raises((DispatchDenied, AssignmentError, session_authority.SessionAuthorityUnavailable)):
        await op.executor.execute(stored)
    assert len(op.physical) == 1
    record = await operation_current(op)
    assert record.usage["spent"]["tool_calls"] == 1
    assert all(amount == 0 for amount in record.usage["outstanding"].values())
    assert record.checkpoint == before.checkpoint and record.tasks == before.tasks
    assert record.wake_generation == before.wake_generation


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
