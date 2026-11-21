"""Sealed credential items: a secret-labeled mesh event whose body holds one box per recipient device, the choice of recipients, and the fold that lets each device open only its own box.
Boxes use the wrap construction of crypto.py, sync.py sends these bodies to every member although the label is secret, and event.py lists the type.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import crypto
from .cj import canonical, strict_loads
from .errors import MeshError

TYPE = "key.credential"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
MAX_VALUE_BYTES = 65536
MAX_BOXES = 64
BODY_FIELDS = {"boxes", "name"}
AUDIENCES = ("all", "list")
BOX_FIELDS = {"epk", "nonce", "ct"}


def box_aad(name: str, device: str) -> dict[str, str]:
    return {"name": name, "purpose": "credential", "recipient": device}


def _valid_name(name: Any) -> bool:
    return isinstance(name, str) and NAME_PATTERN.fullmatch(name) is not None


def check_body(body: Any) -> None:
    if not isinstance(body, dict):
        raise MeshError("credential.malformed", "body is not an object")
    if "boxes" not in body:
        raise MeshError("credential.not_sealed", "no per-recipient boxes")
    if not (set(body) == BODY_FIELDS or set(body) == BODY_FIELDS | {"audience"}):
        raise MeshError("credential.malformed", "fields")
    if not _valid_name(body["name"]):
        raise MeshError("credential.malformed", "name")
    if "audience" in body and body["audience"] not in AUDIENCES:
        raise MeshError("credential.malformed", "audience")
    boxes = body["boxes"]
    if not isinstance(boxes, dict) or len(boxes) > MAX_BOXES:
        raise MeshError("credential.malformed", "boxes")
    for device, box in boxes.items():
        if not crypto.is_id(device):
            raise MeshError("credential.malformed", "recipient is not a device id")
        if not isinstance(box, dict) or set(box) != BOX_FIELDS:
            raise MeshError("credential.malformed", "box fields")
        if not all(isinstance(box[f], str) for f in BOX_FIELDS):
            raise MeshError("credential.malformed", "box values")


def check_item(kind: str, labels: dict[str, str], body: Any) -> dict[str, str]:
    if kind != TYPE:
        raise MeshError("credential.malformed", "not a credential item")
    if labels.get("confidentiality") != "secret":
        raise MeshError("credential.label", "a credential item is labeled secret")
    check_body(body)
    return {"name": body["name"]}


def choose_recipients(members: list[str], requested: list[str] | None) -> list[str]:
    current = sorted(set(members))
    if requested is None:
        return current
    chosen = sorted(set(requested))
    if not chosen or any(device not in current for device in chosen):
        raise MeshError("credential.bad_recipients", "not current devices")
    return chosen


def seal_body(
    name: str,
    value: dict[str, Any],
    recipients: list[dict[str, Any]],
    audience: str | None = None,
) -> dict[str, Any]:
    if audience is not None and audience not in AUDIENCES:
        raise MeshError("credential.malformed", "audience")
    if not _valid_name(name):
        raise MeshError("credential.malformed", "name")
    plaintext = canonical(value)
    if len(plaintext) > MAX_VALUE_BYTES or len(recipients) > MAX_BOXES:
        raise MeshError("credential.too_large", "value or recipients")
    boxes = {
        r["device"]: crypto.wrap_key(
            r["wrap_public"],
            plaintext,
            box_aad(name, r["device"]),
            r["ephemeral"],
            r["nonce"],
        )
        for r in recipients
    }
    body = {"boxes": boxes, "name": name}
    if audience is not None:
        body["audience"] = audience
    return body


def open_body(body: Any, device: str, wrap_private: bytes) -> dict[str, Any]:
    check_body(body)
    box = body["boxes"].get(device)
    if box is None:
        raise MeshError("credential.not_a_recipient", device)
    plaintext = crypto.unwrap_key(wrap_private, box, box_aad(body["name"], device))
    value = strict_loads(plaintext)
    if not isinstance(value, dict):
        raise MeshError("credential.malformed", "value is not an object")
    return value


def _order(event: dict[str, Any]) -> tuple[int, int, str]:
    return (event["clock"]["ms"], event["clock"]["n"], event["feed"])


def fold(
    events: list[dict[str, Any]], device: str, wrap_private: bytes
) -> dict[str, Any]:
    winners: dict[str, dict[str, Any]] = {}
    for item in events:
        try:
            check_body(item["body"])
        except MeshError:
            continue
        name = item["body"]["name"]
        if name not in winners or _order(item) > _order(winners[name]):
            winners[name] = item
    values: dict[str, Any] = {}
    hidden: list[str] = []
    for name in sorted(winners):
        try:
            values[name] = open_body(winners[name]["body"], device, wrap_private)
        except MeshError:
            hidden.append(name)
    return {"hidden": hidden, "values": values}


def reseal_names(
    events: list[dict[str, Any]],
    members: list[str],
    device: str,
    wrap_private: bytes,
) -> list[str]:
    winners: dict[str, dict[str, Any]] = {}
    for item in events:
        try:
            check_body(item["body"])
        except MeshError:
            continue
        name = item["body"]["name"]
        if name not in winners or _order(item) > _order(winners[name]):
            winners[name] = item
    names = []
    for name in sorted(winners):
        body = winners[name]["body"]
        if (
            body.get("audience") != "all"
            or not body["boxes"]
            or set(members) <= set(body["boxes"])
        ):
            continue
        try:
            open_body(body, device, wrap_private)
        except MeshError:
            continue
        names.append(name)
    return names


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("credential_check")
    def _check(i: dict[str, Any]) -> Any:
        return check_item(i["type"], i["labels"], i["body"])

    @op("credential_recipients")
    def _recipients(i: dict[str, Any]) -> Any:
        return {"recipients": choose_recipients(i["members"], i.get("requested"))}

    @op("credential_seal")
    def _seal(i: dict[str, Any]) -> Any:
        recipients = [
            {
                "device": r["device"],
                "wrap_public": bytes.fromhex(r["wrap_public_hex"]),
                "ephemeral": bytes.fromhex(r["ephemeral_hex"]),
                "nonce": bytes.fromhex(r["nonce_hex"]),
            }
            for r in i["recipients"]
        ]
        return {"body": seal_body(i["name"], i["value"], recipients, i.get("audience"))}

    @op("credential_open")
    def _open(i: dict[str, Any]) -> Any:
        return {
            "value": open_body(
                i["body"], i["device"], bytes.fromhex(i["wrap_private_hex"])
            )
        }

    @op("credential_fold")
    def _fold(i: dict[str, Any]) -> Any:
        return fold(i["events"], i["device"], bytes.fromhex(i["wrap_private_hex"]))

    @op("credential_reseal")
    def _reseal(i: dict[str, Any]) -> Any:
        return {
            "names": reseal_names(
                i["events"],
                i["members"],
                i["device"],
                bytes.fromhex(i["wrap_private_hex"]),
            )
        }
