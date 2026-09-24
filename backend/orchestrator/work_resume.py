"""Resumes one owner's paused work using its original session proof and the current
research profile; grants no execution permit by itself. Composes
work_control_authority.py and work_controls.py for work_api.py.
"""

from __future__ import annotations

from astralplane.repositories.assignment_models import AssignmentRecord
from pydantic import ValidationError

from orchestrator.session_authority import SessionAuthorityUnavailable, _same_operation
from orchestrator.work_admission_api import _Composition as ResearchComposition
from orchestrator.work_continuation_authority import refresh_operation_control_authority
from orchestrator.work_control_authority import WorkCallerAuthority
from orchestrator.work_controls import WorkControlRequest, WorkControlService, _method, _owned_operation
from orchestrator.work_service import _identity, _public
from orchestrator.work_submit import FixedResearchPreflight
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.models import AssignmentError, SourceSelection, ToolReference
from persistent_agents.runtime_values import digest, thaw


class WorkResumeService(WorkControlService):
    async def resume(self, identity, body: WorkControlRequest, *, caller):
        if type(caller) is not WorkCallerAuthority:
            raise AssignmentError("work_authentication_required", 401)
        owner = caller.context.owner_id
        self._caller(caller, owner, caller.context.claims)
        if type(body) is not WorkControlRequest:
            raise AssignmentError("work_control_invalid", 422)
        try:
            body = WorkControlRequest.model_validate(body.model_dump())
        except ValidationError:
            raise AssignmentError("work_control_invalid", 422) from None
        identity = _identity(identity)
        signature = digest({"api_version": 1, "operation_id": identity,
                            "command": "resume", **body.model_dump()})

        def receipt(tx, repository):
            found = _method(repository, "get_submission_receipt")(
                tx, owner_id=owner, assignment_id=identity, submission_id=body.submission_id,
                submission_digest=signature, command="resume")
            if found is not None:
                return {"operation": _public(_owned_operation(tx, repository, owner, identity), owner),
                        "applied": False}
            return None

        def select(tx, repository):
            read = _owned_operation(tx, repository, owner, identity)
            replay = receipt(tx, repository)
            if replay is not None:
                return replay
            if read.assignment.state_version != body.expected_revision:
                raise AssignmentError("assignment_revision_conflict", 409)
            return read.assignment

        selected = await self._transaction(select, caller)
        if type(selected) is not AssignmentRecord:
            await caller.verify_delivery()
            return selected
        original_b = caller.require_session()
        try:
            composition = ResearchComposition.capture(caller._binding.app)
            composition.new_admission()
            composition.runner._assert_operation_capability(selected)
            original_a = selected.operation["authority"]["reference_id"]
            authority = await refresh_operation_control_authority(context=caller.context,
                original=selected, command="resume", sessions=composition.sessions,
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

            def resume(tx, repository):
                replay = receipt(tx, repository)
                if replay is not None:
                    return replay
                current = _owned_operation(tx, repository, owner, identity).assignment
                if not _same_operation(current, selected):
                    raise AssignmentError("assignment_revision_conflict", 409)
                composition.new_admission()
                composition.runner._assert_operation_capability(current)
                result = _method(repository, "apply_control")(
                    tx, owner_id=owner, assignment_id=identity,
                    expected_instruction_revision=selected.instruction_revision,
                    expected_control_epoch=selected.control_epoch,
                    expected_state_version=body.expected_revision,
                    submission_id=body.submission_id, submission_digest=signature, control="resume")
                preflight.assert_current(tx, runtime=self.store.plane_runtime,
                                         owner_id=owner, prepared=prepared)
                updated = _owned_operation(tx, repository, owner, identity)
                if result.applied:
                    self.audit.append(tx, owner_id=owner, command="resume", record=updated.assignment)
                composition.new_admission()
                preflight.assert_key(prepared)
                preflight.assert_policy(tx, runtime=self.store.plane_runtime,
                    orchestrator=self.assignments.orch, owner_id=owner, claims=authority.claims)
                composition.new_admission()
                preflight.assert_key(prepared)
                return {"operation": _public(updated, owner), "applied": result.applied}

            result = await self._transaction(resume, caller)
            await caller.verify_delivery()
            return result
        except SessionAuthorityUnavailable:
            raise AssignmentError("work_authority_unavailable", 403) from None
        except DispatchDenied:
            raise AssignmentError("work_research_profile_unavailable", 503) from None
