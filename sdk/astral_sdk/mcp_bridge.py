"""Bridge exposes Astral's Work tools to a local MCP host over stdio JSON-RPC through
one AstralClient; serve_stdio() needs only httpx, while serve_with_official_sdk()
lazily imports the optional mcp package. Both serving paths speak the declared
MCP 2026-07-28 wire shape through astral_sdk.mcp_protocol.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from typing import IO, Any, Optional

from astral_sdk.client import AstralClient
from astral_sdk.errors import AstralError, AstralHTTPError
from astral_sdk.mcp_protocol import (
    INTERNAL_ERROR,
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

_MAX_PENDING_REQUESTS = 256
_MAX_LINE_CHARS = 1048576


class _RequestTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outstanding: set[Any] = set()
        self._cancelled: set[Any] = set()

    def register(self, request_id: Any) -> None:
        with self._lock:
            self._outstanding.add(request_id)

    def cancel(self, request_id: Any) -> None:
        with self._lock:
            if request_id in self._outstanding:
                self._cancelled.add(request_id)

    def finish(self, request_id: Any) -> bool:
        with self._lock:
            cancelled = request_id in self._cancelled
            self._outstanding.discard(request_id)
            self._cancelled.discard(request_id)
            return cancelled


class _IntakeGate:
    def __init__(self, limit: int) -> None:
        self._lock = threading.Lock()
        self._limit = limit
        self._pending = 0

    def admit(self) -> bool:
        with self._lock:
            if self._pending >= self._limit:
                return False
            self._pending += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._pending -= 1


class Bridge:
    def __init__(self, client: AstralClient) -> None:
        self._client = client
        self._stdio_queue: Optional[queue.Queue[Any]] = None

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
        from astral_sdk.tools import ASTRAL_TOOLS

        spec = ASTRAL_TOOLS.get(name or "")
        if spec:
            required = spec.get("input_schema", {}).get("required", ())
            missing = [f for f in required if f not in arguments]
            if missing:
                raise TypeError(f"missing required argument(s): {', '.join(missing)}")
        value = factory()
        return asdict(value)

    def serve_stdio(self, in_stream: Optional[IO[str]] = None,
                    out_stream: Optional[IO[str]] = None) -> None:
        if in_stream is None:
            in_stream = sys.stdin
        if out_stream is None:
            out_stream = sys.stdout

        write_lock = threading.Lock()

        def _write(message: dict[str, Any]) -> None:
            with write_lock:
                out_stream.write(json.dumps(message) + "\n")
                out_stream.flush()

        lines: queue.Queue[Any] = queue.Queue(maxsize=_MAX_PENDING_REQUESTS + 1)
        self._stdio_queue = lines
        gate = _IntakeGate(_MAX_PENDING_REQUESTS)
        tracker = _RequestTracker()

        def _read_lines() -> None:
            try:
                for raw in in_stream:
                    line = raw.strip()
                    if not line:
                        continue
                    if len(line) > _MAX_LINE_CHARS:
                        _write(error_response(
                            None, ProtocolError(INVALID_REQUEST, "frame exceeds the maximum line size")))
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        _write(error_response(
                            None, ProtocolError(PARSE_ERROR, "invalid JSON")))
                        continue
                    if not isinstance(message, dict):
                        _write(error_response(
                            None, ProtocolError(INVALID_REQUEST, "request must be a JSON object")))
                        continue
                    if "id" not in message:
                        params = message.get("params")
                        request_id = params.get("requestId") if isinstance(params, dict) else None
                        if message.get("method") == "notifications/cancelled" and valid_request_id(request_id):
                            tracker.cancel(request_id)
                        continue
                    request_id = message.get("id")
                    if gate.admit():
                        if valid_request_id(request_id):
                            tracker.register(request_id)
                        lines.put(message)
                    else:
                        _write(error_response(
                            request_id if valid_request_id(request_id) else None,
                            ProtocolError(INTERNAL_ERROR, "too many pending requests; retry after earlier requests complete"),
                        ))
            finally:
                lines.put(None)

        reader = threading.Thread(target=_read_lines, daemon=True)
        reader.start()

        while True:
            item = lines.get()
            if item is None:
                break
            request = item
            gate.release()
            request_id = request.get("id") if isinstance(request, dict) else None
            response = self.handle(request)
            if valid_request_id(request_id) and tracker.finish(request_id):
                continue
            if response is not None:
                _write(response)

        reader.join(timeout=5.0)

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
