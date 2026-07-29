#!/usr/bin/env python3
"""MCP server for remote-compute-1 — routes tool/call over the unified registry.

Identical dispatch contract to the other bundled agents: branches on the
``_ui_components`` envelope and flags an error response when any top-level
component has ``variant == "error"``.
"""
import inspect
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from shared.protocol import MCPRequest, MCPResponse
from agents.remote_compute.mcp_tools import TOOL_REGISTRY

logger = logging.getLogger('RemoteComputeMCPServer')

RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, json.JSONDecodeError, OSError)
NON_RETRYABLE_EXCEPTIONS = (TypeError, KeyError, ValueError, AttributeError)


class MCPServer:
    """MCP server that routes tool/call requests to registered verb functions."""

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

    @staticmethod
    def _classify_error(exc: Exception) -> bool:
        if isinstance(exc, RETRYABLE_EXCEPTIONS):
            return True
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        return True

    def process_request(self, request: MCPRequest) -> MCPResponse:
        if request.method == "tools/list":
            return MCPResponse(request_id=request.request_id, result={"tools": self.get_tool_list()})

        if request.method == "tools/call":
            tool_name = request.params.get("name", "")
            arguments = request.params.get("arguments", {})

            if tool_name not in self.tools:
                return MCPResponse(
                    request_id=request.request_id,
                    error={"code": -32601, "message": f"Unknown tool: {tool_name}", "retryable": False},
                )

            try:
                tool_fn = self.tools[tool_name]["function"]
                sig = inspect.signature(tool_fn)
                params = sig.parameters
                has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                if not has_var_keyword:
                    arguments = {k: v for k, v in arguments.items() if k in params}
                result = tool_fn(**arguments)

                if isinstance(result, dict) and "_ui_components" in result:
                    ui_comps = result["_ui_components"]
                    error_comp = next(
                        (c for c in ui_comps if isinstance(c, dict) and c.get("variant") == "error"),
                        None,
                    )
                    if error_comp is not None:
                        msg = error_comp.get("message", "Tool returned an error")
                        logger.warning(f"Tool '{tool_name}' returned error alert: {msg}")
                        return MCPResponse(
                            request_id=request.request_id,
                            error={"code": -32000, "message": msg, "retryable": False},
                            ui_components=ui_comps,
                        )
                    return MCPResponse(request_id=request.request_id,
                                       result=result.get("_data"), ui_components=ui_comps)

                return MCPResponse(request_id=request.request_id, result=result)

            except Exception as e:
                retryable = MCPServer._classify_error(e)
                logger.error(f"Tool '{tool_name}' raised {type(e).__name__}: {e} (retryable={retryable})")
                return MCPResponse(request_id=request.request_id,
                                   error={"code": -32603, "message": str(e), "retryable": retryable})

        return MCPResponse(
            request_id=request.request_id,
            error={"code": -32601, "message": f"Unknown method: {request.method}", "retryable": False},
        )
