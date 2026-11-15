"""Drives the ordinary chat loop to show a repeated failing call is warned about once and then stops the turn."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from orchestrator import repeat_guard
from shared.protocol import MCPResponse
from tests import test_turn_seams_loop as seams

orch = seams.orch


@pytest.mark.asyncio
async def test_chat_loop_warns_once_then_stops_after_six_identical_failures(orch, user_skills_disabled):
    seams._register(orch)
    chat_id = f"repeat-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(orch.history.create_chat, chat_id, user_id="seam-user")
    planned = []

    async def planner(websocket, messages, tools_desc=None, **kwargs):
        planned.append([m for m in messages if isinstance(m, dict) and m.get("role") == "system"])
        return seams._msg(tool_calls=[seams._tc(cid=f"c{len(planned)}")]), seams._usage()

    orch._call_llm = planner
    orch.execute_single_tool = AsyncMock(return_value=MCPResponse(error={"message": "boom"}))
    await orch.handle_chat_message(seams._ws(orch), "do it", chat_id, user_id="seam-user")

    assert len(planned) == 6
    assert orch.execute_single_tool.await_count == 6
    warnings = [m for m in planned[-1] if m.get("content") == repeat_guard.warn_text("search_tool")]
    assert len(warnings) == 1
    notices = [call.args[1][0]["message"] for call in orch.send_ui_render.await_args_list
               if call.args[1] and isinstance(call.args[1][0], dict) and call.args[1][0].get("type") == "alert"]
    assert repeat_guard.STOP_NOTICE in notices
