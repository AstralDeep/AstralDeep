"""First-party user-token denylist (H2).

Delegated / agent-service / MCP tokens are minted by this orchestrator for a
NON-user purpose, yet they carry the human's ``sub``, the human's roles, the
realm ``iss`` and an allow-listed ``azp`` — so signature + azp + issuer alone
accept them as an interactive user session. These tests pin the denylist that
refuses them, and pin that it stays a DENYLIST: a real Keycloak
confidential-client token carries ``aud="account"`` and must keep working.
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
    """Real (non-mock) validator path with the JWKS fetch + decode stubbed."""
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


# ---------------------------------------------------------------------------
# (1) The shared predicate
# ---------------------------------------------------------------------------

def test_predicate_accepts_plain_user_claims():
    ok, reason = auth_clients.is_first_party_user_claims(
        {"sub": "u1", "azp": "astral-frontend"})
    assert ok and reason == ""


def test_predicate_accepts_account_audience():
    """Keycloak confidential clients set aud="account" — the whole reason the
    check must be a denylist and verify_aud must stay off."""
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
    """An ordinary interactive login carries the agent-service audience.

    Captured verbatim from a real session on the deployment realm: the
    delegation setup grants ``astral-agent-service`` to the FRONTEND client by
    protocol mapper, so an interactive token carries it too. Denylisting that
    audience refuses every real user — this pins the regression.
    """
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
    """shared/ cannot import orchestrator/, so the literal is duplicated —
    pin the two together."""
    from orchestrator.mcp_authz import MCP_AUDIENCE
    assert auth_clients.MCP_AUDIENCE == MCP_AUDIENCE


# ---------------------------------------------------------------------------
# (2) The REST dependency (JWKS branch)
# ---------------------------------------------------------------------------

def test_rest_accepts_first_party_token(jwks_env):
    payload = {"sub": "u1", "azp": "astral-frontend", "aud": "account",
               "iss": AUTHORITY}
    assert _validate(payload, jwks_env)["sub"] == "u1"


def test_rest_accepts_token_without_iss(jwks_env):
    """The issuer binding is present-and-mismatched only — a token with no iss
    claim keeps flowing (matches Orchestrator.validate_token)."""
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
