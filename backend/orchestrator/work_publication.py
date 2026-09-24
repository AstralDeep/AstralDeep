"""Owner review-and-save of a completed research result into a canvas: propose records a
reviewed digest, save consumes it once. Rebuilds the result via work_result.py inside
one Plane transaction. Used by work_api.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from astralplane.repositories.assignment_models import (
    AssignmentActionDecision, AssignmentActionRecord, AssignmentRecord,
)
from astralplane.repositories.assignments import digest as plane_digest
from astralplane.repositories.history import SessionExecutionObservation
from astralplane.repositories.result_publication_models import (
    ResultPublicationContent, ResultPublicationPreparation, ResultPublicationProposal,
    ResultPublicationReceipt,
)
from astralplane.repositories.result_publications import result_publication_stage_digest
from astralplane.repositories.workspaces import PublicationRebaseComponent, PublicationRebaseLayout
from pydantic import Field, field_validator

from orchestrator import session_authority
from orchestrator.session_authority import SessionAuthorityUnavailable, _same_operation
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_continuations import _prepared, _request
from orchestrator.work_controls import WorkControlRequest, WorkControlService, _method, _owned_operation
from orchestrator.work_result import project_research_result
from orchestrator.work_service import _identity
from orchestrator.work_submit import _sync
from persistent_agents.models import AssignmentError, SourceSelection, validate_id
from persistent_agents.runtime_values import digest, thaw

KIND = "result_publication"
PROPOSAL_SECONDS = 900
COMPONENT_TYPE = "card"
_PROPOSAL_FIELDS = ("action_id", "result_action_id", "result_digest", "publication_id",
                    "conversation_id", "component_id", "base_render_revision",
                    "base_publication_id", "content_digest", "stage_digest",
                    "permission_digest", "precondition_digest")


class WorkResultProposalRequest(WorkControlRequest):
    version: int = Field(ge=1, le=1)
    publication_id: str = Field(min_length=36, max_length=36)
    conversation_id: str = Field(min_length=1, max_length=512)
    expected_workspace_revision: int = Field(ge=0, le=2**53 - 1)
    expected_workspace_publication_id: str | None = Field(default=None, min_length=36, max_length=36)

    @field_validator("publication_id")
    @classmethod
    def canonical_publication(cls, value):
        return validate_id(value)

    @field_validator("expected_workspace_publication_id")
    @classmethod
    def canonical_head(cls, value):
        return None if value is None else validate_id(value)

    @field_validator("conversation_id")
    @classmethod
    def bounded_destination(cls, value):
        if value != value.strip() or not value.isprintable():
            raise ValueError("invalid destination")
        return value


class WorkResultSaveRequest(WorkControlRequest):
    version: int = Field(ge=1, le=1)
    proposal_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


def _unauthenticated():
    raise AssignmentError("work_authentication_required", 401)


def _conflict(code="assignment_publication_conflict", status=409):
    raise AssignmentError(code, status)


def component_id(assignment_id):
    return "au_work_result_" + _identity(assignment_id)


def result_component(record, content):
    from astralprims import Card, KeyValue, Text

    source = content["source"]
    children = [Text(content=passage["text"]) for passage in content["passages"]]
    if not children:
        children.append(Text(content="Insufficient evidence. No passages were selected "
                                     "from this source.", variant="caption"))
    children.append(Text(content="Selected source text, not a summary of the full visual page.",
                         variant="caption"))
    children.append(KeyValue(title=source["title"] or "Source", items=[
        {"label": "Requested URL", "value": source["requested_url"]},
        {"label": "Retrieved URL", "value": source["final_url"]},
        {"label": "Retrieved at", "value": source["retrieved_at"]},
    ]))
    return thaw(Card(title=record.definition.name, content=children).to_dict())


def _row_id(publication_id, identity):
    return str(uuid5(UUID(publication_id), identity))


def rebase(destination, *, publication_id, identity, payload, title):
    ordered = sorted(destination.components, key=lambda item: (item.position, item.component_id))
    components, replaced = [], False
    for entry in ordered:
        position = len(components)
        if entry.component_id == identity:
            components.append(PublicationRebaseComponent(_row_id(publication_id, identity), identity,
                                                         payload, COMPONENT_TYPE, title, position))
            replaced = True
        else:
            components.append(PublicationRebaseComponent(_row_id(publication_id, entry.component_id),
                entry.component_id, thaw(entry.payload), entry.component_type, entry.title, position))
    if not replaced:
        components.append(PublicationRebaseComponent(_row_id(publication_id, identity), identity,
                                                     payload, COMPONENT_TYPE, title, len(components)))
    layouts = tuple(PublicationRebaseLayout(entry.layout_key, position, thaw(entry.tree))
                    for position, entry in enumerate(sorted(destination.layouts,
                        key=lambda item: (item.position, item.layout_key))))
    return ResultPublicationContent(tuple(components), layouts)


def permission_digest(record, scopes):
    return digest({"kind": KIND, "tools": scopes, "instruction_revision": record.instruction_revision,
                   "control_epoch": record.control_epoch,
                   "operation_authority": thaw(record.operation["authority"])})


def precondition_digest(record, *, result_action_id, result_digest, conversation_id,
                        base_render_revision, base_publication_id):
    return digest({"kind": KIND, "source": thaw(record.definition.source),
                   "result_action_id": result_action_id, "result_digest": result_digest,
                   "conversation_id": conversation_id, "base_render_revision": base_render_revision,
                   "base_publication_id": base_publication_id})


def _completed_context(value, owner_id, assignment_id):
    selected = session_authority._operation_reference(value, owner_id, assignment_id)
    record = selected[0]
    if (record.lifecycle != "completed" or value.disposition != "completed"
            or value.terminal_outcome != "completed"
            or record.operation.get("terminal_outcome") != "completed"
            or record.operation.get("source_retention") != "operation"):
        session_authority._unavailable()
    return selected


def _stored_proposal(action):
    request = thaw(action.intent.request)
    if (type(request) is not dict or request.get("kind") != KIND or request.get("version") != 1
            or type(request.get("proposal")) is not dict):
        return None
    values = dict(request["proposal"])
    try:
        values["expires_at"] = datetime.fromisoformat(values["expires_at"])
        proposal = ResultPublicationProposal(**values)
    except (KeyError, TypeError, ValueError):
        return None
    return proposal if proposal.action_id == action.action_id else None


def _receipt(value, owner, identity, action_id):
    if (type(value) is not ResultPublicationReceipt or value.owner_id != owner
            or value.assignment_id != identity or value.action_id != action_id):
        raise AssignmentError("work_control_unavailable", 503)
    return value


class WorkPublicationService(WorkControlService):
    def _owner(self, caller):
        if type(caller) is not WorkCallerAuthority:
            _unauthenticated()
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        return owner

    async def _original(self, caller, record):
        consent = caller.require_session()
        binding = caller._binding
        reference = record.operation["authority"]["reference_id"]
        same = reference == consent.credential.incarnation_id
        expected = consent.credential if same else None
        runtime = binding.runtime
        try:
            result = await session_authority._refresh_operation_session(
                owner_id=record.owner_id, assignment_id=record.assignment_id,
                sessions=binding.sessions, plane_runtime=runtime,
                operation_context=_completed_context, expected_record=record,
                request_expires_at=caller.context.principal_expires_at,
                request_check=lambda now: caller.context.assert_current(runtime, now=now),
                expected_session_credential=expected)
        except SessionAuthorityUnavailable:
            raise AssignmentError("work_authority_unavailable", 403) from None
        observation = result.observation
        if (type(observation) is not SessionExecutionObservation
                or observation.credential.owner_id != record.owner_id
                or observation.credential.incarnation_id != reference):
            _unauthenticated()
        if same:
            if observation.credential.session_id != consent.credential.session_id:
                _unauthenticated()
            consent = replace(consent, credential=observation.credential,
                              valid_until=min(consent.valid_until, observation.valid_until))
        cutoff = min(consent.valid_until, observation.valid_until)
        return replace(caller, caller=consent), observation, cutoff

    async def _scopes(self, owner, claims, record):
        source = SourceSelection.model_validate(thaw(record.definition.source))
        return await asyncio.to_thread(self.assignments._live_tools, owner, claims,
                                       record.definition.allowed_tools, source)

    def _sessions(self):
        return self.store.plane_runtime.repositories.history.sessions

    @staticmethod
    def _selected_current(tx, repository, owner, read, action):
        record = read.assignment
        selected = _method(repository, "get_selected_input")(
            tx, owner_id=owner, assignment_id=record.assignment_id)
        if plane_digest(selected) != thaw(action.intent.request)["selected_digest"]:
            _conflict("assignment_guidance_changed")
        _method(repository, "assert_selected_input_current")(
            tx, owner_id=owner, assignment_id=record.assignment_id,
            expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch,
            expected_state_version=record.state_version, expected=selected)
        return selected

    def _content(self, tx, repository, owner, read, *, conversation_id, base_render_revision,
                 base_publication_id, publication_id):
        result = project_research_result(tx, repository, owner_id=owner, read=read)
        if result["available"] is not True:
            raise AssignmentError("work_result_unavailable", 409)
        model = _method(repository, "get_action")(tx, owner_id=owner,
            assignment_id=read.assignment.assignment_id, action_id=read.result_reference)
        if type(model) is not AssignmentActionRecord or model.action_id != read.result_reference:
            raise AssignmentError("work_result_unavailable", 409)
        result_digest = thaw(model.result)["result_digest"]
        destination = _method(repository, "read_result_publication_destination")(
            tx, owner_id=owner, conversation_id=conversation_id,
            expected_render_revision=base_render_revision, expected_publication_id=base_publication_id)
        payload = result_component(read.assignment, result["content"])
        identity = component_id(read.assignment.assignment_id)
        content = rebase(destination, publication_id=publication_id, identity=identity,
                         payload=payload, title=read.assignment.definition.name)
        reviewed = next(entry for entry in content.components if entry.component_id == identity)
        return content, reviewed, result_digest

    @staticmethod
    def _review(action, proposal, reviewed, updated, *, created):
        return {
            "version": 1, "status": "review_required", "created": created,
            "revision": updated.assignment.state_version,
            "proposal": {
                "action_id": action.action_id, "proposal_digest": action.intent.request_digest,
                "publication_id": proposal.publication_id, "conversation_id": proposal.conversation_id,
                "component_id": proposal.component_id,
                "base_render_revision": proposal.base_render_revision,
                "base_publication_id": proposal.base_publication_id,
                "content_digest": proposal.content_digest, "stage_digest": proposal.stage_digest,
                "expires_at": proposal.expires_at.astimezone(UTC).isoformat(),
            },
            "component": {
                "component_id": reviewed.component_id, "component_type": reviewed.component_type,
                "title": reviewed.title, "position": reviewed.position, "payload": reviewed.payload,
            },
        }

    @staticmethod
    def _saved(receipt, updated, *, applied):
        return {
            "version": 1, "status": "saved", "applied": applied, "action_id": receipt.action_id,
            "publication_id": receipt.publication_id, "conversation_id": receipt.conversation_id,
            "component_id": receipt.component_id,
            "render_revision": receipt.committed_render_revision,
            "revision": updated.assignment.state_version,
        }

    async def propose(self, identity, body: WorkResultProposalRequest, *, caller):
        owner = self._owner(caller)
        body = _request(body, WorkResultProposalRequest)
        identity = _identity(identity)
        if (body.expected_workspace_publication_id is None) != (body.expected_workspace_revision == 0):
            raise AssignmentError("work_control_invalid", 422)

        def select(tx, repository):
            read = _owned_operation(tx, repository, owner, identity)
            existing = _method(repository, "get_action")(tx, owner_id=owner, assignment_id=identity,
                                                         action_id=body.submission_id)
            if existing is not None:
                if _stored_proposal(existing) is None or existing.state != "proposed":
                    _conflict("assignment_idempotency_conflict")
                return read.assignment, existing
            if read.assignment.state_version != body.expected_revision:
                _conflict("assignment_revision_conflict")
            if read.terminal_outcome != "completed" or read.disposition != "completed":
                _conflict("assignment_not_terminal")
            return read.assignment, None

        record, existing = await self._transaction(select, caller)
        authority = cutoff = scopes = None
        if existing is None:
            caller, authority, cutoff = await self._original(caller, record)
            scopes = await self._scopes(owner, caller.context.claims, record)
        expires_at = datetime.now(UTC) + timedelta(seconds=PROPOSAL_SECONDS)

        def propose(tx, repository):
            sessions = self._sessions()
            if authority is not None:
                sessions.assert_current_execution(tx, observation=authority)
            read = _owned_operation(tx, repository, owner, identity)
            if not _same_operation(read.assignment, record):
                _conflict("assignment_revision_conflict")
            chat = self.store.plane_runtime.repositories.history.conversations.get(
                tx, owner_id=owner, conversation_id=body.conversation_id)
            if chat is None or chat.owner_id != owner:
                raise AssignmentError("work_not_found", 404)
            if ((chat.render_revision, chat.publication_id)
                    != (body.expected_workspace_revision, body.expected_workspace_publication_id)):
                _conflict()
            selected = _method(repository, "get_selected_input")(tx, owner_id=owner, assignment_id=identity)
            action = _method(repository, "get_action")(tx, owner_id=owner, assignment_id=identity,
                                                       action_id=body.submission_id)
            if (action is None) != (existing is None):
                _conflict("assignment_idempotency_conflict")
            content, reviewed, result_digest = self._content(tx, repository, owner, read,
                conversation_id=body.conversation_id,
                base_render_revision=body.expected_workspace_revision,
                base_publication_id=body.expected_workspace_publication_id,
                publication_id=body.publication_id)
            precondition = precondition_digest(read.assignment, result_action_id=read.result_reference,
                result_digest=result_digest, conversation_id=body.conversation_id,
                base_render_revision=body.expected_workspace_revision,
                base_publication_id=body.expected_workspace_publication_id)
            if action is not None:
                stored = _stored_proposal(action)
                current = (body.submission_id, read.result_reference, result_digest, body.publication_id,
                           body.conversation_id, reviewed.component_id, body.expected_workspace_revision,
                           body.expected_workspace_publication_id, plane_digest(reviewed.payload),
                           result_publication_stage_digest(content), precondition)
                names = tuple(name for name in _PROPOSAL_FIELDS if name != "permission_digest")
                if (stored is None or action.state != "proposed"
                        or tuple(getattr(stored, name) for name in names) != current):
                    _conflict("assignment_idempotency_conflict")
                proposal, created = stored, False
            else:
                proposal = ResultPublicationProposal(
                    body.submission_id, read.result_reference, result_digest, body.publication_id,
                    body.conversation_id, reviewed.component_id, body.expected_workspace_revision,
                    body.expected_workspace_publication_id, plane_digest(reviewed.payload),
                    result_publication_stage_digest(content), permission_digest(read.assignment, scopes),
                    precondition, expires_at)
                action = _sync(_method(repository, "put_result_publication_proposal")(
                    tx, owner_id=owner, assignment_id=identity,
                    expected_instruction_revision=read.assignment.instruction_revision,
                    expected_control_epoch=read.assignment.control_epoch,
                    expected_state_version=body.expected_revision, proposal=proposal,
                    content=content, expected_selected=selected, authority=authority,
                    caller_valid_until=cutoff))
                if (type(action) is not AssignmentActionRecord or action.action_id != body.submission_id
                        or action.state != "proposed"):
                    raise AssignmentError("work_control_unavailable", 503)
                created = True
            updated = _owned_operation(tx, repository, owner, identity)
            if created:
                self.audit.append_publication(tx, owner_id=owner, command="result.propose",
                    record=updated.assignment, action_id=action.action_id,
                    publication_id=proposal.publication_id, conversation_id=proposal.conversation_id)
                sessions.assert_current_execution(tx, observation=authority)
            return self._review(action, proposal, reviewed, updated, created=created)

        result = await self._transaction(propose, caller)
        await caller.verify_delivery()
        return result

    async def save(self, identity, action_id, body: WorkResultSaveRequest, *, caller):
        owner = self._owner(caller)
        body = _request(body, WorkResultSaveRequest)
        identity, action_id = _identity(identity), _identity(action_id)
        signature = digest({"api_version": 1, "operation_id": identity, "action_id": action_id,
                            "command": "result.save", **body.model_dump(exclude={"expected_revision"})})

        def decision_for(action):
            return AssignmentActionDecision(body.proposal_digest, "approve", body.submission_id,
                signature, action.intent.permission_digest, action.intent.precondition_digest)

        def inspect(tx, repository):
            read = _owned_operation(tx, repository, owner, identity)
            action = _method(repository, "get_action")(tx, owner_id=owner, assignment_id=identity,
                                                       action_id=action_id)
            if type(action) is not AssignmentActionRecord or _stored_proposal(action) is None:
                raise AssignmentError("work_not_found", 404)
            if action.state == "succeeded":
                prepared = _prepared(_sync(_method(repository, "prepare_result_publication")(
                    tx, owner_id=owner, assignment_id=identity, action_id=action_id,
                    decision=decision_for(action), expected_state_version=body.expected_revision,
                    authority=None, caller_valid_until=None)),
                    ResultPublicationPreparation, owner, identity)
                if prepared.replayed is not True:
                    raise AssignmentError("work_control_unavailable", 503)
                receipt = _receipt(prepared.receipt, owner, identity, action_id)
                return self._saved(receipt, _owned_operation(tx, repository, owner, identity), applied=False)
            if body.proposal_digest != action.intent.request_digest:
                _conflict()
            if read.assignment.state_version != body.expected_revision:
                _conflict("assignment_revision_conflict")
            if action.state != "proposed":
                _conflict()
            if _stored_proposal(action).expires_at <= datetime.now(UTC):
                _conflict("work_proposal_expired")
            self._selected_current(tx, repository, owner, read, action)
            return read.assignment

        selected_record = await self._transaction(inspect, caller)
        if type(selected_record) is not AssignmentRecord:
            await caller.verify_delivery()
            return selected_record
        caller, authority, cutoff = await self._original(caller, selected_record)
        scopes = await self._scopes(owner, caller.context.claims, selected_record)

        def save(tx, repository):
            sessions = self._sessions()
            sessions.assert_current_execution(tx, observation=authority)
            read = _owned_operation(tx, repository, owner, identity)
            if not _same_operation(read.assignment, selected_record):
                _conflict("assignment_revision_conflict")
            action = _method(repository, "get_action")(tx, owner_id=owner, assignment_id=identity,
                                                       action_id=action_id)
            if type(action) is not AssignmentActionRecord or _stored_proposal(action) is None:
                raise AssignmentError("work_not_found", 404)
            if _stored_proposal(action).expires_at <= datetime.now(UTC):
                _conflict("work_proposal_expired")
            selected = self._selected_current(tx, repository, owner, read, action)
            decision = decision_for(action)
            prepared = _prepared(_sync(_method(repository, "prepare_result_publication")(
                tx, owner_id=owner, assignment_id=identity, action_id=action_id, decision=decision,
                expected_state_version=body.expected_revision, authority=authority,
                caller_valid_until=cutoff)), ResultPublicationPreparation, owner, identity)
            if prepared.replayed:
                receipt = _receipt(prepared.receipt, owner, identity, action_id)
                return self._saved(receipt, _owned_operation(tx, repository, owner, identity), applied=False)
            proposal = prepared.proposal
            if proposal.expires_at <= datetime.now(UTC):
                _conflict("work_proposal_expired")
            if permission_digest(read.assignment, scopes) != prepared.action.intent.permission_digest:
                raise AssignmentError("assignment_scope_changed", 403)
            content, reviewed, result_digest = self._content(tx, repository, owner, read,
                conversation_id=proposal.conversation_id,
                base_render_revision=proposal.base_render_revision,
                base_publication_id=proposal.base_publication_id,
                publication_id=proposal.publication_id)
            expected_precondition = precondition_digest(read.assignment,
                result_action_id=read.result_reference, result_digest=result_digest,
                conversation_id=proposal.conversation_id,
                base_render_revision=proposal.base_render_revision,
                base_publication_id=proposal.base_publication_id)
            if (expected_precondition != prepared.action.intent.precondition_digest
                    or reviewed.component_id != proposal.component_id
                    or plane_digest(reviewed.payload) != proposal.content_digest
                    or result_publication_stage_digest(content) != proposal.stage_digest
                    or result_digest != proposal.result_digest):
                _conflict("assignment_precondition_changed")
            self.audit.append_publication(tx, owner_id=owner, command="result.save",
                record=prepared.assignment, action_id=action_id, publication_id=proposal.publication_id,
                conversation_id=proposal.conversation_id, submission_id=body.submission_id)
            receipt = _receipt(_sync(_method(repository, "commit_result_publication")(
                tx, owner_id=owner, assignment_id=identity, action_id=action_id, decision=decision,
                expected_state_version=body.expected_revision, content=content, authority=authority,
                caller_valid_until=cutoff)), owner, identity, action_id)
            if (receipt.publication_id != proposal.publication_id
                    or receipt.committed_render_revision != proposal.base_render_revision + 1):
                raise AssignmentError("work_control_unavailable", 503)
            updated = _owned_operation(tx, repository, owner, identity)
            _method(repository, "assert_selected_input_current")(
                tx, owner_id=owner, assignment_id=identity,
                expected_instruction_revision=receipt.instruction_revision,
                expected_control_epoch=receipt.control_epoch,
                expected_state_version=updated.assignment.state_version, expected=selected,
                authority_valid_until=min(cutoff, proposal.expires_at))
            sessions.assert_current_execution(tx, observation=authority)
            return self._saved(receipt, updated, applied=True)

        result = await self._transaction(save, caller)
        await caller.verify_delivery()
        return result
