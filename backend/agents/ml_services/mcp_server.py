#!/usr/bin/env python3
"""MCP server for the ML Services agent: dispatches tool/call requests to the union
TOOL_REGISTRY, reusing _wrapper.py's retry classification.
"""
import inspect
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared.protocol import MCPRequest, MCPResponse
from agents.ml_services._wrapper import is_retryable_error
from agents.ml_services.mcp_tools import TOOL_REGISTRY

logger = logging.getLogger("MlServicesAgentMCPServer")


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
            return MCPResponse(
                request_id=request.request_id,
                result={"tools": self.get_tool_list()},
            )

        if request.method == "tools/call":
            tool_name = request.params.get("name", "")
            arguments = request.params.get("arguments", {})

            if tool_name not in self.tools:
                return MCPResponse(
                    request_id=request.request_id,
                    error={"code": -32601, "message": f"Unknown tool: {tool_name}", "retryable": False},
                )

            tool_info = self.tools[tool_name]
            schema = tool_info.get("input_schema") or {}
            required = schema.get("required") or []
            missing = [k for k in required if k not in arguments]
            if missing:
                props = schema.get("properties") or {}
                hints = "; ".join(
                    f"{k}: {(props.get(k) or {}).get('description', 'no description')}"
                    for k in missing
                )
                return MCPResponse(
                    request_id=request.request_id,
                    error={
                        "code": -32602,
                        "message": (
                            f"Missing required argument(s) for '{tool_name}': "
                            f"{', '.join(missing)}. {hints}"
                        ),
                        "retryable": False,
                    },
                )

            try:
                tool_fn = tool_info["function"]
                sig = inspect.signature(tool_fn)
                params = sig.parameters
                has_var_keyword = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                if not has_var_keyword:
                    arguments = {k: v for k, v in arguments.items() if k in params}
                result = tool_fn(**arguments)

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
                        return MCPResponse(
                            request_id=request.request_id,
                            error={
                                "code": -32603,
                                "message": error_msg,
                                "retryable": result.get("_retryable", True),
                            },
                        )
                    data = result.get("_data")
                    return MCPResponse(
                        request_id=request.request_id,
                        result=data,
                        ui_components=ui_comps,
                    )

                return MCPResponse(request_id=request.request_id, result=result)

            except Exception as e:
                retryable = is_retryable_error(e)
                logger.error(
                    "Tool '%s' raised %s: %s (retryable=%s)",
                    tool_name, type(e).__name__, e, retryable,
                )
                return MCPResponse(
                    request_id=request.request_id,
                    error={"code": -32603, "message": str(e), "retryable": retryable},
                )

        return MCPResponse(
            request_id=request.request_id,
            error={"code": -32601, "message": f"Unknown method: {request.method}", "retryable": False},
        )
