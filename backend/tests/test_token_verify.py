"""Covers shared/token_verify.py and the three verification paths that use it: accepted issuer, required azp, canonical owner ids and the personal-node owner binding."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from shared import node_identity as ni
from shared.token_verify import IDP_ISSUER_CLAIM, IDP_SUBJECT_CLAIM, TokenRefused, verify_idp_token
from tests.helpers.idp_tokens import AUTHORITY, CLIENT_ID, FakeIssuer, serve

SECOND = "https://other-idp.example/realms/astral"


@pytest.fixture
def idp(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", AUTHORITY)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    monkeypatch.delenv("AGENT_SERVICE_CLIENT_ID", raising=False)
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    ni.reset_cache()
    issuer = FakeIssuer()
    serve(monkeypatch, issuer)
    return issuer


async def refusal(token, **kwargs):
    try:
        await verify_idp_token(token, **kwargs)
    except TokenRefused as error:
        return error.reason
    except Exception:
        return "signature_or_expiry"
    pytest.fail("the token was accepted")


async def test_valid_token_maps_to_a_canonical_owner(idp):
    claims = await verify_idp_token(idp.token("user-1"))
    assert claims["sub"] == ni.canonical_owner_id(AUTHORITY, "user-1")
    assert claims[IDP_SUBJECT_CLAIM] == "user-1"
    assert claims[IDP_ISSUER_CLAIM] == AUTHORITY


async def test_token_without_an_issuer_is_refused(idp):
    assert await refusal(idp.token(iss=None)) == "missing_issuer"


async def test_token_from_an_unlisted_issuer_is_refused(idp, monkeypatch):
    rogue = FakeIssuer("https://rogue.example/realms/astral")
    serve(monkeypatch, idp, rogue)
    assert await refusal(rogue.token()) in {"signature_or_expiry", "issuer_not_accepted"}
    monkeypatch.setattr("jose.jwt.decode", lambda *a, **k: {"sub": "u", "iss": rogue.authority, "azp": CLIENT_ID})
    assert await refusal(idp.token()) == "issuer_not_accepted"


@pytest.mark.parametrize("azp,reason", [(None, "missing_azp"), ("", "missing_azp"), ("someone-else", "azp_not_accepted")])
async def test_missing_or_unlisted_azp_is_refused(idp, azp, reason):
    assert await refusal(idp.token(azp=azp)) == reason


async def test_extra_azp_is_honoured(idp):
    claims = await verify_idp_token(idp.token(azp="astral-agent-service"), extra_azps=["astral-agent-service"])
    assert claims["azp"] == "astral-agent-service"


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b.c"])
async def test_malformed_tokens_are_refused(idp, bad):
    assert await refusal(bad) in {"missing_token", "signature_or_expiry"}


async def test_expired_and_unsigned_by_the_issuer_tokens_are_refused(idp):
    assert await refusal(idp.token(exp=1)) == "signature_or_expiry"
    imposter = FakeIssuer()
    assert await refusal(imposter.token()) == "signature_or_expiry"


async def test_expiry_surfaces_as_the_original_jose_error(idp):
    from jose.exceptions import ExpiredSignatureError

    with pytest.raises(ExpiredSignatureError):
        await verify_idp_token(idp.token(exp=1))


async def test_token_without_expiry_is_refused(idp):
    assert await refusal(idp.token(exp=None)) == "signature_or_expiry"


async def test_unusable_subjects_are_refused(idp):
    assert await refusal(idp.token(sub="a:b")) == "subject_unusable"
    assert await refusal(idp.token(sub=None)) == "subject_unusable"


async def test_unconfigured_issuer_is_refused(idp, monkeypatch):
    monkeypatch.delenv("KEYCLOAK_AUTHORITY")
    assert await refusal(idp.token()) == "no_accepted_issuer"


async def test_same_subject_from_two_issuers_is_two_owners(idp, monkeypatch):
    second = FakeIssuer(SECOND)
    monkeypatch.setenv(ni.ISSUERS_ENV, SECOND)
    serve(monkeypatch, idp, second)
    first_owner = (await verify_idp_token(idp.token("shared-sub")))["sub"]
    second_owner = (await verify_idp_token(second.token("shared-sub")))["sub"]
    assert first_owner != second_owner


async def test_a_personal_node_bound_to_one_issuer_refuses_the_others_subject(idp, monkeypatch):
    second = FakeIssuer(SECOND)
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.ISSUERS_ENV, SECOND)
    monkeypatch.setenv(ni.OWNER_ENV, ni.canonical_owner_id(AUTHORITY, "owner-sub"))
    serve(monkeypatch, idp, second)
    assert (await verify_idp_token(idp.token("owner-sub")))["sub"] == ni.canonical_owner_id(AUTHORITY, "owner-sub")
    assert await refusal(second.token("owner-sub")) == "owner_mismatch"
    assert await refusal(idp.token("someone-else")) == "owner_mismatch"


async def test_first_verified_token_binds_a_personal_node(idp, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    await verify_idp_token(idp.token("first"))
    assert ni.bound_owner() == ni.canonical_owner_id(AUTHORITY, "first")
    assert await refusal(idp.token("second")) == "owner_mismatch"


async def test_an_unreadable_owner_binding_refuses_instead_of_failing_open(idp, monkeypatch, tmp_path):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    (tmp_path / "blocker").write_text("x")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "blocker" / "node"))
    assert await refusal(idp.token("first")) == "owner_binding_unavailable"


async def test_a_token_whose_keys_belong_to_another_issuer_is_refused(idp, monkeypatch):
    second = FakeIssuer(SECOND)
    monkeypatch.setenv(ni.ISSUERS_ENV, SECOND)
    serve(monkeypatch, idp, second)
    forged = idp.token(iss=SECOND)
    assert await refusal(forged) in {"signature_or_expiry", "issuer_not_accepted"}


async def test_rest_path_returns_the_canonical_owner(idp):
    from orchestrator.auth import verify_production_token

    claims = await verify_production_token(idp.token("rest-user"))
    assert claims["sub"] == ni.canonical_owner_id(AUTHORITY, "rest-user")


@pytest.mark.parametrize("overrides", [{"iss": None}, {"azp": None}, {"iss": "https://rogue.example"}])
async def test_rest_path_refuses_what_the_shared_check_refuses(idp, overrides):
    from orchestrator.auth import verify_production_token

    with pytest.raises(HTTPException) as error:
        await verify_production_token(idp.token(**overrides))
    assert error.value.status_code == 401


async def test_websocket_path_applies_the_same_rules(idp):
    from orchestrator.orchestrator import Orchestrator

    validate = Orchestrator.validate_token
    claims = await validate(object(), idp.token("ws-user"))
    assert claims["sub"] == ni.canonical_owner_id(AUTHORITY, "ws-user")
    for overrides in ({"iss": None}, {"azp": None}, {"azp": "other"}, {"iss": "https://rogue.example"}):
        assert await validate(object(), idp.token(**overrides)) is None


async def test_a2a_path_applies_the_same_rules(idp):
    from shared.a2a_security import A2ASecurityValidator

    validator = A2ASecurityValidator(require_first_party_user=True)
    claims = await validator.validate_token(idp.token("a2a-user"))
    assert claims["sub"] == ni.canonical_owner_id(AUTHORITY, "a2a-user")
    for overrides in ({"iss": None}, {"azp": None}, {"azp": "other"}):
        assert await validator.validate_token(idp.token(**overrides)) is None
    agent_side = A2ASecurityValidator()
    assert await agent_side.validate_token(idp.token(azp=None)) is None
    assert await agent_side.validate_token(idp.token(iss=None)) is None


async def test_a2a_agent_posture_accepts_the_agent_service_client(idp, monkeypatch):
    from shared.a2a_security import A2ASecurityValidator

    claims = await A2ASecurityValidator().validate_token(idp.token(azp="astral-agent-service"))
    assert claims is not None


async def test_mock_auth_keeps_the_bare_test_user(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    from orchestrator.orchestrator import Orchestrator
    from shared.a2a_security import A2ASecurityValidator

    assert (await Orchestrator.validate_token(object(), "dev-token"))["sub"] == "test_user"
    assert (await A2ASecurityValidator().validate_token("dev-token"))["sub"] == "test_user"


async def test_mcp_tokens_map_to_the_same_owner_and_respect_the_node_binding(idp, monkeypatch):
    from jose import jwt as jose_jwt

    from orchestrator import mcp_authz

    async def keys(url, *, token):
        return {"keys": []}

    monkeypatch.setattr(mcp_authz, "get_jwks", keys)
    monkeypatch.setattr(jose_jwt, "decode", lambda *a, **k: {"sub": "mcp-user", "iss": AUTHORITY})
    claims = await mcp_authz.decode_mcp_token("opaque")
    assert claims["sub"] == ni.canonical_owner_id(AUTHORITY, "mcp-user")
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.OWNER_ENV, ni.canonical_owner_id(AUTHORITY, "someone-else"))
    with pytest.raises(ni.OwnerMismatch):
        await mcp_authz.decode_mcp_token("opaque")


async def _verify_path(path, token):
    if path == "rest":
        from orchestrator.auth import verify_production_token

        try:
            return await verify_production_token(token)
        except HTTPException:
            return None
    if path == "websocket":
        from orchestrator.orchestrator import Orchestrator

        return await Orchestrator.validate_token(object(), token)
    from shared.a2a_security import A2ASecurityValidator

    return await A2ASecurityValidator(require_first_party_user=True).validate_token(token)


@pytest.mark.parametrize("path", ["rest", "websocket", "a2a"])
@pytest.mark.parametrize("audience", [None, "", [], {}, 42, True, ["account", 42], ["account", False], [""], "unrelated-service", "astral-node:another-node"])
async def test_all_entry_paths_refuse_missing_malformed_and_unrelated_audiences(idp, path, audience):
    assert await _verify_path(path, idp.token(aud=audience)) is None


@pytest.mark.parametrize("path", ["rest", "websocket", "a2a"])
@pytest.mark.parametrize("audience", ["account", CLIENT_ID, [CLIENT_ID], ["another-service", "account"]])
async def test_all_entry_paths_accept_their_explicit_audiences(idp, path, audience):
    assert await _verify_path(path, idp.token(aud=audience)) is not None


@pytest.mark.parametrize("path", ["rest", "websocket", "a2a"])
async def test_configured_native_client_audience_is_first_party(idp, monkeypatch, path):
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop")
    assert await _verify_path(path, idp.token(azp="astral-desktop", aud="astral-desktop")) is not None


async def test_agent_service_audience_does_not_open_human_entry_paths(idp):
    from shared.a2a_security import A2ASecurityValidator

    token = idp.token(azp="astral-agent-service", aud="astral-agent-service")
    assert await A2ASecurityValidator().validate_token(token) is not None
    for path in ("rest", "websocket", "a2a"):
        assert await _verify_path(path, token) is None


@pytest.mark.parametrize("audience", [None, "unrelated-service"])
async def test_a2a_mock_audience_policy_remains_fail_closed(idp, monkeypatch, audience):
    from shared.a2a_security import A2ASecurityValidator

    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    assert await A2ASecurityValidator(require_first_party_user=True).validate_token(idp.token(aud=audience)) is None
