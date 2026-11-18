"""Tests for the orchestrator's inbound A2A server (a2a_orchestrator_executor.py,
a2a_security.py): bearer gating before dispatch, per-caller task scoping, public-card
projection, and Keycloak token validation.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.node_identity import canonical_owner_id  # noqa: E402
from shared.protocol import MCPResponse  # noqa: E402

ALICE = "alice-sub"
ALICE_OWNER = canonical_owner_id("https://iam.example/realms/astral", ALICE)
BOB = "bob-sub"


_identity = None
_AUDIENCE = ["astral-agent-service", "account"]


def _auth(sub: str, **extra) -> dict:
    return _identity.headers(sub, aud=_AUDIENCE, **extra)


def _user_claims(sub: str, **extra) -> dict:
    claims = {
        "sub": sub,
        "preferred_username": sub,
        "iss": "https://iam.example/realms/astral",
        "azp": "astral-frontend",
        "aud": _AUDIENCE,
        "realm_access": {"roles": ["user"]},
    }
    claims.update(extra)
    return claims


def _skill(skill_id: str, scope: str = "tools:read"):
    return SimpleNamespace(
        id=skill_id,
        name=skill_id,
        description=f"{skill_id} description",
        scope=scope,
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        metadata={},
    )


def _card(agent_id: str, name: str, skills):
    return SimpleNamespace(
        agent_id=agent_id, name=name, description=f"{name} agent", skills=skills
    )


class _Permissions:
    def __init__(self, safe_ids):
        self._safe = set(safe_ids)

    def list_disabled_agents(self, user_id):
        return ()

    def is_tool_allowed(self, user_id, agent_id, tool_name):
        if user_id == _identity.owner(BOB) and tool_name == "get_weather":
            return False
        return True

    def _is_safe_agent(self, agent_id):
        return agent_id in self._safe


def _orchestrator():
    orch = SimpleNamespace()
    orch.agent_cards = {
        "weather-1": _card("weather-1", "Weather", [_skill("get_weather")]),
        "web-research-1": _card("web-research-1", "Web Research", [_skill("web_search", "tools:search")]),
        "draft-secret-9": _card("draft-secret-9", "Secret Draft", [_skill("exfiltrate", "tools:write")]),
    }
    orch.a2a_agent_owners = {}
    orch.agents = {}
    orch.local_agents = {"weather-1": object(), "web-research-1": object(), "draft-secret-9": object()}
    orch.security_flags = {}
    orch._is_draft_agent = lambda agent_id: agent_id == "draft-secret-9"
    orch.tool_permissions = _Permissions(safe_ids={"weather-1"})
    orch.execute_authorized_tool = AsyncMock(
        return_value=MCPResponse(request_id="r1", result={"temp": 21})
    )
    from shared.a2a_publication import A2APublicationRegistry
    from orchestrator.a2a_orchestrator_executor import public_catalog_agent_ids
    from orchestrator.a2a_publication_wiring import registered_audiences
    orch.a2a_publications = A2APublicationRegistry(
        catalog=lambda: public_catalog_agent_ids(orch),
        audiences=lambda agent_id: registered_audiences(orch, agent_id),
    )
    return orch


V1 = {"A2A-Version": "1.0"}


def _task(body: dict) -> dict:
    result = body["result"]
    return result.get("task", result)


def _result_parts(body: dict) -> list:
    task = _task(body)
    msg = task.get("status", {}).get("message") or task
    return msg.get("parts", [])


def _data_part(body: dict) -> dict:
    for part in _result_parts(body):
        if "data" in part:
            return part["data"]
    return {}


def _tool_names(body: dict) -> set:
    return {t["name"] for t in _data_part(body).get("tools", [])}


def test_anonymous_task_scope_is_empty_and_unshared():
    from a2a.auth.user import UnauthenticatedUser
    from a2a.server.context import ServerCallContext
    from orchestrator.a2a_orchestrator_executor import A2APrincipal, a2a_task_owner

    anon_a = a2a_task_owner(ServerCallContext(user=UnauthenticatedUser()))
    anon_b = a2a_task_owner(ServerCallContext(user=UnauthenticatedUser()))
    assert anon_a != anon_b and anon_a != "" and anon_b != ""
    principal = A2APrincipal({"sub": ALICE}, "tok")
    assert a2a_task_owner(ServerCallContext(user=principal)) == f"sub:{ALICE}"
    assert a2a_task_owner(ServerCallContext(user=principal)) == a2a_task_owner(
        ServerCallContext(user=A2APrincipal({"sub": ALICE}, "other-tok"))
    )


@pytest.fixture
def keycloak_env(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://iam.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop")
    monkeypatch.setenv("AGENT_SERVICE_CLIENT_ID", "astral-agent-service")


def _validator(monkeypatch, claims, *, strict=True):
    from shared import a2a_security

    validator = a2a_security.A2ASecurityValidator(require_first_party_user=strict)
    validator._get_jwks = AsyncMock(return_value={"keys": []})
    monkeypatch.setattr("jose.jwt.decode", lambda token, key, **kw: dict(claims))
    return validator


def test_flag_off_mounts_nothing(monkeypatch):
    from shared.feature_flags import FeatureFlags

    monkeypatch.delenv("FF_A2A_SERVER", raising=False)
    assert FeatureFlags().is_enabled("a2a_server") is False
    app = FastAPI()
    paths = {getattr(r, "path", None) for r in app.router.routes}
    assert "/a2a" not in paths and "/a2a/.well-known/agent-card.json" not in paths
