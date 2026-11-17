"""Generates the A2A card vectors from the phase 0 fixture cards and public keys, signing extra cards with deterministic test keys derived from fixed seed strings.
It runs every case through a2a_card.py, and `--check` fails when the committed file differs and writes nothing.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
a2a_card = importlib.import_module("a2a_card")
cj = importlib.import_module("astral_mesh_ref.cj")
b64 = cj.b64url_encode

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed25519  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402

VECTORS = HERE.parent / "vectors"
FIXTURES = HERE.parents[1] / "phase-0/a2a-reader/fixtures"
SPEC = "106/contracts/connection.md"
ORIGIN_URL = "https://agent.example.org/.well-known/agent-card.json"
RSA_KID = "fixture-rsa"
RSA_JWK = {
    "e": "AQAB",
    "kid": RSA_KID,
    "kty": "RSA",
    "n": "uC8vAMfgjNY54Hw50ahoscOawAu-eq5F_qQ_tAYMLEocVk3xj6JNusaSf5BMUvwzGBBplYrl21ci5QBbLErA4S4rEnkq3x2eFYS1hAt30nATbbYYaTeRlSAMY0esf1RqBxwx4DQanat2okJKrOxnT0KgLNeatqMO0l-uIHiiqu6TXpBLwGKNltq-3MQaWadX4_6-SPtjb_ERQAX_6VbWNXsuFLdwSukwAEYoLp1lu9bus0A7ofAaDTHgxljQ1YmmCWSHAsCvHiFieYsP3ecfuu2sHgp4sVoZTJyrSmlcmSN9O3PYjWhNg1KBYG3nORhlD89Lh393cHGPw1DmCORtxQ",
}
RSA_SMALL_JWK = {
    "e": "AQAB",
    "kid": RSA_KID,
    "kty": "RSA",
    "n": "nsqErQ5c_SRlUPZ68GX2E4zuatftAA-vMXOUNN4rh4bkWKltO47-kNr9MwXGjk4CWdE-eoJEgIpNrzFG3NVUFDDlXWm9pp26mXU9tj1aeaKMtQlFuh59c76kfi4KOn4flJAyTVreHzBApY2uMYSiZYQcIMQyCDQ--8g8Ulodw1M",
}
RSA_PROTECTED = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImZpeHR1cmUtcnNhIiwidHlwIjoiSk9TRSJ9"
RSA_SIGNATURE = "bDHtOLyvhlA4kRJ_Xctj39fi9oMvD4dXOl8Hb2Y0rHDRwAENhqIyrHlKlN7-OFHgXOztH4pyxr2o0B-V3M-fEsSCNNGE5I1ZlEykX2bUjeDMWZZSjyWNuJ8QjXy4GyoSPCPLK7cvnuODCXIPpvzO1n9YqiJNU9IutBbClofiZ-F4b5RNT8Gw5yb2B9bPafRi7OOIkjVL7TJ4QU5chk7dDnkLyEXeWeBtwF8Pnd7g-olNNUK963nIcqC7BwXIzzzB-dtSPUn5eecNrbt-shnPT4s3gPfsoEmUt3YXbmVbImSjIeHb3V6SfSFUsFfelAOOxaF--AsyK3fbiTgJGl_3Bw"
SIGNER_SEEDS = {
    "fixture-signer-one": "test-signer-1",
    "fixture-signer-two": "test-signer-2",
}


def load(*parts: str) -> Any:
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


def names(folder: str) -> list[str]:
    return sorted(p.stem for p in (FIXTURES / folder).glob("*.json"))


def derive_ec(seed: str) -> ec.EllipticCurvePrivateKey:
    scalar = (
        int.from_bytes(hashlib.sha256(seed.encode()).digest(), "big") % (2**255) + 1
    )
    return ec.derive_private_key(scalar, ec.SECP256R1())


def derive_ed(seed: str) -> ed25519.Ed25519PrivateKey:
    return ed25519.Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(seed.encode()).digest()
    )


def ec_jwk(key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    return {
        "crv": "P-256",
        "kid": kid,
        "kty": "EC",
        "x": b64(numbers.x.to_bytes(32, "big")),
        "y": b64(numbers.y.to_bytes(32, "big")),
    }


def ed_jwk(key: ed25519.Ed25519PrivateKey, kid: str) -> dict[str, Any]:
    raw = key.public_key().public_bytes_raw()
    return {"crv": "Ed25519", "kid": kid, "kty": "OKP", "x": b64(raw)}


def sign(
    card: dict[str, Any], key: Any, alg: str, kid: str, sdk: bool = False
) -> dict[str, Any]:
    protected = b64(
        json.dumps(
            {"alg": alg, "kid": kid, "typ": "JOSE"}, separators=(",", ":")
        ).encode()
    )
    payload = b64(a2a_card.canonical_card(card, sdk_lenient=sdk))
    message = f"{protected}.{payload}".encode()
    if alg == "ES256":
        r, s = decode_dss_signature(
            key.sign(message, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
        )
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    else:
        raw = key.sign(message)
    body = {k: v for k, v in card.items() if k != "signatures"}
    return {**body, "signatures": [{"protected": protected, "signature": b64(raw)}]}


def with_(card: dict[str, Any], mutate: Any) -> dict[str, Any]:
    clone = copy.deepcopy(card)
    mutate(clone)
    return clone


KEY_ONE = derive_ec(SIGNER_SEEDS["fixture-signer-one"])
KEY_TWO = derive_ec(SIGNER_SEEDS["fixture-signer-two"])
KEY_ED = derive_ed("a2a-card-v1/ed25519")
JWK_ONE = load("keys", "fixture-signer-one.public.jwk.json")
JWK_TWO = load("keys", "fixture-signer-two.public.jwk.json")
JWK_ED = ed_jwk(KEY_ED, "fixture-ed")
assert ec_jwk(KEY_ONE, "fixture-signer-one") == JWK_ONE
assert ec_jwk(KEY_TWO, "fixture-signer-two") == JWK_TWO
KEYS = [JWK_ONE, JWK_TWO]
PIN_ONE = a2a_card.jwk_thumbprint(JWK_ONE)
PIN_TWO = a2a_card.jwk_thumbprint(JWK_TWO)

EXTENDED = load("valid", "extended_card.json")
API_KEY = load("valid", "api_key_header.json")
SIGNED_ONE = load("signed", "signed_by_signer_one.json")
SIGNED_TWO = load("signed", "signed_by_signer_two.json")
CHANGED = load("signed", "unsigned_changed_version.json")

NON_ASCII = {
    **copy.deepcopy(EXTENDED),
    "name": "Café Ágent 日本語 \U0001f600",
    "description": "Grüße, مرحبا and ☃",
    "skills": [
        {
            "id": "grüß",
            "name": "Übersetzen",
            "description": "Café ☕",
            "tags": ["日本"],
        }
    ],
}
UTF16_ORDER = {"name": "n", "\U0001f600": 1, "￿": 2, "é": 3, "e": 4}
HOST = "agent.example.org"


def pin_of(
    card: dict[str, Any], signer: str | None = None, origin: str = "https://" + HOST
) -> dict[str, Any]:
    return {"hash": a2a_card.card_digest(card), "origin": origin, "signer": signer}


def generate() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
        expect = a2a_card.run(kind, case_input)
        cases.append(
            {"name": name, "kind": kind, "input": case_input, "expect": expect}
        )
        return expect

    for name in names("valid"):
        add(
            f"card_valid_{name}",
            "a2a_card_read",
            {"card": load("valid", f"{name}.json")},
        )
    add("card_valid_non_ascii", "a2a_card_read", {"card": NON_ASCII})
    for name in names("malformed"):
        stored = load("malformed", f"{name}.json")
        expect = add(
            f"card_malformed_{name}", "a2a_card_read", {"card": stored["card"]}
        )
        assert expect == {"error": "a2a." + stored["expectedRefusal"]}, name
    for value in ([], "card", None, 3):
        add(
            f"card_not_an_object_{type(value).__name__}",
            "a2a_card_read",
            {"card": value},
        )
    for name, mutate in (
        ("skills_not_array", lambda c: c.update(skills="x")),
        ("capabilities_not_object", lambda c: c.update(capabilities="x")),
        ("signatures_not_array", lambda c: c.update(signatures="x")),
        ("interfaces_not_objects", lambda c: c.update(supportedInterfaces=["x"])),
        ("requirements_not_array", lambda c: c.update(securityRequirements={})),
        ("requirement_not_object", lambda c: c.update(securityRequirements=["x"])),
        ("scheme_empty_object", lambda c: c.update(securitySchemes={"k": {}})),
        (
            "scheme_type_saml",
            lambda c: c.update(securitySchemes={"k": {"type": "saml"}}),
        ),
        (
            "scheme_body_not_object",
            lambda c: c.update(securitySchemes={"k": {"apiKeySecurityScheme": "x"}}),
        ),
        ("name_not_text", lambda c: c.update(name=4)),
    ):
        add(f"card_refused_{name}", "a2a_card_read", {"card": with_(API_KEY, mutate)})
    add(
        "card_refused_pkce_flag_not_boolean",
        "a2a_card_read",
        {
            "card": with_(
                load("valid", "oauth2_auth_code_pkce.json"),
                lambda c: c["securitySchemes"]["oauth"]["oauth2SecurityScheme"][
                    "flows"
                ]["authorizationCode"].update(pkceRequired="yes"),
            )
        },
    )
    deprecated = {
        "implicit": {
            "authorizationUrl": "https://login.example.org/authorize",
            "refreshUrl": "https://login.example.org/refresh",
            "scopes": {},
        },
        "password": {"tokenUrl": "https://login.example.org/token", "scopes": {}},
    }
    for flow, body in deprecated.items():
        card = with_(
            load("valid", "oauth2_implicit_deprecated.json"),
            lambda c, f=flow, b=body: c["securitySchemes"]["oauth"][
                "oauth2SecurityScheme"
            ].update(flows={f: b}),
        )
        add(f"card_valid_deprecated_flow_{flow}", "a2a_card_read", {"card": card})
        for field in body:
            if field.endswith("Url"):
                bad = with_(
                    card,
                    lambda c, f=flow, k=field: c["securitySchemes"]["oauth"][
                        "oauth2SecurityScheme"
                    ]["flows"][f].update({k: "http://login.example.org/x"}),
                )
                add(
                    f"card_refused_{flow}_{field}_plain_http",
                    "a2a_card_read",
                    {"card": bad},
                )
    add(
        "card_empty_scope_object_reads_as_no_scopes",
        "a2a_card_read",
        {
            "card": with_(
                API_KEY,
                lambda c: c.update(securityRequirements=[{"schemes": {"key": {}}}]),
            )
        },
    )

    for name in names("valid"):
        for node in (False, True):
            add(
                f"select_{name}_{'node' if node else 'device'}",
                "a2a_card_select",
                {"card": load("valid", f"{name}.json"), "node": node},
            )

    for name, card in (
        ("extended_card", EXTENDED),
        ("api_key_empty_scope_lists", API_KEY),
        ("no_requirements", load("valid", "no_requirements.json")),
        ("v03_flat_types", load("valid", "v03_flat_types.json")),
        ("non_ascii", NON_ASCII),
        ("utf16_key_order", UTF16_ORDER),
        ("signatures_dropped", SIGNED_ONE),
        (
            "false_flags_dropped_presence_booleans_kept",
            {
                "name": "n",
                "capabilities": {"streaming": False},
                "x": {"pkceRequired": False, "y": 1},
            },
        ),
        ("empty_values_dropped", {**EXTENDED, "extra": "", "list": [], "none": None}),
        ("integer_valued_float", {"name": "n", "weight": 2.0}),
        ("fractional_number_refused", {"name": "n", "weight": 1.5}),
        ("key_order_does_not_matter", json.loads(json.dumps(EXTENDED, sort_keys=True))),
    ):
        add(f"canonical_{name}", "a2a_card_canonical", {"card": card})

    for name, jwk in (
        ("signer_one", JWK_ONE),
        ("signer_two", JWK_TWO),
        ("ed25519", JWK_ED),
        ("rsa", RSA_JWK),
    ):
        add(f"thumbprint_{name}", "a2a_thumbprint", {"jwk": jwk})
    add("thumbprint_oct_refused", "a2a_thumbprint", {"jwk": {"kty": "oct", "k": "x"}})
    add(
        "thumbprint_ignores_kid", "a2a_thumbprint", {"jwk": {**JWK_ONE, "kid": "other"}}
    )

    signed_ed = sign(EXTENDED, KEY_ED, "EdDSA", "fixture-ed")
    signed_rsa = {
        **EXTENDED,
        "signatures": [{"protected": RSA_PROTECTED, "signature": RSA_SIGNATURE}],
    }
    sdk_only = sign(API_KEY, KEY_ONE, "ES256", "fixture-signer-one", sdk=True)
    pkce_card = load("valid", "oauth2_auth_code_pkce.json")
    sdk_equal = sign(pkce_card, KEY_ONE, "ES256", "fixture-signer-one", sdk=True)
    sdk_flag_only = sign(
        with_(pkce_card, lambda c: c["capabilities"].update(streaming=False)),
        KEY_ONE,
        "ES256",
        "fixture-signer-one",
        sdk=True,
    )
    resigned = sign(CHANGED, KEY_ONE, "ES256", "fixture-signer-one")
    first_bad = {
        **SIGNED_ONE,
        "signatures": [
            {"protected": "!!", "signature": "x"},
            *SIGNED_ONE["signatures"],
        ],
    }
    no_kid = {
        **SIGNED_ONE,
        "signatures": [
            {
                "protected": b64(b'{"alg":"ES256"}'),
                "signature": SIGNED_ONE["signatures"][0]["signature"],
            }
        ],
    }
    wrong_alg = {
        **SIGNED_ONE,
        "signatures": [
            {
                "protected": b64(b'{"alg":"HS256","kid":"fixture-signer-one"}'),
                "signature": SIGNED_ONE["signatures"][0]["signature"],
            }
        ],
    }
    alg_none = {
        **SIGNED_ONE,
        "signatures": [
            {"protected": b64(b'{"alg":"none","kid":"k"}'), "signature": ""}
        ],
    }
    alg_mismatch = {
        **SIGNED_ONE,
        "signatures": [
            {
                "protected": b64(b'{"alg":"EdDSA","kid":"fixture-signer-one"}'),
                "signature": SIGNED_ONE["signatures"][0]["signature"],
            }
        ],
    }
    kidless_key = {k: v for k, v in JWK_ONE.items() if k != "kid"}
    imposter = {**JWK_TWO, "kid": "fixture-signer-one"}
    changed_signed = {**SIGNED_ONE, "version": "9.9.9"}
    for name, card, keys, pin in (
        ("signed_by_signer_one_pinned", SIGNED_ONE, KEYS, PIN_ONE),
        ("signed_by_signer_two_new_signer", SIGNED_TWO, KEYS, PIN_ONE),
        ("first_sight_reports_the_signer", SIGNED_ONE, KEYS, None),
        ("changed_signed_card_invalid", changed_signed, KEYS, PIN_ONE),
        ("no_matching_key_invalid", SIGNED_ONE, [], PIN_ONE),
        ("unsigned", EXTENDED, KEYS, None),
        ("ed25519_pinned", signed_ed, [*KEYS, JWK_ED], a2a_card.jwk_thumbprint(JWK_ED)),
        (
            "rsa_2048_pinned",
            signed_rsa,
            [*KEYS, RSA_JWK],
            a2a_card.jwk_thumbprint(RSA_JWK),
        ),
        ("rsa_1024_refused", signed_rsa, [RSA_SMALL_JWK], None),
        ("sdk_form_equal_to_strict_form", sdk_equal, KEYS, PIN_ONE),
        ("sdk_form_differs_only_outside_requirements", sdk_flag_only, KEYS, PIN_ONE),
        ("sdk_form_misses_empty_requirement_entries", sdk_only, KEYS, PIN_ONE),
        ("resigned_changed_card_pinned", resigned, KEYS, PIN_ONE),
        ("first_signature_bad_second_good", first_bad, KEYS, PIN_ONE),
        ("header_without_kid_invalid", no_kid, KEYS, PIN_ONE),
        ("algorithm_hs256_refused", wrong_alg, KEYS, PIN_ONE),
        ("algorithm_none_refused", alg_none, KEYS, PIN_ONE),
        ("algorithm_does_not_fit_key", alg_mismatch, KEYS, PIN_ONE),
        ("key_without_kid_still_verifies", SIGNED_ONE, [kidless_key], PIN_ONE),
        (
            "pin_is_the_thumbprint_not_the_kid",
            sign(EXTENDED, KEY_TWO, "ES256", "fixture-signer-one"),
            [imposter],
            PIN_ONE,
        ),
        ("signature_not_object", {**SIGNED_ONE, "signatures": ["x"]}, KEYS, PIN_ONE),
        (
            "signature_not_base64",
            {**SIGNED_ONE, "signatures": [{"protected": "!!", "signature": "x"}]},
            KEYS,
            PIN_ONE,
        ),
    ):
        add(
            f"verify_{name}",
            "a2a_card_verify",
            {"card": card, "keys": keys, "pin": pin},
        )

    for name, card in (
        ("unsigned", EXTENDED),
        ("signed", SIGNED_ONE),
        ("ed25519", signed_ed),
        ("non_ascii_unsigned", NON_ASCII),
    ):
        add(
            f"propose_{name}",
            "a2a_card_propose",
            {"card": card, "fetched_from": ORIGIN_URL, "keys": KEYS + [JWK_ED]},
        )
    for name, card, fetched in (
        (
            "origin_is_lowercased_without_default_port",
            EXTENDED,
            "https://AGENT.example.org:443/x",
        ),
        ("origin_keeps_other_port", EXTENDED, "https://agent.example.org:8443/x"),
        ("tampered_signature_invalid", changed_signed, ORIGIN_URL),
        ("sdk_only_signature_scope", sdk_only, ORIGIN_URL),
        ("sdk_form_differs_outside_requirements_accepted", sdk_flag_only, ORIGIN_URL),
        ("fetched_over_http", EXTENDED, "http://agent.example.org/card"),
        ("fetched_with_userinfo", EXTENDED, "https://user:pw@agent.example.org/card"),
        ("fetched_without_scheme", EXTENDED, "agent.example.org"),
        (
            "card_refused_first",
            load("malformed", "api_key_no_name.json")["card"],
            ORIGIN_URL,
        ),
    ):
        add(
            f"propose_{name}",
            "a2a_card_propose",
            {"card": card, "fetched_from": fetched, "keys": KEYS},
        )

    requirement_changes = (
        (
            "requirements_emptied",
            "api_key_header",
            lambda c: c.update(securityRequirements=[]),
        ),
        (
            "requirements_removed",
            "api_key_header",
            lambda c: c.pop("securityRequirements"),
        ),
        (
            "alternative_dropped",
            "alternatives",
            lambda c: c["securityRequirements"].pop(1),
        ),
        (
            "alternative_added",
            "api_key_header",
            lambda c: (
                c["securitySchemes"].update(
                    tok={"httpAuthSecurityScheme": {"scheme": "bearer"}}
                )
                or c["securityRequirements"].append({"schemes": {"tok": {"list": []}}})
            ),
        ),
        (
            "scope_added",
            "api_key_header",
            lambda c: c["securityRequirements"][0]["schemes"].update(
                key={"list": ["read"]}
            ),
        ),
        (
            "skill_requirements_emptied",
            "skill_requirements",
            lambda c: c["skills"][0].update(securityRequirements=[]),
        ),
    )
    for name, fixture, mutate in requirement_changes:
        card = load("valid", f"{fixture}.json")
        changed = with_(card, mutate)
        add(
            f"check_unsigned_{name}_breaks_the_pin",
            "a2a_card_check",
            {
                "pin": pin_of(card),
                "card": changed,
                "fetched_from": ORIGIN_URL,
                "keys": [],
                "egress": [HOST],
            },
        )
        signed = sign(card, KEY_ONE, "ES256", "fixture-signer-one")
        add(
            f"check_signed_{name}_breaks_the_signature",
            "a2a_card_check",
            {
                "pin": pin_of(card, PIN_ONE),
                "card": with_(signed, mutate),
                "fetched_from": ORIGIN_URL,
                "keys": KEYS,
                "egress": [HOST],
            },
        )
        add(
            f"check_signed_{name}_resigned_is_a_changed_card",
            "a2a_card_check",
            {
                "pin": pin_of(card, PIN_ONE),
                "card": sign(changed, KEY_ONE, "ES256", "fixture-signer-one"),
                "fetched_from": ORIGIN_URL,
                "keys": KEYS,
                "egress": [HOST],
            },
        )

    def check(name: str, **change: Any) -> None:
        base = {
            "pin": pin_of(EXTENDED),
            "card": EXTENDED,
            "fetched_from": ORIGIN_URL,
            "keys": KEYS,
            "egress": [HOST],
        }
        add(f"check_{name}", "a2a_card_check", {**base, **change})

    check("unsigned_matches")
    check(
        "unsigned_key_order_and_origin_spelling_do_not_matter",
        card=json.loads(json.dumps(EXTENDED, sort_keys=True)),
        fetched_from="https://AGENT.example.org:443/other",
    )
    check("unsigned_version_changed", card=CHANGED)
    check(
        "unsigned_origin_host_changed",
        fetched_from="https://evil.example.net/.well-known/agent-card.json",
    )
    check(
        "unsigned_origin_port_changed", fetched_from="https://agent.example.org:8443/x"
    )
    check("unsigned_origin_http", fetched_from="http://agent.example.org/x")
    check("unsigned_pin_card_now_signed_still_matches", card=SIGNED_ONE)
    check("signed_pinned_matches", pin=pin_of(EXTENDED, PIN_ONE), card=SIGNED_ONE)
    check("signed_new_signer", pin=pin_of(EXTENDED, PIN_ONE), card=SIGNED_TWO)
    check("signed_signature_removed", pin=pin_of(EXTENDED, PIN_ONE), card=EXTENDED)
    check("signed_tampered", pin=pin_of(EXTENDED, PIN_ONE), card=changed_signed)
    check(
        "signed_sdk_form_misses_empty_entries",
        pin=pin_of(API_KEY, PIN_ONE),
        card=sdk_only,
    )
    check(
        "signed_by_pinned_signer_but_card_changed",
        pin=pin_of(EXTENDED, PIN_ONE),
        card=resigned,
    )
    check(
        "signed_order_origin_before_signature",
        pin=pin_of(EXTENDED, PIN_ONE),
        card=SIGNED_TWO,
        fetched_from="https://evil.example.net/x",
    )
    check(
        "signed_order_signature_before_card_change",
        pin=pin_of(CHANGED, PIN_ONE),
        card=SIGNED_TWO,
    )
    check(
        "signed_order_card_change_before_interface",
        pin=pin_of(CHANGED),
        card=EXTENDED,
        egress=[],
    )
    check(
        "card_refused_before_origin",
        card=load("malformed", "api_key_no_name.json")["card"],
        fetched_from="https://evil.example.net/x",
    )
    check(
        "interface_host_listed_with_port",
        card=with_(
            EXTENDED,
            lambda c: c.update(
                supportedInterfaces=[{"url": "https://agent.example.org:8443/a2a/v1"}]
            ),
        ),
        pin=pin_of(
            with_(
                EXTENDED,
                lambda c: c.update(
                    supportedInterfaces=[
                        {"url": "https://agent.example.org:8443/a2a/v1"}
                    ]
                ),
            )
        ),
        egress=["agent.example.org:8443"],
    )
    check("interface_host_not_in_egress", egress=["other.example.org"])
    check(
        "interface_port_not_in_egress",
        card=with_(
            EXTENDED,
            lambda c: c.update(
                supportedInterfaces=[{"url": "https://agent.example.org:8443/a2a/v1"}]
            ),
        ),
        pin=pin_of(
            with_(
                EXTENDED,
                lambda c: c.update(
                    supportedInterfaces=[
                        {"url": "https://agent.example.org:8443/a2a/v1"}
                    ]
                ),
            )
        ),
    )
    check(
        "interface_ip_literal",
        card=with_(
            EXTENDED,
            lambda c: c.update(
                supportedInterfaces=[{"url": "https://203.0.113.9/a2a/v1"}]
            ),
        ),
        pin=pin_of(
            with_(
                EXTENDED,
                lambda c: c.update(
                    supportedInterfaces=[{"url": "https://203.0.113.9/a2a/v1"}]
                ),
            )
        ),
        egress=["203.0.113.9"],
    )
    check(
        "interface_second_host_not_in_egress",
        card=with_(
            EXTENDED,
            lambda c: c.update(
                supportedInterfaces=[
                    {"url": "https://agent.example.org/a2a/v1"},
                    {"url": "https://mirror.example.org/a2a/v1"},
                ]
            ),
        ),
        pin=pin_of(
            with_(
                EXTENDED,
                lambda c: c.update(
                    supportedInterfaces=[
                        {"url": "https://agent.example.org/a2a/v1"},
                        {"url": "https://mirror.example.org/a2a/v1"},
                    ]
                ),
            )
        ),
    )
    check("pin_with_extra_field", pin={**pin_of(EXTENDED), "approved": True})
    check("pin_hash_not_an_id", pin={**pin_of(EXTENDED), "hash": "abc"})
    check(
        "pin_missing_signer",
        pin={"hash": pin_of(EXTENDED)["hash"], "origin": "https://" + HOST},
    )
    check("pin_not_an_object", pin=None)

    extended_card = with_(
        EXTENDED,
        lambda c: c["skills"].append(
            {
                "id": "admin",
                "name": "Admin",
                "description": "Admin skill.",
                "tags": ["a"],
            }
        ),
    )
    for name, pinned, card in (
        ("pinned_matches", a2a_card.card_digest(extended_card), extended_card),
        ("not_yet_reviewed", None, extended_card),
        ("changed_after_review", a2a_card.card_digest(EXTENDED), extended_card),
        (
            "card_refused",
            a2a_card.card_digest(EXTENDED),
            load("malformed", "api_key_no_name.json")["card"],
        ),
    ):
        add(
            f"extended_{name}",
            "a2a_card_extended",
            {"card": card, "extended_hash": pinned},
        )

    for state in (
        "TASK_STATE_AUTH_REQUIRED",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_FAILED",
        "TASK_STATE_UNSPECIFIED",
        "auth_required",
    ):
        for credential in (False, True):
            add(
                f"task_state_{state.removeprefix('TASK_STATE_').lower() if state.startswith('TASK_STATE_') else 'unprefixed_' + state}_{'with' if credential else 'without'}_credential",
                "a2a_task_state",
                {"state": state, "has_credential": credential},
            )

    return {
        "format": "astral.vectors/v1",
        "name": "a2a-card-v1",
        "spec": SPEC,
        "cases": cases,
    }


def render() -> dict[str, str]:
    return {
        "a2a-card-v1": json.dumps(
            generate(), indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n"
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = render()
    differs = [
        name
        for name, content in rendered.items()
        if not (VECTORS / f"{name}.json").exists()
        or (VECTORS / f"{name}.json").read_text(encoding="utf-8") != content
    ]
    if args.check:
        if differs:
            raise SystemExit(", ".join(differs) + " vectors differ")
        return
    for name, content in rendered.items():
        (VECTORS / f"{name}.json").write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
