"""Qualifies standard harness accounting through the ordinary orchestrator model runner.
Only the provider SDK and audit sinks are scripted; retries and compatibility behavior use production code.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from llm_config.types import LLMUnavailable
from llm_config.invocation import InvocationContractError, InvocationResult
from orchestrator.harness import (
    HarnessAccounting,
    HarnessContractError,
    HarnessOutcome,
    HarnessTurnRequest,
)
from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
from tests.test_llm_streaming import _FakeCompletions, _Resp, _bare_orch, _content_chunk


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    from orchestrator import model_router

    original = asyncio.sleep

    async def advance(_seconds):
        await original(0)

    monkeypatch.setattr(asyncio, "sleep", advance)
    monkeypatch.setattr(model_router, "router_enabled", lambda: False)


async def through_harness(orch, *, websocket=None, **kwargs):
    async def invoke():
        return await orch._call_llm(
            websocket, [{"role": "user", "content": "Synthetic request"}], **kwargs
        )

    harness = StandardLLMHarness(invoke=invoke)
    execution = await harness.start(
        HarnessTurnRequest(user_id="synthetic-owner", message="Synthetic request")
    )
    return await harness.await_result(execution)


@pytest.mark.asyncio
async def test_exhausted_actual_runner_is_failure_with_every_dispatch_counted():
    provider = Provider([TimeoutError("synthetic timeout")] * 3)
    orch = _bare_orch(provider)
    orch.MAX_RETRIES = 3
    result = await through_harness(orch)
    assert len(provider.calls) == 3
    assert result.outcome is HarnessOutcome.FAILED
    assert result.accounting.attempts == 3
    assert result.accounting.credential_outcome is None


@pytest.mark.asyncio
async def test_unconfigured_actual_runner_denial_has_no_provider_dispatch():
    provider = Provider([])
    orch = _bare_orch(provider)

    async def unavailable(_websocket):
        raise LLMUnavailable("synthetic unavailable")

    orch._resolve_llm_client_for = unavailable
    result = await through_harness(orch)
    assert provider.calls == []
    assert result.outcome is HarnessOutcome.DENIED
    assert result.accounting.attempts == 0
    assert result.accounting.credential_outcome == "unavailable"


@pytest.mark.asyncio
async def test_actual_runner_retry_success_counts_failed_and_successful_dispatch():
    provider = Provider(
        [TimeoutError("synthetic timeout"), _Resp("Synthetic completion")]
    )
    result = await through_harness(_bare_orch(provider))
    assert len(provider.calls) == 2
    assert result.outcome is HarnessOutcome.SUCCEEDED
    assert result.accounting.attempts == 2
    assert result.accounting.total_tokens == 10


@pytest.mark.asyncio
async def test_actual_runner_compatibility_retry_counts_attempt_with_reported_charge():
    error = ValueError("synthetic forced choice unsupported")
    error.usage = SimpleNamespace(total_tokens=4)
    provider = Provider([error, _Resp("Synthetic completion")])
    result = await through_harness(
        _bare_orch(provider), tools_desc=[{"type": "function"}], tool_choice="required"
    )
    assert len(provider.calls) == 2
    assert result.outcome is HarnessOutcome.SUCCEEDED
    assert result.accounting.attempts == 2
    assert result.accounting.total_tokens == 14
    assert result.accounting.usage_complete


@pytest.mark.asyncio
async def test_actual_stream_failure_and_nonstream_fallback_retain_both_charges():
    chunk = _content_chunk("Synthetic partial")
    chunk.usage = SimpleNamespace(total_tokens=2)
    provider = _FakeCompletions(chunks=[chunk], raise_after=1)
    result = await through_harness(
        _bare_orch(provider), websocket=object(), allow_stream=True
    )
    assert len(provider.calls) == 2
    assert result.outcome is HarnessOutcome.SUCCEEDED
    assert result.accounting.attempts == 2
    assert result.accounting.total_tokens == 12
    assert result.accounting.usage_complete


@pytest.mark.asyncio
async def test_actual_successful_stream_accounts_usage_after_consumption():
    first, last = _content_chunk("Synthetic "), _content_chunk("completion")
    last.usage = SimpleNamespace(total_tokens=12)
    provider = _FakeCompletions(chunks=[first, last])
    result = await through_harness(
        _bare_orch(provider), websocket=object(), allow_stream=True
    )
    assert len(provider.calls) == result.accounting.attempts == 1
    assert result.decision[0].content == "Synthetic completion"
    assert result.accounting.total_tokens == 12
    assert result.accounting.usage_complete


@pytest.mark.asyncio
async def test_actual_cancellation_retains_prior_charge_and_inflight_dispatch():
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class ReportedProviderFailure(RuntimeError):
        status_code = 503

    error = ReportedProviderFailure("Synthetic retry")
    error.usage = SimpleNamespace(total_tokens=4)

    class BlockingProvider(Provider):
        def create(self, **kwargs):
            if not self.calls:
                return super().create(**kwargs)
            self.calls.append(kwargs)
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return _Resp("Synthetic late completion")

    provider = BlockingProvider([error])
    orch = _bare_orch(provider)

    async def invoke():
        return await orch._call_llm(
            None, [{"role": "user", "content": "Synthetic request"}]
        )

    harness = StandardLLMHarness(invoke=invoke)
    execution = await harness.start(
        HarnessTurnRequest(user_id="synthetic-owner", message="x")
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        result = await harness.cancel(execution)
    finally:
        release.set()
    assert len(provider.calls) == 2
    assert result.outcome is HarnessOutcome.CANCELLED
    assert result.accounting.attempts == 2
    assert result.accounting.total_tokens == 4
    assert not result.accounting.usage_complete


@pytest.mark.asyncio
async def test_accounting_isolated_between_concurrent_actual_runner_calls():
    first = Provider([TimeoutError("Synthetic timeout"), _Resp("First completion")])
    second = Provider([_Resp("Second completion")])
    results = await asyncio.gather(
        through_harness(_bare_orch(first)), through_harness(_bare_orch(second))
    )
    assert [result.accounting.attempts for result in results] == [2, 1]
    assert [result.accounting.total_tokens for result in results] == [10, 10]
    assert [result.accounting.usage_complete for result in results] == [False, True]


@pytest.mark.asyncio
async def test_untyped_ambiguous_callable_cannot_claim_credential_denial():
    async def invoke():
        return None, None

    harness = StandardLLMHarness(invoke=invoke)
    execution = await harness.start(
        HarnessTurnRequest(user_id="synthetic-owner", message="x")
    )
    with pytest.raises(HarnessContractError, match="accounted"):
        await harness.await_result(execution)


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": []},
        {"outcome": "invented"},
        {"attempts": -1},
        {"attempts": True},
        {"attempts": "1"},
        {"usage_complete": "yes"},
        {"total_tokens": -1},
        {"total_tokens": True},
        {"total_tokens": "10"},
        {"credential_outcome": []},
        {"credential_outcome": "invented"},
        {"error_class": []},
        {"error_class": "x" * 129},
        {"error_class": "not a class"},
        {"outcome": "succeeded"},
        {"outcome": "succeeded", "attempts": 0, "message": "Synthetic"},
        {"outcome": "denied", "attempts": 1},
        {"message": "Unexpected failed output"},
        {
            "outcome": "succeeded",
            "message": "Synthetic",
            "credential_outcome": "unavailable",
        },
        {"attempts": 0, "total_tokens": 1},
        {"usage_complete": True},
    ],
)
def test_typed_runner_result_rejects_malformed_or_inconsistent_values(changes):
    values = {"outcome": "failed", "attempts": 1, **changes}
    with pytest.raises(InvocationContractError):
        InvocationResult(**values)


@pytest.mark.asyncio
async def test_unaccounted_exception_is_a_contract_error_without_invented_attempts():
    async def invoke():
        raise TimeoutError("Synthetic unaccounted provider failure")

    harness = StandardLLMHarness(invoke=invoke)
    execution = await harness.start(
        HarnessTurnRequest(user_id="synthetic-owner", message="x")
    )
    with pytest.raises(HarnessContractError, match="accounted"):
        await harness.await_result(execution)


@pytest.mark.asyncio
async def test_runner_resolution_exception_retains_safe_class_without_provider_dispatch():
    provider = Provider([])
    orch = _bare_orch(provider)

    async def forbidden(_websocket):
        raise PermissionError("Synthetic private provider details")

    orch._resolve_llm_client_for = forbidden
    result = await through_harness(orch)
    assert provider.calls == []
    assert result.outcome is HarnessOutcome.FAILED
    assert result.accounting.attempts == 0
    assert result.error_class == "PermissionError"
    assert "private" not in str(result.accounting.detail)


@pytest.mark.asyncio
async def test_ordinary_tuple_runner_api_still_preserves_success_and_failure():
    provider = Provider(
        [_Resp("Ordinary completion"), ValueError("Synthetic provider rejection")]
    )
    orch = _bare_orch(provider)
    message, usage = await orch._call_llm(
        None, [{"role": "user", "content": "Synthetic request"}]
    )
    assert message.content == "Ordinary completion" and usage.total_tokens == 10
    assert await orch._call_llm(
        None, [{"role": "user", "content": "Synthetic request"}]
    ) == (None, None)


@pytest.mark.parametrize(
    "changes",
    [
        {"attempts": True},
        {"elapsed_ms": False},
        {"total_tokens": True},
        {"usage_complete": "yes"},
    ],
)
def test_accounting_rejects_coerced_values(changes):
    values = {
        "attempts": 1,
        "elapsed_ms": 0,
        "outcome": HarnessOutcome.FAILED,
        **changes,
    }
    with pytest.raises(HarnessContractError):
        HarnessAccounting(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("deny", [False, True])
async def test_actual_context_guard_runs_before_counted_provider_dispatch(
    monkeypatch, deny
):
    from orchestrator import evidence_context

    provider = Provider([_Resp("Guarded completion")])
    orch = _bare_orch(provider)
    orch._llm_context_user_id = lambda _websocket: "synthetic-owner"
    guards = []

    async def guard():
        guards.append("checked")
        if deny:
            raise PermissionError("Synthetic denied context authority")

    async def prepare(**_kwargs):
        return guard

    async def model_call(_owner, _chat, _purpose, _model, physical, **_kwargs):
        return await physical()

    service = SimpleNamespace(
        prepare_model=prepare, usage=SimpleNamespace(model_call=model_call)
    )
    monkeypatch.setattr(evidence_context, "get_context", lambda _orch: service)
    monkeypatch.setattr(evidence_context, "enabled", lambda: True)
    result = await through_harness(
        orch, websocket=object(), stream_chat_id="synthetic-chat"
    )
    assert guards == ["checked"]
    assert len(provider.calls) == (0 if deny else 1)
    assert result.accounting.attempts == (0 if deny else 1)
    assert result.outcome is (
        HarnessOutcome.FAILED if deny else HarnessOutcome.SUCCEEDED
    )
    assert result.accounting.credential_outcome is None
