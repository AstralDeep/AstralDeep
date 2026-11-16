"""Proves a personal node refuses a validly signed bearer JWT from an accepted issuer at every door of the app Orchestrator.build_http_app serves, with the door's refusal code and with no key fetch or token parse, while the same token still works on the server profile.
The doors are orchestrator/auth.py, web_auth.py, mesh_api.py, mcp_authz.py, a2a_orchestrator_executor.py, orchestrator.validate_token, shared/token_verify.py and shared/a2a_security.py, and test_node_route_census.py proves no route sits outside them."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import jose.jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from orchestrator import a2a_orchestrator_executor as a2a, auth, mcp_authz, web_auth
from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_worker_endpoint import WORKER_CONTROL_PATH
from shared import jwks_cache, node_identity as ni
from shared.a2a_security import A2ASecurityValidator
from shared.token_verify import TokenRefused, verify_idp_token
from tests.helpers import orchestrator_app
from tests.helpers.idp_tokens import AUTHORITY
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

BEARER = "node.bearer_refused"
SIGN_IN = "node.sign_in_unavailable"
LEGACY = "node.legacy_enrollment_closed"
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
REST_DOORS = [
    ("GET", "/api/connection/status", 401, REFUSED),
    ("GET", "/api/share", 401, REFUSED),
    ("GET", "/api/download/s1/f.txt", 401, REFUSED),
    ("POST", "/api/audit/session-resume-failed", 401, REFUSED),
    ("GET", "/api/remote-mcp/oauth/start", 401, REFUSED),
    ("GET", "/api/emergency-stop", 401, REFUSED),
    ("GET", "/api/chrome/commands", 401, REFUSED),
    ("GET", "/api/work/v1/operations/op-1", 401, {"error": "work_authentication_required"}),
    ("POST", "/api/work/v1/operations", 503, {"error": "work_submit_unavailable"}),
    ("POST", "/api/work/v1/operations/op-1/pause", 503, {"error": "work_read_unavailable"}),
    ("POST", "/api/export/canvas/c1/presentation", 401, {"error": "presentation_authentication_required"}),
]
SIGN_IN_DOORS = [
    ("GET", "/"),
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


@pytest.fixture(params=["production", "mock"])
def node(request, plane_runtime, server_identity, spies, monkeypatch):
    if request.param == "mock":
        monkeypatch.setenv("ASTRAL_ENV", "development")
        monkeypatch.setenv("USE_MOCK_AUTH", "true")
        token, mcp_token = "dev-token", "dev-mcp-token"
    else:
        monkeypatch.setenv("ASTRAL_ENV", "production")
        monkeypatch.setenv("USE_MOCK_AUTH", "false")
        token = server_identity.token("owner-1", roles=("user", "admin"))
        mcp_token = server_identity.mcp_token("owner-1")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    orch = orchestrator_app.host(plane_runtime)
    app = orchestrator_app.build(monkeypatch, orch)
    client = TestClient(app, base_url=orchestrator_app.PUBLIC_BASE_URL, raise_server_exceptions=False)
    yield NS(client=client, orch=orch, token=token, mcp_token=mcp_token, spies=spies, mode=request.param)
    client.close()


@pytest.fixture
def server(plane_runtime, server_identity, spies, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
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
    expected = ("work_authority_unavailable", 403) if node.mode == "mock" else ("work_authentication_required", 401)
    assert (refused.value.code, refused.value.status_code) == expected
    _untouched(node)


@pytest.mark.parametrize("path", ["/api/share", "/api/download/s1/f.txt", "/api/connection/status"])
def test_a_query_token_and_a_bare_request_are_refused_the_same_way(node, path):
    for response in (node.client.get(path, params={"token": node.token}), node.client.get(path)):
        assert response.status_code == 401 and _detail(response) == BEARER
    _untouched(node)


@pytest.mark.parametrize("method,path", SIGN_IN_DOORS)
def test_sign_in_token_proxy_and_device_grant_are_unavailable(node, method, path):
    response = node.client.request(method, path, headers={
        "Authorization": f"Bearer {node.token}", "X-Astral-Session-Custody": "1",
        "Content-Type": "application/x-www-form-urlencoded"},
        content=b"grant_type=refresh_token&refresh_token=r", follow_redirects=False)
    assert response.status_code == 403, response.text
    assert _detail(response) == SIGN_IN
    _untouched(node)


@pytest.mark.parametrize("method,path", LEGACY_DOORS)
def test_legacy_enrollment_doors_are_closed(node, method, path):
    response = node.client.request(method, path, headers={
        "Authorization": f"Bearer {node.token}", "Origin": orchestrator_app.PUBLIC_BASE_URL,
        "Content-Type": "application/json"}, content=b'{"owner_id": "o", "member_id": "m"}')
    assert response.status_code == 403, response.text
    assert _detail(response) == LEGACY
    _untouched(node)


def test_websocket_registration_answers_the_refusal_frame(node):
    with node.client.websocket_connect("/ws") as socket:
        socket.send_text(json.dumps({"type": "register_ui", "token": node.token, "resumed": True}))
        assert json.loads(socket.receive_text()) == {"type": "auth_required", "reason": BEARER}
    assert node.orch.ui_sessions == {}
    _untouched(node)


@pytest.mark.parametrize("bearer", ["token", "afk_framework-credential"])
def test_a2a_refuses_every_bearer_before_reading_it(node, bearer):
    token = node.token if bearer == "token" else bearer
    response = node.client.post("/a2a", json=A2A_BODY, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == BEARER
    _untouched(node)


@pytest.mark.parametrize("bearer", ["token", "afk_framework-credential"])
def test_mcp_refuses_every_bearer_before_reading_it(node, bearer):
    token = node.mcp_token if bearer == "token" else bearer
    response = node.client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MCP_META}},
        headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2026-07-28",
                 "Mcp-Method": "server/discover"})
    assert response.status_code == 401
    assert f'error="{BEARER}"' in response.headers["www-authenticate"]
    _untouched(node)


def test_the_voice_worker_socket_refuses_a_bearer(node):
    with pytest.raises(WebSocketDisconnect):
        with node.client.websocket_connect(WORKER_CONTROL_PATH, headers={"Authorization": f"Bearer {node.token}"}):
            pass
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


def test_the_same_token_still_works_on_the_server_profile(server, monkeypatch):
    headers = {"Authorization": f"Bearer {server.token}"}
    shared = server.client.get("/api/share", headers=headers)
    assert shared.status_code == 404 and _detail(shared) == "Not Found"
    downloaded = server.client.get("/api/download/s1/f.txt", headers=headers)
    assert downloaded.status_code == 404 and downloaded.json() == {"error": "File not found"}
    assert server.client.get("/api/share", params={"token": server.token}).status_code == 404
    monkeypatch.setattr(web_auth, "_idp_reachable", lambda _authority: _true())
    login = server.client.get("/auth/login", follow_redirects=False)
    assert login.status_code == 307 and login.headers["location"].startswith(AUTHORITY)
    for method, path in SIGN_IN_DOORS[11:]:
        response = server.client.request(method, path, headers=headers, json={})
        assert response.status_code != 403 or _detail(response) != SIGN_IN
    for method, path in LEGACY_DOORS:
        response = server.client.request(method, path, headers={**headers, "Origin": orchestrator_app.PUBLIC_BASE_URL}, json={})
        assert response.status_code != 403 or _detail(response) != LEGACY
    assert server.spies["jwks"] > 0


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
