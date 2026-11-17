"""Walks every route of the app Orchestrator.build_http_app serves and requires each to sit behind a dependency that refuses a bearer on a personal node, or on a short named list of routes that take no bearer or refuse inside their transport.
The gates live in orchestrator/auth.py, mesh_api.py and node_api.py, test_node_bearer_refusal.py proves each one refuses, and tests/helpers/orchestrator_app.py mounts every flag-gated surface first."""

from __future__ import annotations

from typing import Any, Iterable

import pytest
from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from starlette.routing import Mount, Route, WebSocketRoute

from orchestrator import auth, mesh_api, node_api
from orchestrator.voice_worker_endpoint import WORKER_CONTROL_PATH
from tests.helpers import orchestrator_app
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

GATES = frozenset({
    auth.refuse_node_bearer,
    auth.refuse_node_sign_in,
    auth.get_current_user_payload,
    auth.get_web_or_bearer_user_payload,
    auth.get_download_user_payload,
    mesh_api.refuse_legacy_enrollment,
    node_api.pairing_gate,
})

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
    ("GET", "/api/node/descriptor"): "the node descriptor",
    ("GET", "/.well-known/astral-node/jwks.json"): "the node's public keys",
    ("GET", "/share/{token}"): "a capability link that reads no Authorization header",
    ("GET", "/.well-known/oauth-protected-resource/mcp"): "MCP resource metadata",
    ("GET,HEAD", "/a2a/.well-known/agent-card.json"): "the anonymous A2A agent card",
}

IN_BAND = {
    ("WS", "/ws"): "register_ui answers auth_required with node.bearer_refused",
    ("POST", "/a2a"): "the gated JSON-RPC endpoint takes only a caller ticket and refuses every bearer unread",
    ("POST", "/mcp"): "authorize_mcp_caller takes only a caller ticket and refuses every bearer unread",
    ("WS", WORKER_CONTROL_PATH): "the worker control socket refuses any Authorization or cookie header",
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


def _gated(context: Any) -> bool:
    route = context.original_route
    if not isinstance(route, (APIRoute, APIWebSocketRoute)):
        return False
    return not GATES.isdisjoint(_calls(context.dependant))


def census(app) -> tuple[list, set]:
    findings, seen = [], set()
    for context in iter_route_contexts(app.routes):
        key = _key(context)
        seen.add(key)
        if not (_gated(context) or key in PUBLIC or key in IN_BAND):
            findings.append(key)
    return findings, seen


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("node_route_census") as runtime:
        yield runtime


@pytest.fixture
def app(plane_runtime, server_identity, monkeypatch):
    return orchestrator_app.build(monkeypatch, orchestrator_app.host(plane_runtime))


def test_every_served_route_refuses_a_bearer_or_is_named(app):
    findings, seen = census(app)
    assert findings == []
    assert {path for _, path in seen} >= FLAG_GATED | {"/api/node/mesh/offer", "/ws"}
    assert set(PUBLIC) | set(IN_BAND) <= seen
    assert len(seen) > 250


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


def test_a_router_included_without_a_gate_is_reported_and_a_gated_one_passes(app):
    async def probe():
        return {}

    open_router = APIRouter(prefix="/census/open")
    open_router.add_api_route("/item", probe, methods=["POST"], dependencies=[Depends(auth.security)])
    closed_router = APIRouter(prefix="/census/closed")
    closed_router.add_api_route("/item", probe, methods=["POST"])
    app.include_router(open_router)
    app.include_router(closed_router, dependencies=[Depends(auth.refuse_node_bearer)])
    assert census(app)[0] == [("POST", "/census/open/item")]


def test_a_nested_gate_counts_but_a_lookalike_does_not(app):
    async def lookalike(credentials=Depends(auth.security)):
        return credentials

    async def nested(user=Depends(auth.require_user_id)):
        return user

    async def probe():
        return {}

    app.add_api_route("/census/lookalike", probe, methods=["GET"], dependencies=[Depends(lookalike)])
    app.add_api_route("/census/nested", probe, methods=["GET"], dependencies=[Depends(nested)])
    assert census(app)[0] == [("GET", "/census/lookalike")]
