"""A local-profile Work execution is charged through the UNCHANGED adapter.

Feature 088 T016 / FR-019. ``persistent_agents.execution`` is used read-only:
the local profile's reservation is the action maximum, the adapter's own rule
(``tokens = maximum.tokens if total is None else total``) charges an unknown
usage observation at the reserved maximum and a parsed one at its actual
count, and no synthetic zero/negative counter is ever recorded.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astralplane.repositories.assignments import AssignmentActionIntent, AssignmentResourceAmount
from llm_config import research_profile as profile
from persistent_agents.dispatch_context import current_dispatch
from persistent_agents.runtime_values import digest
from persistent_agents.tests.test_execution import action_record, executor as shared_executor
from persistent_agents.tests.test_service import service as shared_service

service = shared_service
executor = shared_executor

LOCAL_MODEL = "qwen2.5:7b-instruct-q4_K_M"


def local_action(record):
    request = {"kind": "model", "provider": "ollama", "model": LOCAL_MODEL,
               "max_output_tokens": profile.LOCAL_OUTPUT_TOKENS,
               "messages": [{"role": "user", "content": "Select the relevant passages."}]}
    maximum = AssignmentResourceAmount(**profile.LOCAL_PROFILE.reservation())
    intent = AssignmentActionIntent("local-research", request, digest(request), maximum,
                                    "b" * 64, "c" * 64)
    return action_record(record, request, intent=intent), maximum


def model_stub(usage_total):
    async def model(*args, **kwargs):
        usage = None if usage_total is None else SimpleNamespace(total_tokens=usage_total)
        response = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop",
                                     message=SimpleNamespace(content='{"version":1,"passage_ids":["p001"]}'))],
            usage=usage, model=LOCAL_MODEL)
        await current_dispatch().invoke_model(AsyncMock(return_value=response),
                                              {"messages": kwargs.get("messages") or args[1]})
        return response.choices[0].message, None
    return model


@pytest.mark.asyncio
async def test_local_profile_execution_with_unknown_usage_charges_the_reserved_maximum(executor):
    action, maximum = local_action(executor.record)
    assert maximum == AssignmentResourceAmount(model_calls=1, tokens=32768 + 1024, elapsed_ms=120_000)
    assert maximum.tokens > 0 and 0 < maximum.elapsed_ms <= 120_000
    executor.orch._call_llm = AsyncMock(side_effect=model_stub(None))
    assert "p001" in (await executor.execute(action))["text"]
    reserved = next(kwargs["maximum"] for method, kwargs in executor.test_calls if method == "reserve_action")
    assert reserved == maximum == action.intent.maximum
    receipt = executor.test_outcomes[0]
    assert receipt.outcome == "succeeded"
    # Unknown provider usage: the receipt carries NO actual amount, so the
    # reservation (the profile's declared maximum) is what stays charged. A
    # zero or negative synthetic counter is never written.
    assert receipt.actual is None
    executor.store.repository.start_action.assert_called_once()
    assert not any(method == "release_unstarted_action" for method, _ in executor.test_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [1, 57, 32768 + 1024, 40000])
async def test_local_profile_execution_with_parsed_usage_charges_actual_tokens(executor, total):
    action, maximum = local_action(executor.record)
    executor.orch._call_llm = AsyncMock(side_effect=model_stub(total))
    assert "p001" in (await executor.execute(action))["text"]
    reserved = next(kwargs["maximum"] for method, kwargs in executor.test_calls if method == "reserve_action")
    assert reserved == maximum
    receipt = executor.test_outcomes[0]
    assert receipt.outcome == "succeeded"
    actual = receipt.actual
    # Actual tokens are the provider's own count -- never clamped to the
    # reservation, never zero -- and the elapsed charge is bounded by the
    # profile's declared elapsed maximum while staying strictly positive.
    assert actual.tokens == total > 0
    assert actual.model_calls == maximum.model_calls == 1
    assert actual.tool_calls == 0
    assert 1 <= actual.elapsed_ms <= maximum.elapsed_ms == profile.LOCAL_RESERVED_MILLISECONDS
    assert actual.spend_micro_units is None and actual.currency is None


@pytest.mark.asyncio
async def test_local_and_openai_reservations_share_one_charging_shape(executor):
    """The adapter never sees a profile object -- only the reservation shape."""
    for declared in profile.PROFILES:
        amount = AssignmentResourceAmount(**declared.reservation())
        assert amount.model_calls == 1 and amount.tokens == declared.reserved_tokens
        assert amount.elapsed_ms == declared.reserved_milliseconds
        assert 0 < amount.elapsed_ms <= 120_000 and amount.tokens > 0
    openai = AssignmentResourceAmount(model_calls=1, tokens=profile.RESERVED_TOKENS,
                                      elapsed_ms=profile.RESERVED_MILLISECONDS)
    assert AssignmentResourceAmount(**profile.OPENAI_PROFILE.reservation()) == openai
    assert AssignmentResourceAmount(**profile.LOCAL_PROFILE.reservation()) != openai
