"""Exercise provider-setup Safety navigation through shared rendering and signed owner dispatch.
Ordinary settings remain gated after the owner returns from Safety without configuring a provider.
"""

import json
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import chrome_events, llm_gate
from orchestrator.projection_surfaces import llm
from rote.capabilities import DeviceProfile
from tests.chrome.test_surface_llm import FakeRecorder, FakeStore, make_orch
from tests.test_emergency_stop_web_delivery import dispatch
from tests.test_emergency_stop_postgres import (
    stop as stop, human as human, bound as bound, fixture as fixture, runtime as runtime,
    service as service, signing_key as signing_key, socket_request as socket_request,
)


class Buttons(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.controls = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.controls.append(dict(attrs))


def navigation(frame):
    if frame["type"] == "chrome_render":
        controls = [item for item in Buttons(frame["html"]).controls
                    if item.get("data-action") == "chrome_open"]
        assert len(controls) == 1
        return json.loads(controls[0]["data-payload"])
    controls = [item for item in frame["components"]
                if item.get("type") == "button" and item.get("action") == "chrome_open"]
    assert len(controls) == 1
    assert controls[0]["label"] == "Open emergency stop controls"
    return controls[0]["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["browser", "windows", "android", "ios", "macos"])
async def test_unconfigured_owner_can_open_safety_from_actual_setup_and_remains_gated(
        stop, human, socket_request, fixture, device):
    orch, socket = human[2], socket_request[0]
    frames = []

    async def send(target, wire):
        assert target is socket
        frames.append(json.loads(wire))
        return True

    orch._safe_send = send
    orch._llm_store = FakeStore()
    orch._ff_llm_first_run = True
    orch.llm_configured_for = AsyncMock(return_value=False)
    orch._record_llm_unconfigured = AsyncMock()
    orch.audit_recorder = FakeRecorder()
    orch._llm_audit_principals = lambda _: (fixture[1], fixture[1])
    orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.from_dict({"device_type": device}))
    await llm_gate.push_setup_dialog(orch, socket, fixture[1])
    payload = navigation(frames[-1])
    assert payload == {"surface": "safety"}
    generation = await dispatch(human, socket_request, "chrome_open", payload, fixture[1])
    safety = frames[-1]
    assert safety["surface_key"] == "safety" and safety["request_generation"] == generation
    assert safety["mode"] == "replace"
    assert "Stop everything now" in json.dumps(safety)
    assert llm_gate.is_gated(orch, socket)
    assert not await orch.llm_configured_for(fixture[1])
    assert orch._record_llm_unconfigured.await_count == 0
    await chrome_events.handle_chrome_event(orch, socket, "chrome_close", {}, fixture[1])
    assert llm_gate.is_gated(orch, socket)
    assert navigation(frames[-1]) == payload
    await chrome_events.handle_chrome_event(orch, socket, "chrome_open", {"surface": "theme"}, fixture[1])
    assert navigation(frames[-1]) == payload
    assert orch._record_llm_unconfigured.await_count == 2
    assert llm_gate.is_gated(orch, socket)
    assert await orch._llm_store.get(fixture[1]) is None


@pytest.mark.asyncio
async def test_regular_provider_settings_do_not_add_a_first_run_navigation_control():
    orch = make_orch()
    html = await llm.render(orch, "owner", ["user"], {})
    components = await llm.components(orch, "owner", ["user"], {})
    assert all(item.get("data-action") != "chrome_open" for item in Buttons(html).controls)
    assert all(item.get("action") != "chrome_open" for item in components)
