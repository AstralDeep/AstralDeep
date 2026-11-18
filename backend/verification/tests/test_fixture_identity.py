"""Tests for the synthetic fixture IAM
(backend/verification/drivers/fixture_identity.py, backend/orchestrator/auth.py):
signature verification, trust scoped to one namespaced run, and refusal of foreign or
overlapping realms.
"""

from __future__ import annotations


import pytest

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
