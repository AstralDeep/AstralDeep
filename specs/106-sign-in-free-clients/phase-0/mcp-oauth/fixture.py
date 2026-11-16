"""Loopback authorization server and MCP resource server that the spike adapter talks to.
Each FixtureConfig switch turns one server behavior on or off, and adapter.py and test_flow.py drive them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit


@dataclass
class FixtureConfig:
    challenge_mode: str = "resource_metadata"
    challenge_scope: str | None = "files:read"
    scopes_supported: list[str] | None = None
    wellknown_at: str = "path"
    pkce_methods: list[str] | None = field(default_factory=lambda: ["S256"])
    registration_endpoint: bool = True
    cimd_supported: bool = False
    cimd_documents: dict[str, dict] = field(default_factory=dict)
    preregistered_client_ids: tuple[str, ...] = ()
    metadata_at: str = "oauth"
    issuer_path: str = ""
    metadata_issuer_override: str | None = None
    advertise_iss: bool = True
    iss_mode: str = "correct"
    extra_authorization_servers: tuple[str, ...] = ()
    reject_native_https_registration: bool = False


class Fixture:
    def __init__(self, config: FixtureConfig | None = None) -> None:
        self.config = config or FixtureConfig()
        self.requests: list[tuple[str, str, str]] = []
        self.authorize_params: list[dict[str, str]] = []
        self.registrations: list[dict] = []
        self.token_requests: list[dict[str, str]] = []
        self.secrets: list[str] = []
        self.tokens: dict[str, str] = {}
        self._codes: dict[str, dict] = {}
        self._clients: dict[str, list[str]] = {}
        self._servers: list[ThreadingHTTPServer] = []
        self.rs_url = ""
        self.issuer = ""
        self.as_origin = ""

    def __enter__(self) -> Fixture:
        as_server = self._serve(_authorization_handler(self))
        rs_server = self._serve(_resource_handler(self))
        self.as_origin = f"http://127.0.0.1:{as_server.server_port}"
        self.issuer = self.as_origin + self.config.issuer_path
        self.rs_url = f"http://127.0.0.1:{rs_server.server_port}/mcp"
        return self

    def __exit__(self, *_exc: object) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()

    def _serve(self, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self._servers.append(server)
        return server

    def requests_to(self, role: str) -> list[str]:
        return [f"{method} {path}" for who, method, path in self.requests if who == role]

    def metadata(self) -> dict:
        cfg = self.config
        meta: dict = {
            "issuer": cfg.metadata_issuer_override or self.issuer,
            "authorization_endpoint": f"{self.as_origin}/authorize",
            "token_endpoint": f"{self.as_origin}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
        if cfg.pkce_methods is not None:
            meta["code_challenge_methods_supported"] = cfg.pkce_methods
        if cfg.registration_endpoint:
            meta["registration_endpoint"] = f"{self.as_origin}/register"
        if cfg.cimd_supported:
            meta["client_id_metadata_document_supported"] = True
        if cfg.advertise_iss:
            meta["authorization_response_iss_parameter_supported"] = True
        return meta

    def resource_metadata(self) -> dict:
        servers = [self.issuer, *self.config.extra_authorization_servers]
        meta: dict = {"resource": self.rs_url, "authorization_servers": servers}
        if self.config.scopes_supported is not None:
            meta["scopes_supported"] = self.config.scopes_supported
        return meta

    def authorize(self, params: dict[str, str]) -> tuple[int, str]:
        self.authorize_params.append(dict(params))
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256":
            return 400, "unsupported request"
        if not params.get("code_challenge") or not params.get("resource"):
            return 400, "missing code_challenge or resource"
        if client_id.startswith("https://"):
            document = self.config.cimd_documents.get(client_id)
            if not self.config.cimd_supported or not document or document.get("client_id") != client_id:
                return 400, "invalid client metadata document"
            allowed = document.get("redirect_uris", [])
        elif client_id in self.config.preregistered_client_ids:
            allowed = [redirect_uri]
        else:
            allowed = self._clients.get(client_id, [])
        if redirect_uri not in allowed:
            return 400, "redirect_uri not registered"
        code = secrets.token_urlsafe(24)
        self.secrets.append(code)
        self._codes[code] = {"challenge": params["code_challenge"], "client_id": client_id,
                             "redirect_uri": redirect_uri, "resource": params["resource"]}
        query = {"code": code, "state": params.get("state", "")}
        if self.config.iss_mode == "correct":
            query["iss"] = self.issuer
        elif self.config.iss_mode == "wrong":
            query["iss"] = "https://attacker.example"
        return 302, f"{redirect_uri}?{urlencode(query)}"

    def token(self, form: dict[str, str]) -> tuple[int, dict]:
        self.token_requests.append(dict(form))
        if form.get("code_verifier"):
            self.secrets.append(form["code_verifier"])
        grant = self._codes.pop(form.get("code", ""), None)
        if grant is None:
            return 400, {"error": "invalid_grant"}
        digest = hashlib.sha256(form.get("code_verifier", "").encode("ascii")).digest()
        verifier_ok = base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == grant["challenge"]
        if (not verifier_ok or form.get("client_id") != grant["client_id"]
                or form.get("redirect_uri") != grant["redirect_uri"]
                or form.get("resource") != grant["resource"]):
            return 400, {"error": "invalid_grant"}
        token = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.secrets.extend([token, refresh])
        self.tokens[token] = grant["resource"]
        return 200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh}

    def register(self, body: dict) -> tuple[int, dict]:
        self.registrations.append(body)
        https_redirect = any(uri.startswith("https://") for uri in body.get("redirect_uris", []))
        if self.config.reject_native_https_registration and body.get("application_type") == "native" and https_redirect:
            return 400, {"error": "invalid_redirect_uri"}
        client_id = "dyn-" + secrets.token_hex(6)
        self._clients[client_id] = list(body.get("redirect_uris", []))
        return 201, {"client_id": client_id, "token_endpoint_auth_method": "none"}


class _Base(BaseHTTPRequestHandler):
    role = ""
    fixture: Fixture

    def log_message(self, *_args: object) -> None:
        return

    def _send(self, status: int, body: dict | str = "", headers: dict[str, str] | None = None) -> None:
        payload = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if isinstance(body, str) else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _note(self) -> str:
        path = urlsplit(self.path).path
        self.fixture.requests.append((self.role, self.command, path))
        return path

    def _read(self) -> str:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()


def _authorization_handler(fixture: Fixture) -> type[BaseHTTPRequestHandler]:
    cfg = fixture.config

    class Handler(_Base):
        role = "as"

        def do_GET(self) -> None:
            path = self._note()
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            prefix = cfg.issuer_path
            served = {
                "oauth": f"/.well-known/oauth-authorization-server{prefix}",
                "openid": f"/.well-known/openid-configuration{prefix}",
                "openid_append": f"{prefix}/.well-known/openid-configuration",
            }[cfg.metadata_at]
            if path == served:
                self._send(200, fixture.metadata())
            elif path == "/authorize":
                status, target = fixture.authorize(query)
                if status == 302:
                    self._send(302, "", {"Location": target})
                else:
                    self._send(status, target)
            else:
                self._send(404, "not found")

        def do_POST(self) -> None:
            path = self._note()
            raw = self._read()
            if path == "/token":
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                status, body = fixture.token(form)
                self._send(status, body)
            elif path == "/register" and cfg.registration_endpoint:
                status, body = fixture.register(json.loads(raw))
                self._send(status, body)
            else:
                self._send(404, "not found")

    Handler.fixture = fixture
    return Handler


def _resource_handler(fixture: Fixture) -> type[BaseHTTPRequestHandler]:
    cfg = fixture.config

    class Handler(_Base):
        role = "rs"

        def _challenge(self) -> str:
            if cfg.challenge_mode != "resource_metadata":
                return "Bearer"
            origin = fixture.rs_url.rsplit("/mcp", 1)[0]
            parts = [f'resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"']
            if cfg.challenge_scope:
                parts.append(f'scope="{cfg.challenge_scope}"')
            return "Bearer " + ", ".join(parts)

        def do_GET(self) -> None:
            path = self._note()
            served = {"path": "/.well-known/oauth-protected-resource/mcp",
                      "root": "/.well-known/oauth-protected-resource"}[cfg.wellknown_at]
            if path == served:
                self._send(200, fixture.resource_metadata())
            else:
                self._send(404, "not found")

        def do_POST(self) -> None:
            path = self._note()
            raw = self._read()
            if path != "/mcp":
                self._send(404, "not found")
                return
            if "access_token" in urlsplit(self.path).query:
                self._send(400, "token in query string")
                return
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if fixture.tokens.get(token) != fixture.rs_url:
                self._send(401, "", {"WWW-Authenticate": self._challenge()})
                return
            request = json.loads(raw)
            meta = (request.get("params") or {}).get("_meta") or {}
            if meta.get("io.modelcontextprotocol/protocolVersion") != "2026-07-28":
                self._send(400, {"error": "UnsupportedProtocolVersionError"})
                return
            self._send(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": {"tools": [{"name": "search"}]}})

    Handler.fixture = fixture
    return Handler
