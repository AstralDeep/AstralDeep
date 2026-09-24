"""Minimal WebSocket echo endpoint mirroring sse_transport.py for academic
latency/throughput comparison; exercised by
qual_audit/suites/test_transport_comparison.py.
"""

import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


def create_ws_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/echo")
    async def ws_echo(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                msg["server_timestamp"] = time.time()
                await websocket.send_text(json.dumps(msg))
        except WebSocketDisconnect:
            pass

    return router
