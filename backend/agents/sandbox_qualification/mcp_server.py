"""Bridges one fixed diagnostic call to the host loop with its unforgeable dispatch context.
The actuator claims the ordinary LETS permit immediately before launching the bounded effect.
"""

import asyncio

from orchestrator.sandbox_tool_sessions import (
    AGENT_ID,
    TOOL_ID,
    PUBLIC_MODES,
    DispatchContext,
    ToolDenied,
)
from shared.agent_runtime import AgentRuntime
from shared.protocol import MCPResponse

TOOL_REGISTRY = {
    TOOL_ID: {
        "description": "Prepare an owner-bound pilot, run one fixed harmless sandbox diagnostic, or close it. Human approval required.",
        "scope": "tools:write",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": list(PUBLIC_MODES)},
                "qualification_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{32}$",
                    "maxLength": 32,
                },
            },
            "required": ["mode"],
        },
    },
}


class MCPServer:
    protected_executor_at_actuator = True

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.tools = TOOL_REGISTRY

    def get_tool_list(self):
        return [{"name": name, **info} for name, info in self.tools.items()]

    def process_request(self, request):
        if request.method == "tools/list":
            return MCPResponse(
                request_id=request.request_id, result={"tools": self.get_tool_list()}
            )
        params = request.params or {}
        arguments = params.get("arguments")
        runtime = arguments.get("_runtime") if type(arguments) is dict else None
        context = getattr(request, "_sandbox_qualification_context", None)
        if (
            request.method != "tools/call"
            or params.get("name") != TOOL_ID
            or type(runtime) is not AgentRuntime
            or runtime.agent_id != AGENT_ID
            or runtime.request_id != request.request_id
            or runtime.loop.is_closed()
            or type(context) is not DispatchContext
            or context.request is not request
            or not context.current()
        ):
            return self._refused(request.request_id)
        public = getattr(request, "_protected_wire_arguments", None)
        capability = (request.caller_capabilities or {}).get("astraldeep.lets/v1")
        future = asyncio.run_coroutine_threadsafe(
            context.service.execute(context, public, capability), runtime.loop
        )
        try:
            result = future.result(timeout=180)
            return MCPResponse(request_id=request.request_id, result=result)
        except Exception as error:
            future.cancel()
            return self._refused(
                request.request_id,
                error.code if isinstance(error, ToolDenied) else None,
            )

    @staticmethod
    def _refused(request_id, code=None):
        return MCPResponse(
            request_id=request_id,
            error={
                "code": code or "sandbox_qualification_denied",
                "message": "Sandbox qualification is unavailable or not authorized.",
                "retryable": False,
            },
        )


_CANONICAL_PROCESS_REQUEST = MCPServer.process_request
