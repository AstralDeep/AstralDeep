"""Tool catalogs: the body of `tools.catalog`, the definition hash, and the fold that gives each current member its latest valid catalog.
The kernel writes and reads these events, the vectors in tools.json pin them, and fold.py supplies the order key the fold uses.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import crypto
from .cj import canonical
from .errors import MeshError
from .fold import order_key

CATALOG = "tools.catalog"
EFFECTS = ("mesh_read", "mesh_write", "outward_read", "outside_effect")
TOOL_FIELDS = {"annotations", "description", "effect", "hash", "input_schema", "name"}
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
MAX_TOOLS = 64
MAX_DESCRIPTION = 4096


def _malformed(detail: str) -> MeshError:
    return MeshError("tools.malformed", detail)


def definition_hash(
    name: str, description: str, input_schema: Any, annotations: Any
) -> str:
    return crypto.sha256_id(
        canonical(
            {
                "annotations": annotations,
                "description": description,
                "input_schema": input_schema,
                "name": name,
            }
        )
    )


def check_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != {"tools"}:
        raise _malformed("fields")
    tools = body["tools"]
    if not isinstance(tools, list) or len(tools) > MAX_TOOLS:
        raise _malformed("tools")
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != TOOL_FIELDS:
            raise _malformed("tool fields")
        name = tool["name"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            raise _malformed("name")
        names.add(name)
        description = tool["description"]
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
            raise _malformed("description")
        if not isinstance(tool["input_schema"], dict) or not isinstance(
            tool["annotations"], dict
        ):
            raise _malformed("schema")
        if tool["effect"] not in EFFECTS:
            raise _malformed("effect")
        try:
            wanted = definition_hash(
                name, description, tool["input_schema"], tool["annotations"]
            )
        except MeshError as exc:
            raise _malformed("not canonical") from exc
        if tool["hash"] != wanted:
            raise MeshError("tools.hash_mismatch", name)
    return {"count": len(tools), "names": sorted(names)}


def _valid(event: dict[str, Any]) -> bool:
    try:
        check_body(event["body"])
    except MeshError:
        return False
    return True


def fold(
    events: list[dict[str, Any]], device_of_feed: Callable[[str], str | None]
) -> dict[str, Any]:
    ignored: set[str] = set()
    latest: dict[str, dict[str, Any]] = {}
    for e in sorted(events, key=order_key):
        if e["type"] != CATALOG:
            continue
        device = device_of_feed(e["feed"])
        if device is None or not _valid(e):
            ignored.add(e["id"])
            continue
        if device in latest:
            ignored.add(latest[device]["id"])
        latest[device] = e
    catalogs = [
        {"device": d, "event": e["id"], "tools": e["body"]["tools"]}
        for d, e in sorted(latest.items())
    ]
    return {"catalogs": catalogs, "ignored": sorted(ignored)}


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("tools_check")
    def _check(i: dict[str, Any]) -> Any:
        return check_body(i["body"])

    @op("tools_hash")
    def _hash(i: dict[str, Any]) -> Any:
        return {"hash": definition_hash(i["name"], i["description"], i["input_schema"], i["annotations"])}

    @op("tools_fold")
    def _fold(i: dict[str, Any]) -> Any:
        return fold(i["events"], i["devices"].get)
