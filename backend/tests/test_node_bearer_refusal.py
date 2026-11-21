"""Probes caller-ticket transports and retired browser routes through the personal node app.
Credential spies check that rejected bearers never trigger JWT parsing or a network request."""

from __future__ import annotations

import socket
import time
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
from shared import node_identity as ni
from shared.a2a_security import A2ASecurityValidator
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

RETIRED_REST = [
    ("GET", "/api/connection/status"),
    ("GET", "/api/share"),
    ("GET", "/share/anything"),
    ("GET", "/api/download/s1/f.txt"),
    ("POST", "/api/audit/session-resume-failed"),
    ("GET", "/api/emergency-stop"),
    ("GET", "/api/chrome/commands"),
    ("GET", "/api/work/v1/operations/op-1"),
    ("POST", "/api/work/v1/operations"),
    ("POST", "/api/work/v1/operations/op-1/pause"),
    ("POST", "/api/export/canvas/c1/presentation"),
    ("GET", "/api/persistent-agents"),
    ("GET", "/api/voice/v2/status"),
    ("POST", "/api/terminals"),
    ("GET", "/api/terminals/approvals/example"),
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

    def refuse_outbound(self, *args, **kwargs):
        seen["jwks"] += 1
        raise AssertionError("no outbound connection may be opened")

    monkeypatch.setattr(socket.socket, "connect", refuse_outbound)
    for name in ("decode", "get_unverified_claims", "get_unverified_header"):
        original = getattr(jose.jwt, name)

        def parse(*args, _original=original, **kwargs):
            seen["parse"] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(jose.jwt, name, parse)
    return seen


def _signed_token(audience: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    now = int(time.time())
    return jose.jwt.encode(
        {"sub": "owner-1", "iss": "https://idp.invalid/realms/r", "aud": audience, "azp": "astral-web",
         "iat": now, "exp": now + 300, "realm_access": {"roles": ["user", "admin"]}},
        key, algorithm="RS256", headers={"kid": "k"})


@pytest.fixture(params=["production", "dev-token"])
def node(request, plane_runtime, node_data_dir, spies, monkeypatch):
    if request.param == "dev-token":
        monkeypatch.setenv("ASTRAL_ENV", "development")
        monkeypatch.setenv("USE_MOCK_AUTH", "true")  # the removed flag must change nothing
        token, mcp_token = "dev-token", "dev-mcp-token"
    else:
        monkeypatch.setenv("ASTRAL_ENV", "production")
        monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
        token = _signed_token("account")
        mcp_token = _signed_token("astral-mcp")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    orch = orchestrator_app.host(plane_runtime)
    app = orchestrator_app.build(monkeypatch, orch)
    client = TestClient(app, base_url=orchestrator_app.PUBLIC_BASE_URL, raise_server_exceptions=False)
    yield NS(client=client, orch=orch, token=token, mcp_token=mcp_token, spies=spies, mode=request.param)
    client.close()


def _untouched(node) -> None:
    assert node.spies == {"jwks": 0, "parse": 0}


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
def test_a_query_token_cannot_reopen_a_retired_route(node, path):
    for response in (node.client.get(path, params={"token": node.token}), node.client.get(path)):
        assert response.status_code == 404
    _untouched(node)


REMOVED_DOORS = [*SIGN_IN_DOORS, *LEGACY_DOORS, *RETIRED_REST,
                 ("GET", "/api/remote-mcp/oauth/start"), ("GET", "/api/remote-mcp/oauth/callback")]
CREDENTIALS = (
    {"Authorization": "Bearer {token}"},
    {"Cookie": "astral_session=value.signature"},
    {"Authorization": "Bearer {token}", "Cookie": "astral_session=value.signature"},
    {},
)


@pytest.mark.parametrize("method,path", REMOVED_DOORS)
def test_removed_doors_answer_404(node, method, path):
    for credentials in CREDENTIALS:
        headers = {name: value.format(token=node.token) for name, value in credentials.items()}
        response = node.client.request(
            method, path, headers={**headers, "Origin": orchestrator_app.PUBLIC_BASE_URL},
            content=b"grant_type=refresh_token&refresh_token=r", follow_redirects=False)
        assert response.status_code == 404, response.text
    _untouched(node)


@pytest.mark.parametrize("credentials", CREDENTIALS)
def test_the_node_hosts_no_shell_and_never_redirects_to_sign_in(node, credentials):
    headers = {name: value.format(token=node.token) for name, value in credentials.items()}
    for path in ("/", "/static/client.js"):
        response = node.client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code == 404, (path, response.status_code)
        assert node.token not in response.text and "value.signature" not in response.text
        assert "location" not in response.headers
    _untouched(node)


def test_browser_socket_is_absent_even_with_a_bearer(node):
    with pytest.raises(WebSocketDisconnect):
        with node.client.websocket_connect("/ws", headers={"Authorization": f"Bearer {node.token}"}):
            pytest.fail("retired browser socket accepted a connection")
    assert node.orch.ui_sessions == {}
    _untouched(node)


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
    for call in (mcp_authz.decode_mcp_token(node.mcp_token), mcp_authz.authorize_mcp_request(
            headers={"authorization": f"Bearer {node.mcp_token}"}, query_params={}, cookies={},
            required_scopes=("mcp:discover",))):
        with pytest.raises(mcp_authz.MCPAuthError) as mcp_refused:
            await call
        assert (mcp_refused.value.status_code, mcp_refused.value.error) == (401, BEARER)
    assert await Orchestrator.validate_token(node.orch, node.token) is None
    assert await A2ASecurityValidator(require_first_party_user=True).validate_token(node.token) is None
    assert await A2ASecurityValidator().validate_token(node.token) is None
    assert not hasattr(a2a, "authenticate_a2a_request")
    assert ni.bound_owner() is None
    _untouched(node)


async def _true():
    return True


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


def test_a_personal_node_with_no_keycloak_setting_mounts_and_refuses_mcp_and_a2a(plane_runtime, spies, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv(ni.PROFILE_ENV, ni.PERSONAL)
    for name in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    app = orchestrator_app.build(monkeypatch, orchestrator_app.host(plane_runtime))
    with TestClient(app, base_url=orchestrator_app.PUBLIC_BASE_URL, raise_server_exceptions=False) as client:
        _no_identity_server(client, None)
        probe = NS(client=client, token="not-a-token", mcp_token="not-a-token")
        _a2a_probe(probe)
        _mcp_probe(probe)
    assert spies == {"jwks": 0, "parse": 0}

