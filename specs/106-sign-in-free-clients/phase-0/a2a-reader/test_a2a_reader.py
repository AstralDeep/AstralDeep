"""Exercises a2a_reader against every fixture card, the refusals, signature verification, signer pinning and unsigned pinning.
The fixtures come from make_fixtures.py, and one interop test checks the reader against the a2a-sdk signer when it is installed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import a2a_reader as r
import make_fixtures as mf

FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN_URL = "https://agent.example.org/.well-known/agent-card.json"


def load(*parts: str):
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


def keys():
    return [load("keys", f"{kid}.public.jwk.json") for kid in mf.SIGNER_SEEDS]


def pin_for(kid: str) -> r.SignerPin:
    return r.SignerPin(r.jwk_thumbprint(load("keys", f"{kid}.public.jwk.json")), kid)


def test_fixture_files_match_generator():
    for name, card in mf.VALID.items():
        assert load("valid", f"{name}.json") == card


@pytest.mark.parametrize(
    ("fixture", "scheme", "kind"),
    [
        ("api_key_header", "key", r.API_KEY),
        ("api_key_query", "key", r.API_KEY),
        ("api_key_cookie", "key", r.API_KEY),
        ("http_bearer", "tok", r.HTTP_BEARER),
        ("http_basic", "basic", r.HTTP_BASIC),
        ("http_digest", "dig", r.HTTP_OTHER),
        ("oauth2_auth_code_pkce", "oauth", r.OAUTH_AUTHORIZATION_CODE),
        ("oauth2_device_code", "oauth", r.OAUTH_DEVICE_CODE),
        ("oauth2_client_credentials", "oauth", r.OAUTH_CLIENT_CREDENTIALS),
        ("oauth2_implicit_deprecated", "oauth", r.OAUTH_DEPRECATED),
        ("openid_connect", "oidc", r.OPENID_CONNECT),
        ("mutual_tls", "mtls", r.MUTUAL_TLS),
    ],
)
def test_each_scheme_parses_to_its_kind(fixture, scheme, kind):
    reading = r.read_card(load("valid", f"{fixture}.json"))
    assert reading.schemes[scheme].kind == kind
    assert reading.dialect == "v1"
    assert len(reading.alternatives) == 1
    assert reading.alternatives[0].scheme_names == (scheme,)


@pytest.mark.parametrize(
    ("fixture", "location", "parameter"),
    [("api_key_header", "header", "X-Api-Key"), ("api_key_query", "query", "api_key"), ("api_key_cookie", "cookie", "session_key")],
)
def test_api_key_location_and_name(fixture, location, parameter):
    scheme = r.read_card(load("valid", f"{fixture}.json")).schemes["key"]
    assert (scheme.location, scheme.parameter) == (location, parameter)


def test_http_scheme_names_are_case_insensitive_and_keep_bearer_format():
    scheme = r.read_card(load("valid", "http_bearer.json")).schemes["tok"]
    assert (scheme.http_scheme, scheme.bearer_format) == ("Bearer", "JWT")


def test_auth_code_flow_carries_urls_scopes_and_pkce():
    reading = r.read_card(load("valid", "oauth2_auth_code_pkce.json"))
    scheme = reading.schemes["oauth"]
    flow = scheme.flows[0]
    assert flow.pkce_required and flow.scopes == ("read", "write")
    assert flow.authorization_url == "https://login.example.org/authorize"
    assert scheme.needs_browser and scheme.supported
    assert reading.auth_hosts() == ("login.example.org",)
    assert reading.alternatives[0].scopes_by_scheme == (("oauth", ("read",)),)
    assert "pkce_not_declared_client_enforces" not in reading.flags


def test_auth_code_without_pkce_flag_is_read_and_flagged():
    reading = r.read_card(load("valid", "oauth2_auth_code_no_pkce_flag.json"))
    assert not reading.schemes["oauth"].flows[0].pkce_required
    assert "pkce_not_declared_client_enforces" in reading.flags


def test_device_code_needs_no_browser_on_this_device():
    scheme = r.read_card(load("valid", "oauth2_device_code.json")).schemes["oauth"]
    assert scheme.supported and not scheme.needs_browser
    assert scheme.flows[0].device_authorization_url == "https://login.example.org/device"


def test_openid_connect_keeps_discovery_url_and_scopes():
    reading = r.read_card(load("valid", "openid_connect.json"))
    assert reading.schemes["oidc"].discovery_url.endswith("/.well-known/openid-configuration")
    assert reading.alternatives[0].scopes_by_scheme == (("oidc", ("openid", "profile")),)


@pytest.mark.parametrize("fixture", ["http_digest", "mutual_tls", "oauth2_client_credentials", "oauth2_implicit_deprecated"])
def test_unsupported_schemes_parse_but_leave_no_usable_alternative(fixture):
    reading = r.read_card(load("valid", f"{fixture}.json"))
    assert not reading.schemes[next(iter(reading.schemes))].supported
    assert reading.usable_alternatives() == ()
    assert "no_supported_alternative" in reading.flags


def test_alternatives_or_across_entries_and_and_within_one():
    reading = r.read_card(load("valid", "alternatives.json"))
    assert [alt.scheme_names for alt in reading.alternatives] == [("mtls",), ("key", "tok"), ("tok",)]
    assert [alt.scheme_names for alt in reading.usable_alternatives()] == [("key", "tok"), ("tok",)]


def test_extended_card_flag_and_skill_requirements():
    assert r.read_card(load("valid", "extended_card.json")).extended_card is True
    assert r.read_card(load("valid", "api_key_header.json")).extended_card is False
    reading = r.read_card(load("valid", "skill_requirements.json"))
    assert [alt.scheme_names for alt in reading.skill_requirements["admin"]] == [("tok",)]


def test_spec_sample_flat_security_key_is_accepted():
    reading = r.read_card(load("valid", "spec_sample_flat_security.json"))
    assert reading.alternatives[0].scopes_by_scheme == (("google", ("openid", "profile", "email")),)


def test_v03_shapes_are_accepted():
    reading = r.read_card(load("valid", "v03_flat_types.json"))
    assert reading.dialect == "v0.3"
    assert reading.extended_card is True
    assert reading.schemes["key"].location == "header"
    assert reading.schemes["oauth"].kind == r.OAUTH_AUTHORIZATION_CODE
    assert [alt.scheme_names for alt in reading.alternatives] == [("key",), ("oauth",)]
    assert reading.interface_urls == ("https://agent.example.org/a2a/v1",)


def test_card_without_requirements_is_flagged():
    reading = r.read_card(load("valid", "no_requirements.json"))
    assert reading.alternatives == () and "no_requirements_declared" in reading.flags


@pytest.mark.parametrize("name", sorted(mf.MALFORMED))
def test_malformed_cards_are_refused_with_a_reason(name):
    stored = load("malformed", f"{name}.json")
    with pytest.raises(r.CardRefusal) as refusal:
        r.read_card(stored["card"])
    assert refusal.value.code == stored["expectedRefusal"]
    assert refusal.value.reason


@pytest.mark.parametrize("value", [[], "card", None, 3])
def test_non_object_cards_are_refused(value):
    with pytest.raises(r.CardRefusal) as refusal:
        r.read_card(value)
    assert refusal.value.code == "not_an_object"


def test_other_shape_refusals():
    card = copy.deepcopy(mf.VALID["api_key_header"])
    for mutate, code in [
        (lambda c: c.update(skills="x"), "wrong_type"),
        (lambda c: c.update(capabilities="x"), "wrong_type"),
        (lambda c: c.update(signatures="x"), "wrong_type"),
        (lambda c: c.update(supportedInterfaces=["x"]), "wrong_type"),
        (lambda c: c.update(securitySchemes={"k": "x"}), "wrong_type"),
        (lambda c: c.update(securityRequirements={}), "wrong_type"),
        (lambda c: c.update(securityRequirements=["x"]), "wrong_type"),
        (lambda c: c.update(securitySchemes={"k": {}}), "unknown_scheme_type"),
        (lambda c: c.update(securitySchemes={"k": {"type": "saml"}}), "unknown_scheme_type"),
        (lambda c: c.update(securitySchemes={"k": {"apiKeySecurityScheme": "x"}}), "wrong_type"),
        (lambda c: c.update(name=4), "wrong_type"),
    ]:
        broken = copy.deepcopy(card)
        mutate(broken)
        with pytest.raises(r.CardRefusal) as refusal:
            r.read_card(broken)
        assert refusal.value.code == code


def test_pkce_flag_must_be_boolean():
    card = copy.deepcopy(mf.VALID["oauth2_auth_code_pkce"])
    card["securitySchemes"]["oauth"]["oauth2SecurityScheme"]["flows"]["authorizationCode"]["pkceRequired"] = "yes"
    with pytest.raises(r.CardRefusal):
        r.read_card(card)


def test_canonical_form_is_key_order_independent_and_drops_signatures_and_empties():
    card = mf.VALID["extended_card"]
    reordered = json.loads(json.dumps(card, sort_keys=True))
    noisy = {**card, "signatures": [{"protected": "a", "signature": "b"}], "extra": "", "list": []}
    assert r.canonical_card(card) == r.canonical_card(reordered) == r.canonical_card(noisy)
    assert b'"streaming":true' in r.canonical_card(card)


def test_canonical_form_keeps_presence_booleans_and_drops_other_false():
    card = {"name": "n", "capabilities": {"streaming": False}, "x": {"pkceRequired": False, "y": 1}}
    assert r.canonical_card(card) == b'{"capabilities":{"streaming":false},"name":"n","x":{"y":1}}'


def test_canonical_form_sorts_by_utf16_and_emits_raw_utf8():
    card = {"name": "café", "\U0001f600": 1, "￿": 2}
    assert r.canonical_card(card) == '{"name":"café","\U0001f600":1,"￿":2}'.encode()


def test_canonical_form_refuses_fractional_numbers():
    with pytest.raises(r.CardRefusal):
        r.canonical_card({"name": "n", "weight": 1.5})


def test_signed_card_verifies_against_its_pinned_signer():
    result = r.verify_signed(load("signed", "signed_by_signer_one.json"), keys(), pin_for("fixture-signer-one"))
    assert result.status == "verified_pinned" and result.accepted
    assert result.digest.startswith("sha256:") and result.kid == "fixture-signer-one"


def test_signed_card_from_a_new_signer_is_not_accepted_until_approved():
    result = r.verify_signed(load("signed", "signed_by_signer_two.json"), keys(), pin_for("fixture-signer-one"))
    assert result.status == "verified_new_signer" and not result.accepted
    assert result.thumbprint == pin_for("fixture-signer-two").thumbprint


def test_first_sight_with_no_pin_reports_the_signer_for_approval():
    result = r.verify_signed(load("signed", "signed_by_signer_one.json"), keys(), None)
    assert result.status == "verified_new_signer"
    assert result.thumbprint == pin_for("fixture-signer-one").thumbprint


def test_a_changed_signed_card_fails_verification():
    card = load("signed", "signed_by_signer_one.json")
    card["version"] = "9.9.9"
    result = r.verify_signed(card, keys(), pin_for("fixture-signer-one"))
    assert result.status == "invalid" and not result.accepted


def test_a_signature_with_no_matching_key_fails():
    result = r.verify_signed(load("signed", "signed_by_signer_one.json"), [], pin_for("fixture-signer-one"))
    assert result.status == "invalid"


def test_unsigned_card_reports_unsigned():
    assert r.verify_signed(load("valid", "extended_card.json"), keys(), None).status == "unsigned"


def test_malformed_and_disallowed_signatures_fail_closed():
    card = load("signed", "signed_by_signer_one.json")
    for bad in [{"protected": "!!", "signature": "x"}, {"signature": "x"}, {"protected": r._b64url(b'{"alg":"none","kid":"k"}'), "signature": ""}]:
        assert r.verify_signed({**card, "signatures": [bad]}, keys(), None).status == "invalid"


def test_signer_pin_is_the_key_thumbprint_not_the_key_id():
    imposter = dict(load("keys", "fixture-signer-two.public.jwk.json"), kid="fixture-signer-one")
    card = mf.sign_card(mf.VALID["extended_card"], mf.derive_key(mf.SIGNER_SEEDS["fixture-signer-two"]), "fixture-signer-one")
    result = r.verify_signed(card, [imposter], pin_for("fixture-signer-one"))
    assert result.status == "verified_new_signer"


def test_thumbprint_rejects_unusable_keys():
    with pytest.raises(r.CardRefusal):
        r.jwk_thumbprint({"kty": "oct", "k": "x"})


def test_unsigned_card_pins_by_hash_and_origin():
    card = load("valid", "extended_card.json")
    pin = r.pin_unsigned(card, ORIGIN_URL)
    assert pin.origin == "https://agent.example.org" and pin.digest.startswith("sha256:")
    assert r.check_unsigned(pin, card, ORIGIN_URL) == "match"
    assert r.check_unsigned(pin, json.loads(json.dumps(card, sort_keys=True)), "https://AGENT.example.org:443/other") == "match"


def test_changed_unsigned_card_fails_its_pin_and_names_what_changed():
    card = load("valid", "extended_card.json")
    pin = r.pin_unsigned(card, ORIGIN_URL)
    changed = load("signed", "unsigned_changed_version.json")
    assert r.check_unsigned(pin, changed, ORIGIN_URL) == "card_changed"
    assert r.changed_paths(card, changed) == ["version"]
    widened = copy.deepcopy(card)
    widened["securitySchemes"]["tok"]["httpAuthSecurityScheme"]["scheme"] = "basic"
    assert r.check_unsigned(pin, widened, ORIGIN_URL) == "card_changed"


def test_unsigned_card_served_from_another_origin_fails_its_pin():
    card = load("valid", "extended_card.json")
    pin = r.pin_unsigned(card, ORIGIN_URL)
    assert r.check_unsigned(pin, card, "https://evil.example.net/.well-known/agent-card.json") == "origin_changed"
    assert r.check_unsigned(pin, card, "https://agent.example.org:8443/x") == "origin_changed"


def test_pinning_needs_an_https_origin():
    for url in ["http://agent.example.org/card", "https://user:pw@agent.example.org/card", "agent.example.org"]:
        with pytest.raises(r.CardRefusal):
            r.pin_unsigned(mf.VALID["extended_card"], url)


def test_signature_does_not_change_the_unsigned_digest():
    base = mf.VALID["extended_card"]
    signed = load("signed", "signed_by_signer_one.json")
    assert r.card_digest(base) == r.card_digest(signed)


def test_reader_verifies_a_card_signed_by_the_a2a_sdk_and_the_sdk_verifies_ours():
    signing = pytest.importorskip("a2a.utils.signing")
    from a2a.types import AgentCard
    from google.protobuf.json_format import MessageToDict, ParseDict

    key = mf.derive_key("sdk-interop")
    jwk = mf.public_jwk(key, "sdk-kid")
    card = ParseDict(copy.deepcopy(mf.VALID["extended_card"]), AgentCard())
    signed = signing.create_agent_card_signer(key, {"kid": "sdk-kid", "alg": "ES256", "jku": None, "typ": "JOSE"})(card)
    result = r.verify_signed(MessageToDict(signed), [jwk], r.SignerPin(r.jwk_thumbprint(jwk)))
    assert result.status == "verified_pinned"

    ours = ParseDict(mf.sign_card(mf.VALID["extended_card"], key, "sdk-kid"), AgentCard())
    signing.create_signature_verifier(lambda kid, jku: jwk_to_pyjwk(jwk), ["ES256"])(ours)


def jwk_to_pyjwk(jwk):
    import jwt

    return jwt.PyJWK.from_dict(jwk)
