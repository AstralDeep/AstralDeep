"""Tests for work resume (backend/orchestrator/work_control_authority.py,
work_controls.py): original-session binding, replay without reauth, retirement
rollback, and audit atomicity through real IAM and Plane.
"""

import importlib
from uuid import uuid4

import pytest

from orchestrator.work_control_authority import authenticate_work_control_request
from orchestrator.work_controls import WorkControlRequest
from orchestrator.work_submit import FixedResearchPreflight
from persistent_agents.models import AssignmentError
from tests.test_request_session_authority_088 import request
from tests.test_work_admission_api_postgres_088 import (
    api as api, fixture as fixture, plane as plane, research_service as research_service,
    service as service, signing_key as signing_key, source_service as source_service,
)
from tests.test_work_continuation_authority_088 import control, current
from tests.test_work_research_preflight_postgres_088 import research_command
from tests.test_work_submit_postgres_088 import context

runtime = plane
pytestmark = pytest.mark.asyncio


async def paused(api):
    accepted = await api.service.submit(await context(api.fixture, api.runtime),
                                        research_command(api.service))
    return control(api.runtime, accepted.record)


async def caller(api, sid):
    req = request(sid, headers=[(b"authorization", ("Bearer " + api.fixture[3]()).encode()),
        (b"content-type", b"application/json")])
    req.scope["app"] = api.app
    return await authenticate_work_control_request(req, assignments=api.service.assignments,
                                                  sessions=api.fixture[0])


def body(record):
    return WorkControlRequest(expected_revision=record.state_version, submission_id=str(uuid4()))


def service_for(api):
    return importlib.import_module("orchestrator.work_resume").WorkResumeService(api.service.assignments)


@pytest.mark.parametrize("same_session", [True, False])
async def test_resume_uses_original_a_and_preserves_independent_caller_b(api, same_session):
    record = await paused(api)
    sid = api.fixture[2] if same_session else uuid4().hex
    if not same_session:
        api.fixture[0].create(sid, user_id=api.fixture[1], access_token=api.fixture[3](),
            refresh_token="synthetic-other-caller-refresh", hard_max_seconds=3600)
    try:
        selected = await caller(api, sid)
        old_b = selected.require_session().credential
        before = len(api.fixture[-1])
        result = await service_for(api).resume(record.assignment_id, body(record), caller=selected)
        assert result["applied"] is True
        after = current(api.runtime, record)
        assert after.lifecycle == "active" and after.control_epoch == record.control_epoch + 1
        assert after.operation == record.operation and after.definition == record.definition
        assert after.checkpoint == record.checkpoint and after.usage == record.usage
        assert len(api.fixture[-1]) == before + 1
        fresh_b = api.fixture[0].capture_execution_reference(
            owner_id=api.fixture[1], session_id=sid).state.credential
        assert (fresh_b != old_b) is same_session
        rows, _ = api.service.audit.list_for_user(api.fixture[1])
        assert sum(row.action_type == "assignment_resume" for row in rows) == 1
        assert api.service.audit.verify_chain(api.fixture[1]) is None
    finally:
        if not same_session:
            api.fixture[0].delete(sid)


async def test_accepted_resume_replay_needs_no_cookie_runner_or_new_refresh(api):
    record = await paused(api)
    request_body = body(record)
    resume = service_for(api)
    await resume.resume(record.assignment_id, request_body, caller=await caller(api, api.fixture[2]))
    before = len(api.fixture[-1])
    api.orch.persistent_assignment_runner = None
    api.orch._llm_store = None
    result = await resume.resume(record.assignment_id, request_body, caller=await caller(api, None))
    assert result["applied"] is False and len(api.fixture[-1]) == before
    rows, _ = api.service.audit.list_for_user(api.fixture[1])
    assert sum(row.action_type == "assignment_resume" for row in rows) == 1


@pytest.mark.parametrize("change", ["bare_bearer", "stopped_runner", "stale_revision"])
async def test_new_resume_refuses_missing_caller_session_capability_or_revision_before_oauth(api, change):
    record = await paused(api)
    selected = await caller(api, None if change == "bare_bearer" else api.fixture[2])
    request_body = body(record)
    if change == "stopped_runner":
        api.runner._stopping = True
    elif change == "stale_revision":
        request_body = request_body.model_copy(update={"expected_revision": record.state_version - 1})
    before = len(api.fixture[-1])
    with pytest.raises(AssignmentError):
        await service_for(api).resume(record.assignment_id, request_body, caller=selected)
    assert current(api.runtime, record) == record
    assert len(api.fixture[-1]) == before
    rows, _ = api.service.audit.list_for_user(api.fixture[1])
    assert not any(row.action_type == "assignment_resume" for row in rows)


@pytest.mark.parametrize("retirement", ["runner", "key"])
async def test_retirement_at_final_policy_boundary_rolls_back_resume_and_audit(api, monkeypatch, retirement):
    record = await paused(api)
    selected = await caller(api, api.fixture[2])
    original = FixedResearchPreflight.assert_policy
    checked = []
    def policy(*args, **kwargs):
        result = original(*args, **kwargs)
        checked.append(True)
        if retirement == "runner":
            api.runner._stopping = True
        else:
            monkeypatch.setenv("AUDIT_HMAC_SECRET_PREFLIGHT_TEST", "synthetic-retired-key-" + "z" * 40)
        return result
    monkeypatch.setattr(FixedResearchPreflight, "assert_policy", staticmethod(policy))
    with pytest.raises(AssignmentError):
        await service_for(api).resume(record.assignment_id, body(record), caller=selected)
    assert checked == [True]
    assert current(api.runtime, record) == record
    rows, _ = api.service.audit.list_for_user(api.fixture[1])
    assert not any(row.action_type == "assignment_resume" for row in rows)


async def test_resume_snapshots_validated_body_before_awaiting_config(api, monkeypatch):
    from persistent_agents.runtime_values import digest
    record = await paused(api)
    request_body = body(record)
    original_id = request_body.submission_id
    expected_digest = digest({"api_version": 1, "operation_id": record.assignment_id,
                              "command": "resume", **request_body.model_dump()})
    original_prepare = FixedResearchPreflight.prepare

    async def prepare(self, **kwargs):
        prepared = await original_prepare(self, **kwargs)
        request_body.submission_id = str(uuid4())
        return prepared

    monkeypatch.setattr(FixedResearchPreflight, "prepare", prepare)
    result = await service_for(api).resume(record.assignment_id, request_body,
                                         caller=await caller(api, api.fixture[2]))
    assert result["applied"] is True
    with api.runtime.transaction() as tx:
        receipt = api.runtime.repositories.assignments.get_submission_receipt(
            tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
            submission_id=original_id, submission_digest=expected_digest, command="resume")
    assert receipt is not None


@pytest.mark.parametrize("change", ["policy", "operation", "config"])
async def test_resume_rejects_changes_after_preflight_without_overwriting_or_partial_audit(
    api, monkeypatch, change
):
    record = await paused(api)
    expected = [record]
    original = FixedResearchPreflight.prepare

    async def prepare(self, **kwargs):
        prepared = await original(self, **kwargs)
        if change == "policy":
            api.orch.tool_permissions.set_agent_scopes(
                record.owner_id, "web-research-1", {"tools:read": False})
        elif change == "operation":
            expected[0] = control(api.runtime, record)
        else:
            with api.runtime.transaction() as tx:
                api.runtime.repositories.encrypted_llm_config.delete_user(tx, owner_id=record.owner_id)
        return prepared

    monkeypatch.setattr(FixedResearchPreflight, "prepare", prepare)
    with pytest.raises(AssignmentError):
        await service_for(api).resume(record.assignment_id, body(record),
                                     caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, record) == expected[0]
    rows, _ = api.service.audit.list_for_user(record.owner_id)
    assert not any(row.action_type == "assignment_resume" for row in rows)


async def test_resume_commit_ack_loss_retains_original_receipt_and_one_audit(api, monkeypatch):
    record = await paused(api)
    selected = await caller(api, api.fixture[2])
    request_body = body(record)
    original = api.service.store.transaction
    lost = []

    async def transaction(callback, **kwargs):
        result = await original(callback, **kwargs)
        if isinstance(result, dict) and result.get("applied") is True and not lost:
            lost.append(True)
            raise AssignmentError("assignment_transaction_unavailable", 503)
        return result

    monkeypatch.setattr(api.service.store, "transaction", transaction)
    with pytest.raises(AssignmentError, match="assignment_transaction_unavailable"):
        await service_for(api).resume(record.assignment_id, request_body, caller=selected)
    assert lost == [True] and current(api.runtime, record).lifecycle == "active"
    refreshed = len(api.fixture[-1])
    result = await service_for(api).resume(record.assignment_id, request_body,
                                         caller=await caller(api, None))
    assert result["applied"] is False and len(api.fixture[-1]) == refreshed
    rows, _ = api.service.audit.list_for_user(record.owner_id)
    assert sum(row.action_type == "assignment_resume" for row in rows) == 1


async def test_resume_requires_successful_audit_in_same_transaction(api, monkeypatch):
    record = await paused(api)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit refusal")
    monkeypatch.setattr(api.service.audit, "insert_in_transaction", fail)
    with pytest.raises(AssignmentError):
        await service_for(api).resume(record.assignment_id, body(record),
                                     caller=await caller(api, api.fixture[2]))
    assert current(api.runtime, record) == record
    rows, _ = api.service.audit.list_for_user(record.owner_id)
    assert not any(row.action_type == "assignment_resume" for row in rows)


@pytest.mark.parametrize("invalid", ["caller", "body_type", "body_revision", "body_id"])
async def test_resume_refuses_invalid_private_inputs_before_refresh(api, invalid):
    record = await paused(api)
    selected = await caller(api, api.fixture[2])
    request_body = body(record)
    if invalid == "caller":
        selected = None
    elif invalid == "body_type":
        request_body = request_body.model_dump()
    else:
        field, value = ("expected_revision", True) if invalid == "body_revision" else ("submission_id", "invalid")
        request_body = request_body.model_copy(update={field: value})
    before = len(api.fixture[-1])
    with pytest.raises(AssignmentError) as caught:
        await service_for(api).resume(record.assignment_id, request_body, caller=selected)
    assert caught.value.code == ("work_authentication_required" if invalid == "caller" else "work_control_invalid")
    assert current(api.runtime, record) == record and len(api.fixture[-1]) == before


async def test_resume_does_not_replace_missing_original_a_with_current_b(api):
    record = await paused(api)
    sid = uuid4().hex
    api.fixture[0].create(sid, user_id=api.fixture[1], access_token=api.fixture[3](),
        refresh_token="synthetic-independent-caller", hard_max_seconds=3600)
    try:
        selected = await caller(api, sid)
        api.fixture[0].delete(api.fixture[2])
        before = len(api.fixture[-1])
        with pytest.raises(AssignmentError, match="work_authority_unavailable"):
            await service_for(api).resume(record.assignment_id, body(record), caller=selected)
        assert current(api.runtime, record) == record and len(api.fixture[-1]) == before
    finally:
        api.fixture[0].delete(sid)


async def test_resume_rejects_changed_fixed_reader_scope_before_refresh(api):
    record = await paused(api)
    selected = await caller(api, api.fixture[2])
    api.orch.tool_permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:search"})
    api.orch.tool_permissions.set_agent_scopes(record.owner_id, "web-research-1", {"tools:search": True})
    before = len(api.fixture[-1])
    with pytest.raises(AssignmentError, match="work_research_profile_unavailable"):
        await service_for(api).resume(record.assignment_id, body(record), caller=selected)
    assert current(api.runtime, record) == record and len(api.fixture[-1]) == before
