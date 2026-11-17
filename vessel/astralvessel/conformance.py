# Black-box acceptance checks for a running node over its public HTTP surface: health, descriptor, A2A with a caller
# ticket, and pairing. operate.py runs them as `astralvessel conformance`; each check prints PASS or FAIL.

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from . import caller as callers
from . import node
from .deployment import Deployment


@dataclass
class Context:
    deployment: Deployment
    caller: callers.Caller
    offer: Callable[[], dict]


MESSAGE = {"messageId": "c1", "role": "ROLE_USER", "parts": [{"text": "conformance check"}]}
JWT_SHAPED = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJjb25mb3JtYW5jZSJ9.c2ln"

Check = tuple[str, Callable[[Context], str | None]]


def _get(ctx: Context, path: str, **kw) -> httpx.Response:
    return node._call(ctx.deployment.node, "GET", path, **kw)


def ready(ctx: Context) -> str | None:
    state = node.health(ctx.deployment.node)
    return None if state == "ok" else state


def descriptor(ctx: Context) -> str | None:
    return node.descriptor_problem(node.fetch_descriptor(ctx.deployment.node))


def node_key_published(ctx: Context) -> str | None:
    response = _get(ctx, "/.well-known/astral-node/jwks.json")
    keys = response.json().get("keys", []) if response.status_code == 200 else []
    return None if keys and keys[0].get("crv") == "Ed25519" else "the node publishes no Ed25519 key"


def agent_card(ctx: Context) -> str | None:
    response = _get(ctx, "/a2a/.well-known/agent-card.json")
    if response.status_code != 200:
        return f"the agent card answered HTTP {response.status_code}"
    card = response.json()
    interfaces = card.get("supportedInterfaces") or []
    ok = any(i.get("protocolBinding") == "JSONRPC" and i.get("protocolVersion") == "1.0" for i in interfaces)
    return None if ok and (card.get("capabilities") or {}).get("streaming") else "the card lacks JSON-RPC 1.0 streaming"


def a2a_needs_a_ticket(ctx: Context) -> str | None:
    status = node.a2a_call(ctx.deployment.node, None, "SendMessage", MESSAGE).status_code
    return None if status == 401 else f"an anonymous call answered HTTP {status}"


def a2a_refuses_a_bearer_token(ctx: Context) -> str | None:
    status = node.a2a_call(ctx.deployment.node, None, "SendMessage", MESSAGE,
                           headers={"Authorization": f"Bearer {JWT_SHAPED}"}).status_code
    return None if status == 401 else f"a JWT bearer token answered HTTP {status}"


def a2a_refuses_a_forged_proof(ctx: Context) -> str | None:
    forger = callers.Caller(ctx.caller.ticket_text, callers.KEY_TEXT + callers.b64(os.urandom(32)))
    status = node.a2a_call(ctx.deployment.node, forger, "SendMessage", MESSAGE).status_code
    return None if status == 401 else f"a proof from another key answered HTTP {status}"


def a2a_answers_the_caller(ctx: Context) -> str | None:
    response = node.a2a_call(ctx.deployment.node, ctx.caller, "SendMessage", MESSAGE)
    task = (response.json().get("result") or {}).get("task") if response.status_code == 200 else None
    state = (task or {}).get("status", {}).get("state", "")
    return None if state.endswith("COMPLETED") else f"the caller's message ended as {state or response.status_code}"


def a2a_refuses_unknown_methods(ctx: Context) -> str | None:
    response = node.a2a_call(ctx.deployment.node, ctx.caller, "NoSuchMethod", MESSAGE)
    return None if response.status_code == 200 and "error" in response.json() else "an unknown method was accepted"


def pairing_needs_the_secret(ctx: Context) -> str | None:
    response = node._call(ctx.deployment.node, "POST", "/api/node/mesh/offer", body={})
    status = response.status_code
    return None if status in (401, 403) else f"a pairing call with no secret answered HTTP {status}"


def old_pairing_is_gone(ctx: Context) -> str | None:
    response = node._call(ctx.deployment.node, "POST", "/api/node/pairing", body={})
    status = response.status_code
    return None if status >= 400 else f"the owner-token pairing route answered HTTP {status}"


def pairing_offers_a_mesh_code(ctx: Context) -> str | None:
    reply = ctx.offer()
    if reply.get("status") != 200:
        return node.pairing_refusal(reply.get("status", 0), reply)
    if reply.get("state") == "enrolled":
        return None if reply.get("mesh_id") and reply.get("member_id") else "the node is enrolled with no mesh id"
    if reply.get("state") != "offer":
        return f"no offer was made (state {reply.get('state')})"
    try:
        json.loads(base64.urlsafe_b64decode(reply["payload"] + "=" * (-len(reply["payload"]) % 4)))
    except (KeyError, ValueError):
        return "the payload is not an encoded document"
    return None if reply.get("code") else "the offer has no code"


CHECKS: tuple[Check, ...] = (
    ("the node reports ready with its database up", ready),
    ("the v2 descriptor names the node's key and capabilities", descriptor),
    ("the node publishes its Ed25519 key", node_key_published),
    ("the A2A card advertises JSON-RPC 1.0 with streaming", agent_card),
    ("A2A refuses a call with no ticket", a2a_needs_a_ticket),
    ("A2A refuses a JWT bearer token", a2a_refuses_a_bearer_token),
    ("A2A refuses a proof signed by another key", a2a_refuses_a_forged_proof),
    ("A2A answers the caller", a2a_answers_the_caller),
    ("A2A rejects an unknown method", a2a_refuses_unknown_methods),
    ("pairing refuses a caller without the node's secret", pairing_needs_the_secret),
    ("the owner-token pairing route is gone", old_pairing_is_gone),
    ("pairing offers a code and payload, or names the mesh joined", pairing_offers_a_mesh_code),
)


def run(deployment: Deployment, caller: callers.Caller, offer: Callable[[], dict]) -> list[str]:
    ctx = Context(deployment, caller, offer)
    failed: list[str] = []
    for name, check in CHECKS:
        try:
            problem = check(ctx)
        except (node.NodeError, ValueError, KeyError) as exc:
            problem = str(exc) or type(exc).__name__
        print(f"{'PASS' if problem is None else 'FAIL'}  {name}" + (f" ({problem})" if problem else ""))
        if problem is not None:
            failed.append(name)
    return failed
