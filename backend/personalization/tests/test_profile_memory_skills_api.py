"""030 — deferred REST contract tests for profile/memory/skills + personalize steps.

Closes 025 T013 (profile), T033 (memory), T024 (skills), and T014 (personalize
steps) — the formal pytest-TestClient files that 025 deferred (FR-015).
"""
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class _CleanGate:
    def contains_phi(self, value):
        return False


class _FakeTP:
    """Minimal ToolPermissionManager for skills + personalize-steps tests."""
    def __init__(self, authorized=True):
        self._authorized = authorized
        self._tool_scope_map = {"web-research-1": {"web_search": "tools:search"}}
        self.enabled = {}

    def get_tool_scope_map(self, agent_id):
        return dict(self._tool_scope_map.get(agent_id, {}))

    def get_tool_scope(self, agent_id, tool_name):
        return self._tool_scope_map.get(agent_id, {}).get(tool_name, "tools:read")

    def is_scope_enabled(self, user_id, agent_id, scope):
        return self._authorized

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        return self.enabled.get((agent_id, tool_name), False)

    def set_skill_enabled(self, user_id, agent_id, tool_name, enabled):
        self.enabled[(agent_id, tool_name)] = enabled


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from personalization.service import PersonalizationService
    from orchestrator.auth import get_current_user_payload, require_user_id
    from personalization import api as papi
    from onboarding import api as oapi
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    # Avoid the heavy Presidio gate in tests — values here are clean.
    monkeypatch.setattr(papi, "get_phi_gate", lambda: _CleanGate())

    user_id = f"pytest-pms-{uuid.uuid4().hex[:8]}"
    with isolated_plane_runtime("personalization_api") as runtime:
        svc = PersonalizationService(
            None,
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        )
        tp = _FakeTP(authorized=True)

        import types

        orch = types.SimpleNamespace(
            personalization_service=svc,
            tool_permissions=tp,
        )

        app = FastAPI()
        app.state.orchestrator = orch
        app.include_router(papi.personalization_router)
        app.include_router(papi.memory_router)
        app.include_router(papi.skills_router)
        app.include_router(oapi.onboarding_user_router)

        payload = {
            "sub": user_id,
            "preferred_username": user_id,
            "realm_access": {"roles": ["user"]},
        }
        app.dependency_overrides[require_user_id] = lambda: user_id
        app.dependency_overrides[get_current_user_payload] = lambda: payload

        with TestClient(app) as test_client:
            yield test_client, svc, user_id, tp


def test_profile_get_put_roundtrip(client):
    tc, svc, user_id, tp = client
    # default empty profile
    r = tc.get("/api/personalization/profile")
    assert r.status_code == 200
    # PUT persists
    r = tc.put("/api/personalization/profile",
               json={"profession": "Researcher", "goals": ["grants"]})
    assert r.status_code == 200, r.text
    assert r.json()["profession"] == "Researcher"
    # GET reflects it
    assert tc.get("/api/personalization/profile").json()["goals"] == ["grants"]


def test_memory_list_and_delete(client):
    tc, svc, user_id, tp = client
    item = svc.repo.create_memory(user_id, "preference", "concise", source="explicit")
    r = tc.get("/api/memory")
    assert r.status_code == 200
    assert any(m["id"] == item["id"] for m in r.json()["items"])
    r = tc.delete(f"/api/memory/{item['id']}")
    assert r.status_code == 204
    assert tc.delete(f"/api/memory/{item['id']}").status_code == 404


def test_skills_catalog_and_scope_gating(client):
    tc, svc, user_id, tp = client
    r = tc.get("/api/skills")
    assert r.status_code == 200
    assert any(s["agent_id"] == "web-research-1" for s in r.json()["skills"])
    # authorized enable succeeds
    r = tc.put("/api/skills", json={"agent_id": "web-research-1",
                                    "tool_name": "web_search", "enabled": True})
    assert r.status_code == 200
    # FR-011: unauthorized scope is refused with 403
    tp._authorized = False
    r = tc.put("/api/skills", json={"agent_id": "web-research-1",
                                    "tool_name": "web_search", "enabled": True})
    assert r.status_code == 403


def test_personalize_step_returns_param_picker(client):
    tc, svc, user_id, tp = client
    r = tc.get("/api/onboarding/personalize/profession")
    assert r.status_code == 200
    body = r.json()
    assert "_ui_components" in body and body["_ui_components"]
