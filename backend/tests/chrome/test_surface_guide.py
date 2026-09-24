"""Tests for the static guide chrome surface (AstralProjection
webrender/chrome/surfaces/guide.py and guide_content.py): section inventory, TOC
navigation via chrome_open, admin gating, and escaping.
"""

import asyncio
import inspect

from webrender.chrome import guide_content
from webrender.chrome.surfaces import guide

EXPECTED_SLUGS = [
    "intro", "signing-in", "dashboard", "chat", "attachments", "voice",
    "agents", "components", "feedback", "audit", "tutorial", "tooltips",
    "preferences", "device", "privacy", "admin",
]


def _render(roles=("user",), params=None):
    return asyncio.run(guide.render(None, "user-1", list(roles), params or {}))


def test_module_contract():
    assert guide.TITLE == "User guide"
    assert not getattr(guide, "ADMIN_ONLY", False)
    assert inspect.iscoroutinefunction(guide.render)
    assert not getattr(guide, "HANDLERS", {})


def test_sections_cover_full_react_panel_inventory_in_order():
    slugs = [s["slug"] for s in guide_content.SECTIONS]
    assert slugs == EXPECTED_SLUGS


def test_sections_are_well_formed():
    seen = set()
    for s in guide_content.SECTIONS:
        assert s["slug"] and isinstance(s["slug"], str)
        assert s["title"] and isinstance(s["title"], str)
        assert s["body_html"] and isinstance(s["body_html"], str)
        assert "<h1" in s["body_html"], f"section {s['slug']} missing heading"
        assert s["slug"] not in seen, f"duplicate slug {s['slug']}"
        seen.add(s["slug"])


def test_only_admin_section_is_admin_only():
    flags = {s["slug"]: bool(s.get("admin_only")) for s in guide_content.SECTIONS}
    assert flags.pop("admin") is True
    assert not any(flags.values())


def test_body_html_escapes_text_literals():
    by_slug = {s["slug"]: s["body_html"] for s in guide_content.SECTIONS}
    assert "Combine &amp; condense" in by_slug["components"]
    assert "&quot;summarise this PDF&quot;" in by_slug["chat"]
    assert "you&#x27;ll" in by_slug["intro"]
    assert "<script" not in "".join(by_slug.values())


def test_default_render_selects_first_section_and_lists_full_toc():
    html = _render()
    assert "Welcome to AstralDeep" in html
    assert 'aria-label="User guide sections"' in html
    assert 'data-ui-action="chrome_open"' in html
    for slug in EXPECTED_SLUGS[:-1]:
        assert f"&quot;section&quot;: &quot;{slug}&quot;" in html, f"TOC missing {slug}"
    assert "&quot;surface&quot;: &quot;guide&quot;" in html


def test_section_param_selects_article_and_marks_toc_active():
    html = _render(params={"section": "audit"})
    assert "Your audit log" in html
    assert "append-only and signed" in html
    assert "Welcome to AstralDeep" not in html
    assert 'aria-current="true"' in html
    assert html.count('aria-current="true"') == 1


def test_unknown_section_falls_back_to_first():
    html = _render(params={"section": "definitely-not-a-section"})
    assert "Welcome to AstralDeep" in html


def test_none_params_tolerated():
    html = asyncio.run(guide.render(None, "user-1", ["user"], None))
    assert "Welcome to AstralDeep" in html


def test_every_visible_section_renders_when_requested():
    for s in guide_content.SECTIONS:
        html = _render(roles=("admin", "user"), params={"section": s["slug"]})
        assert s["body_html"] in html, f"section {s['slug']} did not render"


def test_admin_section_absent_for_non_admin():
    html = _render(roles=("user",))
    assert "For administrators" not in html
    assert "&quot;section&quot;: &quot;admin&quot;" not in html


def test_admin_section_request_by_non_admin_falls_back():
    html = _render(roles=("user",), params={"section": "admin"})
    assert "operator-only operations" not in html
    assert "Welcome to AstralDeep" in html


def test_admin_section_present_for_admin():
    html = _render(roles=("admin", "user"))
    assert "For administrators" in html
    detail = _render(roles=("admin", "user"), params={"section": "admin"})
    assert "operator-only operations" in detail


def test_toc_titles_are_escaped():
    html = _render()
    assert "Attachments &amp; files" in html
    assert "Attachments & files</button>" not in html
