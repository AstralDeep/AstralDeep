"""Rechecks an owner's waiting Work operation through the existing durable completion wake fence.
Composes work_controls.py and work_subscriptions.py for HTTP and shared chrome recovery without granting execution authority.
"""

from __future__ import annotations

from orchestrator.work_completion_wake import (
    _wait, completion_event_source as wake_event_source, continuation_view,
)
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_controls import WorkControlRequest, WorkControlService, _method, _owned_operation
from orchestrator.work_service import _identity, _public
from orchestrator.work_subscriptions import reconcile_completion_wait
from persistent_agents.models import AssignmentError


def _continuation_view(tx, repository, owner, read):
    wait = _wait(read.assignment)
    if wait is None:
        return None
    source_id = wake_event_source(wait["event_key"])
    if source_id is None:
        return None
    source = _method(repository, "get_operation")(
        tx, owner_id=owner, assignment_id=source_id)
    return continuation_view(read, source)


class WorkContinuityService(WorkControlService):
    async def recheck(self, identity, body: WorkControlRequest, *, caller):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        if type(body) is not WorkControlRequest:
            raise AssignmentError("work_control_invalid", 422)
        identity = _identity(identity)

        def recheck(tx, repository):
            read = _owned_operation(tx, repository, owner, identity)
            if read.continuation_supported is not True:
                raise AssignmentError("assignment_version_unsupported", 409)
            record = read.assignment
            wait = _wait(record)
            if (wait is None or record.lifecycle != "active"
                    or record.phase != "awaiting_event"):
                return {"operation": _public(read, owner), "applied": False,
                        "continuation": _continuation_view(tx, repository, owner, read)}
            if body.expected_revision != record.state_version:
                raise AssignmentError("assignment_revision_conflict", 409)
            fired = reconcile_completion_wait(self.assignments, tx, repository, record)
            updated = _owned_operation(tx, repository, owner, identity)
            if fired:
                self.audit.append(tx, owner_id=owner, command="continuity",
                                  record=updated.assignment, submission_id=body.submission_id)
            return {"operation": _public(updated, owner), "applied": fired,
                    "continuation": _continuation_view(tx, repository, owner, updated)}

        result = await self._transaction(recheck, caller)
        await caller.verify_delivery()
        return result
