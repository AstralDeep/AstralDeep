"""Reference verifier for the owner-minted caller ticket and its per-request holder proof, plus the ticket and proof builders the vector generator uses.
It reuses the preserved 105 reference for canonical JSON, hashes and signatures, and the vectors in vectors/caller-v1.json pin its behavior for every other implementation.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / "105-decentralized-personal-mesh/contracts/reference"
    ),
)
from astral_mesh_ref import crypto, ticket
from astral_mesh_ref.cj import b64url_decode, b64url_encode, canonical, strict_loads
from astral_mesh_ref.errors import MeshError

TICKET_CONTEXT = "astral.caller/v1"
PROOF_CONTEXT = "astral.caller-proof/v1"
SCHEME = "AstralCaller "
TICKET_TEXT = "astral-caller-ticket:v1:"
KEY_TEXT = "astral-caller-key:v1:"
A2A = "a2a.message"
MAX_HEADER = 8192
MAX_TOOLS = 32
MAX_LABEL = 64
MAX_LIFETIME_S = 2592000
LIFETIMES = {"1h": 3600, "1d": 86400, "7d": 604800, "30d": 2592000}
DEFAULT_LIFETIME = "7d"
FUTURE_SKEW_MS = 300000
PROOF_WINDOW_MS = 120000
PROOF_AHEAD_MS = 5000
RESTART_PAUSE_MS = 5000
MINTER_KINDS = ("desktop", "pocket", "satellite")
HOLDER_TEXT = "astral-caller-holder:v1:"
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
TICKET_FIELDS = {
    "v",
    "id",
    "mesh",
    "device",
    "node",
    "holder",
    "label",
    "tools",
    "issued_ms",
    "expires_ms",
    "stop",
    "sig",
}
PROOF_FIELDS = {"v", "ticket", "method", "target", "body", "ms", "nonce", "sig"}
CODES = (
    "caller.malformed",
    "caller.proof_malformed",
    "caller.mesh_mismatch",
    "caller.wrong_node",
    "caller.device_removed",
    "caller.wrong_minter",
    "caller.bad_signature",
    "caller.stopped",
    "caller.expired",
    "caller.not_yet_valid",
    "caller.revoked",
    "caller.proof_mismatch",
    "caller.proof_stale",
    "caller.proof_bad_signature",
    "caller.replayed",
    "caller.tool_not_covered",
    "caller.tool_changed",
    "caller.tool_unavailable",
    "caller.no_catalog",
)


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _raw(value: Any, size: int) -> bool:
    try:
        return len(b64url_decode(value)) == size
    except MeshError:
        return False


def _check_tool(tool: Any) -> str:
    if not isinstance(tool, dict) or set(tool) != {"name", "def"}:
        raise MeshError("caller.malformed", "tool fields")
    name, definition = tool["name"], tool["def"]
    if name == A2A:
        if definition is not None:
            raise MeshError("caller.malformed", "a2a definition")
    elif (
        not isinstance(name, str)
        or not TOOL_NAME.fullmatch(name)
        or not crypto.is_id(definition)
    ):
        raise MeshError("caller.malformed", "tool")
    return name


def check_ticket(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != TICKET_FIELDS:
        raise MeshError("caller.malformed", "fields")
    bad = (
        value["v"] != 1
        or isinstance(value["v"], bool)
        or not _raw(value["id"], 16)
        or not all(crypto.is_id(value[k]) for k in ("mesh", "device", "node"))
        or not _raw(value["holder"], 32)
        or not _raw(value["sig"], 64)
        or not all(_int(value[k]) for k in ("issued_ms", "expires_ms", "stop"))
    )
    label = value["label"]
    bad = bad or not (
        isinstance(label, str)
        and 1 <= len(label) <= MAX_LABEL
        and not any(unicodedata.category(c) in ("Cc", "Cf") for c in label)
    )
    if bad:
        raise MeshError("caller.malformed", "field value")
    tools = value["tools"]
    if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS:
        raise MeshError("caller.malformed", "tools")
    names = [_check_tool(t) for t in tools]
    if names != sorted(set(names)):
        raise MeshError("caller.malformed", "tool order")
    lifetime = value["expires_ms"] - value["issued_ms"]
    if not 0 < lifetime <= MAX_LIFETIME_S * 1000:
        raise MeshError("caller.malformed", "lifetime")
    return value


def check_proof(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PROOF_FIELDS:
        raise MeshError("caller.proof_malformed", "fields")
    bad = (
        value["v"] != 1
        or isinstance(value["v"], bool)
        or not _raw(value["ticket"], 16)
        or not isinstance(value["method"], str)
        or not 1 <= len(value["method"]) <= 16
        or not isinstance(value["target"], str)
        or not value["target"].startswith("/")
        or len(value["target"]) > 2048
        or not crypto.is_id(value["body"])
        or not _int(value["ms"])
        or not _raw(value["nonce"], 16)
        or not _raw(value["sig"], 64)
    )
    if bad:
        raise MeshError("caller.proof_malformed", "field value")
    return value


def _decode(header: Any, prefix: str, code: str) -> Any:
    if (
        not isinstance(header, str)
        or len(header) > MAX_HEADER
        or not header.startswith(prefix)
    ):
        raise MeshError(code, "header")
    try:
        raw = b64url_decode(header[len(prefix) :])
        value = strict_loads(raw)
        if canonical(value) != raw:
            raise MeshError(code, "not canonical")
    except (MeshError, ValueError, RecursionError) as exc:
        raise MeshError(code, "undecodable") from exc
    return value


def sign_ticket(presence_scalar: int, body: dict[str, Any]) -> dict[str, Any]:
    message = TICKET_CONTEXT.encode() + b"\n" + canonical(body)
    return {**body, "sig": b64url_encode(crypto.p256_sign(presence_scalar, message))}


def sign_proof(holder_seed: bytes, body: dict[str, Any]) -> dict[str, Any]:
    message = PROOF_CONTEXT.encode() + b"\n" + canonical(body)
    return {**body, "sig": b64url_encode(crypto.ed25519_sign(holder_seed, message))}


def authorization_header(ticket_object: dict[str, Any]) -> str:
    return SCHEME + b64url_encode(canonical(ticket_object))


def proof_header(proof: dict[str, Any]) -> str:
    return b64url_encode(canonical(proof))


def body_digest(body: bytes) -> str:
    return crypto.sha256_id(body)


def _verify_with(public: bytes, context: str, obj: dict[str, Any], alg: str) -> bool:
    message = (
        context.encode()
        + b"\n"
        + canonical({k: v for k, v in obj.items() if k != "sig"})
    )
    raw = b64url_decode(obj["sig"])
    check = crypto.p256_verify if alg == "p256" else crypto.ed25519_verify
    return check(public, message, raw)


class Verifier:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.seen: dict[tuple[str, str], int] = {}

    def verify(
        self, authorization: Any, proof_value: Any, request: dict[str, Any]
    ) -> dict[str, Any]:
        state = self.state
        ticket_object = check_ticket(_decode(authorization, SCHEME, "caller.malformed"))
        proof = check_proof(_decode(proof_value, "", "caller.proof_malformed"))
        if ticket_object["mesh"] != state["mesh"]:
            raise MeshError("caller.mesh_mismatch")
        if ticket_object["node"] != state["node"]:
            raise MeshError("caller.wrong_node")
        device = ticket_object["device"]
        member = state["members"].get(device)
        if member is None or device in state["removed"]:
            raise MeshError("caller.device_removed")
        if member["kind"] not in MINTER_KINDS:
            raise MeshError("caller.wrong_minter")
        if not _verify_with(
            b64url_decode(member["presence"]), TICKET_CONTEXT, ticket_object, "p256"
        ):
            raise MeshError("caller.bad_signature")
        if state["stop"] > ticket_object["stop"]:
            raise MeshError("caller.stopped")
        now = state["now_ms"]
        if now >= ticket_object["expires_ms"]:
            raise MeshError("caller.expired")
        if ticket_object["issued_ms"] > now + FUTURE_SKEW_MS:
            raise MeshError("caller.not_yet_valid")
        if ticket_object["id"] in revoked_ids(state["revokes"]):
            raise MeshError("caller.revoked")
        wanted = (ticket_object["id"], request["method"], request["target"])
        if (proof["ticket"], proof["method"], proof["target"]) != wanted or proof[
            "body"
        ] != body_digest(request["body"].encode()):
            raise MeshError("caller.proof_mismatch")
        if (
            not now - PROOF_WINDOW_MS <= proof["ms"] <= now + PROOF_AHEAD_MS
            or proof["ms"] <= state["started_ms"] + RESTART_PAUSE_MS
        ):
            raise MeshError("caller.proof_stale")
        if not _verify_with(
            b64url_decode(ticket_object["holder"]), PROOF_CONTEXT, proof, "ed25519"
        ):
            raise MeshError("caller.proof_bad_signature")
        self.seen = {
            k: ms for k, ms in self.seen.items() if now <= ms + PROOF_WINDOW_MS
        }
        key = (ticket_object["id"], proof["nonce"])
        if key in self.seen:
            raise MeshError("caller.replayed")
        self.seen[key] = proof["ms"]
        out = {
            "caller": ticket_object["id"],
            "label": ticket_object["label"],
            "mesh": ticket_object["mesh"],
        }
        tool = request.get("tool")
        if tool is None:
            return {**out, "tools": [t["name"] for t in ticket_object["tools"]]}
        return {**out, "tool": self._covered(ticket_object, tool)}

    def _covered(self, ticket_object: dict[str, Any], name: str) -> str:
        covered = {t["name"]: t["def"] for t in ticket_object["tools"]}
        if name not in covered:
            raise MeshError("caller.tool_not_covered", name)
        if name != A2A and self.state["catalog"].get(name) != covered[name]:
            raise MeshError("caller.tool_changed", name)
        return name


def revoked_ids(revokes: list[str]) -> list[str]:
    out = []
    for key in revokes:
        parts = key.split("/", 2)
        if (
            len(parts) == 3
            and parts[0].startswith("caller:")
            and parts[1:] == ["*", "*"]
        ):
            out.append(parts[0].removeprefix("caller:"))
    return sorted(set(out))


def revoke_fold(i: dict[str, Any]) -> dict[str, Any]:
    ticket.grant_check(i["action"], i["effect_kind"], i["grants"], i["revokes"])
    return {"ordinary_grant": "ok", "revoked": revoked_ids(i["revokes"])}


def _result(verifier: Verifier, step: dict[str, Any]) -> dict[str, Any]:
    try:
        return {
            "ok": verifier.verify(
                step["authorization"], step.get("proof"), step["request"]
            )
        }
    except MeshError as exc:
        return {"error": exc.code}


def parse_holder_text(text: str) -> str:
    if not isinstance(text, str) or not text.startswith(HOLDER_TEXT):
        raise MeshError("caller.malformed", "holder form")
    body = text[len(HOLDER_TEXT) :]
    if not _raw(body, 32):
        raise MeshError("caller.malformed", "holder key")
    return body


def parse_ticket_text(text: str) -> dict[str, str]:
    value = check_ticket(_decode(text, TICKET_TEXT, "caller.malformed"))
    return {
        "id": value["id"],
        "authorization": SCHEME + text[len(TICKET_TEXT) :],
    }


def parse_key_text(text: str) -> dict[str, str]:
    if not isinstance(text, str) or not text.startswith(KEY_TEXT):
        raise MeshError("caller.malformed", "key form")
    body = text[len(KEY_TEXT) :]
    if not _raw(body, 32):
        raise MeshError("caller.malformed", "key seed")
    public = b64url_encode(crypto.ed25519_public(b64url_decode(body)))
    return {"holder": public, "holder_text": HOLDER_TEXT + public}


def _guard(call: Any) -> dict[str, Any]:
    try:
        return {"ok": call()}
    except MeshError as exc:
        return {"error": exc.code}


def run(kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
    if kind == "caller_verify":
        return _result(Verifier(case_input["state"]), case_input)
    if kind == "caller_verify_sequence":
        patched = dict(case_input["state"])
        verifier = Verifier(patched)
        results = []
        for step in case_input["steps"]:
            if "state" in step:
                patched = {**patched, **step["state"]}
                verifier.state = patched
            if "restart" in step:
                patched = {**patched, **step["restart"]}
                verifier = Verifier(patched)
            results.append(_result(verifier, step))
        return {"ok": {"results": results}}
    if kind == "caller_revoke_fold":
        return _guard(lambda: revoke_fold(case_input))
    if kind == "caller_holder_text":
        return _guard(lambda: {"holder": parse_holder_text(case_input["text"])})
    if kind == "caller_ticket_text":
        return _guard(lambda: parse_ticket_text(case_input["text"]))
    if kind == "caller_key_text":
        return _guard(lambda: parse_key_text(case_input["text"]))
    raise KeyError(kind)
