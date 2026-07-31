"""
MCP Server — dispatches tool calls to registered tool functions.
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
    """Simple MCP server that routes tool/call requests to registered functions."""

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

                # 001-tool-stream-ui: a streaming tool (async generator
                # decorated with @streaming_tool) called via the snapshot
                # path. This happens when:
                #   1. FF_TOOL_STREAMING is OFF and the LLM picks a streaming
                #      tool from tools/list (the SDK still registers them).
                #   2. The user calls a streaming tool directly without the
                #      _stream=True flag.
                # In either case, drain the generator to its FIRST yielded
                # chunk and return that as the MCPResponse — the user gets
                # a working snapshot. The generator's `finally` cleanup
                # runs on aclose() so no upstream subscriptions leak.
                if inspect.isasyncgenfunction(tool_fn):
                    return self._drain_streaming_tool_to_snapshot(
                        request.request_id, tool_name, tool_fn, arguments,
                    )

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

    def _drain_streaming_tool_to_snapshot(
        self,
        request_id: str,
        tool_name: str,
        tool_fn: Any,
        arguments: Dict[str, Any],
    ) -> MCPResponse:
        """Drain a @streaming_tool async generator to its first yielded
        StreamComponents and return that as a single MCPResponse.

        Used when the LLM picks a streaming tool from `tools/list` but the
        request comes through the normal snapshot path (FF_TOOL_STREAMING off,
        or _stream flag not set). The user sees a one-shot snapshot — exactly
        the behavior of the equivalent non-streaming tool — without breaking
        when the tool happens to be an async generator.

        The agent's regular streaming dispatch in BaseA2AAgent still handles
        the full async-generator path when FF_TOOL_STREAMING is on AND
        _stream=True. This method is the fallback for the snapshot case.
        """
        # The MCPServer is invoked from a worker thread (asyncio.to_thread).
        # We need our own loop here to drive the async generator.
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
                # Always close the generator so its `finally` block runs
                # to release upstream subscriptions / file handles / etc.
                try:
                    loop.run_until_complete(agen.aclose())
                except Exception:
                    pass

            # first_chunk is a StreamComponents instance — extract its
            # components and raw data into the snapshot wire shape.
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
