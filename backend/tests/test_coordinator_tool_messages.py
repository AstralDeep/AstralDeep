"""Tests for orchestrator/coordinator.py and task_state.py: coordinated sub-task
tool-result messages use a provider-compatible schema and stay safe/actionable after
failures or mini-turn limits.
"""

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


@pytest.mark.asyncio
async def test_coordinated_failure_is_safe_in_tool_context_and_synthesis() -> None:
    from unittest.mock import AsyncMock

    from orchestrator.task_state import TaskState

    orchestrator = _Orchestrator()
    orchestrator.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        error={"message": "PRIVATE provider HTML", "retryable": False}, result=None))
    coordinator = Coordinator(orchestrator)
    plan = CoordinatorPlan(original_message="research", subtasks=[SubTask(subtask_id="one", description="search")])
    await coordinator.execute_plan(None, plan, "c1", "u1", [], {"search_tool": "agent"})
    assert "PRIVATE" not in str(orchestrator.calls)
    assert '"retryable": false' in str(orchestrator.calls)

    await coordinator.synthesize_results(None, plan, [SubTask(
        subtask_id="failed", description="search", error="PRIVATE traceback", state=TaskState.FAILED)])
    assert "PRIVATE" not in str(orchestrator.calls)
    assert "Subtask could not complete" in str(orchestrator.calls[-1])


@pytest.mark.asyncio
async def test_failure_summary_remains_actionable_after_mini_turn_limit() -> None:
    from unittest.mock import AsyncMock

    orchestrator = _Orchestrator()
    tc = SimpleNamespace(id="failed-call", function=SimpleNamespace(name="search_tool"))
    orchestrator._call_llm = AsyncMock(return_value=(SimpleNamespace(content=None, tool_calls=[tc]), None))
    orchestrator.execute_single_tool = AsyncMock(return_value=SimpleNamespace(
        error={"code": "SEARCH_BLOCKED", "message": "PRIVATE error", "retryable": False}, result=None))
    coordinator = Coordinator(orchestrator)
    plan = CoordinatorPlan(original_message="research", subtasks=[
        SubTask(subtask_id="one", description="search"),
        SubTask(subtask_id="two", description="follow up", depends_on=["one"]),
    ])
    results = await coordinator.execute_plan(None, plan, "c1", "u1", [], {"search_tool": "agent"})
    await coordinator.synthesize_results(None, plan, results)
    assert "PRIVATE" not in str(orchestrator._call_llm.await_args_list)
    assert "Add a search provider API key" in str(orchestrator._call_llm.await_args_list)
    assert all(result.result["status"] == "error" for result in results)
