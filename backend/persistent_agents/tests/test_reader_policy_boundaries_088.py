"""Tests for persistent_agents dispatch readers: policy is re-read at every content or
effect boundary -- revocation after async checks denies the permit or content, and
typed refusal survives a real transaction while releasing its reservation.
"""

import asyncio

import pytest

from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError
from orchestrator.tool_permissions import FixedReaderPolicyError
from persistent_agents.tests.test_operation_reader_postgres_088 import (
    REQUEST,
    actions,
    current,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    plane as plane,
    signing_key as signing_key,
)

runtime = plane
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("boundary", ["commit", "cached", "retain"])
async def test_reader_revoke_after_async_checks_denies_permit_or_content(
    operation, monkeypatch, boundary
):
    op = operation
    before = await current(op)
    cached_result = None
    if boundary == "cached":
        await op.executor.action("policy-page", REQUEST)
        [prior_action] = await actions(op)
        cached_result = prior_action.result
    transaction = op.executor._reader_policy_transaction
    revoked = []

    async def revoke(authority, action_id, callback):
        if callback.__name__ == boundary:
            await asyncio.to_thread(
                op.executor.orch.tool_permissions.set_agent_scopes,
                op.owner,
                "web-research-1",
                {"tools:read": False},
            )
            revoked.append(action_id)
        return await transaction(authority, action_id, callback)

    monkeypatch.setattr(op.executor, "_reader_policy_transaction", revoke)
    expected = DispatchDenied if boundary == "retain" else AssignmentError
    with pytest.raises(expected) as caught:
        await op.executor.action("policy-page", REQUEST)
    if boundary != "retain":
        assert caught.value.code == "assignment_scope_revoked"
        assert caught.value.status_code == 403
    else:
        assert str(caught.value) == "assignment_result_unavailable"
    assert len(revoked) == 1
    [action] = await actions(op)
    record = await current(op)
    if boundary == "commit":
        assert op.physical == []
        assert not action.ever_started and action.state == "failed_not_started"
        assert record.usage["spent"].get("tool_calls", 0) == 0
        assert record.usage["outstanding"].get("tool_calls", 0) == 0
    else:
        assert len(op.physical) == 1
        assert record.usage["spent"]["tool_calls"] == 1
        if boundary == "cached":
            assert action.result == cached_result
            assert "Public release 088" in action.result["result"]["text"]
        else:
            assert action.result["result_available"] is False
            assert action.result["result"] == {}
    assert record.checkpoint == before.checkpoint and record.tasks == before.tasks


@pytest.mark.parametrize("code,status", [
    ("assignment_scope_revoked", 403), ("assignment_source_permission_unavailable", 503),
])
async def test_typed_policy_refusal_survives_real_transaction_and_releases_reservation(
    operation, monkeypatch, code, status,
):
    def refuse(*args, **kwargs):
        raise FixedReaderPolicyError(code)
    monkeypatch.setattr(operation.executor.orch.tool_permissions, "assert_fixed_reader_current", refuse)
    with pytest.raises(AssignmentError) as caught:
        await operation.executor.action("closed-policy", REQUEST)
    assert (caught.value.code, caught.value.status_code) == (code, status)
    [action] = await actions(operation)
    assert not action.ever_started and action.state == "failed_not_started"
    assert operation.physical == []
    record = await current(operation)
    assert record.usage["spent"].get("tool_calls", 0) == 0
    assert record.usage["outstanding"].get("tool_calls", 0) == 0
