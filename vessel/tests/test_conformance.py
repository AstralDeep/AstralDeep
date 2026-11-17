# The acceptance checks against a scripted node: each passes on a correct node and names the fault on a broken one.

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from astralvessel import conformance, node
from astralvessel.deployment import NodeSection, OwnerSection

ISSUER = "https://keycloak.test/realms/astral"
DEPLOYMENT = SimpleNamespace(node=NodeSection(), owner=OwnerSection(issuer=ISSUER, client_id="c"))
TOKEN = "header.payload.signature"
PAYLOAD = base64.urlsafe_b64encode(json.dumps({"v": 1}).encode()).decode().rstrip("=")
DESCRIPTOR = {"format": node.DESCRIPTOR_FORMAT, "node_id": "n", "node_key": {"crv": "Ed25519"},
              "accepted_issuers": [ISSUER], "capabilities": ["chat", "tool-dispatch"], "owner_hash": None}
CARD = {"supportedInterfaces": [{"protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": True}}


def good_node(request: httpx.Request) -> httpx.Response:
    path, auth = request.url.path, request.headers.get("authorization", "")
    if path == "/readyz":
        return httpx.Response(200, json={"status": "ok", "db": "ok"})
    if path == "/api/node/descriptor":
        return httpx.Response(200, json=DESCRIPTOR)
    if path == "/.well-known/astral-node/jwks.json":
        return httpx.Response(200, json={"keys": [{"crv": "Ed25519"}]})
    if path == "/a2a/.well-known/agent-card.json":
        return httpx.Response(200, json=CARD)
    if path == "/a2a":
        if auth != f"Bearer {TOKEN}":
            return httpx.Response(401)
        method = json.loads(request.content)["method"]
        if method != "SendMessage":
            return httpx.Response(200, json={"error": {"code": -32601}})
        return httpx.Response(200, json={"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"}}}})
    if path == "/api/node/pairing":
        if auth != f"Bearer {TOKEN}":
            return httpx.Response(401)
        return httpx.Response(200, json={"pairing": {}, "mesh": {"state": "offer", "code": "1111 2222",
                                                                 "payload": PAYLOAD}})
    return httpx.Response(404)


@pytest.fixture
def serve(monkeypatch):
    def install(handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(node.httpx, "request", lambda method, url, **kw: client.request(
            method, url, json=kw.get("json"), headers=kw.get("headers")))
    return install


def test_a_correct_node_passes_every_check(serve, capsys):
    serve(good_node)
    assert conformance.run(DEPLOYMENT, TOKEN) == []
    assert capsys.readouterr().out.count("PASS") == len(conformance.CHECKS)


def test_a_node_that_is_already_in_the_mesh_still_passes_the_pairing_check(serve):
    def enrolled(request):
        if request.url.path == "/api/node/pairing" and request.headers.get("authorization"):
            return httpx.Response(200, json={"pairing": {}, "mesh": {
                "state": "enrolled", "mesh_id": "m", "member_id": "d"}})
        return good_node(request)

    serve(enrolled)
    assert conformance.run(DEPLOYMENT, TOKEN) == []


READY, DESC, KEY, CARD_CHECK = (name for name, _ in conformance.CHECKS[:4])
PAIRING = conformance.CHECKS[-1][0]


@pytest.mark.parametrize("path,reply,failing", [
    ("/readyz", httpx.Response(503), READY),
    ("/api/node/descriptor", httpx.Response(404), DESC),
    ("/.well-known/astral-node/jwks.json", httpx.Response(200, json={"keys": []}), KEY),
    ("/a2a/.well-known/agent-card.json", httpx.Response(500), CARD_CHECK),
    ("/a2a/.well-known/agent-card.json", httpx.Response(200, json={}), CARD_CHECK),
])
def test_a_broken_surface_names_the_failing_check(serve, path, reply, failing):
    serve(lambda request: reply if request.url.path == path else good_node(request))
    assert conformance.run(DEPLOYMENT, TOKEN) == [failing]


def test_open_a2a_and_open_pairing_fail(serve):
    def open_node(request):
        if request.url.path in ("/a2a", "/api/node/pairing"):
            return httpx.Response(200, json={"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"}}},
                                             "mesh": {"state": "offer", "code": "1", "payload": PAYLOAD}})
        return good_node(request)

    serve(open_node)
    failed = conformance.run(DEPLOYMENT, TOKEN)
    for name in ("A2A refuses a call with no token", "A2A refuses a forged token", "A2A rejects an unknown method",
                 "pairing refuses a caller who is not the owner"):
        assert name in failed


def test_an_unanswered_message_and_a_bad_payload_fail(serve):
    def broken(request):
        if request.url.path == "/a2a" and request.headers.get("authorization"):
            return httpx.Response(200, json={"error": {}})
        if request.url.path == "/api/node/pairing" and request.headers.get("authorization"):
            return httpx.Response(200, json={"pairing": {}, "mesh": {"state": "offer", "code": "1", "payload": "!!"}})
        return good_node(request)

    serve(broken)
    assert conformance.run(DEPLOYMENT, TOKEN) == ["A2A refuses a forged token", "A2A answers the owner", PAIRING]


def test_pairing_refusals_other_states_and_a_down_node_are_reported(serve):
    def pairing(reply):
        def handler(request):
            if request.url.path == "/api/node/pairing" and request.headers.get("authorization"):
                return reply
            return good_node(request)
        return handler

    for reply in (httpx.Response(401), httpx.Response(200, json={"mesh": {"state": "not_enrolled"}}),
                  httpx.Response(200, json={"mesh": {"state": "enrolled"}}),
                  httpx.Response(200, json={"mesh": {"state": "offer", "payload": PAYLOAD}})):
        serve(pairing(reply))
        assert conformance.run(DEPLOYMENT, TOKEN) == [PAIRING]

    def down(request):
        raise httpx.ConnectError("down")

    serve(down)
    assert len(conformance.run(DEPLOYMENT, TOKEN)) == len(conformance.CHECKS)
