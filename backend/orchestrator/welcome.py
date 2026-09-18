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

from astralprims import Button, Card, Collapsible, Grids, Hero, Text
from shared.feature_flags import flags


def _slug(title: str) -> str:
    """Deterministic ascii slug from an example title.

    Anything that is not an ascii letter or digit is a separator, so a title
    keeps a stable identity through punctuation and through the stray
    non-ascii character a future title might carry.
    """
    return "_".join(re.findall(r"[a-z0-9]+", title.lower())) or "example"


def _stamp(comp: Dict[str, Any], ident: str) -> Dict[str, Any]:
    """Stamp a wel_ identity on a rendered welcome dict (id + component_id)."""
    comp["id"] = ident
    comp["component_id"] = ident
    return comp

#: (title, caption, query) — the complete curated example catalog, and the only
#: definition of it: the welcome canvas every client renders and the web
#: console's "Start here" cards are both built from this list, so they cannot
#: drift apart.
#:
#: Each one is chosen to answer "what is this thing for?" in a single click and
#: to land on a different part of the catalog — the adaptive designer, live
#: host data, a live external API, the research and summarizing agents, a
#: domain specialist, and the smallest possible end-to-end turn.
#:
#: Titles are plain text. Nothing here carries an emoji, by project rule, and
#: the renderers no longer split a glyph off the front of a title.
WELCOME_EXAMPLES = [
    ("Build a business dashboard",
     "A hero, live metrics, two charts and today's schedule, arranged by the "
     "adaptive designer rather than a fixed template.",
     "Build a rich dashboard for a dog grooming business — booking requests, "
     "monthly revenue line chart, most popular services pie chart, and today's "
     "schedule as a table"),
    ("Brief me, with citations",
     "Web research read and distilled into a multi-part brief that cites only "
     "the pages it actually opened.",
     "Research the latest developments in small modular reactors and give me a cited brief"),
    ("Read a page for me",
     "The TL;DR, the key points and the quotable lines from any URL.",
     "Summarize https://en.wikipedia.org/wiki/Dog_grooming — give me a TL;DR and key points"),
    ("Weather for the week ahead",
     "Seven days of forecast for any city, drawn rather than described.",
     "What's the weather forecast for Lexington, KY this week? Show it with charts"),
    ("Check on this machine",
     "CPU, memory and disk read live from the host and laid out as KPI tiles "
     "and gauges.",
     "Show current system status with CPU and memory metrics"),
    ("Choose a journal for a paper",
     "A domain specialist at work: match a paper to venues, then compare the "
     "shortlist on impact, fit and review time.",
     "Find journals that fit a paper on self-supervised learning for chest CT, "
     "then compare the top three on impact and review time"),
    ("Roll some dice",
     "Six six-sided rolls, normalized — the shortest honest path from a "
     "sentence to a rendered, audited result.",
     "Roll exactly six six-sided dice and show the normalized results."),
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
    return Card(title="Agents are off for this account", content=[
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
    ], attributes={"data-welcome": "permission"}).to_dict()


def welcome_components(tools_available: bool = True) -> List[Dict[str, Any]]:
    """The welcome canvas as plain component dicts (pre-ROTE).

    Welcome identities are ephemeral, never workspace-persisted. Additional
    examples use the shared disclosure primitive rather than filling the canvas.

    Args:
        tools_available: per-user flag from
            ``Orchestrator.compute_tools_available_for_user``. When False the
            enable-agents consent card is prepended so the examples below are
            honest promises instead of guaranteed failures (feature 030).
    """
    examples = {}
    for title, _caption, query in WELCOME_EXAMPLES:
        # The title IS the label. It used to be the title with a leading emoji
        # chopped off, which made the label silently depend on the glyph still
        # being there.
        label = title
        examples[_slug(title)] = Button(
            label=label, action="chat_message", payload={"message": query},
            variant="secondary",
            attributes={"aria-label": label, "data-welcome": "example"},
        )
    primary = ("brief_me_with_citations", "read_a_page_for_me",
               "build_a_business_dashboard")
    tree = [
        Hero(
            title="How can I help?", variant="subtle",
            attributes={"data-welcome": "intro"},
        ),
        Grids(
            columns=3, gap=12, children=[examples[key] for key in primary],
            attributes={"data-welcome": "examples"},
        ),
        Collapsible(
            title="More examples",
            content=[button for key, button in examples.items() if key not in primary],
            default_open=False, attributes={"data-welcome": "more"},
        ),
    ]
    rendered = [c.to_dict() for c in tree]
    if not tools_available:
        rendered.insert(1, enable_agents_card())
    if flags.is_enabled("first_turn_contract"):
        _stamp_welcome_tree(rendered)
    return rendered


def _stamp_welcome_tree(rendered: List[Dict[str, Any]]) -> None:
    """Assign wel_ identities in place: top-level components own the purge
    contract; example buttons retain their stable per-example ids across both
    the visible grid and the disclosure. The consent card stays independently
    addressable and is never mistaken for an ordinary example."""
    for comp in rendered:
        ctype = comp.get("type")
        if ctype == "hero":
            _stamp(comp, "wel_hero")
        elif ctype == "grid":
            _stamp(comp, "wel_examples")
        elif ctype == "collapsible":
            _stamp(comp, "wel_more")
        elif ctype == "card":  # the enable-agents consent card
            _stamp(comp, "wel_enable")
        for child in comp.get("children", []) + comp.get("content", []):
            if child.get("action") == "chat_message":
                _stamp(child, f"wel_ex_{_slug(child['label'])}")
