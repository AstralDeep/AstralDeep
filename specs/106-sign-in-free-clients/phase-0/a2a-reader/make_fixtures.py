"""Writes the fixture agent cards, the test signer keys and the signed cards under fixtures/.
The keys derive from fixed seed strings, so no private key is stored, and a2a_reader's tests import the signing helpers here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

import a2a_reader

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "https://agent.example.org"

BASE: dict[str, Any] = {
    "name": "Fixture Agent",
    "description": "Fixture card for the reader tests.",
    "version": "1.0.0",
    "supportedInterfaces": [{"url": f"{HOST}/a2a/v1", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": True},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{"id": "echo", "name": "Echo", "description": "Echoes text.", "tags": ["echo"]}],
}


def _card(schemes: dict[str, Any], requirements: list[Any], **extra: Any) -> dict[str, Any]:
    card = copy.deepcopy(BASE)
    card["securitySchemes"] = schemes
    card["securityRequirements"] = requirements
    card.update(extra)
    return {key: value for key, value in card.items() if value is not None}


def _req(**schemes: list[str]) -> dict[str, Any]:
    return {"schemes": {name: {"list": scopes} for name, scopes in schemes.items()}}


AUTH_CODE = {"authorizationUrl": "https://login.example.org/authorize", "tokenUrl": "https://login.example.org/token",
             "scopes": {"read": "Read", "write": "Write"}, "pkceRequired": True}

VALID: dict[str, dict[str, Any]] = {
    "api_key_header": _card({"key": {"apiKeySecurityScheme": {"location": "header", "name": "X-Api-Key"}}}, [_req(key=[])]),
    "api_key_query": _card({"key": {"apiKeySecurityScheme": {"location": "query", "name": "api_key"}}}, [_req(key=[])]),
    "api_key_cookie": _card({"key": {"apiKeySecurityScheme": {"location": "cookie", "name": "session_key"}}}, [_req(key=[])]),
    "http_bearer": _card({"tok": {"httpAuthSecurityScheme": {"scheme": "Bearer", "bearerFormat": "JWT"}}}, [_req(tok=[])]),
    "http_basic": _card({"basic": {"httpAuthSecurityScheme": {"scheme": "basic"}}}, [_req(basic=[])]),
    "http_digest": _card({"dig": {"httpAuthSecurityScheme": {"scheme": "Digest"}}}, [_req(dig=[])]),
    "oauth2_auth_code_pkce": _card(
        {"oauth": {"oauth2SecurityScheme": {"flows": {"authorizationCode": AUTH_CODE},
                                            "oauth2MetadataUrl": "https://login.example.org/.well-known/oauth-authorization-server"}}},
        [_req(oauth=["read"])]),
    "oauth2_auth_code_no_pkce_flag": _card(
        {"oauth": {"oauth2SecurityScheme": {"flows": {"authorizationCode": {**AUTH_CODE, "pkceRequired": False}}}}},
        [_req(oauth=["read"])]),
    "oauth2_device_code": _card(
        {"oauth": {"oauth2SecurityScheme": {"flows": {"deviceCode": {
            "deviceAuthorizationUrl": "https://login.example.org/device", "tokenUrl": "https://login.example.org/token",
            "scopes": {"read": "Read"}}}}}}, [_req(oauth=["read"])]),
    "oauth2_client_credentials": _card(
        {"oauth": {"oauth2SecurityScheme": {"flows": {"clientCredentials": {
            "tokenUrl": "https://login.example.org/token", "scopes": {"read": "Read"}}}}}}, [_req(oauth=["read"])]),
    "oauth2_implicit_deprecated": _card(
        {"oauth": {"oauth2SecurityScheme": {"flows": {"implicit": {
            "authorizationUrl": "https://login.example.org/authorize", "scopes": {}}}}}}, [_req(oauth=[])]),
    "openid_connect": _card(
        {"oidc": {"openIdConnectSecurityScheme": {"openIdConnectUrl": "https://login.example.org/.well-known/openid-configuration"}}},
        [_req(oidc=["openid", "profile"])]),
    "mutual_tls": _card({"mtls": {"mtlsSecurityScheme": {}}}, [_req(mtls=[])]),
    "alternatives": _card(
        {"key": {"apiKeySecurityScheme": {"location": "header", "name": "X-Api-Key"}},
         "tok": {"httpAuthSecurityScheme": {"scheme": "bearer"}},
         "mtls": {"mtlsSecurityScheme": {}}},
        [_req(mtls=[]), _req(key=[], tok=[]), _req(tok=[])]),
    "extended_card": _card(
        {"tok": {"httpAuthSecurityScheme": {"scheme": "bearer"}}}, [_req(tok=[])],
        capabilities={"streaming": True, "extendedAgentCard": True}),
    "skill_requirements": _card(
        {"key": {"apiKeySecurityScheme": {"location": "header", "name": "X-Api-Key"}},
         "tok": {"httpAuthSecurityScheme": {"scheme": "bearer"}}},
        [_req(key=[])],
        skills=[{"id": "admin", "name": "Admin", "description": "Admin.", "tags": ["a"], "securityRequirements": [_req(tok=[])]}]),
    "spec_sample_flat_security": _card(
        {"google": {"openIdConnectSecurityScheme": {"openIdConnectUrl": "https://accounts.example.org/.well-known/openid-configuration"}}},
        [], security=[{"google": ["openid", "profile", "email"]}]),
    "v03_flat_types": {
        **{k: v for k, v in BASE.items() if k != "supportedInterfaces"},
        "url": f"{HOST}/a2a/v1", "supportsAuthenticatedExtendedCard": True,
        "securitySchemes": {
            "key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "oauth": {"type": "oauth2", "flows": {"authorizationCode": {
                "authorizationUrl": "https://login.example.org/authorize", "tokenUrl": "https://login.example.org/token", "scopes": {"read": "Read"}}}},
        },
        "security": [{"key": []}, {"oauth": ["read"]}],
    },
    "no_requirements": copy.deepcopy(BASE),
}

VALID["spec_sample_flat_security"].pop("securityRequirements")
_KEY_SCHEME = {"key": {"apiKeySecurityScheme": {"location": "header", "name": "X-Api-Key"}}}


def _without(card: dict[str, Any], *path: str) -> dict[str, Any]:
    clone = copy.deepcopy(card)
    node = clone
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return clone


def _with(card: dict[str, Any], value: Any, *path: str) -> dict[str, Any]:
    clone = copy.deepcopy(card)
    node = clone
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return clone


MALFORMED: dict[str, tuple[dict[str, Any], str]] = {
    "api_key_bad_location": (_with(VALID["api_key_header"], "body", "securitySchemes", "key", "apiKeySecurityScheme", "location"), "bad_api_key_location"),
    "api_key_no_name": (_without(VALID["api_key_header"], "securitySchemes", "key", "apiKeySecurityScheme", "name"), "missing_field"),
    "http_no_scheme": (_without(VALID["http_bearer"], "securitySchemes", "tok", "httpAuthSecurityScheme", "scheme"), "missing_field"),
    "oauth2_no_flows": (_with(VALID["oauth2_auth_code_pkce"], {}, "securitySchemes", "oauth", "oauth2SecurityScheme", "flows"), "missing_field"),
    "oauth2_plain_http_token_url": (
        _with(VALID["oauth2_auth_code_pkce"], "http://login.example.org/token", "securitySchemes", "oauth", "oauth2SecurityScheme", "flows", "authorizationCode", "tokenUrl"),
        "insecure_url"),
    "oauth2_missing_auth_url": (
        _without(VALID["oauth2_auth_code_pkce"], "securitySchemes", "oauth", "oauth2SecurityScheme", "flows", "authorizationCode", "authorizationUrl"), "missing_field"),
    "oauth2_unknown_flow": (_with(VALID["oauth2_auth_code_pkce"], {"magic": {}}, "securitySchemes", "oauth", "oauth2SecurityScheme", "flows"), "unknown_flow"),
    "oidc_no_url": (_with(VALID["openid_connect"], "", "securitySchemes", "oidc", "openIdConnectSecurityScheme", "openIdConnectUrl"), "missing_field"),
    "mtls_with_two_variants": (_with(VALID["mutual_tls"], {"mtlsSecurityScheme": {}, "apiKeySecurityScheme": {"location": "header", "name": "k"}}, "securitySchemes", "mtls"), "ambiguous_scheme"),
    "scheme_unknown_variant": (_with(VALID["api_key_header"], {"samlSecurityScheme": {}}, "securitySchemes", "key"), "unknown_scheme_type"),
    "requirement_undefined_scheme": (_with(VALID["api_key_header"], [_req(ghost=[])], "securityRequirements"), "requirement_unknown_scheme"),
    "requirement_scopes_not_list": (_with(VALID["api_key_header"], [{"schemes": {"key": {"list": "read"}}}], "securityRequirements"), "wrong_type"),
    "requirement_empty": (_with(VALID["api_key_header"], [{"schemes": {}}], "securityRequirements"), "empty_requirement"),
    "requirements_conflict": (_with(VALID["api_key_header"], [{"key": ["x"]}], "security"), "conflicting_requirements"),
    "extended_flag_not_bool": (_with(VALID["extended_card"], "yes", "capabilities", "extendedAgentCard"), "wrong_type"),
    "interface_plain_http": (_with(VALID["api_key_header"], [{"url": "http://agent.example.org/a2a"}], "supportedInterfaces"), "insecure_url"),
    "schemes_not_object": (_with(VALID["api_key_header"], [], "securitySchemes"), "wrong_type"),
    "mixed_dialects": (_with(VALID["api_key_header"], {**_KEY_SCHEME, "old": {"type": "http", "scheme": "bearer"}}, "securitySchemes"), "mixed_dialects"),
    "no_name": (_without(VALID["api_key_header"], "name"), "missing_field"),
}


def derive_key(seed: str) -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(hashlib.sha256(seed.encode()).digest(), "big") % (2**255) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def public_jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid}


def sign_card(card: dict[str, Any], key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    header = {"alg": "ES256", "typ": "JOSE", "kid": kid}
    token = jwt.PyJWS().encode(a2a_reader.canonical_card(card), key, algorithm="ES256", headers=header)
    protected, _, signature = token.split(".")
    return {**{k: v for k, v in card.items() if k != "signatures"}, "signatures": [{"protected": protected, "signature": signature}]}


SIGNER_SEEDS = {"fixture-signer-one": "test-signer-1", "fixture-signer-two": "test-signer-2"}


def write_all() -> None:
    for sub in ("valid", "malformed", "signed", "keys"):
        (FIXTURES / sub).mkdir(parents=True, exist_ok=True)
    for name, card in VALID.items():
        _dump(FIXTURES / "valid" / f"{name}.json", card)
    for name, (card, code) in MALFORMED.items():
        _dump(FIXTURES / "malformed" / f"{name}.json", {"expectedRefusal": code, "card": card})
    base = VALID["extended_card"]
    for kid, seed in SIGNER_SEEDS.items():
        key = derive_key(seed)
        _dump(FIXTURES / "keys" / f"{kid}.public.jwk.json", public_jwk(key, kid))
    _dump(FIXTURES / "signed" / "signed_by_signer_one.json", sign_card(base, derive_key(SIGNER_SEEDS["fixture-signer-one"]), "fixture-signer-one"))
    _dump(FIXTURES / "signed" / "signed_by_signer_two.json", sign_card(base, derive_key(SIGNER_SEEDS["fixture-signer-two"]), "fixture-signer-two"))
    unsigned_changed = _with(base, "2.0.0", "version")
    _dump(FIXTURES / "signed" / "unsigned_changed_version.json", unsigned_changed)


def _dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_all()
