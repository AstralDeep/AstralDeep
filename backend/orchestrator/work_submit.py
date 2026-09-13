"""Unregistered interactive public-reader acceptance; no execution or ingress.

Receipt identity is the original owner/namespace/key and immutable command, not
today's source policy or refreshed credentials. New work commits with its audit.
The optional fixed research preflight additionally qualifies model availability
and whole-episode ceilings; omitting it preserves the source-only acceptance API.
"""
from __future__ import annotations

from collections.abc import Mapping
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
        if (not isinstance(value, dict) or set(value) not in (_FIELDS, _FIELDS | {"source_retention"})
                or type(value.get("source_retention", "operation")) is not str
                or value.get("source_retention", "operation") not in {"operation", "none"}
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


@dataclass(frozen=True, slots=True)
class _ResearchAdmission:
    selection: object = field(repr=False)
    key: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class FixedResearchPreflight:
    """Opt-in admission availability check, never a reusable execution permit.

    Keep the exact USER row locked through acceptance and audit. The model path
    independently captures and guards its own configuration/input at dispatch;
    no selection, provider key or prompt is added to the operation or receipt.
    """

    config_store: object = field(repr=False)

    def __post_init__(self):
        from llm_config.user_store import UserLLMConfigStore
        if type(self.config_store) is not UserLLMConfigStore:
            raise TypeError("fixed research requires the USER configuration store")

    async def prepare(self, *, owner_id, runtime, definition, source_bound):
        """Check complete declared ceilings and capture uncached private config."""
        from audit.pii import private_binding_key
        from llm_config import research_profile as profile
        source = definition.source
        if (not isinstance(source, Mapping) or source.get("profile") != "public_page"
                or source.get("agent_id") != "web-research-1" or source.get("tool_name") != "fetch_page"
                or source.get("linked_document_urls")
                or definition.allowed_tools != ("web-research-1:fetch_page",)
                or definition.consented_scopes != ("tools:read",)):
            raise AssignmentError("work_research_profile_unavailable", 503)
        minimum = {**source_bound}
        minimum["model_calls"] += 1
        minimum["tokens"] += profile.RESERVED_TOKENS
        minimum["elapsed_ms"] += profile.RESERVED_MILLISECONDS
        if any(definition.limits[name] < amount for name, amount in minimum.items()):
            raise AssignmentError("work_research_budget_insufficient", 422)
        try:
            if self.config_store._repository.plane_runtime is not runtime:
                raise ValueError
            key = private_binding_key()
            selection = profile.select_config(
                await self.config_store.capture_user(owner_id),
                store=self.config_store, binding_key=key,
            )
            return _ResearchAdmission(selection, key)
        except Exception:
            raise AssignmentError("work_research_profile_unavailable", 503) from None

    @staticmethod
    def assert_key(prepared):
        """Require the original named key to remain resolvable and unchanged."""
        from audit.pii import private_binding_key
        try:
            current = private_binding_key(prepared.key.key_id)
            marker = b"fixed-research-admission/v1"
            if not prepared.key.verify("config", marker, current.sign("config", marker)):
                raise ValueError
        except Exception:
            raise AssignmentError("work_research_profile_unavailable", 503) from None

    def assert_current(self, transaction, *, runtime, owner_id, prepared):
        """Lock the captured USER row after owner/session and before new insert."""
        try:
            if (type(prepared) is not _ResearchAdmission
                    or self.config_store._repository.plane_runtime is not runtime
                    or prepared.selection.owner_id != owner_id):
                raise ValueError
            row = runtime.repositories.encrypted_llm_config.get_user_for_update(
                transaction, owner_id=owner_id)
            if not prepared.selection.matches(row):
                raise ValueError
            self.assert_key(prepared)
        except Exception:
            raise AssignmentError("work_research_profile_unavailable", 503) from None

    @staticmethod
    def assert_policy(transaction, *, runtime, orchestrator, owner_id, claims):
        """Take the fixed policy fence last; later work cannot write policy rows."""
        from orchestrator.tool_permissions import FixedReaderPolicyError
        try:
            orchestrator.tool_permissions.assert_fixed_reader_current(transaction,
                owner_id=owner_id, plane_runtime=runtime,
                orchestrator=orchestrator, identity_claims=claims)
        except FixedReaderPolicyError as error:
            raise AssignmentError(error.code,
                403 if error.code == "assignment_scope_revoked" else 503) from None


class WorkSubmitService:
    def __init__(self, assignments, audit, sessions, *, research_preflight=None, new_admission_check=None):
        """Keep source-only acceptance unless this exact capability is supplied."""
        if research_preflight is not None and type(research_preflight) is not FixedResearchPreflight:
            raise TypeError("research_preflight must be FixedResearchPreflight")
        if new_admission_check is not None and not callable(new_admission_check):
            raise TypeError("new_admission_check must be a synchronous callable")
        self.assignments, self.audit, self.sessions = assignments, audit, sessions
        self.store = assignments.store
        self.research_preflight = research_preflight
        self.new_admission_check = new_admission_check

    def _check_new_admission(self):
        """Run an optional server-only capability check, never on receipt replay."""
        if self.new_admission_check is not None:
            if _sync(self.new_admission_check()) is not None:
                raise AssignmentError("work_submit_unavailable", 503)

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
            self._check_new_admission()
            authority = await refresh_work_submission_authority(context, sessions=self.sessions)
            definition = await self._definition(context, authority, body)
            preflight = self.research_preflight
            prepared = None if preflight is None else await preflight.prepare(
                owner_id=context.owner_id, runtime=self.store.plane_runtime, definition=definition,
                source_bound=self.assignments.tool_bound("web-research-1:fetch_page"))
            state = authority.observation
            hard_expiry = datetime.fromtimestamp(state.credential.hard_expires_at, timezone.utc)
            if not state.started_at < deadline <= min(hard_expiry, state.started_at + timedelta(days=1)):
                _invalid()
            observation = replace(state, valid_until=min(state.valid_until, deadline))
            operation = AssignmentOperationSpec("research", AssignmentOperationAuthority(
                context.owner_id, "interactive", "session_incarnation", state.credential.incarnation_id,
                deadline), deadline, body.get("source_retention", "operation"))
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
                self._check_new_admission()
                if preflight is not None:
                    preflight.assert_current(transaction, runtime=self.store.plane_runtime,
                                             owner_id=context.owner_id, prepared=prepared)
                    preflight.assert_policy(transaction, runtime=self.store.plane_runtime,
                        orchestrator=self.assignments.orch, owner_id=context.owner_id,
                        claims=authority.claims)
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
                if preflight is not None:
                    preflight.assert_key(prepared)
                self._check_new_admission()
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
