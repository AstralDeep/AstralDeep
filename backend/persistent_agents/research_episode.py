"""Unregistered finite one-page USER research episode and private result proof.

Only exact attributed excerpts can enter this profile's checkpoint. The result
proof is reconstructed from guarded source/model receipts and checked again in
the transaction that retires both execution leases. It grants no Save or publish
authority and is never serialized as an operation field.
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


def source_request(record):
    source = fixed_reader_source(record)
    if (
        not isinstance(source, dict)
        or record.execution_profile != "one_shot"
        or record.operation.get("kind") != "research"
        or record.operation.get("source_retention") != "operation"
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
    """Closed in-memory binding to one exact source and settled model attempt."""

    private: ResearchInput = field(repr=False)
    model_action_id: str

    def rebuild(self, executor, tx, repository, current, checks):
        """Called only inside the executor's guarded source/config transaction."""
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
        return AssignmentEpisodeCompletion(
            expected_state_version=current.state_version,
            checkpoint=checkpoint,
            completion_digest=digest(["research-result-v1", page, model.action_id]),
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
        # Lease renewal may advance state_version without changing meaningful
        # state. The runner separately binds the handler's exact snapshot version.
        expected = thaw(rebuilt)
        expected["expected_state_version"] = completion.expected_state_version
        if canonical(expected) != canonical(thaw(completion)):
            raise DispatchDenied("assignment_research_result_invalid")


async def run_research_episode(executor):
    """One retained public-page read and one fixed model action, never recurrence.

    Automatic retries and non-retained/provider breadth remain separate profiles;
    unsupported work is refused before any source action. Existing successful
    action identities can be reused only through their current governed checks.
    """
    from persistent_agents.runner import OneShotEpisodeResult

    request = source_request(executor.record)
    observation = await executor.action(SOURCE_KEY, request)
    source_id = observation["source_action_id"]
    await executor.research_selection(MODEL_KEY, source_action_id=source_id)
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
                action_key="research-v1-" + MODEL_KEY,
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
        )
        proof = ResearchCompletion(private, model.action_id)
        refreshed = await proof.refresh(executor)

        def prepare(tx, repository, guarded):
            return guarded, proof.rebuild(executor, tx, repository, guarded, refreshed)

        record, completion = await executor._research_transaction(
            private, refreshed["model"]["authority"], prepare, action_id=model.action_id
        )
        await safe_text(
            "\n".join(
                passage["text"]
                for passage in completion.checkpoint["research_result"]["passages"]
            )
        )
        return OneShotEpisodeResult(record, completion, research=proof)
