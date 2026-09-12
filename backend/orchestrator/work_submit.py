"""Unregistered interactive public-reader acceptance; no execution or ingress.

Receipt identity is the original owner/namespace/key and immutable command, not
today's source policy or refreshed credentials. New work commits with its audit.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import re
from uuid import uuid4

from astralplane.repositories.assignment_models import (
    AssignmentDefinition, AssignmentOperationAuthority, AssignmentOperationSpec, AssignmentRecord,
)
from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator.work_submit_authority import (
    AuthenticatedWorkRequest, refresh_work_submission_authority,
)
from persistent_agents.models import AssignmentError, SourceSelection, ToolReference, digest

_NAMESPACE = "interactive.work.v1"
_FIELDS = {"version", "caller_key", "kind", "name", "instructions", "source", "limits",
           "deadline_at", "conversation_id"}
_DEADLINE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")


def _invalid():
    raise AssignmentError("work_submit_invalid", 422)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _parse(raw):
    """Bound structure/digest only; never reapply mutable source/limit policy here."""
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= 16384:
            _invalid()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _invalid())
        if (not isinstance(value, dict) or set(value) != _FIELDS
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["caller_key"], str)
                or not 1 <= len(value["caller_key"]) <= 256
                or "\x00" in value["caller_key"]
                or not isinstance(value["deadline_at"], str)
                or _DEADLINE.fullmatch(value["deadline_at"]) is None):
            _invalid()
        deadline = datetime.fromisoformat(value["deadline_at"].replace("Z", "+00:00"))
        return value, deadline, digest({"command": "work.submit", "body": value})
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _invalid()


def _text(value, maximum, *, optional=False):
    if optional and value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value):
        _invalid()
    return value


def _limits(value):
    bounds = {"model_calls": (1, 100000), "tool_calls": (1, 1000000),
              "tokens": (1, 100000000), "elapsed_ms": (30000, 86400000), "max_retries": (0, 3)}
    if (not isinstance(value, dict) or set(value) != set(bounds)
            or any(type(value[key]) is not int or not minimum <= value[key] <= maximum
                   for key, (minimum, maximum) in bounds.items())):
        _invalid()
    # Budgets never grant model or fanout capability; this adapter admits one reader only.
    return {**value, "max_depth": 0, "max_concurrent_tasks": 1, "max_tasks": 1}


def _sync(value):
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise AssignmentError("work_repository_unavailable", 503)
    return value


@dataclass(frozen=True, slots=True)
class WorkSubmissionResult:
    record: AssignmentRecord = field(repr=False)
    created: bool


class WorkSubmitService:
    def __init__(self, assignments, audit, sessions):
        self.assignments, self.audit, self.sessions = assignments, audit, sessions
        self.store = assignments.store

    def _current(self, context, *, now=None):
        if type(context) is not AuthenticatedWorkRequest:
            raise AssignmentError("work_authority_unavailable", 403)
        context.assert_current(self.store.plane_runtime, now=now)
        self.assignments._owner(context.owner_id, context.claims)

    @staticmethod
    def _receipt(repository, transaction, context, body, signature):
        method = getattr(repository, "get_operation_receipt", None)
        if not callable(method):
            raise AssignmentError("work_repository_unavailable", 503)
        record = _sync(method(transaction, owner_id=context.owner_id, origin_namespace=_NAMESPACE,
            caller_key=body["caller_key"], command_digest=signature, credential_id=None))
        if record is not None and (not isinstance(record, AssignmentRecord)
                or record.owner_id != context.owner_id or record.execution_profile != "one_shot"):
            raise AssignmentError("work_repository_unavailable", 503)
        return record

    async def _accepted(self, context, body, signature):
        self._current(context)
        record = await self.store.transaction(
            lambda tx, repo: self._receipt(repo, tx, context, body, signature), bound_session_waits=True)
        self._current(context)
        return WorkSubmissionResult(record, False) if record is not None else None

    async def _definition(self, context, authority, body):
        if body["kind"] != "research" or not isinstance(body["source"], dict) or set(body["source"]) != {"url"}:
            _invalid()
        try:
            source = SourceSelection(agent_id="web-research-1", tool_name="fetch_page",
                                     arguments=body["source"])
            limits = _limits(body["limits"])
            name, instructions = _text(body["name"], 120), _text(body["instructions"], 4096)
            conversation = _text(body["conversation_id"], 128, optional=True)
        except (ValueError, TypeError, AttributeError):
            _invalid()
        tools = [ToolReference(agent_id=source.agent_id, tool_name=source.tool_name)]
        scopes = await self.assignments._definition_policy(context.owner_id, authority.claims,
            name=name, instructions=instructions, source=source, allowed_tools=tools,
            completion_condition=None, conversation_id=conversation)
        if self.assignments.tool_bound(source.identity)["elapsed_ms"] > limits["elapsed_ms"]:
            _invalid()
        return AssignmentDefinition(name=name, instructions=instructions, source=source.model_dump(),
            allowed_tools=(source.identity,), consented_scopes=tuple(sorted(set(scopes.values()))),
            offline_grant_id=None, limits=limits, conversation_id=conversation)

    async def submit(self, context: AuthenticatedWorkRequest, raw_body: bytes) -> WorkSubmissionResult:
        body, deadline, signature = _parse(raw_body)
        accepted = await self._accepted(context, body, signature)
        if accepted is not None:
            return accepted
        try:
            authority = await refresh_work_submission_authority(context, sessions=self.sessions)
            definition = await self._definition(context, authority, body)
            state = authority.observation
            hard_expiry = datetime.fromtimestamp(state.credential.hard_expires_at, timezone.utc)
            if not state.started_at < deadline <= min(hard_expiry, state.started_at + timedelta(days=1)):
                _invalid()
            observation = replace(state, valid_until=min(state.valid_until, deadline))
            operation = AssignmentOperationSpec("research", AssignmentOperationAuthority(
                context.owner_id, "interactive", "session_incarnation", state.credential.incarnation_id,
                deadline), deadline, "operation")
            identity = str(uuid4())

            def accept(transaction, repository):
                self._current(context)
                replay = self._receipt(repository, transaction, context, body, signature)
                if replay is not None:
                    self._current(context)
                    return WorkSubmissionResult(replay, False)
                sessions = self.store.plane_runtime.repositories.history.sessions
                current = _sync(sessions.assert_current_execution(transaction, observation=observation))
                self._current(context, now=current.observed_at)
                # The owner lock now excludes a concurrent create between this read and insert.
                replay = self._receipt(repository, transaction, context, body, signature)
                if replay is not None:
                    self._current(context)
                    return WorkSubmissionResult(replay, False)
                create = getattr(repository, "create_operation", None)
                append = getattr(self.audit, "insert_in_transaction", None)
                if not callable(create) or not callable(append):
                    raise AssignmentError("work_repository_unavailable", 503)
                record = _sync(create(transaction, owner_id=context.owner_id, assignment_id=identity,
                    origin_namespace=_NAMESPACE, caller_key=body["caller_key"], command_digest=signature,
                    definition=definition, operation=operation, authority=observation))
                if (not isinstance(record, AssignmentRecord) or record.assignment_id != identity
                        or record.owner_id != context.owner_id or record.execution_profile != "one_shot"):
                    raise AssignmentError("work_repository_unavailable", 503)
                event = AuditEventCreate(actor_user_id=context.owner_id, auth_principal=context.owner_id,
                    event_class="conversation", action_type="work.accept", description="Work accepted",
                    conversation_id=definition.conversation_id, correlation_id=identity, outcome="success",
                    inputs_meta={"operation_id": identity, "kind": "research", "version": 2},
                    outputs_meta={}, started_at=current.observed_at, completed_at=current.observed_at)
                audited = _sync(append(event, transaction=transaction, plane_runtime=self.store.plane_runtime))
                if not isinstance(audited, AuditEventDTO):
                    raise AssignmentError("work_repository_unavailable", 503)
                current = _sync(sessions.assert_current_execution(transaction, observation=observation))
                self._current(context, now=current.observed_at)
                if current.observed_at >= deadline:
                    raise AssignmentError("work_authority_unavailable", 403)
                return WorkSubmissionResult(record, True)

            result = await self.store.transaction(accept, bound_session_waits=True)
            self._current(context)
            return result
        except Exception:
            # A concurrent accepted receipt or lost commit acknowledgement is the
            # only proof of success. Never retry refresh, compensate or create again.
            accepted = await self._accepted(context, body, signature)
            if accepted is not None:
                return accepted
            raise
