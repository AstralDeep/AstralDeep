"""Server-driven welcome canvas of example queries as ordinary astralprims components,
dispatching the standard chat_message action. Read by web_landing.py for the shell's
cards and rendered by orchestrator.py for every client.
"""

import re
from typing import Any, Dict, List

from astralprims import Button, Card, Collapsible, Grids, Hero, Text
from shared.feature_flags import flags


def _slug(title: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", title.lower())) or "example"


def _stamp(comp: Dict[str, Any], ident: str) -> Dict[str, Any]:
    comp["id"] = ident
    comp["component_id"] = ident
    return comp

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
    examples = {}
    for title, _caption, query in WELCOME_EXAMPLES:
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
    for comp in rendered:
        ctype = comp.get("type")
        if ctype == "hero":
            _stamp(comp, "wel_hero")
        elif ctype == "grid":
            _stamp(comp, "wel_examples")
        elif ctype == "collapsible":
            _stamp(comp, "wel_more")
        elif ctype == "card":
            _stamp(comp, "wel_enable")
        for child in comp.get("children", []) + comp.get("content", []):
            if child.get("action") == "chat_message":
                _stamp(child, f"wel_ex_{_slug(child['label'])}")
