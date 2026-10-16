"""Tests durable subscription receipt replay after source completion and owner deletion.
Real PostgreSQL restart cases preserve current credentials and prevent duplicate effects.
"""

from uuid import uuid4
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from orchestrator.work_control_authority import authenticate_work_control_request
from orchestrator.work_controls import WorkControlService, WorkDeleteRequest
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError
from persistent_agents.runner import OneShotEpisodeResult
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from tests.helpers.plane_template import plane_template as plane_template
from tests.test_request_session_authority_088 import request
from tests.test_work_subscriptions_postgres_088 import (
    _audits, _claim_source, _issue, _operation, _read, _runner, _subscribe, _terminal_completion,
    _env as _env, audit as audit, credentials as credentials, fixture as fixture,
    plane as plane, signing_key as signing_key, work as work,
)

runtime = plane


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "completed"])
@pytest.mark.parametrize("retry", ["same_sid_after_restart", "fresh_sid", "revoked_same_sid", "different_source_same_sid", "same_sid_after_purge", "fresh_sid_after_purge"])
async def test_committed_subscribe_receipt_survives_source_terminal_transition(work, fixture, terminal, retry):
    caller = _issue(work)
    source, target, other_source = _operation(work), _operation(work), _operation(work)
    submission = str(uuid4())
    result = await _subscribe(work, caller, source, target, submission_id=submission)
    assert result['applied'] is True
    if terminal == 'cancelled':
        await work.ops.cancel(caller, operation_id=source.assignment_id,
            submission_id=str(uuid4()), expected_revision=source.state_version)
    else:
        runner = await _runner(work)
        try:
            executor = await _claim_source(work, runner, source)
            before = (await _read(work, source)).assignment
            await runner._finish_operation(executor, OneShotEpisodeResult(before, _terminal_completion(before, 'independent-receipt-retry')))
        finally:
            await runner.stop()
    before = (await _read(work, target)).assignment
    terminal_source = (await _read(work, source)).assignment
    assert terminal_source.operation['terminal_outcome'] == terminal
    assert before.phase == 'waiting' and before.operation['control']['wait'] is None
    if retry.endswith('_after_purge'):
        work.orch.persistent_assignments = work.service
        work.orch.web_sessions = work.store
        work.orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
            runtime=work.runtime, repositories=work.runtime.repositories))
        app = FastAPI()
        app.state.orchestrator = work.orch
        incoming = request(work.sid, headers=[(b'authorization', ('Bearer ' + fixture[3]()).encode()), (b'content-type', b'application/json')])
        incoming.scope['app'] = app
        owner = await authenticate_work_control_request(incoming,
            assignments=work.service, sessions=work.store)
        await WorkControlService(work.service).delete(work.owner, owner.context.claims,
            source.assignment_id, WorkDeleteRequest(expected_revision=terminal_source.state_version), caller=owner)
        assert await _read(work, source) is None
        terminal_source = None
    subscribe_audits = await _audits(work, work.owner, 'assignment_subscribe')
    wake_audits = await _audits(work, work.owner, 'work.completion_wake')
    assert len(subscribe_audits) == 1 and len(wake_audits) == 1
    arguments = dict(source_operation_id=source.assignment_id, target_operation_id=target.assignment_id,
        submission_id=submission, expected_revision=target.state_version)
    reopened = AssignmentStore(plane_runtime=work.runtime)
    try:
        restarted = FrameworkWorkOperations(assignments=AssignmentService(work.orch, reopened,
            enabled=True, phi_gate=work.service.phi_gate), credentials=work.credentials)
        if retry in {'same_sid_after_restart', 'same_sid_after_purge'}:
            replay = await restarted.subscribe(caller, **arguments)
            assert replay['applied'] is False
            assert replay['subscription'] == {'source_operation_id': source.assignment_id,
                'target_operation_id': target.assignment_id}
            assert replay['operation']['revision'] == before.state_version
        elif retry in {'fresh_sid', 'fresh_sid_after_purge'}:
            arguments.update(submission_id=str(uuid4()), expected_revision=before.state_version)
            with pytest.raises(AssignmentError, match='work_not_found' if retry.endswith('_after_purge') else 'assignment_not_waiting'):
                await restarted.subscribe(caller, **arguments)
        elif retry == 'revoked_same_sid':
            work.credentials.revoke(owner_id=work.owner, credential_id=caller.credential_id)
            with pytest.raises(AssignmentError, match='framework_credential_authority_unavailable'):
                await restarted.subscribe(caller, **arguments)
        else:
            arguments['source_operation_id'] = other_source.assignment_id
            with pytest.raises(AssignmentError, match='assignment_idempotency_conflict'):
                await restarted.subscribe(caller, **arguments)
    finally:
        reopened.close()
    assert (await _read(work, target)).assignment == before
    source_after = await _read(work, source)
    assert (source_after.assignment if source_after else None) == terminal_source
    assert len(await _audits(work, work.owner, 'assignment_subscribe')) == len(subscribe_audits)
    assert len(await _audits(work, work.owner, 'work.completion_wake')) == len(wake_audits)
    assert work.audit.verify_chain(work.owner) is None
