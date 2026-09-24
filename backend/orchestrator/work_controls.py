"""Owner-only pause, cancel, delete, and approve/reject commands for Work v1, audited
through work_control_audit.py under work_control_authority.py's caller fence. Never
admits work or grants execution. Used by work_api.py.
"""

from __future__ import annotations

from typing import Literal

from astralplane.repositories.assignment_models import (
    AssignmentActionDecision, AssignmentActionRecord, AssignmentRecord,
)
from pydantic import Field, ValidationError, field_validator

from orchestrator.work_control_audit import WorkControlAudit
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_service import _identity, _public
from persistent_agents.models import AssignmentError, StrictModel, validate_id
from persistent_agents.runtime_values import digest


class WorkDeleteRequest(StrictModel):
    expected_revision: int = Field(ge=1, le=2**63 - 1)


class WorkControlRequest(WorkDeleteRequest):
    submission_id: str = Field(min_length=36, max_length=36)

    @field_validator("submission_id")
    @classmethod
    def canonical_submission(cls, value):
        return validate_id(value)


class WorkDecideRequest(WorkControlRequest):
    proposal_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]


_PLANE_DECISION = {"approve": "approve", "reject": "decline"}
_DECIDED_STATES = frozenset({"approved", "declined"})


def _method(repository, name):
    method = getattr(repository, name, None)
    if not callable(method):
        raise AssignmentError("work_repository_unavailable", 503)
    return method


def _owned_operation(transaction, repository, owner_id, identity):
    read = _method(repository, "get_operation")(
        transaction, owner_id=owner_id, assignment_id=identity)
    if read is None:
        raise AssignmentError("work_not_found", 404)
    _public(read, owner_id)
    return read


class WorkControlService:
    def __init__(self, assignments):
        self.assignments = assignments
        self.store = assignments.store
        self.audit = WorkControlAudit(assignments)

    def _caller(self, caller, owner_id, claims):
        if (type(caller) is not WorkCallerAuthority or caller.context.owner_id != owner_id
                or caller.context.claims != claims):
            raise AssignmentError("work_authentication_required", 401)
        caller._assert_local(self.assignments)

    async def control(self, owner_id, claims, identity, command, body: WorkControlRequest, *, caller=None):
        self._caller(caller, owner_id, claims)
        self.assignments._owner(owner_id, claims)
        identity = _identity(identity)
        if command not in {"cancel", "pause"}:
            raise AssignmentError("work_control_invalid", 422)
        plane_command = "stop" if command == "cancel" else command
        signature = digest({"api_version": 1, "operation_id": identity,
                            "command": command, **body.model_dump()})

        def transaction(tx, repository):
            read = _owned_operation(tx, repository, owner_id, identity)
            receipt = _method(repository, "get_submission_receipt")(
                tx, owner_id=owner_id, assignment_id=identity,
                submission_id=body.submission_id, submission_digest=signature,
                command=plane_command)
            if receipt is not None:
                updated = _owned_operation(tx, repository, owner_id, identity)
                return {"operation": _public(updated, owner_id), "applied": False}, updated.assignment
            current = read.assignment
            result = _method(repository, "apply_control")(
                tx, owner_id=owner_id, assignment_id=identity,
                expected_instruction_revision=current.instruction_revision,
                expected_control_epoch=current.control_epoch,
                expected_state_version=body.expected_revision,
                submission_id=body.submission_id, submission_digest=signature,
                control=plane_command)
            updated = _owned_operation(tx, repository, owner_id, identity)
            if result.applied:
                self.audit.append(tx, owner_id=owner_id, command=plane_command,
                                  record=updated.assignment)
            return {"operation": _public(updated, owner_id), "applied": result.applied}, updated.assignment

        result, _record = await self._transaction(transaction, caller)
        await caller.verify_delivery()
        return result

    async def delete(self, owner_id, claims, identity, body: WorkDeleteRequest, *, caller=None):
        self._caller(caller, owner_id, claims)
        self.assignments._owner(owner_id, claims)
        identity = _identity(identity)

        def transaction(tx, repository):
            read = _owned_operation(tx, repository, owner_id, identity)
            deleted = _method(repository, "delete_for_owner")(
                tx, owner_id=owner_id, assignment_id=identity,
                expected_control_epoch=read.assignment.control_epoch,
                expected_state_version=body.expected_revision)
            if deleted is not True:
                raise AssignmentError("work_not_found", 404)
            self.audit.append(tx, owner_id=owner_id, command="delete", record=read.assignment)
            return read.assignment

        await self._transaction(transaction, caller)
        await caller.verify_delivery()
        return {"id": identity, "deleted": True}

    async def decide(self, identity, action_id, body: WorkDecideRequest, *, caller=None):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner_id, claims = caller.context.owner_id, caller.context.claims
        self._caller(caller, owner_id, claims)
        self.assignments._owner(owner_id, claims)
        if type(body) is not WorkDecideRequest:
            raise AssignmentError("work_control_invalid", 422)
        try:
            body = WorkDecideRequest.model_validate(body.model_dump())
        except ValidationError:
            raise AssignmentError("work_control_invalid", 422) from None
        identity, action_id = _identity(identity), _identity(action_id)
        signature = digest({"api_version": 1, "operation_id": identity, "action_id": action_id,
                            "command": "decide", **body.model_dump(exclude={"expected_revision"})})

        def transaction(tx, repository):
            read = _owned_operation(tx, repository, owner_id, identity)
            action = _method(repository, "get_action")(
                tx, owner_id=owner_id, assignment_id=identity, action_id=action_id)
            if (type(action) is not AssignmentActionRecord or action.owner_id != owner_id
                    or action.assignment_id != identity):
                raise AssignmentError("work_not_found", 404)
            if body.proposal_digest != action.intent.request_digest:
                raise AssignmentError("assignment_proposal_changed", 409)
            decided_before = action.state in _DECIDED_STATES
            decision = AssignmentActionDecision(
                body.proposal_digest, _PLANE_DECISION[body.decision], body.submission_id, signature,
                action.intent.permission_digest, action.intent.precondition_digest)
            current = read.assignment
            decided = _method(repository, "decide_action")(
                tx, owner_id=owner_id, assignment_id=identity, action_id=action_id,
                expected_instruction_revision=current.instruction_revision,
                expected_control_epoch=current.control_epoch,
                expected_state_version=body.expected_revision, decision=decision)
            if (type(decided) is not AssignmentActionRecord or decided.action_id != action_id
                    or decided.state not in _DECIDED_STATES):
                raise AssignmentError("work_control_unavailable", 503)
            updated = _owned_operation(tx, repository, owner_id, identity)
            applied = not decided_before
            if applied:
                self._append_decision(tx, owner_id=owner_id, record=updated.assignment,
                                      submission_id=body.submission_id, action_id=action_id,
                                      decision=body.decision)
            return {"operation": _public(updated, owner_id), "action_id": action_id,
                    "state": decided.state, "applied": applied}

        result = await self._transaction(transaction, caller)
        await caller.verify_delivery()
        return result

    def _append_decision(self, tx, *, owner_id, record, submission_id, action_id, decision):
        self.audit.assert_current()
        if (type(record) is not AssignmentRecord or record.owner_id != owner_id
                or decision not in _PLANE_DECISION):
            raise AssignmentError("work_control_unavailable", 503)
        try:
            metadata = {"submission_id": validate_id(submission_id),
                        "action_id": validate_id(action_id), "decision": decision}
        except (ValueError, TypeError, AttributeError):
            raise AssignmentError("work_control_unavailable", 503) from None
        self.audit._insert(tx, owner_id=owner_id, record=record, action_type="work.action.decide",
                           description="Work owner decision", metadata=metadata)

    async def _transaction(self, callback, caller):
        def current(transaction, repository):
            self.audit.assert_store_current()
            if repository is not self.audit.repository:
                raise AssignmentError("work_control_unavailable", 503)
            caller.assert_current(transaction, assignments=self.assignments)
            result = callback(transaction, repository)
            self.audit.assert_store_current()
            caller.assert_current(transaction, assignments=self.assignments)
            return result

        try:
            return await self.store.transaction(current, bound_session_waits=True)
        except AssignmentError as exc:
            if exc.status_code == 404:
                raise AssignmentError("work_not_found", 404) from exc
            raise
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise AssignmentError("work_control_unavailable", 503) from exc
