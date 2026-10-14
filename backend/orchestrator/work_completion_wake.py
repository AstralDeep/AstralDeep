"""Owner-authorized "wake this work when that operation finishes" continuations: a
bounded proposal/confirmation that durably holds the waiter on a completion event
key through the guarded event-wait records, plus the completion-transaction wake
that releases waiting work to ordinary dispatch. Used by work_api.py,
work_service.py, work_controls.py, work_operations.py, and the assignment runner.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignment_models import (
    AssignmentControlResult, AssignmentOperationRead, AssignmentRecord,
)
from pydantic import Field, ValidationError, field_validator

from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_controls import (
    WorkControlRequest, WorkControlService, _method, _owned_operation,
)
from orchestrator.work_subscriptions import (
    _REDRIVE_ATTEMPTS as _REDRIVE_ATTEMPTS, _REDRIVE_LIMIT as _REDRIVE_LIMIT,
    _redrive_pending as _redrive_pending, fire_completions as fire_completions,
    redrive_pending as redrive_pending, schedule_redrive as schedule_redrive,
)
from orchestrator.work_service import _identity, _public
from persistent_agents.models import AssignmentError, validate_id
from persistent_agents.runtime_values import digest

_COMPLETION_PREFIX = "completion:"
_EVENT_NAMESPACE = "work.completion.v1"
_TERMINAL_OUTCOMES = frozenset({"completed", "failed", "cancelled"})
_PROPOSAL_TTL_SECONDS = 900
_PROPOSALS_PER_OWNER = 64


def completion_event_key(source_operation_id: str) -> str:
    try:
        return _COMPLETION_PREFIX + validate_id(source_operation_id)
    except (ValueError, TypeError, AttributeError):
        raise AssignmentError("work_control_invalid", 422) from None


def completion_event_source(event_key: object) -> str | None:
    if not isinstance(event_key, str) or not event_key.startswith(_COMPLETION_PREFIX):
        return None
    try:
        return validate_id(event_key[len(_COMPLETION_PREFIX):])
    except (ValueError, TypeError, AttributeError):
        return None


def _wait(record: AssignmentRecord) -> dict | None:
    operation = record.operation if isinstance(record.operation, Mapping) else {}
    control = operation.get("control") if isinstance(operation.get("control"), Mapping) else {}
    wait = control.get("wait")
    if isinstance(wait, Mapping) and set(wait) == {"event_key", "source_revision"}:
        return {"event_key": wait["event_key"], "source_revision": wait["source_revision"]}
    return None


def _watermark(record: AssignmentRecord, key: str) -> int:
    operation = record.operation if isinstance(record.operation, Mapping) else {}
    control = operation.get("control") if isinstance(operation.get("control"), Mapping) else {}
    watermarks = control.get("watermarks")
    value = watermarks.get(key, 0) if isinstance(watermarks, Mapping) else 0
    return value if type(value) is int else 0


def terminal_outcome(record: object) -> str | None:
    if type(record) is not AssignmentRecord or record.execution_profile != "one_shot":
        return None
    if record.lifecycle == "stopped":
        return "cancelled"
    if record.lifecycle != "completed":
        return None
    operation = record.operation if isinstance(record.operation, Mapping) else {}
    outcome = operation.get("terminal_outcome")
    return outcome if outcome in _TERMINAL_OUTCOMES else "completed"


def continuation_view(read: AssignmentOperationRead,
                      source_read: AssignmentOperationRead | None) -> dict | None:
    record = read.assignment
    wait = _wait(record)
    source_id = completion_event_source(wait["event_key"]) if wait else None
    if source_id is None:
        return None
    view = {"source_operation_id": source_id, "event_revision": wait["source_revision"],
            "awaiting_event": record.lifecycle == "active" and record.phase == "awaiting_event"}
    if source_read is None:
        view.update(source_disposition=None, source_missing=True)
    else:
        view.update(source_disposition=source_read.disposition, source_missing=False,
                    source_terminal_outcome=terminal_outcome(source_read.assignment))
    return view


def _dispatchable_waiter(record: AssignmentRecord) -> bool:
    operation = record.operation if isinstance(record.operation, Mapping) else {}
    authority = (operation.get("authority")
                 if isinstance(operation.get("authority"), Mapping) else {})
    return authority.get("origin") == "interactive"


def _wake_event_id(source_id: str, target_id: str, revision: int) -> str:
    return f"completion:{source_id}:{target_id}:{revision}"


def _event_digest(command: str, source_id: str, target_id: str, revision: int,
                  outcome: str | None = None) -> str:
    content = {"api_version": 1, "namespace": _EVENT_NAMESPACE, "command": command,
               "source_operation_id": source_id, "target_operation_id": target_id,
               "event_revision": revision}
    if outcome is not None:
        content["outcome"] = outcome
    return digest(content)


class WorkContinuationProposalRequest(WorkControlRequest):
    source_operation_id: str = Field(min_length=36, max_length=36)

    @field_validator("source_operation_id")
    @classmethod
    def canonical_source(cls, value):
        return validate_id(value)


class WorkContinuationConfirmRequest(WorkContinuationProposalRequest):
    proposal_id: str = Field(min_length=32, max_length=32)
    proposal_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("proposal_id")
    @classmethod
    def canonical_proposal(cls, value):
        if re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError
        return value


def _proposals(orch) -> dict:
    if not hasattr(orch, "_work_continuation_proposals"):
        orch._work_continuation_proposals = {}
    return orch._work_continuation_proposals


def _proposal_digest(waiter_id: str, source_id: str) -> str:
    return digest({"api_version": 1, "waiter_operation_id": waiter_id,
                   "source_operation_id": source_id})


def _request(body, expected):
    if type(body) is not expected:
        raise AssignmentError("work_control_invalid", 422)
    try:
        return expected.model_validate(body.model_dump())
    except ValidationError:
        raise AssignmentError("work_control_invalid", 422) from None


class WorkCompletionWakeService(WorkControlService):
    async def propose(self, identity, body: WorkContinuationProposalRequest, *, caller):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        body = _request(body, WorkContinuationProposalRequest)
        waiter_id = _identity(identity)
        source_id = _identity(body.source_operation_id)
        if waiter_id == source_id:
            raise AssignmentError("work_control_invalid", 422)

        def inspect(tx, repository):
            waiter = _owned_operation(tx, repository, owner, waiter_id)
            if waiter.continuation_supported is not True:
                raise AssignmentError("assignment_version_unsupported", 409)
            if not _dispatchable_waiter(waiter.assignment):
                raise AssignmentError("work_continuation_waiter_unsupported", 409)
            wait = _wait(waiter.assignment)
            if wait is not None and wait["event_key"] != completion_event_key(source_id):
                raise AssignmentError("assignment_event_key_conflict", 409)
            if (waiter.assignment.lifecycle == "stopped"
                    or terminal_outcome(waiter.assignment) is not None):
                raise AssignmentError("assignment_not_waiting", 409)
            source = _owned_operation(tx, repository, owner, source_id)
            if source.continuation_supported is not True:
                raise AssignmentError("assignment_version_unsupported", 409)
            if terminal_outcome(source.assignment) is not None:
                raise AssignmentError("work_continuation_source_terminal", 409)
            return waiter, source

        await self._transaction(inspect, caller)
        proposals = _proposals(self.assignments.orch)
        bucket = proposals.setdefault(owner, {})
        while len(bucket) >= _PROPOSALS_PER_OWNER:
            bucket.pop(next(iter(bucket)))
        proposal_id = uuid.uuid4().hex
        bucket[proposal_id] = {"waiter": waiter_id, "source": source_id,
                               "created_at": time.time()}
        return {"proposal_id": proposal_id,
                "proposal_digest": _proposal_digest(waiter_id, source_id),
                "source_operation_id": source_id, "waiter_operation_id": waiter_id,
                "expires_at": int(time.time()) + _PROPOSAL_TTL_SECONDS}

    def _proposal(self, owner, waiter_id, source_id, body):
        bucket = _proposals(self.assignments.orch).get(owner)
        entry = bucket.get(body.proposal_id) if isinstance(bucket, dict) else None
        if not isinstance(entry, dict):
            raise AssignmentError("work_proposal_expired", 409)
        if time.time() - entry["created_at"] > _PROPOSAL_TTL_SECONDS:
            bucket.pop(body.proposal_id, None)
            raise AssignmentError("work_proposal_expired", 409)
        if (entry.get("waiter") != waiter_id or entry.get("source") != source_id
                or _proposal_digest(waiter_id, source_id) != body.proposal_digest):
            raise AssignmentError("assignment_proposal_changed", 409)
        return entry

    def _drop_proposal(self, owner, proposal_id):
        bucket = _proposals(self.assignments.orch).get(owner)
        if isinstance(bucket, dict):
            bucket.pop(proposal_id, None)

    async def confirm(self, identity, body: WorkContinuationConfirmRequest, *, caller):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        body = _request(body, WorkContinuationConfirmRequest)
        waiter_id = _identity(identity)
        source_id = _identity(body.source_operation_id)
        if waiter_id == source_id:
            raise AssignmentError("work_control_invalid", 422)
        key = completion_event_key(source_id)
        signature = digest({"api_version": 1, "operation_id": waiter_id,
                            "command": "continuation.confirm",
                            **body.model_dump(exclude={"expected_revision"})})

        def subscribe(tx, repository):
            read = _owned_operation(tx, repository, owner, waiter_id)
            receipt = _method(repository, "get_submission_receipt")(
                tx, owner_id=owner, assignment_id=waiter_id, submission_id=body.submission_id,
                submission_digest=signature, command="wait")
            if receipt is not None:
                return {"operation": _public(read, owner), "applied": False,
                        "continuation": continuation_view(read, _source_read(tx, repository,
                                                                             owner, source_id))}
            self._proposal(owner, waiter_id, source_id, body)
            record = read.assignment
            if not _dispatchable_waiter(record):
                raise AssignmentError("work_continuation_waiter_unsupported", 409)
            wait = _wait(record)
            if wait is not None:
                if wait["event_key"] != key:
                    raise AssignmentError("assignment_event_key_conflict", 409)
                return {"operation": _public(read, owner), "applied": False,
                        "continuation": continuation_view(read, _source_read(tx, repository,
                                                                             owner, source_id))}
            source_read = _method(repository, "get_operation")(
                tx, owner_id=owner, assignment_id=source_id, lock=True)
            if source_read is None:
                raise AssignmentError("work_not_found", 404)
            if source_read.continuation_supported is not True:
                raise AssignmentError("assignment_version_unsupported", 409)
            if terminal_outcome(source_read.assignment) is not None:
                raise AssignmentError("work_continuation_source_terminal", 409)
            result = _method(repository, "set_owner_event_wait")(
                tx, owner_id=owner, assignment_id=waiter_id,
                expected_instruction_revision=record.instruction_revision,
                expected_control_epoch=record.control_epoch,
                expected_state_version=body.expected_revision,
                submission_id=body.submission_id, submission_digest=signature,
                event_key=key, source_revision=source_read.assignment.state_version)
            if (type(result) is not AssignmentControlResult
                    or result.assignment.owner_id != owner
                    or result.assignment.assignment_id != waiter_id):
                raise AssignmentError("work_control_unavailable", 503)
            updated = _owned_operation(tx, repository, owner, waiter_id)
            if result.applied:
                self.audit.append(tx, owner_id=owner, command="subscribe",
                                  record=updated.assignment, submission_id=body.submission_id,
                                  source_id=source_id, target_id=waiter_id)
            return {"operation": _public(updated, owner), "applied": result.applied,
                    "continuation": {"source_operation_id": source_id,
                                     "event_revision": source_read.assignment.state_version}}

        result = await self._transaction(subscribe, caller)
        if result["applied"] is True:
            self._drop_proposal(owner, body.proposal_id)
        await caller.verify_delivery()
        return result

    async def revoke(self, identity, source_operation_id, body: WorkControlRequest, *, caller):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        body = _request(body, WorkControlRequest)
        waiter_id = _identity(identity)
        source_id = _identity(source_operation_id)
        key = completion_event_key(source_id)

        def unsubscribe(tx, repository):
            read = _owned_operation(tx, repository, owner, waiter_id)
            record = read.assignment
            wait = _wait(record)
            if wait is None or wait["event_key"] != key:
                raise AssignmentError("work_not_found", 404)
            revision = max(wait["source_revision"], _watermark(record, key)) + 1
            result = _method(repository, "accept_wake")(
                tx, owner_id=owner, assignment_id=waiter_id,
                expected_state_version=body.expected_revision,
                expected_instruction_revision=record.instruction_revision,
                expected_control_epoch=record.control_epoch,
                event_id=_wake_event_id(source_id, waiter_id, revision),
                event_key=key, source_revision=revision,
                event_digest=_event_digest("unsubscribe", source_id, waiter_id, revision))
            if (type(result) is not AssignmentControlResult
                    or result.assignment.owner_id != owner
                    or result.assignment.assignment_id != waiter_id):
                raise AssignmentError("work_control_unavailable", 503)
            updated = _owned_operation(tx, repository, owner, waiter_id)
            if result.applied:
                self.audit.append(tx, owner_id=owner, command="unsubscribe",
                                  record=updated.assignment, submission_id=body.submission_id,
                                  source_id=source_id, target_id=waiter_id)
            return {"operation": _public(updated, owner), "applied": result.applied,
                    "continuation": continuation_view(updated, _source_read(tx, repository,
                                                                            owner, source_id))}

        result = await self._transaction(unsubscribe, caller)
        await caller.verify_delivery()
        return result


def _source_read(tx, repository, owner, source_id):
    try:
        return _method(repository, "get_operation")(
            tx, owner_id=owner, assignment_id=source_id)
    except RepositoryConflictError:
        return None
