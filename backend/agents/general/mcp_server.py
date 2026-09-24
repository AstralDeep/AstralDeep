"""MCP server for the General agent: dispatches tools/list and tools/call to
mcp_tools.py's TOOL_REGISTRY, including draining a @streaming_tool async generator to
its first chunk for callers on the non-streaming snapshot path.
"""

import asyncio
import inspect
import os
import sys
import json
import logging
from typing import Dict, Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from shared.protocol import MCPRequest, MCPResponse
from agents.general.mcp_tools import TOOL_REGISTRY

logger = logging.getLogger('MCPServer')

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
        if isinstance(exc, RETRYABLE_EXCEPTIONS):
            return True
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
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

                if inspect.isasyncgenfunction(tool_fn):
                    return self._drain_streaming_tool_to_snapshot(
                        request.request_id, tool_name, tool_fn, arguments,
                    )

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

    def _drain_streaming_tool_to_snapshot(
        self,
        request_id: str,
        tool_name: str,
        tool_fn: Any,
        arguments: Dict[str, Any],
    ) -> MCPResponse:
        loop = asyncio.new_event_loop()
        try:
            agen = tool_fn(arguments, {})
            try:
                first_chunk = loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                logger.warning(
                    f"Streaming tool '{tool_name}' yielded nothing; "
                    f"returning empty snapshot"
                )
                return MCPResponse(
                    request_id=request_id,
                    result=None,
                    ui_components=[],
                )
            finally:
                try:
                    loop.run_until_complete(agen.aclose())
                except Exception:
                    pass

            components = list(getattr(first_chunk, "components", []) or [])
            raw = getattr(first_chunk, "raw", None)
            return MCPResponse(
                request_id=request_id,
                result=raw,
                ui_components=components,
            )
        except Exception as e:
            retryable = self._classify_error(e)
            logger.error(
                f"Streaming tool '{tool_name}' (snapshot path) raised "
                f"{type(e).__name__}: {e}"
            )
            return MCPResponse(
                request_id=request_id,
                error={"code": -32603, "message": str(e), "retryable": retryable},
            )
        finally:
            try:
                loop.close()
            except Exception:
                pass
