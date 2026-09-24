"""Read-only facade for one-shot operation metadata, timing/claim measurements, and
retained results via work_result.py. Cannot admit, dispatch, or control work. Shared
by work_api.py, work_controls.py, and work_operations.py.
"""

from __future__ import annotations

from collections.abc import Mapping
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
_USAGE_BASIS = frozenset({"observed", "estimated", "uncertain", "none"})
_MONEY_STATUS = frozenset({"unknown", "reported"})
_TERMINAL_DISPOSITIONS = frozenset({"completed", "failed", "cancelled"})
MEASUREMENT_PAGE = 100


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


def _basis(basis):
    if basis is None:
        return None
    if not isinstance(basis, Mapping):
        raise ValueError("invalid operation usage basis")
    values = {}
    for key in _USAGE_DIMENSIONS:
        if key in basis:
            value = basis[key]
            if value is not None and value not in _USAGE_BASIS:
                raise ValueError("invalid operation usage basis")
            values[key] = value
    return values


def _public(read, owner_id):
    record = read.assignment
    if record.owner_id != owner_id or record.execution_profile != "one_shot":
        raise AssignmentError("work_not_found", 404)
    supported = read.continuation_supported is True
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
    if "basis" in record.usage:
        usage["basis"] = _basis(record.usage["basis"])
    for key, allowed in (("money_status", _MONEY_STATUS), ("currency", None)):
        if key not in record.usage:
            continue
        value = record.usage[key]
        if value is not None and (
                type(value) is not str
                or (value not in allowed if allowed is not None
                    else not 1 <= len(value) <= 8 or value.strip() != value)):
            raise ValueError("invalid operation usage")
        usage[key] = value
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


def _moment(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("invalid operation timestamp")
    return value.astimezone(UTC)


def _span(start, end):
    if start is None or end is None:
        return None
    span = int((end - start).total_seconds() * 1000)
    return span if span >= 0 else None


def _observed(attempt):
    if not isinstance(attempt, Mapping):
        raise ValueError("invalid operation claim")
    outcome = attempt.get("outcome")
    actual = outcome.get("actual") if isinstance(outcome, Mapping) else None
    value = actual.get("elapsed_ms") if isinstance(actual, Mapping) else None
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("invalid operation claim")
    return value


def _task(action):
    attempts = getattr(action, "attempts", ())
    if type(attempts) is not tuple:
        raise ValueError("invalid operation claims")
    observed = None
    measured = 0
    for attempt in attempts:
        value = _observed(attempt)
        if value is None:
            continue
        measured += 1
        observed = value if observed is None else observed + value
    return {
        "id": _identity(action.action_id), "state": action.state,
        "claims": len(attempts), "measured_claims": measured,
        "observed_ms": observed, "incomplete": measured != len(attempts),
    }


def _intervals(record, activity, terminal):
    start = _moment(record.created_at)
    intervals = []
    truncated = False
    for item in activity:
        moment = _moment(item.created_at)
        if moment is None or start is None or moment < start:
            truncated = True
            continue
        intervals.append({"sequence": _integer(item.sequence), "start_at": _time(start),
                          "end_at": _time(moment), "duration_ms": _span(start, moment),
                          "open": False})
        start = moment
    end = _moment(record.updated_at) if terminal else None
    intervals.append({"sequence": None, "start_at": _time(start), "end_at": _time(end),
                      "duration_ms": _span(start, end), "open": not terminal})
    return intervals, truncated


def _measurements(public, record, actions, activity):
    if public["schema_supported"] is not True:
        return {"id": public["id"], "revision": public["revision"],
                "disposition": public["disposition"], "task_count": None, "claim_count": None,
                "measured_claim_count": None, "observed_ms": None, "elapsed_ms": None,
                "tasks": [], "intervals": [], "incomplete": True, "cutoff": True}
    terminal = public["disposition"] in _TERMINAL_DISPOSITIONS
    tasks = [_task(action) for action in actions]
    intervals, dropped = _intervals(record, activity, terminal)
    truncated = (len(tasks) >= MEASUREMENT_PAGE or len(tuple(activity)) >= MEASUREMENT_PAGE
                 or dropped)
    observed = None
    for task in tasks:
        if task["observed_ms"] is not None:
            observed = task["observed_ms"] if observed is None else observed + task["observed_ms"]
    claims = sum(task["claims"] for task in tasks)
    measured = sum(task["measured_claims"] for task in tasks)
    return {
        "id": public["id"], "revision": public["revision"],
        "disposition": public["disposition"], "task_count": len(tasks), "claim_count": claims,
        "measured_claim_count": measured, "observed_ms": observed,
        "elapsed_ms": _span(_moment(record.created_at),
                            _moment(record.updated_at) if terminal else None),
        "tasks": tasks, "intervals": intervals,
        "incomplete": truncated or measured != claims, "cutoff": truncated or not terminal,
    }


class WorkService:
    def __init__(self, assignments):
        self.assignments = assignments
        self.store = assignments.store

    def _owner(self, owner_id, claims):
        self.assignments._owner(owner_id, claims)

    async def assert_read_session(self, owner_id, claims, identity):
        self._owner(owner_id, claims)
        principal_expiry = claims["exp"]

        def read(transaction, _repository):
            sessions = self.store.plane_runtime.repositories.history.sessions
            state = sessions.get_execution_state(
                transaction, owner_id=owner_id, session_id=identity[0])
            if state is None:
                raise AssignmentError("work_authentication_required", 401)
            credential = state.credential
            cap = min(credential.hard_expires_at, principal_expiry)
            if (credential.owner_id != owner_id
                    or (credential.session_id, credential.incarnation_id) != identity
                    or not credential.interactive_anchor <= state.observed_at.timestamp() < cap):
                raise AssignmentError("work_authentication_required", 401)
            return float(cap)

        return await self.store.transaction(read, bound_session_waits=True)

    async def _read(self, method, owner_id, *, include_result=False, include_operation=False, **kwargs):
        def transaction(tx, repository):
            callback = getattr(repository, method, None)
            if not callable(callback):
                raise AssignmentError("work_repository_unavailable", 503)
            value = callback(tx, owner_id=owner_id, **kwargs)
            if method == "get_operation":
                if value is None:
                    raise AssignmentError("work_not_found", 404)
                public = _public(value, owner_id)
                if include_result:
                    from orchestrator.work_result import project_research_result
                    result = {"id": public["id"], "revision": public["revision"],
                            "result": project_research_result(tx, repository,
                                owner_id=owner_id, read=value)}
                    return {"operation": public, "result": result} if include_operation else result
                return public
            return [_public(record, owner_id) for record in value]
        try:
            return await self.store.transaction(transaction, bound_session_waits=include_result)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise AssignmentError("work_read_unavailable", 503) from exc

    async def get(self, owner_id, claims, identity):
        self._owner(owner_id, claims)
        return await self._read("get_operation", owner_id, assignment_id=_identity(identity))

    async def result(self, owner_id, claims, identity):
        self._owner(owner_id, claims)
        return await self._read("get_operation", owner_id, include_result=True,
                                assignment_id=_identity(identity))

    async def result_view(self, owner_id, claims, identity):
        self._owner(owner_id, claims)
        return await self._read("get_operation", owner_id, include_result=True,
                                include_operation=True, assignment_id=_identity(identity))

    async def measurements(self, owner_id, claims, identity):
        self._owner(owner_id, claims)
        assignment_id = _identity(identity)

        def transaction(tx, repository):
            for name in ("get_operation", "list_actions", "list_activity"):
                if not callable(getattr(repository, name, None)):
                    raise AssignmentError("work_repository_unavailable", 503)
            read = repository.get_operation(tx, owner_id=owner_id, assignment_id=assignment_id)
            if read is None:
                raise AssignmentError("work_not_found", 404)
            public = _public(read, owner_id)
            if public["schema_supported"] is not True:
                return _measurements(public, read.assignment, (), ())
            actions = repository.list_actions(tx, owner_id=owner_id,
                assignment_id=assignment_id, limit=MEASUREMENT_PAGE)
            activity = repository.list_activity(tx, owner_id=owner_id,
                assignment_id=assignment_id, limit=MEASUREMENT_PAGE)
            return _measurements(public, read.assignment, actions, activity)

        try:
            return await self.store.transaction(transaction)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise AssignmentError("work_read_unavailable", 503) from exc

    async def _cursor_exists(self, owner_id, after_id):
        def transaction(tx, repository):
            callback = getattr(repository, "get_operation", None)
            if not callable(callback):
                raise AssignmentError("work_repository_unavailable", 503)
            value = callback(tx, owner_id=owner_id, assignment_id=after_id)
            record = getattr(value, "assignment", None)
            return (record is not None and record.owner_id == owner_id
                    and record.execution_profile == "one_shot")
        try:
            return await self.store.transaction(transaction) is True
        except AssignmentError as exc:
            if exc.status_code == 404:
                return False
            raise
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise AssignmentError("work_read_unavailable", 503) from exc

    async def list(self, owner_id, claims, *, limit=50, after_id=None):
        self._owner(owner_id, claims)
        _integer(limit, 100)
        if after_id is not None:
            _identity(after_id)
            if not await self._cursor_exists(owner_id, after_id):
                return {"operations": [], "next_cursor": None, "page_full": False,
                        "resync_required": True}
        records = await self._read("list_operations", owner_id, limit=limit, after_id=after_id)
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
