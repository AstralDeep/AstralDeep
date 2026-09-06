"""Readable-extraction hygiene for the web_research + summarizer agents.

Covers the shared shared/web_readability helpers and their wiring into both
agents' HTML extractors and page tools: navigation/boilerplate is stripped by
class/id/role (not just semantic tag), unbroken junk blobs (base64/serialized
state) are dropped, real content is kept, and fetched-page output carries a
source link for auditability.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://fake.api")
os.environ.setdefault("LLM_MODEL", "test-model")

from shared import web_readability as wr  # noqa: E402

_JUNK = "eNq9mFFv" + ("AbCdEf0123456789" * 9)  # 152 chars, base64-shaped, no spaces

_PAGE = (
    "<!doctype html><html><head><title>NEA SMR Dashboard</title></head><body>"
    '<div class="usa-banner">Official websites use .gov. A .gov website belongs '
    "to an official government organization in the United States.</div>"
    '<a class="skip-link" href="#main">Skip to main content</a>'
    '<ul class="main-nav"><li>Home</li><li>About us</li><li>Topics</li>'
    "<li>News and resources</li><li>Learning and tools</li></ul>"
    "<nav><a>Homepage</a><a>Topics</a></nav>"
    "<main><h1>Overview</h1>"
    "<p>The NEA SMR Dashboard provides a comprehensive assessment of progress "
    "made by SMR designers and companies worldwide.</p>"
    f'<div class="serialized-state">{_JUNK}</div>'
    "</main>"
    '<footer class="site-footer">Copyright &copy; 2025 OECD</footer>'
    "</body></html>"
)


# --- shared helper ---------------------------------------------------------

def test_should_skip_attrs_matches_chrome_class_id_role():
    assert wr.should_skip_attrs([("class", "main-nav")])
    assert wr.should_skip_attrs([("id", "site-footer")])
    assert wr.should_skip_attrs([("class", "usa-banner")])
    assert wr.should_skip_attrs([("class", "cookie-consent")])
    assert wr.should_skip_attrs([("role", "navigation")])
    assert wr.should_skip_attrs([("aria-hidden", "true")])


def test_should_skip_attrs_keeps_content_elements():
    assert not wr.should_skip_attrs([("class", "article-body")])
    assert not wr.should_skip_attrs([("class", "overview")])
    assert not wr.should_skip_attrs([("id", "main")])
    assert not wr.should_skip_attrs([])


@pytest.mark.parametrize("attribute", ["class", "id"])
@pytest.mark.parametrize("value", ["menu", "document-list menu", "download-menu", "menu-reference"])
def test_menu_attribute_alone_does_not_discard_content(attribute, value):
    assert not wr.should_skip_attrs([(attribute, value)])


@pytest.mark.parametrize("attribute", ["class", "id"])
@pytest.mark.parametrize("value", [
    "site-menu", "main-menu", "global_menu", "mobile-menu expanded", "megamenu",
])
def test_qualified_navigation_menus_remain_chrome(attribute, value):
    assert wr.should_skip_attrs([(attribute, value)])


@pytest.mark.parametrize("role", ["navigation", "menu", "menubar", "MENU"])
def test_explicit_navigation_roles_remain_chrome(role):
    assert wr.should_skip_attrs([("role", role)])


@pytest.mark.parametrize("extractor", ["web_research", "summarizer"])
def test_both_extractors_keep_menu_content_and_remove_explicit_navigation(extractor):
    html = (
        "<html><body><main><h1>Product release archive</h1>"
        '<ol class="document-list menu"><li><a href="/releases/4-7-2">'
        'Atlas 4.7.2</a><span>Stable release</span><br><img src="badge.png"></li></ol>'
        '<ul id="menu"><li>Atlas 4.6.9 remains supported</li></ul>'
        '<nav><a>Semantic navigation</a></nav><div role="menu">Role menu</div>'
        '<div role="menubar">Role menubar</div><div role="navigation">Role navigation</div>'
        '<div class="site-menu">Site navigation</div>'
        '<div id="mobile_menu">Mobile navigation</div>'
        '<script>hidden_code()</script><p>End of release archive.</p></main></body></html>'
    )
    if extractor == "web_research":
        from agents.web_research.mcp_tools import _extract_readable
        _, text = _extract_readable(html)
    else:
        from agents.summarizer.mcp_tools import _extract_text
        _, text = _extract_text(SimpleNamespace(text=html, headers={"Content-Type": "text/html"}))
    for content in ("Product release archive", "Atlas 4.7.2", "Stable release",
                    "Atlas 4.6.9 remains supported", "End of release archive."):
        assert content in text
    for chrome in ("Semantic navigation", "Role menu", "Role menubar", "Role navigation",
                   "Site navigation", "Mobile navigation", "hidden_code"):
        assert chrome not in text


def test_clean_page_text_drops_boilerplate_and_junk_keeps_prose():
    raw = (
        "Skip to main content\n\n"
        "Official websites use .gov\n\n"
        "# Overview\n\n"
        "The dashboard tracks 42 SMRs across the world.\n\n"
        f"{_JUNK}\n\n"
        "Copyright © 2025 OECD"
    )
    out = wr.clean_page_text(raw)
    assert "# Overview" in out
    assert "42 SMRs" in out
    assert "Skip to main content" not in out
    assert "Official websites use .gov" not in out
    assert _JUNK not in out
    assert "Copyright" not in out


def test_clean_page_text_keeps_long_real_sentences_and_urls():
    # A long sentence (has spaces) is prose, not a junk token.
    sentence = "word " * 60
    # A URL contains ':' and '.', excluded from the junk-token charset.
    url_line = "https://www.oecd-nea.org/jcms/pl_12345/nea-smr-dashboard-edition-iii"
    out = wr.clean_page_text(f"{sentence}\n\n{url_line}")
    assert "word word" in out
    assert url_line in out


def test_source_markdown_is_a_link():
    assert wr.source_markdown("https://x.test/a") == "Source: [https://x.test/a](https://x.test/a)"


# --- web_research extractor + fetch_page -----------------------------------

def test_web_research_extractor_strips_chrome_keeps_content():
    from agents.web_research.mcp_tools import _extract_readable
    title, text = _extract_readable(_PAGE)
    assert title == "NEA SMR Dashboard"
    assert "Overview" in text
    assert "comprehensive assessment of progress" in text
    for chrome in ("Home", "About us", "News and resources", "Learning and tools",
                   "Skip to main content", "Official websites use .gov",
                   "Homepage", "Copyright"):
        assert chrome not in text, f"chrome leaked: {chrome!r}"
    assert _JUNK not in text


def test_fetch_page_prepends_source_link(monkeypatch):
    from agents.web_research import mcp_tools as wrt
    resp = SimpleNamespace(text=_PAGE, headers={"Content-Type": "text/html"})
    monkeypatch.setattr(wrt, "_fetch_url", lambda url: resp)
    out = wrt.fetch_page(url="https://www.oecd-nea.org/smr")
    card = out["_ui_components"][-1]
    content = card["content"]
    src = content[0]
    assert src["type"] == "text" and src.get("variant") == "markdown"
    assert "https://www.oecd-nea.org/smr" in src["content"]
    body = content[1]["content"]
    assert "comprehensive assessment" in body
    assert "Skip to main content" not in body and _JUNK not in body
    assert out["_data"]["url"] == "https://www.oecd-nea.org/smr"


# --- summarizer extractor + summarize_url ----------------------------------

def test_summarizer_extractor_strips_chrome_keeps_content():
    from agents.summarizer.mcp_tools import _extract_text
    resp = SimpleNamespace(text=_PAGE, headers={"Content-Type": "text/html"})
    title, text = _extract_text(resp)
    assert title == "NEA SMR Dashboard"
    assert "comprehensive assessment of progress" in text
    for chrome in ("Home", "About us", "Learning and tools",
                   "Skip to main content", "Official websites use .gov", "Copyright"):
        assert chrome not in text, f"chrome leaked: {chrome!r}"
    assert _JUNK not in text


def test_summarize_url_prepends_source_link(monkeypatch):
    from agents.summarizer import mcp_tools as st
    resp = SimpleNamespace(text=_PAGE, headers={"Content-Type": "text/html"})
    monkeypatch.setattr(st, "_fetch_url", lambda url: resp)
    monkeypatch.setattr(st, "_call_summary_llm",
                        lambda text, focus, kwargs: {"tldr": "ok", "key_points": [], "quotes": []})
    out = st.summarize_url(url="https://www.energy.gov/smr")
    first = out["_ui_components"][0]
    assert first["type"] == "text" and first.get("variant") == "markdown"
    assert "https://www.energy.gov/smr" in first["content"]
    assert out["_data"]["url"] == "https://www.energy.gov/smr"
