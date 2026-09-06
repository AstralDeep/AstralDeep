"""fetch_page and research_brief tests — egress gating, truncation, synthesis."""
from unittest.mock import patch

import requests

from agents.web_research import mcp_tools
from agents.web_research.mcp_tools import (
    DDG_HTML_URL,
    FETCH_MAX_BYTES,
    PAGE_TEXT_CAP,
    fetch_page,
    research_brief,
)
from agents.web_research.tests.conftest import ExplodingOpenAI
from agents.web_research.tests.test_ddg_parsing import DDG_HTML
from shared.tests._http_mock import HttpMock

PAGE_ONE = """<html><head><title>Page One</title></head><body>
<h1>Alpha</h1><p>Content about pythons.</p>
<script>hidden()</script></body></html>"""

PAGE_TWO = """<html><head><title>Page Two</title></head><body>
<h1>Beta</h1><p>More snake facts.</p></body></html>"""


# ---------------------------------------------------------------------------
# fetch_page
# ---------------------------------------------------------------------------


def test_fetch_page_renders_card_with_markdown(rmock: HttpMock) -> None:
    rmock.add("GET", "https://example.com/python", status=200,
              body=PAGE_ONE.encode("utf-8"),
              headers={"Content-Type": "text/html; charset=utf-8"})
    result = fetch_page(url="https://example.com/python")
    card = result["_ui_components"][0]
    assert card["type"] == "card"
    assert card["title"] == "Page One"
    source = card["content"][0]
    assert source["type"] == "text" and source["variant"] == "markdown"
    assert "https://example.com/python" in source["content"]
    text = card["content"][1]
    assert text["type"] == "text"
    assert text["variant"] == "markdown"
    assert "# Alpha" in text["content"]
    assert "hidden()" not in text["content"]
    assert result["_data"]["truncated"] is False


def test_fetch_page_keeps_release_menu_rows(rmock: HttpMock) -> None:
    html = (
        '<html><body><nav>Site links</nav><main><h1>Release archive</h1>'
        '<ol class="list-row-container menu"><li><a href="/release/4-7-2">Atlas 4.7.2</a>'
        '<span>Stable</span></li><li>Atlas 4.6.9</li></ol>'
        '<ul class="main-menu"><li>Navigation links</li></ul></main></body></html>'
    )
    rmock.add("GET", "https://example.com/releases", status=200,
              body=html.encode("utf-8"), headers={"Content-Type": "text/html"})
    result = fetch_page(url="https://example.com/releases")
    text = result["_ui_components"][0]["content"][1]["content"]
    assert "Atlas 4.7.2" in text and "Atlas 4.6.9" in text
    assert "Stable" in text
    assert "Site links" not in text and "Navigation links" not in text
    assert result["_data"]["truncated"] is False


def test_fetch_page_truncation_notice_when_capped(rmock: HttpMock) -> None:
    long_paragraph = "word " * ((PAGE_TEXT_CAP // 5) + 2000)
    html = f"<html><head><title>Long</title></head><body><p>{long_paragraph}</p></body></html>"
    rmock.add("GET", "https://example.com/long", status=200,
              body=html.encode("utf-8"),
              headers={"Content-Type": "text/html"})
    result = fetch_page(url="https://example.com/long")
    alert = result["_ui_components"][0]
    assert alert["type"] == "alert"
    assert alert["variant"] == "info"
    assert "truncated" in alert["message"].lower()
    card = result["_ui_components"][1]
    assert len(card["content"][0]["content"]) <= PAGE_TEXT_CAP
    assert result["_data"]["truncated"] is True


def test_fetch_page_over_one_megabyte_is_refused(rmock: HttpMock) -> None:
    rmock.add("GET", "https://example.com/huge", status=200,
              body=b"x" * (FETCH_MAX_BYTES + 1))
    result = fetch_page(url="https://example.com/huge")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "1 MB" in alert["message"]


def test_fetch_page_egress_refusal_on_private_host() -> None:
    result = fetch_page(url="https://internal.example.com/secret")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "egress is blocked" in alert["message"]


def test_fetch_page_follows_redirect_with_revalidation(rmock: HttpMock) -> None:
    rmock.add("GET", "https://redirect.example.com/old", status=301, body=b"",
              headers={"Location": "https://example.com/python"})
    rmock.add("GET", "https://example.com/python", status=200,
              body=PAGE_ONE.encode("utf-8"),
              headers={"Content-Type": "text/html"})
    result = fetch_page(url="https://redirect.example.com/old")
    assert result["_ui_components"][0]["title"] == "Page One"


def test_fetch_page_redirect_into_private_space_is_blocked(rmock: HttpMock) -> None:
    rmock.add("GET", "https://redirect.example.com/trap", status=302, body=b"",
              headers={"Location": "https://internal.example.com/admin"})
    result = fetch_page(url="https://redirect.example.com/trap")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "egress is blocked" in alert["message"]


def test_fetch_page_redirect_without_location_is_error(rmock: HttpMock) -> None:
    rmock.add("GET", "https://redirect.example.com/nowhere", status=302, body=b"")
    result = fetch_page(url="https://redirect.example.com/nowhere")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "Location" in alert["message"]


def test_fetch_page_redirect_loop_is_error(rmock: HttpMock) -> None:
    rmock.add("GET", "https://redirect.example.com/loop", status=301, body=b"",
              headers={"Location": "https://redirect.example.com/loop"})
    result = fetch_page(url="https://redirect.example.com/loop")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "Too many redirects" in alert["message"]


def test_fetch_page_unreachable_is_error_alert() -> None:
    with patch("requests.request", side_effect=requests.ConnectionError("down")):
        result = fetch_page(url="https://example.com/python")
    assert result["_ui_components"][0]["variant"] == "error"


def test_fetch_page_empty_url_is_error() -> None:
    result = fetch_page(url="")
    assert result["_ui_components"][0]["variant"] == "error"


def test_fetch_page_non_html_returns_raw_text(rmock: HttpMock) -> None:
    rmock.add("GET", "https://example.com/data.txt", status=200,
              body=b"plain text payload",
              headers={"Content-Type": "text/plain"})
    result = fetch_page(url="https://example.com/data.txt")
    card = result["_ui_components"][0]
    assert card["title"] == "https://example.com/data.txt"
    assert "https://example.com/data.txt" in card["content"][0]["content"]
    assert card["content"][1]["content"] == "plain text payload"


# ---------------------------------------------------------------------------
# research_brief
# ---------------------------------------------------------------------------

BRIEF_WITH_SECTIONS = (
    "## Overview\nPythons are constrictors [1]. Bogus citation [9].\n\n"
    "## Habitat\nThey live in warm climates [2]."
)


def _register_search_and_pages(rmock: HttpMock) -> None:
    rmock.add("GET", DDG_HTML_URL, status=200, body=DDG_HTML.encode("utf-8"))
    rmock.add("GET", "https://example.com/python", status=200,
              body=PAGE_ONE.encode("utf-8"), headers={"Content-Type": "text/html"})
    rmock.add("GET", "https://direct.example.org/page", status=200,
              body=PAGE_TWO.encode("utf-8"), headers={"Content-Type": "text/html"})


def test_brief_happy_path_card_table_tabs(rmock: HttpMock, fake_openai) -> None:
    _register_search_and_pages(rmock)
    fake_openai(BRIEF_WITH_SECTIONS)
    result = research_brief(topic="pythons", depth="standard")
    components = result["_ui_components"]
    card, table, tabs = components[0], components[1], components[2]

    assert card["type"] == "card"
    assert card["title"] == "Research brief: pythons"
    brief_text = card["content"][0]["content"]
    assert "[1]" in brief_text and "[2]" in brief_text
    assert "[9]" not in brief_text  # out-of-range citation stripped

    assert table["type"] == "table"
    assert table["headers"] == ["#", "Source", "Title", "Retrieved"]
    assert len(table["rows"]) == 2
    assert table["rows"][0][1] == "https://example.com/python"
    assert all(row[3] for row in table["rows"])  # Retrieved timestamp present

    assert tabs["type"] == "tabs"
    assert [tab["label"] for tab in tabs["tabs"]] == ["Overview", "Habitat"]


def test_brief_cites_only_fetched_urls_in_data(rmock: HttpMock, fake_openai) -> None:
    _register_search_and_pages(rmock)
    fake_openai(BRIEF_WITH_SECTIONS)
    result = research_brief(topic="pythons")
    urls = {s["url"] for s in result["_data"]["sources"]}
    assert urls == {"https://example.com/python", "https://direct.example.org/page"}


def test_brief_shallow_depth_fetches_two_pages(rmock: HttpMock, fake_openai) -> None:
    """Three results available, but shallow depth fetches only the first two."""
    provider_url = "https://search.example.com/api"
    creds = {"SEARCH_API_URL": provider_url, "SEARCH_API_KEY": "sk"}
    rmock.add("POST", provider_url, status=200, json={"results": [
        {"title": "One", "url": "https://example.com/one", "content": "s1"},
        {"title": "Two", "url": "https://example.com/two", "content": "s2"},
        {"title": "Three", "url": "https://example.com/three", "content": "s3"},
    ]})
    for path in ("one", "two", "three"):
        rmock.add("GET", f"https://example.com/{path}", status=200,
                  body=PAGE_ONE.encode("utf-8"),
                  headers={"Content-Type": "text/html"})
    fake_openai("## Only\nOne section [1].")
    result = research_brief(topic="pythons", depth="shallow", _credentials=creds)
    assert len(result["_data"]["sources"]) == 2
    page_fetches = [c for c in rmock.calls if c["method"] == "GET"]
    assert len(page_fetches) == 2
    assert "https://example.com/three" not in {c["url"] for c in rmock.calls}


def test_brief_single_section_has_no_tabs(rmock: HttpMock, fake_openai) -> None:
    _register_search_and_pages(rmock)
    fake_openai("## Only\nOne section [1].")
    result = research_brief(topic="pythons")
    types = [c["type"] for c in result["_ui_components"]]
    assert types == ["card", "table"]


def test_brief_invalid_depth_falls_back_to_standard(rmock: HttpMock, fake_openai) -> None:
    _register_search_and_pages(rmock)
    fake_openai(BRIEF_WITH_SECTIONS)
    result = research_brief(topic="pythons", depth="exhaustive")
    assert result["_data"]["depth"] == "standard"


def test_brief_zero_fetched_pages_is_error_without_llm_call(
        rmock: HttpMock, monkeypatch) -> None:
    """Search succeeds but every page 404s -> error Alert, LLM never touched."""
    rmock.add("GET", DDG_HTML_URL, status=200, body=DDG_HTML.encode("utf-8"))
    monkeypatch.setattr(mcp_tools, "OpenAI", ExplodingOpenAI)
    result = research_brief(
        topic="pythons",
        _session_llm_credentials={"OPENAI_API_KEY": "test-key"})
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "never cites" in alert["message"]


def test_brief_search_failure_is_actionable_error() -> None:
    with patch("requests.request", side_effect=requests.ConnectionError("down")):
        result = research_brief(topic="pythons")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "DuckDuckGo" in alert["message"]
    assert "SEARCH_API_URL" in alert["message"]


def test_brief_no_search_results_is_error(rmock: HttpMock) -> None:
    from agents.web_research.tests.test_ddg_parsing import EMPTY_HTML
    rmock.add("GET", DDG_HTML_URL, status=200, body=EMPTY_HTML.encode("utf-8"))
    result = research_brief(topic="nothingburger")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "never fabricated" in alert["message"]


def test_brief_llm_unavailable_is_error(rmock: HttpMock, no_llm_credentials) -> None:
    _register_search_and_pages(rmock)
    result = research_brief(topic="pythons")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "LLM" in alert["message"]


def test_brief_skips_sources_with_no_readable_text(
        rmock: HttpMock, fake_openai) -> None:
    """A fetched page with no extractable text is skipped, not cited."""
    rmock.add("GET", DDG_HTML_URL, status=200, body=DDG_HTML.encode("utf-8"))
    rmock.add("GET", "https://example.com/python", status=200, body=b"",
              headers={"Content-Type": "text/html"})
    rmock.add("GET", "https://direct.example.org/page", status=200,
              body=PAGE_TWO.encode("utf-8"), headers={"Content-Type": "text/html"})
    fake_openai("## Only\nFacts [1].")
    result = research_brief(topic="pythons")
    sources = result["_data"]["sources"]
    assert len(sources) == 1
    assert sources[0]["url"] == "https://direct.example.org/page"


def test_brief_empty_llm_output_is_error(rmock: HttpMock, fake_openai) -> None:
    _register_search_and_pages(rmock)
    fake_openai("")
    result = research_brief(topic="pythons")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "empty" in alert["message"].lower()


def test_brief_llm_exception_is_synthesis_error(rmock: HttpMock, fake_openai) -> None:
    _register_search_and_pages(rmock)
    fake_openai(RuntimeError("model exploded"))
    result = research_brief(topic="pythons")
    alert = result["_ui_components"][0]
    assert alert["variant"] == "error"
    assert "model exploded" in alert["message"]


def test_brief_empty_topic_is_error() -> None:
    result = research_brief(topic=" ")
    assert result["_ui_components"][0]["variant"] == "error"


def test_brief_uses_session_llm_credentials(rmock: HttpMock, fake_openai) -> None:
    """Per-session credential resolution mirrors the general agent (006)."""
    _register_search_and_pages(rmock)
    fake_cls = fake_openai(BRIEF_WITH_SECTIONS)
    research_brief(
        topic="pythons",
        _session_llm_credentials={
            "OPENAI_API_KEY": "session-key",
            "OPENAI_BASE_URL": "https://llm.example.com/v1",
            "LLM_MODEL": "session-model",
        },
    )
    assert fake_cls.last_init == {
        "api_key": "session-key", "base_url": "https://llm.example.com/v1",
    }
    assert fake_cls.calls_log[-1]["model"] == "session-model"
