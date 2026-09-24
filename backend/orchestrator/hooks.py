"""Async lifecycle-hook registry (PRE_TOOL_USE, etc.) letting external code observe or
block/modify orchestrator behavior without changing orchestrator source, mirroring
Claude Code's hook pattern.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Dict, List, Optional

logger = logging.getLogger("Orchestrator.Hooks")


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_FAILURE = "post_tool_failure"
    PERMISSION_DENIED = "permission_denied"
    AGENT_REGISTERED = "agent_registered"
    AGENT_DISCONNECTED = "agent_disconnected"


@dataclass
class HookContext:
    event: HookEvent
    user_id: str = ""
    agent_id: str = ""
    tool_name: str = ""
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[Any] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResponse:
    action: str = "continue"
    modified_args: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None


HookHandler = Callable[[HookContext], Awaitable[Optional[HookResponse]]]


class HookManager:
    def __init__(self):
        self._hooks: Dict[HookEvent, List[HookHandler]] = defaultdict(list)

    def register(self, event: HookEvent, handler: HookHandler):
        self._hooks[event].append(handler)
        logger.info(f"Hook registered: {event.value} -> {handler.__name__}")

    def unregister(self, event: HookEvent, handler: HookHandler):
        handlers = self._hooks.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    async def emit(self, context: HookContext) -> HookResponse:
        handlers = self._hooks.get(context.event, [])
        if not handlers:
            return HookResponse()

        final = HookResponse()

        for handler in handlers:
            try:
                response = await handler(context)
                if response is None:
                    continue

                if response.action == "block" and final.action != "block":
                    final.action = "block"
                    final.reason = response.reason
                    logger.info(
                        f"Hook {handler.__name__} blocked {context.event.value}: "
                        f"tool={context.tool_name} reason={response.reason}"
                    )

                if response.action == "modify" and final.action == "continue":
                    final.action = "modify"
                    final.modified_args = response.modified_args

            except Exception as e:
                logger.error(
                    f"Hook handler {handler.__name__} failed for "
                    f"{context.event.value}: {e}"
                )

        return final

    @property
    def registered_events(self) -> List[HookEvent]:
        return [event for event, handlers in self._hooks.items() if handlers]
