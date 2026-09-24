"""Tests for agents/web_research/mcp_tools.py: DuckDuckGo HTML result parsing, uddg href
decoding, and readable-text extraction, against embedded HTML fixtures.
"""

from agents.web_research.mcp_tools import (
    PageTextExtractor,
    _decode_ddg_href,
    _extract_readable,
    _parse_ddg_html,
    _split_sections,
    _strip_out_of_range_citations,
)

DDG_HTML = """<!DOCTYPE html>
<html><head><title>python at DuckDuckGo</title></head><body>
<div class="serp__results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a"
           href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpython&amp;rut=abc123">
          Python <b>Tutorial</b> &mdash; Example</a>
      </h2>
      <a class="result__snippet"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpython&amp;rut=abc123">
        Learn <b>Python</b> from
        scratch.</a>
    </div>
  </div>
  <div class="result">
    <h2 class="result__title">
      <a class="result__a" href="https://direct.example.org/page">Direct result</a>
    </h2>
    <div class="result__snippet">A direct, unwrapped link.</div>
  </div>
  <div class="result">
    <h2 class="result__title">
      <a class="result__a" href="https://direct.example.org/page">Duplicate URL</a>
    </h2>
  </div>
</div>
</body></html>"""

EMPTY_HTML = """<!DOCTYPE html>
<html><head><title>no results</title></head>
<body><div class="no-results">No results.</div></body></html>"""


def test_decode_uddg_wrapped_href() -> None:
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Ffoo.bar%2Fbaz%3Fq%3D1&rut=xyz"
    assert _decode_ddg_href(href) == "https://foo.bar/baz?q=1"


def test_decode_uddg_relative_path() -> None:
    assert _decode_ddg_href("/l/?uddg=https%3A%2F%2Fexample.com%2Fa%20b") == \
        "https://example.com/a b"


def test_decode_direct_href_passthrough() -> None:
    assert _decode_ddg_href("https://example.com/page") == "https://example.com/page"


def test_decode_empty_href() -> None:
    assert _decode_ddg_href("") == ""


def test_decode_uddg_missing_param_falls_back() -> None:
    href = "//duckduckgo.com/l/?rut=onlytracking"
    assert _decode_ddg_href(href) == href


def test_parse_happy_path_extracts_title_url_snippet() -> None:
    results = _parse_ddg_html(DDG_HTML, max_results=10)
    assert len(results) == 2
    first = results[0]
    assert first["url"] == "https://example.com/python"
    assert first["title"] == "Python Tutorial — Example"
    assert first["snippet"] == "Learn Python from scratch."
    second = results[1]
    assert second["url"] == "https://direct.example.org/page"
    assert second["title"] == "Direct result"
    assert second["snippet"] == "A direct, unwrapped link."


def test_parse_respects_max_results() -> None:
    assert len(_parse_ddg_html(DDG_HTML, max_results=1)) == 1


def test_parse_empty_results_page() -> None:
    assert _parse_ddg_html(EMPTY_HTML, max_results=10) == []


def test_parse_garbage_input_yields_nothing() -> None:
    assert _parse_ddg_html("<<<<not html at all", max_results=10) == []


def test_parse_nested_same_tag_inside_snippet() -> None:
    html = """
    <a class="result__a" href="https://example.com/x">Title X</a>
    <a class="result__snippet">starts <a href="#">nested anchor</a> ends.</a>
    """
    results = _parse_ddg_html(html, max_results=10)
    assert results[0]["snippet"] == "starts nested anchor ends."


PAGE_HTML = """<!DOCTYPE html>
<html><head><title>  The   Page Title </title>
<script>var secret = "do-not-leak";</script>
<style>.x { color: red; }</style></head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<header>Site banner</header>
<main>
  <h1>Main Heading</h1>
  <p>First paragraph with
     wrapped    whitespace.</p>
  <h2>Sub Heading</h2>
  <ul><li>Item one</li><li>Item two</li></ul>
</main>
<footer>Copyright nobody</footer>
<script>console.log("also hidden");</script>
</body></html>"""


def test_extract_readable_title_and_markdown() -> None:
    title, text = _extract_readable(PAGE_HTML)
    assert title == "The Page Title"
    assert "# Main Heading" in text
    assert "## Sub Heading" in text
    assert "- Item one" in text
    assert "First paragraph with wrapped whitespace." in text


def test_extract_readable_strips_chrome() -> None:
    _title, text = _extract_readable(PAGE_HTML)
    assert "do-not-leak" not in text
    assert "color: red" not in text
    assert "Home" not in text
    assert "Site banner" not in text
    assert "Copyright nobody" not in text


def test_extractor_tolerates_unclosed_tags() -> None:
    parser = PageTextExtractor()
    parser.feed("<html><body><p>open paragraph <b>bold")
    assert "open paragraph bold" in parser.text()


def test_split_sections() -> None:
    brief = "## Alpha\nbody a\n\n## Beta\nbody b\nmore"
    sections = _split_sections(brief)
    assert [heading for heading, _ in sections] == ["Alpha", "Beta"]
    assert sections[0][1] == "body a"
    assert "more" in sections[1][1]


def test_split_sections_no_headings() -> None:
    assert _split_sections("just a flat paragraph") == []


def test_strip_out_of_range_citations() -> None:
    text = "Claim [1] and [2] but never [7]."
    assert _strip_out_of_range_citations(text, 2) == "Claim [1] and [2] but never ."


def test_strip_citations_keeps_in_range() -> None:
    assert _strip_out_of_range_citations("[1][2][3]", 3) == "[1][2][3]"
