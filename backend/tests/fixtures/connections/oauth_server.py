"""Loopback authorization server and MCP resource server for connection tests, ported from phase-0/mcp-oauth/fixture.py at commit 933f14c3 and kept in step with the Kernel's Rust OAuth fixture.
Each `FixtureConfig` switch turns one server behavior on or off, `GET /authorize` and `FixtureAuth.approve` play the browser, and every request and issued secret is recorded so tests can prove none leaks.
It differs from the Rust twin in one way: by default MCP calls must carry the 2026-07-28 `_meta` key and `MCP-Protocol-Version` header, which every correct client sends, and `require_protocol_meta` turns that off.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as stdlib_secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from enum import Enum
from urllib.parse import parse_qs, urlencode, urlsplit

from .loopback import Loopback, Request, Response
from .mcp_server import McpEndpoint, ToolDefinition

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
WRONG_ISSUER = "https://attacker.example"
RESOURCE_PATH = "/.well-known/oauth-protected-resource"
USER_CODE = "WDJB-MJHT"


class Challenge(Enum):
    RESOURCE_METADATA = "resource_metadata"
    TWO_LINES = "two_lines"
    BARE = "bare"
    ABSENT = "absent"


class DeviceStep(Enum):
    PENDING = "authorization_pending"
    SLOW_DOWN = "slow_down"
    DENIED = "access_denied"
    EXPIRED = "expired_token"
    APPROVE = "approve"


class FixtureRefusal(Exception):
    pass


@dataclass
class FixtureConfig:
    open: bool = False
    challenge: Challenge = Challenge.RESOURCE_METADATA
    challenge_lines: tuple[str, ...] | None = None
    challenge_scope: str | None = "files:read"
    resource_metadata_url: str | None = None
    resource: str | None = None
    scopes_supported: list[str] | None = None
    wellknown_at: str = "path"
    pkce_methods: list[str] | None = field(default_factory=lambda: ["S256"])
    registration_endpoint: bool = True
    cimd_supported: bool = False
    cimd_documents: dict[str, list[str]] = field(default_factory=dict)
    preregistered_client_ids: tuple[str, ...] = ()
    metadata_at: str = "oauth"
    issuer_path: str = ""
    metadata_issuer: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_url: str | None = None
    device_url: str | None = None
    advertise_iss: bool = True
    iss_mode: str = "correct"
    iss_override: tuple[str, ...] | None = None
    extra_authorization_servers: tuple[str, ...] = ()
    reject_native_https_registration: bool = False
    register_script: list[tuple[int, dict] | None] = field(default_factory=list)
    registration_auth: str | None = None
    device_endpoint: bool = False
    device_steps: list[DeviceStep] = field(default_factory=lambda: [DeviceStep.APPROVE])
    refuse_refresh: bool = False
    rotate_refresh: bool = False
    omit_refresh: bool = False
    fail_tools: bool = False
    forbid_scope: str | None = None
    redirect_to: str | None = None
    reject_bearers: bool = False
    require_protocol_meta: bool = True
    canned: dict[str, tuple[int, object]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, document: dict) -> FixtureConfig:
        if not isinstance(document, dict):
            raise ValueError("the config must be a JSON object")
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(document) - known)
        if unknown:
            raise ValueError(f"unknown fixture switches: {', '.join(unknown)}")
        values = dict(document)
        if "challenge" in values:
            values["challenge"] = Challenge(values["challenge"])
        if "device_steps" in values:
            values["device_steps"] = [DeviceStep(item) for item in values["device_steps"]]
        for name in ("preregistered_client_ids", "extra_authorization_servers", "iss_override", "challenge_lines"):
            if values.get(name) is not None:
                values[name] = tuple(values[name])
        if "register_script" in values:
            values["register_script"] = [None if item is None else (item[0], item[1]) for item in values["register_script"]]
        if "canned" in values:
            values["canned"] = {path: (item[0], item[1]) for path, item in values["canned"].items()}
        return cls(**values)


@dataclass(frozen=True)
class Urls:
    issuer: str
    as_origin: str
    rs_origin: str
    rs_url: str

    @property
    def egress(self) -> list[str]:
        return sorted(
            (origin.split("://", 1)[1] for origin in (self.as_origin, self.rs_origin)),
            key=lambda entry: int(entry.rsplit(":", 1)[1]),
        )


def _mint() -> str:
    return stdlib_secrets.token_hex(24)


def _form(text: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(text, keep_blank_values=True).items()}


def _error(status: int, code: str) -> Response:
    return Response.of_json({"error": code}, status)


class FixtureAuth:
    def __init__(
        self,
        config: FixtureConfig | None = None,
        tools: list[ToolDefinition] | None = None,
        tls: tuple[str, str] | None = None,
    ) -> None:
        self.config = config or FixtureConfig()
        self.mcp = McpEndpoint(tools)
        self.requests: list[tuple[str, str]] = []
        self.authorize_requests: list[dict[str, str]] = []
        self.registrations: list[dict] = []
        self.token_requests: list[dict[str, str]] = []
        self.device_requests: list[dict[str, str]] = []
        self.secrets: list[str] = []
        self._codes: dict[str, dict[str, str]] = {}
        self._clients: dict[str, list[str]] = {}
        self._client_secrets: dict[str, str] = {}
        self._access: dict[str, str] = {}
        self._refresh: dict[str, str] = {}
        self._device_codes: dict[str, str] = {}
        self._device_step = 0
        self._lock = threading.RLock()
        self._tls = tls
        self._servers: list[Loopback] = []
        self.urls: Urls | None = None

    def start(self) -> Urls:
        as_server = Loopback(lambda req: self._locked(self._authorization_server, req), self._tls)
        rs_server = Loopback(lambda req: self._locked(self._resource_server, req), self._tls)
        self._servers = [as_server, rs_server]
        self.urls = Urls(
            as_server.url + self.config.issuer_path, as_server.url, rs_server.url, rs_server.url + "/mcp"
        )
        return self.urls

    def stop(self) -> None:
        for server in self._servers:
            server.stop()
        self._servers = []

    def __enter__(self) -> FixtureAuth:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _locked(self, handler: Callable[[Request], Response], req: Request) -> Response:
        with self._lock:
            return handler(req)

    @property
    def issuer(self) -> str:
        assert self.urls is not None
        return self.urls.issuer

    @property
    def rs_url(self) -> str:
        assert self.urls is not None
        return self.urls.rs_url

    def configure(self, change: Callable[[FixtureConfig], None]) -> None:
        with self._lock:
            change(self.config)

    def redefine_tool(self, name: str, **changes: object) -> None:
        with self._lock:
            self.mcp.redefine(name, **changes)

    def expire_access_tokens(self) -> None:
        with self._lock:
            self._access.clear()

    def revoke_clients(self) -> None:
        with self._lock:
            self._clients.clear()

    def stop_resource_server(self) -> None:
        if len(self._servers) > 1:
            self._servers.pop().stop()

    def accepts(self, access_token: str) -> bool:
        with self._lock:
            return self._access.get(access_token) == self.rs_url

    def requests_to(self, role: str) -> list[str]:
        with self._lock:
            return [line for who, line in self.requests if who == role]

    def approve(self, authorization_url: str) -> str:
        with self._lock:
            prefix = f"{self.urls.as_origin}/authorize?"
            if not authorization_url.startswith(prefix):
                raise FixtureRefusal("not the authorization endpoint")
            self.requests.append(("as", "GET /authorize"))
            return self._authorize(_form(authorization_url[len(prefix):]))

    def _authorize(self, params: dict[str, str]) -> str:
        self.authorize_requests.append(params)
        if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256":
            raise FixtureRefusal("unsupported request")
        if not params.get("code_challenge") or not params.get("resource"):
            raise FixtureRefusal("missing code_challenge or resource")
        client_id, redirect_uri = params.get("client_id", ""), params.get("redirect_uri", "")
        if client_id.startswith("https://"):
            allowed = self.config.cimd_documents.get(client_id) if self.config.cimd_supported else None
            if allowed is None:
                raise FixtureRefusal("invalid client metadata document")
        elif client_id in self.config.preregistered_client_ids:
            allowed = [redirect_uri]
        else:
            allowed = self._clients.get(client_id, [])
        if redirect_uri not in allowed:
            raise FixtureRefusal("redirect_uri not registered")
        code = _mint()
        self.secrets.append(code)
        self._codes[code] = {
            "challenge": params["code_challenge"],
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": params["resource"],
        }
        pairs = [("code", code), ("state", params.get("state", ""))]
        pairs.extend(("iss", value) for value in self._iss_values())
        return f"{redirect_uri}?{urlencode(pairs)}"

    def _iss_values(self) -> list[str]:
        if self.config.iss_override is not None:
            return list(self.config.iss_override)
        return {"correct": [self.issuer], "wrong": [WRONG_ISSUER]}.get(self.config.iss_mode, [])

    def _metadata_path(self) -> str:
        prefix = self.config.issuer_path
        return {
            "oauth": f"/.well-known/oauth-authorization-server{prefix}",
            "openid": f"/.well-known/openid-configuration{prefix}",
            "openid_append": f"{prefix}/.well-known/openid-configuration",
        }[self.config.metadata_at]

    def _metadata(self) -> dict:
        cfg, origin = self.config, self.urls.as_origin
        meta: dict = {
            "issuer": cfg.metadata_issuer or self.issuer,
            "authorization_endpoint": cfg.authorization_endpoint or f"{origin}/authorize",
            "token_endpoint": cfg.token_endpoint or f"{origin}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token", DEVICE_GRANT],
            "token_endpoint_auth_methods_supported": ["none"],
        }
        if cfg.pkce_methods is not None:
            meta["code_challenge_methods_supported"] = cfg.pkce_methods
        if cfg.registration_endpoint:
            meta["registration_endpoint"] = cfg.registration_url or f"{origin}/register"
        if cfg.cimd_supported:
            meta["client_id_metadata_document_supported"] = True
        if cfg.advertise_iss:
            meta["authorization_response_iss_parameter_supported"] = True
        if cfg.device_endpoint:
            meta["device_authorization_endpoint"] = cfg.device_url or f"{origin}/device"
        return meta

    def _resource_metadata(self) -> dict:
        servers = [self.issuer, *self.config.extra_authorization_servers]
        meta: dict = {"resource": self.config.resource or self.rs_url, "authorization_servers": servers}
        if self.config.scopes_supported is not None:
            meta["scopes_supported"] = self.config.scopes_supported
        return meta

    def _issue(self, resource: str, refresh: bool) -> dict:
        access = _mint()
        self.secrets.append(access)
        self._access[access] = resource
        body: dict = {"access_token": access, "token_type": "Bearer", "expires_in": 3600}
        if refresh and not self.config.omit_refresh:
            token = _mint()
            self.secrets.append(token)
            self._refresh[token] = resource
            body["refresh_token"] = token
        return body

    def _client(self, form: dict[str, str], authorization: str | None) -> str | None:
        if authorization and authorization.startswith("Basic "):
            try:
                pair = base64.b64decode(authorization[6:]).decode()
            except ValueError:
                return None
            if ":" not in pair:
                return None
            raw_id, raw_secret = pair.split(":", 1)
            client_id = parse_qs(f"v={raw_id}")["v"][0] if raw_id else ""
            secret: str | None = parse_qs(f"v={raw_secret}")["v"][0] if raw_secret else ""
        else:
            client_id, secret = form.get("client_id", ""), form.get("client_secret")
        expected = self._client_secrets.get(client_id)
        if expected is not None and secret != expected:
            return None
        return client_id

    def _token(self, form: dict[str, str], authorization: str | None) -> Response:
        self.token_requests.append(dict(form))
        if form.get("code_verifier"):
            self.secrets.append(form["code_verifier"])
        client_id = self._client(form, authorization)
        if client_id is None or (client_id.startswith("dyn-") and client_id not in self._clients):
            return _error(401, "invalid_client")
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            grant = self._codes.pop(form.get("code", ""), None)
            if grant is None:
                return _error(400, "invalid_grant")
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            valid = (
                challenge == grant["challenge"]
                and client_id == grant["client_id"]
                and form.get("redirect_uri") == grant["redirect_uri"]
                and form.get("resource") == grant["resource"]
            )
            return Response.of_json(self._issue(grant["resource"], True)) if valid else _error(400, "invalid_grant")
        if grant_type == "refresh_token":
            token, resource = form.get("refresh_token", ""), form.get("resource", "")
            if self._refresh.get(token) != resource or self.config.refuse_refresh:
                return _error(400, "invalid_grant")
            if self.config.rotate_refresh:
                del self._refresh[token]
            return Response.of_json(self._issue(resource, self.config.rotate_refresh))
        if grant_type == DEVICE_GRANT:
            resource = self._device_codes.get(form.get("device_code", ""))
            if resource is None:
                return _error(400, "invalid_grant")
            steps = self.config.device_steps
            step = steps[min(self._device_step, len(steps) - 1)]
            self._device_step += 1
            if step is DeviceStep.APPROVE:
                return Response.of_json(self._issue(resource, True))
            return _error(400, step.value)
        return _error(400, "unsupported_grant_type")

    def _register(self, body: dict) -> Response:
        self.registrations.append(body)
        if self.config.register_script:
            step = self.config.register_script.pop(0)
            if step is None:
                return Response(drop=True)
            return Response.of_json(step[1], step[0])
        redirects = [uri for uri in body.get("redirect_uris", []) if isinstance(uri, str)]
        native_https = body.get("application_type") == "native" and any(u.startswith("https://") for u in redirects)
        if self.config.reject_native_https_registration and native_https:
            return _error(400, "invalid_redirect_uri")
        client_id = "dyn-" + _mint()[:12]
        self._clients[client_id] = redirects
        reply: dict = {"client_id": client_id, "token_endpoint_auth_method": "none"}
        if self.config.registration_auth:
            secret = _mint()
            self.secrets.append(secret)
            self._client_secrets[client_id] = secret
            reply["client_secret"] = secret
            reply["token_endpoint_auth_method"] = self.config.registration_auth
        return Response.of_json(reply, 201)

    def _device(self, form: dict[str, str]) -> Response:
        self.device_requests.append(dict(form))
        code = _mint()
        self.secrets.append(code)
        self._device_codes[code] = form.get("resource", "")
        origin = self.urls.as_origin
        return Response.of_json(
            {
                "device_code": code,
                "user_code": USER_CODE,
                "verification_uri": f"{origin}/device",
                "verification_uri_complete": f"{origin}/device?user_code={USER_CODE}",
                "expires_in": 600,
                "interval": 5,
            }
        )

    def _canned(self, path: str) -> Response | None:
        if path not in self.config.canned:
            return None
        status, body = self.config.canned[path]
        return Response.of_json(body, status) if body is not None else Response(status)

    def _authorization_server(self, req: Request) -> Response:
        path = urlsplit(req.target).path
        self.requests.append(("as", f"{req.method} {path}"))
        canned = self._canned(path)
        if canned is not None:
            return canned
        if req.method == "GET" and path == self._metadata_path():
            return Response.of_json(self._metadata())
        if req.method == "GET" and path == "/authorize":
            try:
                target = self._authorize(_form(urlsplit(req.target).query))
            except FixtureRefusal as refusal:
                return Response(400, [("content-type", "text/plain")], str(refusal).encode())
            return Response(302, [("location", target)])
        if req.method == "POST" and path == "/token":
            return self._token(_form(req.body.decode()), req.headers.get("authorization"))
        if req.method == "POST" and path == "/register" and self.config.registration_endpoint:
            return self._register(req.json())
        if req.method == "POST" and path == "/device" and self.config.device_endpoint:
            return self._device(_form(req.body.decode()))
        return Response(404)

    def _challenge(self) -> list[tuple[str, str]]:
        cfg = self.config
        if cfg.challenge_lines is not None:
            return [("www-authenticate", line) for line in cfg.challenge_lines]
        advertised = cfg.resource_metadata_url or f"{self.urls.rs_origin}{RESOURCE_PATH}/mcp"
        parts = [f'resource_metadata="{advertised}"']
        if cfg.challenge_scope:
            parts.append(f'scope="{cfg.challenge_scope}"')
        full = "Bearer " + ", ".join(parts)
        return {
            Challenge.RESOURCE_METADATA: [("www-authenticate", full)],
            Challenge.TWO_LINES: [("www-authenticate", 'Basic realm="fixture"'), ("www-authenticate", full)],
            Challenge.BARE: [("www-authenticate", "Bearer")],
            Challenge.ABSENT: [],
        }[cfg.challenge]

    def _mcp(self, req: Request, bearer: str | None) -> Response:
        request = req.json()
        scope = self.config.forbid_scope
        if scope and request.get("method") == "tools/call":
            self.mcp.record(request, bearer)
            return Response(403, [("www-authenticate", f'Bearer error="insufficient_scope", scope="{scope}"')])
        status, body = self.mcp.handle(
            request, bearer, req.headers.get("mcp-protocol-version"), self.config.require_protocol_meta
        )
        return Response.of_json(body, status)

    def _resource_server(self, req: Request) -> Response:
        parts = urlsplit(req.target)
        self.requests.append(("rs", f"{req.method} {parts.path}"))
        canned = self._canned(parts.path)
        if canned is not None:
            return canned
        served = f"{RESOURCE_PATH}/mcp" if self.config.wellknown_at == "path" else RESOURCE_PATH
        if req.method == "GET" and parts.path == served:
            return Response.of_json(self._resource_metadata())
        if req.method == "POST" and parts.path == "/mcp":
            if "access_token" in parts.query:
                return Response(400)
            header = req.headers.get("authorization", "")
            bearer = header[7:] if header.startswith("Bearer ") else None
            granted = (
                not self.config.reject_bearers and bearer is not None and self._access.get(bearer) == self.rs_url
            )
            if granted and self.config.fail_tools:
                return Response(503)
            if granted or self.config.open:
                if self.config.redirect_to:
                    return Response(307, [("location", self.config.redirect_to)])
                return self._mcp(req, bearer)
            return Response(401, self._challenge())
        return Response(404)

