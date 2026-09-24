"""Tests that the cacheable leading system-message prefix repeats byte-for-byte across
turns, reusing the orch fixture from test_wave0_live_wiring.py; the vLLM server's
prefix caching depends on this staying stable.
"""

import asyncio
import re
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.test_wave0_live_wiring import _fake_ws, _msg, _register_tool_agent, _usage


@pytest.fixture
def orch(orchestrator_factory):
    orch = orchestrator_factory()
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
