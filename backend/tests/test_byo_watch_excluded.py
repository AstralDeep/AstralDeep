"""Tests for watch exclusion from BYO authoring (backend/orchestrator/chrome_events.py,
AstralProjection's menu_model.py): the flag-gated 'My agents' entry point and the
watch's absence from the chrome SDUI device list.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from webrender.chrome import menu_model as mm  # noqa: E402
from orchestrator import chrome_events as ce  # noqa: E402


def _authoring_items(model):
    out = []
    for group in model.menu:
        for item in group.items:
            if item.surface == "agent_authoring":
                out.append(item)
    return out


def test_my_agents_item_present_only_when_flag_on():
    on = _authoring_items(mm.build_menu_model([], byo_enabled=True))
    off = _authoring_items(mm.build_menu_model([], byo_enabled=False))
    assert len(on) == 1, "exactly one BYO authoring entry point"
    assert on[0].key == "my-agents" and on[0].label == "My agents & skills"
    assert off == [], "flag off ⇒ no authoring affordance on any client"


def test_my_agents_is_the_only_authoring_surface_entry_point():
    for roles in ([], ["user"], ["admin"], ["user", "admin"]):
        model = mm.build_menu_model(roles, byo_enabled=True)
        assert len(_authoring_items(model)) == 1


def test_watch_absent_from_chrome_sdui_device_list():
    assert "watch" not in ce._NATIVE_SDUI_DEVICE_TYPES
    assert set(ce._NATIVE_SDUI_DEVICE_TYPES) == {"windows", "android", "ios", "macos"}


def test_watch_menu_and_authoring_surface_unreachable_end_to_end():
    model = mm.build_menu_model(["user"], byo_enabled=True)
    assert len(_authoring_items(model)) == 1
    assert "watch" not in ce._NATIVE_SDUI_DEVICE_TYPES
    for native in ("windows", "android", "ios", "macos"):
        assert native in ce._NATIVE_SDUI_DEVICE_TYPES
