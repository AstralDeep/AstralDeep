"""
Coordinator/Worker Pattern — Decompose complex requests into sub-tasks.

When the LLM detects a complex multi-step request, the coordinator can
break it into independent sub-tasks (workers), execute them in parallel
or sequentially, and synthesize the results.

Inspired by Claude Code's coordinator mode where one agent manages workers
via SendMessage, and workers operate independently.

Usage:
    coordinator = Coordinator(orchestrator)
    plan = await coordinator.create_plan(message, tools_desc, tool_to_agent)
    results = await coordinator.execute_plan(websocket, plan, chat_id, user_id)
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any

from orchestrator.task_state import TaskState

logger = logging.getLogger("Orchestrator.Coordinator")


class SubTaskType(str, Enum):
    PARALLEL = "parallel"    # Can run concurrently with other subtasks
    SEQUENTIAL = "sequential"  # Must wait for previous subtasks to complete


@dataclass
class SubTask:
    """A single unit of work within a coordinated plan."""
    subtask_id: str
    description: str
    task_type: SubTaskType = SubTaskType.PARALLEL
    tool_hints: List[str] = field(default_factory=list)  # Suggested tools
    depends_on: List[str] = field(default_factory=list)  # subtask_ids this depends on
    result: Optional[Any] = None
    error: Optional[str] = None
    state: TaskState = TaskState.PENDING


@dataclass
class CoordinatorPlan:
    """A decomposed plan for a complex request."""
    original_message: str
    subtasks: List[SubTask] = field(default_factory=list)
    synthesis_prompt: str = ""  # How to combine results into final response

    def get_parallel_groups(self) -> List[List[SubTask]]:
        """Return subtasks grouped into execution waves.

        Within each wave, all subtasks can run in parallel.
        Between waves, ordering is preserved (sequential dependencies).
        """
        completed_ids = set()
        waves = []
        remaining = list(self.subtasks)

        while remaining:
            wave = []
            still_remaining = []
            for st in remaining:
                deps_met = all(d in completed_ids for d in st.depends_on)
                if deps_met:
                    wave.append(st)
                else:
                    still_remaining.append(st)
            if not wave:
                # Circular dependency or unresolvable — just run everything
                waves.append(still_remaining)
                break
            waves.append(wave)
            completed_ids.update(st.subtask_id for st in wave)
            remaining = still_remaining

        return waves


class Coordinator:
    """Orchestrates multi-step task decomposition and execution."""

    # System prompt for the planning LLM call
    PLANNING_PROMPT = """You are a task planner. Given a user request and available tools,
decompose it into independent sub-tasks that can be executed in parallel where possible.

Return a JSON object with this structure:
{
  "subtasks": [
    {
      "id": "st_1",
      "description": "What this subtask should accomplish",
      "tool_hints": ["tool_name_1"],
      "depends_on": [],
      "type": "parallel"
    },
    {
      "id": "st_2",
      "description": "What this subtask should accomplish",
      "tool_hints": ["tool_name_2"],
      "depends_on": ["st_1"],
      "type": "sequential"
    }
  ],
  "synthesis": "How to combine the results into a final answer"
}

Rules:
- Use "parallel" type when subtasks are independent.
- Use "sequential" type and specify "depends_on" when a subtask needs results from another.
- Each subtask should map to 1-2 tool calls.
- If the request is simple enough for a single tool call, return a single subtask.
- tool_hints should reference actual tool names from the available tools list.
"""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def should_coordinate(self, message: str, tools_desc: List[Dict]) -> bool:
        """Determine if a message is complex enough to warrant coordination.

        Simple heuristic: if the message contains multiple distinct requests
        (conjunctions, lists, or multi-step phrasing), use coordination.
        """
        complexity_signals = [
            " and then ",
            " after that ",
            " followed by ",
            " first ",
            " next ",
            " finally ",
            ", then ",
        ]
        signal_count = sum(1 for s in complexity_signals if s in message.lower())

        # Also check for numbered lists
        import re
        numbered = len(re.findall(r'\d+[\.\)]\s', message))

        return (signal_count >= 2) or (numbered >= 2)

    async def create_plan(
        self,
        websocket,
        message: str,
        tools_desc: List[Dict],
        tool_to_agent: Dict[str, str],
    ) -> Optional[CoordinatorPlan]:
        """Ask the LLM to decompose a complex request into a plan."""
        tool_summary = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']}"
            for t in tools_desc
        )

        planning_messages = [
            {"role": "system", "content": self.PLANNING_PROMPT},
            {"role": "user", "content": f"Available tools:\n{tool_summary}\n\nUser request: {message}"},
        ]

        try:
            response, _ = await self.orchestrator._call_llm(websocket, planning_messages)
            if not response or not response.content:
                return None

            # Parse the JSON plan
            content = response.content.strip()
            # Handle markdown code blocks
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            plan_data = json.loads(content)
            subtasks = []
            for st_data in plan_data.get("subtasks", []):
                subtasks.append(SubTask(
                    subtask_id=st_data.get("id", f"st_{len(subtasks)}"),
                    description=st_data.get("description", ""),
                    task_type=SubTaskType(st_data.get("type", "parallel")),
                    tool_hints=st_data.get("tool_hints", []),
                    depends_on=st_data.get("depends_on", []),
                ))

            return CoordinatorPlan(
                original_message=message,
                subtasks=subtasks,
                synthesis_prompt=plan_data.get("synthesis", "Summarize all results."),
            )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse coordinator plan: {e}")
            return None

    async def execute_plan(
        self,
        websocket,
        plan: CoordinatorPlan,
        chat_id: str,
        user_id: str,
        tools_desc: List[Dict],
        tool_to_agent: Dict[str, str],
    ) -> List[SubTask]:
        """Execute a coordinated plan wave by wave.

        Each wave is a group of subtasks that can run in parallel.
        Between waves, results from previous subtasks are available.
        """
        waves = plan.get_parallel_groups()
        all_results: Dict[str, SubTask] = {}

        for wave_idx, wave in enumerate(waves):
            logger.info(f"Coordinator wave {wave_idx + 1}/{len(waves)}: {len(wave)} subtasks")

            # Build per-subtask context including results from dependencies
            async def execute_subtask(subtask: SubTask) -> SubTask:
                # Build context from dependent results
                dep_context = ""
                for dep_id in subtask.depends_on:
                    dep = all_results.get(dep_id)
                    if dep and dep.result:
                        dep_context += f"\nResult from '{dep.description}': {json.dumps(dep.result)[:2000]}\n"

                sub_message = subtask.description
                if dep_context:
                    sub_message = f"{subtask.description}\n\nContext from previous steps:{dep_context}"

                # Filter tools to just the hinted ones (if hints provided)
                if subtask.tool_hints:
                    sub_tools = [t for t in tools_desc if t["function"]["name"] in subtask.tool_hints]
                    if not sub_tools:
                        sub_tools = tools_desc  # Fallback to all tools
                else:
                    sub_tools = tools_desc

                # Execute a mini Re-Act loop for this subtask (max 3 turns)
                subtask.state = TaskState.RUNNING
                messages = [
                    {"role": "system", "content": "You are executing a single subtask. Complete it and return the result."},
                    {"role": "user", "content": sub_message},
                ]

                for turn in range(3):
                    llm_msg, _ = await self.orchestrator._call_llm(websocket, messages, sub_tools)
                    if not llm_msg:
                        subtask.error = "LLM returned no response"
                        subtask.state = TaskState.FAILED
                        return subtask

                    if llm_msg.tool_calls:
                        messages.append(llm_msg)
                        for tc in llm_msg.tool_calls:
                            res = await self.orchestrator.execute_single_tool(
                                websocket, tc, tool_to_agent, chat_id, user_id=user_id
                            )
                            content_str = "No output"
                            if res:
                                if res.error:
                                    content_str = f"Error: {res.error.get('message')}"
                                elif res.result:
                                    content_str = json.dumps(res.result.get("_data", res.result) if isinstance(res.result, dict) else res.result)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": content_str,
                            })
                            subtask.result = res.result if res else None
                    else:
                        # Final response from this subtask
                        subtask.result = {"summary": llm_msg.content}
                        subtask.state = TaskState.COMPLETED
                        return subtask

                subtask.state = TaskState.COMPLETED
                return subtask

            # Execute wave in parallel
            if len(wave) == 1:
                result = await execute_subtask(wave[0])
                all_results[result.subtask_id] = result
            else:
                wave_results = await asyncio.gather(
                    *[execute_subtask(st) for st in wave],
                    return_exceptions=True,
                )
                for i, result in enumerate(wave_results):
                    if isinstance(result, Exception):
                        wave[i].error = str(result)
                        wave[i].state = TaskState.FAILED
                        all_results[wave[i].subtask_id] = wave[i]
                    else:
                        all_results[result.subtask_id] = result

        return list(all_results.values())

    async def synthesize_results(
        self,
        websocket,
        plan: CoordinatorPlan,
        completed_subtasks: List[SubTask],
    ) -> str:
        """Generate a final synthesis from all subtask results."""
        results_summary = []
        for st in completed_subtasks:
            status = "completed" if st.state == TaskState.COMPLETED else "failed"
            result_str = json.dumps(st.result)[:2000] if st.result else st.error or "no result"
            results_summary.append(f"- {st.description} [{status}]: {result_str}")

        synthesis_messages = [
            {
                "role": "system",
                "content": (
                    "You are synthesizing results from multiple completed subtasks. "
                    "Provide a cohesive summary that addresses the original user request. "
                    f"Synthesis guidance: {plan.synthesis_prompt}"
                ),
            },
            {
                "role": "user",
                "content": f"Original request: {plan.original_message}\n\nSubtask results:\n" + "\n".join(results_summary),
            },
        ]

        response, _ = await self.orchestrator._call_llm(websocket, synthesis_messages)
        if response and response.content:
            return response.content
        return "Task completed but synthesis failed."
