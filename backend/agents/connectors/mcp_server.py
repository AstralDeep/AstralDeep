"""
MCP Server for the Claude Connectors Agent — US-22.

Registers all connector tools across office, dev, runtime, and creative domains
and dispatches ``tools/list`` / ``tools/call`` MCP requests against them.
"""
import json
import logging
import os
import sys
from typing import Dict, Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from shared.protocol import MCPRequest, MCPResponse

from agents.connectors.mcp_tools_office import OFFICE_TOOL_REGISTRY
from agents.connectors.mcp_tools_dev import DEV_TOOL_REGISTRY
from agents.connectors.mcp_tools_runtime import RUNTIME_TOOL_REGISTRY
from agents.connectors.mcp_tools_creative import CREATIVE_TOOL_REGISTRY

logger = logging.getLogger("ConnectorsMCPServer")

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}
TOOL_REGISTRY.update(OFFICE_TOOL_REGISTRY)
TOOL_REGISTRY.update(DEV_TOOL_REGISTRY)
TOOL_REGISTRY.update(RUNTIME_TOOL_REGISTRY)
TOOL_REGISTRY.update(CREATIVE_TOOL_REGISTRY)


RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, json.JSONDecodeError, OSError)
try:
    import requests
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (requests.exceptions.RequestException,)
except ImportError:
    pass

NON_RETRYABLE_EXCEPTIONS = (TypeError, KeyError, ValueError, AttributeError)


class ConnectorsMCPServer:
    """Routes ``tools/list`` and ``tools/call`` MCP requests to TOOL_REGISTRY.

    Handlers accept a single dict positional argument (``args``) — the
    orchestrator-injected ``_credentials``, ``_runtime``, ``user_id``,
    ``session_id`` etc. all live inside that dict.
    """

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
            return MCPResponse(
                request_id=request.request_id,
                result={"tools": self.get_tool_list()},
            )

        if request.method == "tools/call":
            tool_name = request.params.get("name", "") if request.params else ""
            arguments = (request.params.get("arguments", {}) if request.params else {}) or {}

            tool_info = self.tools.get(tool_name)
            if tool_info is None:
                return MCPResponse(
                    request_id=request.request_id,
                    error={
                        "code": -32601,
                        "message": f"Unknown tool: {tool_name}",
                        "retryable": False,
                    },
                )

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
                handler = tool_info["function"]
                result = handler(arguments)

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
                    return MCPResponse(
                        request_id=request.request_id,
                        result=result.get("_data"),
                        ui_components=ui_comps,
                    )

                return MCPResponse(request_id=request.request_id, result=result)

            except Exception as e:
                retryable = ConnectorsMCPServer._classify_error(e)
                logger.exception(
                    "Connectors tool '%s' raised %s: %s (retryable=%s)",
                    tool_name, type(e).__name__, e, retryable,
                )
                return MCPResponse(
                    request_id=request.request_id,
                    error={
                        "code": -32603,
                        "message": str(e),
                        "tool": tool_name,
                        "retryable": retryable,
                    },
                )

        return MCPResponse(
            request_id=request.request_id,
            error={
                "code": -32601,
                "message": f"Unknown method: {request.method}",
                "retryable": False,
            },
        )
