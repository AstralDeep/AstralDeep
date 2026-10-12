"""MCP router for the Cresco agent: exposes the read-only topology tool registry over the
standard tools/list and tools/call methods and refuses everything else. Tool handlers live
in mcp_tools.py; registration is gated on the cresco feature flag by
orchestrator/local_agents.py and every call re-checks the flag here.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.feature_flags import flags
from shared.protocol import MCPRequest, MCPResponse

from agents.cresco.mcp_tools import TOOL_REGISTRY

logger = logging.getLogger("CrescoAgentMCPServer")


class MCPServer:
    def __init__(self):
        self.tools = TOOL_REGISTRY

    def get_tool_list(self) -> list:
        return [
            {
                "name": name,
                "description": info["description"],
                "input_schema": info.get("input_schema", {"type": "object", "properties": {}}),
                "scope": info.get("scope", "tools:read"),
            }
            for name, info in self.tools.items()
        ]

    def process_request(self, request: MCPRequest) -> MCPResponse:
        if not flags.is_enabled("cresco"):
            return MCPResponse(
                request_id=request.request_id,
                error={"code": -32000, "message": "Cresco tools are disabled by feature flag.", "retryable": False},
            )
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
            return self._respond(request.request_id, tool_fn(**arguments))
        except Exception as error:  # noqa: BLE001
            logger.error("Tool '%s' raised %s", tool_name, type(error).__name__)
            return MCPResponse(
                request_id=request.request_id,
                error={"code": -32603, "message": "The Cresco tool failed unexpectedly", "retryable": False},
            )

    @staticmethod
    def _respond(request_id: str, result: Any) -> MCPResponse:
        if isinstance(result, dict) and isinstance(result.get("_error"), dict):
            return MCPResponse(request_id=request_id, error=result["_error"])
        if isinstance(result, dict) and "_ui_components" in result:
            return MCPResponse(
                request_id=request_id,
                result=result.get("_data"),
                ui_components=result["_ui_components"],
            )
        return MCPResponse(request_id=request_id, result=result)
