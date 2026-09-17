"""Feature 089 (T052): the landing and sidebar data, for the web shell only.

The a8p console shows two things the server already knows and no client
should invent: the agent directory, and the example scenarios a new user can
start from. Both are built here and injected into the *shell*, which only the
web client ever fetches.

That placement is the point. A new websocket frame would have to be emitted
for one target and withheld from the others, and every native client would
have to be taught to ignore it. The shell is web-by-construction, so nothing
a native client receives changes at all — see
``backend/tests/test_web_landing_089.py``, which holds the registration frames
to that promise.

The two sources are the ones that already exist:

* scenarios come from ``orchestrator.welcome.WELCOME_EXAMPLES`` — the same
  list the server-driven welcome components are built from, so the landing
  and the welcome can never drift apart;
* the directory comes from the agents view model's own row builder
  (``projection_surfaces.agents._agent_rows``), so the sidebar lists exactly
  the agents the settings surface lists.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Which filter tab each welcome example belongs under. Keyed by the slug
# ``welcome._slug`` derives from the example's title, so a renamed example
# fails the mapping loudly in the test rather than silently losing its tab.
SCENARIO_CATEGORIES: Dict[str, str] = {
    "business_dashboard": "Dashboards",
    "weather_outlook": "Live data",
    "research_brief": "Research",
    "summarize_a_page": "Research",
    "roll_some_dice": "Utilities",
    "system_status": "Live data",
}

#: Tab order on the landing. "All" is prepended by the client.
CATEGORY_ORDER = ("Dashboards", "Research", "Live data", "Utilities")

#: The directory is a sidebar, not a search result page.
MAX_AGENTS = 60


def scenarios() -> List[Dict[str, str]]:
    """The welcome examples as landing cards: title, description, prompt, tab."""
    from orchestrator.welcome import WELCOME_EXAMPLES, _slug

    out: List[Dict[str, str]] = []
    for title, caption, query in WELCOME_EXAMPLES:
        slug = _slug(title)
        # The title carries a leading emoji for the button label; the card
        # shows the emoji as its own glyph instead of inside the text.
        glyph, _, plain = title.partition(" ")
        out.append({
            "id": slug,
            "glyph": glyph,
            "title": plain or title,
            "description": caption,
            "prompt": query,
            "category": SCENARIO_CATEGORIES.get(slug, "Utilities"),
        })
    return out


def categories(items: List[Dict[str, str]]) -> List[str]:
    """The tabs actually needed, in a fixed order, with strays appended."""
    present = {item["category"] for item in items}
    ordered = [name for name in CATEGORY_ORDER if name in present]
    ordered.extend(sorted(present - set(ordered)))
    return ordered


async def agents(orch, user_id: str) -> List[Dict[str, Any]]:
    """The agent directory for this user, from the agents view model's rows.

    Returns an empty list rather than raising: a sidebar that cannot be built
    should leave the directory empty and let the rest of the page work, not
    take the shell down with it.
    """
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
    """Everything the shell injects as ``window.__ASTRAL_LANDING__``."""
    items = scenarios()
    return {
        "scenarios": items,
        "categories": categories(items),
        "agents": await agents(orch, user_id) if user_id else [],
    }


def as_script_json(data: Dict[str, Any]) -> str:
    """JSON safe to embed in a ``<script>`` element.

    ``</script>`` inside a string would end the block early, and ``<!--``
    would open an HTML comment; both are escaped at the character level so no
    agent-supplied name or description can break out of the script.
    """
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
