"""The cacheable prompt prefix must repeat byte-for-byte across turns.

The deployment's LLM is a vLLM-family OpenAI-compatible server, whose automatic
prefix caching is server-side and hash-based: it needs no client opt-in, only a
token prefix that actually repeats. The leading system message plus the tool
definitions is the large majority of a prompt (~16k of ~20k tokens is tool
schemas alone), so anything per-turn and random placed in front of that block
throws the reuse away on every call.

Datamarking's per-turn sentinel used to be appended to the leading system
message and did exactly that. It now rides a trailing system message.
"""

import asyncio
import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_wave0_live_wiring import _fake_ws, _msg, _register_tool_agent, _usage


@pytest.fixture
def orch():
    """A chat-loop orchestrator with delivery and heartbeat stubbed out."""
    from orchestrator.orchestrator import Orchestrator

    orch = Orchestrator()
    orch._llm_store.set_sync("wave0-user", provider="custom",
                             base_url="http://test.invalid/v1",
                             model="test-model", api_key="test-key")
    orch.audit_recorder = MagicMock()
    orch.audit_recorder.record = AsyncMock()
    orch._record_llm_call = AsyncMock()
    orch._record_llm_unconfigured = AsyncMock()
    orch._safe_send = AsyncMock()
    orch.send_ui_render = AsyncMock()
    fake_hb = MagicMock()
    fake_hb.cancel = MagicMock()
    orch._start_heartbeat = AsyncMock(return_value=fake_hb)
    orch._send_or_replace_components = AsyncMock()
    orch._emit_llm_usage_report = AsyncMock()
    orch._deliver_round_components = AsyncMock(return_value=[])
    return orch


@pytest.mark.asyncio
async def test_leading_system_message_is_identical_across_turns(orch):
    """Two turns in one chat must present the same leading system block."""

    _register_tool_agent(orch)
    ws = _fake_ws(orch)
    chat_id = f"prefix-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(
        orch.history.create_chat, chat_id, user_id="wave0-user")

    leading = []
    tool_blocks = []

    async def fake_call_llm(websocket, messages, tools_desc=None, temperature=None,
                            feature="tool_dispatch"):
        leading.append(messages[0]["content"])
        tool_blocks.append(tools_desc)
        return _msg(content="Done."), _usage()

    orch._call_llm = fake_call_llm

    await orch.handle_chat_message(
        ws, "first question", chat_id, user_id="wave0-user")
    await orch.handle_chat_message(
        ws, "second question", chat_id, user_id="wave0-user")

    assert len(leading) >= 2

    for text in leading:
        assert not re.search(r"<<UNTRUSTED [0-9a-f]{32}>>", text), (
            "the per-turn sentinel must not sit in the cacheable prefix"
        )

    assert leading[0] == leading[-1], (
        "leading system message drifted between turns — prefix caching is lost"
    )
    assert tool_blocks[0] == tool_blocks[-1], (
        "tool definitions drifted between turns — prefix caching is lost"
    )

    await asyncio.to_thread(
        orch.history.delete_chat, chat_id, user_id="wave0-user")
