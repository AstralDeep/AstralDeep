"""Tests for DelegationService (orchestrator/delegation.py): RFC 8693 token exchange,
mock token creation, scope filtering and extraction, and the guard against sending
Keycloak an empty scope.
"""

import os
import sys
import json
import base64
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

os.environ["USE_MOCK_AUTH"] = "true"

from orchestrator.delegation import DelegationService


@pytest.fixture
def service():
    return DelegationService()


TOOLS = ["get_system_status", "modify_data", "search_wikipedia"]


class TestMockDelegationToken:
    def test_creates_token(self, service):
        result = service._create_mock_delegation_token(
            agent_id="general-1",
            allowed_tools=TOOLS,
            user_id="test-user"
        )
        assert "access_token" in result
        assert result["token_type"] == "Bearer"
        assert result["expires_in"] == 300
        assert result["agent_id"] == "general-1"

    def test_token_has_act_claim(self, service):
        result = service._create_mock_delegation_token(
            agent_id="general-1",
            allowed_tools=TOOLS,
            user_id="test-user"
        )
        token = result["access_token"]
        parts = token.split(".")
        assert len(parts) == 3
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))

        assert payload["sub"] == "test-user"
        assert "act" in payload
        assert payload["act"]["sub"] == "agent:general-1"

    def test_token_scope_contains_tools(self, service):
        result = service._create_mock_delegation_token(
            agent_id="general-1",
            allowed_tools=TOOLS,
            user_id="test-user"
        )
        scope = result["scope"]
        for tool in TOOLS:
            assert f"tool:{tool}" in scope

    def test_token_scope_limited(self, service):
        result = service._create_mock_delegation_token(
            agent_id="general-1",
            allowed_tools=["get_system_status"],
            user_id="test-user"
        )
        assert "tool:get_system_status" in result["scope"]
        assert "tool:modify_data" not in result["scope"]

    def test_delegation_flag(self, service):
        result = service._create_mock_delegation_token(
            agent_id="general-1",
            allowed_tools=TOOLS,
            user_id="test-user"
        )
        token = result["access_token"]
        parts = token.split(".")
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        assert payload.get("delegation") is True


class TestDelegationInfoExtraction:
    def test_extract_from_delegation_token(self):
        payload = {
            "sub": "user-123",
            "act": {"sub": "agent:general-1"},
            "scope": "tool:get_system_status tool:modify_data"
        }
        info = DelegationService.extract_delegation_info(payload)
        assert info is not None
        assert info["user_id"] == "user-123"
        assert info["actor"] == "agent:general-1"
        assert "tool:get_system_status" in info["scopes"]
        assert "tool:modify_data" in info["scopes"]
        assert info["is_delegation"] is True

    def test_extract_from_regular_token(self):
        payload = {
            "sub": "user-123",
            "scope": "openid profile"
        }
        info = DelegationService.extract_delegation_info(payload)
        assert info is None


class _FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    captured = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, data=None):
        _FakeSession.captured = (url, data)
        return _FakeResp(200, {
            "access_token": "exchanged-token",
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": data["scope"],
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        })


# Empty scope param 400s at Keycloak like a realm misconfig
class TestExchangeGuards:
    def _real_service(self, monkeypatch):
        monkeypatch.setenv("USE_MOCK_AUTH", "false")
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://kc.example/realms/T")
        monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
        monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "secret")
        return DelegationService()

    @pytest.mark.asyncio
    async def test_empty_scope_refused_locally(self, monkeypatch):
        service = self._real_service(monkeypatch)

        def _no_http(*args, **kwargs):
            raise AssertionError(
                "token endpoint must not be called with an empty scope")

        monkeypatch.setattr(
            "orchestrator.delegation.aiohttp.ClientSession", _no_http)
        result = await service.exchange_token_for_agent(
            "user-token", "web-research-1",
            allowed_tools=["web_search"], enabled_scopes=[])
        assert result["error"] == "no_enabled_scopes"

    @pytest.mark.asyncio
    async def test_empty_scope_refused_in_mock_mode_too(self, monkeypatch):
        monkeypatch.setenv("USE_MOCK_AUTH", "true")
        service = DelegationService()
        result = await service.exchange_token_for_agent(
            "user-token", "web-research-1", allowed_tools=["web_search"],
            user_id="u1", enabled_scopes=[])
        assert result["error"] == "no_enabled_scopes"
        assert "access_token" not in result

    @pytest.mark.asyncio
    async def test_nonempty_scope_reaches_keycloak(self, monkeypatch):
        service = self._real_service(monkeypatch)
        _FakeSession.captured = None
        monkeypatch.setattr(
            "orchestrator.delegation.aiohttp.ClientSession", _FakeSession)
        result = await service.exchange_token_for_agent(
            "user-token", "web-research-1",
            allowed_tools=["web_search", "fetch_page"],
            enabled_scopes=["tools:read", "tools:search"])
        assert result["access_token"] == "exchanged-token"
        url, form = _FakeSession.captured
        assert url.endswith("/protocol/openid-connect/token")
        assert form["scope"] == "tools:read tools:search"
        assert form["audience"] == "astral-agent-service"


class TestToolScopeCheck:
    def test_tool_in_scope(self):
        scopes = ["tool:get_system_status", "tool:modify_data"]
        assert DelegationService.is_tool_in_scope("get_system_status", scopes) is True
        assert DelegationService.is_tool_in_scope("modify_data", scopes) is True

    def test_tool_not_in_scope(self):
        scopes = ["tool:get_system_status"]
        assert DelegationService.is_tool_in_scope("modify_data", scopes) is False

    def test_no_tool_scopes_allows_all(self):
        scopes = ["openid", "profile"]
        assert DelegationService.is_tool_in_scope("modify_data", scopes) is True
        assert DelegationService.is_tool_in_scope("anything", scopes) is True

    def test_empty_scopes_allows_all(self):
        assert DelegationService.is_tool_in_scope("modify_data", []) is True
