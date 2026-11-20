# The acceptance checks against a scripted node: each passes on a correct node and names the fault on a broken one.

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from astralvessel import caller as callers
from astralvessel import conformance, node
from astralvessel.deployment import NodeSection

SEED = bytes(range(32))
HOLDER = ed25519.Ed25519PrivateKey.from_private_bytes(SEED).public_key()
DEPLOYMENT = SimpleNamespace(node=NodeSection())
TICKET = callers.TICKET_TEXT + callers.b64(json.dumps({"id": "ticket-1"}).encode())
CALLER = callers.Caller(TICKET, callers.KEY_TEXT + callers.b64(SEED))
PAYLOAD = base64.urlsafe_b64encode(json.dumps({"v": 1}).encode()).decode().rstrip("=")
DESCRIPTOR = {"format": node.DESCRIPTOR_FORMAT, "node_id": "n", "node_key": {"crv": "Ed25519"},
              "capabilities": ["chat", "tool-dispatch"], "owner_hash": None}
CARD = {"supportedInterfaces": [{"protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": True}}
OFFER = {"status": 200, "state": "offer", "code": "1111 2222", "payload": PAYLOAD, "expires_ms": 1}
READY, DESC, KEY, CARD_CHECK = (name for name, _ in conformance.CHECKS[:4])
PAIRING = conformance.CHECKS[-1][0]


def run(offer=None):
    return conformance.run(DEPLOYMENT, CALLER, lambda: OFFER if offer is None else offer)


def a2a(request: httpx.Request) -> httpx.Response:
    if not request.headers.get("authorization", "").startswith("AstralCaller "):
        return httpx.Response(401)
    proof = json.loads(callers.unb64(request.headers["astral-caller-proof"]))
    unsigned = {k: v for k, v in proof.items() if k != "sig"}
    try:
        HOLDER.verify(callers.unb64(proof["sig"]), callers.PROOF_CONTEXT + callers.canonical(unsigned))
    except InvalidSignature:
        return httpx.Response(401)
    if proof["body"] != "sha256:" + hashlib.sha256(request.content).hexdigest():
        return httpx.Response(403)
    if json.loads(request.content)["method"] != "SendMessage":
        return httpx.Response(200, json={"error": {"code": -32601}})
    return httpx.Response(200, json={"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"}}}})


def good_node(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/readyz":
        return httpx.Response(200, json={"status": "ok", "db": "ok"})
    if path == "/api/node/descriptor":
        return httpx.Response(200, json=DESCRIPTOR)
    if path == "/.well-known/astral-node/jwks.json":
        return httpx.Response(200, json={"keys": [{"crv": "Ed25519"}]})
    if path == "/a2a/.well-known/agent-card.json":
        return httpx.Response(200, json=CARD)
    if path == "/a2a":
        return a2a(request)
    if path == "/api/node/mesh/offer":
        return httpx.Response(403)
    return httpx.Response(404)


@pytest.fixture
def serve(monkeypatch):
    def install(handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(node.httpx, "request", lambda method, url, **kw: client.request(
            method, url, json=kw.get("json"), content=kw.get("content"), headers=kw.get("headers")))
    return install


def test_a_correct_node_passes_every_check(serve, capsys):
    serve(good_node)
    assert run() == []
    assert capsys.readouterr().out.count("PASS") == len(conformance.CHECKS)


def test_a_node_that_is_already_in_the_mesh_still_passes_the_pairing_check(serve):
    serve(good_node)
    assert run({"status": 200, "state": "enrolled", "mesh_id": "m", "member_id": "d"}) == []


@pytest.mark.parametrize("path,reply,failing", [
    ("/readyz", httpx.Response(503), READY),
    ("/api/node/descriptor", httpx.Response(404), DESC),
    ("/api/node/descriptor", httpx.Response(200, json={**DESCRIPTOR, "format": node.LEGACY_DESCRIPTOR_FORMAT}), DESC),
    ("/.well-known/astral-node/jwks.json", httpx.Response(200, json={"keys": []}), KEY),
    ("/a2a/.well-known/agent-card.json", httpx.Response(500), CARD_CHECK),
    ("/a2a/.well-known/agent-card.json", httpx.Response(200, json={}), CARD_CHECK),
])
def test_a_broken_surface_names_the_failing_check(serve, path, reply, failing):
    serve(lambda request: reply if request.url.path == path else good_node(request))
    assert run() == [failing]


def test_open_a2a_and_open_pairing_fail(serve):
    def open_node(request):
        if request.url.path in ("/a2a", "/api/node/mesh/offer", "/api/node/pairing"):
            return httpx.Response(200, json={"result": {"task": {"status": {"state": "TASK_STATE_COMPLETED"}}}})
        return good_node(request)

    serve(open_node)
    failed = run()
    for name in ("A2A refuses a call with no ticket", "A2A refuses a JWT bearer token",
                 "A2A refuses a proof signed by another key", "A2A rejects an unknown method",
                 "pairing refuses a caller without the node's secret", "the owner-token pairing route is gone"):
        assert name in failed


def test_a_node_that_still_takes_a_jwt_bearer_fails(serve):
    seen = []

    def bearer(request):
        if request.url.path == "/a2a" and request.headers.get("authorization", "").startswith("Bearer "):
            seen.append(request.headers["authorization"])
            return httpx.Response(200, json={})
        return good_node(request)

    serve(bearer)
    assert run() == ["A2A refuses a JWT bearer token"]
    assert seen[0].count(".") == 2 and conformance.MESSAGE["parts"][0]["text"] == "conformance check"


def test_an_unanswered_message_fails(serve):
    def broken(request):
        if request.url.path == "/a2a" and request.headers.get("authorization"):
            return httpx.Response(200, json={"error": {}})
        return good_node(request)

    serve(broken)
    assert "A2A answers the caller" in run()


@pytest.mark.parametrize("offer", [
    {"status": 409, "detail": "node.legacy_owner"}, {"status": 200, "state": "not_enrolled"},
    {"status": 200, "state": "enrolled"}, {"status": 200, "state": "offer", "payload": PAYLOAD},
    {"status": 200, "state": "offer", "code": "1", "payload": "!!"},
])
def test_pairing_refusals_other_states_and_a_bad_payload_are_reported(serve, offer):
    serve(good_node)
    assert run(offer) == [PAIRING]


def test_a_down_node_fails_every_check(serve):
    def down(request):
        raise httpx.ConnectError("down")

    serve(down)
    assert len(run()) == len(conformance.CHECKS) - 1
