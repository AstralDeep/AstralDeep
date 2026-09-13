"""Owner-authenticated chrome delivery preserves independent canvas flags."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.api import chrome_router
from orchestrator.auth import get_current_user_payload
from orchestrator.chrome_availability import projection_chrome_availability
from shared.feature_flags import FeatureFlags, flags
from shared.protocol import ChromeMenu
from webrender.chrome.menu_model import menu_model_dict
from webrender.chrome.topbar import render_topbar


def test_workspace_deployment_defaults_remain_export_on_share_off(monkeypatch):
    monkeypatch.delenv("FF_ARTIFACT_EXPORT", raising=False)
    monkeypatch.delenv("FF_ARTIFACT_SHARING", raising=False)
    defaults = FeatureFlags()
    assert defaults.is_enabled("artifact_export") is True
    assert defaults.is_enabled("artifact_sharing") is False


@pytest.mark.parametrize("export,share", [(False, False), (True, False), (False, True), (True, True)])
def test_rest_ws_and_web_resolve_same_workspace_inventory(monkeypatch, export, share):
    configured = {"artifact_export": export, "artifact_sharing": share, "user_skills": True}
    monkeypatch.setattr(flags, "is_enabled", lambda key: configured.get(key, False))
    app = FastAPI()
    app.include_router(chrome_router)
    app.dependency_overrides[get_current_user_payload] = lambda: {"realm_access": {"roles": ["admin", "user"]}}
    response = TestClient(app).get("/api/chrome/menu")
    assert response.status_code == 200
    model = response.json()
    availability = projection_chrome_availability()
    frame = json.loads(ChromeMenu(model=menu_model_dict(
        ["admin", "user"], include_admin=False, include_tour=False, **availability)).to_json())
    assert frame["model"] == model and model["version"] == 2
    assert all(group["key"] != "admin" for group in model["menu"])
    html = render_topbar(["admin", "user"], **availability)
    for enabled, key in ((export, "export"), (share, "share")):
        assert any(item["key"] == key for item in model["topbar"]) is enabled
        assert (f'id="astral-{key}-page-btn"' in html) is enabled


@pytest.mark.parametrize("failed", ["artifact_export", "artifact_sharing", "byo_agents"])
def test_one_unavailable_flag_does_not_hide_unrelated_workspace_controls(monkeypatch, failed):
    def resolve(key):
        if key == failed:
            raise KeyError("unknown flag")
        return True

    monkeypatch.setattr(flags, "is_enabled", resolve)
    availability = projection_chrome_availability()
    assert availability["export_enabled"] is (failed != "artifact_export")
    assert availability["share_enabled"] is (failed != "artifact_sharing")
    if failed != "byo_agents":
        assert all(availability[key] for key in (
            "byo_enabled", "remote_enabled", "computer_enabled", "skills_enabled"))
    model = menu_model_dict(**availability)
    assert any(item["key"] == "timeline" for item in model["topbar"])
    assert any(group["key"] == "account" for group in model["menu"])


def test_workspace_controls_never_make_unauthenticated_menu_public():
    app = FastAPI()
    app.include_router(chrome_router)
    response = TestClient(app).get("/api/chrome/menu")
    assert response.status_code == 401


@pytest.mark.parametrize("capabilities,expected", [
    (["work_read_v1"], True), ([], False), (["render"], False),
    ("work_read_v1", False), (None, False),
])
def test_work_menu_requires_current_native_negotiation(monkeypatch, capabilities, expected):
    from orchestrator.chrome_availability import projection_native_chrome_availability
    monkeypatch.setattr(flags, "is_enabled", lambda _: True)
    availability = projection_native_chrome_availability({"_client_capabilities": capabilities})
    assert availability["work_enabled"] is expected
    assert availability["export_enabled"] and availability["share_enabled"]


def test_work_menu_flag_refusal_is_independent_and_legacy_rest_hides_work(monkeypatch):
    from orchestrator.chrome_availability import projection_native_chrome_availability
    monkeypatch.setattr(flags, "is_enabled", lambda _: True)
    assert projection_native_chrome_availability(None)["work_enabled"] is False
    app = FastAPI()
    app.include_router(chrome_router)
    app.dependency_overrides[get_current_user_payload] = lambda: {"realm_access": {"roles": ["user"]}}
    assert all(item["key"] != "work" for item in TestClient(app).get("/api/chrome/menu").json()["topbar"])
    def resolve(key):
        if key == "persistent_agents":
            raise KeyError("unavailable")
        return True
    monkeypatch.setattr(flags, "is_enabled", resolve)
    availability = projection_native_chrome_availability({"_client_capabilities": ["work_read_v1"]})
    assert availability["work_enabled"] is False and availability["export_enabled"] is True
