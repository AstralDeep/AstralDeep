"""Tests for DelegationService (orchestrator/delegation.py): RFC 8693 token exchange
against a signing token endpoint, scope filtering and extraction, and the guard against
sending Keycloak an empty scope.
"""

import os
import sys
import pytest
from jose import jwt as jose_jwt

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.delegation import DelegationService


TOOLS = ["get_system_status", "modify_data", "search_wikipedia"]


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


def _echoing_session(identity, agent_id):
    class _EchoingSession(_FakeSession):
        def post(self, url, data=None):
            _FakeSession.captured = (url, data)
            subject = jose_jwt.get_unverified_claims(data["subject_token"])
            token = identity.issuer.token(
                subject["sub"], aud=data["audience"], scope=data["scope"],
                act={"sub": "agent:" + agent_id}, delegation=True,
            )
            return _FakeResp(200, {"access_token": token, "token_type": "Bearer",
                                   "expires_in": 300, "scope": data["scope"]})

    return _EchoingSession


class TestSignedExchange:
    async def _exchange(self, identity, monkeypatch, scopes, tools=TOOLS):
        monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "secret")
        monkeypatch.setattr(
            "orchestrator.delegation.aiohttp.ClientSession",
            _echoing_session(identity, "general-1"),
        )
        user_token = identity.token("user-1")
        result = await DelegationService().exchange_token_for_agent(
            user_token, "general-1", allowed_tools=tools, enabled_scopes=scopes,
        )
        return user_token, result

    @pytest.mark.asyncio
    async def test_exchange_sends_the_callers_token_and_requested_audience(
        self, server_identity, monkeypatch
    ):
        user_token, result = await self._exchange(
            server_identity, monkeypatch, ["tools:read", "tools:write"]
        )
        url, form = _FakeSession.captured
        assert url == f"{server_identity.issuer.authority}/protocol/openid-connect/token"
        assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
        assert form["subject_token"] == user_token
        assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
        assert form["requested_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
        assert form["audience"] == "astral-agent-service"
        assert form["scope"] == "tools:read tools:write"
        assert result["agent_id"] == "general-1"
        assert result["scope"] == "tools:read tools:write"

    @pytest.mark.asyncio
    async def test_narrowed_scopes_are_all_the_exchange_requests(
        self, server_identity, monkeypatch
    ):
        _, result = await self._exchange(
            server_identity, monkeypatch, ["tools:read"], tools=["get_system_status"]
        )
        _, form = _FakeSession.captured
        assert form["scope"] == "tools:read"
        assert "tools:write" not in form["scope"]
        claims = jose_jwt.decode(
            result["access_token"], server_identity.issuer.jwks,
            algorithms=["RS256"], audience="astral-agent-service",
            issuer=server_identity.issuer.authority,
        )
        assert claims["sub"] == "user-1"
        assert claims["scope"] == "tools:read"

    @pytest.mark.asyncio
    async def test_unconfigured_exchange_is_refused_without_a_fallback_token(
        self, server_identity, monkeypatch
    ):
        monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
        result = await DelegationService().exchange_token_for_agent(
            server_identity.token("user-1"), "general-1",
            allowed_tools=TOOLS, enabled_scopes=["tools:read"],
        )
        assert result["error"] == "server_error"
        assert "access_token" not in result


# Empty scope param 400s at Keycloak like a realm misconfig
class TestExchangeGuards:
    @pytest.fixture(autouse=True)
    def _identity(self, server_identity):
        self.identity = server_identity

    def _real_service(self, monkeypatch):
        monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://kc.example/realms/T")
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
            self.identity.token("user-1"), "web-research-1",
            allowed_tools=["web_search"], enabled_scopes=[])
        assert result["error"] == "no_enabled_scopes"

    @pytest.mark.asyncio
    async def test_nonempty_scope_reaches_keycloak(self, monkeypatch):
        service = self._real_service(monkeypatch)
        _FakeSession.captured = None
        monkeypatch.setattr(
            "orchestrator.delegation.aiohttp.ClientSession", _FakeSession)
        result = await service.exchange_token_for_agent(
            self.identity.token("user-1"), "web-research-1",
            allowed_tools=["web_search", "fetch_page"],
            enabled_scopes=["tools:read", "tools:search"])
        assert result["access_token"] == "exchanged-token"
        url, form = _FakeSession.captured
        assert url.endswith("/protocol/openid-connect/token")
        assert form["scope"] == "tools:read tools:search"
        assert form["audience"] == "astral-agent-service"
