"""Owner-only pause, cancellation and terminal deletion for Work v1.

These commands never admit work or grant authority. Resume requires a separate
qualified current-authority adapter. Cancellation cannot recall an issued effect;
Plane retains its liability and refuses deletion until settlement is understood.
"""
from __future__ import annotations

from pydantic import Field, field_validator

from orchestrator.work_control_audit import WorkControlAudit
from orchestrator.work_service import _identity, _public
from persistent_agents.models import AssignmentError, StrictModel, validate_id
from persistent_agents.runtime_values import digest


class WorkDeleteRequest(StrictModel):
    """The observed Work revision, never a worker execution fence."""

    expected_revision: int = Field(ge=1, le=2**63 - 1)


class WorkControlRequest(WorkDeleteRequest):
    """A bounded duplicate-safe owner command within this versioned API."""

    submission_id: str = Field(min_length=36, max_length=36)

    @field_validator("submission_id")
    @classmethod
    def canonical_submission(cls, value):
        return validate_id(value)


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
    # Reuse the exact public read/profile/owner validation before any mutation.
    _public(read, owner_id)
    return read


class WorkControlService:
    """Compose current-state Plane commands through the existing async store."""

    def __init__(self, assignments):
        self.assignments = assignments
        self.store = assignments.store
        self.audit = WorkControlAudit(assignments)

    async def control(self, owner_id, claims, identity, command, body: WorkControlRequest):
        """Pause or cancel once; exact replay acknowledges current safe state."""
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

        result, _record = await self._transaction(transaction)
        return result

    async def delete(self, owner_id, claims, identity, body: WorkDeleteRequest):
        """Delete settled terminal work; absent/foreign/repeated IDs remain 404.

        Plane retains the original submission identity against effect replay but
        has no delete-command receipt. This method does not invent such a receipt.
        """
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

        await self._transaction(transaction)
        return {"id": identity, "deleted": True}

    async def _transaction(self, callback):
        def current(transaction, repository):
            self.audit.assert_store_current()
            if repository is not self.audit.repository:
                raise AssignmentError("work_control_unavailable", 503)
            result = callback(transaction, repository)
            self.audit.assert_store_current()
            return result

        try:
            return await self.store.transaction(current, bound_session_waits=True)
        except AssignmentError as exc:
            if exc.status_code == 404:
                raise AssignmentError("work_not_found", 404) from exc
            raise
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            # A malformed/future repository contract is not an empty success.
            raise AssignmentError("work_control_unavailable", 503) from exc
