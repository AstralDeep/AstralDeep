"""Tests for onboarding/seed.py's canonical tutorial seed: the user and admin flow steps
exist, are strictly ordered, every static target resolves to a real chrome anchor,
and legacy steps are archived or absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from onboarding.seed import seed_tutorial_steps

BACKEND_DIR = Path(__file__).resolve().parents[2]

USER_FLOW = {
    "welcome-tour": (10, "none", None),
    "meet-the-canvas": (20, "static", "canvas.workspace"),
    "turn-on-agents": (30, "static", "canvas.workspace"),
    "ask-in-plain-language": (40, "static", "chat.input"),
    "open-settings-menu": (50, "static", "topbar.settings"),
    "agents-and-permissions": (60, "static", "sidebar.agents"),
    "personalize-your-assistant": (70, "static", "sidebar.personalization"),
    "review-your-audit-log": (80, "static", "sidebar.audit"),
    "workspace-timeline": (90, "static", "topbar.timeline"),
    "help-anytime": (100, "static", "sidebar.guide"),
    "tour-complete": (110, "none", None),
}

ADMIN_FLOW = {
    "admin-tool-quality": (200, "static", "sidebar.tool-quality"),
    "admin-knowledge-proposals": (210, "static", "sidebar.tool-quality"),
    "admin-edit-this-tour": (220, "static", "sidebar.tutorial-admin"),
}

LEGACY_TUTORIAL_SLUGS = (
    "welcome",
    "chat-with-agent",
    "personalize-profession",
    "personalize-skills",
    "personalize-personality",
    "open-agents-panel",
    "enable-agents",
    "open-audit-log",
    "give-feedback",
    "finish",
    "admin-feedback-flagged",
    "admin-feedback-proposals",
    "admin-feedback-quarantine",
    "admin-tutorial-editor",
)


@pytest.fixture
def fresh_seed(database):
    seed_tutorial_steps(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )
    yield database


def _fetch_step(database, slug: str) -> dict | None:
    with database.transaction() as transaction:
        row = transaction.fetch_one(
            "SELECT slug, audience, display_order, target_kind, target_key, "
            "title, body, archived_at FROM tutorial_step WHERE slug = %s",
            (slug,),
        )
    if row is None:
        return None
    return dict(row)


def test_seed_creates_the_canonical_user_flow(fresh_seed):
    for slug, (order, kind, key) in USER_FLOW.items():
        row = _fetch_step(fresh_seed, slug)
        assert row is not None, f"seed must create the {slug!r} step"
        assert row["audience"] == "user", slug
        assert row["display_order"] == order, slug
        assert row["target_kind"] == kind, slug
        assert row["target_key"] == key, slug
        assert row["archived_at"] is None, slug


def test_seed_creates_the_canonical_admin_flow(fresh_seed):
    max_user_order = max(order for order, _, _ in USER_FLOW.values())
    for slug, (order, kind, key) in ADMIN_FLOW.items():
        row = _fetch_step(fresh_seed, slug)
        assert row is not None, f"seed must create the {slug!r} step"
        assert row["audience"] == "admin", slug
        assert row["display_order"] == order, slug
        assert row["target_kind"] == kind, slug
        assert row["target_key"] == key, slug
        assert row["display_order"] > max_user_order, slug
        assert "quarantine" not in row["body"].lower(), slug


def test_turn_on_agents_step_explains_enablement(fresh_seed):
    row = _fetch_step(fresh_seed, "turn-on-agents")
    assert row is not None
    body_lower = row["body"].lower()
    assert "agent" in body_lower
    assert any(phrase in body_lower for phrase in ("turn", "switch on", "enable")), (
        f"step body must instruct the user to turn on agents: {row['body']!r}"
    )
    assert "enable recommended agents" in body_lower, (
        "step body must name the consent card's actual button"
    )
    assert "read-only" in body_lower and "never write" in body_lower, (
        "step body must state the read-only, never-write grant"
    )


def test_user_flow_is_strictly_ordered(fresh_seed):
    sequence = [
        "welcome-tour", "meet-the-canvas", "turn-on-agents",
        "ask-in-plain-language", "open-settings-menu", "agents-and-permissions",
        "personalize-your-assistant", "review-your-audit-log",
        "workspace-timeline", "help-anytime", "tour-complete",
    ]
    orders = [_fetch_step(fresh_seed, slug)["display_order"] for slug in sequence]
    assert orders == sorted(orders) and len(set(orders)) == len(orders), orders


def test_every_static_target_resolves_to_a_real_anchor(fresh_seed):
    from astralprojection.resources import template_path
    from webrender.chrome import render_settings_nav
    from webrender.chrome.menu_model import build_menu_model
    from webrender.chrome.topbar import render_topbar

    dom = render_topbar(roles=["admin", "user"])
    dom += render_settings_nav(build_menu_model(roles=["admin", "user"]), "agents")
    dom += template_path("shell.html").read_text(encoding="utf-8")
    anchors = set(re.findall(r'data-tour-target="([^"]+)"', dom))
    for slug, (_, kind, key) in {**USER_FLOW, **ADMIN_FLOW}.items():
        if kind != "static":
            continue
        assert key in anchors, (
            f"step {slug!r} targets {key!r}, which no chrome/shell element "
            f"carries as data-tour-target (known anchors: {sorted(anchors)})"
        )


def test_legacy_steps_are_no_longer_active(fresh_seed):
    for slug in LEGACY_TUTORIAL_SLUGS:
        row = _fetch_step(fresh_seed, slug)
        assert row is None or row["archived_at"] is not None, (
            f"legacy step {slug!r} is still active — the 030 tour refresh "
            f"migration did not archive it"
        )
