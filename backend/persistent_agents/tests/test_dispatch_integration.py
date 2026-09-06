"""Persistent capabilities at actual Orchestrator provider/transport seams.

Construct the hub without startup to avoid changing the qualified Plane pin.
The methods under test are the live central dispatch implementations; only
external provider/transport and already-covered governance results are faked.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from llm_config.types import CredentialSource, LLMUnavailable
from orchestrator.orchestrator import Orchestrator
from persistent_agents.dispatch_context import (
    DispatchDenied,
    PersistentDispatchContext,
    bind_dispatch,
)
from shared.protocol import MCPResponse


def context(kind="tool", **changes):
    values = {"owner_id": "owner", "kind": kind, "agent_id": "reader", "tool_name": "read",
        "arguments": {"item": "release"}, "timeout_seconds": 1, "max_input_bytes": 2000,
        "max_output_tokens": 256, "authorize": AsyncMock(),
        "start": AsyncMock(return_value="permit"), "observe": AsyncMock()}
    values.update(changes)
    return PersistentDispatchContext(**values)


def transport_hub():
    hub = Orchestrator.__new__(Orchestrator)
    hub.local_agents = {"reader": object()}
    hub.agents = {}
    hub.a2a_clients = {}
    hub.agent_cards = {}
    hub.agent_urls = {}
    hub._protected_dispatch_channel = lambda socket: "persistent_assignment"
    hub._execute_in_process = AsyncMock(return_value=MCPResponse(result={"ok": True}))
    hub._execute_via_websocket = AsyncMock(return_value=MCPResponse(result={"ok": True}))
    hub._execute_via_a2a = AsyncMock(return_value=MCPResponse(result={"ok": True}))
    async def governed(*args, **kwargs):
        return await kwargs["invoke"]({"test": "authorized-transport"})
    hub._execute_governed_attempt = AsyncMock(side_effect=governed)
    return hub


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["local", "websocket", "a2a"])
async def test_actual_transport_seams_use_one_permit_after_governance(transport):
    hub = transport_hub()
    if transport != "local":
        hub.local_agents = {}
        hub.agents = {"reader": object()}
    if transport == "a2a":
        hub.agents = {}
        hub.a2a_clients = {"reader": object()}
    ctx = context()
    with bind_dispatch(ctx):
        response = await hub._dispatch_tool_call("reader", "read", {"item": "release"}, 1, None,
            protected_owner_id="owner", protected_channel="persistent_assignment")
    assert response.result == {"ok": True}
    assert ctx.consumed
    ctx.start.assert_awaited_once()
    ctx.observe.assert_awaited_once()
    hub._execute_governed_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_governance_denial_never_consumes_physical_permit():
    hub = transport_hub()
    hub._execute_governed_attempt = AsyncMock(return_value=MCPResponse(error={"message": "Denied"}))
    ctx = context()
    with bind_dispatch(ctx):
        response = await hub._dispatch_tool_call("reader", "read", {"item": "release"}, 1, None)
    assert response.error
    assert not ctx.consumed
    ctx.start.assert_not_awaited()
    hub._execute_in_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_websocket_to_a2a_fallback_cannot_reuse_persistent_reservation():
    hub = transport_hub()
    hub.local_agents = {}
    hub.agents = {"reader": object()}
    hub.a2a_clients = {"reader": object()}
    hub._execute_via_websocket.return_value = MCPResponse(error={"message": "Disconnected", "retryable": True})
    ctx = context()
    with bind_dispatch(ctx), pytest.raises(DispatchDenied, match="already_started"):
        await hub._dispatch_tool_call("reader", "read", {"item": "release"}, 1, None)
    hub._execute_via_websocket.assert_awaited_once()
    hub._execute_via_a2a.assert_not_awaited()
    ctx.start.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,agent,args", [("other", "reader", {"item": "release"}),
    ("owner", "writer", {"item": "release"}), ("owner", "reader", {"item": "other"}),
    ("owner", "__persistent_assignments__", {"item": "release"})])
async def test_logical_dispatch_rejects_changed_owner_tool_or_arguments_before_routes(owner, agent, args):
    hub = Orchestrator.__new__(Orchestrator)
    call = SimpleNamespace(function=SimpleNamespace(name="read", arguments=json.dumps(args)))
    ctx = context()
    with bind_dispatch(ctx), pytest.raises(DispatchDenied, match="binding_changed"):
        await hub.execute_single_tool(None, call, {"read": agent}, user_id=owner)
    ctx.start.assert_not_awaited()


def model_hub(completions):
    hub = Orchestrator.__new__(Orchestrator)
    hub._llm_unsupported_params = {}
    hub.llm_reasoning_effort = None
    hub.audit_recorder = None
    hub._CredentialSource = CredentialSource
    hub._LLMUnavailable = LLMUnavailable
    hub._llm_audit_principals = lambda socket: ("owner", "principal")
    hub._resolve_llm_client_for = AsyncMock(return_value=(
        SimpleNamespace(chat=SimpleNamespace(completions=completions)), CredentialSource.SYSTEM,
        SimpleNamespace(model="test-model", base_url="https://provider.invalid/v1")))
    hub._record_llm_call = AsyncMock()
    hub._record_llm_unconfigured = AsyncMock()
    hub._emit_llm_usage_report = AsyncMock()
    hub._llm_streaming_enabled = lambda: True
    hub._call_llm_streamed = AsyncMock()
    return hub


class _Completions:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Verified.", tool_calls=None))],
                               usage=SimpleNamespace(total_tokens=10))


@pytest.mark.asyncio
async def test_model_boundary_applies_hard_output_cap_and_disables_streaming():
    completions = _Completions()
    hub = model_hub(completions)
    ctx = context("model")
    with bind_dispatch(ctx):
        message, _ = await hub._call_llm(object(), [{"role": "user", "content": "Check release."}], allow_stream=True)
    assert message.content == "Verified."
    assert len(completions.calls) == 1
    assert completions.calls[0]["max_completion_tokens"] == 256
    hub._call_llm_streamed.assert_not_awaited()
    ctx.start.assert_awaited_once()
    ctx.observe.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_failure_cannot_probe_or_retry_with_same_reservation():
    completions = _Completions(RuntimeError("unsupported response_format; try again"))
    hub = model_hub(completions)
    ctx = context("model")
    with bind_dispatch(ctx), pytest.raises(RuntimeError):
        await hub._call_llm(None, [{"role": "user", "content": "Check release."}], response_format={"type": "json_object"})
    assert len(completions.calls) == 1
    ctx.start.assert_awaited_once()
    assert ctx.observe.call_args.args[1] == "uncertain"


@pytest.mark.asyncio
async def test_stop_at_model_permit_boundary_prevents_provider_call():
    completions = _Completions()
    hub = model_hub(completions)
    ctx = context("model", authorize=AsyncMock(side_effect=DispatchDenied("assignment_stopped")))
    with bind_dispatch(ctx), pytest.raises(DispatchDenied):
        await hub._call_llm(None, [{"role": "user", "content": "Check release."}])
    assert completions.calls == []
    ctx.start.assert_not_awaited()
