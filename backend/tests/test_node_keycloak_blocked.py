"""A personal node never asks an identity server: a session token from an accepted issuer is refused whether the issuer is reachable, blocked or already cached, while the server profile keeps verifying on its last good keys through the grace window.
It drives shared/token_verify.py, shared/jwks_cache.py and Orchestrator.validate_token, beside the per-door refusals in test_node_bearer_refusal.py."""

from __future__ import annotations

import types

import pytest

from shared import jwks_cache
from shared import node_identity as ni
from shared.token_verify import TokenRefused, verify_idp_token
from tests.helpers.idp_tokens import AUTHORITY, CLIENT_ID, FakeIssuer

JWKS_URL = f"{AUTHORITY}/protocol/openid-connect/certs"


@pytest.fixture
def issuer(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", AUTHORITY)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", CLIENT_ID)
    for name in (ni.OWNER_ENV, ni.ISSUERS_ENV, "KEYCLOAK_ALLOWED_AZP", "JWKS_STALE_GRACE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    ni.reset_cache()
    jwks_cache.clear()
    state = types.SimpleNamespace(now=1_000_000.0, up=True, fetches=0, key=FakeIssuer())
    monkeypatch.setattr(jwks_cache, "time", types.SimpleNamespace(time=lambda: state.now))

    async def fetch(url):
        state.fetches += 1
        if not state.up:
            raise OSError("issuer blocked")
        jwks_cache._cache[url] = {"jwks": state.key.jwks, "fetched_at": state.now}
        return state.key.jwks

    monkeypatch.setattr(jwks_cache, "_fetch", fetch)
    yield state
    jwks_cache.clear()
    ni.reset_cache()


@pytest.mark.parametrize("reachable,cached", [(True, False), (False, False), (False, True)])
async def test_a_session_token_is_refused_whether_the_issuer_is_up_down_or_cached(issuer, reachable, cached):
    from orchestrator.orchestrator import Orchestrator

    issuer.up = reachable
    if cached:
        jwks_cache._cache[JWKS_URL] = {"jwks": issuer.key.jwks, "fetched_at": issuer.now}
    token = issuer.key.token("owner-1")
    with pytest.raises(TokenRefused) as refused:
        await verify_idp_token(token)
    assert refused.value.reason == "node_bearer_refused"
    assert await Orchestrator.validate_token(object(), token) is None
    assert issuer.fetches == 0 and ni.bound_owner() is None


async def test_the_server_profile_keeps_verifying_on_its_last_good_keys(issuer, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV)
    token = issuer.key.token("owner-1")
    owner = ni.canonical_owner_id(AUTHORITY, "owner-1")
    assert (await verify_idp_token(token))["sub"] == owner
    issuer.up = False
    issuer.now += jwks_cache._TTL_SECONDS + 60
    assert (await verify_idp_token(token))["sub"] == owner
    issuer.now += jwks_cache._DEFAULT_GRACE_SECONDS
    with pytest.raises(OSError):
        await verify_idp_token(token)


async def test_a_new_server_sign_in_needs_the_issuer(issuer, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV)
    issuer.up = False
    with pytest.raises(OSError):
        await verify_idp_token(issuer.key.token("owner-1"))
