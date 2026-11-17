# Black-box acceptance checks for a running node, over its public HTTP surface: health, descriptor, A2A and pairing.
# operate.py runs them as `astralvessel conformance`; each check prints PASS or FAIL and a failure exits non-zero.

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import httpx

from . import node
from .deployment import Deployment

Check = tuple[str, Callable[[Deployment, str], str | None]]


def _get(deployment: Deployment, path: str, token: str | None = None, **kw) -> httpx.Response:
    return node._call(deployment.node, "GET", path, token=token, **kw)


def _a2a(deployment: Deployment, token: str | None, method: str = "SendMessage") -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": "c1", "method": method, "params": {"message": {
        "messageId": "c1", "role": "ROLE_USER", "parts": [{"text": "conformance check"}]}}}
    return node._call(deployment.node, "POST", "/a2a", token=token, body=body, headers={"A2A-Version": "1.0"})


def ready(deployment: Deployment, token: str) -> str | None:
    state = node.health(deployment.node)
    return None if state == "ok" else state


def descriptor(deployment: Deployment, token: str) -> str | None:
    return node.descriptor_problem(node.fetch_descriptor(deployment.node), deployment.owner)


def node_key_published(deployment: Deployment, token: str) -> str | None:
    response = _get(deployment, "/.well-known/astral-node/jwks.json")
    keys = response.json().get("keys", []) if response.status_code == 200 else []
    return None if keys and keys[0].get("crv") == "Ed25519" else "the node publishes no Ed25519 key"


def agent_card(deployment: Deployment, token: str) -> str | None:
    response = _get(deployment, "/a2a/.well-known/agent-card.json")
    if response.status_code != 200:
        return f"the agent card answered HTTP {response.status_code}"
    card = response.json()
    interfaces = card.get("supportedInterfaces") or []
    ok = any(i.get("protocolBinding") == "JSONRPC" and i.get("protocolVersion") == "1.0" for i in interfaces)
    return None if ok and (card.get("capabilities") or {}).get("streaming") else "the card lacks JSON-RPC 1.0 streaming"


def a2a_needs_a_token(deployment: Deployment, token: str) -> str | None:
    status = _a2a(deployment, None).status_code
    return None if status == 401 else f"an anonymous call answered HTTP {status}"


def a2a_refuses_a_forged_token(deployment: Deployment, token: str) -> str | None:
    forged = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
    status = _a2a(deployment, forged).status_code
    return None if status in (401, 403) else f"a forged token answered HTTP {status}"


def a2a_answers_the_owner(deployment: Deployment, token: str) -> str | None:
    response = _a2a(deployment, token)
    task = (response.json().get("result") or {}).get("task") if response.status_code == 200 else None
    state = (task or {}).get("status", {}).get("state", "")
    return None if state.endswith("COMPLETED") else f"the owner's message ended as {state or response.status_code}"


def a2a_refuses_unknown_methods(deployment: Deployment, token: str) -> str | None:
    response = _a2a(deployment, token, "NoSuchMethod")
    return None if response.status_code == 200 and "error" in response.json() else "an unknown method was accepted"


def pairing_needs_the_owner(deployment: Deployment, token: str) -> str | None:
    response = node._call(deployment.node, "POST", "/api/node/pairing", body={})
    return None if response.status_code in (401, 403) else f"anonymous pairing answered HTTP {response.status_code}"


def pairing_offers_a_mesh_code(deployment: Deployment, token: str) -> str | None:
    try:
        offer = node.request_pairing(deployment.node, token, "conformance")
    except node.NodeError as exc:
        return str(exc)
    mesh = offer.get("mesh") or {}
    if mesh.get("state") == "enrolled":
        return None if mesh.get("mesh_id") and mesh.get("member_id") else "the node is enrolled with no mesh id"
    if mesh.get("state") != "offer":
        return f"no offer was made (state {mesh.get('state')})"
    try:
        json.loads(base64.urlsafe_b64decode(mesh["payload"] + "=" * (-len(mesh["payload"]) % 4)))
    except (KeyError, ValueError):
        return "the payload is not an encoded document"
    return None if mesh.get("code") else "the offer has no code"


CHECKS: tuple[Check, ...] = (
    ("the node reports ready with its database up", ready),
    ("the descriptor names the owner's issuer and the node's capabilities", descriptor),
    ("the node publishes its Ed25519 key", node_key_published),
    ("the A2A card advertises JSON-RPC 1.0 with streaming", agent_card),
    ("A2A refuses a call with no token", a2a_needs_a_token),
    ("A2A refuses a forged token", a2a_refuses_a_forged_token),
    ("A2A answers the owner", a2a_answers_the_owner),
    ("A2A rejects an unknown method", a2a_refuses_unknown_methods),
    ("pairing refuses a caller who is not the owner", pairing_needs_the_owner),
    ("pairing offers the owner a code and payload, or names the mesh joined", pairing_offers_a_mesh_code),
)


def run(deployment: Deployment, token: str) -> list[str]:
    failed: list[str] = []
    for name, check in CHECKS:
        try:
            problem = check(deployment, token)
        except (node.NodeError, ValueError, KeyError) as exc:
            problem = str(exc) or type(exc).__name__
        print(f"{'PASS' if problem is None else 'FAIL'}  {name}" + (f" ({problem})" if problem else ""))
        if problem is not None:
            failed.append(name)
    return failed
