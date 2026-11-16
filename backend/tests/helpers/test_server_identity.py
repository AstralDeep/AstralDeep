"""Proves the server_identity helper signs real tokens that the production verifier accepts, refuses foreign issuers, derives canonical owner ids and restores the JWKS cache after each use."""

from __future__ import annotations

import pytest
from jose import jwt as jose_jwt

from shared import jwks_cache
from shared import node_identity as ni
from shared.token_verify import TokenRefused, verify_idp_token
from tests.helpers.idp_tokens import FakeIssuer
from tests.helpers.server_identity import ServerIdentity


async def test_token_verifies_through_the_real_jwks_path(server_identity):
    claims = await verify_idp_token(server_identity.token("alice"))
    assert claims["sub"] == server_identity.owner("alice")
    assert claims["astral_idp_sub"] == "alice"


async def test_verification_goes_through_the_jwks_cache(server_identity):
    await verify_idp_token(server_identity.token("alice"))
    assert server_identity.jwks_url in jwks_cache._cache


async def test_token_from_an_issuer_outside_keycloak_authority_is_refused(server_identity):
    rogue = FakeIssuer("https://rogue.example/realms/astral")
    with pytest.raises(Exception) as refused:
        await verify_idp_token(rogue.token("alice"))
    assert isinstance(refused.value, (TokenRefused, jose_jwt.JWTError))


async def test_token_forged_with_the_wrong_key_is_refused(server_identity):
    forged = FakeIssuer(server_identity.issuer.authority).token("alice")
    with pytest.raises(jose_jwt.JWTError):
        await verify_idp_token(forged)


def test_owner_matches_the_canonical_owner_id(server_identity):
    assert server_identity.owner("alice") == ni.canonical_owner_id(server_identity.issuer.authority, "alice")
    assert server_identity.owner("alice") != server_identity.owner("bob")
    assert server_identity.owner("alice").startswith("oidc.")


async def test_roles_default_to_user_and_admin_is_opt_in(server_identity):
    plain = await verify_idp_token(server_identity.token("alice"))
    assert plain["realm_access"]["roles"] == ["user"]
    admin = await verify_idp_token(server_identity.token("root", roles=("admin", "user")))
    assert admin["realm_access"]["roles"] == ["admin", "user"]
    assert admin["resource_access"]["astral-frontend"]["roles"] == ["admin", "user"]


async def test_extra_claims_override_the_defaults(server_identity):
    claims = await verify_idp_token(server_identity.token("alice", scope="tools:read", aud="account"))
    assert claims["scope"] == "tools:read"


def test_headers_carry_a_bearer_token_for_the_subject(server_identity):
    scheme, _, token = server_identity.headers("alice")["Authorization"].partition(" ")
    assert scheme == "Bearer"
    assert jose_jwt.get_unverified_claims(token)["sub"] == "alice"


async def test_an_unreachable_issuer_leaves_no_keys_to_verify_with(server_identity):
    server_identity.take_issuer_offline()
    with pytest.raises(Exception):
        await verify_idp_token(server_identity.token("alice"))


def test_installing_restores_the_jwks_cache_and_environment(monkeypatch):
    jwks_cache._cache["sentinel"] = {"jwks": {"keys": []}, "fetched_at": 1.0}
    before_fetch = jwks_cache._fetch
    before = dict(jwks_cache._cache)
    with monkeypatch.context() as scoped:
        ServerIdentity.install(scoped)
        assert jwks_cache._fetch is not before_fetch
        assert "sentinel" not in jwks_cache._cache
    try:
        assert jwks_cache._fetch is before_fetch
        assert jwks_cache._cache == before
    finally:
        jwks_cache._cache.pop("sentinel", None)
