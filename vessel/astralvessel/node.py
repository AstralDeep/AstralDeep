# A small client for the personal node a deployment runs: its health and descriptor, the owner's pairing offer, and
# A2A questions. operate.py calls it for up, check, pair, ask and chat; it never prints or stores the owner's token.

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .deployment import NodeSection, OwnerSection

DESCRIPTOR_FORMAT = "astral.node.descriptor/v1"
TOKEN_VARIABLE = "ASTRAL_OWNER_TOKEN"
REQUIRED_CAPABILITIES = ("chat", "tool-dispatch")
PAIRING_LABEL = "astralvessel"


class NodeError(Exception):
    pass


def base_url(node: NodeSection) -> str:
    host = "127.0.0.1" if node.bind in ("0.0.0.0", "::") else node.bind
    return f"http://{host}:{node.port}" if ":" not in host else f"http://[{host}]:{node.port}"


def _call(node: NodeSection, method: str, path: str, *, token: str | None = None, body: dict[str, Any] | None = None,
          timeout: float = 15, headers: dict[str, str] | None = None) -> httpx.Response:
    sent = dict(headers or {})
    if token:
        sent["Authorization"] = f"Bearer {token}"
    try:
        return httpx.request(method, base_url(node) + path, json=body, headers=sent, timeout=timeout)
    except httpx.HTTPError as exc:
        raise NodeError(f"the node at {base_url(node)} is not answering ({type(exc).__name__})") from None


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


def owner_hash(owner_id: str) -> str:
    return "sha256:" + hashlib.sha256(owner_id.encode()).hexdigest()


def descriptor_problem(document: dict[str, Any] | None, owner: OwnerSection) -> str | None:
    if not isinstance(document, dict):
        return "no descriptor"
    if document.get("format") != DESCRIPTOR_FORMAT:
        return f"unknown descriptor format {document.get('format')!r}"
    if not document.get("node_id") or (document.get("node_key") or {}).get("crv") != "Ed25519":
        return "the descriptor has no Ed25519 node key"
    if owner.issuer.rstrip("/") not in (document.get("accepted_issuers") or []):
        return "the node does not accept the owner's issuer"
    missing = [c for c in REQUIRED_CAPABILITIES if c not in (document.get("capabilities") or [])]
    if missing:
        return f"the node lacks capabilities: {', '.join(missing)}"
    bound = document.get("owner_hash")
    if owner.owner_id and bound is not None and bound != owner_hash(owner.owner_id):
        return "the node is bound to a different owner than the deployment names"
    return None


def owner_token(token_file: str | None) -> str:
    # The owner's access token comes from the environment or a file, never from the command line.
    raw = Path(token_file).expanduser().read_text() if token_file else os.environ.get(TOKEN_VARIABLE, "")
    token = raw.strip()
    if not token:
        raise SystemExit(f"set {TOKEN_VARIABLE} to an access token for the node's owner, or pass --token-file")
    return token


def _device_key() -> dict[str, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": base64.urlsafe_b64encode(raw).rstrip(b"=").decode()}


def request_pairing(node: NodeSection, token: str, label: str | None) -> dict[str, Any]:
    response = _call(node, "POST", "/api/node/pairing", token=token,
                     body={"label": label or PAIRING_LABEL, "device_key": _device_key()})
    if response.status_code in (401, 403):
        raise NodeError("the node refused the owner token: use a current access token from the owner's issuer, "
                        "for the one owner this node serves")
    if response.status_code != 200:
        raise NodeError(f"the node refused the pairing request (HTTP {response.status_code}): "
                        f"{_detail(response)}")
    return response.json()


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", ""))[:200]
    except (ValueError, AttributeError):
        return ""


def describe_offer(offer: dict[str, Any], node: NodeSection) -> str:
    mesh = offer.get("mesh")
    if not mesh:
        raise NodeError("this node has no mesh peer, so it has nothing to pair; start it with the kernel binding")
    if mesh.get("state") != "offer":
        if mesh.get("error"):
            raise NodeError(f"the node cannot offer pairing: {mesh['error']}")
        return f"the node is already in the mesh {mesh.get('mesh_id')} as member {mesh.get('member_id')}"
    expires = offer["pairing"]["expires_at"] // 1000
    return "\n".join([
        "Add this node from the AstralDeep app on a device you already use: choose Add a device, then enter the",
        "code or paste the payload. Approve it on that device.",
        "",
        f"  code     {mesh['code']}",
        f"  payload  {mesh['payload']}",
        f"  node     {node.public_url or base_url(node)}",
        "",
        f"The offer expires at unix time {expires}. Run astralvessel pair again for a new one.",
    ])


def _text(task: dict[str, Any]) -> str:
    status = (task.get("status") or {}).get("message") or {}
    parts = [p.get("text", "") for p in status.get("parts") or [] if "text" in p]
    for artifact in task.get("artifacts") or []:
        parts += [p.get("text", "") for p in artifact.get("parts") or [] if "text" in p]
    return "\n".join(part for part in parts if part).strip()


def converse(node: NodeSection, token: str, text: str, context: str | None = None) -> tuple[str, str | None]:
    message: dict[str, Any] = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}]}
    if context:
        message["contextId"] = context
    response = _call(node, "POST", "/a2a", token=token, timeout=600, headers={"A2A-Version": "1.0"},
                     body={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendMessage",
                           "params": {"message": message}})
    if response.status_code in (401, 403):
        raise NodeError("the node refused the owner token: use a current access token for the node's owner")
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


def ask(node: NodeSection, token: str, text: str) -> str:
    return converse(node, token, text)[0]
