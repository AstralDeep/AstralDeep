"""Tests for the first-party user-token denylist (orchestrator/auth.py, mcp_authz.py):
delegated/agent-service/MCP tokens are refused by actor/audience/delegation claims
while ordinary Keycloak user tokens (aud=account) keep working.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared import auth_clients  # noqa: E402

AUTHORITY = "https://idp.example/realms/astral"


class _Req:
    method = "GET"
    query_params: dict = {}


def _creds(token: str = "a.b.c") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture
def jwks_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", AUTHORITY)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    monkeypatch.delenv("AGENT_SERVICE_CLIENT_ID", raising=False)

    async def _jwks(url, token=None):
        return {"keys": [{"kid": "k"}]}

    monkeypatch.setattr("shared.jwks_cache.get_jwks", _jwks)
    yield monkeypatch


def _decode_as(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr("jose.jwt.decode", lambda token, key, **kw: payload)


def _validate(payload: dict, monkeypatch):
    from orchestrator.auth import get_current_user_payload
    _decode_as(monkeypatch, payload)
    return asyncio.run(get_current_user_payload(_Req(), _creds()))


def test_predicate_accepts_plain_user_claims():
    ok, reason = auth_clients.is_first_party_user_claims(
        {"sub": "u1", "azp": "astral-frontend"})
    assert ok and reason == ""


def test_predicate_accepts_account_audience():
    ok, _ = auth_clients.is_first_party_user_claims(
        {"sub": "u1", "aud": "account", "azp": "astral-frontend"})
    assert ok
    ok, _ = auth_clients.is_first_party_user_claims(
        {"sub": "u1", "aud": ["account", "broker"], "azp": "astral-frontend"})
    assert ok


def test_predicate_rejects_actor_claim():
    ok, reason = auth_clients.is_first_party_user_claims(
        {"sub": "u1", "act": {"sub": "agent:web-research-1"}})
    assert not ok and reason == "delegated_actor_claim"


def test_predicate_rejects_delegation_flag():
    ok, reason = auth_clients.is_first_party_user_claims(
        {"sub": "u1", "delegation": True})
    assert not ok and reason == "delegation_flag"


def test_predicate_accepts_the_live_realm_user_token_shape():
    live = {
        "sub": "58e0d4ff-f006-4fbe-aa13-109c6d51c99d",
        "azp": "astral-frontend",
        "aud": ["astral-agent-service", "realm-management", "account"],
        "realm_access": {"roles": ["default-roles-astral", "offline_access"]},
    }
    ok, reason = auth_clients.is_first_party_user_claims(live)
    assert ok, f"live realm user token must be accepted, got {reason!r}"


def test_predicate_rejects_mcp_audience():
    ok, reason = auth_clients.is_first_party_user_claims(
        {"sub": "u1", "aud": ["account", "astral-mcp"]})
    assert not ok and reason == "mcp_audience"


def test_predicate_rejects_non_dict():
    ok, reason = auth_clients.is_first_party_user_claims(None)  # type: ignore[arg-type]
    assert not ok and reason == "malformed_claims"


def test_mcp_audience_literal_matches_mcp_authz():
    from orchestrator.mcp_authz import MCP_AUDIENCE
    assert auth_clients.MCP_AUDIENCE == MCP_AUDIENCE


def test_rest_accepts_first_party_token(jwks_env):
    payload = {"sub": "u1", "azp": "astral-frontend", "aud": "account",
               "iss": AUTHORITY}
    assert _validate(payload, jwks_env)["sub"] == "u1"


def test_rest_accepts_token_without_iss(jwks_env):
    assert _validate({"sub": "u1", "azp": "astral-frontend"}, jwks_env)["sub"] == "u1"


@pytest.mark.parametrize("payload", [
    {"sub": "u1", "azp": "astral-frontend", "act": {"sub": "agent:summarizer-1"}},
    {"sub": "u1", "azp": "astral-frontend", "delegation": True},
    {"sub": "u1", "azp": "astral-frontend", "aud": ["account", "astral-mcp"]},
])
def test_rest_rejects_non_first_party_tokens(jwks_env, payload):
    with pytest.raises(HTTPException) as exc:
        _validate(payload, jwks_env)
    assert exc.value.status_code == 401


def test_rest_rejects_foreign_issuer(jwks_env):
    payload = {"sub": "u1", "azp": "astral-frontend",
               "iss": "https://evil.example/realms/other"}
    with pytest.raises(HTTPException) as exc:
        _validate(payload, jwks_env)
    assert exc.value.status_code == 401


def test_rest_tolerates_trailing_slash_issuer(jwks_env):
    payload = {"sub": "u1", "azp": "astral-frontend", "iss": AUTHORITY + "/"}
    assert _validate(payload, jwks_env)["sub"] == "u1"
