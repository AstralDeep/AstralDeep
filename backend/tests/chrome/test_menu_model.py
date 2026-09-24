"""Tests for webrender/chrome/menu_model.py, the single source every client (web,
Windows, Android) renders from: topbar/menu order, admin gating, and serialization
shape.
"""

import pytest

from webrender.chrome.menu_model import (
    MODEL_VERSION,
    build_menu_model,
    menu_model_dict,
)
from orchestrator.projection_surfaces import SURFACE_MODULES


@pytest.fixture(autouse=True)
def _pulse_off(monkeypatch):
    monkeypatch.delenv("FF_PULSE_DIGEST", raising=False)


def test_topbar_order_non_admin_pulse_off():
    m = build_menu_model(["user"], pulse_enabled=False)
    assert [c.key for c in m.topbar] == ["brand", "status", "timeline", "settings"]
    kinds = {c.key: c.kind for c in m.topbar}
    assert kinds == {"brand": "brand", "status": "status", "timeline": "action", "settings": "menu"}


def test_topbar_pulse_present_only_when_enabled():
    on = build_menu_model(["user"], pulse_enabled=True)
    assert [c.key for c in on.topbar] == ["brand", "status", "pulse", "timeline", "settings"]
    keys = [c.key for c in on.topbar]
    assert keys.index("pulse") < keys.index("timeline")
    pulse = next(c for c in on.topbar if c.key == "pulse")
    assert pulse.action.surface == "pulse"
    assert pulse.icon == "sparkle"


def test_timeline_control_opens_workspace_timeline():
    m = build_menu_model(["user"], pulse_enabled=False)
    tl = next(c for c in m.topbar if c.key == "timeline")
    assert tl.action.surface == "workspace_timeline"
    assert tl.icon == "history"


def test_settings_control_is_menu_kind_no_action():
    m = build_menu_model(["user"], pulse_enabled=False)
    gear = next(c for c in m.topbar if c.key == "settings")
    assert gear.kind == "menu" and gear.action is None and gear.icon == "gear"


def test_account_and_help_groups_exact_order_and_labels():
    m = build_menu_model(["user"], pulse_enabled=False, byo_enabled=False, remote_enabled=False)
    assert [g.key for g in m.menu] == ["account", "help"]
    account = m.menu[0]
    assert account.label == "Account"
    assert [(i.key, i.label, i.surface) for i in account.items] == [
        ("agents", "Agents & permissions", "agents"),
        ("llm", "LLM settings", "llm"),
        ("personalization", "Personalization", "personalization"),
        ("audit", "Audit log", "audit"),
        ("theme", "Theme", "theme"),
    ]
    help_ = m.menu[1]
    assert help_.label == "Help"
    assert [(i.key, i.label, i.surface) for i in help_.items] == [
        ("tour", "Take the tour", "tour"),
        ("guide", "User guide", "guide"),
    ]


def test_admin_group_present_for_admin_absent_otherwise():
    admin = build_menu_model(["admin", "user"], pulse_enabled=False)
    assert [g.key for g in admin.menu] == ["account", "help", "admin"]
    grp = admin.menu[-1]
    assert grp.label == "Admin tools" and grp.admin_only is True
    assert [(i.key, i.label, i.surface, i.params) for i in grp.items] == [
        ("tool-quality", "Tool quality", "admin_tools", {"tab": "quality"}),
        ("tutorial-admin", "Tutorial admin", "admin_tools", {"tab": "tutorial"}),
        ("system-llm", "System LLM", "llm_system", {}),
    ]
    for roles in (["user"], [], None):
        assert all(g.key != "admin" for g in build_menu_model(roles, pulse_enabled=False).menu)


def test_include_admin_false_makes_admin_web_only():
    native = build_menu_model(["admin", "user"], pulse_enabled=False, include_admin=False)
    assert [g.key for g in native.menu] == ["account", "help"]
    web = build_menu_model(["admin", "user"], pulse_enabled=False)
    assert [g.key for g in web.menu] == ["account", "help", "admin"]


def test_every_menu_item_surface_resolves():
    m = build_menu_model(["admin", "user"], pulse_enabled=True)
    for g in m.menu:
        for item in g.items:
            assert item.surface in SURFACE_MODULES, f"unknown surface {item.surface}"
    for c in m.topbar:
        if c.action is not None:
            assert c.action.surface in SURFACE_MODULES


def test_signout_is_danger_logout():
    so = build_menu_model(["user"]).signout
    assert (so.key, so.label, so.style, so.action) == ("signout", "Sign out", "danger", "logout")


def test_to_dict_shape_and_version():
    d = menu_model_dict(["admin", "user"], pulse_enabled=True)
    assert d["version"] == MODEL_VERSION == 2
    assert set(d.keys()) == {"version", "topbar", "menu", "signout"}
    assert [c["key"] for c in d["topbar"]] == ["brand", "status", "pulse", "timeline", "settings"]
    assert [g["key"] for g in d["menu"]] == ["account", "help", "admin"]
    agents = d["menu"][0]["items"][0]
    assert agents == {
        "key": "agents", "label": "Agents & permissions", "surface": "agents",
        "params": {}, "admin_only": False,
    }
    tq = d["menu"][2]["items"][0]
    assert tq["params"] == {"tab": "quality"} and tq["admin_only"] is True
    assert d["signout"] == {"key": "signout", "label": "Sign out", "style": "danger", "action": "logout"}


def test_to_dict_non_admin_has_no_admin_anything():
    d = menu_model_dict(["user"], pulse_enabled=False)
    assert all(g["key"] != "admin" for g in d["menu"])
    flat = str(d)
    for marker in ("admin_tools", "Tool quality", "Tutorial admin",
                   "System LLM", "llm_system"):
        assert marker not in flat


def test_connections_absent_by_default_present_when_enabled():
    off = build_menu_model(["user"], pulse_enabled=False)
    assert "connections" not in [i.key for g in off.menu for i in g.items]
    on = build_menu_model(["user"], pulse_enabled=False, connections_enabled=True)
    account = next(g for g in on.menu if g.key == "account")
    assert [(i.key, i.label, i.surface) for i in account.items][-1] == (
        "connections", "Connections", "connections")


def test_connections_surface_resolves_in_the_host_registry():
    m = build_menu_model(["user"], pulse_enabled=False, connections_enabled=True)
    account = next(g for g in m.menu if g.key == "account")
    item = next(i for i in account.items if i.key == "connections")
    assert item.surface in SURFACE_MODULES


def test_pulse_requires_an_explicit_host_policy_input(monkeypatch):
    monkeypatch.setenv("FF_PULSE_DIGEST", "on")
    assert not any(c.key == "pulse" for c in build_menu_model(["user"]).topbar)
    assert any(
        c.key == "pulse"
        for c in build_menu_model(["user"], pulse_enabled=True).topbar
    )
