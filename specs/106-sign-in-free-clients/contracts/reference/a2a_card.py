"""Reference for A2A agent cards: the reader, canonical form and hash, JWS signature check against a pinned signer, the unsigned pin, the extended card pin and the interface egress check.
It touches no network and reads cards already parsed from JSON, and vectors/a2a-card-v1.json pins it for the Kernel and every other implementation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import connection
from astral_mesh_ref import crypto
from astral_mesh_ref.cj import b64url_decode, b64url_encode
from astral_mesh_ref.errors import MeshError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

ALGORITHMS = ("ES256", "EdDSA", "RS256")
MIN_RSA_BITS = 2048
AUTH_KEYS = frozenset({"securitySchemes", "securityRequirements", "security"})
PRESENCE_BOOLS = frozenset({"streaming", "pushNotifications", "extendedAgentCard"})

API_KEY = "api_key"
HTTP_BEARER = "http_bearer"
HTTP_BASIC = "http_basic"
HTTP_OTHER = "http_other"
OAUTH_AUTHORIZATION_CODE = "oauth2_authorization_code"
OAUTH_DEVICE_CODE = "oauth2_device_code"
OAUTH_CLIENT_CREDENTIALS = "oauth2_client_credentials"
OAUTH_DEPRECATED = "oauth2_deprecated"
OPENID_CONNECT = "openid_connect"
MUTUAL_TLS = "mutual_tls"

SUPPORTED_KINDS = frozenset(
    {
        API_KEY,
        HTTP_BEARER,
        HTTP_BASIC,
        OAUTH_AUTHORIZATION_CODE,
        OAUTH_DEVICE_CODE,
        OPENID_CONNECT,
    }
)
NODE_ONLY_KINDS = frozenset({OAUTH_DEVICE_CODE})

V1_VARIANTS = {
    "apiKeySecurityScheme": "apiKey",
    "httpAuthSecurityScheme": "http",
    "oauth2SecurityScheme": "oauth2",
    "openIdConnectSecurityScheme": "openIdConnect",
    "mtlsSecurityScheme": "mutualTLS",
}
FLOW_KINDS = {
    "authorizationCode": OAUTH_AUTHORIZATION_CODE,
    "deviceCode": OAUTH_DEVICE_CODE,
    "clientCredentials": OAUTH_CLIENT_CREDENTIALS,
    "implicit": OAUTH_DEPRECATED,
    "password": OAUTH_DEPRECATED,
}
READ_CODES = (
    "a2a.not_an_object",
    "a2a.missing_field",
    "a2a.wrong_type",
    "a2a.insecure_url",
    "a2a.ambiguous_scheme",
    "a2a.unknown_scheme_type",
    "a2a.bad_api_key_location",
    "a2a.unknown_flow",
    "a2a.requirement_unknown_scheme",
    "a2a.empty_requirement",
    "a2a.conflicting_requirements",
    "a2a.mixed_dialects",
    "a2a.float_in_card",
    "a2a.bad_key",
)
CHECK_CODES = (
    "a2a.scheme_unsupported",
    "a2a.signature_invalid",
    "a2a.signature_missing",
    "a2a.signature_scope",
    "a2a.signer_changed",
    "a2a.origin_changed",
    "a2a.card_changed",
    "a2a.interface_offsite",
    "a2a.extended_unreviewed",
    "a2a.extended_changed",
    "a2a.not_a_card_pin",
)
TASK_CODES = ("connection.sign_in_needed",)
CODES = READ_CODES + CHECK_CODES + TASK_CODES
TASK_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
THUMBPRINT_MEMBERS = {
    "EC": ("crv", "kty", "x", "y"),
    "OKP": ("crv", "kty", "x"),
    "RSA": ("e", "kty", "n"),
}


class CardRefusal(Exception):
    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Flow:
    kind: str
    authorization_url: str = ""
    token_url: str = ""
    device_authorization_url: str = ""
    refresh_url: str = ""
    scopes: tuple[str, ...] = ()
    pkce_required: bool = False


@dataclass(frozen=True)
class Scheme:
    name: str
    kind: str
    location: str = ""
    parameter: str = ""
    http_scheme: str = ""
    bearer_format: str = ""
    discovery_url: str = ""
    metadata_url: str = ""
    flows: tuple[Flow, ...] = ()

    def urls(self) -> tuple[str, ...]:
        found = [self.discovery_url, self.metadata_url]
        for flow in self.flows:
            found += [
                flow.authorization_url,
                flow.token_url,
                flow.device_authorization_url,
                flow.refresh_url,
            ]
        return tuple(url for url in found if url)


@dataclass(frozen=True)
class Requirement:
    scopes_by_scheme: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def scheme_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.scopes_by_scheme)


@dataclass(frozen=True)
class CardReading:
    name: str
    schemes: dict[str, Scheme]
    alternatives: tuple[Requirement, ...]
    skill_requirements: dict[str, tuple[Requirement, ...]]
    extended_card: bool
    interface_urls: tuple[str, ...]
    signature_count: int
    dialect: str
    flags: tuple[str, ...] = field(default=())

    def usable(self, node: bool) -> tuple[int, ...]:
        def scheme_usable(name: str) -> bool:
            kind = self.schemes[name].kind
            return kind in SUPPORTED_KINDS and (node or kind not in NODE_ONLY_KINDS)

        return tuple(
            index
            for index, alt in enumerate(self.alternatives)
            if all(scheme_usable(name) for name in alt.scheme_names)
        )

    def auth_hosts(self) -> tuple[str, ...]:
        hosts = {
            urlsplit(url).hostname
            for scheme in self.schemes.values()
            for url in scheme.urls()
        }
        return tuple(sorted(host for host in hosts if host))


def _refuse(code: str, reason: str) -> CardRefusal:
    return CardRefusal("a2a." + code, reason)


def _text(value: Any, where: str, *, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise _refuse("missing_field", f"{where} is required.")
        return ""
    if type(value) is not str:
        raise _refuse("wrong_type", f"{where} must be a string.")
    return value


def _https(value: Any, where: str, *, required: bool = True) -> str:
    url = _text(value, where, required=required)
    if not url:
        return ""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise _refuse("insecure_url", f"{where} must be a credential-free HTTPS URL.")
    return url


def _scopes(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if type(value) is dict:
        return tuple(sorted(value))
    raise _refuse("wrong_type", f"{where} must be an object mapping scope to text.")


def _flow(kind_key: str, body: Any, where: str) -> Flow:
    if type(body) is not dict:
        raise _refuse("wrong_type", f"{where} must be an object.")
    kind = FLOW_KINDS[kind_key]
    pkce = body.get("pkceRequired", False)
    if type(pkce) is not bool:
        raise _refuse("wrong_type", f"{where}.pkceRequired must be true or false.")
    return Flow(
        kind=kind,
        authorization_url=_https(
            body.get("authorizationUrl"),
            f"{where}.authorizationUrl",
            required=kind == OAUTH_AUTHORIZATION_CODE,
        ),
        token_url=_https(
            body.get("tokenUrl"),
            f"{where}.tokenUrl",
            required=kind != OAUTH_DEPRECATED,
        ),
        device_authorization_url=_https(
            body.get("deviceAuthorizationUrl"),
            f"{where}.deviceAuthorizationUrl",
            required=kind == OAUTH_DEVICE_CODE,
        ),
        refresh_url=_https(
            body.get("refreshUrl"), f"{where}.refreshUrl", required=False
        ),
        scopes=_scopes(body.get("scopes"), f"{where}.scopes"),
        pkce_required=pkce,
    )


def _scheme(name: str, raw: Any) -> tuple[Scheme, str]:
    where = f"securitySchemes.{name}"
    if type(raw) is not dict:
        raise _refuse("wrong_type", f"{where} must be an object.")
    v1 = [key for key in raw if key in V1_VARIANTS]
    if v1:
        if len(v1) != 1 or len(raw) != 1:
            raise _refuse(
                "ambiguous_scheme", f"{where} must hold exactly one scheme variant."
            )
        variant, body, dialect = V1_VARIANTS[v1[0]], raw[v1[0]], "v1"
    elif "type" in raw:
        variant, body, dialect = raw["type"], raw, "v0.3"
    else:
        raise _refuse("unknown_scheme_type", f"{where} names no known scheme variant.")
    if type(body) is not dict:
        raise _refuse("wrong_type", f"{where} body must be an object.")
    if variant == "apiKey":
        location = body.get("location", body.get("in"))
        if location not in ("header", "query", "cookie"):
            raise _refuse(
                "bad_api_key_location",
                f"{where} location must be header, query or cookie.",
            )
        parameter = _text(body.get("name"), f"{where}.name", required=True)
        return Scheme(name, API_KEY, location=location, parameter=parameter), dialect
    if variant == "http":
        http_scheme = _text(body.get("scheme"), f"{where}.scheme", required=True)
        kind = {"bearer": HTTP_BEARER, "basic": HTTP_BASIC}.get(
            http_scheme.lower(), HTTP_OTHER
        )
        bearer_format = _text(body.get("bearerFormat"), f"{where}.bearerFormat")
        return Scheme(
            name, kind, http_scheme=http_scheme, bearer_format=bearer_format
        ), dialect
    if variant == "oauth2":
        flows_raw = body.get("flows")
        if type(flows_raw) is not dict or not flows_raw:
            raise _refuse("missing_field", f"{where}.flows is required.")
        unknown = [key for key in flows_raw if key not in FLOW_KINDS]
        if unknown:
            raise _refuse("unknown_flow", f"{where}.flows names unknown flow.")
        if dialect == "v1" and len(flows_raw) != 1:
            raise _refuse(
                "ambiguous_scheme", f"{where}.flows must hold exactly one flow."
            )
        flows = tuple(
            _flow(key, flows_raw[key], f"{where}.flows.{key}")
            for key in sorted(flows_raw)
        )
        metadata = _https(
            body.get("oauth2MetadataUrl"), f"{where}.oauth2MetadataUrl", required=False
        )
        return Scheme(name, flows[0].kind, flows=flows, metadata_url=metadata), dialect
    if variant == "openIdConnect":
        discovery = _https(body.get("openIdConnectUrl"), f"{where}.openIdConnectUrl")
        return Scheme(name, OPENID_CONNECT, discovery_url=discovery), dialect
    if variant == "mutualTLS":
        return Scheme(name, MUTUAL_TLS), dialect
    raise _refuse("unknown_scheme_type", f"{where} has an unknown type.")


def _requirement(entry: Any, where: str, schemes: dict[str, Scheme]) -> Requirement:
    if type(entry) is not dict:
        raise _refuse("wrong_type", f"{where} must be an object.")
    mapping = (
        entry["schemes"]
        if set(entry) == {"schemes"} and type(entry["schemes"]) is dict
        else entry
    )
    pairs = []
    for name in sorted(mapping):
        scopes = mapping[name]
        if type(scopes) is dict and set(scopes) <= {"list"}:
            scopes = scopes.get("list", [])
        if type(scopes) is not list or any(type(item) is not str for item in scopes):
            raise _refuse("wrong_type", f"{where}.{name} must be a list of scopes.")
        if name not in schemes:
            raise _refuse(
                "requirement_unknown_scheme",
                f"{where} names a scheme securitySchemes does not define.",
            )
        pairs.append((name, tuple(scopes)))
    if not pairs:
        raise _refuse("empty_requirement", f"{where} names no scheme.")
    return Requirement(tuple(pairs))


def _requirements(
    value: Any, where: str, schemes: dict[str, Scheme]
) -> tuple[Requirement, ...]:
    if value is None:
        return ()
    if type(value) is not list:
        raise _refuse("wrong_type", f"{where} must be an array.")
    return tuple(
        _requirement(entry, f"{where}[{index}]", schemes)
        for index, entry in enumerate(value)
    )


def read_card(card: Any) -> CardReading:
    if type(card) is not dict:
        raise _refuse("not_an_object", "The agent card must be a JSON object.")
    name = _text(card.get("name"), "name", required=True)
    raw_schemes = card.get("securitySchemes", {})
    if type(raw_schemes) is not dict:
        raise _refuse("wrong_type", "securitySchemes must be an object.")
    schemes: dict[str, Scheme] = {}
    dialects = set()
    for scheme_name, raw in raw_schemes.items():
        schemes[scheme_name], dialect = _scheme(scheme_name, raw)
        dialects.add(dialect)
    if len(dialects) > 1:
        raise _refuse("mixed_dialects", "securitySchemes mixes the 1.0 and 0.3 shapes.")
    present = [key for key in ("securityRequirements", "security") if key in card]
    parsed = [_requirements(card[key], key, schemes) for key in present]
    if len(parsed) == 2 and parsed[0] != parsed[1]:
        raise _refuse(
            "conflicting_requirements", "securityRequirements and security disagree."
        )
    alternatives = parsed[0] if parsed else ()
    skills = card.get("skills") or []
    if type(skills) is not list:
        raise _refuse("wrong_type", "skills must be an array.")
    skill_requirements: dict[str, tuple[Requirement, ...]] = {}
    for index, skill in enumerate(skills):
        if type(skill) is not dict:
            raise _refuse("wrong_type", f"skills[{index}] must be an object.")
        key = "securityRequirements" if "securityRequirements" in skill else "security"
        if key in skill:
            skill_requirements[str(skill.get("id", index))] = _requirements(
                skill[key], f"skills[{index}].{key}", schemes
            )
    capabilities = card.get("capabilities") or {}
    if type(capabilities) is not dict:
        raise _refuse("wrong_type", "capabilities must be an object.")
    extended = capabilities.get(
        "extendedAgentCard", card.get("supportsAuthenticatedExtendedCard", False)
    )
    if type(extended) is not bool:
        raise _refuse("wrong_type", "The extended card flag must be true or false.")
    interfaces = card.get("supportedInterfaces") or (
        [{"url": card["url"]}] if card.get("url") else []
    )
    if type(interfaces) is not list or any(
        type(item) is not dict for item in interfaces
    ):
        raise _refuse("wrong_type", "supportedInterfaces must be an array of objects.")
    urls = tuple(
        _https(item.get("url"), "supportedInterfaces[].url") for item in interfaces
    )
    signatures = card.get("signatures") or []
    if type(signatures) is not list:
        raise _refuse("wrong_type", "signatures must be an array.")
    reading = CardReading(
        name=name,
        schemes=schemes,
        alternatives=alternatives,
        skill_requirements=skill_requirements,
        extended_card=extended,
        interface_urls=urls,
        signature_count=len(signatures),
        dialect=next(iter(dialects)) if dialects else "none",
    )
    flags = []
    if alternatives and not reading.usable(True):
        flags.append("no_supported_alternative")
    if any(
        flow.kind == OAUTH_AUTHORIZATION_CODE and not flow.pkce_required
        for s in schemes.values()
        for flow in s.flows
    ):
        flags.append("pkce_not_declared_client_enforces")
    if not alternatives:
        flags.append("no_requirements_declared")
    return CardReading(**{**reading.__dict__, "flags": tuple(flags)})


def _prune(value: Any, key: str = "", keep_containers: bool = False) -> Any:
    if isinstance(value, float):
        if value != int(value):
            raise _refuse("float_in_card", "The card holds a non-integer number.")
        return int(value)
    if isinstance(value, dict):
        keep = keep_containers or key in AUTH_KEYS
        kept = {k: _prune(v, k, keep) for k, v in value.items()}
        kept = {k: v for k, v in kept.items() if v is not None}
        return kept if kept or keep else None
    if isinstance(value, list):
        keep = keep_containers or key in AUTH_KEYS
        items = [
            item for item in (_prune(v, "", keep) for v in value) if item is not None
        ]
        return items if items or keep else None
    if value == "" or value is None:
        return None
    if value is False and key not in PRESENCE_BOOLS:
        return None
    return value


def _prune_all(value: Any) -> Any:
    if isinstance(value, dict):
        kept = {k: _prune_all(v) for k, v in value.items()}
        kept = {k: v for k, v in kept.items() if v is not None}
        return kept or None
    if isinstance(value, list):
        items = [item for item in (_prune_all(v) for v in value) if item is not None]
        return items or None
    if isinstance(value, float):
        return _prune(value)
    return None if value == "" or value is None else value


def _jcs(value: Any) -> str:
    if isinstance(value, dict):
        members = sorted(value.items(), key=lambda item: item[0].encode("utf-16-be"))
        return (
            "{"
            + ",".join(
                json.dumps(k, ensure_ascii=False) + ":" + _jcs(v) for k, v in members
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def canonical_card(card: dict[str, Any], *, sdk_lenient: bool = False) -> bytes:
    body = {key: value for key, value in card.items() if key != "signatures"}
    pruned = _prune_all(body) if sdk_lenient else _prune(body)
    return _jcs(pruned or {}).encode("utf-8")


def card_digest(card: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_card(card)).hexdigest()


def tool_hash(card: dict[str, Any]) -> str:
    pruned = _prune(
        {
            "description": card.get("description"),
            "name": card.get("name"),
            "skills": card.get("skills"),
        }
    )
    return crypto.object_id(
        {"description": "", "name": "", "skills": [], **(pruned or {})}
    )


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    members = THUMBPRINT_MEMBERS.get(jwk.get("kty"))
    if members is None or any(type(jwk.get(m)) is not str for m in members):
        raise _refuse("bad_key", "The key is not a usable public JWK.")
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"))
    return b64url_encode(hashlib.sha256(canonical.encode("ascii")).digest())


def _integer(text: str) -> int:
    return int.from_bytes(b64url_decode(text), "big")


def _public_key(jwk: dict[str, Any], alg: str) -> Any:
    kty = jwk.get("kty")
    if alg == "ES256" and kty == "EC" and jwk.get("crv") == "P-256":
        return ec.EllipticCurvePublicNumbers(
            _integer(jwk["x"]), _integer(jwk["y"]), ec.SECP256R1()
        ).public_key()
    if alg == "EdDSA" and kty == "OKP" and jwk.get("crv") == "Ed25519":
        return ed25519.Ed25519PublicKey.from_public_bytes(b64url_decode(jwk["x"]))
    if alg == "RS256" and kty == "RSA":
        key = rsa.RSAPublicNumbers(_integer(jwk["e"]), _integer(jwk["n"])).public_key()
        if key.key_size < MIN_RSA_BITS:
            raise ValueError("rsa key too small")
        return key
    raise ValueError("key does not fit the algorithm")


def _verifies(
    alg: str, jwk: dict[str, Any], signing_input: bytes, signature: bytes
) -> bool:
    try:
        key = _public_key(jwk, alg)
        if alg == "ES256":
            if len(signature) != 64:
                return False
            der = encode_dss_signature(
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:], "big"),
            )
            key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        elif alg == "EdDSA":
            key.verify(signature, signing_input)
        else:
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, KeyError, TypeError, MeshError):
        return False
    return True


@dataclass(frozen=True)
class SignatureResult:
    status: str
    thumbprint: str = ""
    kid: str = ""
    alg: str = ""
    digest: str = ""
    covers_requirements: bool = True

    @property
    def accepted(self) -> bool:
        return self.status == "verified_pinned" and self.covers_requirements


def _requirement_view(canonical: bytes) -> dict[str, Any]:
    card = json.loads(canonical)
    skills = card.get("skills") or []
    return {
        "card": {key: card.get(key) for key in sorted(AUTH_KEYS)},
        "skills": [
            {key: skill.get(key) for key in sorted(AUTH_KEYS)}
            for skill in skills
            if isinstance(skill, dict)
        ],
    }


def _covers_requirements(card: dict[str, Any]) -> bool:
    return _requirement_view(canonical_card(card)) == _requirement_view(
        canonical_card(card, sdk_lenient=True)
    )


def verify_signed(
    card: dict[str, Any], keys: list[dict[str, Any]], pin: str | None
) -> SignatureResult:
    signatures = card.get("signatures") or []
    if not signatures:
        return SignatureResult("unsigned")
    payloads = [
        (b64url_encode(canonical_card(card)), True),
        (b64url_encode(canonical_card(card, sdk_lenient=True)), False),
    ]
    for signature in signatures:
        try:
            header = json.loads(b64url_decode(signature["protected"]))
            alg, kid = header["alg"], header["kid"]
            raw = b64url_decode(signature["signature"])
            if alg not in ALGORITHMS or type(kid) is not str:
                continue
        except (KeyError, ValueError, TypeError, MeshError):
            continue
        candidates = [
            k
            for k in keys
            if isinstance(k, dict)
            and k.get("kid") in (None, kid)
            and k.get("kty") in THUMBPRINT_MEMBERS
        ]
        for payload, strict in payloads:
            signing_input = f"{signature['protected']}.{payload}".encode("ascii")
            for jwk in candidates:
                if _verifies(alg, jwk, signing_input, raw):
                    thumbprint = jwk_thumbprint(jwk)
                    return SignatureResult(
                        "verified_pinned"
                        if thumbprint == pin
                        else "verified_new_signer",
                        thumbprint,
                        kid,
                        alg,
                        card_digest(card),
                        strict or _covers_requirements(card),
                    )
    return SignatureResult("invalid")


def https_origin(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise _refuse("insecure_url", "The card origin must be an HTTPS URL.")
    port = f":{parts.port}" if parts.port not in (None, 443) else ""
    return f"https://{parts.hostname.lower()}{port}"


def interface_entry(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not connection.dns_host(host):
        return None
    port = parts.port or 443
    return host if port == 443 else f"{host}:{port}"


def select_alternative(reading: CardReading, node: bool) -> dict[str, Any]:
    if not reading.alternatives:
        return {"alternative": None, "schemes": []}
    usable = reading.usable(node)
    if not usable:
        raise CardRefusal("a2a.scheme_unsupported", "no usable alternative")
    chosen = reading.alternatives[usable[0]]
    return {
        "alternative": usable[0],
        "schemes": [[n, list(scopes)] for n, scopes in chosen.scopes_by_scheme],
    }


def _signature_gate(
    card: dict[str, Any], keys: list[dict[str, Any]], pin: str | None
) -> SignatureResult:
    result = verify_signed(card, keys, pin)
    if result.status == "invalid":
        raise CardRefusal("a2a.signature_invalid")
    if result.status != "unsigned" and not result.covers_requirements:
        raise CardRefusal("a2a.signature_scope")
    return result


def propose(
    card: dict[str, Any], fetched_from: str, keys: list[dict[str, Any]]
) -> dict[str, Any]:
    read_card(card)
    origin = https_origin(fetched_from)
    result = _signature_gate(card, keys, None)
    return {
        "hash": card_digest(card),
        "origin": origin,
        "signer": result.thumbprint if result.status != "unsigned" else None,
        "signed": result.status != "unsigned",
    }


def check(case_input: dict[str, Any]) -> dict[str, Any]:
    pin, card = case_input["pin"], case_input["card"]
    if not (
        isinstance(pin, dict)
        and set(pin) == {"hash", "origin", "signer"}
        and crypto.is_id(pin["hash"])
    ):
        raise CardRefusal("a2a.not_a_card_pin")
    reading = read_card(card)
    if https_origin(case_input["fetched_from"]) != pin["origin"]:
        raise CardRefusal("a2a.origin_changed")
    if pin["signer"] is not None:
        result = _signature_gate(card, case_input["keys"], pin["signer"])
        if result.status == "unsigned":
            raise CardRefusal("a2a.signature_missing")
        if result.status == "verified_new_signer":
            raise CardRefusal("a2a.signer_changed")
    if card_digest(card) != pin["hash"]:
        raise CardRefusal("a2a.card_changed")
    for url in reading.interface_urls:
        entry = interface_entry(url)
        if entry is None or entry not in case_input["egress"]:
            raise CardRefusal("a2a.interface_offsite", url)
    return {"hash": pin["hash"], "signed": pin["signer"] is not None}


def check_extended(case_input: dict[str, Any]) -> dict[str, Any]:
    read_card(case_input["card"])
    pinned = case_input["extended_hash"]
    if pinned is None:
        raise CardRefusal("a2a.extended_unreviewed")
    if card_digest(case_input["card"]) != pinned:
        raise CardRefusal("a2a.extended_changed")
    return {"hash": pinned}


def task_state(state: str, has_credential: bool) -> dict[str, Any]:
    if state != TASK_AUTH_REQUIRED:
        return {"action": "continue"}
    return {
        "action": "fail",
        "error": "connection.sign_in_needed",
        "state": "needs_attention",
        "reason": "refused" if has_credential else "not_signed_in",
        "sends": False,
    }


def summary(card: dict[str, Any]) -> dict[str, Any]:
    reading = read_card(card)
    return {
        "dialect": reading.dialect,
        "name": reading.name,
        "schemes": {n: s.kind for n, s in sorted(reading.schemes.items())},
        "alternatives": [
            [[n, list(scopes)] for n, scopes in alt.scopes_by_scheme]
            for alt in reading.alternatives
        ],
        "usable_device": list(reading.usable(False)),
        "usable_node": list(reading.usable(True)),
        "skill_requirements": {
            skill: [
                [[n, list(scopes)] for n, scopes in alt.scopes_by_scheme]
                for alt in alts
            ]
            for skill, alts in sorted(reading.skill_requirements.items())
        },
        "extended_card": reading.extended_card,
        "interface_urls": list(reading.interface_urls),
        "auth_hosts": list(reading.auth_hosts()),
        "signature_count": reading.signature_count,
        "flags": list(reading.flags),
        "digest": card_digest(card),
        "tool_hash": tool_hash(card),
    }


def verify_summary(case_input: dict[str, Any]) -> dict[str, Any]:
    result = verify_signed(case_input["card"], case_input["keys"], case_input["pin"])
    return {
        "status": result.status,
        "accepted": result.accepted,
        "covers_requirements": result.covers_requirements,
        "thumbprint": result.thumbprint,
        "kid": result.kid,
        "alg": result.alg,
        "digest": result.digest,
    }


def _guard(call: Any) -> dict[str, Any]:
    try:
        return {"ok": call()}
    except CardRefusal as error:
        return {"error": error.code}


def run(kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
    calls = {
        "a2a_card_read": lambda: summary(case_input["card"]),
        "a2a_card_canonical": lambda: {
            "canonical": canonical_card(case_input["card"]).decode("utf-8"),
            "sdk": canonical_card(case_input["card"], sdk_lenient=True).decode("utf-8"),
            "digest": card_digest(case_input["card"]),
        },
        "a2a_card_select": lambda: select_alternative(
            read_card(case_input["card"]), case_input["node"]
        ),
        "a2a_card_verify": lambda: verify_summary(case_input),
        "a2a_card_propose": lambda: propose(
            case_input["card"], case_input["fetched_from"], case_input["keys"]
        ),
        "a2a_card_check": lambda: check(case_input),
        "a2a_card_extended": lambda: check_extended(case_input),
        "a2a_task_state": lambda: task_state(
            case_input["state"], case_input["has_credential"]
        ),
        "a2a_thumbprint": lambda: {"thumbprint": jwk_thumbprint(case_input["jwk"])},
    }
    return _guard(calls[kind])
