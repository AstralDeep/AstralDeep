# The node client: health, v2 descriptor checks, caller tickets and holder proofs, the pairing wording and A2A
# questions, against a scripted node.

from __future__ import annotations

import hashlib
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from astralvessel import node
from astralvessel.deployment import NodeSection

SECTION = NodeSection()
GOOD = {"format": node.DESCRIPTOR_FORMAT, "node_id": "n", "node_key": {"crv": "Ed25519"},
        "capabilities": ["chat", "tool-dispatch", "mesh-peer"], "owner_hash": "sha256:" + "0" * 64}
SEED = bytes(range(32))
TICKET = node.TICKET_TEXT + node.b64(json.dumps({"id": "ticket-1", "v": 1}).encode())
KEY = node.KEY_TEXT + node.b64(SEED)
CALLER = node.Caller(TICKET, KEY)


@pytest.fixture
def served(monkeypatch):
    seen: list[httpx.Request] = []
    replies: list = []

    def request(method, url, **kwargs):
        built = httpx.Request(method, url, headers=kwargs.get("headers"), json=kwargs.get("json"),
                              content=kwargs.get("content"))
        seen.append(built)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    monkeypatch.setattr(node.httpx, "request", request)
    return seen, replies


def test_the_base_url_follows_the_published_address():
    assert node.base_url(SECTION) == "http://127.0.0.1:8001"
    assert node.base_url(NodeSection(bind="0.0.0.0", port=9000)) == "http://127.0.0.1:9000"
    assert node.base_url(NodeSection(bind="::1")) == "http://[::1]:8001"


def test_health_says_why_the_node_is_not_ready(served):
    _, replies = served
    replies += [{"status": "ok", "db": "ok"}, {"status": "ok", "db": "down"}, httpx.Response(503, text="no"),
                httpx.Response(200, text="<html>"), httpx.ConnectError("refused")]
    assert node.health(SECTION) == "ok"
    assert "db 'down'" in node.health(SECTION)
    assert node.health(SECTION) == "/readyz answered HTTP 503"
    assert node.health(SECTION) == "/readyz did not answer JSON"
    assert "is not answering (ConnectError)" in node.health(SECTION)


def test_the_descriptor_must_be_v2_with_a_key_and_capabilities(served):
    _, replies = served
    assert node.descriptor_problem(GOOD) is None
    assert node.descriptor_problem(None) == "no descriptor"
    assert "unknown descriptor format" in node.descriptor_problem({**GOOD, "format": "x"})
    assert "Ed25519" in node.descriptor_problem({**GOOD, "node_key": {"crv": "P-256"}})
    assert "lacks capabilities: tool-dispatch" in node.descriptor_problem({**GOOD, "capabilities": ["chat"]})
    replies += [GOOD, httpx.Response(404), httpx.Response(200, text="x"), httpx.ConnectError("x")]
    assert node.fetch_descriptor(SECTION) == GOOD
    assert [node.fetch_descriptor(SECTION) for _ in range(3)] == [None, None, None]


def test_a_v1_descriptor_gets_a_clear_error():
    problem = node.descriptor_problem({**GOOD, "format": node.LEGACY_DESCRIPTOR_FORMAT, "accepted_issuers": ["x"]})
    assert "v1 descriptor" in problem and "identity provider" in problem


def test_the_owner_binding_is_read_from_the_descriptor():
    refused = {"mesh": {"owner_binding": {"state": "refused", "code": "node.legacy_owner"}}}
    assert node.owner_binding(refused)["code"] == "node.legacy_owner"
    assert node.owner_binding(GOOD) == {} and node.owner_binding(None) == {}
    assert node.owner_binding({"mesh": {"owner_binding": "x"}}) == {}


def test_the_caller_comes_from_the_environment_or_files(tmp_path, monkeypatch):
    monkeypatch.delenv(node.TICKET_VARIABLE, raising=False)
    monkeypatch.delenv(node.KEY_VARIABLE, raising=False)
    with pytest.raises(SystemExit, match="ASTRAL_CALLER_TICKET"):
        node.load_caller(None, None)
    monkeypatch.setenv(node.TICKET_VARIABLE, f" {TICKET} \n")
    monkeypatch.setenv(node.KEY_VARIABLE, KEY)
    assert node.load_caller(None, None).ticket_id == "ticket-1"
    ticket, key = tmp_path / "ticket", tmp_path / "key"
    ticket.write_text(TICKET + "\n")
    key.write_text("  \n")
    with pytest.raises(SystemExit, match="ASTRAL_CALLER_KEY"):
        node.load_caller(str(ticket), str(key))
    key.write_text(KEY)
    assert node.load_caller(str(ticket), str(key)).ticket_id == "ticket-1"


@pytest.mark.parametrize("ticket,key", [("nope", KEY), (TICKET, "nope"), (node.TICKET_TEXT + "!!", KEY),
                                        (node.TICKET_TEXT + node.b64(b"[]"), KEY),
                                        (TICKET, node.KEY_TEXT + node.b64(b"short"))])
def test_a_malformed_ticket_or_key_is_refused(ticket, key, monkeypatch):
    with pytest.raises(node.NodeError, match="caller ticket must be"):
        node.Caller(ticket, key)
    monkeypatch.setenv(node.TICKET_VARIABLE, ticket)
    monkeypatch.setenv(node.KEY_VARIABLE, key)
    with pytest.raises(SystemExit, match="caller ticket must be"):
        node.load_caller(None, None)


def test_a_request_proof_is_canonical_signed_by_the_holder_and_bound_to_the_body():
    headers = CALLER.headers("POST", "/a2a", b'{"x":1}')
    assert headers["Authorization"] == "AstralCaller " + TICKET[len(node.TICKET_TEXT):]
    raw = node._unb64(headers[node.PROOF_HEADER])
    proof = json.loads(raw)
    assert set(proof) == {"v", "ticket", "method", "target", "body", "ms", "nonce", "sig"}
    assert (proof["ticket"], proof["method"], proof["target"]) == ("ticket-1", "POST", "/a2a")
    assert proof["body"] == "sha256:" + hashlib.sha256(b'{"x":1}').hexdigest()
    unsigned = {k: v for k, v in proof.items() if k != "sig"}
    assert raw == node._canonical({**unsigned, "sig": proof["sig"]})
    public = ed25519.Ed25519PrivateKey.from_private_bytes(SEED).public_key()
    public.verify(node._unb64(proof["sig"]), node.PROOF_CONTEXT + node._canonical(unsigned))
    other = json.loads(node._unb64(CALLER.headers("POST", "/a2a", b"")[node.PROOF_HEADER]))
    assert other["nonce"] != proof["nonce"] and other["body"] == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_the_proof_bytes_match_the_kernels_canonical_json():
    kernel = pytest.importorskip("astral_kernel")
    fields = {"v": 1, "ticket": "t", "method": "POST", "target": "/a2a?x=\u00e9", "body": "sha256:" + "a" * 64,
              "ms": 1_700_000_000_000, "nonce": node.b64(os.urandom(16))}
    assert node._canonical(fields) == bytes(kernel.canonical_json(json.dumps(fields).encode()))


def test_pairing_refusals_are_explained():
    for code, words in (("node.loopback_required", "own machine"), ("node.legacy_owner", "untouched"),
                        ("node.pairing_rate_limited", "wait a minute"), ("mesh_unavailable", "no mesh peer"),
                        ("node.owner_mismatch", "different owner")):
        assert words in node.pairing_refusal(409, {"detail": code})
    assert "(HTTP 418) weird" in node.pairing_refusal(418, {"detail": "weird"})
    assert "HTTP 500" in node.pairing_refusal(500, {})


def test_the_offer_is_printed_for_the_app_or_explained():
    offer = {"state": "offer", "code": "1111 2222", "payload": "PAY", "expires_ms": 5_000_000}
    text = node.describe_offer(offer, NodeSection(public_url="https://node.test"))
    assert "1111 2222" in text and "PAY" in text and "https://node.test" in text and "5000" in text
    assert "http://127.0.0.1:8001" in node.describe_offer(offer, SECTION)
    joined = {"state": "enrolled", "mesh_id": "m1", "member_id": "d1"}
    assert node.describe_offer(joined, SECTION) == "the node is already in the mesh m1 as member d1"
    with pytest.raises(node.NodeError, match="no offer"):
        node.describe_offer({"state": "offer"}, SECTION)


def task(text="the answer", state="TASK_STATE_COMPLETED", context="ctx-1"):
    return {"result": {"task": {"id": "t", "contextId": context, "status": {
        "state": state, "message": {"parts": [{"text": text}, {"data": {"tools": []}}]}}}}}


def test_a_question_goes_to_the_a2a_endpoint_and_continues_a_context(served):
    seen, replies = served
    replies += [task(), task("second", context="ctx-1")]
    assert node.ask(SECTION, CALLER, "hello") == "the answer"
    request = seen[0]
    assert request.url.path == "/a2a" and request.headers["a2a-version"] == "1.0"
    assert request.headers["authorization"].startswith("AstralCaller ")
    proof = json.loads(node._unb64(request.headers["astral-caller-proof"]))
    assert proof["body"] == "sha256:" + hashlib.sha256(request.content).hexdigest()
    body = json.loads(request.content)
    assert body["method"] == "SendMessage" and body["params"]["message"]["parts"] == [{"text": "hello"}]
    assert "contextId" not in body["params"]["message"]
    text, context = node.converse(SECTION, CALLER, "again", "ctx-1")
    assert (text, context) == ("second", "ctx-1")
    assert json.loads(seen[1].content)["params"]["message"]["contextId"] == "ctx-1"


def test_answers_include_artifact_text_and_failures_raise(served):
    _, replies = served
    document = task("status text")
    document["result"]["task"]["artifacts"] = [{"parts": [{"text": "from an artifact"}]}]
    replies += [document, task("it broke", state="TASK_STATE_FAILED"), {"error": {"message": "bad request"}},
                httpx.Response(401), httpx.Response(200, text="<html>"), task("", state="TASK_STATE_FAILED")]
    assert node.ask(SECTION, CALLER, "q") == "status text\nfrom an artifact"
    with pytest.raises(node.NodeError, match="it broke"):
        node.ask(SECTION, CALLER, "q")
    with pytest.raises(node.NodeError, match="bad request"):
        node.ask(SECTION, CALLER, "q")
    with pytest.raises(node.NodeError, match="refused the caller ticket"):
        node.ask(SECTION, CALLER, "q")
    with pytest.raises(node.NodeError, match="no JSON"):
        node.ask(SECTION, CALLER, "q")
    with pytest.raises(node.NodeError, match="could not answer"):
        node.ask(SECTION, CALLER, "q")
