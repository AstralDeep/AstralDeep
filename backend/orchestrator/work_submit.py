"""Accepts new Work submissions (chat or fixed-research) under
work_submit_authority.py's caller fence, screening chat text through typesafe_routing
before admission. Used by work_admission_api.py, work_operations.py, and
work_publication.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
import re
import unicodedata
from uuid import UUID, uuid4

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


def _selected_ids(value):
    if (type(value) is not dict or set(value) != {"version", "agent", "skills", "notes"}
            or type(value["version"]) is not int or value["version"] != 1):
        _invalid()
    agent = value["agent"]
    if agent is not None:
        if (type(agent) is not dict or set(agent) != {"agent_id", "revision_id"}
                or type(agent["agent_id"]) is not str
                or not 1 <= len(agent["agent_id"]) <= 255
                or agent["agent_id"] != agent["agent_id"].strip()
                or any(unicodedata.category(char) in {"Cc", "Cs"} for char in agent["agent_id"])):
            _invalid()
        _selected_uuid(agent["revision_id"])
    for kind, field_name, maximum in (("skills", "skill_id", 20), ("notes", "note_id", 8)):
        entries = value[kind]
        if type(entries) is not list or len(entries) > maximum:
            _invalid()
        seen = set()
        for entry in entries:
            if (type(entry) is not dict or set(entry) != {field_name, "revision"}
                    or type(entry["revision"]) is not int or not 1 <= entry["revision"] <= 2**53 - 1):
                _invalid()
            identity = _selected_uuid(entry[field_name])
            if identity in seen:
                _invalid()
            seen.add(identity)
    if agent is None and not value["skills"] and not value["notes"]:
        _invalid()


def _selected_uuid(value):
    if type(value) is not str:
        _invalid()
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        _invalid()
    return value


def _parse(raw):
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= 16384:
            _invalid()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _invalid())
        if (not isinstance(value, dict) or not _FIELDS <= set(value)
                or set(value) - _FIELDS - {"source_retention", "selection"}
                or type(value.get("source_retention", "operation")) is not str
                or value.get("source_retention", "operation") not in {"operation", "none"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["caller_key"], str)
                or not 1 <= len(value["caller_key"]) <= 256
                or "\x00" in value["caller_key"]
                or not isinstance(value["deadline_at"], str)
                or _DEADLINE.fullmatch(value["deadline_at"]) is None):
            _invalid()
        if "selection" in value:
            _selected_ids(value["selection"])
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


@dataclass(frozen=True, slots=True, repr=False)
class _SelectedAdmission:
    envelope: object
    boundary: object
    agent_revision: object = None


@dataclass(frozen=True, slots=True)
class FixedResearchPreflight:
    config_store: object = field(repr=False)

    def __post_init__(self):
        from llm_config.user_store import UserLLMConfigStore
        if type(self.config_store) is not UserLLMConfigStore:
            raise TypeError("fixed research requires the USER configuration store")

    async def prepare(self, *, owner_id, runtime, definition, source_bound):
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
        return await self._capture(owner_id=owner_id, runtime=runtime, definition=definition,
                                   minimum=minimum)

    async def prepare_chat(self, *, owner_id, runtime, definition):
        from llm_config import research_profile as profile
        if (definition.source != {} or definition.allowed_tools != ()
                or definition.consented_scopes != () or definition.offline_grant_id is not None):
            raise AssignmentError("work_chat_profile_unavailable", 503)
        minimum = {"model_calls": 1, "tokens": profile.RESERVED_TOKENS,
                   "elapsed_ms": profile.RESERVED_MILLISECONDS}
        return await self._capture(owner_id=owner_id, runtime=runtime, definition=definition,
                                   minimum=minimum)

    async def _capture(self, *, owner_id, runtime, definition, minimum):
        from audit.pii import private_binding_key
        from llm_config import research_profile as profile
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
        from audit.pii import private_binding_key
        try:
            current = private_binding_key(prepared.key.key_id)
            marker = b"fixed-research-admission/v1"
            if not prepared.key.verify("config", marker, current.sign("config", marker)):
                raise ValueError
        except Exception:
            raise AssignmentError("work_research_profile_unavailable", 503) from None

    def assert_current(self, transaction, *, runtime, owner_id, prepared):
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
        if research_preflight is not None and type(research_preflight) is not FixedResearchPreflight:
            raise TypeError("research_preflight must be FixedResearchPreflight")
        if new_admission_check is not None and not callable(new_admission_check):
            raise TypeError("new_admission_check must be a synchronous callable")
        self.assignments, self.audit, self.sessions = assignments, audit, sessions
        self.store = assignments.store
        self.research_preflight = research_preflight
        self.new_admission_check = new_admission_check

    def _check_new_admission(self):
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


    async def _typesafe_screen_chat(self, owner_id: str, instructions: str) -> None:
        try:
            from orchestrator.typesafe_routing import screen_instruction
            from orchestrator.typesafe_routing.security_policy import Verdict

            store = getattr(self.assignments.orch, "_typesafe_store", None)
            if store is None:
                return
            stored = await store.get_key(owner_id)
            if stored is None:
                return
            verdict = await screen_instruction(
                user_id=owner_id,
                text=instructions,
                api_key=stored.api_key,
                fingerprint=stored.fingerprint,
            )
        except AssignmentError:
            raise
        except Exception:
            logging.getLogger("Orchestrator.WorkSubmit").debug(
                "typesafe submission screen unavailable", exc_info=True
            )
            return
        if verdict is Verdict.REFUSE:
            raise AssignmentError("assignment_sensitive_content_refused", 422)

    async def _chat_definition(self, context, authority, body):
        from persistent_agents.privacy import content_text, privacy_text
        if (body["source"] is not None or "selection" in body
                or body.get("source_retention", "operation") != "operation"):
            _invalid()
        try:
            limits = _limits(body["limits"])
            name, instructions = _text(body["name"], 120), _text(body["instructions"], 4096)
            conversation = _text(body["conversation_id"], 128, optional=True)
        except (ValueError, TypeError, AttributeError):
            _invalid()
        assignments = self.assignments
        assignments._owner(context.owner_id, authority.claims)
        try:
            protected = privacy_text(content_text({"name": name, "instructions": instructions}), ())
            contains_phi = await asyncio.to_thread(assignments.phi_gate.contains_phi, protected)
        except ValueError as exc:
            raise AssignmentError("assignment_sensitive_content_refused", 422) from exc
        except Exception as exc:
            raise AssignmentError("assignment_phi_gate_unavailable", 503) from exc
        if contains_phi:
            raise AssignmentError("assignment_sensitive_content_refused", 422)
        await self._typesafe_screen_chat(context.owner_id, instructions)
        if conversation is not None:
            try:
                owned = await asyncio.to_thread(assignments.orch.history.get_chat, conversation,
                                                user_id=context.owner_id)
            except Exception as exc:
                raise AssignmentError("assignment_destination_unavailable", 503) from exc
            if owned is None:
                raise AssignmentError("assignment_destination_not_found", 404)
        return AssignmentDefinition(name=name, instructions=instructions, source={},
            allowed_tools=(), consented_scopes=(), offline_grant_id=None, limits=limits,
            conversation_id=conversation)

    async def _definition(self, context, authority, body):
        if body["kind"] == "chat":
            return await self._chat_definition(context, authority, body)
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

    def _selection_current(self, context, prepared, selected):
        self._current(context)
        orch, runtime = self.assignments.orch, self.store.plane_runtime
        plane = getattr(getattr(orch, "runtime_composition", None), "plane", None)
        if (self.research_preflight is None or type(prepared) is not _ResearchAdmission
                or type(selected) is not _SelectedAdmission
                or getattr(plane, "runtime", None) is not runtime
                or getattr(plane, "repositories", None) is not runtime.repositories
                or getattr(orch, "persistent_assignments", None) is not self.assignments
                or getattr(orch, "web_sessions", None) is not self.sessions
                or getattr(orch, "audit_repo", None) is not self.audit
                or getattr(orch, "_llm_store", None) is not self.research_preflight.config_store):
            raise AssignmentError("work_selection_unavailable", 503)
        self.research_preflight.assert_key(prepared)
        selected.boundary.assert_current()

    def _agent_selection_policy(self, revision, definition):
        from llm_config import research_profile
        from orchestrator.agent_authoring import byo_enabled
        from orchestrator.projection_surfaces.authoring import DeclarativeAgentService
        parsed = DeclarativeAgentService._stored_definition(revision)
        limits = parsed.to_dict()["limits"]
        if not byo_enabled() or not parsed.fixed_research_shape:
            raise AssignmentError("work_selected_agent_unavailable", 503)
        if (any(definition.limits[name] > limits[period][name]
                for period in ("daily", "lifetime")
                for name in ("model_calls", "tool_calls", "tokens", "elapsed_ms"))
                or definition.limits["max_retries"] > limits["max_retries"]
                or limits["step_timeout_ms"] < max(research_profile.RESERVED_MILLISECONDS,
                    self.assignments.tool_bound("web-research-1:fetch_page")["elapsed_ms"])
                or any(limits[period]["spend_micro_units"] is not None for period in ("daily", "lifetime"))):
            raise AssignmentError("work_selected_agent_budget_refused", 422)

    def _read_selection(self, tx, context, selection, definition, prepared, selected, now):
        from astralplane.repositories.guidance_models import GuidanceReference
        reference = selection["agent"]
        references = tuple(GuidanceReference(kind, ref[field_name], ref["revision"])
            for kind, field_name, key in (("skill", "skill_id", "skills"), ("note", "note_id", "notes"))
            for ref in selection[key])
        result = selected.boundary.capture(tx, owner_id=context.owner_id,
            instruction=definition.instructions,
            agent=None if reference is None else (reference["agent_id"], reference["revision_id"]),
            references=references, binding_key=prepared.key, now=now)
        if result.agent_revision is not None:
            self._agent_selection_policy(result.agent_revision, definition)
        return result

    async def _prepare_selection(self, context, observation, selection, definition, prepared):
        from personalization.selected_guidance_boundary import SelectedGuidanceBoundary
        selected = _SelectedAdmission(None, SelectedGuidanceBoundary(self.assignments.orch,
            plane_runtime=self.store.plane_runtime, include_notes=bool(selection["notes"])))
        self._selection_current(context, prepared, selected)

        def capture(tx, repository):
            self._selection_current(context, prepared, selected)
            repos = self.store.plane_runtime.repositories
            current = repos.history.sessions.assert_current_execution(tx, observation=observation)
            self._current(context, now=current.observed_at)
            repos.preferences.skills.lock_owner(tx, owner_id=context.owner_id)
            result = self._read_selection(tx, context, selection, definition, prepared, selected, current.observed_at)
            self._selection_current(context, prepared, selected)
            return result

        expanded = await self.store.transaction(capture, bound_session_waits=True)
        self._selection_current(context, prepared, selected)
        from persistent_agents.privacy import privacy_text
        try:
            sensitive = await asyncio.to_thread(self.assignments.phi_gate.contains_phi, privacy_text(expanded.prepared.text))
        except ValueError:
            raise AssignmentError("assignment_sensitive_content_refused", 422) from None
        except Exception:
            raise AssignmentError("assignment_phi_gate_unavailable", 503) from None
        if sensitive:
            raise AssignmentError("assignment_sensitive_content_refused", 422)
        self._selection_current(context, prepared, selected)
        return replace(selected, envelope=expanded.prepared.envelope, agent_revision=expanded.agent_revision)

    def _bind_selection(self, tx, repository, record, context, selection, definition, prepared, selected, observation):
        from astralplane.repositories.selected_input_models import SelectedInputEnvelope, SelectedAgentReference
        self._selection_current(context, prepared, selected)
        expected = selected.envelope
        envelope = SelectedInputEnvelope(expected.references,
            None if expected.agent is None else SelectedAgentReference(**asdict(expected.agent)),
            expected.binding_key_id, expected.combined_binding,
            expected.expansion_version, expected.version)
        record = repository.bind_selected_input(tx, owner_id=context.owner_id,
            assignment_id=record.assignment_id, expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch, expected_state_version=record.state_version,
            envelope=envelope)
        now = self.store.plane_runtime.repositories.history.sessions.assert_current_execution(
            tx, observation=observation).observed_at
        actual = self._read_selection(tx, context, selection, definition, prepared, selected, now)
        if asdict(actual.prepared.envelope) != asdict(expected):
            raise AssignmentError("work_selection_changed", 409)
        snapshot = repository.get_selected_input(tx, owner_id=context.owner_id, assignment_id=record.assignment_id)
        return record, snapshot

    async def submit(self, context: AuthenticatedWorkRequest, raw_body: bytes) -> WorkSubmissionResult:
        body, deadline, signature = _parse(raw_body)
        accepted = await self._accepted(context, body, signature)
        if accepted is not None:
            return accepted
        try:
            self._check_new_admission()
            kind = body["kind"]
            if kind not in {"research", "chat"}:
                _invalid()
            preflight = self.research_preflight
            if kind == "chat" and preflight is None:
                raise AssignmentError("work_chat_profile_unavailable", 503)
            authority = await refresh_work_submission_authority(context, sessions=self.sessions)
            definition = await self._definition(context, authority, body)
            if kind == "chat":
                prepared = await preflight.prepare_chat(owner_id=context.owner_id,
                    runtime=self.store.plane_runtime, definition=definition)
            else:
                prepared = None if preflight is None else await preflight.prepare(
                    owner_id=context.owner_id, runtime=self.store.plane_runtime, definition=definition,
                    source_bound=self.assignments.tool_bound("web-research-1:fetch_page"))
            state = authority.observation
            hard_expiry = datetime.fromtimestamp(state.credential.hard_expires_at, timezone.utc)
            if not state.started_at < deadline <= min(hard_expiry, state.started_at + timedelta(days=1)):
                _invalid()
            observation = replace(state, valid_until=min(state.valid_until, deadline))
            selected = (None if "selection" not in body else await self._prepare_selection(
                context, observation, body["selection"], definition, prepared))
            operation = AssignmentOperationSpec(kind, AssignmentOperationAuthority(
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
                replay = self._receipt(repository, transaction, context, body, signature)
                if replay is not None:
                    self._current(context)
                    return WorkSubmissionResult(replay, False)
                self._check_new_admission()
                if preflight is not None and selected is None:
                    preflight.assert_current(transaction, runtime=self.store.plane_runtime,
                                             owner_id=context.owner_id, prepared=prepared)
                    if kind == "research":
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
                selected_snapshot = None
                if selected is not None:
                    record, selected_snapshot = self._bind_selection(transaction, repository,
                        record, context, body["selection"], definition, prepared, selected, observation)
                    preflight.assert_current(transaction, runtime=self.store.plane_runtime,
                                             owner_id=context.owner_id, prepared=prepared)
                    preflight.assert_policy(transaction, runtime=self.store.plane_runtime,
                        orchestrator=self.assignments.orch, owner_id=context.owner_id,
                        claims=authority.claims)
                inputs_meta = {"operation_id": identity, "kind": kind, "version": 2}
                if selected is not None:
                    inputs_meta["selection"] = {"version": 1, "skills": len(body["selection"]["skills"]),
                        "notes": len(body["selection"]["notes"]),
                        "agent_selected": body["selection"]["agent"] is not None}
                event = AuditEventCreate(actor_user_id=context.owner_id, auth_principal=context.owner_id,
                    event_class="conversation", action_type="work.accept", description="Work accepted",
                    conversation_id=definition.conversation_id, correlation_id=identity, outcome="success",
                    inputs_meta=inputs_meta,
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
                if selected is not None:
                    self._selection_current(context, prepared, selected)
                    if selected.agent_revision is not None:
                        self._agent_selection_policy(selected.agent_revision, definition)
                    record = repository.assert_selected_input_current(transaction,
                        owner_id=context.owner_id, assignment_id=identity,
                        expected_instruction_revision=record.instruction_revision,
                        expected_control_epoch=record.control_epoch,
                        expected_state_version=record.state_version, expected=selected_snapshot,
                        authority_valid_until=observation.valid_until)
                    self._selection_current(context, prepared, selected)
                    if selected.agent_revision is not None:
                        self._agent_selection_policy(selected.agent_revision, definition)
                    self._check_new_admission()
                return WorkSubmissionResult(record, True)

            result = await self.store.transaction(accept, bound_session_waits=True)
            self._current(context)
            return result
        except Exception:
            accepted = await self._accepted(context, body, signature)
            if accepted is not None:
                return accepted
            raise
