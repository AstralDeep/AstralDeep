"""Fixed research result incorporation through the actual one-shot lifecycle.

External IAM and source/provider responses are synthetic. The real PostgreSQL
ledger, encrypted configuration, dispatcher and both execution fences run.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentActivityRecord,
    AssignmentEpisodeCompletion,
)

from llm_config.tests.test_research_profile_088 import reply
from orchestrator.session_authority import SessionAuthorityUnavailable
from orchestrator.work_admission import OperationOwner, OperationState, OwnerScope
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError
from persistent_agents.runner import (
    AssignmentRunner,
    OneShotEpisodeResult,
    OneShotLifecycle,
    _episode_lease,
)
from persistent_agents.runtime_values import digest, thaw
from persistent_agents.tests.test_research_execution_postgres_088 import (
    actions,
    change_config,
    control,
    current,
    fixture as fixture,
    gate_orchestrator as gate_orchestrator,
    operation as operation,
    plane as plane,
    research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True),
]


def attach_runner(op, episode):
    runner = AssignmentRunner(
        op.executor.orch,
        op.executor.service,
        one_shot=OneShotLifecycle(op.sessions, episode),
    )
    op.executor.runner = runner
    op.executor.operation_authority_lock = _episode_lease(op.executor).lock
    return runner


async def admission(op):
    return await asyncio.to_thread(
        op.executor.orch.work_admission.query_operation,
        owner=OperationOwner(OwnerScope.USER, op.owner, None),
        operation_id=op.executor.operation_fence.operation_id,
    )


async def outcome(op):
    from persistent_agents.research_episode import run_research_episode

    runner = attach_runner(op, run_research_episode)
    return runner, await run_research_episode(op.executor)


async def test_fixed_episode_commits_exact_attributed_result_and_retires_leases(
    research,
):
    op = research
    before = await current(op)
    runner, result = await outcome(op)
    # A handler result alone cannot make a result visible or retire its capacity.
    assert (await current(op)).checkpoint == before.checkpoint
    assert (await admission(op)).state == OperationState.RUNNING
    final = await runner._finish_operation(op.executor, result)
    page = thaw(final.checkpoint)["research_result"]
    assert page["disposition"] == "evidence"
    assert page["scope"] == "one_page_excerpts"
    assert page["passages"] == [{"id": "p001", "text": "Public release 088"}]
    ledger = await actions(op)
    model = next(
        action for action in ledger if action.intent.request["kind"] == "model"
    )
    source = next(
        action for action in ledger if action.action_id == page["source"]["action_id"]
    )
    assert final.operation["result_reference"] == model.action_id
    assert page["source"]["result_digest"] == source.result["result_digest"]
    assert final.lifecycle == "completed" and final.next_wake_at is None
    assert (await admission(op)).state == OperationState.COMPLETED
    assert _episode_lease(op.executor).terminal
    assert len(op.model_calls) == 1
    assert final.usage["spent"]["tokens"] == 120
    with pytest.raises((AssignmentError, DispatchDenied, SessionAuthorityUnavailable)):
        await runner._finish_operation(op.executor, result)
    assert len(op.model_calls) == 1


async def test_empty_selection_finishes_with_explicit_insufficient_evidence(research):
    op = research
    op.model_response = reply([])
    runner, result = await outcome(op)
    final = await runner._finish_operation(op.executor, result)
    page = thaw(final.checkpoint)["research_result"]
    assert page["disposition"] == "insufficient_evidence" and page["passages"] == []
    assert final.lifecycle == "completed" and final.usage["spent"]["tokens"] == 120


@pytest.mark.parametrize("change", ["config", "key", "pause", "stop", "permission"])
async def test_change_after_model_settlement_cannot_incorporate_result(
    research, monkeypatch, change
):
    op = research
    runner, result = await outcome(op)
    if change == "config":
        await change_config(op)
    elif change == "key":
        monkeypatch.setenv(
            "AUDIT_HMAC_SECRET", "synthetic-replaced-binding-" + "y" * 40
        )
    elif change in {"pause", "stop"}:
        await control(op, change)
    else:
        await asyncio.to_thread(
            op.executor.orch.tool_permissions.set_agent_scopes,
            op.owner,
            "web-research-1",
            {"tools:read": False},
        )
    with pytest.raises(
        (AssignmentError, DispatchDenied, PermissionError, SessionAuthorityUnavailable)
    ):
        await runner._finish_operation(op.executor, result)
    final = await current(op)
    assert "research_result" not in final.checkpoint
    assert final.operation.get("result_reference") is None
    assert final.usage["spent"]["tokens"] == 120
    assert len(op.model_calls) == 1


@pytest.mark.parametrize("change", ["prose", "reference", "digest", "proof"])
async def test_unbound_handler_output_cannot_become_a_result(research, change):
    op = research
    runner, result = await outcome(op)
    if change == "prose":
        checkpoint = thaw(result.completion.checkpoint)
        checkpoint["research_result"]["passages"][0]["text"] = "Unsupported conclusion"
        result = replace(
            result, completion=replace(result.completion, checkpoint=checkpoint)
        )
    elif change == "reference":
        result = replace(
            result,
            completion=replace(
                result.completion, result_reference=op.source_action.action_id
            ),
        )
    elif change == "digest":
        result = replace(
            result, completion=replace(result.completion, completion_digest="0" * 64)
        )
    else:
        result = replace(result, research=None)
    with pytest.raises((AssignmentError, DispatchDenied)):
        await runner._finish_operation(op.executor, result)
    assert "research_result" not in (await current(op)).checkpoint
    assert (await admission(op)).state == OperationState.RUNNING


@pytest.mark.parametrize(
    "change",
    [
        "empty_success",
        "implicit_success",
        "report_success",
        "report_yield",
        "source_yield",
        "activity",
    ],
)
async def test_research_cannot_bypass_proof_with_another_output_shape(research, change):
    op = research
    from persistent_agents.research_episode import run_research_episode

    runner = attach_runner(op, run_research_episode)
    before = await current(op)
    values = dict(
        expected_state_version=before.state_version,
        checkpoint=before.checkpoint,
        completion_digest=digest(["unbound-output", change]),
        phase="waiting",
        wake_reason="explicit_yield",
        next_wake_at=datetime.now(UTC) + timedelta(seconds=20),
    )
    if change.endswith("success"):
        values.update(completed=True, terminal_outcome="completed", next_wake_at=None)
    if change == "implicit_success":
        values["terminal_outcome"] = None
    elif change.startswith("report"):
        values["checkpoint"] = {"report": "Unsupported conclusion"}
    elif change == "source_yield":
        values["checkpoint"] = {"source_action_id": op.source_action.action_id}
    elif change == "activity":
        values["activity"] = AssignmentActivityRecord(
            "unbound-result", "result", "Research result", "Unsupported conclusion"
        )
    with pytest.raises(DispatchDenied, match="assignment_research_result_invalid"):
        await runner._finish_operation(
            op.executor,
            OneShotEpisodeResult(before, AssignmentEpisodeCompletion(**values)),
        )
    assert await current(op) == before
    assert (await admission(op)).state == OperationState.RUNNING
    assert op.model_calls == []


@pytest.mark.parametrize("terminal_failure", [False, True])
async def test_research_without_output_can_yield_or_record_failure(
    research, terminal_failure
):
    op = research
    from persistent_agents.research_episode import run_research_episode

    runner = attach_runner(op, run_research_episode)
    before = await current(op)
    completion = AssignmentEpisodeCompletion(
        expected_state_version=before.state_version,
        checkpoint=before.checkpoint,
        completion_digest=digest(["no-output", terminal_failure]),
        phase="waiting",
        wake_reason="explicit_yield",
        next_wake_at=None
        if terminal_failure
        else datetime.now(UTC) + timedelta(seconds=20),
        completed=terminal_failure,
        terminal_outcome="failed" if terminal_failure else None,
    )
    result = await runner._finish_operation(
        op.executor, OneShotEpisodeResult(before, completion)
    )
    assert result.checkpoint == before.checkpoint
    assert result.operation.get("result_reference") is None
    assert result.operation.get("terminal_outcome") == (
        "failed" if terminal_failure else None
    )
    assert (await admission(op)).state == OperationState.COMPLETED
    assert op.model_calls == []
