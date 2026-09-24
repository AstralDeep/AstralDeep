"""Tests for persistent_agents/execution.py and runner.py: unknown retained model
consumption cannot be bypassed by pause and resume, or by claiming under a distinct
read key.
"""

import asyncio

import pytest
from astralplane.repositories.assignment_models import AssignmentActionReconciliation
from uuid import uuid4

from llm_config import research_profile as profile
from orchestrator.session_authority import SessionAuthorityUnavailable
from orchestrator.work_admission import OperationState
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError
from persistent_agents.research_episode import run_research_episode, source_request
from persistent_agents.runtime_values import digest
from persistent_agents.tests.test_research_continuation_postgres_088 import (
    actions, attach_runner, control, current, fresh_executor,
    fixture as fixture, gate_orchestrator as gate_orchestrator,
    operation as operation, plane as plane, research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000, "model_calls": 2,
                                                     "elapsed_ms": 300_000}], indirect=True)]


async def test_stale_unknown_model_cannot_issue_new_effect_after_pause_resume(research, monkeypatch):
    from shared import isolated_http

    op = research
    runner = attach_runner(op, run_research_episode)
    original = isolated_http.request
    op.model_status = 503

    async def stale_result(*args, **kwargs):
        response = await original(*args, **kwargs)
        await op.executor.orch._llm_store.set(op.owner, provider="openai",
            base_url=profile.BASE_URL, model=profile.MODEL, api_key="synthetic-replacement-key")
        return response

    monkeypatch.setattr(isolated_http, "request", stale_result)
    with pytest.raises(DispatchDenied, match="assignment_action_uncertain"):
        await run_research_episode(op.executor)
    before = await current(op)
    unknown = next(a for a in await actions(op) if a.intent.request["kind"] == "model")
    assert unknown.state == "uncertain" and before.usage["outstanding"]["model_calls"] == 1
    calls = (len(op.physical), len(op.model_calls))
    await control(op, "pause")
    await asyncio.to_thread(op.executor.orch.work_admission.terminalize,
        op.executor.operation_fence, state=OperationState.CANCELLED,
        terminal_code="synthetic_owner_pause", safe_summary=None, retry_after_ms=None)
    await control(op, "resume")
    monkeypatch.setattr(isolated_http, "request", original)
    op.model_status = 200
    refused = False
    try:
        executor = await fresh_executor(op, runner)
        await run_research_episode(executor)
    except (AssignmentError, DispatchDenied, SessionAuthorityUnavailable):
        refused = True
    assert (len(op.physical), len(op.model_calls)) == calls
    assert refused
    after = await current(op)
    assert after.phase == "reconciliation" and after.next_wake_at is None
    assert after.usage == before.usage
    held = next(a for a in await actions(op) if a.action_id == unknown.action_id)
    assert held == unknown
    authority = await runner._operation_authority(after)
    decision = AssignmentActionReconciliation(held.result["result_digest"],
        "confirmed_applied", "verified:synthetic-provider-receipt", str(uuid4()), digest("settle-held-model"))
    settled = await runner.store.operation_lifecycle_transaction(authority=authority,
        callback=lambda tx, repository, value: repository.reconcile_action(tx,
            owner_id=value.owner_id, assignment_id=value.assignment_id, action_id=held.action_id,
            expected_instruction_revision=value.instruction_revision,
            expected_control_epoch=value.control_epoch, expected_state_version=value.state_version,
            decision=decision, authority=authority.observation))
    assert settled.state == "succeeded" and settled.result["result"] == {}
    charged = await current(op)
    assert charged.usage["outstanding"]["model_calls"] == 0
    assert charged.usage["spent"]["model_calls"] == 1
    executor = await fresh_executor(op, runner)
    result = await run_research_episode(executor)
    final = await runner._finish_operation(executor, result)
    assert final.lifecycle == "completed"
    assert len(op.physical) == calls[0] + 1 and len(op.model_calls) == calls[1] + 1
    assert final.usage["spent"]["model_calls"] == 2


async def test_current_claim_cannot_bypass_unknown_model_with_a_distinct_read_key(research, monkeypatch):
    from shared import isolated_http

    op = research
    attach_runner(op, run_research_episode)
    original = isolated_http.request
    op.model_status = 503

    async def stale_result(*args, **kwargs):
        response = await original(*args, **kwargs)
        await op.executor.orch._llm_store.set(op.owner, provider="openai",
            base_url=profile.BASE_URL, model=profile.MODEL, api_key="synthetic-replacement-key")
        return response

    monkeypatch.setattr(isolated_http, "request", stale_result)
    with pytest.raises(DispatchDenied, match="assignment_action_uncertain"):
        await run_research_episode(op.executor)
    before = await current(op)
    calls = (len(op.physical), len(op.model_calls))
    refused = False
    try:
        await op.executor.action("distinct-source-under-current-claim", source_request(before))
    except (AssignmentError, DispatchDenied, SessionAuthorityUnavailable):
        refused = True
    assert (len(op.physical), len(op.model_calls)) == calls
    assert refused
    assert (await current(op)).usage == before.usage
