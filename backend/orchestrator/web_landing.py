"""Builds the web shell's landing payload - welcome.py's example scenarios plus the
agent directory - injected only into the web client, never sent over the websocket to
native clients. Used by orchestrator.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

SCENARIO_CATEGORIES: Dict[str, str] = {
    "build_a_business_dashboard": "Dashboards",
    "brief_me_with_citations": "Research",
    "read_a_page_for_me": "Research",
    "choose_a_journal_for_a_paper": "Research",
    "weather_for_the_week_ahead": "Live data",
    "check_on_this_machine": "Live data",
    "roll_some_dice": "Utilities",
}

CATEGORY_ORDER = ("Dashboards", "Research", "Live data", "Utilities")

MAX_AGENTS = 60


def scenarios() -> List[Dict[str, str]]:
    from orchestrator.welcome import WELCOME_EXAMPLES, _slug

    out: List[Dict[str, str]] = []
    for title, caption, query in WELCOME_EXAMPLES:
        slug = _slug(title)
        out.append({
            "id": slug,
            "title": title,
            "description": caption,
            "prompt": query,
            "category": SCENARIO_CATEGORIES.get(slug, "Utilities"),
        })
    return out


def categories(items: List[Dict[str, str]]) -> List[str]:
    present = {item["category"] for item in items}
    ordered = [name for name in CATEGORY_ORDER if name in present]
    ordered.extend(sorted(present - set(ordered)))
    return ordered


async def agents(orch, user_id: str) -> List[Dict[str, Any]]:
    try:
        from orchestrator.projection_surfaces.agents import _agent_rows, _list_context

        email, ownership, disabled = await _list_context(orch, user_id)
        rows = await asyncio.to_thread(_agent_rows, orch, ownership, disabled)
    except Exception:
        logger.debug("web_landing: agent directory unavailable", exc_info=True)
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        owned = bool(email) and row.get("owner_email") == email
        if not owned and not row.get("is_public"):
            continue
        out.append({
            "id": row["id"],
            "name": row["name"],
            "description": row.get("description") or "",
            "state": "offline" if row.get("disabled") else "ready",
            "owned": owned,
        })
    out.sort(key=lambda a: (a["state"] != "ready", a["name"].lower()))
    return out[:MAX_AGENTS]


async def payload(orch, user_id: str) -> Dict[str, Any]:
    items = scenarios()
    return {
        "scenarios": items,
        "categories": categories(items),
        "agents": await agents(orch, user_id) if user_id else [],
    }


def as_script_json(data: Dict[str, Any]) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
