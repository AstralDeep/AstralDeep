"""Provider-compatible tool-result messages in coordinated subtasks."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from orchestrator.coordinator import Coordinator, CoordinatorPlan, SubTask


class _Orchestrator:
    def __init__(self) -> None:
        self.calls: list[list[object]] = []

    async def _call_llm(self, _websocket, messages, _tools=None):
        self.calls.append(deepcopy(messages))
        if len(self.calls) == 1:
            tool_call = SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="search_tool"),
            )
            return SimpleNamespace(content=None, tool_calls=[tool_call]), None
        return SimpleNamespace(content="finished", tool_calls=[]), None

    async def execute_single_tool(self, *_args, **_kwargs):
        return SimpleNamespace(error=None, result={"value": "ok"})


@pytest.mark.asyncio
async def test_coordinated_tool_result_uses_supported_provider_schema() -> None:
    orchestrator = _Orchestrator()
    coordinator = Coordinator(orchestrator)
    plan = CoordinatorPlan(
        original_message="run one task",
        subtasks=[SubTask(subtask_id="one", description="search")],
    )

    await coordinator.execute_plan(
        None,
        plan,
        "chat-1",
        "user-1",
        [{"type": "function", "function": {"name": "search_tool"}}],
        {"search_tool": "agent"},
    )

    tool_messages = [
        message
        for message in orchestrator.calls[1]
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    assert tool_messages == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"value": "ok"}',
        }
    ]
