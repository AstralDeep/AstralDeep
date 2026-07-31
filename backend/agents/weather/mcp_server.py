#!/usr/bin/env python3
"""
MCP Server for Weather Agent — dispatches tool calls to weather tool functions.
"""
import os
import sys
import json
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from shared.protocol import MCPRequest, MCPResponse
from agents.weather.mcp_tools import TOOL_REGISTRY

logger = logging.getLogger('WeatherMCPServer')

# Exceptions that indicate a transient/network issue worth retrying
RETRYABLE_EXCEPTIONS = (
    ConnectionError, TimeoutError, json.JSONDecodeError,
    OSError,  # covers socket errors
)

try:
    import requests
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (
        requests.exceptions.RequestException,
    )
except ImportError:
    pass

# Exceptions that indicate bad arguments / logic errors — never retry
NON_RETRYABLE_EXCEPTIONS = (TypeError, KeyError, ValueError, AttributeError)


class MCPServer:
    """Simple MCP server that routes tool/call requests to registered weather functions."""

    def __init__(self):
        self.tools = TOOL_REGISTRY

    def get_tool_list(self) -> list:
        """Return list of available tools with their schemas."""
        return [
            {
                "name": name,
                "description": info["description"],
                "input_schema": info.get("input_schema", {"type": "object", "properties": {}})
            }
            for name, info in self.tools.items()
        ]

    @staticmethod
    def _classify_error(exc: Exception) -> bool:
        """Return True if the error is retryable (transient), False otherwise."""
        if isinstance(exc, RETRYABLE_EXCEPTIONS):
            return True
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        # Default: mark unknown errors as retryable to give them a chance
        return True

    def process_request(self, request: MCPRequest) -> MCPResponse:
        """Process an MCP request and return a response."""
        if request.method == "tools/list":
            return MCPResponse(
                request_id=request.request_id,
                result={"tools": self.get_tool_list()}
            )

        if request.method == "tools/call":
            tool_name = request.params.get("name", "")
            arguments = request.params.get("arguments", {})

            if tool_name not in self.tools:
                return MCPResponse(
                    request_id=request.request_id,
                    error={"code": -32601, "message": f"Unknown tool: {tool_name}",
                           "retryable": False}
                )

            try:
                tool_fn = self.tools[tool_name]["function"]
                result = tool_fn(**arguments)

                # Check if the tool itself returned an error via UI components
                if isinstance(result, dict) and "_ui_components" in result:
                    ui_comps = result["_ui_components"]
                    # Detect tool-level errors (Alert with variant="error")
                    has_error = any(
                        isinstance(c, dict) and c.get("variant") == "error"
                        for c in ui_comps
                    )
                    if has_error:
                        # Extract the error message from the alert
                        error_msg = "Tool returned an error"
                        for c in ui_comps:
                            if isinstance(c, dict) and c.get("variant") == "error":
                                error_msg = c.get("message", error_msg)
                                break
                        logger.warning(f"Tool '{tool_name}' returned error alert: {error_msg}")
                        return MCPResponse(
                            request_id=request.request_id,
                            error={"code": -32603, "message": error_msg,
                                   "retryable": True}
                        )

                    data = result.get("_data")
                    return MCPResponse(
                        request_id=request.request_id,
                        result=data,
                        ui_components=ui_comps
                    )

                return MCPResponse(
                    request_id=request.request_id,
                    result=result
                )

            except Exception as e:
                retryable = self._classify_error(e)
                logger.error(f"Tool '{tool_name}' raised {type(e).__name__}: {e} "
                             f"(retryable={retryable})")
                return MCPResponse(
                    request_id=request.request_id,
                    error={"code": -32603, "message": str(e),
                           "retryable": retryable}
                )

        return MCPResponse(
            request_id=request.request_id,
            error={"code": -32601, "message": f"Unknown method: {request.method}",
                   "retryable": False}
        )
