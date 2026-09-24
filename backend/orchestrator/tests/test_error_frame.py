"""Tests that orchestrator/orchestrator.py's handle_ui_message emits a generic error
frame on an unhandled failure, instead of only logging and leaving the client stuck
'thinking'.
"""

import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator.orchestrator import Orchestrator  # noqa: E402


class _FakeSelf:
    def __init__(self):
        self.sent = []

    async def _safe_send(self, websocket, data):
        self.sent.append(json.loads(data))
        return True


def test_generic_ui_failure_emits_internal_error_frame():
    fake = _FakeSelf()
    asyncio.new_event_loop().run_until_complete(
        Orchestrator.handle_ui_message(fake, object(), "{not json")
    )
    assert fake.sent, "outer catch must emit an error frame, not just log"
    frame = fake.sent[-1]
    assert frame["type"] == "error"
    assert frame["code"] == "internal"
    assert frame["message"]
