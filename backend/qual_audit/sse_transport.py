"""Minimal SSE echo/broadcast endpoint built solely for academic WebSocket-vs-SSE
benchmarking, not production transport; exercised by
qual_audit/suites/test_transport_comparison.py.
"""

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator, Dict

from starlette.requests import Request
from starlette.responses import StreamingResponse
from fastapi import APIRouter


def create_sse_router() -> APIRouter:
    router = APIRouter()

    _connections: Dict[str, asyncio.Queue] = {}

    async def _event_stream(
        queue: asyncio.Queue, conn_id: str
    ) -> AsyncGenerator[str, None]:
        try:
            while True:
                msg = await queue.get()
                if msg is None:
                    break
                event_id = msg.get("id", str(uuid.uuid4()))
                data = json.dumps(msg)
                yield f"id: {event_id}\nevent: message\ndata: {data}\n\n"
        finally:
            _connections.pop(conn_id, None)

    @router.get("/sse")
    async def sse_endpoint(request: Request):
        conn_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        _connections[conn_id] = queue

        await queue.put({
            "type": "connected",
            "connection_id": conn_id,
            "timestamp": time.time(),
        })

        return StreamingResponse(
            _event_stream(queue, conn_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Connection-Id": conn_id,
            },
        )

    @router.post("/sse/send/{conn_id}")
    async def sse_send(conn_id: str, request: Request):
        queue = _connections.get(conn_id)
        if not queue:
            return {"error": "connection not found"}
        body = await request.json()
        body["server_timestamp"] = time.time()
        body["id"] = body.get("id", str(uuid.uuid4()))
        await queue.put(body)
        return {"status": "sent", "id": body["id"]}

    @router.post("/sse/echo")
    async def sse_echo(request: Request):
        body = await request.json()
        body["server_timestamp"] = time.time()
        return body

    @router.post("/sse/broadcast")
    async def sse_broadcast(request: Request):
        body = await request.json()
        body["server_timestamp"] = time.time()
        body["id"] = body.get("id", str(uuid.uuid4()))
        for queue in _connections.values():
            await queue.put(body)
        return {"status": "broadcast", "connections": len(_connections)}

    @router.delete("/sse/{conn_id}")
    async def sse_disconnect(conn_id: str):
        queue = _connections.get(conn_id)
        if queue:
            await queue.put(None)
        return {"status": "disconnected"}

    router._connections = _connections  # type: ignore[attr-defined]

    return router
