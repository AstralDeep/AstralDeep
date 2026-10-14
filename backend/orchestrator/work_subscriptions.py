"""Completion subscriptions for framework Work callers: subscribe, list, and cancel a
durable user-authorized "wake this operation when that operation finishes" continuation,
stored as the target's owner-event wait on a source-completion event key and released by
fire_completions when the source turns terminal. Used by work_operations.py,
work_controls.py, and persistent_agents/runner.py.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.assignment_models import (
    AssignmentControlResult, AssignmentOperationRead, AssignmentRecord,
)

from orchestrator.work_control_audit import WorkControlAudit
from orchestrator.work_controls import WorkControlService, _method, _owned_operation
from orchestrator.work_operations import _ORIGIN_NAMESPACE, _require_scope, _revision, _submission_id
from orchestrator.work_service import _identity, _public
from persistent_agents.models import AssignmentError, validate_id
from persistent_agents.runtime_values import digest

_logger = logging.getLogger("WorkSubscriptions")

_COMPLETION_PREFIX = "operation:"
_COMPLETION_SUFFIX = ":completed"
_SCAN_PAGE = 100
_SCAN_PAGES = 10
_MAX_LIST = 100
_REDRIVE_LIMIT = 256
_REDRIVE_ATTEMPTS = 3
_TERMINAL_OUTCOMES = frozenset({"completed", "failed", "cancelled"})
_REDRIVABLE_CODES = frozenset({
    "assignment_action_uncertain", "work_control_unavailable", "work_repository_unavailable",
})
_redrive_pending: dict[tuple[str, str], int] = {}


def completion_event_key(source_operation_id: str) -> str:
    try:
        return _COMPLETION_PREFIX + validate_id(source_operation_id) + _COMPLETION_SUFFIX
    except (ValueError, TypeError, AttributeError):
        raise AssignmentError("work_control_invalid", 422) from None


def completion_event_source(event_key: object) -> str | None:
    if (not isinstance(event_key, str) or not event_key.startswith(_COMPLETION_PREFIX)
            or not event_key.endswith(_COMPLETION_SUFFIX)):
        return None
    try:
        return validate_id(event_key[len(_COMPLETION_PREFIX):-len(_COMPLETION_SUFFIX)])
    except (ValueError, TypeError, AttributeError):
        return None


def _wait(record: AssignmentRecord) -> dict | None:
    operation = record.operation if isinstance(record.operation, Mapping) else {}
    control = operation.get("control") if isinstance(operation.get("control"), Mapping) else {}
    wait = control.get("wait")
    if isinstance(wait, Mapping) and set(wait) == {"event_key", "source_revision"}:
        return dict(wait)
    return None


def _watermark(record: AssignmentRecord, key: str) -> int:
    operation = record.operation if isinstance(record.operation, Mapping) else {}
    control = operation.get("control") if isinstance(operation.get("control"), Mapping) else {}
    watermarks = control.get("watermarks")
    value = watermarks.get(key, 0) if isinstance(watermarks, Mapping) else 0
    return value if type(value) is int else 0


def _signature(command: str, source_id: str, target_id: str) -> str:
    return digest({"api_version": 1, "namespace": _ORIGIN_NAMESPACE, "command": command,
                   "source_operation_id": source_id, "target_operation_id": target_id})


def _subscription(source_id: str, target_id: str) -> dict:
    return {"source_operation_id": source_id, "target_operation_id": target_id}


class WorkSubscriptionService(WorkControlService):
    def __init__(self, assignments, credentials):
        super().__init__(assignments)
        self.credentials = credentials

    def _caller(self, caller, scope: str):
        _require_scope(caller, scope)
        observation = self.credentials.fresh_observation(caller)
        if observation is None:
            raise AssignmentError("framework_credential_authority_unavailable", 409)
        return caller.owner_id, observation

    async def subscribe(self, caller, *, source_operation_id, target_operation_id,
                        submission_id, expected_revision) -> dict:
        owner, observation = self._caller(caller, "operations.control")
        source_id, target_id = _identity(source_operation_id), _identity(target_operation_id)
        sid = _submission_id(submission_id)
        revision = _revision(expected_revision)
        if source_id == target_id:
            raise AssignmentError("work_control_invalid", 422)
        key = completion_event_key(source_id)
        signature = _signature("subscribe", source_id, target_id)

        def subscribe(tx, repository):
            _method(repository, "get_operation")(
                tx, owner_id=owner, assignment_id=source_id, lock=True)
            self.credentials.assert_execution(tx, observation)
            source = _owned_operation(tx, repository, owner, source_id).assignment
            if source.lifecycle in {"stopped", "completed"} or source.operation.get("terminal_outcome") is not None:
                raise AssignmentError("assignment_not_waiting", 409)
            read = _owned_operation(tx, repository, owner, target_id)
            record = read.assignment
            if record.operation.get("authority", {}).get("origin") != "interactive":
                raise AssignmentError("framework_control_unavailable", 501)
            receipt = _method(repository, "get_submission_receipt")(
                tx, owner_id=owner, assignment_id=target_id, submission_id=sid,
                submission_digest=signature, command="wait")
            wait = _wait(record)
            if receipt is not None:
                return {"operation": _public(read, owner), "applied": False,
                        "subscription": _subscription(source_id, target_id)}
            if wait is not None:
                if wait["event_key"] != key:
                    raise AssignmentError("assignment_event_key_conflict", 409)
                return {"operation": _public(read, owner), "applied": False,
                        "subscription": _subscription(source_id, target_id)}
            self.credentials.assert_execution(tx, observation)
            result = _method(repository, "set_owner_event_wait")(
                tx, owner_id=owner, assignment_id=target_id,
                expected_instruction_revision=record.instruction_revision,
                expected_control_epoch=record.control_epoch,
                expected_state_version=revision,
                submission_id=sid, submission_digest=signature,
                event_key=key, source_revision=_watermark(record, key))
            if (type(result) is not AssignmentControlResult
                    or result.assignment.owner_id != owner
                    or result.assignment.assignment_id != target_id):
                raise AssignmentError("work_control_unavailable", 503)
            updated = _owned_operation(tx, repository, owner, target_id)
            if result.applied:
                self.audit.append(tx, owner_id=owner, command="subscribe",
                                  record=updated.assignment, submission_id=sid,
                                  source_id=source_id, target_id=target_id)
            return {"operation": _public(updated, owner), "applied": result.applied,
                    "subscription": _subscription(source_id, target_id)}

        return await self.store.transaction(subscribe, bound_session_waits=True)

    async def subscriptions(self, caller, *, limit=50, after_id=None) -> dict:
        _require_scope(caller, "operations.read")
        owner = caller.owner_id
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST:
            raise AssignmentError("work_control_invalid", 422)
        cursor = None if after_id is None else _identity(after_id)

        def scan(tx, repository):
            found = []
            position = cursor
            for _ in range(_SCAN_PAGES):
                page = _method(repository, "list_operations")(
                    tx, owner_id=owner, limit=_SCAN_PAGE, after_id=position)
                if not page:
                    break
                for read in page:
                    wait = _wait(read.assignment)
                    source_id = completion_event_source(wait["event_key"]) if wait else None
                    if source_id is not None:
                        found.append(_entry(tx, repository, owner, read, source_id, wait))
                position = page[-1].assignment.assignment_id
                if len(page) < _SCAN_PAGE:
                    break
            selected = found[:limit]
            next_after_id = selected[-1]["target_operation_id"] if len(found) > limit else None
            return {"subscriptions": selected, "next_after_id": next_after_id}

        return await self.store.transaction(scan, bound_session_waits=True)

    async def cancel(self, caller, *, source_operation_id, target_operation_id,
                     submission_id, expected_revision) -> dict:
        owner, observation = self._caller(caller, "operations.control")
        source_id, target_id = _identity(source_operation_id), _identity(target_operation_id)
        sid = _submission_id(submission_id)
        revision = _revision(expected_revision)
        key = completion_event_key(source_id)
        signature = _signature("unsubscribe", source_id, target_id)

        def cancel(tx, repository):
            _method(repository, "get_operation")(
                tx, owner_id=owner, assignment_id=target_id, lock=True)
            read = _owned_operation(tx, repository, owner, target_id)
            record = read.assignment
            wait = _wait(record)
            if wait is None or wait["event_key"] != key:
                raise AssignmentError("work_not_found", 404)
            self.credentials.assert_execution(tx, observation)
            result = _method(repository, "accept_wake")(
                tx, owner_id=owner, assignment_id=target_id,
                expected_state_version=revision,
                expected_instruction_revision=record.instruction_revision,
                expected_control_epoch=record.control_epoch,
                event_id=sid, event_key=key, source_revision=_watermark(record, key) + 1,
                event_digest=signature)
            if (type(result) is not AssignmentControlResult
                    or result.assignment.owner_id != owner
                    or result.assignment.assignment_id != target_id):
                raise AssignmentError("work_control_unavailable", 503)
            updated = _owned_operation(tx, repository, owner, target_id)
            if result.applied:
                self.audit.append(tx, owner_id=owner, command="unsubscribe",
                                  record=updated.assignment, submission_id=sid,
                                  source_id=source_id, target_id=target_id)
            return {"operation": _public(updated, owner), "applied": result.applied,
                    "subscription": _subscription(source_id, target_id)}

        return await self.store.transaction(cancel, bound_session_waits=True)


def _entry(tx, repository, owner: str, read: AssignmentOperationRead,
           source_id: str, wait: dict) -> dict:
    record = read.assignment
    entry = {"target_operation_id": record.assignment_id, "source_operation_id": source_id,
             "target_disposition": read.disposition, "target_phase": record.phase,
             "event_revision": wait["source_revision"]}
    source = _method(repository, "get_operation")(tx, owner_id=owner, assignment_id=source_id)
    entry["source_disposition"] = source.disposition if source is not None else None
    entry["source_missing"] = source is None
    return entry


def _release(tx, repository, audit: WorkControlAudit, owner_id: str, source_id: str,
             target: AssignmentRecord, source_revision: int, outcome: str) -> bool:
    wait = _wait(target)
    key = wait["event_key"]
    revision = max(source_revision, wait["source_revision"] + 1, _watermark(target, key) + 1)
    event_id = "completion:" + source_id + ":" + target.assignment_id + ":" + str(revision)
    try:
        with tx.savepoint("work_subscription_fire_" + uuid.uuid4().hex):
            fresh = _method(repository, "get_operation")(
                tx, owner_id=owner_id, assignment_id=target.assignment_id)
            if fresh is None:
                return False
            current = fresh.assignment
            current_wait = _wait(current)
            if (fresh.continuation_supported is not True or current.lifecycle != "active"
                    or current.phase != "awaiting_event" or current_wait is None
                    or current_wait["event_key"] != key):
                return False
            audit.assert_current()
            result = _method(repository, "accept_wake")(
                tx, owner_id=owner_id, assignment_id=current.assignment_id,
                expected_state_version=current.state_version,
                expected_instruction_revision=current.instruction_revision,
                expected_control_epoch=current.control_epoch,
                event_id=event_id, event_key=key, source_revision=revision,
                event_digest=digest([key, revision, outcome]))
            if (type(result) is not AssignmentControlResult
                    or result.assignment.owner_id != owner_id
                    or result.assignment.assignment_id != current.assignment_id):
                raise AssignmentError("work_control_unavailable", 503)
            if result.applied:
                audit.append_completion_wake(tx, owner_id=owner_id, record=result.assignment,
                                             source_id=source_id, revision=revision,
                                             outcome=outcome)
            return result.applied
    except (AssignmentError, RepositoryConflictError) as exc:
        if exc.code in _REDRIVABLE_CODES or isinstance(exc, AssignmentError) and exc.status_code >= 500:
            raise
        _logger.info("work_subscription_fire_blocked code=%s", exc.code)
        return False


def fire_completions(assignments, tx, repository, *, owner_id: str, source_id: str,
                     source_revision: int, outcome: str) -> int:
    if (outcome not in _TERMINAL_OUTCOMES or type(source_revision) is not int
            or source_revision < 1 or type(owner_id) is not str or not owner_id):
        return 0
    try:
        source_id = validate_id(source_id)
    except (ValueError, TypeError, AttributeError):
        return 0
    audit = WorkControlAudit(assignments)
    keys = {completion_event_key(source_id), "completion:" + source_id}
    return sum(_release(tx, repository, audit, owner_id, source_id, read.assignment,
                        source_revision, outcome)
               for read in event_waiters(repository, tx, owner_id=owner_id, event_keys=keys))


def event_waiters(repository, tx, *, owner_id: str, event_keys: set[str]):
    cursor = None
    for _ in range(_SCAN_PAGES):
        page = _method(repository, "list_operations")(
            tx, owner_id=owner_id, limit=_SCAN_PAGE, after_id=cursor)
        if not page:
            return
        for read in page:
            record = read.assignment
            wait = _wait(record)
            if (wait is not None and wait["event_key"] in event_keys
                    and record.lifecycle == "active" and record.phase == "awaiting_event"):
                yield read
        cursor = page[-1].assignment.assignment_id
        if len(page) < _SCAN_PAGE:
            return
    if _method(repository, "list_operations")(
            tx, owner_id=owner_id, limit=1, after_id=cursor):
        raise AssignmentError("work_completion_capacity_exhausted", 503)


def schedule_redrive(owner_id: str, source_id: str) -> None:
    try:
        if type(owner_id) is not str or not owner_id or type(source_id) is not str:
            return
        validate_id(source_id)
    except (ValueError, TypeError, AttributeError):
        _logger.warning("work_subscription_redrive_invalid_source")
        return
    if len(_redrive_pending) >= _REDRIVE_LIMIT:
        _redrive_pending.pop(next(iter(_redrive_pending)), None)
    _redrive_pending.setdefault((owner_id, source_id), 0)


async def redrive_pending(assignments) -> int:
    if not _redrive_pending:
        return 0
    fired = 0
    for key, attempts in list(_redrive_pending.items()):
        if attempts >= _REDRIVE_ATTEMPTS:
            _redrive_pending.pop(key, None)
            _logger.warning("work_subscription_redrive_exhausted")
            continue
        owner_id, source_id = key
        store = assignments.store

        def redrive(tx, repo, owner_id=owner_id, source_id=source_id):
            source = repo.get_operation(tx, owner_id=owner_id, assignment_id=source_id)
            if source is None or source.assignment.operation.get("terminal_outcome") is None:
                return -1
            return fire_completions(assignments, tx, repo, owner_id=owner_id,
                                    source_id=source_id,
                                    source_revision=source.assignment.state_version,
                                    outcome=source.assignment.operation["terminal_outcome"])

        try:
            outcome = await store.transaction(redrive, bound_session_waits=True)
        except AssignmentError as exc:
            _logger.warning("work_subscription_redrive_failed code=%s", exc.code)
            outcome = -1
        except Exception:
            _logger.warning("work_subscription_redrive_unavailable", exc_info=True)
            outcome = -1
        if outcome < 0:
            _redrive_pending[key] = attempts + 1
        else:
            _redrive_pending.pop(key, None)
            fired += outcome
    return fired
