"""Exercises owner-bound MCP OAuth discovery, PKCE exchange, refresh, and refusal.
Network requests use a deterministic fake while the connector store uses its real API.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet, InvalidToken
from orchestrator import remote_mcp_connectors as connectors
from orchestrator import remote_mcp_oauth as oauth
from orchestrator.credential_manager import CredentialManager
from orchestrator.projection_surfaces import remote_mcp as remote_mcp_surface

RESOURCE = "https://mcp.example/mcp"
ISSUER = "https://id.example"
CALLBACK = "https://astral.example/api/remote-mcp/oauth/callback"


class _Credentials:
    def __init__(self):
        self.rows = {}
        self.fernet = Fernet(Fernet.generate_key())

    def get_credential(self, owner, agent, key):
        return self.rows.get((owner, agent, key))

    def set_credential(self, owner, agent, key, value, *, e2e):
        assert e2e is False
        self.rows[(owner, agent, key)] = value

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
                 scopes_supported=("user",), resource_documents=None, authorization_documents=None):
        self.resource = resource
        self.challenge_origin = challenge_origin
        self.token_status = token_status
        self.authorization_query = authorization_query
        self.challenge_header = challenge_header
        self.authorization_servers = authorization_servers
        self.scopes_supported = scopes_supported
        self.resource_documents = resource_documents or {}
        self.authorization_documents = authorization_documents or {}
        self.calls = []

    async def __call__(self, url, method, body, headers, maximum):
        self.calls.append((url, method, body))
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
            return 200, {}, json.dumps({
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize" + self.authorization_query,
                "token_endpoint": ISSUER + "/token",
                "registration_endpoint": ISSUER + "/register",
                "code_challenge_methods_supported": ["S256"],
            }).encode()
        if url == ISSUER + "/register":
            assert method == "POST" and b"AstralDeep Remote MCP" in body
            return 201, {}, b'{"client_id":"registered-client"}'
        if url == ISSUER + "/token":
            if self.token_status != 200:
                return self.token_status, {}, b'{"error":"invalid_grant"}'
            assert method == "POST" and b"client_id=" in body
            return 200, {}, b'{"access_token":"access-1","refresh_token":"refresh-1","token_type":"Bearer","expires_in":3600}'
        raise AssertionError((url, method))


def _connector(orch, *, client_id="client-1"):
    return connectors.save_connector(orch, "owner-1", {
        "name": "Example MCP", "url": RESOURCE, "transport": "streamable_http",
        "auth_type": "oauth", "oauth_client_id": client_id, "enabled": True, "tools": {},
    })


def test_saving_connector_clears_oauth_client_and_authorization_state_only_when_requested():
    orch = _Orch()
    saved = _connector(orch)
    connector = connectors.get_connector(orch, "owner-1", saved["id"])
    connector.update({"oauth_client_secret": "client-secret", "access_token": "access",
                      "refresh_token": "refresh", "token_endpoint": ISSUER + "/token",
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
        "access_token", "refresh_token", "token_endpoint", "authorization_endpoint", "token_expires_at", "scope"))


def test_clear_oauth_client_credentials_requires_boolean():
    orch = _Orch()
    with pytest.raises(connectors.RemoteMCPError, match="must be a boolean"):
        connectors.save_connector(orch, "owner-1", {
            "name": "Example MCP", "url": RESOURCE, "transport": "streamable_http",
            "auth_type": "oauth", "oauth_client_id": "client-1",
        }, clear_oauth_client_credentials="true")


def test_remote_mcp_edit_surfaces_expose_explicit_oauth_client_clear(monkeypatch):
    monkeypatch.setattr(remote_mcp_surface, "_enabled", lambda: True)
    monkeypatch.setattr(connectors, "list_connectors", lambda *_args: [])
    html = asyncio.run(remote_mcp_surface.render(_Orch(), "owner-1", [], {}))
    assert 'name="clear_oauth_client_credentials" type="checkbox" value="true"' in html

    components = asyncio.run(remote_mcp_surface.components(_Orch(), "owner-1", [], {}))
    form = next(component for component in components if component.get("submit_action") == "chrome_remote_mcp_save")
    clear_field = next(field for field in form["fields"] if field["name"] == "clear_oauth_client_credentials")
    assert clear_field["kind"] == "boolean"
    assert clear_field["default"] is False


def test_remote_mcp_save_action_forwards_explicit_oauth_clear(monkeypatch):
    connector_id = "a" * 32
    saved = {}
    monkeypatch.setattr(remote_mcp_surface, "_enabled", lambda: True)
    monkeypatch.setattr(connectors, "get_connector", lambda *_args: {"tools": {}, "tools_catalog": [], "enabled": True})

    def save(_orch, _owner, _config, *, clear_oauth_client_credentials=False):
        saved["clear"] = clear_oauth_client_credentials

    monkeypatch.setattr(connectors, "save_connector", save)
    asyncio.run(remote_mcp_surface._save(None, None, "owner-1", [], {
        "connector_id": connector_id,
        "fields": {"name": "Example MCP", "url": RESOURCE, "transport": "streamable_http",
                   "auth_type": "oauth", "clear_oauth_client_credentials": "true"},
    }))
    assert saved["clear"] is True


@pytest.mark.asyncio
async def test_pkce_exchange_stores_owner_token_and_consumes_state():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()

    authorization_url = await oauth.begin_authorization(
        orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    query = parse_qs(urlsplit(authorization_url).query)

    assert query["client_id"] == ["client-1"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [RESOURCE]
    assert query["scope"] == ["user"]
    state = query["state"][0]
    callback_worker = _Orch()
    callback_worker.credential_manager = orch.credential_manager
    assert await oauth.complete_authorization(callback_worker, "code-1", state, requester=requester) == saved["id"]
    stored = connectors.get_connector(orch, "owner-1", saved["id"])
    assert stored["access_token"] == "access-1"
    assert stored["refresh_token"] == "refresh-1"
    assert stored["token_endpoint"] == ISSUER + "/token"
    assert stored["token_expires_at"] > 0
    assert connectors.list_connectors(orch, "other-owner") == []
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code-1", state, requester=requester)


@pytest.mark.asyncio
async def test_dynamic_registration_and_refresh():
    orch = _Orch()
    saved = _connector(orch, client_id="")
    requester = _Requester()

    authorization_url = await oauth.begin_authorization(
        orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    assert parse_qs(urlsplit(authorization_url).query)["client_id"] == ["registered-client"]
    assert connectors.get_connector(orch, "owner-1", saved["id"])["oauth_client_id"] == "registered-client"
    await oauth.complete_authorization(orch, "code-1", state, requester=requester)
    assert await oauth.refresh_authorization(orch, "owner-1", saved["id"], requester=requester) is True
    assert [url for url, method, _ in requester.calls if url == ISSUER + "/token" and method == "POST"] == [
        ISSUER + "/token", ISSUER + "/token"]


@pytest.mark.asyncio
async def test_authorization_endpoint_preserves_provider_query():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester(authorization_query="?tenant=demo")
    authorization_url = await oauth.begin_authorization(
        orch, "owner-1", saved["id"], CALLBACK, requester=requester)
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

    authorization_url = await oauth.begin_authorization(
        orch, "owner-1", saved["id"], CALLBACK, requester=requester)

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

    authorization_url = await oauth.begin_authorization(
        orch, "owner-1", saved["id"], CALLBACK, requester=requester)

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
    }
    requester = _Requester(
        authorization_servers=(issuer,),
        authorization_documents={path_urls[0]: (404, None), path_urls[1]: (404, None),
                                 path_urls[2]: (200, metadata)},
    )

    await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)

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
    }
    requester = _Requester(authorization_documents={oauth_url: (404, None), oidc_url: (200, metadata)})

    await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)

    assert [call[0] for call in requester.calls if call[0] in (oauth_url, oidc_url)] == [oauth_url, oidc_url]

    for methods in (None, ["plain"]):
        invalid_metadata = dict(metadata)
        if methods is None:
            invalid_metadata.pop("code_challenge_methods_supported")
        else:
            invalid_metadata["code_challenge_methods_supported"] = methods
        requester = _Requester(authorization_documents={oauth_url: (200, invalid_metadata)})
        with pytest.raises(oauth.RemoteMCPOAuthError, match="PKCE S256"):
            await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)


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

    authorization_url = await oauth.begin_authorization(
        orch, "owner-1", saved["id"], CALLBACK, requester=requester)

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
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)


@pytest.mark.asyncio
async def test_failed_token_exchange_does_not_store_access_token():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester(token_status=400)
    authorization_url = await oauth.begin_authorization(
        orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]

    with pytest.raises(oauth.RemoteMCPOAuthError, match="rejected"):
        await oauth.complete_authorization(orch, "bad-code", state, requester=requester)
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
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)


@pytest.mark.asyncio
async def test_pending_oauth_state_rejects_tampering_expiry_and_cross_owner_payload():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    state = parse_qs(urlsplit(url).query)["state"][0]

    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code", state[:-2] + "xx", requester=requester)

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
        await oauth.complete_authorization(orch, "code", state_expired, requester=requester)

    state_other = orch.credential_manager.seal_transient_state(
        json.dumps({"owner_id": "owner-2", "nonce": "1" * 32}))
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code", state_other, requester=requester)

    state_bad_nonce = orch.credential_manager.seal_transient_state(
        json.dumps({"owner_id": "owner-1", "nonce": "not-a-nonce"}))
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "code", state_bad_nonce, requester=requester)
    for invalid_state in ("", "short"):
        with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
            await oauth.complete_authorization(orch, "code", invalid_state, requester=requester)


@pytest.mark.asyncio
async def test_complete_authorization_consumes_state_before_rejecting_invalid_code_or_changed_connector():
    orch = _Orch()
    saved = _connector(orch)
    requester = _Requester()
    url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(oauth.RemoteMCPOAuthError, match="code was invalid"):
        await oauth.complete_authorization(orch, "bad\ncode", state, requester=requester)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="expired"):
        await oauth.complete_authorization(orch, "valid", state, requester=requester)

    url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    changed = connectors.get_connector(orch, "owner-1", saved["id"])
    changed["oauth_client_id"] = "replacement-client"
    connectors.save_connector(orch, "owner-1", changed)
    with pytest.raises(oauth.RemoteMCPOAuthError, match="connector changed"):
        await oauth.complete_authorization(orch, "valid", state, requester=requester)


@pytest.mark.asyncio
async def test_complete_authorization_rejects_client_secret_rotation_during_sign_in():
    orch = _Orch()
    saved = _connector(orch)
    saved["oauth_client_secret"] = "secret-old"
    connectors.save_connector(orch, "owner-1", saved)
    requester = _Requester()
    url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    changed = connectors.get_connector(orch, "owner-1", saved["id"])
    changed["oauth_client_secret"] = "secret-new"
    connectors.save_connector(orch, "owner-1", changed)

    with pytest.raises(oauth.RemoteMCPOAuthError, match="connector changed"):
        await oauth.complete_authorization(orch, "valid", state, requester=requester)
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
    url = await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(oauth.RemoteMCPOAuthError, match=reason):
        await oauth.complete_authorization(orch, "valid-code", state, requester=requester)


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
        await oauth.begin_authorization(orch, "owner-1", saved["id"], CALLBACK, requester=requester)
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
