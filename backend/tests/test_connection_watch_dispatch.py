"""Connection-only watch navigation uses captured ordinary chrome request and close fences.
The existing dispatcher retains role, LLM and registered-owner authority across awaits.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import chrome_events as chrome
from orchestrator.projection_surfaces import connection
from tests import test_connection_guide as fixtures

mock_auth = fixtures.mock_auth


@pytest.fixture
def watch(mock_auth):
    orch = fixtures._PushOrch(device_type="watch")
    orch.ui_sessions[orch.websocket]["realm_access"] = {"roles": ["user"]}
    async def configured(_):
        return True
    orch.llm_configured_for = configured
    return orch


async def open_connection(orch, generation=None):
    generation = generation or str(uuid4())
    assert await chrome.handle_chrome_event(orch, orch.websocket, "chrome_open", {"surface": "connection"},
        "user", request_generation=generation)
    return generation


async def test_watch_recheck_and_close_use_current_native_request(watch):
    opening = await open_connection(watch)
    frame = json.loads(watch.sent[-1])
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == "connection"
    assert frame["request_generation"] == opening
    closing = str(uuid4())
    await chrome.handle_chrome_event(watch, watch.websocket, "chrome_close", {}, "user", request_generation=closing)
    frame = json.loads(watch.sent[-1])
    assert frame["type"] == "chrome_surface" and frame["surface_key"] == ""
    assert frame["request_generation"] == closing
    assert not chrome.open_surface_for(watch, watch.websocket)


@pytest.mark.parametrize("change", ["owner", "registration", "generation", "context", "pending", "request", "close"])
async def test_watch_recheck_rejects_stale_delivery(watch, monkeypatch, change):
    entered, release = asyncio.Event(), asyncio.Event()
    async def paused(*args):
        entered.set()
        await release.wait()
        return {"overall": "ready", "checks": [], "endpoint": {}}
    monkeypatch.setattr(connection, "guide_payload", paused)
    task = asyncio.create_task(open_connection(watch))
    await entered.wait()
    context = watch._connection_contexts[id(watch.websocket)]
    if change == "owner":
        watch.ui_sessions[watch.websocket]["sub"] = "other"
    elif change == "registration":
        watch.ui_sessions[watch.websocket] = dict(watch.ui_sessions[watch.websocket])
    elif change == "generation":
        context.connection_generation = "new-generation"
    elif change == "context":
        watch._connection_contexts[id(watch.websocket)] = SimpleNamespace(**vars(context))
    elif change == "pending":
        context.work_registrations_pending = 1
    elif change == "request":
        chrome.capture_surface_request(watch, watch.websocket, "chrome_open", {"surface": "connection"}, str(uuid4()))
    else:
        await chrome.handle_chrome_event(watch, watch.websocket, "chrome_close", {}, "user", request_generation=str(uuid4()))
        watch.sent.clear()
    release.set()
    await task
    assert watch.sent == []


async def test_watch_profile_failure_is_not_html_fallback(watch):
    def unavailable(_):
        raise RuntimeError("profile unavailable")
    watch.rote.get_profile = unavailable
    await open_connection(watch)
    assert watch.sent == [] and not chrome.open_surface_for(watch, watch.websocket)


async def test_watch_error_is_correlated_native_and_respects_roles(watch, monkeypatch):
    monkeypatch.setattr(connection, "ADMIN_ONLY", True, raising=False)
    generation = await open_connection(watch)
    frame = json.loads(watch.sent[-1])
    assert frame["type"] == "chrome_surface" and frame["request_generation"] == generation
    assert "admin role" in json.dumps(frame)


async def test_watch_legacy_missing_generation_and_registration_deny(watch):
    await chrome.handle_chrome_event(watch, watch.websocket, "chrome_open", {"surface": "connection"}, "user")
    assert watch.sent == []
    watch._connection_contexts.clear()
    await open_connection(watch)
    assert watch.sent == []


async def test_watch_close_rechecks_current_request_after_authority_await(watch, monkeypatch):
    previous = await open_connection(watch)
    watch.sent.clear()
    async def verify(host, socket):
        chrome.capture_surface_request(host, socket, "chrome_open", {"surface": "connection"}, str(uuid4()))
    monkeypatch.setattr(chrome, "_verify_human_delivery", verify)
    assert not await chrome.push_close(watch, watch.websocket, surface_key="connection", request_generation=previous)
    assert watch.sent == [] and chrome.open_surface_for(watch, watch.websocket) == "connection"


async def test_invalid_profile_cannot_turn_watch_connection_into_html(watch):
    watch.rote.get_profile = lambda _: SimpleNamespace(device_type=None)
    await open_connection(watch)
    assert watch.sent == [] and not chrome.open_surface_for(watch, watch.websocket)


async def test_watch_generic_error_stays_native_and_correlated(watch, monkeypatch):
    async def failed(*args):
        raise RuntimeError("private rendering error")
    monkeypatch.setattr(chrome, "_render_surface", failed)
    generation = await open_connection(watch)
    frame = json.loads(watch.sent[-1])
    assert frame["type"] == "chrome_surface" and frame["request_generation"] == generation
    assert frame["components"][0]["variant"] == "error"
    assert "private rendering error" not in json.dumps(frame)


async def test_watch_llm_gate_retains_priority_before_claim(watch):
    async def unconfigured(_):
        return False
    watch.llm_configured_for = unconfigured
    await open_connection(watch)
    assert watch.sent == [] and not chrome.open_surface_for(watch, watch.websocket)


@pytest.mark.parametrize("device,surface,expected", [("watch", "theme", "html"), ("watch", "connection", "watch"),
    ("ios", "connection", "native"), ("ios", "theme", "native"), ("browser", "connection", "html")])
def test_capability_is_specific_to_the_connection_surface(device, surface, expected):
    from rote.capabilities import DeviceProfile
    profile = DeviceProfile.default() if device == "browser" else DeviceProfile.from_dict({"device_type": device})
    host = SimpleNamespace(rote=SimpleNamespace(get_profile=lambda _: profile))
    assert chrome._surface_delivery(host, object(), surface) == expected


async def test_other_watch_surface_keeps_existing_html_disposition(watch, monkeypatch):
    from orchestrator import projection_surfaces
    original = projection_surfaces.get_surface
    async def render(*args):
        return "Other watch surface"
    monkeypatch.setattr(projection_surfaces, "get_surface", lambda key: SimpleNamespace(TITLE="Theme", render=render,
        NO_NAV=True) if key == "theme" else original(key))
    await chrome.handle_chrome_event(watch, watch.websocket, "chrome_open", {"surface": "theme"}, "user",
                                    request_generation=str(uuid4()))
    assert json.loads(watch.sent[-1])["type"] == "chrome_render"


async def test_watch_close_denies_owner_change_during_delivery_authority(watch, monkeypatch):
    await open_connection(watch)
    watch.sent.clear()
    async def verify(host, socket):
        host.ui_sessions[socket] = {"sub": "other"}
    monkeypatch.setattr(chrome, "_verify_human_delivery", verify)
    await chrome.handle_chrome_event(watch, watch.websocket, "chrome_close", {}, "user", request_generation=str(uuid4()))
    assert watch.sent == []
