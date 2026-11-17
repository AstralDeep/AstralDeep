# The node client: health, descriptor checks, the owner's token, the pairing offer and A2A questions, against a
# scripted node.

from __future__ import annotations

import json

import httpx
import pytest

from astralvessel import node
from astralvessel.deployment import NodeSection, OwnerSection

ISSUER = "https://keycloak.test/realms/astral"
SECTION = NodeSection()
OWNER = OwnerSection(issuer=ISSUER, client_id="c", subject="owner-1")
GOOD = {"format": node.DESCRIPTOR_FORMAT, "node_id": "n", "node_key": {"crv": "Ed25519"}, "accepted_issuers": [ISSUER],
        "capabilities": ["chat", "tool-dispatch", "mesh-peer"], "owner_hash": None}


@pytest.fixture
def served(monkeypatch):
    seen: list[httpx.Request] = []
    replies: list = []

    def request(method, url, **kwargs):
        built = httpx.Request(method, url, headers=kwargs.get("headers"), json=kwargs.get("json"))
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


def test_the_descriptor_must_match_the_owner(served):
    _, replies = served
    assert node.descriptor_problem(GOOD, OWNER) is None
    assert node.descriptor_problem(None, OWNER) == "no descriptor"
    assert "unknown descriptor format" in node.descriptor_problem({**GOOD, "format": "x"}, OWNER)
    assert "Ed25519" in node.descriptor_problem({**GOOD, "node_key": {"crv": "P-256"}}, OWNER)
    assert "does not accept" in node.descriptor_problem({**GOOD, "accepted_issuers": ["https://x"]}, OWNER)
    assert "lacks capabilities: tool-dispatch" in node.descriptor_problem({**GOOD, "capabilities": ["chat"]}, OWNER)
    bound = {**GOOD, "owner_hash": node.owner_hash(OWNER.owner_id)}
    assert node.descriptor_problem(bound, OWNER) is None
    assert "different owner" in node.descriptor_problem({**GOOD, "owner_hash": node.owner_hash("oidc.x.y")}, OWNER)
    assert node.descriptor_problem({**GOOD, "owner_hash": "sha256:other"}, OwnerSection(issuer=ISSUER, client_id="c")) \
        is None
    replies += [GOOD, httpx.Response(404), httpx.Response(200, text="x"), httpx.ConnectError("x")]
    assert node.fetch_descriptor(SECTION) == GOOD
    assert [node.fetch_descriptor(SECTION) for _ in range(3)] == [None, None, None]


def test_the_owner_token_comes_from_the_environment_or_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv(node.TOKEN_VARIABLE, raising=False)
    with pytest.raises(SystemExit, match="ASTRAL_OWNER_TOKEN"):
        node.owner_token(None)
    monkeypatch.setenv(node.TOKEN_VARIABLE, " from-env \n")
    assert node.owner_token(None) == "from-env"
    file = tmp_path / "token"
    file.write_text("from-file\n")
    assert node.owner_token(str(file)) == "from-file"
    file.write_text("  \n")
    with pytest.raises(SystemExit, match="ASTRAL_OWNER_TOKEN"):
        node.owner_token(str(file))


def test_a_pairing_request_carries_only_a_public_device_key(served):
    seen, replies = served
    replies.append({"pairing": {"expires_at": 5000}, "mesh": {"state": "offer", "code": "1111 2222", "payload": "P"}})
    offer = node.request_pairing(SECTION, "secret-token", None)
    assert offer["mesh"]["code"] == "1111 2222"
    request = seen[0]
    assert request.headers["authorization"] == "Bearer secret-token" and request.url.path == "/api/node/pairing"
    body = json.loads(request.content)
    assert body["label"] == "astralvessel" and set(body["device_key"]) == {"kty", "crv", "x"}
    assert body["device_key"]["crv"] == "Ed25519"
    replies.append({"pairing": {"expires_at": 5000}, "mesh": None})
    node.request_pairing(SECTION, "t", "my label")
    assert json.loads(seen[1].content)["label"] == "my label"


def test_pairing_failures_are_explained_without_the_token(served):
    _, replies = served
    replies += [httpx.Response(401), httpx.Response(403), httpx.Response(400, json={"detail": "label is required"}),
                httpx.Response(500, text="boom"), httpx.ConnectError("x")]
    for _ in range(2):
        with pytest.raises(node.NodeError, match="refused the owner token") as caught:
            node.request_pairing(SECTION, "secret-token", None)
        assert "secret-token" not in str(caught.value)
    with pytest.raises(node.NodeError, match="HTTP 400.*label is required"):
        node.request_pairing(SECTION, "t", None)
    with pytest.raises(node.NodeError, match="HTTP 500"):
        node.request_pairing(SECTION, "t", None)
    with pytest.raises(node.NodeError, match="not answering"):
        node.request_pairing(SECTION, "t", None)


def test_the_offer_is_printed_for_the_app_or_explained():
    offer = {"pairing": {"expires_at": 5_000_000}, "mesh": {"state": "offer", "code": "1111 2222", "payload": "PAY"}}
    text = node.describe_offer(offer, NodeSection(public_url="https://node.test"))
    assert "1111 2222" in text and "PAY" in text and "https://node.test" in text and "5000" in text
    assert "http://127.0.0.1:8001" in node.describe_offer(offer, SECTION)
    joined = {"pairing": {}, "mesh": {"state": "enrolled", "mesh_id": "m1", "member_id": "d1"}}
    assert node.describe_offer(joined, SECTION) == "the node is already in the mesh m1 as member d1"
    with pytest.raises(node.NodeError, match="no mesh peer"):
        node.describe_offer({"pairing": {}}, SECTION)
    with pytest.raises(node.NodeError, match="mailbox_unavailable"):
        node.describe_offer({"mesh": {"state": "not_enrolled", "error": "mailbox_unavailable"}}, SECTION)


def task(text="the answer", state="TASK_STATE_COMPLETED", context="ctx-1"):
    return {"result": {"task": {"id": "t", "contextId": context, "status": {
        "state": state, "message": {"parts": [{"text": text}, {"data": {"tools": []}}]}}}}}


def test_a_question_goes_to_the_a2a_endpoint_and_continues_a_context(served):
    seen, replies = served
    replies += [task(), task("second", context="ctx-1")]
    assert node.ask(SECTION, "tok", "hello") == "the answer"
    request = seen[0]
    assert request.url.path == "/a2a" and request.headers["a2a-version"] == "1.0"
    assert request.headers["authorization"] == "Bearer tok"
    body = json.loads(request.content)
    assert body["method"] == "SendMessage" and body["params"]["message"]["parts"] == [{"text": "hello"}]
    assert "contextId" not in body["params"]["message"]
    text, context = node.converse(SECTION, "tok", "again", "ctx-1")
    assert (text, context) == ("second", "ctx-1")
    assert json.loads(seen[1].content)["params"]["message"]["contextId"] == "ctx-1"


def test_answers_include_artifact_text_and_failures_raise(served):
    _, replies = served
    document = task("status text")
    document["result"]["task"]["artifacts"] = [{"parts": [{"text": "from an artifact"}]}]
    replies += [document, task("it broke", state="TASK_STATE_FAILED"), {"error": {"message": "bad request"}},
                httpx.Response(401), httpx.Response(200, text="<html>"), task("", state="TASK_STATE_FAILED")]
    assert node.ask(SECTION, "t", "q") == "status text\nfrom an artifact"
    with pytest.raises(node.NodeError, match="it broke"):
        node.ask(SECTION, "t", "q")
    with pytest.raises(node.NodeError, match="bad request"):
        node.ask(SECTION, "t", "q")
    with pytest.raises(node.NodeError, match="refused the owner token"):
        node.ask(SECTION, "t", "q")
    with pytest.raises(node.NodeError, match="no JSON"):
        node.ask(SECTION, "t", "q")
    with pytest.raises(node.NodeError, match="could not answer"):
        node.ask(SECTION, "t", "q")
