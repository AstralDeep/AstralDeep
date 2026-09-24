#!/usr/bin/env python3
"""MCP server for the computer-use agent: dispatches tool/call requests from
mcp_tools.py's verb registry and keeps screenshot `_images` separate from the `_data`
text digest so no base64 reaches the model's text context.
"""
import inspect
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from shared.protocol import MCPRequest, MCPResponse
from agents.computer_use.mcp_tools import TOOL_REGISTRY

logger = logging.getLogger('ComputerUseMCPServer')

NON_RETRYABLE_EXCEPTIONS = (TypeError, KeyError, ValueError, AttributeError)


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
                        logger.info("Tool '%s' refused: %s", tool_name, msg)
                        return MCPResponse(
                            request_id=request.request_id,
                            error={"code": -32603, "message": msg, "retryable": False},
                        )
                    payload = {"_data": result.get("_data")}
                    if result.get("_images"):
                        payload["_images"] = result["_images"]
                    return MCPResponse(request_id=request.request_id,
                                       result=payload, ui_components=ui_comps)

                return MCPResponse(request_id=request.request_id, result=result)

            except Exception as e:  # noqa: BLE001
                logger.error("Tool '%s' raised %s: %s", tool_name, type(e).__name__, e)
                return MCPResponse(request_id=request.request_id,
                                   error={"code": -32603, "message": str(e), "retryable": False})

        return MCPResponse(
            request_id=request.request_id,
            error={"code": -32601, "message": f"Unknown method: {request.method}", "retryable": False},
        )
