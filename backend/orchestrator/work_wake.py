"""Acknowledges a manual owner event to unblock a waiting operation, checking current
continuation state via work_control_authority.py without granting execution; the
runner still acquires its own fences later. Used by work_api.py.
"""

from __future__ import annotations

from astralplane.repositories.assignment_models import (
    AssignmentControlResult, AssignmentRecord, AssignmentWakePreparation,
)
from pydantic import Field, ValidationError, field_validator

from orchestrator.session_authority import SessionAuthorityUnavailable, _same_operation
from orchestrator.work_admission_api import _Composition as ResearchComposition
from orchestrator.work_continuation_authority import refresh_operation_control_authority
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_controls import WorkControlRequest, WorkControlService, _method, _owned_operation
from orchestrator.work_service import _identity, _public
from orchestrator.work_submit import FixedResearchPreflight, _sync
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError, SourceSelection, ToolReference, validate_id
from persistent_agents.runtime_values import digest, thaw


class WorkOwnerWakeRequest(WorkControlRequest):
    owner_event_id: str = Field(min_length=36, max_length=36)
    owner_revision: int = Field(ge=0, le=2**53 - 1)

    @field_validator("owner_event_id")
    @classmethod
    def canonical_owner_event(cls, value):
        return validate_id(value)


def _prepared(value, owner, identity):
    if (type(value) is not AssignmentWakePreparation
            or type(value.assignment) is not AssignmentRecord
            or value.assignment.owner_id != owner or value.assignment.assignment_id != identity
            or type(value.replayed) is not bool):
        raise AssignmentError("work_control_unavailable", 503)
    return value


class WorkWakeService(WorkControlService):
    async def wake(self, identity, body: WorkOwnerWakeRequest, *, caller):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        if type(body) is not WorkOwnerWakeRequest:
            raise AssignmentError("work_control_invalid", 422)
        try:
            body = WorkOwnerWakeRequest.model_validate(body.model_dump())
        except ValidationError:
            raise AssignmentError("work_control_invalid", 422) from None
        identity = _identity(identity)
        event_digest = digest({"api_version": 1, "operation_id": identity, "command": "wake",
                               **body.model_dump(exclude={"expected_revision"})})

        def arguments(record):
            return dict(owner_id=owner, assignment_id=identity,
                expected_instruction_revision=record.instruction_revision,
                expected_control_epoch=record.control_epoch, expected_state_version=body.expected_revision,
                event_id=body.submission_id, event_key="owner:" + body.owner_event_id,
                source_revision=body.owner_revision, event_digest=event_digest, control_version=1)

        def prepare(tx, repository):
            read = _owned_operation(tx, repository, owner, identity)
            return _prepared(_sync(_method(repository, "prepare_wake")(
                tx, **arguments(read.assignment))), owner, identity)

        def response(tx, repository, *, applied):
            return {"operation": _public(_owned_operation(tx, repository, owner, identity), owner),
                    "applied": applied}

        prepared_wake = await self._transaction(prepare, caller)
        if prepared_wake.replayed:
            result = await self._transaction(lambda tx, repo: response(tx, repo, applied=False), caller)
            await caller.verify_delivery()
            return result
        selected = prepared_wake.assignment
        original_b = caller.require_session()
        try:
            composition = ResearchComposition.capture(caller._binding.app)
            composition.new_admission()
            composition.runner._assert_operation_capability(selected)
            original_a = selected.operation["authority"]["reference_id"]
            authority = await refresh_operation_control_authority(context=caller.context,
                original=selected, command="wake", sessions=composition.sessions,
                expected_request_credential=(original_b.credential
                    if original_a == original_b.credential.incarnation_id else None))
            caller = caller.with_original(authority)
            preflight = FixedResearchPreflight(composition.config)
            source = SourceSelection.model_validate(thaw(selected.definition.source))
            scopes = await self.assignments._definition_policy(owner, authority.claims,
                name=selected.definition.name, instructions=selected.definition.instructions,
                source=source, allowed_tools=[ToolReference(agent_id=source.agent_id, tool_name=source.tool_name)],
                completion_condition=selected.definition.completion_condition,
                conversation_id=selected.definition.conversation_id)
            if tuple(sorted(set(scopes.values()))) != selected.definition.consented_scopes:
                raise AssignmentError("assignment_scope_changed", 403)
            prepared = await preflight.prepare(owner_id=owner, runtime=self.store.plane_runtime,
                definition=selected.definition,
                source_bound=self.assignments.tool_bound("web-research-1:fetch_page"))
            composition.new_admission()

            def wake(tx, repository):
                current = prepare(tx, repository)
                if current.replayed:
                    return response(tx, repository, applied=False)
                if not _same_operation(current.assignment, selected):
                    raise AssignmentError("assignment_revision_conflict", 409)
                composition.new_admission()
                composition.runner._assert_operation_capability(current.assignment)
                clear = _sync(_method(repository, "assert_operation_continuation_clear")(
                    tx, owner_id=owner, assignment_id=identity,
                    expected_instruction_revision=selected.instruction_revision,
                    expected_control_epoch=selected.control_epoch,
                    expected_state_version=body.expected_revision))
                if type(clear) is not AssignmentRecord or not _same_operation(clear, selected):
                    raise AssignmentError("work_control_unavailable", 503)
                result = _sync(_method(repository, "accept_wake")(tx, **arguments(selected)))
                if (type(result) is not AssignmentControlResult or result.applied is not True
                        or type(result.assignment) is not AssignmentRecord
                        or result.assignment.owner_id != owner or result.assignment.assignment_id != identity):
                    raise AssignmentError("work_control_unavailable", 503)
                preflight.assert_current(tx, runtime=self.store.plane_runtime,
                                         owner_id=owner, prepared=prepared)
                self.audit.append(tx, owner_id=owner, command="wake", record=result.assignment)
                composition.new_admission()
                preflight.assert_key(prepared)
                reply = response(tx, repository, applied=True)
                preflight.assert_policy(tx, runtime=self.store.plane_runtime,
                    orchestrator=self.assignments.orch, owner_id=owner, claims=authority.claims)
                composition.new_admission()
                preflight.assert_key(prepared)
                return reply

            result = await self._transaction(wake, caller)
            await caller.verify_delivery()
            return result
        except SessionAuthorityUnavailable:
            raise AssignmentError("work_authority_unavailable", 403) from None
        except DispatchDenied:
            raise AssignmentError("work_research_profile_unavailable", 503) from None
