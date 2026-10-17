"""Owner wait-for-event and reconcile-uncertain-attempt commands, layered on
work_controls.py and work_service.py without granting continuation. Used by
work_api.py and work_publication.py.
"""

from __future__ import annotations

import re
from typing import Literal

from astralplane.repositories.tracked_jobs import TrackedJobRecord
from astralplane.repositories.assignment_models import (
    AssignmentActionReconciliation, AssignmentActionReconciliationPreparation,
    AssignmentActionRecord, AssignmentControlResult, AssignmentOwnerWaitPreparation,
    AssignmentRecord,
)
from pydantic import Field, ValidationError, field_validator

from orchestrator import remote_jobs
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_controls import WorkControlRequest, WorkControlService, _method, _owned_operation
from orchestrator.work_service import _identity, _public
from orchestrator.work_subscriptions import assert_event_wait_capacity
from orchestrator.work_submit import _sync
from persistent_agents.models import AssignmentError, validate_id
from persistent_agents.runtime_values import digest


class WorkOwnerWaitRequest(WorkControlRequest):
    owner_event_id: str = Field(min_length=36, max_length=36)
    owner_revision: int = Field(ge=0, le=2**53 - 1)

    @field_validator("owner_event_id")
    @classmethod
    def canonical_owner_event(cls, value):
        return validate_id(value)


class WorkReconcileRequest(WorkControlRequest):
    prior_result_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decision: Literal["confirmed_applied", "confirmed_not_applied"]


class WorkJobWaitRequest(WorkControlRequest):
    tracked_job_id: str = Field(min_length=32, max_length=32)
    machine_id: str = Field(min_length=32, max_length=32)
    scheduler_job_id: str = Field(min_length=1, max_length=32)

    @field_validator("tracked_job_id", "machine_id")
    @classmethod
    def canonical_tracked_ids(cls, value):
        if re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError("a canonical 32-hex identifier is required")
        return value

    @field_validator("scheduler_job_id")
    @classmethod
    def canonical_scheduler_job(cls, value):
        if re.fullmatch(r"[0-9]{1,32}", value) is None:
            raise ValueError("a numeric scheduler job id is required")
        return value


def _request(body, expected):
    if type(body) is not expected:
        raise AssignmentError("work_control_invalid", 422)
    try:
        return expected.model_validate(body.model_dump())
    except ValidationError:
        raise AssignmentError("work_control_invalid", 422) from None


def _prepared(value, expected, owner, identity):
    if (type(value) is not expected or type(value.assignment) is not AssignmentRecord
            or value.assignment.owner_id != owner or value.assignment.assignment_id != identity
            or type(value.replayed) is not bool):
        raise AssignmentError("work_control_unavailable", 503)
    return value


class WorkContinuationService(WorkControlService):
    def _owner(self, caller):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        return owner

    async def wait(self, identity, body: WorkOwnerWaitRequest, *, caller):
        owner = self._owner(caller)
        body = _request(body, WorkOwnerWaitRequest)
        identity = _identity(identity)
        signature = digest({"api_version": 1, "operation_id": identity,
                            "command": "wait", **body.model_dump()})

        def wait(tx, repository):
            read = _owned_operation(tx, repository, owner, identity)
            receipt = _sync(_method(repository, "get_submission_receipt")(
                tx, owner_id=owner, assignment_id=identity, submission_id=body.submission_id,
                submission_digest=signature, command="wait"))
            if receipt is not None:
                return {"operation": _public(read, owner), "applied": False}
            current = read.assignment
            args = dict(owner_id=owner, assignment_id=identity,
                expected_instruction_revision=current.instruction_revision,
                expected_control_epoch=current.control_epoch, expected_state_version=body.expected_revision,
                submission_id=body.submission_id, submission_digest=signature,
                event_key="owner:" + body.owner_event_id, source_revision=body.owner_revision,
                control_version=1)
            prepared = _prepared(_sync(_method(repository, "prepare_owner_event_wait")(tx, **args)),
                AssignmentOwnerWaitPreparation, owner, identity)
            if prepared.replayed:
                return {"operation": _public(_owned_operation(tx, repository, owner, identity), owner),
                        "applied": False}
            self.audit.append(tx, owner_id=owner, command="wait", record=prepared.assignment,
                              submission_id=body.submission_id)
            result = _sync(_method(repository, "set_owner_event_wait")(tx, **args))
            if (type(result) is not AssignmentControlResult or result.applied is not True
                    or result.assignment.owner_id != owner or result.assignment.assignment_id != identity):
                raise AssignmentError("work_control_unavailable", 503)
            return {"operation": _public(_owned_operation(tx, repository, owner, identity), owner),
                    "applied": True}

        result = await self._transaction(wait, caller)
        await caller.verify_delivery()
        return result

    async def wait_job(self, identity, body: WorkJobWaitRequest, *, caller):
        owner = self._owner(caller)
        body = _request(body, WorkJobWaitRequest)
        identity = _identity(identity)
        source = plane_source_from_orchestrator(self.assignments.orch)
        context = remote_jobs._tracked_context(source)
        if context.plane_runtime is not self.store.plane_runtime:
            raise AssignmentError("work_control_unavailable", 503)
        event_key = remote_jobs.WAKE_EVENT_PREFIX + body.tracked_job_id
        signature = digest({"api_version": 1, "operation_id": identity,
                            "command": "wait_job", **body.model_dump()})

        def wait(tx, repository):
            read = _owned_operation(tx, repository, owner, identity)
            receipt = _method(repository, "get_submission_receipt")(
                tx, owner_id=owner, assignment_id=identity, submission_id=body.submission_id,
                submission_digest=signature, command="wait")
            if receipt is not None:
                return {"operation": _public(read, owner), "applied": False}
            job = context.repository.get(
                tx, owner_id=owner, tracked_job_id=body.tracked_job_id, lock=True)
            if job is None:
                raise AssignmentError("work_not_found", 404)
            if type(job) is not TrackedJobRecord or job.owner_id != owner:
                raise AssignmentError("work_control_unavailable", 503)
            if job.machine_id != body.machine_id or job.scheduler_job_id != body.scheduler_job_id:
                raise AssignmentError("work_not_found", 404)
            if job.terminal:
                raise AssignmentError("work_job_already_terminal", 409)
            source_revision = job.last_polled_at if job.last_polled_at is not None else job.created_at
            current = read.assignment
            assert_event_wait_capacity(repository, tx, owner_id=owner,
                                       event_keys=(event_key,), target=current)
            args = dict(owner_id=owner, assignment_id=identity,
                        expected_instruction_revision=current.instruction_revision,
                        expected_control_epoch=current.control_epoch,
                        expected_state_version=body.expected_revision,
                        submission_id=body.submission_id, submission_digest=signature,
                        event_key=event_key, source_revision=source_revision,
                        control_version=1)
            result = _sync(_method(repository, "set_owner_event_wait")(tx, **args))
            if (type(result) is not AssignmentControlResult or result.applied is None
                    or result.assignment.owner_id != owner
                    or result.assignment.assignment_id != identity):
                raise AssignmentError("work_control_unavailable", 503)
            prepared_applied = bool(result.applied)
            if prepared_applied:
                self.audit.append(tx, owner_id=owner, command="wait",
                                  record=result.assignment,
                                  submission_id=body.submission_id)
            return {"operation": _public(_owned_operation(tx, repository, owner, identity), owner),
                    "applied": prepared_applied}

        result = await self._transaction(wait, caller)
        await caller.verify_delivery()
        return result

    async def reconcile(self, identity, action_id, body: WorkReconcileRequest, *, caller):
        owner = self._owner(caller)
        body = _request(body, WorkReconcileRequest)
        identity, action_id = _identity(identity), _identity(action_id)
        signature = digest({"api_version": 1, "operation_id": identity, "action_id": action_id,
            "command": "reconcile", **body.model_dump(exclude={"expected_revision"})})
        decision = AssignmentActionReconciliation(body.prior_result_digest, body.decision,
            "work-owner-decision:v1:" + body.submission_id, body.submission_id, signature)

        def reconcile(tx, repository):
            current = _owned_operation(tx, repository, owner, identity).assignment
            args = dict(owner_id=owner, assignment_id=identity, action_id=action_id,
                expected_instruction_revision=current.instruction_revision,
                expected_control_epoch=current.control_epoch, expected_state_version=body.expected_revision,
                decision=decision, authority=None)
            prepared = _prepared(_sync(_method(repository, "prepare_action_reconciliation")(tx, **args)),
                AssignmentActionReconciliationPreparation, owner, identity)
            if not prepared.replayed:
                self.audit.append(tx, owner_id=owner, command="reconcile", record=prepared.assignment,
                    submission_id=body.submission_id, action_id=action_id, decision=body.decision)
                settled = _sync(_method(repository, "reconcile_action")(tx, **args))
                if (type(settled) is not AssignmentActionRecord or settled.owner_id != owner
                        or settled.assignment_id != identity or settled.action_id != action_id):
                    raise AssignmentError("work_control_unavailable", 503)
            return {"operation": _public(_owned_operation(tx, repository, owner, identity), owner),
                    "action_id": action_id, "applied": not prepared.replayed}

        result = await self._transaction(reconcile, caller)
        await caller.verify_delivery()
        return result
