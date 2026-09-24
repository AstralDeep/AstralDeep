#!/usr/bin/env python3
"""MCP dispatch server for the Web Research agent: routes tool/call requests to
mcp_tools functions, classifying exceptions as retryable/non-retryable and flagging
tool-level error Alerts as MCP errors.
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from shared.protocol import MCPRequest, MCPResponse  # noqa: E402
from agents.web_research.mcp_tools import TOOL_REGISTRY  # noqa: E402

logger = logging.getLogger('WebResearchMCPServer')

RETRYABLE_EXCEPTIONS = (
    ConnectionError, TimeoutError, json.JSONDecodeError,
    OSError,
)

try:
    import requests
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (
        requests.exceptions.RequestException,
    )
except ImportError:
    pass

NON_RETRYABLE_EXCEPTIONS = (TypeError, KeyError, ValueError, AttributeError)

try:
    from shared.external_http import BadRequestError, EgressBlockedError
    NON_RETRYABLE_EXCEPTIONS = NON_RETRYABLE_EXCEPTIONS + (
        EgressBlockedError, BadRequestError,
    )
except ImportError:
    pass


class MCPServer:
    def __init__(self):
        self.tools = TOOL_REGISTRY

    def get_tool_list(self) -> list:
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
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        if isinstance(exc, RETRYABLE_EXCEPTIONS):
            return True
        return True

    def process_request(self, request: MCPRequest) -> MCPResponse:
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

                if isinstance(result, dict) and isinstance(result.get("_error"), dict):
                    return MCPResponse(request_id=request.request_id, error=result["_error"])

                if isinstance(result, dict) and "_ui_components" in result:
                    ui_comps = result["_ui_components"]
                    has_error = any(
                        isinstance(c, dict) and c.get("variant") == "error"
                        for c in ui_comps
                    )
                    if has_error:
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
