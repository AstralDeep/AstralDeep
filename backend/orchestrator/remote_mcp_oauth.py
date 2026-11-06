"""Runs owner-bound OAuth authorization code and PKCE for remote MCP connectors.
The connector service supplies bounded outbound HTTP and encrypted owner storage.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote_plus, urlencode, urlsplit, urlunsplit

Request = Callable[[str, str, bytes | None, Mapping[str, str] | None, int],
                   Awaitable[tuple[int, Mapping[str, str], bytes]]]
_STATE_TTL = 600
_MAX_PENDING = 32
_MAX_METADATA = 32768
_MAX_TOKEN = 8192
_MAX_AUTHORIZATION_SERVERS = 4
_CHALLENGE_METADATA = re.compile(r'\bresource_metadata="([^"\r\n]+)"', re.IGNORECASE)
_CHALLENGE_SCOPE = re.compile(r'(?:^|[\s,])scope=(?:"([^"\r\n]*)"|([^,\s]+))', re.IGNORECASE)
_PENDING_AGENT = "__remote_mcp_oauth_pending__"
_PENDING_PREFIX = "_pending_"
_TOKEN_AUTH_METHODS = {"none", "client_secret_basic", "client_secret_post"}


@dataclass(frozen=True)
class _Pending:
    owner_id: str
    connector_id: str
    endpoint: str
    client_id: str
    client_secret: str
    token_endpoint: str
    token_endpoint_auth_method: str
    redirect_uri: str
    resource: str
    verifier: str
    scope: str
    expires_at: float


class RemoteMCPOAuthError(ValueError):
    pass


def _url(value: Any, *, allow_query: bool = False) -> str:
    if type(value) is not str or not 1 <= len(value) <= 2048 or value != value.strip():
        raise RemoteMCPOAuthError("The OAuth endpoint URL is invalid.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RemoteMCPOAuthError("The OAuth endpoint URL is invalid.") from None
    if (parsed.scheme != "https" or not parsed.hostname or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or (parsed.query and not allow_query)
            or any(ord(character) < 33 or ord(character) > 126 for character in value)):
        raise RemoteMCPOAuthError("OAuth endpoints must use public HTTPS without embedded credentials.")
    return value


def _authorization_metadata_urls(issuer: str) -> tuple[str, ...]:
    parsed = urlsplit(_url(issuer))
    path = parsed.path.rstrip("/")
    candidates = [urlunsplit(("https", parsed.netloc,
                              "/.well-known/oauth-authorization-server" + path, "", ""))]
    if path:
        candidates.extend((
            urlunsplit(("https", parsed.netloc, "/.well-known/openid-configuration" + path, "", "")),
            urlunsplit(("https", parsed.netloc, path + "/.well-known/openid-configuration", "", "")),
        ))
    else:
        candidates.append(urlunsplit(("https", parsed.netloc, "/.well-known/openid-configuration", "", "")))
    return tuple(candidates)


def _resource_metadata_url(endpoint: str) -> str:
    parsed = urlsplit(_url(endpoint))
    path = parsed.path.strip("/")
    suffix = "/" + path if path else ""
    return urlunsplit(("https", parsed.netloc, "/.well-known/oauth-protected-resource" + suffix, "", ""))


def _resource_metadata_urls(endpoint: str) -> tuple[str, ...]:
    path_specific = _resource_metadata_url(endpoint)
    root = _resource_metadata_url(urlunsplit(("https", urlsplit(endpoint).netloc, "", "", "")))
    return (path_specific,) if path_specific == root else (path_specific, root)


def _json_body(body: bytes, maximum: int) -> dict[str, Any]:
    if len(body) > maximum:
        raise RemoteMCPOAuthError("The OAuth server response is too large.")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RemoteMCPOAuthError("The OAuth server returned invalid JSON.") from None
    if type(value) is not dict:
        raise RemoteMCPOAuthError("The OAuth server returned invalid metadata.")
    return value


async def _request(requester: Request | None, url: str, method: str,
                   body: bytes | None = None, headers: Mapping[str, str] | None = None,
                   maximum: int = _MAX_METADATA) -> tuple[int, Mapping[str, str], bytes]:
    _url(url, allow_query=True)
    if requester is None:
        from orchestrator.remote_mcp_connectors import http_request
        requester = http_request
    try:
        status, response_headers, response_body = await requester(url, method, body, headers, maximum)
    except Exception:  # noqa: BLE001
        raise RemoteMCPOAuthError("The OAuth server could not be reached securely.") from None
    if (type(status) is not int or not isinstance(response_headers, Mapping)
            or type(response_body) is not bytes or len(response_body) > maximum):
        raise RemoteMCPOAuthError("The OAuth server response was invalid.")
    return status, response_headers, response_body


async def _metadata(requester: Request | None, url: str) -> dict[str, Any]:
    status, _headers, body = await _request(requester, url, "GET")
    if status != 200:
        raise RemoteMCPOAuthError("OAuth discovery failed.")
    return _json_body(body, _MAX_METADATA)


async def _discover_metadata(requester: Request | None, urls: tuple[str, ...],
                             failure: str) -> dict[str, Any]:
    for url in urls:
        try:
            status, _headers, body = await _request(requester, url, "GET")
        except RemoteMCPOAuthError:
            continue
        if status == 200:
            return _json_body(body, _MAX_METADATA)
    raise RemoteMCPOAuthError(failure)


def _challenge_parameter(challenge: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(challenge)
    if match is None:
        return ""
    return next((value.strip() for value in match.groups() if value is not None), "")


async def _authorization_server_metadata(requester: Request | None,
                                         servers: Any) -> dict[str, Any]:
    if type(servers) is not list or not servers:
        raise RemoteMCPOAuthError("The MCP server did not advertise an authorization server.")
    tried: set[str] = set()
    for value in servers[:_MAX_AUTHORIZATION_SERVERS]:
        try:
            issuer = _url(_string(value, "authorization server"))
        except RemoteMCPOAuthError:
            continue
        if issuer in tried:
            continue
        tried.add(issuer)
        try:
            metadata = await _discover_metadata(
                requester, _authorization_metadata_urls(issuer), "OAuth discovery failed.")
        except RemoteMCPOAuthError:
            continue
        if metadata.get("issuer") != issuer:
            continue
        methods = metadata.get("code_challenge_methods_supported")
        if type(methods) is list and "S256" in methods:
            return metadata
    raise RemoteMCPOAuthError("No advertised authorization server supports PKCE S256.")


def _header(headers: Mapping[str, str], name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name), "")


def _string(value: Any, name: str, maximum: int = 2048) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise RemoteMCPOAuthError(f"The OAuth server omitted {name}.")
    return value


def _token_auth_methods(metadata: dict[str, Any]) -> tuple[str, ...]:
    if "token_endpoint_auth_methods_supported" not in metadata:
        return ("client_secret_basic",)
    methods = metadata.get("token_endpoint_auth_methods_supported")
    if (type(methods) is not list or not methods or len(methods) > 16
            or any(type(method) is not str or not 1 <= len(method) <= 64 for method in methods)):
        raise RemoteMCPOAuthError("The authorization server's token authentication metadata is invalid.")
    supported = tuple(dict.fromkeys(method for method in methods if method in _TOKEN_AUTH_METHODS))
    if not supported:
        raise RemoteMCPOAuthError("The authorization server supports no safe token authentication method.")
    return supported


def _token_auth_method(method: Any, client_secret: str) -> str:
    if type(method) is not str or method not in _TOKEN_AUTH_METHODS:
        raise RemoteMCPOAuthError("The OAuth client uses an unsupported token authentication method.")
    if method in {"client_secret_basic", "client_secret_post"} and not client_secret:
        raise RemoteMCPOAuthError("The authorization server requires a client secret for token requests.")
    return method


def _token_request_auth(method: str, client_id: str, client_secret: str,
                        fields: dict[str, str]) -> tuple[dict[str, str], bytes]:
    headers = {"content-type": "application/x-www-form-urlencoded"}
    if method == "client_secret_basic":
        credentials = f"{quote_plus(client_id, safe='')}:{quote_plus(client_secret, safe='')}"
        encoded = base64.b64encode(credentials.encode("ascii")).decode("ascii")
        headers["authorization"] = "Basic " + encoded
        fields.pop("client_secret", None)
        fields.pop("client_id", None)
    elif method == "client_secret_post":
        fields.update({"client_id": client_id, "client_secret": client_secret})
    else:
        fields["client_id"] = client_id
        fields.pop("client_secret", None)
    return headers, urlencode(fields).encode("ascii")


def _pending_put(orch: Any, pending: _Pending) -> str:
    manager = orch.credential_manager
    existing = [key for key in manager.list_credential_keys(pending.owner_id, _PENDING_AGENT)
                if key.startswith(_PENDING_PREFIX)]
    active = 0
    for key in existing:
        raw = manager.get_credential(pending.owner_id, _PENDING_AGENT, key)
        try:
            expiry = json.loads(raw or "{}").get("expires_at", 0)
        except (TypeError, ValueError):
            expiry = 0
        if type(expiry) not in (int, float) or expiry <= time.time():
            manager.delete_credential(pending.owner_id, _PENDING_AGENT, key)
        else:
            active += 1
    if active >= _MAX_PENDING:
        raise RemoteMCPOAuthError("Too many OAuth sign-ins are pending. Try again shortly.")
    nonce = secrets.token_hex(16)
    key = _PENDING_PREFIX + nonce
    state = manager.seal_transient_state(json.dumps({"owner_id": pending.owner_id, "nonce": nonce}))
    manager.set_credential(pending.owner_id, _PENDING_AGENT, key,
                           json.dumps(asdict(pending), separators=(",", ":")), e2e=False)
    return state


def _pending_take(orch: Any, state: str) -> _Pending:
    if type(state) is not str or not 32 <= len(state) <= 2048:
        raise RemoteMCPOAuthError("The OAuth sign-in expired. Start it again.")
    manager = orch.credential_manager
    decoded = manager.open_transient_state(state, ttl_seconds=_STATE_TTL)
    try:
        identity = json.loads(decoded or "")
        owner_id = identity["owner_id"]
        nonce = identity["nonce"]
        if (type(owner_id) is not str or not owner_id or type(nonce) is not str
                or re.fullmatch(r"[0-9a-f]{32}", nonce) is None):
            raise ValueError
        raw = manager.take_credential(owner_id, _PENDING_AGENT, _PENDING_PREFIX + nonce)
        payload = json.loads(raw or "")
        payload.setdefault("token_endpoint_auth_method", "")
        pending = _Pending(**payload)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise RemoteMCPOAuthError("The OAuth sign-in expired. Start it again.") from None
    if pending.owner_id != owner_id or pending.expires_at <= time.time():
        raise RemoteMCPOAuthError("The OAuth sign-in expired. Start it again.")
    return pending


async def begin_authorization(orch: Any, owner_id: str, connector_id: str,
                              redirect_uri: str, *, requester: Request | None = None) -> str:
    from orchestrator.remote_mcp_connectors import (
        get_connector,
        update_oauth_credentials,
    )

    redirect_uri = _url(redirect_uri)
    connector = await asyncio.to_thread(get_connector, orch, owner_id, connector_id)
    if connector.get("auth_type") != "oauth" or not connector.get("enabled"):
        raise RemoteMCPOAuthError("Enable an OAuth connector before signing in.")
    endpoint = _url(connector["url"])
    status, challenge_headers, _body = await _request(
        requester, endpoint, "POST", b"{}", {"content-type": "application/json"}, 4096)
    if status != 401:
        raise RemoteMCPOAuthError("The MCP server did not request OAuth authorization.")
    challenge = _header(challenge_headers, "www-authenticate")
    advertised_resource_metadata = _challenge_parameter(challenge, _CHALLENGE_METADATA)
    challenged_scope = _challenge_parameter(challenge, _CHALLENGE_SCOPE)
    if advertised_resource_metadata:
        resource_metadata_url = _url(advertised_resource_metadata)
        if (urlsplit(resource_metadata_url).netloc != urlsplit(endpoint).netloc
                or not urlsplit(resource_metadata_url).path.startswith("/.well-known/oauth-protected-resource")):
            raise RemoteMCPOAuthError("The MCP server advertised an invalid resource metadata URL.")
        resource_metadata = await _metadata(requester, resource_metadata_url)
    else:
        resource_metadata = await _discover_metadata(
            requester, _resource_metadata_urls(endpoint), "Protected resource discovery failed.")
    resource = _string(resource_metadata.get("resource"), "resource")
    if resource != endpoint:
        raise RemoteMCPOAuthError("The MCP resource identity did not match the saved endpoint.")
    metadata = await _authorization_server_metadata(
        requester, resource_metadata.get("authorization_servers"))
    authorization_endpoint = _url(_string(metadata.get("authorization_endpoint"), "authorization endpoint"),
                                  allow_query=True)
    token_endpoint = _url(_string(metadata.get("token_endpoint"), "token endpoint"), allow_query=True)
    client_id = connector.get("oauth_client_id") or ""
    client_secret = connector.get("oauth_client_secret") or ""
    methods = _token_auth_methods(metadata)
    if client_id:
        method = connector.get("oauth_token_endpoint_auth_method")
        if method:
            method = _token_auth_method(method, client_secret)
            if ("token_endpoint_auth_methods_supported" in metadata
                    and method not in methods):
                raise RemoteMCPOAuthError("The authorization server does not support the saved token authentication method.")
        else:
            method = next((candidate for candidate in ("client_secret_basic", "client_secret_post")
                           if candidate in methods and client_secret), None)
            if method is None and not client_secret and "none" in methods:
                method = "none"
            if method is None:
                raise RemoteMCPOAuthError("The authorization server's token authentication methods do not match the saved client credentials.")
        method = _token_auth_method(method, client_secret)
        if connector.get("oauth_token_endpoint_auth_method") != method:
            connector = await asyncio.to_thread(
                update_oauth_credentials, orch, owner_id, connector,
                {"oauth_token_endpoint_auth_method": method},
            )
    else:
        method = next((candidate for candidate in ("none", "client_secret_basic", "client_secret_post")
                       if candidate in methods), None)
    if not client_id:
        registration_endpoint = _url(_string(metadata.get("registration_endpoint"), "registration endpoint"),
                                     allow_query=True)
        registration = json.dumps({
            "client_name": "AstralDeep Remote MCP",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }, separators=(",", ":")).encode("utf-8")
        status, _headers, body = await _request(requester, registration_endpoint, "POST", registration,
                                                {"content-type": "application/json"}, _MAX_METADATA)
        if status not in (200, 201):
            raise RemoteMCPOAuthError("OAuth client registration failed.")
        registered = _json_body(body, _MAX_METADATA)
        client_id = _string(registered.get("client_id"), "client ID", 512)
        client_secret = registered.get("client_secret", "")
        if client_secret is None:
            client_secret = ""
        if type(client_secret) is not str or len(client_secret) > _MAX_TOKEN:
            raise RemoteMCPOAuthError("OAuth client registration returned an invalid secret.")
        registered_method = registered.get("token_endpoint_auth_method", "client_secret_basic")
        if registered_method != method:
            raise RemoteMCPOAuthError("OAuth client registration selected an unexpected token authentication method.")
        method = _token_auth_method(registered_method, client_secret)
        if method == "none":
            client_secret = ""
        connector = await asyncio.to_thread(
            update_oauth_credentials, orch, owner_id, connector,
            {"oauth_client_id": client_id, "oauth_client_secret": client_secret,
             "oauth_token_endpoint_auth_method": method},
        )
    scopes = resource_metadata.get("scopes_supported")
    if "scopes_supported" in resource_metadata and type(scopes) is not list:
        raise RemoteMCPOAuthError("The OAuth scopes are invalid.")
    if challenged_scope:
        scope = challenged_scope
    elif type(scopes) is list:
        if any(type(item) is not str or not item or any(ord(char) < 33 for char in item) for item in scopes):
            raise RemoteMCPOAuthError("The OAuth scopes are invalid.")
        scope = " ".join(scopes)
    else:
        scope = ""
    if (type(scope) is not str or len(scope) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in scope)):
        raise RemoteMCPOAuthError("The OAuth scope is invalid.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = await asyncio.to_thread(
        _pending_put, orch, _Pending(owner_id, connector_id, endpoint, client_id, client_secret,
                                    token_endpoint, method, redirect_uri, resource, verifier, scope,
                                    time.time() + _STATE_TTL))
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
             "resource": resource}
    if scope:
        query["scope"] = scope
    separator = "&" if urlsplit(authorization_endpoint).query else "?"
    return authorization_endpoint + separator + urlencode(query)


async def complete_authorization(orch: Any, code: str, state: str,
                                 *, requester: Request | None = None) -> str:
    from orchestrator.remote_mcp_connectors import (
        get_connector,
        update_oauth_credentials,
    )

    pending = await asyncio.to_thread(_pending_take, orch, state)
    if type(code) is not str or not 1 <= len(code) <= 2048 or any(ord(c) < 33 for c in code):
        raise RemoteMCPOAuthError("The OAuth authorization code was invalid.")
    connector = await asyncio.to_thread(get_connector, orch, pending.owner_id, pending.connector_id)
    if (connector.get("url") != pending.endpoint or connector.get("auth_type") != "oauth"
            or connector.get("oauth_client_id") != pending.client_id
            or (connector.get("oauth_client_secret") or "") != pending.client_secret
            or (connector.get("oauth_token_endpoint_auth_method") or "") != pending.token_endpoint_auth_method):
        raise RemoteMCPOAuthError("The connector changed during OAuth sign-in. Start again.")
    fields = {"grant_type": "authorization_code", "code": code,
              "redirect_uri": pending.redirect_uri, "client_id": pending.client_id,
              "code_verifier": pending.verifier, "resource": pending.resource}
    headers, body = _token_request_auth(
        pending.token_endpoint_auth_method or ("client_secret_post" if pending.client_secret else "none"),
        pending.client_id, pending.client_secret, fields)
    status, _headers, body = await _request(requester, pending.token_endpoint, "POST",
                                            body, headers, _MAX_METADATA)
    if status != 200:
        raise RemoteMCPOAuthError("The authorization server rejected the code exchange.")
    token = _json_body(body, _MAX_METADATA)
    access_token = _string(token.get("access_token"), "access token", _MAX_TOKEN)
    if str(token.get("token_type", "Bearer")).lower() != "bearer":
        raise RemoteMCPOAuthError("The authorization server returned an unsupported token type.")
    refresh_token = token.get("refresh_token") or ""
    if type(refresh_token) is not str or len(refresh_token) > _MAX_TOKEN:
        raise RemoteMCPOAuthError("The authorization server returned an invalid refresh token.")
    expires_in = token.get("expires_in")
    expires_at = time.time() + expires_in if type(expires_in) is int and 0 < expires_in <= 86400 * 365 else 0
    await asyncio.to_thread(
        update_oauth_credentials, orch, pending.owner_id, connector,
        {"access_token": access_token, "refresh_token": refresh_token,
         "token_endpoint": pending.token_endpoint, "token_expires_at": expires_at,
         "scope": pending.scope,
         "oauth_token_endpoint_auth_method": pending.token_endpoint_auth_method},
    )
    return pending.connector_id


async def refresh_authorization(orch: Any, owner_id: str, connector_id: str,
                                *, requester: Request | None = None) -> bool:
    from orchestrator.remote_mcp_connectors import (
        get_connector,
        update_oauth_credentials,
    )

    connector = await asyncio.to_thread(get_connector, orch, owner_id, connector_id)
    if connector.get("auth_type") != "oauth" or not connector.get("refresh_token"):
        return False
    endpoint = _url(_string(connector.get("token_endpoint"), "token endpoint"), allow_query=True)
    fields = {"grant_type": "refresh_token", "refresh_token": connector["refresh_token"],
              "resource": _url(connector["url"])}
    client_id = _string(connector.get("oauth_client_id"), "client ID", 512)
    client_secret = connector.get("oauth_client_secret") or ""
    method = connector.get("oauth_token_endpoint_auth_method")
    if not method:
        method = "client_secret_post" if client_secret else "none"
    method = _token_auth_method(method, client_secret)
    headers, body = _token_request_auth(method, client_id, client_secret, fields)
    status, _headers, body = await _request(requester, endpoint, "POST", body, headers, _MAX_METADATA)
    async def concurrent_refresh_succeeded() -> bool:
        from orchestrator.remote_mcp_connectors import _same_oauth_authority

        current = await asyncio.to_thread(get_connector, orch, owner_id, connector_id)
        current_expiry = current.get("token_expires_at")
        return (
            _same_oauth_authority(current, connector)
            and (current.get("refresh_token") != connector.get("refresh_token")
                 or current.get("access_token") != connector.get("access_token"))
            and type(current.get("access_token")) is str and bool(current["access_token"])
            and type(current_expiry) in (int, float) and current_expiry > time.time()
        )

    if status != 200:
        if await concurrent_refresh_succeeded():
            return True
        raise RemoteMCPOAuthError("The authorization server rejected token refresh.")
    token = _json_body(body, _MAX_METADATA)
    updates = {"access_token": _string(token.get("access_token"), "access token", _MAX_TOKEN)}
    if token.get("refresh_token"):
        updates["refresh_token"] = _string(token["refresh_token"], "refresh token", _MAX_TOKEN)
    expires_in = token.get("expires_in")
    updates["token_expires_at"] = (time.time() + expires_in
                                    if type(expires_in) is int and 0 < expires_in <= 86400 * 365 else 0)
    try:
        await asyncio.to_thread(update_oauth_credentials, orch, owner_id, connector, updates)
    except ValueError:
        if not await concurrent_refresh_succeeded():
            raise
    return True
