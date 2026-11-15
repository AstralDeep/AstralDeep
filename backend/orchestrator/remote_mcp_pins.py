"""Hashes each approved remote MCP tool definition and reconciles a freshly listed catalog against the pins.
remote_mcp_connectors.py stores the results on the connector and audits each change this module reports.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_ANNOTATION_BOOLEANS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
_HASH_PREFIX = "sha256:"
CHANGED = "changed"
REMOVED = "removed"


def sanitize_annotations(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        return {}
    clean: dict[str, Any] = {key: value[key] for key in _ANNOTATION_BOOLEANS if type(value.get(key)) is bool}
    title = value.get("title")
    if type(title) is str and title.strip():
        clean["title"] = " ".join(title.split())[:120]
    return clean


def definition_hash(tool: Any) -> str:
    if type(tool) is not dict or type(tool.get("name")) is not str:
        return ""
    canonical = json.dumps(
        {"name": tool["name"], "description": tool.get("description", ""),
         "inputSchema": tool.get("inputSchema", {}), "annotations": tool.get("annotations", {})},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return _HASH_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_pin(value: Any) -> bool:
    return (type(value) is str and value.startswith(_HASH_PREFIX) and len(value) == len(_HASH_PREFIX) + 64
            and all(c in "0123456789abcdef" for c in value[len(_HASH_PREFIX):]))


def verdict(pins: dict[str, Any], tool: Any) -> str:
    pin = pins.get(tool.get("name")) if type(tool) is dict else None
    if not is_pin(pin):
        return "unapproved"
    return "ok" if definition_hash(tool) == pin else CHANGED


def reconcile(pins: dict[str, Any], tools: dict[str, bool], changes: dict[str, Any],
              listed: list[dict[str, Any]]) -> tuple[dict[str, bool], dict[str, Any], list[dict[str, str]]]:
    listed_by_name = {tool["name"]: tool for tool in listed}
    new_tools = {name: False for name in listed_by_name}
    new_changes: dict[str, Any] = {}
    events: list[dict[str, str]] = []
    for name, tool in listed_by_name.items():
        pin = pins.get(name)
        if not is_pin(pin):
            continue
        current = definition_hash(tool)
        if current == pin:
            new_tools[name] = tools.get(name) is True
            continue
        record = {"status": CHANGED, "pinned": pin, "current": current}
        new_changes[name] = record
        if changes.get(name) != record:
            events.append({"tool": name, **record})
    for name, pin in pins.items():
        if name in listed_by_name or not is_pin(pin):
            continue
        record = {"status": REMOVED, "pinned": pin, "current": ""}
        new_changes[name] = record
        if changes.get(name) != record:
            events.append({"tool": name, **record})
    return new_tools, new_changes, events
