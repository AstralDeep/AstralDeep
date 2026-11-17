"""Live-system WS smoke test: registers against the orchestrator's real /ws endpoint
with an operator-supplied access token (TEST_UI_TOKEN) and asserts the handshake
completes; also a manual long-form multi-agent flow runner.
"""

import asyncio
import json
import os
import sys

import pytest
import websockets
from dotenv import load_dotenv
load_dotenv()

QUERY = (
    "search all the patients, graph their age, then do an arxiv search about "
    "the main disease in this patient population, then give me the system "
    "stats (cpu, memory, storage, all of it)"
)
URI = f"ws://localhost:{os.getenv('ORCHESTRATOR_PORT', '8001')}/ws"
ACCESS_TOKEN = os.getenv("TEST_UI_TOKEN", "")


def _register_message() -> dict:
    return {
        "type": "register_ui",
        "token": ACCESS_TOKEN,
        "capabilities": ["text", "images"],
        "session_id": "test_agent_flow",
    }


async def _register(websocket) -> None:
    await websocket.send(json.dumps(_register_message()))
    while True:
        resp = json.loads(await asyncio.wait_for(websocket.recv(), timeout=30))
        msg_type = resp.get("type")
        if msg_type == "system_config":
            return
        if msg_type == "auth_required":
            raise AssertionError(
                "register_ui was refused (auth_required): TEST_UI_TOKEN is not a "
                "valid access token for this orchestrator"
            )
        if msg_type == "error":
            raise AssertionError(f"register_ui error: {resp.get('message')}")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_register_handshake():
    import socket as _socket

    host, _, port = URI.partition("ws://")[2].partition("/")[0].partition(":")
    try:
        await asyncio.to_thread(_socket.create_connection, (host, int(port or 8001)), 2)
    except OSError:
        pytest.skip(f"live orchestrator not listening at {URI}")
    if not ACCESS_TOKEN:
        pytest.skip("TEST_UI_TOKEN is not set")
    last_exc: Exception | None = None
    for _attempt in range(3):
        try:
            async with websockets.connect(URI) as websocket:
                await _register(websocket)
            return
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            last_exc = exc
            await asyncio.sleep(2)
    if isinstance(last_exc, OSError) and "Errno 111" in str(last_exc):
        pytest.skip(f"live orchestrator not reachable at {URI}: {last_exc}")
    pytest.fail(f"Could not complete WS registration at {URI} after 3 attempts: {last_exc}")


async def run_full_flow() -> None:
    if not ACCESS_TOKEN:
        raise SystemExit("Set TEST_UI_TOKEN to an access token for this orchestrator.")
    print(f"Connecting to {URI}...")
    async with websockets.connect(URI) as websocket:
        await _register(websocket)
        print("System ready.")

        await websocket.send(json.dumps({
            "type": "ui_event",
            "action": "chat_message",
            "payload": {"message": QUERY},
        }))
        print(f"Sent query: {QUERY}")

        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < 240:
            resp = json.loads(await websocket.recv())
            msg_type = resp.get("type")
            if msg_type == "chat_status":
                print(f"STATUS: {resp.get('status')} - {resp.get('message')}")
            elif msg_type in ("ui_render", "ui_upsert"):
                components = resp.get("components", [])
                ops = resp.get("ops", [])
                print(f"{msg_type.upper()}: {len(components) or len(ops)} item(s)")
                for c in components:
                    print(f"  - [{c.get('type', 'unknown')}] {c.get('title', 'No Title')}")
                    if c.get("title") == "Analysis":
                        print("--- FINAL ANALYSIS RECEIVED ---")
                        return
            elif msg_type == "error":
                print(f"ERROR: {resp.get('message')}")
        print("Timeout waiting for full flow.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run_full_flow())
    except KeyboardInterrupt:
        print("Stopped.")
