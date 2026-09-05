"""Owner controls delegate durable CAS/replay, never local state mutation."""
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
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
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
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
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
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    payload = {**create_payload(), **control_payload(), "consent": False}
    with pytest.raises(AssignmentError, match="consent_required"):
        await service.revise("owner", {"sub": "owner"}, record.assignment_id,
                              ReviseAssignmentRequest.model_validate(payload))
    payload["consent"] = True
    real_call = service.store.call

    async def call(method, **kwargs):
        if method == "apply_control":
            assert kwargs["replacement"].instructions == payload["instructions"]
            raise AssignmentError("assignment_stale_control")
        return await real_call(method, **kwargs)
    service.store.call = call
    with pytest.raises(AssignmentError, match="stale_control"):
        await service.revise("owner", {"sub": "owner"}, record.assignment_id,
                              ReviseAssignmentRequest.model_validate(payload))
