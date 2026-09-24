"""Runs one finite USER research episode (one page read, one fixed model action, never
recurrence) and produces its private result proof; orchestrated by runner.py via
research_input.py and research_recovery.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from astralplane.repositories.assignment_models import AssignmentEpisodeCompletion
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.execution import _OperationAuthorityWindow, safe_text
from persistent_agents.research_input import ResearchInput, fixed_reader_source, route
from persistent_agents.research_result import build_page_result
from persistent_agents.runtime_values import canonical, digest, thaw

SOURCE_KEY = "research-source-v1"
MODEL_KEY = "research-selection-v1"


def research_action_keys(record):
    for value in (record.instruction_revision, record.control_epoch):
        if type(value) is not int or not 1 <= value <= 2**53 - 1:
            raise DispatchDenied("assignment_operation_profile_unavailable")
    return tuple(digest([domain, record.instruction_revision, record.control_epoch])
                 for domain in ("research-source-v2", "research-selection-v2"))


def source_request(record):
    source = fixed_reader_source(record)
    if (
        not isinstance(source, dict)
        or record.execution_profile != "one_shot"
        or record.operation.get("kind") != "research"
        or record.operation.get("source_retention") not in {"operation", "none"}
        or source.get("profile") != "public_page"
        or source.get("agent_id") != "web-research-1"
        or source.get("tool_name") != "fetch_page"
        or source.get("linked_document_urls")
    ):
        raise DispatchDenied("assignment_operation_profile_unavailable")
    return {
        "kind": "tool",
        **{key: source[key] for key in ("agent_id", "tool_name", "arguments")},
    }


def _matching_checks(action, checks):
    if (
        action.intent.permission_digest != checks["permission_digest"]
        or action.intent.precondition_digest != checks["precondition_digest"]
    ):
        raise DispatchDenied("assignment_precondition_changed")


@dataclass(frozen=True, slots=True)
class ResearchCompletion:
    private: ResearchInput = field(repr=False)
    model_action_id: str

    def rebuild(self, executor, tx, repository, current, checks):
        repository.assert_current_assignment_execution(
            tx,
            fence=executor.claim.fence,
            binding=executor.binding,
            authority=checks["model"]["authority"].observation,
            action_id=self.model_action_id,
        )
        source = repository.get_action(
            tx,
            owner_id=current.owner_id,
            assignment_id=current.assignment_id,
            action_id=self.private.source_action_id,
        )
        model = repository.get_action(
            tx,
            owner_id=current.owner_id,
            assignment_id=current.assignment_id,
            action_id=self.model_action_id,
        )
        _matching_checks(source, checks["source"])
        _matching_checks(model, checks["model"])
        selection = self.private.retained_result(model)
        retained = thaw(source.result)
        page = build_page_result(
            retained["result"],
            selection["passage_ids"],
            source_action_id=source.action_id,
            source_result_digest=retained["result_digest"],
        )
        checkpoint = {**thaw(current.checkpoint), "research_result": page}
        completion_input = ["research-result-v1", page, model.action_id]
        if selection["version"] == 3:
            completion_input = ["research-result-v3", page, model.action_id,
                                selection["selected_input"]]
        return AssignmentEpisodeCompletion(
            expected_state_version=current.state_version,
            checkpoint=checkpoint,
            completion_digest=digest(completion_input),
            phase="waiting",
            wake_reason="research_completed",
            completed=True,
            terminal_outcome="completed",
            result_reference=model.action_id,
        )

    async def refresh(self, executor):
        self.private.assert_record(executor.record)
        source = await executor.refresh(source_request(executor.record))
        model = await executor.refresh(
            route(), authority=source["authority"], _research=self.private
        )
        return {"source": source, "model": model}

    def assert_completion(self, executor, tx, repository, current, checks, completion):
        rebuilt = self.rebuild(executor, tx, repository, current, checks)
        expected = thaw(rebuilt)
        expected["expected_state_version"] = completion.expected_state_version
        if canonical(expected) != canonical(thaw(completion)):
            raise DispatchDenied("assignment_research_result_invalid")


@dataclass(frozen=True, slots=True)
class EphemeralResearchCompletion(ResearchCompletion):
    _selection_json: str = field(repr=False)

    def rebuild(self, executor, tx, repository, current, checks):
        import json

        repository.assert_current_assignment_execution(tx, fence=executor.claim.fence,
            binding=executor.binding, authority=checks["model"]["authority"].observation,
            action_id=self.model_action_id)
        source = repository.get_action(tx, owner_id=current.owner_id,
            assignment_id=current.assignment_id, action_id=self.private.source_action_id)
        model = repository.get_action(tx, owner_id=current.owner_id,
            assignment_id=current.assignment_id, action_id=self.model_action_id)
        _matching_checks(source, checks["source"])
        _matching_checks(model, checks["model"])
        self.private._ephemeral.identity(current, source)
        self.private.ephemeral_result(model, json.loads(self._selection_json))
        metadata = self.private._ephemeral.metadata()
        # Never copy a prior text checkpoint into a non-retained run
        if canonical(thaw(current.checkpoint)) not in {"{}", '{"schema_version":1}'}:
            raise DispatchDenied("assignment_research_result_invalid")
        checkpoint = {"schema_version": 1, "research_source": metadata}
        completion_input = ["ephemeral-research-result-v1", metadata, model.action_id]
        selection = json.loads(self._selection_json)
        if selection["version"] == 3:
            completion_input = ["ephemeral-research-result-v3", metadata, model.action_id,
                                selection["selected_input"]]
        return AssignmentEpisodeCompletion(expected_state_version=current.state_version,
            checkpoint=checkpoint,
            completion_digest=digest(completion_input),
            phase="waiting", wake_reason="research_completed", completed=True,
            terminal_outcome="completed", result_reference=model.action_id)


async def run_research_episode(executor):
    from persistent_agents.runner import OneShotEpisodeResult

    request = source_request(executor.record)
    source_key, model_key = research_action_keys(executor.record)
    ephemeral = None
    if executor.record.operation.get("source_retention") == "none":
        from persistent_agents.research_recovery import model_key as ephemeral_model_key
        ephemeral = await executor.acquire_research_source()
        source_id = ephemeral.source_action_id
        model_key = ephemeral_model_key(executor.record, ephemeral)
        selection = await executor.research_selection(model_key, source_action_id=source_id,
                                                      ephemeral=ephemeral)
    else:
        observation = await executor.action(source_key, request)
        source_id = observation["source_action_id"]
        await executor.research_selection(model_key, source_action_id=source_id)
    async with _OperationAuthorityWindow(executor.operation_authority_lock):
        checks = await executor.refresh(request)
        current, source = await executor.store.read_current_action(
            fence=executor.claim.fence,
            binding=executor.binding,
            action_id=source_id,
            authority=checks["authority"].observation,
        )
        model = await executor.store.transaction(
            lambda tx, repository: repository.get_action_by_key(
                tx,
                owner_id=current.owner_id,
                assignment_id=current.assignment_id,
                action_key="research-v1-" + model_key,
            ),
            bound_session_waits=True,
        )
        if model is None or model.intent.transient_input is None:
            raise DispatchDenied("assignment_research_binding_changed")
        private = await ResearchInput.capture(
            current,
            source,
            config_store=executor.orch._llm_store,
            key_id=model.intent.transient_input.binding_key_id,
            ephemeral=ephemeral,
            guidance=executor._research_guidance,
        )
        proof = (ResearchCompletion(private, model.action_id) if ephemeral is None else
                 EphemeralResearchCompletion(private, model.action_id, canonical(selection)))
        refreshed = await proof.refresh(executor)

        def prepare(tx, repository, guarded):
            return guarded, proof.rebuild(executor, tx, repository, guarded, refreshed)

        record, completion = await executor._research_transaction(
            private, refreshed["model"]["authority"], prepare, action_id=model.action_id
        )
        if ephemeral is None:
            await safe_text(
                "\n".join(
                    passage["text"]
                    for passage in completion.checkpoint["research_result"]["passages"]
                )
            )
        return OneShotEpisodeResult(record, completion, research=proof)
