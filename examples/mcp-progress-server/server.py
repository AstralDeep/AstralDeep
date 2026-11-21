"""A stateless MCP server for protocol 2026-07-28 with a long-running tool that streams progress before its result.
Run it on loopback and add it as a `local` connection, as the README beside it describes; `backend/tests/test_mcp_progress_example.py` drives it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

PROTOCOL_VERSION = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
MAX_STEPS = 20

TOOLS = [
    {
        "name": "echo",
        "description": "Return the text it was given.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "long_task",
        "description": "Count through a number of steps, reporting progress after each one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {"type": "integer", "minimum": 1, "maximum": MAX_STEPS},
                "step_seconds": {"type": "number", "minimum": 0, "maximum": 5},
            },
            "required": ["steps"],
        },
    },
]


def _reply(ident: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": {"resultType": "complete", **result}}


def _error(ident: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _text(ident: object, text: str, is_error: bool = False) -> dict:
    return _reply(ident, {"content": [{"type": "text", "text": text}], "isError": is_error})


def _event(message: dict) -> bytes:
    return f"event: message\ndata: {json.dumps(message)}\n\n".encode()


async def _long_task(ident: object, arguments: dict, token: object) -> AsyncIterator[bytes]:
    steps, pause = arguments["steps"], arguments.get("step_seconds", 0)
    for done in range(1, steps + 1):
        await asyncio.sleep(pause)
        if token is not None:
            params = {"progressToken": token, "progress": done, "total": steps, "message": f"step {done} of {steps}"}
            yield _event({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})
    yield _event(_text(ident, f"finished {steps} steps"))


def _valid_steps(arguments: dict) -> bool:
    steps, pause = arguments.get("steps"), arguments.get("step_seconds", 0)
    numbers = isinstance(pause, int | float) and not isinstance(pause, bool) and 0 <= pause <= 5
    return isinstance(steps, int) and not isinstance(steps, bool) and 1 <= steps <= MAX_STEPS and numbers


async def mcp(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(_error(None, -32600, "invalid request"), status_code=400)
    ident, method = body.get("id"), body.get("method")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    if method != "server/discover" and (
        request.headers.get("mcp-protocol-version"),
        meta.get(VERSION_KEY),
    ) != (PROTOCOL_VERSION, PROTOCOL_VERSION):
        return JSONResponse(
            {"error": "UnsupportedProtocolVersionError", "supportedVersions": [PROTOCOL_VERSION]}, status_code=400
        )
    if method == "server/discover":
        return JSONResponse(
            _reply(ident, {"supportedVersions": [PROTOCOL_VERSION], "capabilities": {"tools": {}}})
        )
    if method == "tools/list":
        return JSONResponse(_reply(ident, {"tools": TOOLS}))
    if method != "tools/call":
        return JSONResponse(_error(ident, -32601, "unknown method"))
    name = params.get("name")
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    if name == "echo":
        text = arguments.get("text")
        if not isinstance(text, str):
            return JSONResponse(_error(ident, -32602, "text must be a string"))
        return JSONResponse(_text(ident, text))
    if name == "long_task":
        if not _valid_steps(arguments):
            return JSONResponse(_error(ident, -32602, f"steps must be 1 to {MAX_STEPS}, step_seconds 0 to 5"))
        token = meta.get("progressToken")
        return StreamingResponse(_long_task(ident, arguments, token), media_type="text/event-stream")
    return JSONResponse(_error(ident, -32602, "unknown tool"))


app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the example MCP server on loopback.")
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=options.port, log_level="warning")


if __name__ == "__main__":
    main()
