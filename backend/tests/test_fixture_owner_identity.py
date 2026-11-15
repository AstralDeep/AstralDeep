"""Verifies that synthetic JWT subjects produce the same canonical owners used by registered-human test stores, with issuer mismatches refused."""

import pytest

from shared.token_verify import verify_idp_token
from tests.helpers.fixture_owner import AUTHORITY, fixture_owner, fixture_subject
from verification.drivers.fixture_identity import FixtureIdentity


@pytest.mark.asyncio
async def test_signed_fixture_subject_resolves_to_the_stored_owner():
    identity = FixtureIdentity("__verif__owner_helper", authority=AUTHORITY)
    try:
        subject = "__verif__owner_helper_owner"
        _, token = identity.claims_and_token({"sub": subject, "realm_access": {"roles": ["user"]}})
        owner = fixture_owner(subject)
        assert (await verify_idp_token(token))["sub"] == owner
        assert fixture_subject(owner) == subject
    finally:
        identity.close()


def test_an_unrelated_issuer_cannot_be_substituted_for_the_fixture_owner():
    with pytest.raises(ValueError, match="selected synthetic issuer"):
        fixture_subject(fixture_owner("owner", authority="https://host.invalid/realms/real"))


def test_explicit_fixture_identity_cannot_impersonate_a_real_host_authority():
    with pytest.raises(ValueError, match="verification.invalid"):
        FixtureIdentity("__verif__owner_helper", authority="https://host.invalid/realms/real")


def test_registered_fixture_never_replaces_an_existing_host_issuer(monkeypatch):
    from tests.helpers.registered_human import registered_human_turn

    authority = "https://host.invalid/realms/real"
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", authority)
    with pytest.raises(ValueError, match="existing host issuer"):
        with registered_human_turn(object(), object(), "hello", "chat", user_id=fixture_owner("owner")):
            pytest.fail("host issuer was replaced")
    import os

    assert os.environ["KEYCLOAK_AUTHORITY"] == authority
