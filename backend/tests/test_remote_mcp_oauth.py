"""Exercises owner-bound MCP OAuth discovery, browser-bound start and callback, PKCE exchange,
refresh, and refusal. Network requests use a deterministic fake while the connector store uses its real API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography.fernet import Fernet, InvalidToken
from orchestrator import remote_mcp_api
from orchestrator import remote_mcp_connectors as connectors
from orchestrator import remote_mcp_oauth as oauth
from orchestrator.credential_manager import CredentialManager
from orchestrator.projection_surfaces import remote_mcp as remote_mcp_surface
from starlette.requests import Request as StarletteRequest

RESOURCE = "https://mcp.example/mcp"
ISSUER = "https://id.example"
CALLBACK = "https://astral.example/api/remote-mcp/oauth/callback"
START = "https://astral.example/api/remote-mcp/oauth/start"
_UNSET = object()


class _Credentials:
    def __init__(self):
        self.rows = {}
        self.fernet = Fernet(Fernet.generate_key())

    def get_credential(self, owner, agent, key):
        return self.rows.get((owner, agent, key))

    def set_credential(self, owner, agent, key, value, *, e2e):
        assert e2e is False
        self.rows[(owner, agent, key)] = value

    def compare_and_set_credential(self, owner, agent, key, expected, value):
        identity = (owner, agent, key)
        if self.rows.get(identity) != expected:
            return False
        self.rows[identity] = value
        return True

    def list_credential_keys(self, owner, agent):
        return [key for current_owner, current_agent, key in self.rows
                if current_owner == owner and current_agent == agent]

    def delete_credential(self, owner, agent, key):
        self.rows.pop((owner, agent, key), None)

    def take_credential(self, owner, agent, key):
        return self.rows.pop((owner, agent, key), None)

    def seal_transient_state(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def open_transient_state(self, value, *, ttl_seconds):
        try:
            return self.fernet.decrypt(value.encode(), ttl=ttl_seconds).decode()
        except InvalidToken:
            return None


class _Orch:
    def __init__(self):
        self.credential_manager = _Credentials()


class _Requester:
    def __init__(self, *, resource=RESOURCE, challenge_origin=RESOURCE, token_status=200,
                 authorization_query="", challenge_header=None, authorization_servers=(ISSUER,),
                 scopes_supported=("user",), resource_documents=None, authorization_documents=None,
                 token_auth_methods=("none",), registered_method="none", registered_secret=_UNSET):
        self.resource = resource
        self.challenge_origin = challenge_origin
        self.token_status = token_status
        self.authorization_query = authorization_query
        self.challenge_header = challenge_header
        self.authorization_servers = authorization_servers
        self.scopes_supported = scopes_supported
        self.resource_documents = resource_documents or {}
        self.authorization_documents = authorization_documents or {}
        self.token_auth_methods = token_auth_methods
        self.registered_method = registered_method
        self.registered_secret = registered_secret
        self.calls = []
        self.request_details = []

    async def __call__(self, url, method, body, headers, maximum):
        self.calls.append((url, method, body))
        self.request_details.append((url, method, body, dict(headers or {})))
        if url == RESOURCE and method == "POST":
            if self.challenge_header is not None:
                return 401, {"WWW-Authenticate": self.challenge_header}, b""
            origin = urlsplit(self.challenge_origin)
            metadata_url = f"{origin.scheme}://{origin.netloc}/.well-known/oauth-protected-resource/mcp"
            return 401, {"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'}, b""
        if url in self.resource_documents:
            status, document = self.resource_documents[url]
            return status, {}, json.dumps(document).encode() if document is not None else b""
        if url in self.authorization_documents:
            status, document = self.authorization_documents[url]
            return status, {}, json.dumps(document).encode() if document is not None else b""
        if url == "https://mcp.example/.well-known/oauth-protected-resource/mcp":
            return 200, {}, json.dumps({"resource": self.resource,
                                         "authorization_servers": list(self.authorization_servers),
                                         "scopes_supported": list(self.scopes_supported)}).encode()
        if url == "https://id.example/.well-known/oauth-authorization-server":
            metadata = {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize" + self.authorization_query,
                "token_endpoint": ISSUER + "/token",
                "registration_endpoint": ISSUER + "/register",
                "code_challenge_methods_supported": ["S256"],
            }
            if self.token_auth_methods is not _UNSET:
                metadata["token_endpoint_auth_methods_supported"] = self.token_auth_methods
            return 200, {}, json.dumps(metadata).encode()
        if url == ISSUER + "/register":
            assert method == "POST" and b"AstralDeep Remote MCP" in body
            result = {"client_id": "registered-client"}
            if self.registered_method is not _UNSET:
                result["token_endpoint_auth_method"] = self.registered_method
            if self.registered_secret is not _UNSET:
                result["client_secret"] = self.registered_secret
            return 201, {}, json.dumps(result).encode()
        if url == ISSUER + "/token":
            if self.token_status != 200:
                return self.token_status, {}, b'{"error":"invalid_grant"}'
            assert method == "POST"
            return 200, {}, b'{"access_token":"access-1","refresh_token":"refresh-1","token_type":"Bearer","expires_in":3600}'
        raise AssertionError((url, method))


def _connector(orch, *, client_id="client-1", client_secret="", auth_method=""):
    values = {
        "name": "Example MCP", "url": RESOURCE, "transport": "streamable_http",
        "auth_type": "oauth", "oauth_client_id": client_id,
        "oauth_client_secret": client_secret, "enabled": True, "tools": {},
    }
    if auth_method:
        values["oauth_token_endpoint_auth_method"] = auth_method
    return connectors.save_connector(orch, "owner-1", values)


async def _authorize(orch, connector_id, requester, owner="owner-1"):
    start_url = await oauth.begin_authorization(orch, owner, connector_id, CALLBACK, START, requester=requester)
    assert start_url.startswith(START + "?state=")
    state = parse_qs(urlsplit(start_url).query)["state"][0]
    return await oauth.bind_authorization(orch, owner, state)


def test_saving_connector_clears_oauth_client_and_authorization_state_only_when_requested():
    orch = _Orch()
    saved = _connector(orch)
    connector = connectors.get_connector(orch, "owner-1", saved["id"])
    connector.update({"oauth_client_secret": "client-secret", "access_token": "access",
                      "refresh_token": "refresh", "token_endpoint": ISSUER + "/token",
                      "oauth_token_endpoint_auth_method": "client_secret_post",
                      "authorization_endpoint": ISSUER + "/authorize", "token_expires_at": 1000,
                      "scope": "read"})
    connectors.save_connector(orch, "owner-1", connector)

    edited = connectors.get_connector(orch, "owner-1", saved["id"])
    connectors.save_connector(orch, "owner-1", {**edited, "oauth_client_id": "", "oauth_client_secret": ""})
    preserved = connectors.get_connector(orch, "owner-1", saved["id"])
    assert preserved["oauth_client_id"] == "client-1"
    assert preserved["oauth_client_secret"] == "client-secret"
    assert preserved["access_token"] == "access"

    connectors.save_connector(orch, "owner-1", {**edited, "oauth_client_id": "", "oauth_client_secret": ""},
                              clear_oauth_client_credentials=True)
    cleared = connectors.get_connector(orch, "owner-1", saved["id"])
    assert not cleared.get("oauth_client_id")
    assert not cleared.get("oauth_client_secret")
    assert not any(cleared.get(field) for field in (
        "oauth_token_endpoint_auth_method", "access_token", "refresh_token", "token_endpoint",
        "authorization_endpoint", "token_expires_at", "scope"))


@pytest.mark.parametrize("field,value", [
    ("url", "https://changed.example/mcp"),
    ("oauth_client_id", "replacement-client"),
    ("oauth_client_secret", "replacement-secret"),
])
def test_oauth_authority_or_client_changes_clear_method_and_tokens(field, value):
    orch = _Orch()
    saved = _connector(orch, client_secret="client-secret", auth_method="client_secret_basic")
    connector = connectors.get_connector(orch, "owner-1", saved["id"])
    connector.update({"access_token": "access", "refresh_token": "refresh",
                      "token_endpoint": ISSUER + "/token", "token_expires_at": 1234})
    connectors.save_connector(orch, "owner-1", connector)

    changed = connectors.save_connector(orch, "owner-1", {**connector, field: value})

    assert not changed.get("oauth_token_endpoint_auth_method")
    assert not changed.get("access_token")
    assert not changed.get("refresh_token")
    assert not changed.get("token_endpoint")


def test_clear_oauth_client_credentials_requires_boolean():
    orch = _Orch()
    with pytest.raises(connectors.RemoteMCPError, match="must be a boolean"):
        connectors.save_connector(orch, "owner-1", {
            "name": "Example MCP", "url": RESOURCE, "transport": "streamable_http",
            "auth_type": "oauth", "oauth_client_id": "client-1",
        }, clear_oauth_client_credentials="true")


def test_connector_auth_method_validation_and_no_auth_cleanup():
    with pytest.raises(connectors.RemoteMCPError, match="token authentication method is invalid"):
        connectors._validate_config({
            "name": "Example", "url": RESOURCE, "transport": "streamable_http",
            "auth_type": "oauth", "oauth_token_endpoint_auth_method": [],
        })

    saved = connectors.save_connector(_Orch(), "owner-1", {
        "name": "Example", "url": RESOURCE, "transport": "streamable_http",
        "auth_type": "none", "oauth_token_endpoint_auth_method": "client_secret_post",
    })
    assert not saved.get("oauth_token_endpoint_auth_method")


def test_remote_mcp_edit_surfaces_expose_explicit_oauth_client_clear(monkeypatch):
    monkeypatch.setattr(remote_mcp_surface, "_enabled", lambda: True)
    monkeypatch.setattr(connectors, "list_connectors", lambda *_args: [])
    orch = _Orch()
    saved = _connector(orch)
    params = {"edit": saved["id"]}
    html = asyncio.run(remote_mcp_surface.render(orch, "owner-1", [], params))
    assert 'name="clear_oauth_client_credentials" type="checkbox" value="true"' in html

    components = asyncio.run(remote_mcp_surface.components(orch, "owner-1", [], params))
    form = next(component for component in components if component.get("submit_action") == "chrome_remote_mcp_save")
    clear_field = next(field for field in form["fields"] if field["name"] == "clear_oauth_client_credentials")
    assert clear_field["kind"] == "boolean"
    assert clear_field["default"] is False


def test_remote_mcp_save_action_forwards_explicit_oauth_clear(monkeypatch):
    connector_id = "a" * 32
    saved = {}
    monkeypatch.setattr(remote_mcp_surface, "_enabled", lambda: True)
    monkeypatch.setattr(connectors, "get_connector", lambda *_args: {"tools": {}, "tools_catalog": [], "enabled": True})

    def save(_orch, _owner, _config, *, clear_oauth_client_credentials=False, require_existing=False):
        saved["clear"] = clear_oauth_client_credentials
        saved["require_existing"] = require_existing

    monkeypatch.setattr(connectors, "save_connector", save)
    asyncio.run(remote_mcp_surface._save(None, None, "owner-1", [], {
        "connector_id": connector_id,
        "fields": {"name": "Example MCP", "url": RESOURCE, "transport": "streamable_http",
                   "auth_type": "oauth", "clear_oauth_client_credentials": "true"},
    }))
    assert saved["clear"] is True
    assert saved["require_existing"] is True


@pytest.mark.asyncio
async def test_pkce_exchange_stores_owner_token_and_consumes_state():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()

    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    query = parse_qs(urlsplit(authorization_url).query)

    assert query["client_id"] == ["client-1"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [RESOURCE]
    assert query["scope"] == ["user"]
    state = query["state"][0]
    callback_worker = _Orch()
    callback_worker.credential_manager = orch.credential_manager
    assert await oauth.complete_authorization(callback_worker, "code-1", state, binding=binding, session_owner="owner-1", requester=requester) == saved["id"]
    stored = connectors.get_connector(orch, "owner-1", saved["id"])
    assert stored["access_token"] == "access-1"
    assert stored["refresh_token"] == "refresh-1"
    assert stored["token_endpoint"] == ISSUER + "/token"
    assert stored["token_expires_at"] > 0
    assert connectors.list_connectors(orch, "other-owner") == []
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)


@pytest.mark.asyncio
async def test_dynamic_registration_and_refresh():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester(registered_secret="unused-for-public-client")

    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    assert parse_qs(urlsplit(authorization_url).query)["client_id"] == ["registered-client"]
    assert connectors.get_connector(orch, "owner-1", saved["id"])["oauth_client_id"] == "registered-client"
    await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester) is True
    assert [url for url, method, _ in requester.calls if url == ISSUER + "/token" and method == "POST"] == [
        ISSUER + "/token", ISSUER + "/token"]
    assert connectors.get_connector(orch, "owner-1", saved["id"])["oauth_token_endpoint_auth_method"] == "none"
    assert connectors.get_connector(orch, "owner-1", saved["id"]).get("oauth_client_secret", "") == ""
    registration = next(body for url, method, body in requester.calls if url == ISSUER + "/register")
    assert json.loads(registration)["token_endpoint_auth_method"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_method,client_secret", [
    ("none", ""),
    ("client_secret_basic", "sëcret: value+"),
    ("client_secret_post", "sëcret: value+"),
])
async def test_token_auth_methods_are_used_for_exchange_and_refresh(auth_method, client_secret):
    orch = _Orch()
    client_id = "client:π name+"
    saved = _connector(orch, client_id=client_id, client_secret=client_secret)
    requester = _Requester(token_auth_methods=[auth_method])
    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    assert connectors.get_connector(orch, "owner-1", saved["id"])["oauth_token_endpoint_auth_method"] == auth_method

    await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
    token_calls = [detail for detail in requester.request_details
                   if detail[0] == ISSUER + "/token"]
    assert len(token_calls) == 2
    for _url, _method, body, headers in token_calls:
        fields = parse_qs(body.decode())
        if auth_method == "client_secret_basic":
            encoded_credentials = base64.b64encode(
                f"client%3A%CF%80+name%2B:{'s%C3%ABcret%3A+value%2B'}".encode("ascii")
            ).decode("ascii")
            assert headers["authorization"] == "Basic " + encoded_credentials
            assert "client_id" not in fields and "client_secret" not in fields
        elif auth_method == "client_secret_post":
            assert fields["client_id"] == [client_id]
            assert fields["client_secret"] == [client_secret]
            assert "authorization" not in headers
        else:
            assert fields["client_id"] == [client_id]
            assert "client_secret" not in fields
            assert "authorization" not in headers


@pytest.mark.asyncio
async def test_missing_method_metadata_defaults_confidential_authorization_to_basic():
    orch = _Orch()
    saved = _connector(orch, client_secret="confidential-secret")
    requester = _Requester(token_auth_methods=_UNSET)
    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]

    assert connectors.get_connector(orch, "owner-1", saved["id"])["oauth_token_endpoint_auth_method"] == "client_secret_basic"
    await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
    token_calls = [detail for detail in requester.request_details if detail[0] == ISSUER + "/token"]
    assert len(token_calls) == 2
    assert all(headers.get("authorization", "").startswith("Basic ") for _, _, _, headers in token_calls)
    assert all(b"client_secret=" not in body for _, _, body, _ in token_calls)


@pytest.mark.asyncio
async def test_missing_method_metadata_requires_secret_for_fresh_confidential_client():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester(token_auth_methods=_UNSET)

    with pytest.raises(oauth.RemoteMCPOAuthError, match="do not match the saved client credentials"):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.asyncio
async def test_legacy_refresh_keeps_form_post_until_reauthorization():
    orch = _Orch()
    saved = _connector(orch, client_secret="legacy-secret")
    connector = connectors.get_connector(orch, "owner-1", saved["id"])
    connector.update({"refresh_token": "refresh-old", "token_endpoint": ISSUER + "/token"})
    connectors.save_connector(orch, "owner-1", connector)
    requester = _Requester()

    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)

    _url, _method, body, headers = next(
        detail for detail in requester.request_details if detail[0] == ISSUER + "/token")
    assert parse_qs(body.decode())["client_secret"] == ["legacy-secret"]
    assert "authorization" not in headers


@pytest.mark.asyncio
async def test_dcr_rejects_server_selected_auth_method_mismatch():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester(registered_method="client_secret_post", registered_secret="issued")

    with pytest.raises(oauth.RemoteMCPOAuthError, match="unexpected token authentication method"):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)

    assert not connectors.get_connector(orch, "owner-1", saved["id"]).get("access_token")


@pytest.mark.asyncio
async def test_dcr_defaults_to_basic_when_methods_and_registration_method_are_omitted():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester(token_auth_methods=_UNSET, registered_method=_UNSET,
                           registered_secret="issued-secret")

    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    registration = next(body for url, method, body in requester.calls if url == ISSUER + "/register")
    assert json.loads(registration)["token_endpoint_auth_method"] == "client_secret_basic"
    assert connectors.get_connector(orch, "owner-1", saved["id"])["oauth_token_endpoint_auth_method"] == "client_secret_basic"
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]

    await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
    token_calls = [detail for detail in requester.request_details if detail[0] == ISSUER + "/token"]
    assert len(token_calls) == 2
    assert all(headers.get("authorization", "").startswith("Basic ") for _, _, _, headers in token_calls)
    assert all(b"client_secret=" not in body for _, _, body, _ in token_calls)


@pytest.mark.asyncio
async def test_dcr_selects_confidential_post_when_it_is_the_only_advertised_method():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester(token_auth_methods=["client_secret_post"],
                           registered_method="client_secret_post", registered_secret="issued-secret")

    await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)

    registration = next(body for url, method, body in requester.calls if url == ISSUER + "/register")
    assert json.loads(registration)["token_endpoint_auth_method"] == "client_secret_post"
    stored = connectors.get_connector(orch, "owner-1", saved["id"])
    assert stored["oauth_token_endpoint_auth_method"] == "client_secret_post"
    assert stored["oauth_client_secret"] == "issued-secret"


@pytest.mark.asyncio
async def test_dcr_confidential_method_requires_server_issued_secret():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester(token_auth_methods=["client_secret_basic"],
                           registered_method="client_secret_basic")

    with pytest.raises(oauth.RemoteMCPOAuthError, match="requires a client secret"):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.asyncio
async def test_dcr_requires_explicit_none_response_when_none_was_requested():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester(token_auth_methods=["none"], registered_method=_UNSET)

    with pytest.raises(oauth.RemoteMCPOAuthError, match="unexpected token authentication method"):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.asyncio
async def test_dcr_treats_explicit_null_secret_as_absent_for_public_client():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester(token_auth_methods=["client_secret_basic", "none"], registered_secret=None)

    await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)

    stored = connectors.get_connector(orch, "owner-1", saved["id"])
    assert stored["oauth_token_endpoint_auth_method"] == "none"
    assert not stored.get("oauth_client_secret")
    registration = next(body for url, method, body in requester.calls if url == ISSUER + "/register")
    assert json.loads(registration)["token_endpoint_auth_method"] == "none"


@pytest.mark.asyncio
async def test_pending_callback_rejects_changed_token_auth_method():
    orch = _Orch()
    saved = _connector(orch, client_secret="client-secret")
    requester = _Requester(token_auth_methods=["client_secret_basic", "client_secret_post"])
    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    connector = connectors.get_connector(orch, "owner-1", saved["id"])
    connectors.update_oauth_credentials(
        orch, "owner-1", connector, {"oauth_token_endpoint_auth_method": "client_secret_post"})

    with pytest.raises(oauth.RemoteMCPOAuthError, match="connector changed"):
        await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)


@pytest.mark.asyncio
async def test_saved_token_method_must_remain_supported_by_server_metadata():
    orch = _Orch()
    saved = _connector(orch, client_secret="secret", auth_method="client_secret_post")
    requester = _Requester(token_auth_methods=["client_secret_basic"])

    with pytest.raises(oauth.RemoteMCPOAuthError, match="does not support the saved token"):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.parametrize("method,secret", [
    ([], "secret"),
    ("client_secret_basic", ""),
])
def test_token_auth_method_rejects_invalid_method_and_missing_secret(method, secret):
    with pytest.raises(oauth.RemoteMCPOAuthError):
        oauth._token_auth_method(method, secret)


@pytest.mark.asyncio
@pytest.mark.parametrize("methods,secret,reason", [
    (None, "secret", "metadata is invalid"),
    ("none", "secret", "metadata is invalid"),
    (["private_key_jwt"], "secret", "no safe token authentication method"),
    (["client_secret_basic"], "", "do not match the saved client credentials"),
])
async def test_rejects_malformed_unsupported_or_unusable_token_auth_metadata(methods, secret, reason):
    orch = _Orch()
    saved = _connector(orch, client_secret=secret)
    requester = _Requester(token_auth_methods=methods)

    with pytest.raises(oauth.RemoteMCPOAuthError, match=reason):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.asyncio
async def test_authorization_endpoint_preserves_provider_query():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester(authorization_query="?tenant=demo")
    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    query = parse_qs(urlsplit(authorization_url).query)
    assert query["tenant"] == ["demo"]
    assert query["code_challenge_method"] == ["S256"]


@pytest.mark.asyncio
async def test_resource_metadata_falls_back_from_endpoint_path_to_root_and_requests_all_scopes():
    orch = _Orch()
    saved = _connector(orch)
    path_url = "https://mcp.example/.well-known/oauth-protected-resource/mcp"
    root_url = "https://mcp.example/.well-known/oauth-protected-resource"
    requester = _Requester(
        challenge_header="Bearer realm=\"mcp\"",
        resource_documents={
            path_url: (404, None),
            root_url: (200, {"resource": RESOURCE, "authorization_servers": [ISSUER],
                             "scopes_supported": ["read", "write"]}),
        },
    )

    authorization_url, binding = await _authorize(orch, saved["id"], requester)

    assert [call[0] for call in requester.calls if call[0] in (path_url, root_url)] == [path_url, root_url]
    assert parse_qs(urlsplit(authorization_url).query)["scope"] == ["read write"]


@pytest.mark.asyncio
async def test_challenge_scope_overrides_supported_scopes():
    orch = _Orch()
    saved = _connector(orch)
    metadata_url = "https://mcp.example/.well-known/oauth-protected-resource/mcp"
    requester = _Requester(
        challenge_header=f'Bearer resource_metadata="{metadata_url}", scope="files:read files:write"',
        scopes_supported=("generic", "other"),
    )

    authorization_url, binding = await _authorize(orch, saved["id"], requester)

    assert parse_qs(urlsplit(authorization_url).query)["scope"] == ["files:read files:write"]


@pytest.mark.asyncio
async def test_issuer_path_discovery_uses_ordered_oauth_and_oidc_locations():
    orch = _Orch()
    issuer = "https://id.example/tenant1"
    saved = _connector(orch)
    path_urls = (
        "https://id.example/.well-known/oauth-authorization-server/tenant1",
        "https://id.example/.well-known/openid-configuration/tenant1",
        "https://id.example/tenant1/.well-known/openid-configuration",
    )
    metadata = {
        "issuer": issuer,
        "authorization_endpoint": issuer + "/authorize",
        "token_endpoint": issuer + "/token",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
    requester = _Requester(
        authorization_servers=(issuer,),
        authorization_documents={path_urls[0]: (404, None), path_urls[1]: (404, None),
                                 path_urls[2]: (200, metadata)},
    )

    await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)

    requested = [call[0] for call in requester.calls if call[0] in path_urls]
    assert requested == list(path_urls)


@pytest.mark.asyncio
async def test_root_issuer_discovery_falls_back_to_oidc_and_requires_pkce_metadata():
    orch = _Orch()
    saved = _connector(orch)
    oauth_url = "https://id.example/.well-known/oauth-authorization-server"
    oidc_url = "https://id.example/.well-known/openid-configuration"
    metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/authorize",
        "token_endpoint": ISSUER + "/token",
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
    requester = _Requester(authorization_documents={oauth_url: (404, None), oidc_url: (200, metadata)})

    await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)

    assert [call[0] for call in requester.calls if call[0] in (oauth_url, oidc_url)] == [oauth_url, oidc_url]

    for methods in (None, ["plain"]):
        invalid_metadata = dict(metadata)
        if methods is None:
            invalid_metadata.pop("code_challenge_methods_supported")
        else:
            invalid_metadata["code_challenge_methods_supported"] = methods
        requester = _Requester(authorization_documents={oauth_url: (200, invalid_metadata)})
        with pytest.raises(oauth.RemoteMCPOAuthError, match="PKCE S256"):
            await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.asyncio
async def test_authorization_server_selection_skips_invalid_candidates_and_is_bounded():
    orch = _Orch()
    saved = _connector(orch)
    bad_issuer = "https://bad.example"
    mismatch_issuer = "https://mismatch.example"
    selected_issuer = "https://selected.example"
    unvisited_issuer = "https://unvisited.example"
    bad_url = bad_issuer + "/.well-known/oauth-authorization-server"
    mismatch_url = mismatch_issuer + "/.well-known/oauth-authorization-server"
    selected_url = selected_issuer + "/.well-known/oauth-authorization-server"
    unvisited_url = unvisited_issuer + "/.well-known/oauth-authorization-server"

    def metadata(issuer, *, methods):
        return {
            "issuer": issuer,
            "authorization_endpoint": issuer + "/authorize",
            "token_endpoint": issuer + "/token",
            "code_challenge_methods_supported": methods,
            "token_endpoint_auth_methods_supported": ["none"],
        }

    requester = _Requester(
        authorization_servers=("http://invalid.example", bad_issuer, mismatch_issuer,
                               selected_issuer, unvisited_issuer),
        authorization_documents={
            bad_url: (200, metadata(bad_issuer, methods=["plain"])),
            mismatch_url: (200, metadata("https://other.example", methods=["S256"])),
            selected_url: (200, metadata(selected_issuer, methods=["S256"])),
            unvisited_url: (200, metadata(unvisited_issuer, methods=["S256"])),
        },
    )

    authorization_url, binding = await _authorize(orch, saved["id"], requester)

    requested = [call[0] for call in requester.calls if call[0] in
                 (bad_url, mismatch_url, selected_url, unvisited_url)]
    assert requested == [bad_url, mismatch_url, selected_url]
    assert urlsplit(authorization_url).scheme == "https"
    assert urlsplit(authorization_url).netloc == "selected.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("requester,reason", [
    (_Requester(resource="https://other.example/mcp"), "resource identity"),
    (_Requester(challenge_origin="https://other.example/mcp"), "metadata URL"),
])
async def test_rejects_cross_resource_oauth_metadata(requester, reason):
    orch = _Orch()
    saved = _connector(orch)
    with pytest.raises(oauth.RemoteMCPOAuthError, match=reason):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.asyncio
async def test_failed_token_exchange_does_not_store_access_token():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester(token_status=400)
    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]

    with pytest.raises(oauth.RemoteMCPOAuthError, match="rejected"):
        await oauth.complete_authorization(orch, "bad-code", state, binding=binding, session_owner="owner-1", requester=requester)
    assert not connectors.get_connector(orch, "owner-1", saved["id"]).get("access_token")


@pytest.mark.parametrize("value", [None, "", " https://id.example", "http://id.example",
                                    "https://user:pass@id.example", "https://id.example:444",
                                    "https://id.example/path#fragment", "https://id.example/path?x=1",
                                    "https://id.example:bad"])
def test_endpoint_url_validation_rejects_unsafe_values(value):
    with pytest.raises(oauth.RemoteMCPOAuthError):
        oauth._url(value)


@pytest.mark.parametrize("body,maximum,reason", [
    (b"{}", 1, "too large"), (b"\xff", 10, "invalid JSON"),
    (b"[]", 10, "invalid metadata"),
])
def test_json_metadata_rejects_oversized_malformed_and_non_object(body, maximum, reason):
    with pytest.raises(oauth.RemoteMCPOAuthError, match=reason):
        oauth._json_body(body, maximum)


@pytest.mark.asyncio
async def test_request_boundary_rejects_invalid_response_and_hides_transport_exception():
    async def malformed(*_args):
        return "200", {}, b"{}"

    async def raises(*_args):
        raise OSError("provider details must not escape")

    with pytest.raises(oauth.RemoteMCPOAuthError, match="response was invalid"):
        await oauth._request(malformed, ISSUER, "GET")
    with pytest.raises(oauth.RemoteMCPOAuthError, match="reached securely") as error:
        await oauth._request(raises, ISSUER, "GET")
    assert "provider details" not in str(error.value)


@pytest.mark.asyncio
async def test_metadata_discovery_rejects_missing_authorization_servers():
    with pytest.raises(oauth.RemoteMCPOAuthError, match="did not advertise"):
        await oauth._authorization_server_metadata(_Requester(), [])
    with pytest.raises(oauth.RemoteMCPOAuthError, match="did not advertise"):
        await oauth._authorization_server_metadata(_Requester(), ISSUER)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ("challenge", "did not request OAuth"),
    ("metadata", "invalid resource metadata URL"),
    ("scopes", "scopes are invalid"),
])
async def test_begin_authorization_denies_bad_challenge_metadata_and_scopes(change, reason):
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    if change == "challenge":
        async def no_oauth(url, method, body, headers, maximum):
            if url == RESOURCE and method == "POST":
                return 200, {}, b"{}"
            return await requester(url, method, body, headers, maximum)
        requester = no_oauth
    elif change == "metadata":
        requester = _Requester(challenge_header='Bearer resource_metadata="https://evil.example/.well-known/oauth-protected-resource/mcp"')
    else:
        requester = _Requester(
            resource_documents={
                "https://mcp.example/.well-known/oauth-protected-resource/mcp":
                    (200, {"resource": RESOURCE, "authorization_servers": [ISSUER], "scopes_supported": "all"}),
            },
            challenge_header='Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp"',
        )
    with pytest.raises(oauth.RemoteMCPOAuthError, match=reason):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)


@pytest.mark.asyncio
async def test_pending_oauth_state_rejects_tampering_expiry_and_cross_owner_payload():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(url).query)["state"][0]

    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code", state[:-2] + "xx", binding=binding, session_owner="owner-1", requester=requester)

    state_expired = orch.credential_manager.seal_transient_state(
        json.dumps({"owner_id": "owner-1", "nonce": "0" * 32}))
    orch.credential_manager.set_credential("owner-1", oauth._PENDING_AGENT,
                                           oauth._PENDING_PREFIX + "0" * 32,
                                           json.dumps({
                                               "owner_id": "owner-2", "connector_id": saved["id"],
                                               "endpoint": RESOURCE, "client_id": "client-1", "client_secret": "",
                                               "token_endpoint": ISSUER + "/token", "redirect_uri": CALLBACK,
                                               "resource": RESOURCE, "verifier": "v", "scope": "",
                                               "expires_at": 0,
                                           }), e2e=False)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code", state_expired, binding=binding, session_owner="owner-1", requester=requester)

    state_other = orch.credential_manager.seal_transient_state(
        json.dumps({"owner_id": "owner-2", "nonce": "1" * 32}))
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code", state_other, binding=binding, session_owner="owner-1", requester=requester)

    state_bad_nonce = orch.credential_manager.seal_transient_state(
        json.dumps({"owner_id": "owner-1", "nonce": "not-a-nonce"}))
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code", state_bad_nonce, binding=binding, session_owner="owner-1", requester=requester)
    for invalid_state in ("", "short"):
        with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
            await oauth.complete_authorization(orch, "code", invalid_state, binding=binding, session_owner="owner-1", requester=requester)


@pytest.mark.asyncio
async def test_complete_authorization_consumes_state_before_rejecting_invalid_code_or_changed_connector():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(oauth.RemoteMCPOAuthError, match="code was invalid"):
        await oauth.complete_authorization(orch, "bad\ncode", state, binding=binding, session_owner="owner-1", requester=requester)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "valid", state, binding=binding, session_owner="owner-1", requester=requester)

    url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    changed = connectors.get_connector(orch, "owner-1", saved["id"])
    changed["oauth_client_id"] = "replacement-client"
    connectors.save_connector(orch, "owner-1", changed)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="connector changed"):
        await oauth.complete_authorization(orch, "valid", state, binding=binding, session_owner="owner-1", requester=requester)


@pytest.mark.asyncio
async def test_complete_authorization_rejects_client_secret_rotation_during_sign_in():
    orch = _Orch()
    saved = _connector(orch)
    saved["oauth_client_secret"] = "secret-old"
    connectors.save_connector(orch, "owner-1", saved)
    requester = _Requester(token_auth_methods=["client_secret_basic"])
    url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    changed = connectors.get_connector(orch, "owner-1", saved["id"])
    changed["oauth_client_secret"] = "secret-new"
    connectors.save_connector(orch, "owner-1", changed)

    with pytest.raises(oauth.RemoteMCPOAuthError, match="connector changed"):
        await oauth.complete_authorization(orch, "valid", state, binding=binding, session_owner="owner-1", requester=requester)
    assert not connectors.get_connector(orch, "owner-1", saved["id"]).get("access_token")


@pytest.mark.asyncio
@pytest.mark.parametrize("token,reason", [
    ({"token_type": "mac"}, "omitted access token"),
    ({"access_token": "a", "token_type": "mac"}, "unsupported token type"),
    ({"access_token": "a", "refresh_token": 1}, "invalid refresh token"),
])
async def test_exchange_rejects_invalid_token_payloads(token, reason):
    orch = _Orch()
    saved = _connector(orch)

    class TokenRequester(_Requester):
        async def __call__(self, url, method, body, headers, maximum):
            if url == ISSUER + "/token":
                return 200, {}, json.dumps(token).encode()
            return await super().__call__(url, method, body, headers, maximum)

    requester = TokenRequester()
    url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(oauth.RemoteMCPOAuthError, match=reason):
        await oauth.complete_authorization(orch, "valid-code", state, binding=binding, session_owner="owner-1", requester=requester)


@pytest.mark.asyncio
async def test_refresh_rotation_and_rejection_preserve_owner_scoping():
    orch = _Orch()
    saved = _connector(orch)
    saved.update({"refresh_token": "refresh-old", "token_endpoint": ISSUER + "/token"})
    connectors.save_connector(orch, "owner-1", saved)

    class RefreshRequester(_Requester):
        token_body = b'{"access_token":"access-2","refresh_token":"refresh-new","expires_in":1800}'

        async def __call__(self, url, method, body, headers, maximum):
            if url == ISSUER + "/token":
                return self.token_status, {}, self.token_body
            return await super().__call__(url, method, body, headers, maximum)

    requester = RefreshRequester()
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
    updated = connectors.get_connector(orch, "owner-1", saved["id"])
    assert updated["access_token"] == "access-2"
    assert updated["refresh_token"] == "refresh-new"
    assert updated["token_expires_at"] > 0
    with pytest.raises(connectors.RemoteMCPError, match="not found"):
        connectors.get_connector(orch, "owner-2", saved["id"])

    requester.token_body = b'{"access_token":"access-3"}'
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
    assert connectors.get_connector(orch, "owner-1", saved["id"])["refresh_token"] == "refresh-new"
    requester.token_status = 400
    with pytest.raises(oauth.RemoteMCPOAuthError, match="rejected token refresh"):
        await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)


@pytest.mark.asyncio
async def test_refresh_without_oauth_refresh_token_is_a_noop():
    orch = _Orch()
    saved = _connector(orch)
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=_Requester()) is False


@pytest.mark.asyncio
async def test_refresh_accepts_a_concurrent_successful_rotation_after_rejection():
    orch = _Orch()
    saved = _connector(orch)
    saved.update({"refresh_token": "refresh-old", "token_endpoint": ISSUER + "/token"})
    connectors.save_connector(orch, "owner-1", saved)

    class ConcurrentRefreshRequester(_Requester):
        async def __call__(self, url, method, body, headers, maximum):
            if url == ISSUER + "/token":
                current = connectors.get_connector(orch, "owner-1", saved["id"])
                current.update({"access_token": "access-rotated", "refresh_token": "refresh-rotated",
                                "token_expires_at": oauth.time.time() + 3600})
                connectors.save_connector(orch, "owner-1", current)
                return 400, {}, b'{"error":"invalid_grant"}'
            return await super().__call__(url, method, body, headers, maximum)

    assert await oauth.refresh_authorization(
        orch, "owner-1", saved["id"], requester=ConcurrentRefreshRequester()) is True
    current = connectors.get_connector(orch, "owner-1", saved["id"])
    assert current["refresh_token"] == "refresh-rotated"
    assert current["access_token"] == "access-rotated"


@pytest.mark.asyncio
async def test_pending_state_expires_and_pending_limit_prunes_old_entries(monkeypatch):
    orch = _Orch()
    saved = _connector(orch)
    manager = orch.credential_manager
    manager.set_credential("owner-1", oauth._PENDING_AGENT, "_pending_expired", "bad-json", e2e=False)
    for index in range(oauth._MAX_PENDING):
        manager.set_credential("owner-1", oauth._PENDING_AGENT, f"_pending_{index}",
                               json.dumps({"expires_at": 10**20}), e2e=False)
    requester = _Requester()
    with pytest.raises(oauth.RemoteMCPOAuthError, match="Too many"):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)
    assert "_pending_expired" not in manager.list_credential_keys("owner-1", oauth._PENDING_AGENT)


def test_plane_pending_credential_take_is_atomic_across_workers(plane_template, monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from tests.helpers.plane_template import engine_clone

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with engine_clone(plane_template) as (runtime, _database):
        workers = [CredentialManager(plane_runtime=runtime, data_dir=str(tmp_path)) for _ in range(2)]
        workers[0].set_credential("oauth-owner", "oauth-pending", "state", "pkce-verifier", e2e=False)
        barrier = Barrier(2)

        def take(manager):
            barrier.wait()
            return manager.take_credential("oauth-owner", "oauth-pending", "state")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(take, workers))

    assert sorted(result is None for result in results) == [False, True]
    assert "pkce-verifier" in results


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["callback", "refresh", "registration"])
async def test_oauth_response_preserves_settings_changed_during_provider_request(operation):
    orch = _Orch()
    saved = _connector(orch, client_id="" if operation == "registration" else "client-1")
    if operation == "refresh":
        current = connectors.get_connector(orch, "owner-1", saved["id"])
        current.update({"refresh_token": "refresh-old", "token_endpoint": ISSUER + "/token"})
        connectors.save_connector(orch, "owner-1", current)
    base_requester = _Requester()
    state = ""
    if operation == "callback":
        url, binding = await _authorize(orch, saved["id"], base_requester)
        state = parse_qs(urlsplit(url).query)["state"][0]

    async def requester(url, method, body, headers, maximum):
        if url == ISSUER + ("/register" if operation == "registration" else "/token"):
            current = connectors.get_connector(orch, "owner-1", saved["id"])
            current.update({"enabled": False, "name": "User edited", "tools": {"echo": False}})
            connectors.save_connector(orch, "owner-1", current)
        return await base_requester(url, method, body, headers, maximum)

    if operation == "callback":
        await oauth.complete_authorization(orch, "code", state, binding=binding, session_owner="owner-1", requester=requester)
    elif operation == "refresh":
        await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
    else:
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)
    current = connectors.get_connector(orch, "owner-1", saved["id"])
    assert current["enabled"] is False
    assert current["name"] == "User edited"
    assert current["tools"] == {"echo": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["callback", "refresh", "registration"])
@pytest.mark.parametrize("mutation", [
    "delete", "url", "oauth_client_id", "oauth_client_secret", "oauth_token_endpoint_auth_method",
])
async def test_oauth_response_cannot_recreate_or_restore_changed_connector(operation, mutation):
    orch = _Orch()
    saved = _connector(orch, client_id="" if operation == "registration" else "client-1",
                       client_secret="secret")
    if operation == "refresh":
        current = connectors.get_connector(orch, "owner-1", saved["id"])
        current.update({"refresh_token": "refresh-old", "token_endpoint": ISSUER + "/token"})
        connectors.save_connector(orch, "owner-1", current)
    base_requester = _Requester(token_auth_methods=["client_secret_basic", "client_secret_post"],
                                registered_method="client_secret_basic", registered_secret="issued-secret")
    state = ""
    if operation == "callback":
        url, binding = await _authorize(orch, saved["id"], base_requester)
        state = parse_qs(urlsplit(url).query)["state"][0]

    async def requester(url, method, body, headers, maximum):
        if url == ISSUER + ("/register" if operation == "registration" else "/token"):
            if mutation == "delete":
                connectors.delete_connector(orch, "owner-1", saved["id"])
            else:
                current = connectors.get_connector(orch, "owner-1", saved["id"])
                if mutation == "oauth_token_endpoint_auth_method":
                    current[mutation] = ("client_secret_post" if operation == "callback"
                                         else "client_secret_basic")
                else:
                    current[mutation] = "https://new.example/mcp" if mutation == "url" else "replacement"
                connectors.save_connector(orch, "owner-1", current)
        return await base_requester(url, method, body, headers, maximum)

    with pytest.raises((oauth.RemoteMCPOAuthError, connectors.RemoteMCPError)):
        if operation == "callback":
            await oauth.complete_authorization(orch, "code", state, binding=binding, session_owner="owner-1", requester=requester)
        elif operation == "refresh":
            await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
        else:
            await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)
    if mutation == "delete":
        assert connectors.list_connectors(orch, "owner-1") == []
    else:
        current = connectors.get_connector(orch, "owner-1", saved["id"])
        expected = ("https://new.example/mcp" if mutation == "url" else
                    "client_secret_post" if mutation == "oauth_token_endpoint_auth_method" and operation == "callback" else
                    "client_secret_basic" if mutation == "oauth_token_endpoint_auth_method" else "replacement")
        assert current[mutation] == expected
        assert not current.get("access_token")


@pytest.mark.asyncio
async def test_refresh_success_cannot_overwrite_a_concurrently_rotated_token():
    orch = _Orch()
    saved = _connector(orch)
    saved.update({"refresh_token": "refresh-old", "token_endpoint": ISSUER + "/token"})
    connectors.save_connector(orch, "owner-1", saved)

    async def requester(url, method, body, headers, maximum):
        current = connectors.get_connector(orch, "owner-1", saved["id"])
        current.update({"access_token": "concurrent-access", "refresh_token": "concurrent-refresh",
                        "token_expires_at": oauth.time.time() + 3600})
        connectors.save_connector(orch, "owner-1", current)
        return 200, {}, b'{"access_token":"late-access","refresh_token":"late-refresh","expires_in":3600}'

    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester)
    current = connectors.get_connector(orch, "owner-1", saved["id"])
    assert current["access_token"] == "concurrent-access"
    assert current["refresh_token"] == "concurrent-refresh"


def test_oauth_credential_update_retries_settings_only_conflict_and_bounds_contention(monkeypatch):
    orch = _Orch()
    saved = _connector(orch)
    expected = connectors.get_connector(orch, "owner-1", saved["id"])
    manager = orch.credential_manager
    compare_and_set = manager.compare_and_set_credential
    raced = False

    def race(owner, agent, key, raw, value):
        nonlocal raced
        if not raced:
            raced = True
            current = connectors.get_connector(orch, "owner-1", saved["id"])
            current["enabled"] = False
            manager.set_credential(owner, agent, key, json.dumps(current), e2e=False)
            return False
        return compare_and_set(owner, agent, key, raw, value)

    monkeypatch.setattr(manager, "compare_and_set_credential", race)
    current = connectors.update_oauth_credentials(orch, "owner-1", expected, {"access_token": "new"})
    assert current["enabled"] is False
    assert current["access_token"] == "new"
    expected = current
    monkeypatch.setattr(manager, "compare_and_set_credential", lambda *_args: False)
    with pytest.raises(connectors.RemoteMCPError, match="changed"):
        connectors.update_oauth_credentials(orch, "owner-1", expected, {"access_token": "late"})
    assert connectors.get_connector(orch, "owner-1", saved["id"])["access_token"] == "new"
    with pytest.raises(connectors.RemoteMCPError, match="invalid"):
        connectors.update_oauth_credentials(orch, "owner-1", expected, {"enabled": True})


def _pending_record(orch, owner="owner-1"):
    manager = orch.credential_manager
    [key] = manager.list_credential_keys(owner, oauth._PENDING_AGENT)
    return key, json.loads(manager.get_credential(owner, oauth._PENDING_AGENT, key))


def _token_calls(requester):
    return [url for url, _method, _body in requester.calls if url == ISSUER + "/token"]


@pytest.mark.asyncio
async def test_begin_returns_only_a_server_start_url_and_bind_reveals_provider_url_once():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    start_url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)
    state = parse_qs(urlsplit(start_url).query)["state"][0]
    assert parse_qs(urlsplit(start_url).query) == {"state": [state]}
    assert _pending_record(orch)[1]["binding"] == ""

    with pytest.raises(oauth.RemoteMCPOAuthError, match="different AstralDeep account"):
        await oauth.bind_authorization(orch, "owner-2", state)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="different AstralDeep account"):
        await oauth.bind_authorization(orch, None, state)
    authorization_url, binding = await oauth.bind_authorization(orch, "owner-1", state)

    query = parse_qs(urlsplit(authorization_url).query)
    _key, record = _pending_record(orch)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(record["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert authorization_url.startswith(ISSUER + "/authorize?")
    assert query["state"] == [state]
    assert query["code_challenge"] == [challenge]
    assert query["redirect_uri"] == [CALLBACK]
    assert record["binding"] == hashlib.sha256(binding.encode()).hexdigest()
    assert binding not in json.dumps(record)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="already opened in a browser"):
        await oauth.bind_authorization(orch, "owner-1", state)
    assert _pending_record(orch)[1] == record
    assert await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1",
                                              requester=requester) == saved["id"]


@pytest.mark.asyncio
async def test_bind_refuses_when_a_concurrent_writer_changed_the_pending_record(monkeypatch):
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    start_url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)
    state = parse_qs(urlsplit(start_url).query)["state"][0]
    before = _pending_record(orch)
    monkeypatch.setattr(orch.credential_manager, "compare_and_set_credential", lambda *_args: False)

    with pytest.raises(oauth.RemoteMCPOAuthError, match="already opened in a browser"):
        await oauth.bind_authorization(orch, "owner-1", state)
    assert _pending_record(orch) == before


@pytest.mark.asyncio
async def test_begin_rejects_an_unsafe_start_url_before_contacting_the_server():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    with pytest.raises(oauth.RemoteMCPOAuthError, match="public HTTPS"):
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK,
                                        "http://astral.example/api/remote-mcp/oauth/start", requester=requester)
    assert requester.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("presented", [None, "", "short", "x" * 129, "é" * 43, "wrong"])
async def test_callback_without_the_starting_browser_binding_is_refused_and_consumed(presented):
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    if presented == "wrong":
        presented = secrets.token_urlsafe(32)

    with pytest.raises(oauth.RemoteMCPOAuthError, match="not started in this browser"):
        await oauth.complete_authorization(orch, "code-1", state, binding=presented, session_owner="owner-1", requester=requester)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)
    assert _token_calls(requester) == []
    assert not connectors.get_connector(orch, "owner-1", saved["id"]).get("access_token")


@pytest.mark.asyncio
@pytest.mark.parametrize("session_owner,accepted", [
    (None, False), ("owner-2", False), ("", False), ("owner-1", True),
])
async def test_callback_web_session_must_belong_to_the_flow_owner(session_owner, accepted):
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]

    if accepted:
        assert await oauth.complete_authorization(orch, "code-1", state, binding=binding,
                                                  session_owner=session_owner, requester=requester) == saved["id"]
        assert connectors.get_connector(orch, "owner-1", saved["id"])["access_token"] == "access-1"
        return
    with pytest.raises(oauth.RemoteMCPOAuthError, match="not started in this browser"):
        await oauth.complete_authorization(orch, "code-1", state, binding=binding,
                                           session_owner=session_owner, requester=requester)
    assert _token_calls(requester) == []
    assert not connectors.get_connector(orch, "owner-1", saved["id"]).get("access_token")


@pytest.mark.asyncio
async def test_bind_and_callback_refuse_tampered_expired_and_unbound_pending_state():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    manager = orch.credential_manager
    start_url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START, requester=requester)
    state = parse_qs(urlsplit(start_url).query)["state"][0]
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.bind_authorization(orch, "owner-1", state[:-2] + "xx")
    key, record = _pending_record(orch)
    manager.set_credential("owner-1", oauth._PENDING_AGENT, key,
                           json.dumps({**record, "expires_at": 0}), e2e=False)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.bind_authorization(orch, "owner-1", state)

    authorization_url, binding = await _authorize(orch, saved["id"], requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    key, record = _pending_record(orch)
    manager.set_credential("owner-1", oauth._PENDING_AGENT, key,
                           json.dumps({**record, "expires_at": 0}), e2e=False)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code-1", state, binding=binding, session_owner="owner-1", requester=requester)

    legacy = {"owner_id": "owner-1", "connector_id": saved["id"], "endpoint": RESOURCE,
              "client_id": "client-1", "client_secret": "", "token_endpoint": ISSUER + "/token",
              "redirect_uri": CALLBACK, "resource": RESOURCE, "verifier": "v", "scope": "",
              "expires_at": 10**20}
    for nonce in ("2" * 32, "3" * 32):
        manager.set_credential("owner-1", oauth._PENDING_AGENT, oauth._PENDING_PREFIX + nonce,
                               json.dumps(legacy), e2e=False)
    legacy_bind = manager.seal_transient_state(json.dumps({"owner_id": "owner-1", "nonce": "2" * 32}))
    legacy_callback = manager.seal_transient_state(json.dumps({"owner_id": "owner-1", "nonce": "3" * 32}))
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.bind_authorization(orch, "owner-1", legacy_bind)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="not started in this browser"):
        await oauth.complete_authorization(orch, "code-1", legacy_callback, binding=binding,
                                           session_owner="owner-1", requester=requester)
    assert _token_calls(requester) == []
    assert manager.list_credential_keys("owner-1", oauth._PENDING_AGENT) == [oauth._PENDING_PREFIX + "2" * 32]


class _Browser:
    def __init__(self, owner=None):
        self.owner = owner
        self.cookies = {}

    def request(self, path, **query):
        cookies = dict(self.cookies)
        if self.owner:
            cookies["astral_session"] = "session-" + self.owner
        headers = [(b"host", b"astral.example"), (b"accept", b"text/html")]
        if cookies:
            headers.append((b"cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode()))
        return StarletteRequest({"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
                                 "query_string": urlencode(query).encode(), "headers": headers,
                                 "root_path": "", "client": ("127.0.0.1", 1),
                                 "server": ("astral.example", 443), "scheme": "https", "http_version": "1.1"})

    def keep(self, response):
        for name, value in response.raw_headers:
            if name == b"set-cookie":
                key, _separator, rest = value.decode().partition("=")
                cookie_value = rest.split(";", 1)[0]
                if cookie_value in ("", '""'):
                    self.cookies.pop(key, None)
                else:
                    self.cookies[key] = cookie_value
        return response

    async def start(self, state):
        return self.keep(await remote_mcp_api.remote_mcp_oauth_start(
            self.request("/api/remote-mcp/oauth/start", state=state), state=state))

    async def callback(self, code, state):
        return self.keep(await remote_mcp_api.remote_mcp_oauth_callback(
            self.request("/api/remote-mcp/oauth/callback", code=code, state=state), code=code, state=state))


@pytest.fixture
def browser_flow(monkeypatch):
    from orchestrator import auth, web_auth

    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()

    async def session(request):
        cookie = request.cookies.get("astral_session", "")
        owner = cookie.removeprefix("session-")
        return {"access_token": "token-" + owner, "sub": owner} if cookie else None

    async def payload(_request, credentials):
        return {"sub": credentials.credentials.removeprefix("token-"), "realm_access": {"roles": ["user"]}}

    monkeypatch.setattr(web_auth, "ensure_session", session)
    monkeypatch.setattr(web_auth, "aget_session", session)
    monkeypatch.setattr(auth, "get_current_user_payload", payload)
    monkeypatch.setattr(remote_mcp_api, "_get_orchestrator", lambda request: orch)
    monkeypatch.setattr(connectors, "http_request", requester)

    async def begin():
        start_url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, START,
                                                    requester=requester)
        return parse_qs(urlsplit(start_url).query)["state"][0]

    return SimpleNamespace(orch=orch, saved=saved, requester=requester, begin=begin)


@pytest.mark.asyncio
async def test_owner_browser_completes_flow_it_started(browser_flow):
    state = await browser_flow.begin()
    owner = _Browser("owner-1")

    started = await owner.start(state)
    assert started.status_code == 303
    assert parse_qs(urlsplit(started.headers["location"]).query)["state"] == [state]
    assert list(owner.cookies) == [remote_mcp_api._binding_cookie(state)]
    done = await owner.callback("code-1", state)

    assert done.status_code == 200
    assert owner.cookies == {}
    stored = connectors.get_connector(browser_flow.orch, "owner-1", browser_flow.saved["id"])
    assert stored["access_token"] == "access-1"
    replay = await owner.callback("code-1", state)
    assert replay.status_code == 400
    assert len(_token_calls(browser_flow.requester)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("victim_session", [None, "victim"])
async def test_forwarded_provider_link_cannot_land_victim_authorization_in_initiator_connector(
        browser_flow, victim_session):
    state = await browser_flow.begin()
    attacker = _Browser("owner-1")
    assert (await attacker.start(state)).status_code == 303
    victim = _Browser(victim_session)

    landed = await victim.callback("victim-code", state)
    late = await attacker.callback("victim-code", state)

    assert landed.status_code == 400
    assert late.status_code == 400
    assert _token_calls(browser_flow.requester) == []
    assert not connectors.get_connector(browser_flow.orch, "owner-1", browser_flow.saved["id"]).get("access_token")


@pytest.mark.asyncio
async def test_forwarded_start_link_requires_the_owner_signed_in_in_that_browser(browser_flow):
    state = await browser_flow.begin()
    signed_out = _Browser()
    sign_in = await signed_out.start(state)
    assert sign_in.status_code == 303
    assert sign_in.headers["location"].startswith("/auth/login?next=")
    victim = _Browser("victim")
    refused = await victim.start(state)
    assert refused.status_code == 400
    assert signed_out.cookies == {} and victim.cookies == {}

    owner = _Browser("owner-1")
    assert (await owner.start(state)).status_code == 303
    assert (await victim.callback("victim-code", state)).status_code == 400
    assert _token_calls(browser_flow.requester) == []


@pytest.mark.asyncio
async def test_reopening_the_start_link_keeps_the_first_browser_flow(browser_flow):
    state = await browser_flow.begin()
    first = _Browser("owner-1")
    second = _Browser("owner-1")
    assert (await first.start(state)).status_code == 303

    assert (await second.start(state)).status_code == 400
    assert second.cookies == {}
    assert (await first.callback("code-1", state)).status_code == 200
    assert connectors.get_connector(browser_flow.orch, "owner-1", browser_flow.saved["id"])["access_token"] == "access-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("later_owner", ["owner-2", None])
async def test_binding_cookie_is_refused_after_the_browser_switches_or_leaves_account(browser_flow, later_owner):
    state = await browser_flow.begin()
    browser = _Browser("owner-1")
    assert (await browser.start(state)).status_code == 303
    browser.owner = later_owner

    assert (await browser.callback("code-1", state)).status_code == 400
    assert _token_calls(browser_flow.requester) == []
    assert not connectors.get_connector(browser_flow.orch, "owner-1", browser_flow.saved["id"]).get("access_token")
