"""Bridge exposes Astral's Work tools to a local MCP host over stdio JSON-RPC through
one AstralClient; serve_stdio() needs only httpx, while serve_with_official_sdk()
lazily imports the optional mcp package.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, Optional

from astral_sdk.client import AstralClient
from astral_sdk.errors import AstralHTTPError
from astral_sdk.tools import all_function_schemas

_PROTOCOL_ERROR = -32601
_INTERNAL_ERROR = -32603


class Bridge:
    def __init__(self, client: AstralClient) -> None:
        self._client = client

    @classmethod
    def connect(cls, base_url: str, token: str, **client_kwargs: Any) -> "Bridge":
        return cls(AstralClient(base_url, token, **client_kwargs))

    def close(self) -> None:
        self._client.close()

    def handle(self, request: dict[str, Any]) -> Optional[dict[str, Any]]:
        request_id = request.get("id")
        method = request.get("method")
        is_notification = "id" not in request
        try:
            if method == "tools/list":
                result: Any = {"tools": all_function_schemas()}
            elif method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                result = self._dispatch(name, arguments)
            elif method == "initialize":
                result = {"protocolVersion": "2026-07-28", "serverInfo": {"name": "astral-sdk-bridge"}}
            else:
                if is_notification:
                    return None
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": _PROTOCOL_ERROR, "message": f"unknown method: {method}"}}
        except AstralHTTPError as exc:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": _INTERNAL_ERROR, "message": str(exc), "data": {"code": exc.code}}}
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

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

    def serve_stdio(self, in_stream: IO[str] = sys.stdin, out_stream: IO[str] = sys.stdout) -> None:
        for line in in_stream:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                out_stream.write(json.dumps(
                    {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "invalid JSON"}}) + "\n")
                out_stream.flush()
                continue
            response = self.handle(request)
            if response is not None:
                out_stream.write(json.dumps(response) + "\n")
                out_stream.flush()

    def serve_with_official_sdk(self) -> None:
        try:
            import mcp.server.stdio  # noqa: F401
            from mcp.server import Server
        except ImportError as exc:
            raise ImportError(
                "the official MCP SDK is required for serve_with_official_sdk(); "
                "install it with: pip install astral-sdk[mcp]"
            ) from exc

        server = Server("astral-sdk-bridge")

        @server.list_tools()
        async def _list_tools():  # pragma: no cover
            return all_function_schemas()

        @server.call_tool()
        async def _call_tool(name: str, arguments: dict):  # pragma: no cover
            return self._dispatch(name, arguments)

        import asyncio

        async def _run():  # pragma: no cover
            async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())

        asyncio.run(_run())


__all__ = ["Bridge"]
