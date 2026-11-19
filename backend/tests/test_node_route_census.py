"""Checks the complete personal node route table and its required pairing, operator and transport gates.
The app fixture mounts every optional transport and test_node_bearer_refusal exercises their credential checks."""

from __future__ import annotations

from typing import Any, Iterable

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from starlette.routing import Mount, Route, WebSocketRoute

from orchestrator import auth, node_api, node_connection_api
from orchestrator.voice_worker_endpoint import WORKER_CONTROL_PATH
from tests.helpers import orchestrator_app
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

PUBLIC = {
    ("GET,HEAD", "/api/openapi.json"): "the API schema",
    ("GET,HEAD", "/api/docs"): "the API docs page",
    ("GET,HEAD", "/api/redoc"): "the API docs page",
    ("GET", "/docs"): "a redirect to the API docs",
    ("GET", "/redoc"): "a redirect to the API docs",
    ("GET", "/openapi.json"): "a redirect to the API schema",
    ("GET", "/healthz"): "liveness",
    ("GET", "/readyz"): "readiness",
    ("GET", "/api/node/agent-servers"): "a loopback-only answer that reads no bearer",
    ("GET", "/api/node/descriptor"): "the node descriptor",
    ("GET", "/.well-known/astral-node/jwks.json"): "the node's public keys",
    ("GET", "/.well-known/oauth-protected-resource/mcp"): "MCP resource metadata",
    ("GET,HEAD", "/a2a/.well-known/agent-card.json"): "the anonymous A2A agent card",
}

IN_BAND = {
    ("POST", "/a2a"): "the gated JSON-RPC endpoint takes only a caller ticket and refuses every bearer unread",
    ("POST", "/mcp"): "authorize_mcp_caller takes only a caller ticket and refuses every bearer unread",
    ("WS", WORKER_CONTROL_PATH): "the worker control socket refuses any Authorization or cookie header",
}

LOCAL_GATES = {
    ("POST", "/api/node/mesh/offer"): node_api.pairing_gate,
    ("GET", "/api/node/connections"): node_connection_api.operator_gate,
    ("POST", "/api/node/connections/{connection}/sign-in"): node_connection_api.operator_gate,
    ("POST", "/api/node/connections/sign-in/{flow}/complete"): node_connection_api.operator_gate,
    ("POST", "/api/node/connections/sign-in/{flow}/poll"): node_connection_api.operator_gate,
    ("DELETE", "/api/node/connections/sign-in/{flow}"): node_connection_api.operator_gate,
}


def _calls(dependant: Any) -> Iterable[Any]:
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            yield dependency.call
        yield from _calls(dependency)


def _key(context: Any) -> tuple:
    route = context.original_route
    if isinstance(route, Mount):
        return ("MOUNT", context.path)
    if isinstance(route, (APIWebSocketRoute, WebSocketRoute)):
        return ("WS", context.path)
    return (",".join(sorted(context.methods or ())), context.path)


def _gated(context: Any, key: tuple) -> bool:
    route = context.original_route
    if not isinstance(route, (APIRoute, APIWebSocketRoute)):
        return False
    return LOCAL_GATES.get(key) in _calls(context.dependant)


def census(app) -> tuple[list, set]:
    findings, seen = [], set()
    for context in iter_route_contexts(app.routes):
        key = _key(context)
        seen.add(key)
        if not (key in PUBLIC or key in IN_BAND or _gated(context, key)):
            findings.append(key)
    return findings, seen


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("node_route_census") as runtime:
        yield runtime


@pytest.fixture
def app(plane_runtime, node_data_dir, monkeypatch):
    return orchestrator_app.build(monkeypatch, orchestrator_app.host(plane_runtime))


def test_every_served_route_is_named_and_has_its_required_gate(app):
    findings, seen = census(app)
    assert findings == []
    assert seen == set(PUBLIC) | set(IN_BAND) | set(LOCAL_GATES)


def test_the_node_hosts_no_browser_shell_or_static_assets(app):
    seen = census(app)[1]
    assert ("GET", "/") not in seen and ("MOUNT", "/static") not in seen
    assert not any(path == "/" or path.startswith("/static") for _, path in seen)


def test_the_owner_token_pairing_routes_are_gone(app):
    paths = {path for _, path in census(app)[1]}
    assert "/api/node/pairing" not in paths and "/api/node/pairing/resolve" not in paths
    assert ("POST", "/api/node/mesh/offer") in census(app)[1]


def test_a_route_added_to_the_app_without_a_gate_is_reported(app):
    async def probe():
        return {}

    app.add_api_route("/census/probe", probe, methods=["GET"])
    app.router.routes.append(Route("/census/starlette", probe, methods=["POST"]))
    app.router.routes.append(WebSocketRoute("/census/socket", probe))
    assert census(app)[0] == [("GET", "/census/probe"), ("POST", "/census/starlette"), ("WS", "/census/socket")]


def test_a_legacy_bearer_router_cannot_be_remounted(app):
    async def probe():
        return {}

    router = APIRouter(prefix="/api/chats")
    router.add_api_route("/{chat_id}", probe, methods=["GET"])
    app.include_router(router, dependencies=[Depends(auth.refuse_node_bearer)])
    assert census(app)[0] == [("GET", "/api/chats/{chat_id}")]


def test_a_local_route_requires_its_own_gate_even_when_nested():
    async def wrong(secret=Depends(node_api.pairing_gate)):
        return secret

    async def nested(secret=Depends(node_connection_api.operator_gate)):
        return secret

    async def probe():
        return {}

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_api_route("/api/node/connections", probe, methods=["GET"], dependencies=[Depends(wrong)])
    app.add_api_route("/api/node/connections/sign-in/{flow}", probe, methods=["DELETE"], dependencies=[Depends(nested)])
    assert census(app)[0] == [("GET", "/api/node/connections")]


REMOVED_SIGN_IN_ROUTES = (
    ("GET", "/docs/oauth2-redirect"), ("GET", "/auth/login"), ("GET", "/auth/session"), ("GET", "/auth/callback"),
    ("POST", "/auth/refresh"), ("POST", "/auth/logout"), ("GET", "/auth/logout"),
    ("GET", "/auth/error"), ("GET", "/kiosk"), ("POST", "/auth/kiosk/start"),
    ("POST", "/auth/kiosk/poll"), ("POST", "/api/auth/token"), ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/device/start"), ("POST", "/api/auth/device/poll"),
    ("POST", "/api/auth/device/refresh"), ("GET", "/enroll"), ("POST", "/api/mesh/session"),
    ("POST", "/api/institution/pairing"), ("GET", "/api/remote-mcp/oauth/start"),
)


@pytest.mark.parametrize(("method", "path"), REMOVED_SIGN_IN_ROUTES)
def test_removed_sign_in_routes_answer_404(app, method, path):
    from fastapi.testclient import TestClient

    response = TestClient(app).request(method, path, follow_redirects=False)
    assert response.status_code == 404
    assert (method, path) not in census(app)[1]
