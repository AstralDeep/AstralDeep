"""Tests for the fixture owner namespace (backend/verification/drivers/fixture_identity.py):
one active namespaced run and development-only construction.
"""

from __future__ import annotations


import pytest

from verification.drivers.fixture_identity import FixtureIdentity


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    value = FixtureIdentity("__verif__signed")
    try:
        yield value
    finally:
        value.close()


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
