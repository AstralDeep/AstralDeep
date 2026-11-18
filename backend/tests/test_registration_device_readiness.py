"""Verifies device updates preserve the latest profile without publishing registration readiness early.
Exercises orchestrator registration and device handling across delayed and failed authentication.
"""

import json
from unittest.mock import AsyncMock

import pytest

from tests.test_register_ui_auth_required import (
    auth_audit as auth_audit,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def ws_audit(monkeypatch):
    import audit.hooks

    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())


def device(width):
    return {"device_type": "windows", "viewport_width": width, "viewport_height": 720}


async def update(host, socket, width):
    await host.handle_ui_message(socket, json.dumps({
        "type": "ui_event", "action": "update_device", "payload": {"device": device(width)}}))
    assert host.rote.get_profile(socket).capabilities.viewport_width == width


def configurations(host):
    return [frame for _, frame in host._sent if frame["type"] == "rote_config"]


async def test_pending_scoped_device_update_is_refused_without_readiness():
    from tests.test_viewport_hydration import frames, harness, message

    host, socket, _binding, _authority = harness()
    host._connection_contexts[id(socket)].work_registrations_pending = 1
    event = message()
    await host.handle_ui_message(socket, event.to_json())
    assert host.rote.get_profile(socket).capabilities.viewport_width == 320
    delivered = frames(host)
    assert len(delivered) == 1 and delivered[0]["type"] == "error"
    assert delivered[0]["code"] == "viewport_snapshot_rejected"
    assert delivered[0]["request_generation"] == event.request_generation
    host.conversation_commits.build_snapshot.assert_not_called()
