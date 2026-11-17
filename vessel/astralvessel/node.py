# A small client for the personal node a deployment runs: its health and v2 descriptor, the pairing offer's wording,
# and A2A questions asked with a caller ticket and holder proof. operate.py calls it for up, check, pair, ask and chat;
# it never prints the holder key and sends no bearer token.

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from .deployment import NodeSection

DESCRIPTOR_FORMAT = "astral.node.descriptor/v2"
LEGACY_DESCRIPTOR_FORMAT = "astral.node.descriptor/v1"
TICKET_VARIABLE = "ASTRAL_CALLER_TICKET"
KEY_VARIABLE = "ASTRAL_CALLER_KEY"
TICKET_TEXT = "astral-caller-ticket:v1:"
KEY_TEXT = "astral-caller-key:v1:"
SCHEME = "AstralCaller "
PROOF_HEADER = "Astral-Caller-Proof"
PROOF_CONTEXT = b"astral.caller-proof/v1\n"
REQUIRED_CAPABILITIES = ("chat", "tool-dispatch")


class NodeError(Exception):
    pass


def base_url(node: NodeSection) -> str:
    host = "127.0.0.1" if node.bind in ("0.0.0.0", "::") else node.bind
    return f"http://{host}:{node.port}" if ":" not in host else f"http://[{host}]:{node.port}"


def _call(node: NodeSection, method: str, path: str, *, body: dict[str, Any] | None = None, timeout: float = 15,
          headers: dict[str, str] | None = None) -> httpx.Response:
    try:
        return httpx.request(method, base_url(node) + path, json=body, headers=dict(headers or {}), timeout=timeout)
    except httpx.HTTPError as exc:
        raise NodeError(f"the node at {base_url(node)} is not answering ({type(exc).__name__})") from None


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:200]
    except (ValueError, AttributeError):
        return ""


def health(node: NodeSection) -> str:
    # "ok" when the node reports itself ready with its database up; otherwise why not.
    try:
        response = _call(node, "GET", "/readyz", timeout=5)
    except NodeError as exc:
        return str(exc)
    if response.status_code != 200:
        return f"/readyz answered HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return "/readyz did not answer JSON"
    if body.get("status") == "ok" and body.get("db") == "ok":
        return "ok"
    return f"/readyz reports status {body.get('status')!r} and db {body.get('db')!r}"


def fetch_descriptor(node: NodeSection) -> dict[str, Any] | None:
    try:
        response = _call(node, "GET", "/api/node/descriptor", timeout=10)
        return response.json() if response.status_code == 200 else None
    except (NodeError, ValueError):
        return None


def descriptor_problem(document: dict[str, Any] | None) -> str | None:
    if not isinstance(document, dict):
        return "no descriptor"
    if document.get("format") == LEGACY_DESCRIPTOR_FORMAT:
        return ("the node publishes a v1 descriptor, so it still signs in through an identity provider; "
                "run a node image built for mesh identity")
    if document.get("format") != DESCRIPTOR_FORMAT:
        return f"unknown descriptor format {document.get('format')!r}"
    if not document.get("node_id") or (document.get("node_key") or {}).get("crv") != "Ed25519":
        return "the descriptor has no Ed25519 node key"
    missing = [c for c in REQUIRED_CAPABILITIES if c not in (document.get("capabilities") or [])]
    if missing:
        return f"the node lacks capabilities: {', '.join(missing)}"
    return None


def owner_binding(document: dict[str, Any] | None) -> dict[str, Any]:
    binding = ((document or {}).get("mesh") or {}).get("owner_binding")
    return binding if isinstance(binding, dict) else {}


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


class Caller:
    # The ticket is public; the holder key is the secret and is only ever used to sign.
    def __init__(self, ticket_text: str, key_text: str):
        try:
            if not ticket_text.startswith(TICKET_TEXT) or not key_text.startswith(KEY_TEXT):
                raise ValueError
            self.ticket_text = ticket_text
            self.ticket_id = json.loads(_unb64(ticket_text[len(TICKET_TEXT):]))["id"]
            self._key = ed25519.Ed25519PrivateKey.from_private_bytes(_unb64(key_text[len(KEY_TEXT):]))
        except (ValueError, KeyError, TypeError):
            raise NodeError("the caller ticket must be astral-caller-ticket:v1:..., and the key "
                            "astral-caller-key:v1:...") from None

    def headers(self, method: str, target: str, body: bytes = b"") -> dict[str, str]:
        fields: dict[str, Any] = {"v": 1, "ticket": self.ticket_id, "method": method, "target": target,
                                  "body": "sha256:" + hashlib.sha256(body).hexdigest(),
                                  "ms": time.time_ns() // 1_000_000, "nonce": b64(os.urandom(16))}
        fields["sig"] = b64(self._key.sign(PROOF_CONTEXT + _canonical(fields)))
        return {"Authorization": SCHEME + self.ticket_text[len(TICKET_TEXT):], PROOF_HEADER: b64(_canonical(fields))}


def load_caller(ticket_file: str | None, key_file: str | None) -> Caller:
    # The ticket and key come from the environment or files, never from the command line.
    def read(path: str | None, variable: str) -> str:
        raw = Path(path).expanduser().read_text() if path else os.environ.get(variable, "")
        return raw.strip()

    ticket, key = read(ticket_file, TICKET_VARIABLE), read(key_file, KEY_VARIABLE)
    if not ticket or not key:
        raise SystemExit(f"set {TICKET_VARIABLE} and {KEY_VARIABLE}, or pass --ticket-file and --key-file; the owner "
                         "mints a caller ticket in the AstralDeep app for this node's A2A messages")
    try:
        return Caller(ticket, key)
    except NodeError as exc:
        raise SystemExit(str(exc)) from None


PAIRING_REFUSALS = {
    "node.loopback_required": "the node only answers pairing requests from its own machine",
    "node.pairing_secret_required": "the node did not accept its pairing secret; run the command again",
    "node.legacy_owner": ("this node's data belongs to an owner who signed in through an identity provider. The node "
                          "keeps that data untouched and will not join a mesh; use a new project for a mesh node"),
    "node.owner_mismatch": "this node's data is bound to a different owner than the mesh it would join",
    "node.owner_binding_unavailable": "the node cannot read its owner binding; check its logs",
    "node.pairing_rate_limited": "the node has served too many offers; wait a minute and run the command again",
    "mesh_unavailable": "the node has no mesh peer running; start it with the kernel binding",
    "node_profile_inactive": "this node is not running the personal profile",
}


def pairing_refusal(status: int, body: dict[str, Any]) -> str:
    code = str(body.get("detail") or body.get("code") or "")
    return PAIRING_REFUSALS.get(code) or f"the node refused the pairing request (HTTP {status}) {code}".strip()


def describe_offer(offer: dict[str, Any], node: NodeSection) -> str:
    if offer.get("state") == "enrolled":
        return f"the node is already in the mesh {offer.get('mesh_id')} as member {offer.get('member_id')}"
    if offer.get("state") != "offer" or not offer.get("code") or not offer.get("payload"):
        raise NodeError("the node answered with no offer and no mesh")
    return "\n".join([
        "Add this node from the AstralDeep app on a device you already use: choose Add a device, then enter the",
        "code or paste the payload. Approve it on that device.",
        "",
        f"  code     {offer['code']}",
        f"  payload  {offer['payload']}",
        f"  node     {node.public_url or base_url(node)}",
        "",
        f"The offer expires at unix time {int(offer['expires_ms']) // 1000}. Run astralvessel pair again for a new "
        "one.",
    ])


def _text(task: dict[str, Any]) -> str:
    status = (task.get("status") or {}).get("message") or {}
    parts = [p.get("text", "") for p in status.get("parts") or [] if "text" in p]
    for artifact in task.get("artifacts") or []:
        parts += [p.get("text", "") for p in artifact.get("parts") or [] if "text" in p]
    return "\n".join(part for part in parts if part).strip()


def converse(node: NodeSection, caller: Caller, text: str, context: str | None = None) -> tuple[str, str | None]:
    message: dict[str, Any] = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}]}
    if context:
        message["contextId"] = context
    response = a2a_call(node, caller, "SendMessage", message, timeout=600)
    if response.status_code in (401, 403):
        raise NodeError(f"the node refused the caller ticket ({_detail(response) or response.status_code}): the owner "
                        "must mint a current one that covers A2A messages for this node")
    try:
        body = response.json()
    except ValueError:
        raise NodeError(f"the node answered HTTP {response.status_code} with no JSON") from None
    if "error" in body:
        raise NodeError(str(body["error"].get("message", "the node refused the request"))[:300])
    task = (body.get("result") or {}).get("task") or {}
    state = (task.get("status") or {}).get("state", "")
    answer = _text(task)
    if state.endswith("FAILED"):
        raise NodeError(answer or "the node could not answer")
    return answer, task.get("contextId")


def a2a_call(node: NodeSection, caller: Caller | None, method: str, message: dict[str, Any] | None = None,
             timeout: float = 15, headers: dict[str, str] | None = None) -> httpx.Response:
    raw = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method,
                      "params": {"message": message or {"messageId": "c1", "role": "ROLE_USER",
                                                        "parts": [{"text": "conformance check"}]}}}).encode()
    sent = {"A2A-Version": "1.0", "Content-Type": "application/json", **(headers or {})}
    if caller is not None:
        sent.update(caller.headers("POST", "/a2a", raw))
    try:
        return httpx.request("POST", base_url(node) + "/a2a", content=raw, headers=sent, timeout=timeout)
    except httpx.HTTPError as exc:
        raise NodeError(f"the node at {base_url(node)} is not answering ({type(exc).__name__})") from None


def ask(node: NodeSection, caller: Caller, text: str) -> str:
    return converse(node, caller, text)[0]
