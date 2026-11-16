"""Minimal MCP over HTTP authorization client that runs the 2026-07-28 flow against the loopback fixture.
It refuses at the first failed check, keeps tokens only in a keystore object, and logs steps without secrets.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

PROTOCOL_VERSION = "2026-07-28"
_MAX_BODY = 262144
_LOG = logging.getLogger("mcp_oauth_spike")
_PARAM = re.compile(r'([a-z_]+)="([^"]*)"')


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass
class Tokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    scope: str = ""


@dataclass
class Keystore:
    items: dict[tuple[str, str], Tokens] = field(default_factory=dict)

    def put(self, connection_id: str, issuer: str, tokens: Tokens) -> None:
        self.items[(connection_id, issuer)] = tokens

    def get(self, connection_id: str, issuer: str) -> Tokens | None:
        return self.items.get((connection_id, issuer))


@dataclass
class Settings:
    endpoint: str
    connection_id: str
    redirect_uri: str
    browser: Callable[[str], str]
    keystore: Keystore
    client_metadata_url: str | None = None
    preregistered: Mapping[str, str] = field(default_factory=dict)
    ask_owner: Callable[[str], str | None] = lambda _issuer: None
    allow_loopback_http: bool = False


@dataclass
class Connection:
    connection_id: str
    endpoint: str
    issuer: str
    registration: str
    client_id: str
    scope: str
    trace: list[str]
    keystore: Keystore


@dataclass
class _Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Adapter:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.trace: list[str] = []

    def connect(self) -> Connection:
        endpoint = self._canonical(self.s.endpoint)
        challenge = self._probe(endpoint)
        resource_meta = self._resource_metadata(endpoint, challenge)
        issuer, as_meta = self._authorization_server(resource_meta)
        registration, client_id = self._register(issuer, as_meta)
        scope = self._scope(challenge, resource_meta)
        tokens = self._authorize(endpoint, issuer, as_meta, client_id, scope)
        self.s.keystore.put(self.s.connection_id, issuer, tokens)
        self._note("tokens stored in keystore under connection and issuer")
        return Connection(self.s.connection_id, endpoint, issuer, registration, client_id, scope,
                          self.trace, self.s.keystore)

    def _note(self, message: str) -> None:
        self.trace.append(message)
        _LOG.info(message)

    def _canonical(self, url: str) -> str:
        parts = urlsplit(url)
        self._require_transport(url)
        if parts.fragment or not parts.netloc:
            raise Refused("bad_endpoint", "The server address is not a canonical URI.")
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    def _require_transport(self, url: str) -> None:
        parts = urlsplit(url)
        loopback = parts.hostname in {"127.0.0.1", "::1"}
        if parts.scheme == "https" or (parts.scheme == "http" and loopback and self.s.allow_loopback_http):
            return
        raise Refused("insecure_transport", "Only HTTPS endpoints are allowed.")

    def _send(self, url: str, method: str = "GET", body: bytes | None = None,
              headers: Mapping[str, str] | None = None) -> _Response:
        self._require_transport(url)
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=5) as reply:
                return _Response(reply.status, dict(reply.headers.items()), reply.read(_MAX_BODY))
        except urllib.error.HTTPError as error:
            return _Response(error.code, dict(error.headers.items()), error.read(_MAX_BODY))
        except urllib.error.URLError as error:
            raise Refused("unreachable", "The server could not be reached.") from error

    def _json(self, url: str) -> dict:
        reply = self._send(url)
        if reply.status != 200:
            return {}
        try:
            value = json.loads(reply.body)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _probe(self, endpoint: str) -> Mapping[str, str]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _meta()}}).encode()
        reply = self._send(endpoint, "POST", body, {"Content-Type": "application/json"})
        self._note(f"probe without token returned {reply.status}")
        if reply.status != 401:
            raise Refused("not_oauth", "The server did not ask for authorization.")
        header = next((v for k, v in reply.headers.items() if k.lower() == "www-authenticate"), "")
        return dict(_PARAM.findall(header))

    def _resource_metadata(self, endpoint: str, challenge: Mapping[str, str]) -> dict:
        parts = urlsplit(endpoint)
        origin = f"{parts.scheme}://{parts.netloc}"
        advertised = challenge.get("resource_metadata")
        if advertised:
            candidates = [advertised]
            self._note("resource metadata url taken from WWW-Authenticate")
        else:
            root = origin + "/.well-known/oauth-protected-resource"
            candidates = [root + parts.path, root] if parts.path else [root]
            self._note("no resource_metadata parameter, falling back to well-known urls")
        for url in candidates:
            meta = self._json(url)
            self._note(f"GET {urlsplit(url).path} -> {'found' if meta else 'missing'}")
            if meta:
                if meta.get("resource") != endpoint:
                    raise Refused("resource_mismatch", "The resource metadata names a different server.")
                if not meta.get("authorization_servers"):
                    raise Refused("no_authorization_server", "The resource metadata lists no authorization server.")
                return meta
        raise Refused("no_resource_metadata", "No protected resource metadata was found.")

    def _authorization_server(self, resource_meta: Mapping) -> tuple[str, dict]:
        for issuer in resource_meta["authorization_servers"]:
            meta = self._as_metadata(issuer)
            if "S256" in (meta.get("code_challenge_methods_supported") or []):
                self._note(f"authorization server {issuer} supports S256")
                return issuer, meta
            self._note(f"authorization server {issuer} does not advertise S256")
        raise Refused("no_s256", "No authorization server advertises S256, so the flow stopped.")

    def _as_metadata(self, issuer: str) -> dict:
        parts = urlsplit(issuer)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.rstrip("/")
        if path:
            urls = [f"{origin}/.well-known/oauth-authorization-server{path}",
                    f"{origin}/.well-known/openid-configuration{path}",
                    f"{origin}{path}/.well-known/openid-configuration"]
        else:
            urls = [f"{origin}/.well-known/oauth-authorization-server", f"{origin}/.well-known/openid-configuration"]
        for url in urls:
            meta = self._json(url)
            self._note(f"GET {urlsplit(url).path} -> {'found' if meta else 'missing'}")
            if meta:
                if meta.get("issuer") != issuer:
                    raise Refused("issuer_mismatch", "The metadata issuer differs from the issuer used to find it.")
                return meta
        raise Refused("no_as_metadata", "No authorization server metadata was found.")

    def _register(self, issuer: str, meta: Mapping) -> tuple[str, str]:
        if issuer in self.s.preregistered:
            self._note("registration: pre-registered client")
            return "pre-registered", self.s.preregistered[issuer]
        if meta.get("client_id_metadata_document_supported") is True and self.s.client_metadata_url:
            self._note("registration: client id metadata document")
            return "client-id-metadata-document", self.s.client_metadata_url
        if meta.get("registration_endpoint"):
            self._note("registration: dynamic")
            for application_type in ("native", "web"):
                registration = json.dumps({
                    "client_name": "Astral", "application_type": application_type,
                    "redirect_uris": [self.s.redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none"}).encode()
                reply = self._send(meta["registration_endpoint"], "POST", registration,
                                   {"Content-Type": "application/json"})
                if reply.status in (200, 201):
                    break
                self._note(f"registration with application_type {application_type} returned {reply.status}")
            client_id = _loads(reply.body).get("client_id") if reply.status in (200, 201) else None
            if not isinstance(client_id, str) or not client_id:
                raise Refused("registration_failed", "Dynamic registration was rejected.")
            return "dynamic", client_id
        owner_client_id = self.s.ask_owner(issuer)
        if not owner_client_id:
            raise Refused("no_client_id", "No registration option is available and the owner gave no client id.")
        self._note("registration: client id supplied by the owner")
        return "owner-supplied", owner_client_id

    @staticmethod
    def _scope(challenge: Mapping[str, str], resource_meta: Mapping) -> str:
        if challenge.get("scope"):
            return challenge["scope"]
        return " ".join(resource_meta.get("scopes_supported") or [])

    def _authorize(self, endpoint: str, issuer: str, meta: Mapping, client_id: str, scope: str) -> Tokens:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        record = {"issuer": issuer, "state": secrets.token_urlsafe(16), "verifier": verifier}
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": self.s.redirect_uri,
                 "state": record["state"], "code_challenge": challenge, "code_challenge_method": "S256",
                 "resource": endpoint}
        if scope:
            query["scope"] = scope
        for url in (meta["authorization_endpoint"], meta["token_endpoint"]):
            self._require_transport(url)
        returned = self.s.browser(f"{meta['authorization_endpoint']}?{urlencode(query)}")
        code = self._validate_response(returned, record, meta)
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.s.redirect_uri,
                "client_id": client_id, "code_verifier": verifier, "resource": endpoint}
        reply = self._send(meta["token_endpoint"], "POST", urlencode(form).encode(),
                           {"Content-Type": "application/x-www-form-urlencoded"})
        body = _loads(reply.body)
        if reply.status != 200 or not isinstance(body.get("access_token"), str):
            raise Refused("token_refused", "The token request was refused.")
        self._note("code redeemed with resource and code_verifier")
        return Tokens(body["access_token"], body.get("refresh_token"), body.get("scope") or scope)

    def _validate_response(self, returned: str, record: Mapping[str, str], meta: Mapping) -> str:
        parts = urlsplit(returned)
        if f"{parts.scheme}://{parts.netloc}{parts.path}" != self.s.redirect_uri.split("?", 1)[0]:
            raise Refused("redirect_mismatch", "The browser returned to an unexpected address.")
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        iss = params.get("iss")
        if meta.get("authorization_response_iss_parameter_supported") is True and iss is None:
            raise Refused("iss_missing", "The response has no iss parameter. The code was discarded.")
        if iss is not None and iss != record["issuer"]:
            raise Refused("iss_mismatch",
                          f"The response iss {iss} differs from the recorded issuer {record['issuer']}. The code was discarded.")
        self._note("iss check passed")
        if params.get("state") != record["state"]:
            raise Refused("state_mismatch", "The response state does not match. The code was discarded.")
        if "error" in params:
            raise Refused("authorization_error", f"The authorization server refused with {params['error']}.")
        if not params.get("code"):
            raise Refused("no_code", "The response carried no code.")
        return params["code"]


def _meta() -> dict[str, str]:
    return {"io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION}


def _loads(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def call(connection: Connection, url: str, method: str, params: dict | None = None) -> dict:
    if url != connection.endpoint:
        raise Refused("token_passthrough", "A token is sent only to the server it was issued for.")
    tokens = connection.keystore.get(connection.connection_id, connection.issuer)
    if tokens is None:
        raise Refused("not_signed_in", "This device holds no credential for the connection.")
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": method,
                       "params": {**(params or {}), "_meta": _meta()}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {tokens.access_token}"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=5) as reply:
            return json.loads(reply.read(_MAX_BODY))
    except urllib.error.HTTPError as error:
        raise Refused("call_refused", f"The server answered {error.code}.") from error
