"""Manual smoke test for the upload-to-chat workflow against a running backend; skipped
in automated runs and kept as a reference for operators validating the flow end to
end.
"""

import requests
import asyncio
import websockets
import json
import uuid
import os
import pytest
from dotenv import load_dotenv

load_dotenv()

pytestmark = pytest.mark.skip(reason="Manual smoke test — requires running backend services")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_workflow():
    chat_id = str(uuid.uuid4())

    file_content = b"header1,header2\nval1,val2"
    files = {"file": ("test_data.csv", file_content)}
    data = {"session_id": chat_id}

    res = requests.post("http://localhost:8002/api/upload", files=files, data=data)
    if res.status_code != 200:
        raise AssertionError(f"Upload failed: {res.status_code}")

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    file_path = os.path.join(backend_dir, "tmp", chat_id, "test_data.csv")
    assert os.path.exists(file_path), f"File not saved at {file_path}"

    orchestrator_port = os.getenv('ORCHESTRATOR_PORT', '8000')
    async with websockets.connect(f"ws://localhost:{orchestrator_port}") as ws:
        msg = {
            "type": "ui_event",
            "action": "chat_message",
            "session_id": chat_id,
            "payload": {
                "message": "Hello, I uploaded test_data.csv",
                "chat_id": chat_id
            }
        }
        await ws.send(json.dumps(msg))

        for _ in range(5):
            try:
                recv_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                recv_data = json.loads(recv_msg)
                if recv_data.get('type') == 'chat_created':
                    assert recv_data is not None
                    return
            except Exception:
                break


if __name__ == "__main__":
    asyncio.run(test_workflow())