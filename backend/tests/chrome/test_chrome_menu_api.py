"""Tests for GET /api/chrome/menu (orchestrator/api.py, chrome_availability.py):
role-gated admin visibility, unauthenticated 401, and that the REST body, WS
chrome_menu frame, and web settings rail all derive from one menu-model builder.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api import chrome_router
from orchestrator.auth import get_current_user_payload
from orchestrator.chrome_availability import (
    projection_chrome_availability, projection_native_chrome_availability,
)
from shared.protocol import ChromeMenu
from webrender.chrome.menu_model import menu_model_dict


@pytest.fixture(autouse=True)
def _pulse_off(monkeypatch):
    monkeypatch.delenv("FF_PULSE_DIGEST", raising=False)
    from shared.feature_flags import flags
    original = flags.is_enabled
    monkeypatch.setattr(flags, "is_enabled", lambda name: False if name in {
        "artifact_export", "artifact_sharing"} else original(name))


def _client(payload):
    app = FastAPI()
    app.include_router(chrome_router)
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    return TestClient(app)


def test_native_menu_body_omits_admin_even_for_admins():
    c = _client({"realm_access": {"roles": ["admin", "user"]}})
    r = c.get("/api/chrome/menu")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2
    assert [g["key"] for g in body["menu"]] == ["account", "help"]
    assert "admin_tools" not in json.dumps(body)
    assert [c_["key"] for c_ in body["topbar"]] == ["brand", "status", "timeline", "settings"]
    assert body["signout"] == {"key": "signout", "label": "Sign out", "style": "danger", "action": "logout"}


def test_non_admin_menu_omits_admin():
    c = _client({"realm_access": {"roles": ["user"]}})
    body = c.get("/api/chrome/menu").json()
    assert all(g["key"] != "admin" for g in body["menu"])
    assert "admin_tools" not in json.dumps(body)


def test_admin_via_resource_access_still_web_only():
    c = _client({"resource_access": {"astral-frontend": {"roles": ["admin"]}}})
    body = c.get("/api/chrome/menu").json()
    assert [g["key"] for g in body["menu"]] == ["account", "help"]


def test_unauthenticated_401():
    app = FastAPI()
    app.include_router(chrome_router)
    r = TestClient(app).get("/api/chrome/menu")
    assert r.status_code == 401


def test_rest_body_equals_unnegotiated_native_model():
    for roles in (["user"], ["admin", "user"]):
        c = _client({"realm_access": {"roles": roles}})
        rest = c.get("/api/chrome/menu").json()
        frame = json.loads(ChromeMenu(model=menu_model_dict(
            roles,
            include_admin=False,
            include_tour=False,
            **projection_native_chrome_availability({}),
        )).to_json())
        assert frame["type"] == "chrome_menu"
        assert frame["model"] == rest


def test_rest_body_matches_web_topbar_labels():
    from webrender.chrome import render_settings_nav
    from webrender.chrome.menu_model import build_menu_model

    body = _client({"realm_access": {"roles": ["admin", "user"]}}).get("/api/chrome/menu").json()
    html = render_settings_nav(build_menu_model(
        ["admin", "user"], **projection_chrome_availability()))
    labels = [i["label"] for g in body["menu"] for i in g["items"]]
    html_norm = html.replace("&amp;", "&")
    positions = [html_norm.index(lbl) for lbl in labels]
    assert positions == sorted(positions), "web menu order diverges from the model"


def test_pulse_in_body_when_flag_on(monkeypatch):
    monkeypatch.setenv("FF_PULSE_DIGEST", "on")
    body = _client({"realm_access": {"roles": ["user"]}}).get("/api/chrome/menu").json()
    keys = [c_["key"] for c_ in body["topbar"]]
    assert keys == ["brand", "status", "pulse", "timeline", "settings"]
    pulse = next(c_ for c_ in body["topbar"] if c_["key"] == "pulse")
    assert pulse["action"] == {"surface": "pulse", "params": {}}
