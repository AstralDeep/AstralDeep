"""Every physical dispatch needs its own durable permit and finite bounds."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from persistent_agents.dispatch_context import (
    DispatchDenied,
    PersistentDispatchContext,
    bind_dispatch,
    current_dispatch,
)


def context(kind="tool", **overrides):
    args = {
        "owner_id": "owner", "kind": kind, "agent_id": "web-research-1",
        "tool_name": "fetch_page", "arguments": {"url": "https://example.org"},
        "timeout_seconds": 1, "max_input_bytes": 1000, "max_output_tokens": 256,
        "authorize": AsyncMock(), "start": AsyncMock(return_value="permit"),
        "observe": AsyncMock(),
    }
    args.update(overrides)
    return PersistentDispatchContext(**args)


@pytest.mark.asyncio
async def test_permit_precedes_send_and_replay_refused():
    ctx = context()
    send = AsyncMock(return_value="result")
    with bind_dispatch(ctx):
        assert current_dispatch() is ctx
        assert await ctx.invoke_tool(send) == "result"
        with pytest.raises(DispatchDenied):
            await ctx.invoke_tool(send)
    assert current_dispatch() is None
    ctx.start.assert_awaited_once()
    send.assert_awaited_once()
    ctx.observe.assert_awaited_once_with("permit", "succeeded", "result")


@pytest.mark.asyncio
async def test_denial_or_lease_loss_never_sends():
    ctx = context(authorize=AsyncMock(side_effect=DispatchDenied("revoked")))
    send = AsyncMock()
    with pytest.raises(DispatchDenied):
        await ctx.invoke_tool(send)
    send.assert_not_awaited()
    ctx.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_is_uncertain_and_charged():
    async def wait():
        await asyncio.sleep(5)
    ctx = context(timeout_seconds=0.01)
    with pytest.raises(TimeoutError):
        await ctx.invoke_tool(wait)
    ctx.observe.assert_awaited_once_with("permit", "uncertain", None)


@pytest.mark.asyncio
async def test_cancelled_external_work_retains_uncertainty():
    ctx = context()
    with pytest.raises(asyncio.CancelledError):
        await ctx.invoke_tool(AsyncMock(side_effect=asyncio.CancelledError))
    ctx.observe.assert_awaited_once_with("permit", "uncertain", None)


@pytest.mark.asyncio
async def test_model_caps_and_nested_spend():
    ctx = context(kind="model")
    send = AsyncMock(return_value="response")
    kwargs = {"messages": [{"role": "user", "content": "hi"}], "model": "configured"}
    assert await ctx.invoke_model(send, kwargs) == "response"
    assert kwargs["max_completion_tokens"] == 256
    with pytest.raises(DispatchDenied):
        await ctx.invoke_tool(send)
    tool_ctx = context()
    with pytest.raises(DispatchDenied):
        await tool_ctx.invoke_model(send, kwargs)


@pytest.mark.asyncio
async def test_oversized_model_input_refused_before_reservation():
    ctx = context(kind="model", max_input_bytes=1)
    with pytest.raises(DispatchDenied):
        await ctx.invoke_model(AsyncMock(), {"messages": ["too much"]})
    ctx.start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,agent,tool,args", [
    ("other", "web-research-1", "fetch_page", {"url": "https://example.org"}),
    ("owner", "other", "fetch_page", {"url": "https://example.org"}),
    ("owner", "web-research-1", "send", {"url": "https://example.org"}),
    ("owner", "web-research-1", "fetch_page", {"url": "https://changed.org"}),
])
async def test_exact_action_cannot_be_retargeted(owner, agent, tool, args):
    ctx = context()
    with pytest.raises(DispatchDenied):
        await ctx.validate_tool(owner, agent, tool, args)
    ctx.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_action_refreshes_authority():
    ctx = context()
    await ctx.validate_tool("owner", "web-research-1", "fetch_page", ctx.arguments)
    ctx.authorize.assert_awaited_once()
