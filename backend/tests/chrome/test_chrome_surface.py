"""Feature 043 — native SDUI settings-surface delivery.

DB-free: a fake orchestrator (with a ROTE profile) + stub surfaces exercise the
device-target branch in ``chrome_events._render_surface`` (web → ChromeRender
HTML, native → ChromeSurface components), the not-yet-converted placeholder, the
native admin gate, the handler re-render-as-ChromeSurface path, the ``_sdui``
helpers, and the menu-model ``include_tour`` filter (spec FR-009).
"""
import asyncio
import json
import sys
import types

import pytest

from orchestrator import chrome_events
from rote.capabilities import DeviceProfile


class FakeWS:
    pass


class FakeRote:
    def __init__(self, profile):
        self._p = profile

    def get_profile(self, websocket):
        return self._p


class FakeOrch:
    def __init__(self, roles=("user",), device="browser"):
        self.ws = FakeWS()
        self.ui_sessions = {self.ws: {"realm_access": {"roles": list(roles)}}}
        self.sent = []
        prof = (DeviceProfile.default() if device == "browser"
                else DeviceProfile.from_dict({"device_type": device}))
        self.rote = FakeRote(prof)

    async def _safe_send(self, websocket, data):
        self.sent.append(json.loads(data))
        return True


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def stub_surfaces(monkeypatch):
    """Register two stub surfaces: 'stub_sdui' (has components()) and
    'stub_html' (render() only — not yet converted)."""
    from webrender.chrome.surfaces import _sdui

    sdui_mod = types.ModuleType("tests.stub_sdui_surface")
    sdui_mod.TITLE = "Stub SDUI"

    async def sdui_render(orch, user_id, roles, params):
        return "<div id='stub-html'>web</div>"

    async def sdui_components(orch, user_id, roles, params):
        return [_sdui.text("hello", "h2"), _sdui.button("Save", "chrome_stub_save")]

    async def _save(orch, websocket, user_id, roles, payload):
        return ("stub_sdui", {"saved": True},
                '<div class="astral-chrome-notice text-green-400">Saved</div>')

    sdui_mod.render = sdui_render
    sdui_mod.components = sdui_components
    sdui_mod.HANDLERS = {"chrome_stub_save": _save}
    sys.modules["tests.stub_sdui_surface"] = sdui_mod

    html_mod = types.ModuleType("tests.stub_html_surface")
    html_mod.TITLE = "Stub HTML"

    async def html_render(orch, user_id, roles, params):
        return "<div id='html-only'>web</div>"

    html_mod.render = html_render  # no components() → not yet converted
    sys.modules["tests.stub_html_surface"] = html_mod

    from orchestrator import projection_surfaces as reg
    monkeypatch.setitem(reg.SURFACE_MODULES, "stub_sdui", "tests.stub_sdui_surface")
    monkeypatch.setitem(reg.SURFACE_MODULES, "stub_html", "tests.stub_html_surface")
    monkeypatch.setattr(chrome_events, "_HANDLERS", None)
    yield
    monkeypatch.setattr(chrome_events, "_HANDLERS", None)


def _last(orch, frame_type):
    frames = [f for f in orch.sent if f.get("type") == frame_type]
    assert frames, f"no {frame_type} frame pushed (sent: {[f.get('type') for f in orch.sent]})"
    return frames[-1]


def _types(components):
    return [c.get("type") for c in components]


# --- the device-target branch (FR-001/FR-002/FR-003) -------------------------

@pytest.mark.parametrize("device", ["windows", "android"])
def test_native_session_gets_chrome_surface_frame(stub_surfaces, device):
    orch = FakeOrch(device=device)
    handled = run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "stub_sdui"}, "u1"))
    assert handled is True
    frame = _last(orch, "chrome_surface")
    assert frame["surface_key"] == "stub_sdui"
    assert frame["title"] == "Stub SDUI"
    assert frame["region"] == "modal" and frame["admin_only"] is False
    assert "button" in _types(frame["components"])
    # a native client never gets HTML
    assert not any(f.get("type") == "chrome_render" for f in orch.sent)


def test_web_session_still_gets_chrome_render_html(stub_surfaces):
    orch = FakeOrch(device="browser")
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "stub_sdui"}, "u1"))
    html = _last(orch, "chrome_render")["html"]
    assert "stub-html" in html and 'role="dialog"' in html  # modal shell HTML
    assert not any(f.get("type") == "chrome_surface" for f in orch.sent)


def test_unconverted_surface_returns_placeholder_component_on_native(stub_surfaces):
    orch = FakeOrch(device="windows")
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "stub_html"}, "u1"))
    frame = _last(orch, "chrome_surface")
    assert frame["surface_key"] == "stub_html"
    assert _types(frame["components"]) == ["alert"]  # exactly one labeled placeholder
    assert "isn't available" in frame["components"][0]["message"]


def test_unknown_surface_on_native_is_not_silent(stub_surfaces):
    orch = FakeOrch(device="android")
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "nope"}, "u1"))
    frame = _last(orch, "chrome_surface")
    assert frame["components"][0]["type"] == "alert"
    assert "Unknown settings surface" in frame["components"][0]["message"]


def test_native_admin_surface_denied_for_non_admin(stub_surfaces):
    orch = FakeOrch(roles=("user",), device="windows")
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "admin_tools"}, "u1"))
    frame = _last(orch, "chrome_surface")
    assert frame["title"] == "Not authorized"
    assert "admin role" in frame["components"][0]["message"]


def test_handler_rerender_is_chrome_surface_on_native(stub_surfaces):
    orch = FakeOrch(device="windows")
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_stub_save", {}, "u1"))
    frame = _last(orch, "chrome_surface")
    kinds = _types(frame["components"])
    assert kinds[0] == "alert"  # the success notice, mapped to a leading Alert
    assert frame["components"][0]["variant"] == "success"
    assert "button" in kinds  # plus the re-rendered surface components


# --- feature 044: device-aware error/close paths (FR-002/FR-017) -------------

@pytest.mark.parametrize("device", ["windows", "android"])
def test_unknown_action_is_visible_on_native(stub_surfaces, device):
    orch = FakeOrch(device=device)
    handled = run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_totally_bogus", {}, "u1"))
    assert handled is True
    frame = _last(orch, "chrome_surface")
    assert frame["title"] == "Not available"
    assert "Unknown action" in frame["components"][0]["message"]
    assert not any(f.get("type") == "chrome_render" for f in orch.sent)


def test_unknown_action_keeps_html_on_web(stub_surfaces):
    orch = FakeOrch(device="browser")
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_totally_bogus", {}, "u1"))
    assert "Unknown action" in _last(orch, "chrome_render")["html"]


@pytest.mark.parametrize("device", ["windows", "android"])
def test_uncaught_handler_failure_is_visible_on_native(stub_surfaces, device, monkeypatch):
    async def boom(orch, websocket, user_id, roles, payload):
        raise RuntimeError("kaput")

    handlers = dict(chrome_events._handlers())
    handlers["chrome_boom"] = ("stub_sdui", boom)
    monkeypatch.setattr(chrome_events, "_HANDLERS", handlers)

    orch = FakeOrch(device=device)
    handled = run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_boom", {}, "u1"))
    assert handled is True
    frame = _last(orch, "chrome_surface")
    assert frame["title"] == "Something went wrong"
    assert frame["components"][0]["type"] == "alert"
    assert frame["components"][0]["variant"] == "error"


def test_admin_denied_action_is_visible_on_native(stub_surfaces, monkeypatch):
    async def noop(orch, websocket, user_id, roles, payload):
        return None

    handlers = dict(chrome_events._handlers())
    handlers["chrome_admin_thing"] = ("admin_tools", noop)
    monkeypatch.setattr(chrome_events, "_HANDLERS", handlers)

    orch = FakeOrch(roles=("user",), device="android")
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_admin_thing", {}, "u1"))
    frame = _last(orch, "chrome_surface")
    assert frame["title"] == "Not authorized"
    assert "admin role" in frame["components"][0]["message"]


@pytest.mark.parametrize("device", ["windows", "android"])
def test_chrome_close_clears_native_modal_with_empty_components(stub_surfaces, device):
    orch = FakeOrch(device=device)
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_close", {}, "u1"))
    frame = _last(orch, "chrome_surface")
    assert frame["components"] == []  # documented clear-modal form
    assert not any(f.get("type") == "chrome_render" for f in orch.sent)


def test_chrome_close_still_clears_web_modal_html(stub_surfaces):
    orch = FakeOrch(device="browser")
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_close", {}, "u1"))
    assert _last(orch, "chrome_render")["html"] == ""


# --- _sdui helpers (FR + research D2) ----------------------------------------

def test_sdui_form_is_parampicker_action_submit():
    from webrender.chrome.surfaces import _sdui
    f = _sdui.form(
        [_sdui.field("base_url", "Base URL", "text"),
         _sdui.field("api_key", "API key", "password")],
        submit_action="chrome_llm_save", submit_label="Save",
        submit_payload={"tab": "soul"})
    assert f["type"] == "param_picker"
    assert f["submit_action"] == "chrome_llm_save"          # action-submit binding
    assert f["submit_payload"] == {"tab": "soul"}
    assert [x["name"] for x in f["fields"]] == ["base_url", "api_key"]
    assert f["fields"][1]["kind"] == "password"             # new field kind


def test_sdui_placeholder_is_a_labeled_alert():
    from webrender.chrome.surfaces import _sdui
    p = _sdui.placeholder("Theme")
    assert p["type"] == "alert" and "Theme" in p["message"]


# --- menu-model: "Take the tour" is web-only on native (FR-009) --------------

def test_native_menu_omits_take_the_tour():
    from webrender.chrome.menu_model import menu_model_dict
    native = menu_model_dict(["user"], include_admin=False, include_tour=False)
    help_groups = [g for g in native["menu"] if g["key"] == "help"]
    assert help_groups, "help group present"
    surfaces = [i["surface"] for g in help_groups for i in g["items"]]
    assert "tour" not in surfaces
    assert "guide" in surfaces  # the other Help item stays


def test_web_menu_keeps_take_the_tour():
    from webrender.chrome.menu_model import menu_model_dict
    web = menu_model_dict(["user"])  # defaults: include_tour=True
    surfaces = [i["surface"] for g in web["menu"] for i in g["items"]]
    assert "tour" in surfaces and "guide" in surfaces
