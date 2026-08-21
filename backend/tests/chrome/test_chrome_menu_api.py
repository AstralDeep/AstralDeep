"""Feature 042 — GET /api/chrome/menu + single-source equivalence.

Verifies the REST delivery channel, role-gating, and that the REST body, the
`chrome_menu` WS frame, and the web `render_topbar` all derive from the ONE
builder (Constitution XII — no divergence).
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api import chrome_router
from orchestrator.auth import get_current_user_payload
from orchestrator.chrome_availability import projection_chrome_availability
from shared.protocol import ChromeMenu
from webrender.chrome import render_topbar
from webrender.chrome.menu_model import menu_model_dict


@pytest.fixture(autouse=True)
def _pulse_off(monkeypatch):
    monkeypatch.delenv("FF_PULSE_DIGEST", raising=False)


def _client(payload):
    app = FastAPI()
    app.include_router(chrome_router)
    app.dependency_overrides[get_current_user_payload] = lambda: payload
    return TestClient(app)


def test_native_menu_body_omits_admin_even_for_admins():
    """ADMIN TOOLS is web-only — the native REST channel never sends it, even to
    an admin caller (include_admin=False)."""
    c = _client({"realm_access": {"roles": ["admin", "user"]}})
    r = c.get("/api/chrome/menu")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert [g["key"] for g in body["menu"]] == ["account", "help"]  # no admin group
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
    assert [g["key"] for g in body["menu"]] == ["account", "help"]  # admin is web-only


def test_unauthenticated_401():
    app = FastAPI()
    app.include_router(chrome_router)  # no override → real dependency → 401 without a token
    r = TestClient(app).get("/api/chrome/menu")
    assert r.status_code == 401


def test_rest_body_equals_ws_frame_model():
    """REST and the chrome_menu WS frame serialize the SAME model."""
    for roles in (["user"], ["admin", "user"]):
        c = _client({"realm_access": {"roles": roles}})
        rest = c.get("/api/chrome/menu").json()
        # Both native channels omit admin AND tour (web-only) — mirroring the
        # actual WS emission (orchestrator.py register_ui path, feature 043).
        frame = json.loads(ChromeMenu(model=menu_model_dict(
            roles,
            include_admin=False,
            include_tour=False,
            **projection_chrome_availability(),
        )).to_json())
        assert frame["type"] == "chrome_menu"
        assert frame["model"] == rest


def test_rest_body_matches_web_topbar_labels():
    """The web shell (render_topbar) and REST agree on items/order — one source."""
    body = _client({"realm_access": {"roles": ["admin", "user"]}}).get("/api/chrome/menu").json()
    html = render_topbar(
        roles=["admin", "user"],
        **projection_chrome_availability(),
    )
    # Every menu item label the REST model advertises is present in the web DOM,
    # in the same order (the web renders from the same builder).
    labels = [i["label"] for g in body["menu"] for i in g["items"]]
    # HTML escapes & as &amp; — normalize for the containment check.
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
