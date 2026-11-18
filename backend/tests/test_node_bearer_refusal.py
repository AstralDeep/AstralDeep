
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import jose.jwt
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from orchestrator import a2a_orchestrator_executor as a2a, auth, mcp_authz
from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_worker_endpoint import WORKER_CONTROL_PATH
from shared import jwks_cache, node_identity as ni
from shared.a2a_security import A2ASecurityValidator
from shared.token_verify import TokenRefused, verify_idp_token
from tests import test_node_route_census as census
from tests.helpers import orchestrator_app
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

BEARER = "node.bearer_refused"
MCP_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "node-test", "version": "1"},
}
A2A_BODY = {
    "jsonrpc": "2.0", "id": "r1", "method": "SendMessage",
    "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hello"}]}},
}

REFUSED = {"detail": BEARER}
VOICE_REFUSED = {"error": "voice_unavailable", "reason": "authentication_required", "retryable": False}
REST_DOORS = [
    ("GET", "/api/connection/status", 401, REFUSED),
    ("GET", "/api/share", 401, REFUSED),
    ("GET", "/api/download/s1/f.txt", 401, REFUSED),
    ("POST", "/api/audit/session-resume-failed", 401, REFUSED),
    ("GET", "/api/emergency-stop", 401, REFUSED),
    ("GET", "/api/chrome/commands", 401, REFUSED),
    ("GET", "/api/work/v1/operations/op-1", 401, {"error": "work_authentication_required"}),
    ("POST", "/api/work/v1/operations", 401, {"error": "work_authentication_required"}),
    ("POST", "/api/work/v1/operations/op-1/pause", 401, {"error": "work_authentication_required"}),
    ("POST", "/api/export/canvas/c1/presentation", 401, {"error": "presentation_authentication_required"}),
    ("GET", "/api/persistent-agents", 401, {"error": "assignment_authentication_required"}),
    ("GET", "/api/voice/v2/status", 401, VOICE_REFUSED),
]
SIGN_IN_DOORS = [
    ("GET", "/auth/login"),
    ("GET", "/auth/callback?code=c&state=s"),
    ("GET", "/auth/session"),
    ("GET", "/auth/logout"),
    ("POST", "/auth/logout"),
    ("GET", "/auth/error"),
    ("GET", "/kiosk"),
    ("POST", "/auth/kiosk/start"),
    ("POST", "/auth/kiosk/poll"),
    ("POST", "/auth/token"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/device/start"),
    ("POST", "/api/auth/device/poll"),
    ("POST", "/api/auth/device/refresh"),
]
LEGACY_DOORS = [
    ("POST", "/api/mesh/invitations"),
    ("GET", "/api/mesh/invitations"),
    ("GET", "/api/mesh/members"),
    ("GET", "/api/mesh/me"),
    ("GET", "/api/mesh/topology"),
    ("POST", "/api/mesh/enrollment/redeem"),
    ("POST", "/api/mesh/nonce"),
    ("POST", "/api/mesh/session"),
    ("POST", "/api/mesh/tools/agent/tool"),
    ("GET", "/enroll"),
    ("POST", "/api/institution/pairing"),
]


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("node_bearer_refusal") as runtime:
        yield runtime


@pytest.fixture
def spies(monkeypatch):
    seen = {"jwks": 0, "parse": 0}
    fetch = jwks_cache.get_jwks

    async def counted(*args, **kwargs):
        seen["jwks"] += 1
        return await fetch(*args, **kwargs)

    monkeypatch.setattr(jwks_cache, "get_jwks", counted)
    monkeypatch.setattr(mcp_authz, "get_jwks", counted)
    for name in ("decode", "get_unverified_claims", "get_unverified_header"):
        original = getattr(jose.jwt, name)

        def parse(*args, _original=original, **kwargs):
            seen["parse"] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(jose.jwt, name, parse)
    return seen


@pytest.fixture(params=["production", "dev-token"])
def node(request, plane_runtime, server_identity, spies, monkeypatch):
    if request.param == "dev-token":
        monkeypatch.setenv("ASTRAL_ENV", "development")
        monkeypatch.setenv("USE_MOCK_AUTH", "true")  # the removed flag must change nothing
        token, mcp_token = "dev-token", "dev-mcp-token"
    else:
        monkeypatch.setenv("ASTRAL_ENV", "production")
        monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
        token = server_identity.token("owner-1", roles=("user", "admin"))
        mcp_token = server_identity.mcp_token("owner-1")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    orch = orchestrator_app.host(plane_runtime)
    app = orchestrator_app.build(monkeypatch, orch)
    client = TestClient(app, base_url=orchestrator_app.PUBLIC_BASE_URL, raise_server_exceptions=False)
    yield NS(client=client, orch=orch, token=token, mcp_token=mcp_token, spies=spies, mode=request.param)
    client.close()


@pytest.fixture
def server(plane_runtime, server_identity, spies, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    orch = orchestrator_app.host(plane_runtime)
    app = orchestrator_app.build(monkeypatch, orch)
    client = TestClient(app, base_url=orchestrator_app.PUBLIC_BASE_URL, raise_server_exceptions=False)
    yield NS(client=client, orch=orch, identity=server_identity, spies=spies,
             token=server_identity.token("owner-1", roles=("user", "admin")))
    client.close()


def _untouched(node) -> None:
    assert node.spies == {"jwks": 0, "parse": 0}


def _detail(response) -> str:
    return response.json().get("detail")


@pytest.mark.parametrize("method,path,status,body", REST_DOORS)
def test_rest_doors_refuse_a_valid_bearer(node, method, path, status, body):
    response = node.client.request(method, path, headers={
        "Authorization": f"Bearer {node.token}", "Origin": orchestrator_app.PUBLIC_BASE_URL,
        "Content-Type": "application/json"}, content=b"{}")
    assert (response.status_code, response.json()) == (status, body)
    _untouched(node)


async def test_the_work_route_authentication_refuses_inside_its_wrapper(node, plane_runtime, monkeypatch):
    from orchestrator.session_store import WebSessionStore
    from orchestrator.work_submit_authority import _authenticate_work_request
    from cryptography.fernet import Fernet
    from persistent_agents.models import AssignmentError

    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    sessions = WebSessionStore(plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories)
    with pytest.raises(AssignmentError) as refused:
        await _authenticate_work_request(_request(node.token), sessions=sessions, plane_runtime=plane_runtime,
                                         methods=("GET",), read_only=True)
    assert (refused.value.code, refused.value.status_code) == ("work_authentication_required", 401)
    _untouched(node)


@pytest.mark.parametrize("path", ["/api/share", "/api/download/s1/f.txt", "/api/connection/status"])
def test_a_query_token_and_a_bare_request_are_refused_the_same_way(node, path):
    for response in (node.client.get(path, params={"token": node.token}), node.client.get(path)):
        assert response.status_code == 401 and _detail(response) == BEARER
    _untouched(node)


REMOVED_DOORS = [*SIGN_IN_DOORS, *LEGACY_DOORS,
                 ("GET", "/api/remote-mcp/oauth/start"), ("GET", "/api/remote-mcp/oauth/callback")]
CREDENTIALS = (
    {"Authorization": "Bearer {token}"},
    {"Cookie": "astral_session=value.signature"},
    {"Authorization": "Bearer {token}", "Cookie": "astral_session=value.signature"},
    {},
)


@pytest.mark.parametrize("method,path", REMOVED_DOORS)
@pytest.mark.parametrize("credentials", CREDENTIALS)
def test_removed_sign_in_session_and_enrollment_doors_answer_404(node, method, path, credentials):
    headers = {name: value.format(token=node.token) for name, value in credentials.items()}
    response = node.client.request(
        method, path, headers={**headers, "Origin": orchestrator_app.PUBLIC_BASE_URL},
        content=b"grant_type=refresh_token&refresh_token=r", follow_redirects=False)
    assert response.status_code == 404, response.text
    _untouched(node)


@pytest.mark.parametrize("credentials", CREDENTIALS)
def test_the_shell_reads_no_credential_and_never_redirects_to_sign_in(node, credentials):
    headers = {name: value.format(token=node.token) for name, value in credentials.items()}
    response = node.client.get("/", headers=headers, follow_redirects=False)
    assert response.status_code in (200, 500), response.status_code
    assert node.token not in response.text and "value.signature" not in response.text
    assert "location" not in response.headers
    _untouched(node)


def _websocket_probe(node):
    with node.client.websocket_connect("/ws") as socket:
        socket.send_text(json.dumps({"type": "register_ui", "token": node.token, "resumed": True}))
        assert json.loads(socket.receive_text()) == {"type": "auth_required", "reason": BEARER}
    assert node.orch.ui_sessions == {}


def _a2a_probe(node):
    for token in (node.token, "afk_framework-credential"):
        response = node.client.post("/a2a", json=A2A_BODY, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.json()["error"]["message"] == BEARER
        assert response.headers["www-authenticate"] == f'AstralCaller realm="astral-a2a", error="{BEARER}"'


def _mcp_probe(node):
    for token in (node.mcp_token, "afk_framework-credential"):
        response = node.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MCP_META}},
            headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2026-07-28",
                     "Mcp-Method": "server/discover"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == f'AstralCaller realm="astral-mcp", error="{BEARER}"'


def _worker_probe(node):
    with pytest.raises(WebSocketDisconnect):
        with node.client.websocket_connect(WORKER_CONTROL_PATH, headers={"Authorization": f"Bearer {node.token}"}):
            pass


IN_BAND_PROBES = {
    ("WS", "/ws"): _websocket_probe,
    ("POST", "/a2a"): _a2a_probe,
    ("POST", "/mcp"): _mcp_probe,
    ("WS", WORKER_CONTROL_PATH): _worker_probe,
}


@pytest.mark.parametrize("door", sorted(census.IN_BAND))
def test_every_in_band_door_refuses_a_valid_bearer(node, door):
    assert door in IN_BAND_PROBES, f"{door} is on the census in-band list without a denial probe"
    IN_BAND_PROBES[door](node)
    _untouched(node)


def _request(token=None, *, query=None):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers,
                    "query_string": (query or "").encode(), "app": None})


async def test_each_rest_dependency_refuses_before_parsing(node):
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=node.token)
    for call in (
        auth.get_current_user_payload(_request(node.token), credentials),
        auth.get_web_or_bearer_user_payload(_request(), None),
        auth.get_download_user_payload(_request(), None),
        auth.verify_production_token(node.token),
    ):
        with pytest.raises(HTTPException) as refused:
            await call
        assert (refused.value.status_code, refused.value.detail) == (401, BEARER)
    _untouched(node)


async def test_every_token_choke_point_refuses_on_the_node(node):
    with pytest.raises(TokenRefused) as refused:
        await verify_idp_token(node.token)
    assert refused.value.reason == "node_bearer_refused"
    for call in (mcp_authz.decode_mcp_token(node.mcp_token), mcp_authz.authorize_mcp_request(
            headers={"authorization": f"Bearer {node.mcp_token}"}, query_params={}, cookies={},
            required_scopes=("mcp:discover",))):
        with pytest.raises(mcp_authz.MCPAuthError) as mcp_refused:
            await call
        assert (mcp_refused.value.status_code, mcp_refused.value.error) == (401, BEARER)
    assert await Orchestrator.validate_token(node.orch, node.token) is None
    assert await A2ASecurityValidator(require_first_party_user=True).validate_token(node.token) is None
    assert await A2ASecurityValidator().validate_token(node.token) is None
    assert await a2a.authenticate_a2a_request(
        A2ASecurityValidator(require_first_party_user=True), {"authorization": f"Bearer {node.token}"},
        resolve_framework_bearer=lambda _token: pytest.fail("framework credentials are a bearer")) is None
    assert ni.bound_owner() is None
    _untouched(node)


async def _true():
    return True


async def test_the_same_token_still_works_at_the_server_profile_choke_points(server):
    owner = server.identity.owner("owner-1")
    assert (await verify_idp_token(server.token))["sub"] == owner
    assert (await Orchestrator.validate_token(server.orch, server.token))["sub"] == owner
    assert (await A2ASecurityValidator(require_first_party_user=True).validate_token(server.token))["sub"] == owner
    principal = await a2a.authenticate_a2a_request(
        A2ASecurityValidator(require_first_party_user=True), {"authorization": f"Bearer {server.token}"})
    assert principal.sub == owner
    claims = await mcp_authz.authorize_mcp_request(
        headers={"authorization": f"Bearer {server.identity.mcp_token('owner-1')}"}, query_params={},
        cookies={}, required_scopes=("mcp:discover",))
    assert claims["sub"] == owner
    payload = await auth.get_current_user_payload(
        _request(server.token), HTTPAuthorizationCredentials(scheme="Bearer", credentials=server.token))
    assert payload["sub"] == owner


def test_the_server_websocket_door_does_not_answer_with_the_node_refusal(server):
    with server.client.websocket_connect("/ws") as socket:
        socket.send_text(json.dumps({"type": "register_ui", "token": "not-a-token"}))
        assert json.loads(socket.receive_text()) == {"type": "auth_required", "reason": "invalid"}


def test_the_same_token_passes_the_a2a_and_mcp_doors_on_the_server_profile(server):
    a2a_response = server.client.post("/a2a", json=A2A_BODY, headers={"Authorization": f"Bearer {server.token}"})
    assert a2a_response.status_code != 401, a2a_response.text
    assert "Authentication required" not in a2a_response.text and BEARER not in a2a_response.text
    fetched = server.spies["jwks"]
    assert fetched > 0
    mcp_response = server.client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MCP_META}},
        headers={"Authorization": f"Bearer {server.identity.mcp_token('owner-1')}",
                 "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"})
    assert mcp_response.status_code != 401, mcp_response.text
    assert "www-authenticate" not in mcp_response.headers
    assert server.spies["jwks"] > fetched


PERSONAL_METADATA = {
    "resource": orchestrator_app.PUBLIC_BASE_URL + "/mcp",
    "scopes_supported": list(mcp_authz.MCP_SCOPES),
    "bearer_methods_supported": [],
}


def _no_identity_server(client, authority):
    metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200 and metadata.json() == PERSONAL_METADATA
    card = client.get("/a2a/.well-known/agent-card.json")
    assert card.status_code == 200
    for text in (metadata.text, card.text):
        assert "openid" not in text.lower() and "keycloak" not in text.lower()
        assert authority is None or authority not in text


def test_a_personal_node_names_no_identity_server_while_keycloak_is_still_configured(node, server_identity):
    _no_identity_server(node.client, server_identity.issuer.authority)
    _untouched(node)


def test_a_personal_node_with_no_keycloak_setting_mounts_and_refuses_mcp_and_a2a(plane_runtime, spies, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    for name in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET", ni.ISSUERS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    app = orchestrator_app.build(monkeypatch, orchestrator_app.host(plane_runtime))
    with TestClient(app, base_url=orchestrator_app.PUBLIC_BASE_URL, raise_server_exceptions=False) as client:
        _no_identity_server(client, None)
        probe = NS(client=client, token="not-a-token", mcp_token="not-a-token")
        _a2a_probe(probe)
        _mcp_probe(probe)
    assert spies == {"jwks": 0, "parse": 0}


def test_the_server_profile_keeps_its_identity_server_metadata_and_bearer_challenges(server):
    authority = server.identity.issuer.authority
    metadata = server.client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["authorization_servers"] == [authority] and metadata["bearer_methods_supported"] == ["header"]
    card = server.client.get("/a2a/.well-known/agent-card.json").json()
    assert f"{authority}/.well-known/openid-configuration" in json.dumps(card)
    refused = server.client.post("/a2a", json=A2A_BODY)
    assert refused.status_code == 401 and refused.headers["www-authenticate"] == 'Bearer realm="astral-a2a"'
    challenged = server.client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MCP_META}},
        headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"})
    assert challenged.status_code == 401 and challenged.headers["www-authenticate"].startswith("Bearer resource_metadata=")


def test_the_mcp_server_still_needs_keycloak_on_the_server_profile(monkeypatch):
    from orchestrator import mcp_server_endpoint

    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    monkeypatch.delenv("KEYCLOAK_AUTHORITY", raising=False)
    with pytest.raises(RuntimeError, match="KEYCLOAK_AUTHORITY"):
        mcp_server_endpoint.install_mcp_server(NS(), object(), public_base_url=orchestrator_app.PUBLIC_BASE_URL)


async def test_the_jwks_warm_loop_never_fetches_on_a_personal_node(monkeypatch):
    import asyncio

    fetched = []

    async def fetch(url, *args, **kwargs):
        fetched.append(url)
        return {"keys": []}

    monkeypatch.setattr(jwks_cache, "get_jwks", fetch)
    monkeypatch.setattr(jwks_cache, "_fetch", fetch)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/r")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    orch = orchestrator_app.host()
    async with asyncio.timeout(5):
        await orch._jwks_warm_loop()
    assert fetched == []
    monkeypatch.delenv(ni.PROFILE_ENV)
    task = asyncio.create_task(orch._jwks_warm_loop())
    try:
        async with asyncio.timeout(5):
            while not fetched:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert fetched == ["https://idp.example/realms/r/protocol/openid-connect/certs"]
