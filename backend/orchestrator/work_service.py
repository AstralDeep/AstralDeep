"""Partial Feature 088: authenticated, payload-free one-shot operation reads.

This facade cannot admit, dispatch or control work. It shares the existing
assignment owner policy and bounded async Plane store.
"""
from __future__ import annotations

from datetime import UTC, datetime

from persistent_agents.models import AssignmentError, validate_id

_SAFE_ERRORS = frozenset({
    "assignment_action_uncertain", "assignment_approval_required",
    "assignment_authorization_required", "assignment_authorization_unavailable",
    "assignment_owner_retired", "assignment_deadline_exceeded",
    "assignment_retry_exhausted", "assignment_budget_exhausted",
    "assignment_interrupted", "assignment_failed", "assignment_result_quarantined",
    "assignment_precondition_changed",
})
_DISPOSITIONS = frozenset({
    "queued", "active", "awaiting_approval", "awaiting_authority", "budget_blocked",
    "reconciliation_required", "paused", "awaiting_event", "retry_eligible",
    "completed", "failed", "cancelled", "unsupported_version",
})
_USAGE_DIMENSIONS = ("model_calls", "tool_calls", "tokens", "elapsed_ms", "spend_micro_units")


def _integer(value, maximum=2**63 - 1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise AssignmentError("work_query_invalid", 422)
    return value


def _identity(value):
    try:
        return validate_id(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise AssignmentError("work_not_found", 404) from exc


def _time(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid operation timestamp")
    return value.astimezone(UTC).isoformat()


def _public(read, owner_id):
    record = read.assignment
    if record.owner_id != owner_id or record.execution_profile != "one_shot":
        raise AssignmentError("work_not_found", 404)
    supported = read.continuation_supported is True
    # Future operation JSON is opaque. Only its already-validated outer
    # assignment identity/lifecycle may be projected until the schema is known.
    operation = record.operation if supported else {}
    disposition = read.disposition if supported else (
        "cancelled" if record.lifecycle == "stopped" else "unsupported_version"
    )
    if disposition not in _DISPOSITIONS:
        raise ValueError("invalid operation disposition")
    usage = {}
    for bucket in ("spent", "daily", "outstanding"):
        if bucket not in record.usage:
            continue
        values = {}
        for key in _USAGE_DIMENSIONS:
            if key in record.usage[bucket]:
                value = record.usage[bucket][key]
                if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
                    raise ValueError("invalid operation usage")
                values[key] = value
        usage[bucket] = values
    error = record.safe_error_code
    return {
        "id": _identity(record.assignment_id), "revision": _integer(record.state_version),
        "instruction_revision": _integer(record.instruction_revision),
        "control_epoch": _integer(record.control_epoch), "title": record.definition.name,
        "kind": operation.get("kind") if operation.get("kind") in ("chat", "research") else None,
        "disposition": disposition, "lifecycle": record.lifecycle, "phase": record.phase,
        "created_at": _time(record.created_at), "updated_at": _time(record.updated_at),
        "next_wake_at": _time(record.next_wake_at),
        "deadline_at": _time(operation.get("deadline_at")), "schema_supported": supported,
        "safe_error_code": error if isinstance(error, str) and error in _SAFE_ERRORS
        else "work_unavailable" if error else None,
        "usage": usage,
    }


class WorkService:
    def __init__(self, assignments):
        self.assignments = assignments
        self.store = assignments.store

    def _owner(self, owner_id, claims):
        # Reuse the current FF_PERSISTENT_AGENTS, human-owner and private
        # dispatch-context policy; a second facade must not drift from it.
        self.assignments._owner(owner_id, claims)

    async def _read(self, method, owner_id, **kwargs):
        def transaction(tx, repository):
            callback = getattr(repository, method, None)
            if not callable(callback):
                raise AssignmentError("work_repository_unavailable", 503)
            value = callback(tx, owner_id=owner_id, **kwargs)
            if method == "get_operation":
                if value is None:
                    raise AssignmentError("work_not_found", 404)
                return _public(value, owner_id)
            return [_public(record, owner_id) for record in value]
        try:
            return await self.store.transaction(transaction)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise AssignmentError("work_read_unavailable", 503) from exc

    async def get(self, owner_id, claims, identity):
        self._owner(owner_id, claims)
        return await self._read("get_operation", owner_id, assignment_id=_identity(identity))

    async def list(self, owner_id, claims, *, limit=50, after_id=None):
        self._owner(owner_id, claims)
        _integer(limit, 100)
        if after_id is not None:
            _identity(after_id)
        records = await self._read("list_operations", owner_id, limit=limit, after_id=after_id)
        # A full page offers a continuation probe; it does not assert another
        # record exists or pretend a concurrent collection has a fixed snapshot.
        full = len(records) == limit
        return {"operations": records, "next_cursor": records[-1]["id"] if full else None,
                "page_full": full}

    async def poll(self, owner_id, claims, identity, *, after_revision=None):
        self._owner(owner_id, claims)
        if after_revision is not None:
            _integer(after_revision)
        current = await self._read("get_operation", owner_id, assignment_id=_identity(identity))
        revision = current["revision"]
        changed = revision != after_revision
        return {"revision": revision, "changed": changed,
                "resync_required": after_revision is not None and after_revision > revision,
                "operation": current if changed else None}
