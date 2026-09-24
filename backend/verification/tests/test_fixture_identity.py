"""Tests for the synthetic fixture IAM
(backend/verification/drivers/fixture_identity.py, backend/orchestrator/auth.py):
signature verification, trust scoped to one namespaced run, and refusal of foreign or
overlapping realms.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from jose import jwt

from verification.drivers.fixture_identity import FixtureIdentity


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://prior.invalid")
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    value = FixtureIdentity("__verif__signed")
    try:
        yield value
    finally:
        value.close()


def test_ordinary_auth_accepts_only_untampered_signature(identity):
    from orchestrator.auth import verify_production_token, verify_user

    claims, token = identity.claims_and_token({
        "sub": "__verif__signed_owner", "realm_access": {"roles": ["user"]},
    })

    async def exercise():
        assert await verify_user(await verify_production_token(token)) == claims
        tampered = jwt.encode(claims | {"sub": "foreign"}, "wrong-key", algorithm="HS256")
        with pytest.raises(HTTPException) as error:
            await verify_production_token(tampered)
        assert error.value.status_code == 401

    asyncio.run(exercise())


def test_close_removes_only_own_trust_and_restores_settings(identity):
    import os

    from shared import jwks_cache

    unrelated = {"jwks": {"keys": []}, "fetched_at": 1}
    jwks_cache._cache["https://unrelated.invalid"] = unrelated
    try:
        identity.close()
        identity.close()
        assert identity.url not in jwks_cache._cache
        assert jwks_cache._cache["https://unrelated.invalid"] is unrelated
        assert os.environ["KEYCLOAK_AUTHORITY"] == "https://prior.invalid"
        assert "KEYCLOAK_ALLOWED_AZP" not in os.environ
        assert identity.private_key == b""
        assert FixtureIdentity._active is None
        with pytest.raises(ValueError, match="outside"):
            identity.claims_and_token({"sub": "__verif__signed_owner"})
    finally:
        jwks_cache._cache.pop("https://unrelated.invalid", None)


@pytest.mark.parametrize("claims", [{}, {"sub": "foreign"}, {"sub": "__verif__signedOTHER"}])
def test_foreign_principals_refused(identity, claims):
    with pytest.raises(ValueError, match="outside"):
        identity.claims_and_token(claims)


def test_overlapping_realm_owners_refused(identity):
    with pytest.raises(RuntimeError, match="active owner"):
        FixtureIdentity("__verif__another")


@pytest.mark.parametrize("environment", [None, "production", "staging"])
def test_no_fixture_iam_outside_explicit_development(monkeypatch, environment):
    if environment is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", environment)
    with pytest.raises(ValueError, match="development"):
        FixtureIdentity("__verif__signed")


def test_run_must_be_namespaced(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    with pytest.raises(ValueError, match="namespaced"):
        FixtureIdentity("ordinary-owner")
