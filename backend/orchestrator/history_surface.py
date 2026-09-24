"""Builds the server-driven chat-history list and its loading skeleton as astralprims
components for ROTE to adapt per device; enrichment (relative time, saved marker) is
derived from rows orchestrator.py already supplies.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from webrender.renderer import skeleton_component

MAX_HISTORY_ITEMS = 20


def history_skeleton_components(label: str = "Loading your chats…") -> List[Dict[str, Any]]:
    return [skeleton_component(variant="chat-history", count=6, label=label)]


def _chat_id(chat: Dict[str, Any]) -> Optional[str]:
    cid = chat.get("id") or chat.get("chat_id")
    return str(cid) if cid else None


def _relative_time(value: Any, *, now: Optional[float] = None) -> str:
    if value is None or value == "":
        return ""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts >= 1e11:
        ts /= 1000.0
    current = time.time() if now is None else now
    delta = current - ts
    if delta < 0:
        delta = 0
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 604800:
        return f"{int(delta // 86400)}d"
    if delta < 2629800:
        return f"{int(delta // 604800)}w"
    if delta < 31557600:
        return f"{int(delta // 2629800)}mo"
    return f"{int(delta // 31557600)}y"


def history_surface_components(chats: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for chat in list(chats or [])[:MAX_HISTORY_ITEMS]:
        if not isinstance(chat, dict):
            continue
        cid = _chat_id(chat)
        if not cid:
            continue
        title = str(chat.get("title") or "Untitled chat").strip() or "Untitled chat"
        items.append({
            "chat_id": cid,
            "title": title,
            "preview": str(chat.get("preview") or "").strip(),
            "time": _relative_time(chat.get("updated_at")),
            "saved": bool(chat.get("has_saved_components")),
        })
    return [{"type": "chat_history", "title": "Recent chats", "items": items}]
