# The node client: health, v2 descriptor checks, caller tickets and holder proofs, the pairing wording and A2A
# questions, against a scripted node.

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from astralvessel import caller as callers
from astralvessel import node
from astralvessel.deployment import NodeSection

SECTION = NodeSection()
GOOD = {"format": node.DESCRIPTOR_FORMAT, "node_id": "n", "node_key": {"crv": "Ed25519"},
        "capabilities": ["chat", "tool-dispatch", "mesh-peer"], "owner_hash": "sha256:" + "0" * 64}
SEED = bytes(range(32))
TICKET = callers.TICKET_TEXT + callers.b64(json.dumps({"id": "ticket-1", "v": 1}).encode())
KEY = callers.KEY_TEXT + callers.b64(SEED)
CALLER = callers.Caller(TICKET, KEY)
MESSAGE = {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]}


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
    proof = json.loads(callers.unb64(request.headers["astral-caller-proof"]))
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


def test_the_message_comes_from_the_caller_and_no_bearer_is_ever_sent(served):
    seen, replies = served
    replies.append(task())
    node.a2a_call(SECTION, CALLER, "SendMessage", MESSAGE)
    sent = json.loads(seen[0].content)
    assert sent["params"]["message"] == MESSAGE
    assert not seen[0].headers["authorization"].lower().startswith("bearer")
