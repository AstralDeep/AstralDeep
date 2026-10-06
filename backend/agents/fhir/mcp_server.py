"""MCP server for the FHIR agent: routes tools/list and tools/call to mcp_tools.py, turns
tool error payloads into coded MCP errors and serves the streaming feed's first chunk to
callers on the non-streaming path.
"""

import asyncio
import inspect
import logging
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.protocol import MCPRequest, MCPResponse

from agents.fhir.mcp_tools import TOOL_REGISTRY

logger = logging.getLogger('FhirAgentMCPServer')


class MCPServer:
    def __init__(self):
        self.tools = TOOL_REGISTRY

    def get_tool_list(self) -> list:
        return [
            {
                "name": name,
                "description": info["description"],
                "input_schema": info.get("input_schema", {"type": "object", "properties": {}}),
            }
            for name, info in self.tools.items()
        ]

    def process_request(self, request: MCPRequest) -> MCPResponse:
        if request.method == "tools/list":
            return MCPResponse(request_id=request.request_id, result={"tools": self.get_tool_list()})
        if request.method != "tools/call":
            return MCPResponse(
                request_id=request.request_id,
                error={"code": -32601, "message": f"Unknown method: {request.method}", "retryable": False},
            )
        tool_name = request.params.get("name", "")
        arguments = dict(request.params.get("arguments", {}) or {})
        if tool_name not in self.tools:
            return MCPResponse(
                request_id=request.request_id,
                error={"code": -32601, "message": f"Unknown tool: {tool_name}", "retryable": False},
            )
        tool_fn = self.tools[tool_name]["function"]
        try:
            if inspect.isasyncgenfunction(tool_fn):
                return self._first_chunk(request.request_id, tool_fn, arguments)
            return self._respond(request.request_id, tool_fn(**arguments))
        except Exception as error:  # noqa: BLE001
            logger.error("Tool '%s' raised %s", tool_name, type(error).__name__)
            return MCPResponse(
                request_id=request.request_id,
                error={"code": -32603, "message": "The FHIR tool failed unexpectedly", "retryable": False},
            )

    @staticmethod
    def _respond(request_id: str, result: Any) -> MCPResponse:
        if isinstance(result, dict) and isinstance(result.get("_error"), dict):
            return MCPResponse(request_id=request_id, error=result["_error"])
        if isinstance(result, dict) and "_ui_components" in result:
            return MCPResponse(request_id=request_id, result=result.get("_data"), ui_components=result["_ui_components"])
        return MCPResponse(request_id=request_id, result=result)

    @staticmethod
    def _first_chunk(request_id: str, tool_fn: Any, arguments: Dict[str, Any]) -> MCPResponse:
        public = {key: value for key, value in arguments.items() if not key.startswith("_")}
        loop = asyncio.new_event_loop()
        try:
            generator = tool_fn(public, {})
            try:
                chunk = loop.run_until_complete(generator.__anext__())
            except StopAsyncIteration:
                return MCPResponse(request_id=request_id, result=None, ui_components=[])
            finally:
                loop.run_until_complete(generator.aclose())
        finally:
            loop.close()
        if chunk.error:
            return MCPResponse(request_id=request_id, error={
                "code": chunk.error.get("code"), "message": chunk.error.get("message", ""),
                "retryable": bool(chunk.error.get("retryable")),
            })
        return MCPResponse(request_id=request_id, result=chunk.raw, ui_components=list(chunk.components))
