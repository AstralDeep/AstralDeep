"""Completion capacity remains metered and immutable across runner upgrades."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astralplane.repositories.assignments import AssignmentResourceAmount
from persistent_agents.dispatch_context import DispatchDenied, canonical
from persistent_agents.runner import AssignmentRunner
from persistent_agents.runtime_values import digest, thaw
from persistent_agents.tests.test_dispatch_integration import _Completions, model_hub
from persistent_agents.tests.test_execution import action_record, executor as shared_executor
from persistent_agents.tests.test_service import service as shared_service

service = shared_service
executor = shared_executor


def messages():
    return [{"role": "system", "content": "Plan a concise analysis."},
            {"role": "user", "content": canonical({"source": "Public release evidence"})}]


async def invoke(runner, executor):
    return await runner._model(executor, "plan", messages()[0]["content"], {"source": "Public release evidence"})


@pytest.mark.asyncio
@pytest.mark.parametrize("global_effort", [None, "high"])
async def test_new_model_allowance_reaches_configured_provider_with_full_reservation(executor, global_effort):
    completions = _Completions()
    hub = model_hub(completions)
    hub.llm_reasoning_effort = global_effort
    executor.orch._call_llm = hub._call_llm
    runner = AssignmentRunner(executor.orch, executor.service)
    definition = executor.record.definition
    assert (await invoke(runner, executor))["text"] == "Verified."
    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["model"] == "test-model" and sent["reasoning_effort"] == "low"
    assert sent["max_completion_tokens"] == 4096
    intent = next(kwargs["intent"] for method, kwargs in executor.test_calls if method == "put_action")
    assert intent.maximum.tokens == len(canonical(messages()).encode("utf-8")) + 512 + 4096
    assert intent.request["reasoning_effort"] == "low"
    reserved = next(kwargs for method, kwargs in executor.test_calls if method == "reserve_action")
    assert reserved["maximum"] == intent.maximum
    assert executor.record.definition == definition


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum", [1024, 4096])
@pytest.mark.parametrize("global_effort", [None, "high"])
async def test_legacy_physical_dispatch_retains_absent_effort_and_original_reservation(
    executor, maximum, global_effort,
):
    request = {"kind": "model", "messages": messages(), "max_output_tokens": maximum}
    original = action_record(executor.record, request)
    original = replace(original, intent=replace(original.intent, action_key="plan",
        boundary="unreplayable", maximum=AssignmentResourceAmount(model_calls=1,
            tokens=len(canonical(messages()).encode("utf-8")) + 512 + maximum, elapsed_ms=30_000)))
    original_digest = original.intent.request_digest
    previous = executor.store.call.side_effect

    async def call(method, **kwargs):
        return original if method == "get_action_by_key" else await previous(method, **kwargs)

    executor.store.call.side_effect = call
    completions = _Completions()
    hub = model_hub(completions)
    hub.llm_reasoning_effort = global_effort
    executor.orch._call_llm = hub._call_llm
    assert (await invoke(AssignmentRunner(executor.orch, executor.service), executor))["text"] == "Verified."
    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["max_completion_tokens"] == maximum
    if global_effort is None:
        assert "reasoning_effort" not in sent
    else:
        assert sent["reasoning_effort"] == global_effort
    assert "reasoning_effort" not in original.intent.request
    assert original.intent.request_digest == original_digest == digest(request)
    assert not any(method == "put_action" for method, _ in executor.test_calls)
    reservation = next(kwargs for method, kwargs in executor.test_calls if method == "reserve_action")
    assert reservation["maximum"] == original.intent.maximum
    executor.store.repository.start_action.assert_called_once()
    assert executor.test_outcomes[0].outcome == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum,effort", [(1024, None), (4096, None), (4096, "low")])
async def test_upgraded_runner_reuses_completed_receipt_without_provider_call(executor, maximum, effort):
    request = {"kind": "model", "messages": messages(), "max_output_tokens": maximum}
    if effort is not None:
        request["reasoning_effort"] = effort
    original = replace(action_record(executor.record, request), state="succeeded", ever_started=True,
                       result={"result": {"text": "Retained finding"}})
    previous = executor.store.call.side_effect
    async def call(method, **kwargs):
        return original if method == "get_action_by_key" else await previous(method, **kwargs)
    executor.store.call.side_effect = call
    executor.orch._call_llm = AsyncMock()
    assert await invoke(AssignmentRunner(executor.orch, executor.service), executor) == {"text": "Retained finding"}
    executor.orch._call_llm.assert_not_awaited()
    executor.store.transaction.assert_not_awaited()
    assert executor.test_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ready", "failed_not_started", "failed", "started", "uncertain", "invalidated"])
@pytest.mark.parametrize("maximum,effort", [(1024, None), (4096, None), (4096, "low")])
async def test_existing_model_intent_cap_and_identity_are_preserved_in_every_disposition(
    executor, state, maximum, effort,
):
    request = {"kind": "model", "messages": messages(), "max_output_tokens": maximum}
    if effort is not None:
        request["reasoning_effort"] = effort
    original = replace(action_record(executor.record, request), state=state)
    executor.store.call = AsyncMock(return_value=original)
    executor.action = AsyncMock(return_value={"text": "Existing disposition"})
    await invoke(AssignmentRunner(executor.orch, executor.service), executor)
    assert executor.action.call_args.args == ("plan", request)
    assert digest(executor.action.call_args.args[1]) == original.intent.request_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["kind", "messages", "extra", "digest", "effort", None, True, "1024", 0, 8193])
async def test_stored_model_shape_and_cap_must_match_before_reuse(executor, change):
    request = {"kind": "model", "messages": messages(), "max_output_tokens": 1024}
    if change == "kind":
        request["kind"] = "tool"
    elif change == "messages":
        request["messages"] = [{"role": "user", "content": "Different instruction"}]
    elif change == "extra":
        request["approval"] = True
    elif change == "effort":
        request["reasoning_effort"] = "high"
    elif change != "digest":
        request["max_output_tokens"] = change
    original = SimpleNamespace(intent=SimpleNamespace(request=request,
        request_digest="f" * 64 if change == "digest" else digest(request)))
    executor.store.call = AsyncMock(return_value=original)
    executor.action = AsyncMock()
    with pytest.raises(DispatchDenied, match="assignment_action_binding_changed"):
        await invoke(AssignmentRunner(executor.orch, executor.service), executor)
    executor.action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "low"])
async def test_existing_successor_chain_keeps_original_cap_and_completed_receipt(executor, effort):
    request = {"kind": "model", "messages": messages(), "max_output_tokens": 1024}
    if effort is not None:
        request["reasoning_effort"] = effort
    original = replace(action_record(executor.record, request), state="invalidated", control_epoch=0)
    successor_key = digest(["plan", "successor", 0])
    successor = replace(original, state="succeeded", control_epoch=executor.record.control_epoch,
        ever_started=True, result={"result": {"text": "Retained successor"}},
        intent=replace(original.intent, action_key=successor_key))
    async def call(method, **kwargs):
        assert method == "get_action_by_key"
        return original if kwargs["action_key"] == "plan" else successor
    executor.store.call = AsyncMock(side_effect=call)
    executor.orch._call_llm = AsyncMock()
    result = await invoke(AssignmentRunner(executor.orch, executor.service), executor)
    assert result == {"text": "Retained successor"}
    executor.orch._call_llm.assert_not_awaited()
    assert executor.store.call.await_count == 3


@pytest.mark.asyncio
async def test_unstarted_legacy_successor_preserves_absent_effort_on_new_physical_call(executor):
    request = {"kind": "model", "messages": messages(), "max_output_tokens": 1024}
    original = replace(action_record(executor.record, request), state="invalidated", control_epoch=0)
    previous = executor.store.call.side_effect

    async def call(method, **kwargs):
        if method == "get_action_by_key":
            return original if kwargs["action_key"] == "plan" else None
        return await previous(method, **kwargs)

    executor.store.call.side_effect = call
    completions = _Completions()
    hub = model_hub(completions)
    hub.llm_reasoning_effort = "high"
    executor.orch._call_llm = hub._call_llm
    assert (await invoke(AssignmentRunner(executor.orch, executor.service), executor))["text"] == "Verified."
    assert len(completions.calls) == 1
    assert completions.calls[0]["reasoning_effort"] == "high"
    assert completions.calls[0]["max_completion_tokens"] == 1024
    successor = next(kwargs["intent"] for method, kwargs in executor.test_calls if method == "put_action")
    assert successor.action_key == digest(["plan", "successor", original.control_epoch])
    assert thaw(successor.request) == thaw(original.intent.request) == request
    assert successor.request_digest == original.intent.request_digest
    assert successor.maximum.tokens == len(canonical(messages()).encode("utf-8")) + 512 + 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, True, "max", " LOW ", ["low"], {"value": "low"}])
async def test_retained_effort_must_be_the_exact_supported_policy(executor, effort):
    request = {"kind": "model", "messages": messages(), "max_output_tokens": 4096,
               "reasoning_effort": effort}
    original = SimpleNamespace(intent=SimpleNamespace(request=request, request_digest=digest(request)))
    executor.store.call = AsyncMock(return_value=original)
    executor.action = AsyncMock()
    with pytest.raises(DispatchDenied, match="assignment_action_binding_changed"):
        await invoke(AssignmentRunner(executor.orch, executor.service), executor)
    executor.action.assert_not_awaited()
