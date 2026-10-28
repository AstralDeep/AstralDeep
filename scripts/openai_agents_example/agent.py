"""Binds the Astral Work client to the OpenAI Agents SDK as strict function tools and
disables cloud trace export before any request can run. The external SDK module is
imported lazily so this package stays importable without it, and no SDK feature
(guardrails, tracing, handoffs) is relied on for server authority — the server keeps
enforcing scopes, admission, PHI, and confirmation gates itself.
"""

from __future__ import annotations

from typing import Any

from scripts.openai_agents_example.client import AstralWorkClient

AGENT_TOOL_NAMES = (
    "astral_submit_operation", "astral_get_operation", "astral_list_operations",
    "astral_get_operation_events", "astral_get_artifact", "astral_cancel_operation",
)
EXCLUDED_TOOL_NAMES = frozenset({
    "astral_pause_operation",
    "astral_emergency_status", "astral_emergency_stop", "astral_emergency_resume",
    "astral_subscribe_operation_completion",
    "astral_list_operation_completion_subscriptions",
    "astral_cancel_operation_completion_subscription",
})
AGENT_INSTRUCTIONS = (
    "You operate Astral Work on behalf of one owner using the attached tools. "
    "Submit operations with a fresh idempotency key, poll astral_get_operation_events "
    "within the caller's bounds, report dispositions exactly as the server returns "
    "them, and never invent completed, failed, or cancelled states. Human-only and "
    "completion-subscription actions are intentionally unavailable."
)
_SDK_INSTALL_HINT = (
    "the pinned openai-agents package is required; install "
    "scripts/openai_agents_example/requirements.lock.txt"
)
_SDK_COLLISION_HINT = (
    "import agents resolved to Astral's backend.agents namespace instead of the OpenAI "
    "Agents SDK; run in the isolated environment from scripts/openai_agents_example/requirements.lock.txt"
)
_SDK_MARKERS = ("set_tracing_disabled", "set_trace_processors", "function_tool", "Agent")

_SUBMIT_DESCRIPTION = "Submit one Astral Work chat operation for the credential owner."
_GET_DESCRIPTION = "Read one Astral Work operation's current server-reported status."
_LIST_DESCRIPTION = "List the credential owner's recent Astral Work operations."
_EVENTS_DESCRIPTION = "Poll one Astral Work operation for a server-reported revision change."
_ARTIFACT_DESCRIPTION = "Read one Astral Work operation's retained result artifact."
_CANCEL_DESCRIPTION = "Cancel one Astral Work operation with a fresh submission id."


def _load_agents_module() -> Any:
    try:
        import agents
    except ImportError as exc:
        raise ImportError(_SDK_INSTALL_HINT) from exc
    if any(not hasattr(agents, marker) for marker in _SDK_MARKERS):
        raise ImportError(_SDK_COLLISION_HINT)
    return agents


class AstralAgentSession:
    def __init__(self, *, client: AstralWorkClient, model: Any = None):
        self.client = client
        self.model = model
        self.tracing_disabled_at: float | None = None
        self._agents = None
        self._configure_tracing()

    def _configure_tracing(self) -> None:
        self._agents = _load_agents_module()
        self._agents.set_tracing_disabled(True)
        self._agents.set_trace_processors([])
        self.tracing_disabled_at = self.client.clock()

    @property
    def tracing_before_first_request(self) -> bool:
        if not self.client.requests:
            return True
        return self.tracing_disabled_at <= min(item["at"] for item in self.client.requests)

    def tools(self) -> list[Any]:
        agents = self._agents
        client = self.client

        @agents.function_tool(
            strict_mode=True, use_docstring_info=False,
            description_override=_SUBMIT_DESCRIPTION,
        )
        async def astral_submit_operation(
            idempotency_key: str, name: str, instructions: str,
            conversation_id: str | None = None, deadline_in_seconds: int | None = None,
        ) -> dict[str, Any]:
            return await client.submit(
                idempotency_key=idempotency_key, name=name, instructions=instructions,
                conversation_id=conversation_id, deadline_in_seconds=deadline_in_seconds,
            )

        @agents.function_tool(
            strict_mode=True, use_docstring_info=False,
            description_override=_GET_DESCRIPTION,
        )
        async def astral_get_operation(operation_id: str) -> dict[str, Any]:
            return await client.get(operation_id)

        @agents.function_tool(
            strict_mode=True, use_docstring_info=False,
            description_override=_LIST_DESCRIPTION,
        )
        async def astral_list_operations(
            limit: int = 50, after_id: str | None = None,
        ) -> dict[str, Any]:
            return await client.list(limit=limit, after_id=after_id)

        @agents.function_tool(
            strict_mode=True, use_docstring_info=False,
            description_override=_EVENTS_DESCRIPTION,
        )
        async def astral_get_operation_events(
            operation_id: str, after_revision: int | None = None,
        ) -> dict[str, Any]:
            return await client.events(operation_id, after_revision=after_revision)

        @agents.function_tool(
            strict_mode=True, use_docstring_info=False,
            description_override=_ARTIFACT_DESCRIPTION,
        )
        async def astral_get_artifact(operation_id: str) -> dict[str, Any]:
            return await client.artifact(operation_id)

        @agents.function_tool(
            strict_mode=True, use_docstring_info=False,
            description_override=_CANCEL_DESCRIPTION,
        )
        async def astral_cancel_operation(
            operation_id: str, submission_id: str, expected_revision: int,
        ) -> dict[str, Any]:
            return await client.cancel(
                operation_id, submission_id=submission_id, expected_revision=expected_revision,
            )

        return [
            astral_submit_operation, astral_get_operation, astral_list_operations,
            astral_get_operation_events, astral_get_artifact, astral_cancel_operation,
        ]

    def build_agent(self) -> Any:
        agent = self._agents.Agent(
            name="astral-work-agent", instructions=AGENT_INSTRUCTIONS,
            tools=self.tools(), model=self.model,
        )
        return agent


__all__ = [
    "AGENT_INSTRUCTIONS",
    "AGENT_TOOL_NAMES",
    "EXCLUDED_TOOL_NAMES",
    "AstralAgentSession",
]
