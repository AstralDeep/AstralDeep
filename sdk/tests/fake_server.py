"""Minimal real-socket fake implementing just the shape of Deep's MCP wire contract
(JSON-RPC over POST /mcp) plus an in-memory Work-operation state machine, so
sdk/tests can run offline without the real backend.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

PROTOCOL_VERSION = "2026-07-28"

_SCOPE_FOR_TOOL = {
    "astral_submit_operation": "operations.submit",
    "astral_get_operation": "operations.read",
    "astral_list_operations": "operations.read",
    "astral_get_operation_events": "operations.read",
    "astral_cancel_operation": "operations.control",
    "astral_pause_operation": "operations.control",
    "astral_get_artifact": "artifacts.read",
}


class FakeAstralState:
    def __init__(self, *, valid_token: str = "afk_test-token", scopes=frozenset(_SCOPE_FOR_TOOL.values())):
        self.lock = threading.Lock()
        self.valid_token = valid_token
        self.scopes = frozenset(scopes)
        self.operations: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.fail_next_n: int = 0
        self.requests: list[dict[str, Any]] = []


def _operation_view(op: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in op.items() if k != "_key"}


class _Handler(BaseHTTPRequestHandler):
    state: FakeAstralState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _send_json(self, status: int, body: dict[str, Any], *, www_authenticate: Optional[str] = None) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        if www_authenticate:
            self.send_header("WWW-Authenticate", www_authenticate)
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, request_id: Any, status: int, message: str) -> None:
        challenge = f'Bearer resource_metadata="http://fake/.well-known", scope="", error="{message}"' \
            if status in (401, 403) else None
        body_message = "MCP authorization failed" if status in (401, 403) else message
        self._send_json(status, {"jsonrpc": "2.0", "id": request_id,
                                 "error": {"code": -32000, "message": body_message}},
                        www_authenticate=challenge)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._error(None, 404, "not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._error(None, 400, "invalid JSON")
            return
        request_id = body.get("id")
        method = body.get("method")

        with self.state.lock:
            self.state.requests.append({"method": method, "body": body})
            if self.state.fail_next_n > 0:
                self.state.fail_next_n -= 1
                self._error(request_id, 503, "synthetic transient failure")
                return

        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or token != self.state.valid_token:
            self._error(request_id, 401, "invalid_token")
            return

        params = body.get("params") or {}
        try:
            if method == "server/discover":
                result: dict[str, Any] = {"resultType": "complete", "name": "fake-astral"}
            elif method == "tools/list":
                result = {"resultType": "complete", "tools": [
                    {"name": name} for name in _SCOPE_FOR_TOOL if _SCOPE_FOR_TOOL[name] in self.state.scopes
                ]}
            elif method == "tools/call":
                result = self._call_tool(params.get("name"), params.get("arguments") or {})
            else:
                self._error(request_id, 404, "method not found")
                return
        except _ToolError as exc:
            result = {"resultType": "complete", "content": [{"type": "text", "text": exc.code}],
                     "structuredContent": {}, "isError": True}
        self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    def _call_tool(self, name: Optional[str], arguments: dict[str, Any]) -> dict[str, Any]:
        scope = _SCOPE_FOR_TOOL.get(name or "")
        if scope is None:
            raise _ToolError("Tool is unavailable or not authorized")
        if scope not in self.state.scopes:
            raise _ToolError("framework_scope_required")
        handler = getattr(self, f"_tool_{name}")
        structured = handler(arguments)
        return {"resultType": "complete", "content": [{"type": "text", "text": json.dumps(structured)}],
                "structuredContent": structured, "isError": False}

    def _tool_astral_submit_operation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        key = arguments["idempotency_key"]
        with self.state.lock:
            existing_id = self.state.by_key.get(key)
            if existing_id is not None:
                return {**_operation_view(self.state.operations[existing_id]), "created": False}
            operation_id = str(uuid.uuid4())
            op = {
                "id": operation_id, "revision": 1, "instruction_revision": 1, "control_epoch": 1,
                "title": arguments["name"], "kind": "chat", "disposition": "queued",
                "lifecycle": "active", "phase": "queued", "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00", "next_wake_at": None, "deadline_at": None,
                "schema_supported": True, "safe_error_code": None, "usage": {},
            }
            self.state.operations[operation_id] = op
            self.state.by_key[key] = operation_id
            return {**_operation_view(op), "created": True}

    def _tool_astral_get_operation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            op = self.state.operations.get(arguments["operation_id"])
        if op is None:
            raise _ToolError("work_not_found")
        return _operation_view(op)

    def _tool_astral_list_operations(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            ops = [_operation_view(o) for o in self.state.operations.values()]
        return {"operations": ops, "next_cursor": None, "page_full": False}

    def _tool_astral_get_operation_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        op = self._tool_astral_get_operation(arguments)
        after = arguments.get("after_revision")
        changed = after != op["revision"]
        return {"revision": op["revision"], "changed": changed, "resync_required": False,
                "operation": op if changed else None}

    def _tool_astral_cancel_operation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            op = self.state.operations.get(arguments["operation_id"])
            if op is None:
                raise _ToolError("work_not_found")
            if op["revision"] != arguments["expected_revision"]:
                raise _ToolError("assignment_revision_conflict")
            if op["disposition"] != "cancelled":
                op["disposition"] = "cancelled"
                op["lifecycle"] = "stopped"
                op["revision"] += 1
                applied = True
            else:
                applied = False
            return {"operation": _operation_view(op), "applied": applied}

    def _tool_astral_pause_operation(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            op = self.state.operations.get(arguments["operation_id"])
            if op is None:
                raise _ToolError("work_not_found")
            if op["revision"] != arguments["expected_revision"]:
                raise _ToolError("assignment_revision_conflict")
            if op["disposition"] != "paused":
                op["disposition"] = "paused"
                op["revision"] += 1
                applied = True
            else:
                applied = False
            return {"operation": _operation_view(op), "applied": applied}

    def _tool_astral_get_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        op = self._tool_astral_get_operation(arguments)
        return {"id": op["id"], "revision": op["revision"], "result": {"text": "synthetic result"}}


class _ToolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FakeAstralServer:
    def __init__(self, state: Optional[FakeAstralState] = None) -> None:
        self.state = state or FakeAstralState()
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeAstralServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
