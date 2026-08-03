"""Safe, deterministic projection from Astral primitives to MCP content."""
from __future__ import annotations

import json
from typing import Any

from ..registry import register_target


TARGET_NAME = "mcp"
_MAX_TEXT = 64 * 1024
_PRIVATE_KEYS = frozenset(
    {
        "action",
        "attributes",
        "component_id",
        "css",
        "events",
        "handler",
        "html",
        "onclick",
        "owner_user_id",
        "payload",
        "script",
        "token",
    }
)


def _bounded(value: Any) -> str:
    text = str("" if value is None else value)
    return text if len(text) <= _MAX_TEXT else f"{text[:_MAX_TEXT]}\n[truncated]"


def _readable(value: Any, *, depth: int = 0) -> str:
    if depth > 12:
        return "[nested content omitted]"
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if str(key).lower() in _PRIVATE_KEYS:
                continue
            if key in {"content", "children"} and isinstance(item, list):
                parts.extend(_readable(child, depth=depth + 1) for child in item)
            elif isinstance(item, (str, int, float, bool)) and item not in ("", None):
                parts.append(f"{key}: {item}")
            elif isinstance(item, list) and key in {"headers", "rows", "items"}:
                parts.append(f"{key}: {json.dumps(item, ensure_ascii=False, default=str)}")
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        return "\n".join(_readable(item, depth=depth + 1) for item in value)
    return _bounded(value)


def _render_one(component: dict[str, Any]) -> dict[str, Any]:
    kind = str(component.get("type") or "unknown").lower()
    if kind == "text":
        return {"type": "text", "text": _bounded(component.get("content", ""))}
    if kind == "alert":
        variant = str(component.get("variant") or "info")
        return {
            "type": "text",
            "text": _bounded(f"{variant}: {component.get('message', '')}"),
        }
    readable = _readable(component)
    if not readable:
        readable = f"[{kind} component has no portable text representation]"
    return {"type": "text", "text": _bounded(readable)}


def render_mcp(
    components: list[dict[str, Any]],
    profile: Any = None,
) -> list[dict[str, Any]]:
    del profile
    return [
        _render_one(component)
        for component in (components or [])
        if isinstance(component, dict)
    ]


def install() -> None:
    register_target(TARGET_NAME, render_mcp)


__all__ = ["TARGET_NAME", "install", "render_mcp"]
