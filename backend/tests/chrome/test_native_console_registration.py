"""Exercises negotiated console delivery through authenticated native registration.
Signed test credentials follow production verification while catalog visibility stays owner scoped.
"""

import asyncio
import json

import pytest

from orchestrator import chrome_availability
from tests.chrome.test_surface_agents import make_orch
from tests.test_register_ui_auth_required import (
    _FakeWS, _make_fake, _register_msg, auth_audit as auth_audit,
)
from tests.test_request_session_authority_088 import (
    fixture as fixture, runtime as runtime, signing_key as signing_key,
)


TYPES = ["text", "alert", "badge", "card", "container", "button", "keyvalue"]


@pytest.fixture
def registered_host(fixture, auth_audit, monkeypatch):
    host = _make_fake()
    catalog = make_orch()
    catalog.history.db.users[fixture[1]] = {"email": "alice@example.com"}
    for name in ("agent_cards", "plane_repository_source", "_is_draft_agent", "user_agent_registry"):
        setattr(host, name, getattr(catalog, name))
    monkeypatch.setattr(chrome_availability, "projection_chrome_availability", lambda: {
        "work_enabled": True, "notes_enabled": True, "connections_enabled": True,
        "export_enabled": True, "share_enabled": True,
    })
    return host


async def register(host, fixture, device, capabilities):
    before = asyncio.all_tasks()
    socket = _FakeWS()
    await host.handle_ui_message(socket, _register_msg(
        token=fixture[3](), device=device, capabilities=capabilities))
    tasks = asyncio.all_tasks() - before
    await asyncio.gather(*tasks, return_exceptions=True)
    return socket, [value for _, value in host._sent if value["type"] == "chrome_menu"]


@pytest.mark.parametrize("capabilities", [[], ["guidance_selection_v1"],
    ["guidance_notes_v1", "guidance_selection_v1", "work_read_v1"]])
async def test_negotiated_watch_registration_delivers_console_without_legacy_capability_gate(
    registered_host, fixture, capabilities,
):
    socket, frames = await register(registered_host, fixture, {
        "device_type": "watch", "console_contract": "console/v2", "viewport_width": 205,
        "viewport_height": 251, "supported_types": TYPES,
    }, capabilities)
    frame, = frames
    model = frame["model"]
    assert registered_host.ui_sessions[socket]["sub"] == fixture[1]
    assert model["console"]["version"] == 2
    actions = {item["key"]: item for item in model["console"]["composer_actions"]}
    assert actions["advanced"]["availability"]["mode"] == (
        "native" if "guidance_selection_v1" in capabilities else "handoff")
    assert ("work" in actions) == ("work_read_v1" in capabilities)
    assert actions["background"]["availability"]["mode"] == "handoff"
    items = {item["key"]: item for group in model["menu"] for item in group["items"]}
    assert ("guidance" in items) == ("guidance_notes_v1" in capabilities)
    assert items["connections"]["availability"]["mode"] == "handoff"
    assert {row["id"] for row in model["console"]["catalog"]["agents"]} == {"alpha", "beta"}
    assert "private" not in json.dumps(model["console"]["catalog"])
    assert not [control for control in model["topbar"] if control["kind"] == "workspace_action"]


@pytest.mark.parametrize("kind,capabilities,has_menu", [
    ("watch", [], False), ("watch", ["guidance_selection_v1"], False),
    ("watch", ["guidance_notes_v1", "work_read_v1"], True), ("windows", [], True),
])
async def test_unnegotiated_registration_preserves_legacy_native_menu(
    registered_host, fixture, kind, capabilities, has_menu,
):
    _, frames = await register(registered_host, fixture, {"device_type": kind}, capabilities)
    assert bool(frames) is has_menu
    if has_menu:
        assert "console" not in frames[0]["model"]
        if kind == "watch":
            assert frames[0]["model"]["menu"] == []
            assert frames[0]["model"]["signout"] == {}


async def test_rejected_registration_never_delivers_console_catalog(registered_host, fixture):
    await registered_host.handle_ui_message(_FakeWS(), _register_msg(token=fixture[3](exp=1),
        device={"device_type": "watch", "console_contract": "console/v2"}))
    assert registered_host.ui_sessions == {}
    assert [frame["type"] for _, frame in registered_host._sent] == ["auth_required"]
