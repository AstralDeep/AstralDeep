"""Initial-load welcome canvas — example queries as ordinary SDUI.

Server-driven per Constitution II: astralprims defines → orchestrator renders
→ ROTE adapts. Every client target receives the same component tree over the
normal ``ui_render`` path (no shell HTML, no client-specific code). The
example buttons dispatch the standard ``chat_message`` ui_event action, so
they work on any client that can press a button and degrade to readable text
on voice profiles.

Feature 055 (US1, FF_FIRST_TURN_CONTRACT): every welcome component carries a
stable identity in the ephemeral ``wel_`` namespace — BOTH ``id`` and
``component_id`` set to the same value, because the web identity wrapper keys
on ``component_id`` while native canvases read ``component_id ?? id``. Clients
purge ``wel_``-identified components from their canvas state at turn start
(replacing the server-side blanking frame that killed loading skeletons).
Welcome components are still never workspace-persisted: the workspace layer
refuses ``wel_`` identities outright.
"""
import re
from typing import Any, Dict, List

from astralprims import Button, Card, Grids, Hero, Text
from shared.feature_flags import flags


def _slug(title: str) -> str:
    """Deterministic ascii slug from an example title (emoji dropped)."""
    return "_".join(re.findall(r"[a-z0-9]+", title.lower())) or "example"


def _stamp(comp: Dict[str, Any], ident: str) -> Dict[str, Any]:
    """Stamp a wel_ identity on a rendered welcome dict (id + component_id)."""
    comp["id"] = ident
    comp["component_id"] = ident
    return comp

#: (title, caption, query) — one tile per example. Queries are aimed at the
#: post-029 agent catalog: connectors dashboards, weather, web_research,
#: summarizer, dice_roller, general system metrics.
WELCOME_EXAMPLES = [
    ("📊 Business dashboard",
     "Hero, metrics, charts and a schedule — arranged by the adaptive designer.",
     "Build a rich dashboard for a dog grooming business — booking requests, "
     "monthly revenue line chart, most popular services pie chart, and today's "
     "schedule as a table"),
    ("⛅ Weather outlook",
     "A week of forecasts charted, not just described.",
     "What's the weather forecast for Lexington, KY this week? Show it with charts"),
    ("🔎 Research brief",
     "Web research distilled into a multi-part brief with citations.",
     "Research the latest developments in small modular reactors and give me a cited brief"),
    ("📄 Summarize a page",
     "TL;DR, key points and quotable lines from any URL.",
     "Summarize https://en.wikipedia.org/wiki/Dog_grooming — give me a TL;DR and key points"),
    ("🎲 Roll some dice",
     "Six six-sided rolls with normalized inputs and results.",
     "Roll exactly six six-sided dice and show the normalized results."),
    ("🖥️ System status",
     "Live host metrics as KPI tiles and gauges.",
     "Show current system status with CPU and memory metrics"),
]


def enable_agents_card() -> Dict[str, Any]:
    """Consent affordance shown when the account has no enabled agent tools.

    Feature 030 (walkthrough finding): a fresh user started fail-closed — every
    agent scope disabled — so all welcome examples silently degraded to
    text-only chat. This card makes that state visible and actionable. The
    "Enable" button is the explicit user grant (Constitution VII: the system
    sets attenuated scopes; the user may adjust per agent afterwards) and is
    handled server-side by the audited ``enable_recommended_agents`` action,
    which never grants ``tools:write``.

    **Feature 040 changed who sees this, and that is intended.** The nine
    bundled built-ins are seeded safe + public, and the safe baseline flips
    deny→allow for a user with no explicit scope row — so a *fresh* account now
    has tools available and correctly never sees this card. What remains
    reachable is the population the copy is still honest for: a user who
    explicitly opted out of everything (explicit ``enabled=False`` rows outrank
    the safe flip), a deployment running with ``FF_SAFE_AGENTS`` off, and an
    account whose only agents are safe-but-private. The gate is
    ``compute_tools_available_for_user``, which reads ``is_tool_allowed``, so
    the card appears exactly when replies really would be text-only — it cannot
    render a false promise. Do not "restore" it for fresh users: that would be
    telling them agents are off while their agents work.
    """
    return Card(title="🔌 Agents are off for this account", content=[
        Text(content=("Replies will be plain text until agents are enabled. "
                      "Enabling grants read-only permissions for the built-in "
                      "public agents — search, data, file and system reads, "
                      "never write access — and each agent can be adjusted or "
                      "turned off any time."),
             variant="caption"),
        Button(label="Enable recommended agents",
               action="enable_recommended_agents",
               payload={"source": "welcome"}),
        Button(label="Choose agents individually", action="chrome_open",
               payload={"surface": "agents"}, variant="secondary"),
    ]).to_dict()


def welcome_components(tools_available: bool = True) -> List[Dict[str, Any]]:
    """The welcome canvas as plain component dicts (pre-ROTE).

    Not workspace components — no identities, never persisted; the canvas
    they occupy is replaced by the first real render/upsert of the session.

    Args:
        tools_available: per-user flag from
            ``Orchestrator.compute_tools_available_for_user``. When False the
            enable-agents consent card is prepended so the examples below are
            honest promises instead of guaranteed failures (feature 030).
    """
    cards = [
        Card(title=title, content=[
            Text(content=caption, variant="caption"),
            # aria-label disambiguates the six otherwise-identical "Run
            # example" accessible names (030 a11y finding); rendered via the
            # webrender attribute whitelist.
            Button(label="Run example", action="chat_message",
                   payload={"message": query}, variant="secondary",
                   attributes={"aria-label": f"Run example: {title}"}),
        ])
        for title, caption, query in WELCOME_EXAMPLES
    ]
    tree = [
        Hero(
            title="What would you like to build?",
            eyebrow="Welcome",
            subtitle=("Ask in plain language — agents answer with live, interactive "
                      "components: dashboards, charts, tables, timelines and cited briefs."),
            variant="gradient",
        ),
        Grids(columns=2, children=cards),
        Text(content="Run an example, or type your own request.", variant="caption"),
    ]
    rendered = [c.to_dict() for c in tree]
    if not tools_available:
        rendered.insert(1, enable_agents_card())
    if flags.is_enabled("first_turn_contract"):
        _stamp_welcome_tree(rendered)
    return rendered


def _stamp_welcome_tree(rendered: List[Dict[str, Any]]) -> None:
    """Assign wel_ identities in place: top-level components own the purge
    contract; example cards inside the grid get per-example ids too so any
    future targeted ops (and tests) can address them individually."""
    for comp in rendered:
        ctype = comp.get("type")
        if ctype == "hero":
            _stamp(comp, "wel_hero")
        elif ctype == "grid":
            _stamp(comp, "wel_examples")
            for child in comp.get("children") or []:
                if not isinstance(child, dict):
                    continue
                title = child.get("title") or ""
                if title:
                    _stamp(child, f"wel_ex_{_slug(title)}")
        elif ctype == "text":
            _stamp(comp, "wel_hint")
        elif ctype == "card":  # the enable-agents consent card
            _stamp(comp, "wel_enable")
