"""Runs native factory clients through the existing orchestrator, audit, streaming and invocation seams. Provider bytes and encrypted repository fixtures remain synthetic."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from llm_config.tests.native_fixtures import native_offline as native_offline

from llm_config import audit_events
from llm_config.client_factory import build_llm_client
from llm_config.invocation import InvocationState, invoke_accounted
from llm_config.types import CredentialSource, LLMUnavailable
from llm_config.tests.test_native_factory import enabled as enabled, selected
from llm_config.tests.test_native_wire import TOOLS, response, stream_bytes
from orchestrator.orchestrator import Orchestrator


async def orchestrator(store, monkeypatch, recorder):
    from orchestrator import model_router
    monkeypatch.setattr(model_router, "router_enabled", lambda: False)
    config = await selected(store)
    client, source, resolved = build_llm_client(config, CredentialSource.USER)
    orch = Orchestrator.__new__(Orchestrator)
    orch._llm_unsupported_params = {}
    orch.llm_reasoning_effort = None
    orch.audit_recorder = recorder
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    orch._llm_audit_principals = lambda _: ("alice", "alice")
    orch._resolve_llm_client_for = AsyncMock(return_value=(client, source, resolved))
    orch._record_llm_call = audit_events.record_llm_call
    orch._record_llm_unconfigured = audit_events.record_llm_unconfigured
    orch._emit_llm_usage_report = AsyncMock()
    orch._emit_narrative_frame = AsyncMock()
    orch._llm_streaming_enabled = lambda: True
    return orch


async def test_native_call_uses_standard_invocation_and_owner_audit(store, enabled, fake_recorder, monkeypatch):
    from shared import external_http
    calls = []

    async def wire(*_, **kwargs):
        calls.append(kwargs)
        yield json.dumps(response()).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    result = await invoke_accounted(lambda: orch._call_llm(None, [{"role": "user", "content": "plot"}], TOOLS), InvocationState())
    assert result.outcome == "succeeded" and result.attempts == 1 and result.total_tokens == 63
    assert result.message.tool_calls[0].function.name == "plot"
    event = fake_recorder.record.await_args.args[0]
    assert event.actor_user_id == "alice" and event.inputs_meta["provider"] == "anthropic-native"
    assert event.outputs_meta["total_tokens"] == 63
    assert len(calls) == 1


async def test_native_http_failure_never_retries_or_changes_tool_choice(store, enabled, fake_recorder, monkeypatch):
    from shared import external_http
    calls = []

    async def wire(*_, **__):
        calls.append(1)
        error = external_http.ExternalHttpError("safe_upstream_status")
        error.status_code = 429
        raise error
        yield b""

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    message, usage = await orch._call_llm(None, [{"role": "user", "content": "plot"}], TOOLS,
        tool_choice={"type": "function", "function": {"name": "plot"}})
    assert message is None and usage is None and calls == [1]
    assert fake_recorder.record.await_args.args[0].outcome == "failure"


async def test_native_stream_tool_and_usage_normalize_through_existing_assembler(store, enabled, fake_recorder, monkeypatch):
    from shared import external_http

    async def wire(*_, **kwargs):
        assert json.loads(kwargs["body"])["stream"] is True
        for offset in range(0, len(stream_bytes()), 11):
            yield stream_bytes()[offset:offset + 11]

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    result = await invoke_accounted(lambda: orch._call_llm(object(), [{"role": "user", "content": "plot"}], TOOLS,
        allow_stream=True, stream_chat_id="fixture-chat"), InvocationState())
    assert result.message.tool_calls[0].id == "toolu_plot"
    assert result.total_tokens == 63 and result.attempts == 1


async def test_native_stream_cancellation_closes_wire_and_does_not_retry(store, enabled, fake_recorder, monkeypatch):
    from shared import external_http
    entered, closed, blocker = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def wire(*_, **__):
        calls.append(1)
        entered.set()
        try:
            await blocker.wait()
            yield b""
        finally:
            closed.set()

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    task = asyncio.create_task(orch._call_llm(object(), [{"role": "user", "content": "plot"}], TOOLS,
        allow_stream=True, stream_chat_id="fixture-chat"))
    while not entered.is_set() and not task.done():
        await asyncio.sleep(0)
    if task.done():
        await task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and calls == [1]


async def test_native_foreign_owner_cannot_dispatch_with_another_owners_bound_client(store, enabled, fake_recorder, monkeypatch):
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    orch._llm_audit_principals = lambda _: ("bob", "bob")
    message, usage = await orch._call_llm(None, [{"role": "user", "content": "private question"}])
    assert message is None and usage is None
    assert fake_recorder.record.await_args.args[0].event_class == "llm_unconfigured"


async def test_native_delivery_rechecks_record_after_awaited_audit(store, credential_plane, enabled, fake_recorder, monkeypatch):
    from shared import external_http

    async def wire(*_, **__):
        yield json.dumps(response(content=[{"type": "text", "text": "must not deliver"}], stop_reason="end_turn")).encode()

    async def audited(event):
        if event.outcome == "success":
            credential_plane.users.pop("alice")

    fake_recorder.record.side_effect = audited
    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    result = await invoke_accounted(lambda: orch._call_llm(None, [{"role": "user", "content": "question"}]), InvocationState())
    assert result.outcome == "failed" and result.message is None
    assert result.attempts == 1 and result.total_tokens == 63


async def test_native_stream_revocation_closes_before_queued_delivery_or_tool_calls(store, credential_plane, enabled, fake_recorder, monkeypatch):
    from shared import external_http
    from llm_config.tests.test_native_wire import event, parts
    closed = asyncio.Event()

    async def wire(*_, **__):
        try:
            raw = b"".join([event("message_start", message=response(content=[], stop_reason=None)),
                event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
                event("content_block_delta", index=0, delta={"type": "text_delta", "text": "must not deliver"})])
            async for value in parts(raw):
                yield value
            credential_plane.users.pop("alice")
            yield event("content_block_stop", index=0)
        finally:
            closed.set()

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    message, usage = await orch._call_llm(object(), [{"role": "user", "content": "question"}],
        allow_stream=True, stream_chat_id="fixture-chat")
    assert message is None and usage is None and closed.is_set()
    orch._emit_narrative_frame.assert_not_awaited()


async def test_native_malformed_stream_never_falls_back_to_a_second_physical_call(store, enabled, fake_recorder, monkeypatch):
    from shared import external_http
    from llm_config.tests.test_native_wire import event
    calls = []

    async def wire(*_, **kwargs):
        payload = json.loads(kwargs["body"])
        calls.append(payload["stream"])
        if payload["stream"]:
            yield event("message_start", message=response(content=[], stop_reason=None))
        else:
            yield json.dumps(response(content=[{"type": "text", "text": "duplicate fallback"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    message, usage = await orch._call_llm(object(), [{"role": "user", "content": "question"}],
        allow_stream=True, stream_chat_id="fixture-chat")
    assert message is None and usage is None and calls == [True]


async def test_refused_native_tool_response_still_charges_one_reported_attempt(store, enabled, fake_recorder, monkeypatch):
    from shared import external_http

    async def wire(*_, **__):
        yield json.dumps(response(content=[{"type": "tool_use", "id": "t", "name": "plot", "input": {"x": "wrong"}}])).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    result = await invoke_accounted(lambda: orch._call_llm(None, [{"role": "user", "content": "plot"}], TOOLS), InvocationState())
    assert result.outcome == "failed" and result.message is None and result.attempts == 1
    assert result.total_tokens == 63 and result.usage_complete is True
    assert fake_recorder.record.await_args.args[0].outputs_meta["total_tokens"] == 63


async def test_native_persistent_model_call_retains_permit_output_cap_and_denial(store, enabled, fake_recorder, monkeypatch):
    from persistent_agents.tests.test_dispatch_integration import context
    from persistent_agents.dispatch_context import DispatchDenied, bind_dispatch
    from shared import external_http
    calls = []

    async def wire(*_, **kwargs):
        calls.append(json.loads(kwargs["body"]))
        yield json.dumps(response(content=[{"type": "text", "text": "bounded task"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    ctx = context("model", owner_id="alice")
    with bind_dispatch(ctx):
        message, _ = await orch._call_llm(object(), [{"role": "user", "content": "task"}], allow_stream=True)
    assert message.content == "bounded task" and len(calls) == 1
    assert calls[0]["max_tokens"] == 256 and calls[0]["stream"] is False
    assert ctx.start.await_count == 1 and ctx.observe.await_count == 1
    denied = context("model", owner_id="alice", authorize=AsyncMock(side_effect=DispatchDenied("denied")))
    with bind_dispatch(denied), pytest.raises(DispatchDenied):
        await orch._call_llm(None, [{"role": "user", "content": "denied task"}])
    assert len(calls) == 1 and denied.start.await_count == 0


@pytest.mark.parametrize("kind", ["structured", "images"])
async def test_compatibility_capability_caches_cannot_silently_downgrade_native_requests(store, enabled, fake_recorder, monkeypatch, kind):
    from shared import external_http
    from llm_config.tests.test_native_wire import MODEL
    calls = []

    async def wire(*_, **__):
        calls.append(1)
        yield json.dumps(response(content=[{"type": "text", "text": "silently downgraded"}], stop_reason="end_turn")).encode()

    monkeypatch.setattr(external_http, "async_post", wire)
    orch = await orchestrator(store, monkeypatch, fake_recorder)
    key = ("https://api.anthropic.com/v1", MODEL)
    orch._llm_unsupported_params = {key: {"response_format", "reasoning_effort"}}
    orch._llm_vision_unsupported = {key}
    messages = [{"role": "user", "content": "question"}]
    kwargs = {"response_format": {"type": "json_object"}}
    if kind == "images":
        messages[0]["content"] = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}]
        kwargs = {}
    message, usage = await orch._call_llm(None, messages, **kwargs)
    assert message is None and usage is None and calls == []
