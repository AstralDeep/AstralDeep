"""Reads an A2A agent card from a parsed JSON object into the sign-in each requirement needs, verifies a signed card against a pinned signer and pins an unsigned card by hash and origin.
It touches no network, so the spike tests and the later kernel adapter port feed it cards loaded from files.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import jwt

ALGORITHMS = ("EdDSA", "ES256", "ES384", "ES512", "RS256", "RS384", "RS512", "PS256", "PS384", "PS512")
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

SUPPORTED_KINDS = frozenset({
    API_KEY, HTTP_BEARER, HTTP_BASIC, OAUTH_AUTHORIZATION_CODE, OAUTH_DEVICE_CODE, OPENID_CONNECT,
})
BROWSER_KINDS = frozenset({OAUTH_AUTHORIZATION_CODE, OPENID_CONNECT})

_V1_VARIANTS = {
    "apiKeySecurityScheme": "apiKey",
    "httpAuthSecurityScheme": "http",
    "oauth2SecurityScheme": "oauth2",
    "openIdConnectSecurityScheme": "openIdConnect",
    "mtlsSecurityScheme": "mutualTLS",
}
_FLOW_KINDS = {
    "authorizationCode": OAUTH_AUTHORIZATION_CODE,
    "deviceCode": OAUTH_DEVICE_CODE,
    "clientCredentials": OAUTH_CLIENT_CREDENTIALS,
    "implicit": OAUTH_DEPRECATED,
    "password": OAUTH_DEPRECATED,
}


class CardRefusal(Exception):
    def __init__(self, code: str, reason: str):
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

    @property
    def supported(self) -> bool:
        return self.kind in SUPPORTED_KINDS

    @property
    def needs_browser(self) -> bool:
        return self.kind in BROWSER_KINDS

    def urls(self) -> tuple[str, ...]:
        found = [self.discovery_url, self.metadata_url]
        for flow in self.flows:
            found += [flow.authorization_url, flow.token_url, flow.device_authorization_url, flow.refresh_url]
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

    def usable_alternatives(self) -> tuple[Requirement, ...]:
        return tuple(
            alt for alt in self.alternatives
            if all(self.schemes[name].supported for name in alt.scheme_names)
        )

    def auth_hosts(self) -> tuple[str, ...]:
        hosts = {urlsplit(url).hostname for scheme in self.schemes.values() for url in scheme.urls()}
        return tuple(sorted(host for host in hosts if host))


def _refuse(code: str, reason: str) -> CardRefusal:
    return CardRefusal(code, reason)


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
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise _refuse("insecure_url", f"{where} must be a credential-free HTTPS URL.")
    return url


def _scopes(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if type(value) is dict:
        return tuple(sorted(value))
    raise _refuse("wrong_type", f"{where} must be an object mapping scope to description.")


def _flow(kind_key: str, body: Any, where: str) -> Flow:
    if type(body) is not dict:
        raise _refuse("wrong_type", f"{where} must be an object.")
    kind = _FLOW_KINDS[kind_key]
    pkce = body.get("pkceRequired", False)
    if type(pkce) is not bool:
        raise _refuse("wrong_type", f"{where}.pkceRequired must be true or false.")
    return Flow(
        kind=kind,
        authorization_url=_https(
            body.get("authorizationUrl"), f"{where}.authorizationUrl", required=kind == OAUTH_AUTHORIZATION_CODE
        ),
        token_url=_https(body.get("tokenUrl"), f"{where}.tokenUrl", required=kind != OAUTH_DEPRECATED),
        device_authorization_url=_https(
            body.get("deviceAuthorizationUrl"), f"{where}.deviceAuthorizationUrl", required=kind == OAUTH_DEVICE_CODE
        ),
        refresh_url=_https(body.get("refreshUrl"), f"{where}.refreshUrl", required=False),
        scopes=_scopes(body.get("scopes"), f"{where}.scopes"),
        pkce_required=pkce,
    )


def _scheme(name: str, raw: Any) -> tuple[Scheme, str]:
    where = f"securitySchemes.{name}"
    if type(raw) is not dict:
        raise _refuse("wrong_type", f"{where} must be an object.")
    v1 = [key for key in raw if key in _V1_VARIANTS]
    if v1:
        if len(v1) != 1 or len(raw) != 1:
            raise _refuse("ambiguous_scheme", f"{where} must hold exactly one scheme variant.")
        variant, body, dialect = _V1_VARIANTS[v1[0]], raw[v1[0]], "v1"
    elif "type" in raw:
        variant, body, dialect = raw["type"], raw, "v0.3"
    else:
        raise _refuse("unknown_scheme_type", f"{where} names no known scheme variant.")
    if type(body) is not dict:
        raise _refuse("wrong_type", f"{where} body must be an object.")
    if variant == "apiKey":
        location = body.get("location", body.get("in"))
        if location not in ("header", "query", "cookie"):
            raise _refuse("bad_api_key_location", f"{where} location must be header, query or cookie.")
        return Scheme(name, API_KEY, location=location, parameter=_text(body.get("name"), f"{where}.name", required=True)), dialect
    if variant == "http":
        http_scheme = _text(body.get("scheme"), f"{where}.scheme", required=True)
        kind = {"bearer": HTTP_BEARER, "basic": HTTP_BASIC}.get(http_scheme.lower(), HTTP_OTHER)
        return Scheme(name, kind, http_scheme=http_scheme, bearer_format=_text(body.get("bearerFormat"), f"{where}.bearerFormat")), dialect
    if variant == "oauth2":
        flows_raw = body.get("flows")
        if type(flows_raw) is not dict or not flows_raw:
            raise _refuse("missing_field", f"{where}.flows is required.")
        unknown = [key for key in flows_raw if key not in _FLOW_KINDS]
        if unknown:
            raise _refuse("unknown_flow", f"{where}.flows names unknown flow {unknown[0]}.")
        if dialect == "v1" and len(flows_raw) != 1:
            raise _refuse("ambiguous_scheme", f"{where}.flows must hold exactly one flow.")
        flows = tuple(_flow(key, flows_raw[key], f"{where}.flows.{key}") for key in sorted(flows_raw))
        return Scheme(name, flows[0].kind, flows=flows, metadata_url=_https(body.get("oauth2MetadataUrl"), f"{where}.oauth2MetadataUrl", required=False)), dialect
    if variant == "openIdConnect":
        return Scheme(name, OPENID_CONNECT, discovery_url=_https(body.get("openIdConnectUrl"), f"{where}.openIdConnectUrl")), dialect
    if variant == "mutualTLS":
        return Scheme(name, MUTUAL_TLS), dialect
    raise _refuse("unknown_scheme_type", f"{where} has unknown type {variant!r}.")


def _requirement(entry: Any, where: str, schemes: dict[str, Scheme]) -> Requirement:
    if type(entry) is not dict:
        raise _refuse("wrong_type", f"{where} must be an object.")
    mapping = entry["schemes"] if set(entry) == {"schemes"} and type(entry["schemes"]) is dict else entry
    pairs = []
    for name in sorted(mapping):
        scopes = mapping[name]
        if type(scopes) is dict and set(scopes) <= {"list"}:
            scopes = scopes.get("list", [])
        if type(scopes) is not list or any(type(item) is not str for item in scopes):
            raise _refuse("wrong_type", f"{where}.{name} must be a list of scope strings.")
        if name not in schemes:
            raise _refuse("requirement_unknown_scheme", f"{where} names {name!r}, which securitySchemes does not define.")
        pairs.append((name, tuple(scopes)))
    if not pairs:
        raise _refuse("empty_requirement", f"{where} names no scheme.")
    return Requirement(tuple(pairs))


def _requirements(value: Any, where: str, schemes: dict[str, Scheme]) -> tuple[Requirement, ...]:
    if value is None:
        return ()
    if type(value) is not list:
        raise _refuse("wrong_type", f"{where} must be an array.")
    return tuple(_requirement(entry, f"{where}[{index}]", schemes) for index, entry in enumerate(value))


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
        raise _refuse("conflicting_requirements", "securityRequirements and security disagree.")
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
            skill_requirements[str(skill.get("id", index))] = _requirements(skill[key], f"skills[{index}].{key}", schemes)
    capabilities = card.get("capabilities") or {}
    if type(capabilities) is not dict:
        raise _refuse("wrong_type", "capabilities must be an object.")
    extended = capabilities.get("extendedAgentCard", card.get("supportsAuthenticatedExtendedCard", False))
    if type(extended) is not bool:
        raise _refuse("wrong_type", "The extended card flag must be true or false.")
    interfaces = card.get("supportedInterfaces") or ([{"url": card["url"]}] if card.get("url") else [])
    if type(interfaces) is not list or any(type(item) is not dict for item in interfaces):
        raise _refuse("wrong_type", "supportedInterfaces must be an array of objects.")
    urls = tuple(_https(item.get("url"), "supportedInterfaces[].url") for item in interfaces)
    signatures = card.get("signatures") or []
    if type(signatures) is not list:
        raise _refuse("wrong_type", "signatures must be an array.")
    flags = []
    if alternatives and not any(all(schemes[n].supported for n in alt.scheme_names) for alt in alternatives):
        flags.append("no_supported_alternative")
    if any(flow.kind == OAUTH_AUTHORIZATION_CODE and not flow.pkce_required for s in schemes.values() for flow in s.flows):
        flags.append("pkce_not_declared_client_enforces")
    if not alternatives:
        flags.append("no_requirements_declared")
    return CardReading(
        name=name, schemes=schemes, alternatives=alternatives, skill_requirements=skill_requirements,
        extended_card=extended, interface_urls=urls, signature_count=len(signatures),
        dialect=dialects.pop() if dialects else "none", flags=tuple(flags),
    )


def _prune(value: Any, key: str = "", keep_containers: bool = False) -> Any:
    if isinstance(value, float):
        if value != int(value):
            raise _refuse("float_in_card", "The card holds a non-integer number, which this reader cannot canonicalize.")
        return int(value)
    if isinstance(value, dict):
        keep = keep_containers or key in AUTH_KEYS
        kept = {k: _prune(v, k, keep) for k, v in value.items()}
        kept = {k: v for k, v in kept.items() if v is not None}
        return kept if kept or keep else None
    if isinstance(value, list):
        keep = keep_containers or key in AUTH_KEYS
        items = [item for item in (_prune(v, "", keep) for v in value) if item is not None]
        return items if items or keep else None
    if value == "" or value is None:
        return None
    if value is False and key not in PRESENCE_BOOLS:
        return None
    return value


def _jcs(value: Any) -> str:
    if isinstance(value, dict):
        members = sorted(value.items(), key=lambda item: item[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _jcs(v) for k, v in members) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def canonical_card(card: dict[str, Any], *, sdk_lenient: bool = False) -> bytes:
    body = {key: value for key, value in card.items() if key != "signatures"}
    if sdk_lenient:
        return _jcs(_prune_all(body) or {}).encode("utf-8")
    return _jcs(_prune(body) or {}).encode("utf-8")


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


def card_digest(card: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_card(card)).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    members = {"EC": ("crv", "kty", "x", "y"), "OKP": ("crv", "kty", "x"), "RSA": ("e", "kty", "n")}.get(jwk.get("kty"))
    if members is None or any(type(jwk.get(m)) is not str for m in members):
        raise _refuse("bad_key", "The key is not a usable public JWK.")
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"))
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


@dataclass(frozen=True)
class SignerPin:
    thumbprint: str
    kid: str = ""


@dataclass(frozen=True)
class SignatureResult:
    status: str
    thumbprint: str = ""
    kid: str = ""
    digest: str = ""
    detail: str = ""
    covers_requirements: bool = True

    @property
    def accepted(self) -> bool:
        return self.status == "verified_pinned" and self.covers_requirements


def verify_signed(card: dict[str, Any], keys: list[dict[str, Any]], pin: SignerPin | None) -> SignatureResult:
    signatures = card.get("signatures") or []
    if not signatures:
        return SignatureResult("unsigned")
    payloads = [(_b64url(canonical_card(card)), True), (_b64url(canonical_card(card, sdk_lenient=True)), False)]
    failures = []
    for signature in signatures:
        try:
            header = json.loads(_b64url_decode(signature["protected"]))
            alg, kid = header["alg"], header["kid"]
            if alg not in ALGORITHMS or type(kid) is not str:
                raise ValueError("algorithm or key id not allowed")
            candidates = [k for k in keys if k.get("kid") in (None, kid) and k.get("kty") in ("EC", "OKP", "RSA")]
            for payload, strict in payloads:
                for jwk in candidates:
                    token = f"{signature['protected']}.{payload}.{signature['signature']}"
                    try:
                        jwt.PyJWS().decode(token, jwt.PyJWK.from_dict(jwk), algorithms=[alg])
                    except jwt.PyJWTError:
                        continue
                    thumbprint = jwk_thumbprint(jwk)
                    pinned = pin is not None and pin.thumbprint == thumbprint
                    detail = "" if pinned else "The signer is not the pinned signer."
                    if not strict:
                        detail = (detail + " The signature does not cover empty requirement entries.").strip()
                    return SignatureResult(
                        "verified_pinned" if pinned else "verified_new_signer", thumbprint, kid, card_digest(card), detail, strict
                    )
            failures.append("no supplied key verifies the signature")
        except (KeyError, ValueError, TypeError) as error:
            failures.append(f"malformed signature ({type(error).__name__})")
    return SignatureResult("invalid", detail="; ".join(failures))


@dataclass(frozen=True)
class UnsignedPin:
    origin: str
    digest: str


def https_origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _refuse("insecure_url", "The card origin must be an HTTPS URL.")
    port = f":{parts.port}" if parts.port not in (None, 443) else ""
    return f"https://{parts.hostname.lower()}{port}"


def pin_unsigned(card: dict[str, Any], fetched_from: str) -> UnsignedPin:
    return UnsignedPin(https_origin(fetched_from), card_digest(card))


def check_unsigned(pin: UnsignedPin, card: dict[str, Any], fetched_from: str) -> str:
    if https_origin(fetched_from) != pin.origin:
        return "origin_changed"
    return "match" if card_digest(card) == pin.digest else "card_changed"


def changed_paths(old: Any, new: Any, prefix: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        paths: list[str] = []
        for key in sorted(set(old) | set(new)):
            paths += changed_paths(old.get(key), new.get(key), f"{prefix}.{key}" if prefix else key)
        return paths
    return [] if old == new else [prefix]
