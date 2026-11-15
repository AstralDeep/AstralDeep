"""Requests addressed to one member: the four event bodies, the optional tool call with its ticket, and the fold that gives each request a state every peer computes identically.
The kernel writes and reads these events, the vectors in requests.json pin them, and fold.py supplies the order key and ancestry the fold uses.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import crypto, ticket
from .cj import canonical, strict_loads
from .errors import MeshError
from .fold import ancestors, order_key
from .labels import EFFECT_KINDS, join

SEND = "request.send"
CLAIM = "request.claim"
REFUSE = "request.refuse"
CANCEL = "request.cancel"
TYPES = (SEND, CLAIM, REFUSE, CANCEL)
TURN_START = "chat.turn_start"
MAX_TEXT_BYTES = 65536
MAX_ARGUMENT_BYTES = 65536
SEND_FIELDS = {"call", "continues", "text", "to"}
CALL_FIELDS = {"action", "arguments", "call_id", "ticket", "tool"}
TICKET_FIELDS = {"capability", "consent", "receipt", "v"}
CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
CODE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+")
MAX_CODE = 80


def _malformed(detail: str) -> MeshError:
    return MeshError("request.malformed", detail)


def _check_ticket(value: Any) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != TICKET_FIELDS
        or value["v"] != 1
        or isinstance(value["v"], bool)
        or not isinstance(value["capability"], str)
        or not value["capability"]
        or not (value["receipt"] is None or isinstance(value["receipt"], dict))
        or not (value["consent"] is None or isinstance(value["consent"], dict))
    ):
        raise _malformed("ticket")


def _check_call(call: Any) -> None:
    if not isinstance(call, dict) or set(call) != CALL_FIELDS:
        raise _malformed("call fields")
    if not isinstance(call["call_id"], str) or not CALL_ID.fullmatch(call["call_id"]):
        raise _malformed("call id")
    tool = call["tool"]
    if (
        not isinstance(tool, str)
        or not 1 <= len(tool) <= 200
        or any(ord(c) < 32 or ord(c) == 127 for c in tool)
    ):
        raise _malformed("tool")
    arguments = call["arguments"]
    if not isinstance(arguments, str):
        raise _malformed("arguments")
    raw = arguments.encode("utf-8")
    if len(raw) > MAX_ARGUMENT_BYTES:
        raise _malformed("arguments size")
    try:
        parsed = strict_loads(arguments)
    except MeshError as exc:
        raise _malformed("arguments json") from exc
    if not isinstance(parsed, dict) or canonical(parsed) != raw:
        raise _malformed("arguments are not canonical")
    _check_ticket(call["ticket"])
    try:
        ticket.action_digest(call["action"])
    except (MeshError, KeyError, TypeError) as exc:
        raise _malformed("action") from exc
    if call["action"]["tool"] != tool or call["action"][
        "args_digest"
    ] != crypto.sha256_id(raw):
        raise MeshError("request.call_mismatch", tool)


def _check_ref(body: Any, fields: set[str]) -> str:
    if not isinstance(body, dict) or set(body) != fields:
        raise _malformed("fields")
    if not crypto.is_id(body["request"]):
        raise _malformed("request id")
    return body["request"]


def check_body(kind: str, body: Any) -> dict[str, Any]:
    if kind == SEND:
        if not isinstance(body, dict) or set(body) != SEND_FIELDS:
            raise _malformed("fields")
        if not crypto.is_id(body["to"]):
            raise _malformed("target")
        text = body["text"]
        if (
            not isinstance(text, str)
            or not text
            or len(text.encode("utf-8")) > MAX_TEXT_BYTES
        ):
            raise _malformed("text")
        if body["continues"] is not None and not crypto.is_id(body["continues"]):
            raise _malformed("continues")
        if body["call"] is not None:
            _check_call(body["call"])
        return {"call": body["call"] is not None, "to": body["to"]}
    if kind == CLAIM:
        return {"request": _check_ref(body, {"request"})}
    if kind == CANCEL:
        return {"request": _check_ref(body, {"request"})}
    if kind == REFUSE:
        request = _check_ref(body, {"code", "request"})
        code = body["code"]
        if (
            not isinstance(code, str)
            or len(code) > MAX_CODE
            or not CODE.fullmatch(code)
        ):
            raise _malformed("code")
        return {"code": code, "request": request}
    raise MeshError("event.unknown_type", kind)


def _valid(event: dict[str, Any]) -> bool:
    try:
        check_body(event["type"], event["body"])
    except MeshError:
        return False
    return True


def fold(
    events: list[dict[str, Any]], device_of_feed: Callable[[str], str | None]
) -> dict[str, Any]:
    ordered = sorted(events, key=order_key)
    ignored: set[str] = set()
    sends: dict[str, dict[str, Any]] = {}
    for e in ordered:
        if e["type"] not in TYPES:
            continue
        if not _valid(e):
            ignored.add(e["id"])
        elif e["type"] == SEND:
            if device_of_feed(e["feed"]) is None:
                ignored.add(e["id"])
            else:
                sends[e["id"]] = e
    below = ancestors(ordered)
    claims: dict[str, list[dict[str, Any]]] = {r: [] for r in sends}
    refusals: dict[str, list[dict[str, Any]]] = {r: [] for r in sends}
    cancels: dict[str, list[dict[str, Any]]] = {r: [] for r in sends}
    for e in ordered:
        if e["type"] not in (CLAIM, REFUSE, CANCEL) or e["id"] in ignored:
            continue
        request = sends.get(e["body"]["request"])
        author = device_of_feed(e["feed"])
        if request is None or author is None:
            ignored.add(e["id"])
            continue
        owner = (
            request["body"]["to"]
            if e["type"] != CANCEL
            else device_of_feed(request["feed"])
        )
        if author != owner:
            ignored.add(e["id"])
            continue
        {CLAIM: claims, REFUSE: refusals, CANCEL: cancels}[e["type"]][
            request["id"]
        ].append(e)
    views = []
    for rid, request in sends.items():
        real = []
        for claim in claims[rid]:
            if any(c["id"] in below[claim["id"]] for c in cancels[rid]):
                ignored.add(claim["id"])
            else:
                real.append(claim)
        target = request["body"]["to"]
        answers = [
            e
            for e in ordered
            if e["type"] == TURN_START
            and isinstance(e["body"], dict)
            and e["body"].get("request") == rid
            and device_of_feed(e["feed"]) == target
        ]
        turn = answers[0]["id"] if answers and real else None
        code = refusals[rid][0]["body"]["code"] if refusals[rid] else None
        if turn is not None:
            state = "answered"
        elif code is not None:
            state = "refused"
        elif real:
            state = "claimed"
        elif cancels[rid]:
            state = "cancelled"
        else:
            state = "pending"
        views.append(
            {
                "author": device_of_feed(request["feed"]),
                "claim": real[0]["id"] if real else None,
                "code": code if state == "refused" else None,
                "id": rid,
                "state": state,
                "to": target,
                "turn": turn,
            }
        )
    return {"ignored": sorted(ignored), "requests": views}


MAX_MINTS = 64
MINT_STATES = ("pending", "claimed", "answered", "refused", "cancelled")


def grant_name(name: str, tool_def: str) -> str:
    digits = tool_def.removeprefix("sha256:")
    return f"{name}@{digits[:8]}"


def _mint_call(call: dict[str, Any]) -> str:
    tool = call["tool"]
    if (
        not isinstance(call["call_id"], str)
        or not CALL_ID.fullmatch(call["call_id"])
        or not isinstance(tool, str)
        or not 1 <= len(tool) <= 200
        or any(ord(c) < 32 or ord(c) == 127 for c in tool)
        or not crypto.is_id(call["tool_def"])
    ):
        raise MeshError("mint.malformed", "call")
    if call["effect"] not in EFFECT_KINDS:
        raise MeshError("labels.invalid", str(call["effect"]))
    arguments = call["arguments"]
    raw = arguments.encode("utf-8") if isinstance(arguments, str) else b""
    try:
        parsed = strict_loads(arguments)
    except (MeshError, TypeError) as exc:
        raise MeshError("mint.malformed", "arguments") from exc
    if (
        len(raw) > MAX_ARGUMENT_BYTES
        or not isinstance(parsed, dict)
        or canonical(parsed) != raw
    ):
        raise MeshError("mint.malformed", "arguments")
    return crypto.sha256_id(raw)


def mint_check(i: dict[str, Any]) -> dict[str, Any]:
    args_digest = _mint_call(i["call"])
    request = i["request"]
    if request["to"] != i["local"]:
        raise MeshError("request.not_target", request["to"])
    if request["state"] == "pending":
        raise MeshError("request.not_claimed", request["state"])
    if request["state"] != "claimed":
        raise MeshError(f"request.{request['state']}", request["state"])
    if request["has_call"]:
        raise MeshError("mint.call_request", "the request carries a call")
    if i["minted"] >= MAX_MINTS:
        raise MeshError("mint.limit", str(i["minted"]))
    call = i["call"]
    own = next((t for t in i["catalog"] if t["name"] == call["tool"]), None)
    if own is None:
        raise MeshError("mint.unknown_tool", call["tool"])
    if own["hash"] != call["tool_def"]:
        raise MeshError("gate.tool_def_changed", call["tool"])
    if own["effect"] != call["effect"]:
        raise MeshError("mint.effect_mismatch", call["tool"])
    running = i["running"] if i["running"] is not None else request["labels"]
    label = join([request["labels"], running])
    if label["confidentiality"] == "secret":
        raise MeshError("mint.secret_label", "secret")
    if label["confidentiality"] == "phi" and not i["phi_member"]:
        raise MeshError("mint.phi_not_approved", "phi")
    return {
        "args_digest": args_digest,
        "grant": grant_name(call["tool"], call["tool_def"]),
        "label": label,
    }


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("request_check")
    def _check(i: dict[str, Any]) -> Any:
        return check_body(i["type"], i["body"])

    @op("request_mint")
    def _mint(i: dict[str, Any]) -> Any:
        return mint_check(i)

    @op("request_fold")
    def _fold(i: dict[str, Any]) -> Any:
        return fold(i["events"], i["devices"].get)
