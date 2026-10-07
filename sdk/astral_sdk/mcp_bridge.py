"""Bridge exposes Astral's Work tools to a local MCP host over stdio JSON-RPC through
one AstralClient; serve_stdio() needs only httpx, while serve_with_official_sdk()
lazily imports the optional mcp package. Both serving paths speak the declared
MCP 2026-07-28 wire shape through astral_sdk.mcp_protocol.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import IO, Any, Optional

from astral_sdk.client import AstralClient
from astral_sdk.errors import AstralError, AstralHTTPError
from astral_sdk.mcp_protocol import (
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_VERSION,
    ProtocolError,
    discover_result,
    error_response,
    negotiated_call_arguments,
    require_request_envelope,
    tool_error_result,
    tool_success_result,
    tools_list_result,
    valid_request_id,
)

logger = logging.getLogger("astral_sdk.mcp_bridge")


class Bridge:
    def __init__(self, client: AstralClient) -> None:
        self._client = client

    @classmethod
    def connect(cls, base_url: str, token: str, **client_kwargs: Any) -> "Bridge":
        return cls(AstralClient(base_url, token, **client_kwargs))

    def close(self) -> None:
        self._client.close()

    def handle(self, request: Any) -> Optional[dict[str, Any]]:
        if not isinstance(request, dict):
            return error_response(
                None, ProtocolError(INVALID_REQUEST, "request must be a JSON object"))
        if "id" not in request:
            return None
        request_id = request.get("id")
        if not valid_request_id(request_id):
            return error_response(
                None, ProtocolError(INVALID_REQUEST, "id must be a string or integer"))
        if request.get("jsonrpc") != "2.0":
            return error_response(
                request_id, ProtocolError(INVALID_REQUEST, 'jsonrpc must be "2.0"'))
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return error_response(
                request_id, ProtocolError(INVALID_REQUEST, "method must be a non-empty string"))
        params = request.get("params")
        try:
            if method == "tools/call":
                require_request_envelope(params)
                name, arguments = negotiated_call_arguments(params)
                result: Any = self._tool_call_outcome(name, arguments)
            elif method == "tools/list":
                require_request_envelope(params)
                result = tools_list_result()
            elif method == "server/discover":
                require_request_envelope(params)
                result = discover_result()
            else:
                return error_response(
                    request_id, ProtocolError(METHOD_NOT_FOUND, "Method not found"))
        except ProtocolError as exc:
            return error_response(request_id, exc)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _tool_call_outcome(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            structured = self._dispatch(name, arguments)
        except AstralError as exc:
            return tool_error_result(str(exc))
        except (KeyError, TypeError) as exc:
            return tool_error_result(f"invalid arguments for tool {name}: {exc}")
        except Exception:
            logger.exception("astral tool dispatch failed unexpectedly")
            return tool_error_result("tool execution failed")
        return tool_success_result(structured)

    def _dispatch(self, name: Optional[str], arguments: dict[str, Any]) -> dict[str, Any]:
        from dataclasses import asdict

        method_for_tool = {
            "astral_submit_operation": lambda: self._client.submit_operation(**arguments),
            "astral_get_operation": lambda: self._client.get_operation(arguments["operation_id"]),
            "astral_list_operations": lambda: self._client.list_operations(
                **{k: v for k, v in arguments.items() if k in ("limit", "after_id")}),
            "astral_get_operation_events": lambda: self._client.poll_operation(
                arguments["operation_id"], after_revision=arguments.get("after_revision")),
            "astral_cancel_operation": lambda: self._client.cancel_operation(
                arguments["operation_id"], submission_id=arguments.get("submission_id"),
                expected_revision=arguments["expected_revision"]),
            "astral_pause_operation": lambda: self._client.pause_operation(
                arguments["operation_id"], submission_id=arguments.get("submission_id"),
                expected_revision=arguments["expected_revision"]),
            "astral_get_artifact": lambda: self._client.get_artifact(arguments["operation_id"]),
        }
        factory = method_for_tool.get(name or "")
        if factory is None:
            raise AstralHTTPError(f"unknown Astral tool: {name}", code="unknown_tool")
        value = factory()
        return asdict(value)

    def serve_stdio(self, in_stream: Optional[IO[str]] = None,
                    out_stream: Optional[IO[str]] = None) -> None:
        if in_stream is None:
            in_stream = sys.stdin
        if out_stream is None:
            out_stream = sys.stdout
        for line in in_stream:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                response = error_response(
                    None, ProtocolError(PARSE_ERROR, "invalid JSON"))
            else:
                response = self.handle(request)
            if response is not None:
                out_stream.write(json.dumps(response) + "\n")
                out_stream.flush()

    def _official_server(self) -> Any:
        import mcp_types as types
        from mcp.server import Server

        server = Server(
            SERVER_NAME,
            version=SERVER_VERSION,
            instructions=SERVER_INSTRUCTIONS,
            on_list_tools=self._official_list_tools,
            on_call_tool=self._official_call_tool,
        )
        server.add_request_handler(
            "server/discover", types.RequestParams, self._official_discover)
        return server

    def serve_with_official_sdk(self) -> None:
        try:
            import mcp.server.stdio  # noqa: F401
            from mcp.server import Server  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "the official MCP SDK is required for serve_with_official_sdk(); "
                "install it with: pip install astral-sdk[mcp]"
            ) from exc

        server = self._official_server()

        import asyncio

        async def _run():  # pragma: no cover
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())

        asyncio.run(_run())

    async def _official_discover(self, ctx: Any, params: Any) -> Any:
        import mcp_types as types

        return types.DiscoverResult.model_validate(discover_result())

    async def _official_list_tools(self, ctx: Any, params: Any) -> Any:
        import mcp_types as types

        return types.ListToolsResult.model_validate(tools_list_result())

    async def _official_call_tool(self, ctx: Any, params: Any) -> Any:
        import mcp_types as types

        arguments = params.arguments if isinstance(params.arguments, dict) else {}
        try:
            structured = self._dispatch(params.name, arguments)
        except AstralError as exc:
            return types.CallToolResult.model_validate(tool_error_result(str(exc)))
        except (KeyError, TypeError) as exc:
            return types.CallToolResult.model_validate(
                tool_error_result(f"invalid arguments for tool {params.name}: {exc}"))
        return types.CallToolResult.model_validate(tool_success_result(structured))


def _main() -> int:
    import os

    base_url = os.environ.get("ASTRAL_BASE_URL")
    token = os.environ.get("ASTRAL_TOKEN")
    if not base_url or not token:
        sys.stderr.write(json.dumps(
            {"error": "ASTRAL_BASE_URL and ASTRAL_TOKEN are required"}) + "\n")
        return 1
    bridge = Bridge.connect(base_url, token)
    try:
        bridge.serve_stdio()
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["Bridge"]
