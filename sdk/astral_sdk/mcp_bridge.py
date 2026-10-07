"""Bridge exposes Astral's Work tools to a local MCP host over stdio JSON-RPC through
one AstralClient; serve_stdio() needs only httpx, while serve_with_official_sdk()
lazily imports the optional mcp package.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import IO, Any, Optional

from astral_sdk.client import AstralClient
from astral_sdk.errors import AstralHTTPError, AstralTimeoutError, RetryExhaustedError
from astral_sdk.tools import (
    ASTRAL_TOOLS,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOL_NAMES,
)

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_UNSUPPORTED_PROTOCOL_VERSION = -32022
_META_PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
_META_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
_META_SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
_SERVER_INFO = {"name": "astral-sdk-bridge", "version": "0.1.0"}
_MAX_PENDING_REQUESTS = 256
_MAX_LINE_CHARS = 1048576


class _InvalidToolCall(Exception):
    pass


class _ToolInputError(Exception):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"missing required argument(s): {', '.join(missing)}")
        self.missing = missing


def _is_valid_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


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


def _error_response(request_id: Any, code: int, message: str, *, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _success_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {"resultType": "complete", "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload, "isError": False}


def _execution_failure_result(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"resultType": "complete", "content": [{"type": "text", "text": str(exc)}],
                              "isError": True}
    code = getattr(exc, "code", None)
    if code is not None:
        result["structuredContent"] = {"code": code}
    return result


def _input_validation_failure_result(exc: "_ToolInputError") -> dict[str, Any]:
    return {"resultType": "complete", "content": [{"type": "text", "text": str(exc)}],
            "structuredContent": {"code": "invalid_arguments", "missing": list(exc.missing)}, "isError": True}


def _mcp_tool_descriptors() -> list[dict[str, Any]]:
    return [{"name": name, "description": ASTRAL_TOOLS[name]["description"],
             "inputSchema": ASTRAL_TOOLS[name]["input_schema"]} for name in TOOL_NAMES]


def _official_handlers(bridge: "Bridge") -> tuple[Any, Any]:
    from mcp import types
    from mcp.shared.exceptions import MCPError

    async def list_tools(context: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[
            types.Tool(name=descriptor["name"], description=descriptor["description"],
                       input_schema=descriptor["inputSchema"])
            for descriptor in _mcp_tool_descriptors()])

    async def call_tool(context: Any, params: Any) -> types.CallToolResult:
        try:
            payload = bridge._dispatch(params.name, dict(params.arguments or {}))
        except _InvalidToolCall as exc:
            raise MCPError(code=_INVALID_PARAMS, message=str(exc)) from exc
        except _ToolInputError as exc:
            failure = _input_validation_failure_result(exc)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=failure["content"][0]["text"])],
                structured_content=failure["structuredContent"], is_error=True)
        except (AstralHTTPError, AstralTimeoutError, RetryExhaustedError) as exc:
            failure = _execution_failure_result(exc)
            kwargs: dict[str, Any] = {
                "content": [types.TextContent(type="text", text=failure["content"][0]["text"])],
                "is_error": True}
            if "structuredContent" in failure:
                kwargs["structured_content"] = failure["structuredContent"]
            return types.CallToolResult(**kwargs)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload, is_error=False)

    return list_tools, call_tool


class Bridge:
    def __init__(self, client: AstralClient) -> None:
        self._client = client

    @classmethod
    def connect(cls, base_url: str, token: str, **client_kwargs: Any) -> "Bridge":
        return cls(AstralClient(base_url, token, **client_kwargs))

    def close(self) -> None:
        self._client.close()

    def handle(self, request: Any) -> Optional[dict[str, Any]]:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "method" not in request:
            return _error_response(None, _INVALID_REQUEST, "request must be a JSON-RPC 2.0 object with a method")
        method = request["method"]
        if not isinstance(method, str):
            return _error_response(None, _INVALID_REQUEST, "method must be a string")
        if "id" not in request:
            return None
        request_id = request["id"]
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return _error_response(None, _INVALID_REQUEST, "request id must be a string or integer")
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error_response(request_id, _INVALID_REQUEST, "params must be an object")
        meta_failure = self._meta_failure(method, params, request_id)
        if meta_failure is not None:
            return meta_failure
        try:
            if method == "server/discover":
                result: Any = self._discover_result()
            elif method == "tools/list":
                result = {"resultType": "complete", "tools": _mcp_tool_descriptors()}
            elif method == "tools/call":
                result = self._tool_call_result(params)
            else:
                return _error_response(request_id, _METHOD_NOT_FOUND, f"unknown method: {method}")
        except _InvalidToolCall as exc:
            return _error_response(request_id, _INVALID_PARAMS, str(exc))
        except _ToolInputError as exc:
            result = _input_validation_failure_result(exc)
        except (AstralHTTPError, AstralTimeoutError, RetryExhaustedError) as exc:
            result = _execution_failure_result(exc)
        except Exception as exc:
            return _error_response(request_id, _INTERNAL_ERROR, f"tool dispatch failed: {type(exc).__name__}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _meta_failure(self, method: str, params: dict[str, Any], request_id: Any) -> Optional[dict[str, Any]]:
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return self._invalid_params(method, request_id, "params._meta must be an object")
        requested = meta.get(_META_PROTOCOL_KEY)
        if not isinstance(requested, str) or not requested:
            return self._invalid_params(method, request_id,
                                        f"params._meta.{_META_PROTOCOL_KEY} must be a non-empty string")
        capabilities = meta.get(_META_CAPABILITIES_KEY)
        if not isinstance(capabilities, dict):
            return self._invalid_params(method, request_id,
                                        f"params._meta.{_META_CAPABILITIES_KEY} must be an object")
        if requested not in SUPPORTED_PROTOCOL_VERSIONS:
            return _error_response(request_id, _UNSUPPORTED_PROTOCOL_VERSION,
                                   f"unsupported protocol version: {requested}",
                                   data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": requested})
        return None

    def _invalid_params(self, method: str, request_id: Any, message: str) -> dict[str, Any]:
        data = {"supported": list(SUPPORTED_PROTOCOL_VERSIONS)} if method == "initialize" else None
        return _error_response(request_id, _INVALID_PARAMS, message, data=data)

    def _discover_result(self) -> dict[str, Any]:
        return {"resultType": "complete", "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": {"tools": {}}, "_meta": {_META_SERVER_INFO_KEY: dict(_SERVER_INFO)}}

    def _tool_call_result(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(name, str) or not name:
            raise _InvalidToolCall("params.name must be a non-empty tool name string")
        if not isinstance(arguments, dict):
            raise _InvalidToolCall("params.arguments must be an object")
        return _success_result(self._dispatch(name, arguments))

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
        factory = method_for_tool.get(name)
        if factory is None:
            raise _InvalidToolCall(f"unknown tool: {name}")
        schema = ASTRAL_TOOLS[name].get("input_schema", {})
        missing = [field for field in schema.get("required", ()) if field not in arguments]
        if missing:
            raise _ToolInputError(missing)
        value = factory()
        return asdict(value)

    def serve_stdio(self, in_stream: IO[str] = sys.stdin, out_stream: IO[str] = sys.stdout) -> None:
        import queue

        lines: "queue.Queue[Any]" = queue.Queue()
        gate = _IntakeGate(_MAX_PENDING_REQUESTS)
        tracker = _RequestTracker()

        def _read_lines() -> None:
            try:
                for raw in in_stream:
                    line = raw.strip()
                    if not line:
                        continue
                    if len(line) > _MAX_LINE_CHARS:
                        lines.put(("OVERSIZE", None))
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        message = None
                    if isinstance(message, dict) and "id" not in message:
                        params = message.get("params")
                        request_id = params.get("requestId") if isinstance(params, dict) else None
                        if message.get("method") == "notifications/cancelled" and _is_valid_id(request_id):
                            tracker.cancel(request_id)
                        continue
                    request_id = message.get("id") if isinstance(message, dict) else None
                    if gate.admit():
                        if _is_valid_id(request_id):
                            tracker.register(request_id)
                        lines.put(("REQ", line))
                    else:
                        lines.put(("OVERLOAD", request_id if _is_valid_id(request_id) else None))
            finally:
                lines.put(None)

        reader = threading.Thread(target=_read_lines, daemon=True)
        reader.start()
        while True:
            item = lines.get()
            if item is None:
                break
            kind, payload = item
            if kind == "OVERSIZE":
                self._write_message(out_stream, _error_response(
                    None, _INVALID_REQUEST, "frame exceeds the maximum line size"))
                continue
            if kind == "OVERLOAD":
                self._write_message(out_stream, _error_response(
                    payload, _INTERNAL_ERROR, "too many pending requests; retry after earlier requests complete"))
                continue
            gate.release()
            try:
                request = json.loads(payload)
            except json.JSONDecodeError:
                self._write_message(out_stream, {"jsonrpc": "2.0", "id": None,
                                                 "error": {"code": _PARSE_ERROR, "message": "invalid JSON"}})
                continue
            request_id = request.get("id") if isinstance(request, dict) else None
            response = self.handle(request)
            if _is_valid_id(request_id) and tracker.finish(request_id):
                continue
            if response is not None:
                self._write_message(out_stream, response)

    def _write_message(self, out_stream: IO[str], message: dict[str, Any]) -> None:
        out_stream.write(json.dumps(message) + "\n")
        out_stream.flush()

    def serve_with_official_sdk(self) -> None:
        try:
            from mcp.server import Server
            from mcp.server.stdio import stdio_server
        except ImportError as exc:
            raise ImportError(
                "the official MCP SDK is required for serve_with_official_sdk(); "
                "install it with: pip install astral-sdk[mcp]"
            ) from exc

        import asyncio

        list_tools, call_tool = _official_handlers(self)
        server = Server("astral-sdk-bridge", version=_SERVER_INFO["version"],
                        on_list_tools=list_tools, on_call_tool=call_tool)

        async def _run():
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())

        asyncio.run(_run())


__all__ = ["Bridge"]
