"""Exercises provider accounting and cancellation at the actual runner SDK seams."""

import asyncio
import httpx
import openai
import pytest
from orchestrator.harness.conformance import _typesafe_request
from orchestrator.harness.typesafe_adapter import TypeSafeRoutingHarness
from tests.test_harness_standard_runner import Provider, through_harness
from tests.test_llm_streaming import _bare_orch


@pytest.mark.asyncio
async def test_typesafe_after_send_cancellation_retains_attempt():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_sleep(seconds):
        entered.set()
        await release.wait()

    request = _typesafe_request("slow")
    client = request.transport["client"]
    client._sleep = blocked_sleep

    class Flags:
        def is_enabled(self, name):
            return True

    harness = TypeSafeRoutingHarness(flags=Flags())
    execution = await harness.start(request)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        result = await harness.cancel(execution)
        assert len(client.calls) == 1
        assert result.accounting.attempts == 1
    finally:
        release.set()
        await asyncio.gather(execution.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_standard_auth_rejection_retains_credential_disposition():
    response = httpx.Response(
        401,
        request=httpx.Request("POST", "https://provider.example/v1/chat/completions"),
    )
    error = openai.AuthenticationError(
        "Synthetic rejected credential",
        response=response,
        body={"error": {"code": "invalid_api_key"}},
    )
    provider = Provider([error])
    orch = _bare_orch(provider)
    orch.MAX_RETRIES = 1
    result = await through_harness(orch)
    assert len(provider.calls) == 1
    assert result.accounting.attempts == 1
    assert result.accounting.credential_outcome == "rejected"


@pytest.mark.asyncio
async def test_actual_stream_cancellation_retains_already_consumed_usage():
    import threading
    from types import SimpleNamespace
    from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
    from orchestrator.harness import HarnessTurnRequest
    from tests.test_llm_streaming import _content_chunk

    release = threading.Event()
    consumed = asyncio.Event()

    class StreamingProvider:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)

            def chunks():
                chunk = _content_chunk("Synthetic content ")
                chunk.usage = SimpleNamespace(total_tokens=4)
                yield chunk
                release.wait()

            return chunks()

    provider = StreamingProvider()
    orch = _bare_orch(provider)
    original_send = orch._safe_send

    async def send(ws, payload):
        await original_send(ws, payload)
        consumed.set()

    orch._safe_send = send

    async def invoke():
        return await orch._call_llm(
            object(), [{"role": "user", "content": "Synthetic"}], allow_stream=True
        )

    harness = StandardLLMHarness(invoke=invoke)
    execution = await harness.start(
        HarnessTurnRequest(user_id="synthetic-owner", message="Synthetic")
    )
    try:
        await asyncio.wait_for(consumed.wait(), timeout=5)
        result = await harness.cancel(execution)
        assert len(provider.calls) == result.accounting.attempts == 1
        assert result.accounting.total_tokens == 4
        assert not result.accounting.usage_complete
    finally:
        release.set()
        await asyncio.gather(execution.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_typesafe_deadline_must_not_return_terminal_with_unsettled_runner():
    from orchestrator.harness import HarnessContractError

    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    now = [0.0]

    async def uncooperative_sleep(seconds):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    request = _typesafe_request("slow")
    request.transport["client"]._sleep = uncooperative_sleep

    class Flags:
        def is_enabled(self, name):
            return True

    harness = TypeSafeRoutingHarness(
        flags=Flags(), clock=lambda: now[0], cancel_timeout_seconds=0
    )
    execution = await harness.start(request)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        now[0] = 100.0
        with pytest.raises(HarnessContractError, match="running"):
            await harness.await_result(execution)
        assert execution.running
    finally:
        release.set()
        await asyncio.gather(execution.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_typesafe_pre_send_deadline_does_not_invent_sdk_attempts(monkeypatch):
    from orchestrator.harness import HarnessOutcome

    request = _typesafe_request("success")
    client = request.transport["client"]
    monkeypatch.setattr(
        "orchestrator.typesafe_routing.runner.attempt_timeout", lambda *_args: 0
    )

    class Flags:
        def is_enabled(self, name):
            return True

    async def no_wait(_seconds):
        await asyncio.sleep(0)

    harness = TypeSafeRoutingHarness(flags=Flags(), sleep=no_wait)
    execution = await harness.start(request)
    result = await harness.await_result(execution)
    assert result.outcome is HarnessOutcome.FALLBACK
    assert not client.calls
    assert result.accounting.attempts == 0


@pytest.mark.asyncio
async def test_typesafe_settled_deadline_retains_sent_attempt():
    from orchestrator.harness import HarnessOutcome

    entered = asyncio.Event()
    now = [0.0]

    async def blocked_sleep(_seconds):
        entered.set()
        await asyncio.Event().wait()

    request = _typesafe_request("slow")
    client = request.transport["client"]
    client._sleep = blocked_sleep

    class Flags:
        def is_enabled(self, name):
            return True

    harness = TypeSafeRoutingHarness(flags=Flags(), clock=lambda: now[0])
    execution = await harness.start(request)
    await asyncio.wait_for(entered.wait(), timeout=5)
    now[0] = 100.0
    result = await harness.await_result(execution)
    assert result.outcome is HarnessOutcome.FALLBACK
    assert not execution.running
    assert len(client.calls) == result.accounting.attempts == 1
