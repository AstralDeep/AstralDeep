"""The body of an `auth.transfer` event, which carries a LETS transfer voucher from the device that gives headroom and the acknowledgement from the device that accepts it.
LETS signs and verifies the objects, so this module only checks the wrapper and event.py lists the type.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import MeshError

TYPE = "auth.transfer"
OBJECT_TYPES = {
    "voucher": "lets.transfer-voucher/v1",
    "ack": "lets.transfer-ack/v1",
}


def check_body(body: Any) -> dict[str, str]:
    if not isinstance(body, dict) or set(body) != {"kind", "transfer"}:
        raise MeshError("transfer.malformed", "fields")
    kind = body["kind"]
    if kind not in OBJECT_TYPES:
        raise MeshError("transfer.malformed", "kind")
    transfer = body["transfer"]
    if (
        not isinstance(transfer, dict)
        or not isinstance(transfer.get("type"), str)
        or not isinstance(transfer.get("transfer_id"), str)
    ):
        raise MeshError("transfer.malformed", "transfer object")
    if transfer["type"] != OBJECT_TYPES[kind]:
        raise MeshError("transfer.type_mismatch", transfer["type"])
    return {"id": transfer["transfer_id"], "kind": kind}


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("transfer_check")
    def _check(i: dict[str, Any]) -> Any:
        return check_body(i["body"])
