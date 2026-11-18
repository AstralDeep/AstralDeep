
from __future__ import annotations

from typing import Any, Iterable

import pytest
from fastapi.routing import APIWebSocketRoute
from starlette.routing import Mount, WebSocketRoute

from shared import node_identity
from orchestrator.voice_worker_endpoint import WORKER_CONTROL_PATH
from tests.helpers import orchestrator_app
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


PUBLIC = {
    ("GET,HEAD", "/api/openapi.json"): "the API schema",
    ("GET,HEAD", "/api/docs"): "the API docs page",
    ("GET,HEAD", "/docs/oauth2-redirect"): "the API docs page",
    ("GET,HEAD", "/api/redoc"): "the API docs page",
    ("GET", "/docs"): "a redirect to the API docs",
    ("GET", "/redoc"): "a redirect to the API docs",
    ("GET", "/openapi.json"): "a redirect to the API schema",
    ("GET", "/healthz"): "liveness",
    ("GET", "/readyz"): "readiness",
    ("MOUNT", "/static"): "static shell assets",
    ("GET", "/api/node/agent-servers"): "a loopback-only answer that reads no bearer",
    ("GET", "/api/node/descriptor"): "the node descriptor",
    ("GET", "/.well-known/astral-node/jwks.json"): "the node's public keys",
    ("GET", "/.well-known/oauth-protected-resource/mcp"): "MCP resource metadata",
    ("GET,HEAD", "/a2a/.well-known/agent-card.json"): "the anonymous A2A agent card",
}

PERSONAL_REFUSED = {
    ("GET", "/share/{token}"): "a capability link that reads no Authorization header and answers 404 on a personal node",
}

IN_BAND = {
    ("WS", "/ws"): "register_ui answers auth_required with node.bearer_refused",
    ("POST", "/a2a"): "the gated JSON-RPC endpoint takes only a caller ticket and refuses every bearer unread",
    ("POST", "/mcp"): "authorize_mcp_caller takes only a caller ticket and refuses every bearer unread",
    ("WS", WORKER_CONTROL_PATH): "the worker control socket refuses any Authorization or cookie header",
}

OPERATOR_PATHS = {
    "/api/node/connections",
    "/api/node/connections/{connection}/sign-in",
    "/api/node/connections/sign-in/{flow}/complete",
    "/api/node/connections/sign-in/{flow}/poll",
    "/api/node/connections/sign-in/{flow}",
}

FLAG_GATED = {"/mcp", "/a2a", "/kiosk", "/auth/kiosk/start", "/api/remote-mcp/oauth/start", WORKER_CONTROL_PATH}


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


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("node_route_census") as runtime:
        yield runtime


@pytest.fixture
def app(plane_runtime, node_data_dir, monkeypatch):
    return orchestrator_app.build(monkeypatch, orchestrator_app.host(plane_runtime))


def test_the_personal_refused_routes_answer_404_on_a_personal_node(app, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    client = TestClient(app)
    for method, path in PERSONAL_REFUSED:
        assert client.request(method, path.replace("{token}", "anything")).status_code == 404
