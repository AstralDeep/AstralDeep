"""A loopback A2A agent for one security scheme that serves a signed or unsigned agent card, an extended card, JSON-RPC calls and a small OAuth endpoint set.
It is built on `loopback.py` like the OAuth fixture, and `test_a2a_fixtures.py` drives it against the contract's a2a-card-v1.json vectors and the node tests reuse it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .loopback import Loopback, Request, Response

API_KEY = "api_key"
HTTP_BEARER = "http_bearer"
HTTP_BASIC = "http_basic"
OAUTH_CODE = "oauth2_authorization_code"
OAUTH_DEVICE = "oauth2_device_code"
OPENID = "openid_connect"
SCHEMES = (API_KEY, HTTP_BEARER, HTTP_BASIC, OAUTH_CODE, OAUTH_DEVICE, OPENID)
OAUTH_SCHEMES = (OAUTH_CODE, OAUTH_DEVICE, OPENID)

CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/a2a/v1"
OIDC_PATH = "/.well-known/openid-configuration"
AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SEND_METHODS = ("SendMessage", "message/send")
TASK_METHODS = ("GetTask", "tasks/get")
EXTENDED_METHODS = ("GetExtendedAgentCard", "agent/getAuthenticatedExtendedCard")
API_KEY_PARAMETER = {"header": "X-Api-Key", "query": "api_key", "cookie": "session_key"}
AUTH_KEYS = frozenset({"securitySchemes", "securityRequirements", "security"})
PRESENCE_BOOLS = frozenset({"streaming", "pushNotifications", "extendedAgentCard"})
SIGNER_SEEDS = {"one": "test-signer-1", "two": "test-signer-2", "ed": "a2a-card-v1/ed25519"}
SIGNER_KIDS = {"one": "fixture-signer-one", "two": "fixture-signer-two", "ed": "fixture-ed"}
THUMBPRINT_MEMBERS = {"EC": ("crv", "kty", "x", "y"), "OKP": ("crv", "kty", "x"), "RSA": ("e", "kty", "n")}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _prune(value: Any, key: str = "", keep_containers: bool = False) -> Any:
    if isinstance(value, float):
        if value != int(value):
            raise ValueError("The card holds a non-integer number.")
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


def canonical_card(card: dict[str, Any]) -> bytes:
    body = {key: value for key, value in card.items() if key != "signatures"}
    return _jcs(_prune(body) or {}).encode("utf-8")


def card_digest(card: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_card(card)).hexdigest()


def jwk_thumbprint(jwk: dict[str, Any]) -> str:
    members = THUMBPRINT_MEMBERS.get(jwk.get("kty"))
    if members is None or any(type(jwk.get(m)) is not str for m in members):
        raise ValueError("The key is not a usable public JWK.")
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"))
    return b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def _private_key(signer: str) -> Any:
    digest = hashlib.sha256(SIGNER_SEEDS[signer].encode()).digest()
    if signer == "ed":
        return ed25519.Ed25519PrivateKey.from_private_bytes(digest)
    return ec.derive_private_key(int.from_bytes(digest, "big") % (2**255) + 1, ec.SECP256R1())


def signer_jwk(signer: str) -> dict[str, Any]:
    key = _private_key(signer)
    kid = SIGNER_KIDS[signer]
    if signer == "ed":
        return {"crv": "Ed25519", "kid": kid, "kty": "OKP", "x": b64url(key.public_key().public_bytes_raw())}
    numbers = key.public_key().public_numbers()
    return {
        "crv": "P-256",
        "kid": kid,
        "kty": "EC",
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }


def signature_entry(card: dict[str, Any], signer: str, payload: bytes | None = None) -> dict[str, str]:
    alg = "EdDSA" if signer == "ed" else "ES256"
    protected = b64url(json.dumps({"alg": alg, "kid": SIGNER_KIDS[signer], "typ": "JOSE"}, separators=(",", ":")).encode())
    message = f"{protected}.{b64url(payload if payload is not None else canonical_card(card))}".encode()
    key = _private_key(signer)
    if signer == "ed":
        raw = key.sign(message)
    else:
        r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256(), deterministic_signing=True)))
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return {"protected": protected, "signature": b64url(raw)}


def signed(card: dict[str, Any], signer: str) -> dict[str, Any]:
    body = {key: value for key, value in card.items() if key != "signatures"}
    return {**body, "signatures": [signature_entry(body, signer)]}


@dataclass
class AgentConfig:
    signer: str = "one"
    api_key_location: str = "header"
    extended_card: bool = True
    auth_required_after: int | None = None
    unauthenticated_task: bool = False
    forced_state: str | None = None
    tamper_card: bool = False
    interface_url: str | None = None
    card_version: str = "1.0.0"
    host_name: str = "127.0.0.1"

    @staticmethod
    def from_json(raw: object) -> AgentConfig:
        if not isinstance(raw, dict):
            raise ValueError("an agent config must be an object")
        known = {item.name for item in fields(AgentConfig)}
        if set(raw) - known:
            raise ValueError(f"unknown agent config {sorted(set(raw) - known)}")
        config = AgentConfig(**raw)
        if config.signer not in SIGNER_SEEDS:
            raise ValueError("signer must be one, two or ed")
        if config.api_key_location not in API_KEY_PARAMETER:
            raise ValueError("api_key_location must be header, query or cookie")
        return config


@dataclass
class Call:
    method: str
    authorized: bool
    state: str | None = None


@dataclass
class Issued:
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    bearer: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    basic_user: str = "fixture-user"
    basic_password: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    access_tokens: list[str] = field(default_factory=list)
    refresh_tokens: list[str] = field(default_factory=list)


class FixtureAgent:
    def __init__(
        self,
        scheme: str,
        signed: bool = False,
        config: AgentConfig | None = None,
        tls: tuple[str, str] | None = None,
    ) -> None:
        if scheme not in SCHEMES:
            raise ValueError(f"scheme must be one of {SCHEMES}")
        self.scheme = scheme
        self.signed = signed
        self.config = config or AgentConfig()
        self.issued = Issued()
        self.card_fetches = 0
        self.extended_fetches = 0
        self.calls: list[Call] = []
        self.refusals: list[str] = []
        self.authorize_requests: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.device_approved = False
        self._codes: dict[str, dict[str, str]] = {}
        self._device_code = secrets.token_urlsafe(16)
        self._sends = 0
        self._lock = threading.Lock()
        self._server = Loopback(self._handle, tls)
        self.origin = self._server.url.replace("127.0.0.1", self.config.host_name, 1)

    @staticmethod
    def from_json(raw: object, tls: tuple[str, str] | None = None) -> FixtureAgent:
        if not isinstance(raw, dict) or set(raw) - {"scheme", "signed", "config"}:
            raise ValueError("an agent spec holds scheme, signed and config")
        return FixtureAgent(str(raw.get("scheme")), bool(raw.get("signed", False)), AgentConfig.from_json(raw.get("config", {})), tls)

    def __enter__(self) -> FixtureAgent:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._server.stop()

    @property
    def card_url(self) -> str:
        return self.origin + CARD_PATH

    @property
    def rpc_url(self) -> str:
        return self.origin + RPC_PATH

    @property
    def public_key(self) -> dict[str, Any]:
        return signer_jwk(self.config.signer)

    @property
    def thumbprint(self) -> str:
        return jwk_thumbprint(self.public_key)

    @property
    def sends(self) -> int:
        return self._sends

    def redefine(self, **changes: Any) -> None:
        for name, value in changes.items():
            if not hasattr(self.config, name):
                raise AttributeError(name)
            setattr(self.config, name, value)

    def approve_device(self) -> None:
        self.device_approved = True

    def complete_task(self, task_id: str) -> None:
        with self._lock:
            self.tasks[task_id]["status"] = {"state": COMPLETED}

    def credential_headers(self) -> dict[str, str]:
        match self.scheme:
            case "api_key":
                return self._api_key_headers()
            case "http_bearer":
                return {"authorization": f"Bearer {self.issued.bearer}"}
            case "http_basic":
                pair = f"{self.issued.basic_user}:{self.issued.basic_password}".encode()
                return {"authorization": "Basic " + base64.b64encode(pair).decode()}
        return {"authorization": f"Bearer {self.issued.access_tokens[-1]}"} if self.issued.access_tokens else {}

    def _api_key_headers(self) -> dict[str, str]:
        location = self.config.api_key_location
        if location == "header":
            return {"x-api-key": self.issued.api_key}
        if location == "cookie":
            return {"cookie": f"{API_KEY_PARAMETER['cookie']}={self.issued.api_key}"}
        return {}

    @property
    def rpc_target(self) -> str:
        if self.scheme == API_KEY and self.config.api_key_location == "query":
            return f"{self.rpc_url}?{urlencode({API_KEY_PARAMETER['query']: self.issued.api_key})}"
        return self.rpc_url

    def card(self, extended: bool = False) -> dict[str, Any]:
        card = self._base_card(extended)
        if not self.signed:
            return card
        signature = signature_entry(card, self.config.signer)
        if self.config.tamper_card:
            card["name"] = "Tampered " + card["name"]
        return {**card, "signatures": [signature]}

    def extended_digest(self) -> str:
        return card_digest(self.card(extended=True))

    def _scheme_block(self) -> tuple[dict[str, Any], str, list[str]]:
        o = self.origin
        match self.scheme:
            case "api_key":
                block = {"apiKeySecurityScheme": {"location": self.config.api_key_location, "name": API_KEY_PARAMETER[self.config.api_key_location]}}
                return {"key": block}, "key", []
            case "http_bearer":
                return {"tok": {"httpAuthSecurityScheme": {"scheme": "bearer"}}}, "tok", []
            case "http_basic":
                return {"basic": {"httpAuthSecurityScheme": {"scheme": "basic"}}}, "basic", []
            case "oauth2_authorization_code":
                flow = {"authorizationUrl": f"{o}/oauth/authorize", "tokenUrl": f"{o}/oauth/token", "refreshUrl": f"{o}/oauth/token", "scopes": {"read": "Read"}, "pkceRequired": True}
                return {"oauth": {"oauth2SecurityScheme": {"flows": {"authorizationCode": flow}}}}, "oauth", ["read"]
            case "oauth2_device_code":
                flow = {"deviceAuthorizationUrl": f"{o}/oauth/device", "tokenUrl": f"{o}/oauth/token", "scopes": {"read": "Read"}}
                return {"oauth": {"oauth2SecurityScheme": {"flows": {"deviceCode": flow}}}}, "oauth", ["read"]
        return {"oidc": {"openIdConnectSecurityScheme": {"openIdConnectUrl": o + OIDC_PATH}}}, "oidc", ["openid", "profile"]

    def _base_card(self, extended: bool) -> dict[str, Any]:
        schemes, name, scopes = self._scheme_block()
        skills = [{"id": "echo", "name": "Echo", "description": "Echoes text.", "tags": ["echo"]}]
        capabilities: dict[str, Any] = {"streaming": True}
        if self.config.extended_card:
            capabilities["extendedAgentCard"] = True
        if extended:
            skills.append({"id": "admin", "name": "Admin", "description": "Admin skill.", "tags": ["a"]})
        return {
            "name": "Fixture Agent",
            "description": "Fixture card for the reader tests.",
            "version": self.config.card_version,
            "supportedInterfaces": [{"url": self.config.interface_url or self.rpc_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            "capabilities": capabilities,
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": skills,
            "securitySchemes": schemes,
            "securityRequirements": [{"schemes": {name: {"list": scopes}}}],
        }

    def _handle(self, request: Request) -> Response:
        parts = urlsplit(request.target)
        path, query = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        if request.method == "GET" and path == CARD_PATH:
            with self._lock:
                self.card_fetches += 1
            return Response.of_json(self.card())
        if request.method == "GET" and path == OIDC_PATH and self.scheme == OPENID:
            return Response.of_json(self._oidc_metadata())
        if request.method == "GET" and path == "/oauth/authorize" and self.scheme in (OAUTH_CODE, OPENID):
            return self._authorize(query)
        if request.method == "POST" and path == "/oauth/device" and self.scheme in (OAUTH_DEVICE, OPENID):
            return self._device()
        if request.method == "POST" and path == "/oauth/token" and self.scheme in OAUTH_SCHEMES:
            return self._token({k: v[0] for k, v in parse_qs(request.body.decode()).items()})
        if request.method == "POST" and path == RPC_PATH:
            return self._rpc(request, query)
        return Response.of_json({"error": "not_found"}, 404)

    def _oidc_metadata(self) -> dict[str, Any]:
        o = self.origin
        return {
            "issuer": o,
            "authorization_endpoint": f"{o}/oauth/authorize",
            "token_endpoint": f"{o}/oauth/token",
            "device_authorization_endpoint": f"{o}/oauth/device",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["openid", "profile"],
        }

    def _authorize(self, query: dict[str, str]) -> Response:
        with self._lock:
            self.authorize_requests.append(query)
        redirect = query.get("redirect_uri", "")
        if query.get("response_type") != "code" or not redirect or query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
            return Response.of_json({"error": "invalid_request"}, 400)
        code = secrets.token_urlsafe(16)
        with self._lock:
            self._codes[code] = {"challenge": query["code_challenge"], "redirect_uri": redirect}
        location = redirect + ("&" if "?" in redirect else "?") + urlencode({"code": code, "state": query.get("state", ""), "iss": self.origin})
        return Response(302, [("location", location)])

    def _device(self) -> Response:
        return Response.of_json(
            {"device_code": self._device_code, "user_code": "FIXT-0001", "verification_uri": self.origin + "/device", "expires_in": 600, "interval": 1}
        )

    def _token(self, form: dict[str, str]) -> Response:
        with self._lock:
            self.token_requests.append(form)
            grant = form.get("grant_type")
            if grant == "authorization_code":
                entry = self._codes.pop(form.get("code", ""), None)
                challenge = b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                if entry is None or entry["challenge"] != challenge or entry["redirect_uri"] != form.get("redirect_uri"):
                    return Response.of_json({"error": "invalid_grant"}, 400)
            elif grant == DEVICE_GRANT:
                if form.get("device_code") != self._device_code:
                    return Response.of_json({"error": "invalid_grant"}, 400)
                if not self.device_approved:
                    return Response.of_json({"error": "authorization_pending"}, 400)
            elif grant == "refresh_token":
                if form.get("refresh_token") not in self.issued.refresh_tokens:
                    return Response.of_json({"error": "invalid_grant"}, 400)
            else:
                return Response.of_json({"error": "unsupported_grant_type"}, 400)
            access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            self.issued.access_tokens.append(access)
            self.issued.refresh_tokens.append(refresh)
        return Response.of_json({"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh, "scope": "read"})

    def _credential_state(self, request: Request, query: dict[str, str]) -> str:
        headers = request.headers
        match self.scheme:
            case "api_key":
                location = self.config.api_key_location
                if location == "header":
                    given = headers.get("x-api-key")
                elif location == "query":
                    given = query.get(API_KEY_PARAMETER["query"])
                else:
                    cookies = dict(item.split("=", 1) for item in headers.get("cookie", "").split("; ") if "=" in item)
                    given = cookies.get(API_KEY_PARAMETER["cookie"])
                expected = self.issued.api_key
            case "http_basic":
                word, _, token = headers.get("authorization", "").partition(" ")
                given = token if word.lower() == "basic" else None
                expected = base64.b64encode(f"{self.issued.basic_user}:{self.issued.basic_password}".encode()).decode()
            case _:
                word, _, token = headers.get("authorization", "").partition(" ")
                given = token if word.lower() == "bearer" else None
                if self.scheme == HTTP_BEARER:
                    expected = self.issued.bearer
                else:
                    expected = given if given in self.issued.access_tokens else ""
        if not given:
            return "missing"
        return "ok" if expected and secrets.compare_digest(given.encode(), expected.encode()) else "wrong"

    def _challenge(self) -> str:
        return {"http_basic": 'Basic realm="fixture"', "api_key": 'ApiKey realm="fixture"'}.get(self.scheme, 'Bearer realm="fixture"')

    def _rpc(self, request: Request, query: dict[str, str]) -> Response:
        body = request.json()
        method, rpc_id = str(body.get("method", "")), body.get("id")
        credential = self._credential_state(request, query)
        if credential != "ok" and not (self.config.unauthenticated_task and method in SEND_METHODS):
            with self._lock:
                self.refusals.append(credential)
                self.calls.append(Call(method, False))
            return Response.of_json({"error": "unauthorized"}, 401, [("www-authenticate", self._challenge())])
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        if method in SEND_METHODS:
            return self._send(rpc_id, params, credential == "ok")
        if method in TASK_METHODS:
            task = self.tasks.get(str(params.get("id", "")))
            with self._lock:
                self.calls.append(Call(method, True, task["status"]["state"] if task else None))
            return self._result(rpc_id, {"task": task}) if task else self._error(rpc_id, -32001, "Task not found")
        if method in EXTENDED_METHODS:
            with self._lock:
                self.calls.append(Call(method, True))
                self.extended_fetches += 1
            if not self.config.extended_card:
                return self._error(rpc_id, -32007, "Extended card not configured")
            return self._result(rpc_id, self.card(extended=True))
        with self._lock:
            self.calls.append(Call(method, True))
        return self._error(rpc_id, -32601, "Method not found")

    def _send(self, rpc_id: object, params: dict[str, Any], authorized: bool) -> Response:
        message = params.get("message") if isinstance(params.get("message"), dict) else {}
        text = "".join(str(part.get("text", "")) for part in message.get("parts", []) if isinstance(part, dict))
        with self._lock:
            auth_after = self.config.auth_required_after
            state = self.config.forced_state or COMPLETED
            if not authorized or (auth_after is not None and self._sends >= auth_after):
                state = AUTH_REQUIRED
            if authorized:
                self._sends += 1
            task_id = secrets.token_urlsafe(8)
            task = {"id": task_id, "contextId": "fixture-context", "status": {"state": state}}
            if state == COMPLETED:
                task["artifacts"] = [{"artifactId": "echo", "parts": [{"text": text}]}]
            self.tasks[task_id] = task
            self.calls.append(Call("SendMessage", authorized, state))
        return self._result(rpc_id, {"task": copy.deepcopy(task)})

    @staticmethod
    def _result(rpc_id: object, result: object) -> Response:
        return Response.of_json({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    @staticmethod
    def _error(rpc_id: object, code: int, message: str) -> Response:
        return Response.of_json({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})
