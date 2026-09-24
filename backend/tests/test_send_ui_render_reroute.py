"""Tests that Orchestrator.send_ui_render reroutes an alert-only frame to the chat panel
regardless of variant, while a frame with any non-alert component keeps its canvas
target.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from orchestrator.orchestrator import Orchestrator
from rote.capabilities import DeviceProfile


class FakeRote:
    def adapt(self, websocket, components):
        return components

    def get_profile(self, websocket):
        return DeviceProfile.default()


def _orch():
    orch = types.SimpleNamespace(rote=FakeRote(), sent=[])

    async def _safe_send(websocket, data):
        orch.sent.append(json.loads(data))
        return True

    orch._safe_send = _safe_send
    orch.send_ui_render = types.MethodType(Orchestrator.send_ui_render, orch)
    return orch


def _send(components, target="canvas"):
    orch = _orch()
    asyncio.new_event_loop().run_until_complete(
        orch.send_ui_render(object(), components, target=target))
    assert len(orch.sent) == 1 and orch.sent[0]["type"] == "ui_render"
    return orch.sent[0]


def test_error_alert_only_frame_reroutes_to_chat():
    frame = _send([{"type": "alert", "variant": "error", "message": "boom"}])
    assert frame["target"] == "chat"


@pytest.mark.parametrize("variant", ["warning", "info", "success"])
def test_any_variant_alert_only_frame_reroutes_to_chat(variant):
    frame = _send([{"type": "alert", "variant": variant,
                    "message": "Couldn't authorize that action."}])
    assert frame["target"] == "chat"


def test_multiple_mixed_variant_alerts_reroute_to_chat():
    frame = _send([
        {"type": "alert", "variant": "warning", "message": "w"},
        {"type": "alert", "variant": "error", "message": "e"},
        {"type": "alert", "message": "no variant at all"},
    ])
    assert frame["target"] == "chat"


def test_frame_with_non_alert_component_keeps_canvas_target():
    frame = _send([
        {"type": "alert", "variant": "warning", "message": "w"},
        {"type": "text", "content": "real content"},
    ])
    assert frame["target"] == "canvas"


def test_non_alert_frame_keeps_canvas_target():
    frame = _send([{"type": "text", "content": "hello"}])
    assert frame["target"] == "canvas"


def test_explicit_chat_target_unchanged():
    frame = _send([{"type": "text", "content": "hello"}], target="chat")
    assert frame["target"] == "chat"
