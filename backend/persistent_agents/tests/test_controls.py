"""Owner controls delegate durable CAS/replay, never local state mutation."""
from tests.helpers.session_consent_088 import synthetic_consent
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from persistent_agents.models import (
    AssignmentError,
    ControlRequest,
    CreateAssignmentRequest,
    ReviseAssignmentRequest,
)
from persistent_agents.tests.test_models import create_payload
from persistent_agents.tests.test_service import service as shared_service

service = shared_service


def control_payload():
    return {"submission_id": str(uuid4()), "expected_instruction_revision": 1, "expected_control_epoch": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["pause", "resume", "stop", "revoke", "run-now"])
async def test_control_calls_owner_repository_with_exact_cas(service, command):
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()), selected_session=synthetic_consent("owner"))
    real_call = service.store.call

    async def call(method, **kwargs):
        if method in ("apply_control", "request_check"):
            assert kwargs["owner_id"] == "owner"
            assert kwargs["expected_instruction_revision"] == 1
            assert kwargs["expected_control_epoch"] == 1
            assert len(kwargs["submission_digest"]) == 64
            assert method == ("request_check" if command == "run-now" else "apply_control")
            return record if command == "run-now" else SimpleNamespace(assignment=record, applied=True)
        return await real_call(method, **kwargs)

    service.store.call = AsyncMock(side_effect=call)
    result = await service.control("owner", {"sub": "owner"}, record.assignment_id, command,
                                    ControlRequest.model_validate(control_payload()))
    assert getattr(result, "assignment", result) == record


@pytest.mark.asyncio
async def test_stop_still_works_after_grant_revocation_and_resume_does_not(service):
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()), selected_session=synthetic_consent("owner"))
    service.orch.offline_grants.is_valid.return_value = False
    with pytest.raises(AssignmentError, match="authorization_required"):
        await service.control("owner", {"sub": "owner"}, record.assignment_id, "resume",
                               ControlRequest.model_validate(control_payload()))
    real_call = service.store.call

    async def call(method, **kwargs):
        if method == "apply_control":
            return SimpleNamespace(assignment=record, applied=True)
        return await real_call(method, **kwargs)
    service.store.call = call
    assert (await service.control("owner", {"sub": "owner"}, record.assignment_id, "stop",
                                  ControlRequest.model_validate(control_payload()))).applied


@pytest.mark.asyncio
async def test_revise_requires_new_consent_and_preserves_conflict(service):
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()), selected_session=synthetic_consent("owner"))
    payload = {**create_payload(), **control_payload(), "consent": False}
    with pytest.raises(AssignmentError, match="consent_required"):
        await service.revise("owner", {"sub": "owner"}, record.assignment_id,
                              ReviseAssignmentRequest.model_validate(payload), selected_session=synthetic_consent("owner"))


    payload["consent"] = True
    def revise(transaction, **kwargs):
        assert kwargs["replacement"].instructions == payload["instructions"]
        raise AssignmentError("assignment_stale_control")

    service.store.repository.apply_control = revise
    with pytest.raises(AssignmentError, match="stale_control"):
        await service.revise("owner", {"sub": "owner"}, record.assignment_id,
                              ReviseAssignmentRequest.model_validate(payload), selected_session=synthetic_consent("owner"))


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["not-a-uuid", str(uuid4())])
async def test_revise_unavailable_record_never_prepares_new_consent(service, identity):
    payload = {**create_payload(), **control_payload()}
    with pytest.raises(AssignmentError) as refused:
        await service.revise("owner", {"sub": "owner"}, identity,
                              ReviseAssignmentRequest.model_validate(payload),
                              selected_session=synthetic_consent("owner"))
    assert refused.value.status_code == 404
    service.orch.offline_grants.prepare_capture.assert_not_called()
