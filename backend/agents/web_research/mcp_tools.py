#!/usr/bin/env python3
"""
MCP Tools for the Web Research agent — tool functions that return UI Primitives.

Includes:
- web_search: keyless DuckDuckGo HTML search (one retry via the Lite endpoint
  when DuckDuckGo answers its bot challenge), or an operator/user-configured
  Tavily-compatible JSON search provider (SEARCH_API_URL + SEARCH_API_KEY)
- fetch_page: egress-gated page fetch (1 MB / 15 s) with readable-text extraction
- research_brief: search -> fetch top sources -> one LLM synthesis call that
  cites only the URLs it actually fetched (sources are never fabricated)

All outbound HTTP goes through ``shared.external_http`` (SSRF/private-host
gating, bounded timeouts, response-size caps). Pure stdlib parsing
(``html.parser``, ``urllib.parse``) — no new third-party dependencies.
"""
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from openai import OpenAI  # noqa: E402

from astralprims import (  # noqa: E402
    Alert, Card, List_, TabItem, Table, Tabs, Text, create_ui_response,
)
from shared import external_http  # noqa: E402
from shared.external_http import (  # noqa: E402
    AuthFailedError,
    EgressBlockedError,
    ExternalHttpError,
    RateLimitedError,
    ResponseTooLargeError,
    ServiceUnreachableError,
)
from shared.llm_text import strip_reasoning_markup  # noqa: E402
from shared.web_readability import (  # noqa: E402
    VOID_TAGS,
    clean_page_text,
    should_skip_attrs,
    source_markdown,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds and constants (FR-013: bounded fetch sizes and timeouts)
# ---------------------------------------------------------------------------

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
# Second keyless endpoint, tried once when the HTML one answers with its
# bot challenge. Different markup (a results table), so it has its own parser.
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FETCH_MAX_BYTES = 1024 * 1024   # 1 MB hard cap per fetch
FETCH_TIMEOUT_S = 15            # per-fetch timeout
SEARCH_TIMEOUT_S = 15
DEFAULT_MAX_RESULTS = 8
MAX_RESULTS_CAP = 20
PAGE_TEXT_CAP = 20_000          # chars of extracted text rendered by fetch_page
BRIEF_SOURCE_CAP = 4_000        # chars of extract per source in the LLM prompt
BRIEF_FETCHES = {"shallow": 2, "standard": 5}  # <= 5 fetches per brief
# 030: overall wall-clock budget for one research_brief run. Worst-case
# search (15 s) + 5 fetches (15 s each) exceeded the orchestrator dispatch
# ceiling and surfaced as "Tool call timed out" with no partial value; the
# brief now stops fetching when the budget is spent and builds from what it
# has (the dispatch ceiling for research_brief is 150 s — keep headroom).
BRIEF_TIME_BUDGET_S = 110
MAX_REDIRECT_HOPS = 3

DDG_BACKEND = "DuckDuckGo HTML search"
DDG_LITE_BACKEND = "DuckDuckGo Lite search"
PROVIDER_BACKEND = "the configured search provider (SEARCH_API_URL)"

_CREDENTIAL_REMEDY = (
    "You can configure a search provider via the optional SEARCH_API_URL and "
    "SEARCH_API_KEY credentials in the agent's settings."
)

# DuckDuckGo intermittently answers non-browser traffic from datacenter
# addresses with HTTP 202 and a ~14 KB "bots use DuckDuckGo too" puzzle page
# that carries no result anchors at all. Parsing it as an ordinary page yields
# an EMPTY result list — which research_brief then reported as "returned no
# results" — so the challenge is recognised explicitly. The markers are the
# specific class names / phrase seen on the live challenge page rather than
# the bare words "anomaly"/"challenge"/"bots": a genuine (well-formed, empty)
# results page echoes the user's query, and a query such as "anomaly
# detection challenge" must still be reported honestly as no results.
_DDG_CHALLENGE_MARKERS = (
    "anomaly-modal",
    "js-anomaly",
    "challenge-form",
    "bots use duckduckgo",
)


class DDGChallengeError(ServiceUnreachableError):
    """DuckDuckGo refused the keyless search with its bot challenge.

    A transient backend refusal (the same address is served real results a
    moment later), raised only after the Lite endpoint retry also failed, so
    the caller reports an actionable error instead of an empty result set.
    """


# ---------------------------------------------------------------------------
# DuckDuckGo HTML result parsing (stdlib html.parser; tolerant by design)
# ---------------------------------------------------------------------------


def _decode_ddg_href(href: str) -> str:
    """Decode a DuckDuckGo result href into the target URL.

    DDG wraps result links in ``/l/?uddg=<urlencoded-target>&rut=…`` redirects
    (often protocol-relative, ``//duckduckgo.com/l/?uddg=…``). Direct hrefs are
    returned unchanged.
    """
    if not href:
        return ""
    candidate = href.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlparse(candidate)
    if parsed.path == "/l" or parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return target  # parse_qs already URL-decodes the value
    return href.strip()


class DDGResultParser(HTMLParser):
    """Tolerant parser for ``html.duckduckgo.com/html`` result pages.

    Result links are ``<a class="result__a" href=…>`` with the snippet in a
    sibling element carrying class ``result__snippet``. The parser extracts
    ``(title, url, snippet)`` triples and survives nested inline markup
    (``<b>``, ``<span>``, …) inside either element.

    The class names are attributes so the Lite-endpoint parser below can
    reuse the capture machinery against its own markup.
    """

    LINK_CLASS = "result__a"
    SNIPPET_CLASS = "result__snippet"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._capture: Optional[str] = None      # "title" | "snippet"
        self._capture_tag: Optional[str] = None  # tag that opened the capture
        self._depth = 0

    def _skip_element(self, tag: str, classes: List[str]) -> bool:
        """Hook: return True to ignore this start tag (no capture begins)."""
        return False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = dict(attrs)
        classes = (attr_map.get("class") or "").split()
        if self._capture:
            if tag == self._capture_tag:
                self._depth += 1
            return
        if self._skip_element(tag, classes):
            return
        if tag == "a" and self.LINK_CLASS in classes:
            self.results.append({
                "title": "",
                "url": _decode_ddg_href(attr_map.get("href") or ""),
                "snippet": "",
            })
            self._capture, self._capture_tag, self._depth = "title", tag, 1
        elif self.SNIPPET_CLASS in classes and self.results:
            self._capture, self._capture_tag, self._depth = "snippet", tag, 1

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag == self._capture_tag:
            self._depth -= 1
            if self._depth <= 0:
                self._capture = None
                self._capture_tag = None

    def handle_data(self, data: str) -> None:
        if self._capture and self.results:
            self.results[-1][self._capture] += data


class DDGLiteResultParser(DDGResultParser):
    """Tolerant parser for ``lite.duckduckgo.com/lite`` result pages.

    Results are table rows: ``<a class="result-link" href=…>`` carries the
    title (``uddg``-wrapped href like the HTML endpoint) and a following
    ``<td class="result-snippet">`` carries the snippet. Rows marked
    ``result-sponsored`` are ads whose "more info" anchor is also a
    ``result-link`` (pointing at a DuckDuckGo help page), so whole sponsored
    rows are skipped rather than surfacing "more info" as a result.
    """

    LINK_CLASS = "result-link"
    SNIPPET_CLASS = "result-snippet"

    def __init__(self) -> None:
        super().__init__()
        self._in_sponsored_row = False

    def _skip_element(self, tag: str, classes: List[str]) -> bool:
        if tag == "tr":
            self._in_sponsored_row = "result-sponsored" in classes
            return True
        return self._in_sponsored_row


def _parse_ddg_html(html_text: str, max_results: int,
                    parser_cls: type = DDGResultParser) -> List[Dict[str, str]]:
    """Parse a DDG HTML page into cleaned, de-duplicated result dicts."""
    parser = parser_cls()
    parser.feed(html_text)
    parser.close()
    seen: set = set()
    cleaned: List[Dict[str, str]] = []
    for raw in parser.results:
        url = raw["url"].strip()
        title = re.sub(r"\s+", " ", raw["title"]).strip()
        if not url or not title or url in seen:
            continue
        seen.add(url)
        cleaned.append({
            "title": title,
            "url": url,
            "snippet": re.sub(r"\s+", " ", raw["snippet"]).strip(),
        })
        if len(cleaned) >= max_results:
            break
    return cleaned


# ---------------------------------------------------------------------------
# Search backends (all egress through shared.external_http)
# ---------------------------------------------------------------------------


def _search_credentials(kwargs: Dict[str, Any]) -> Tuple[str, str]:
    """Return the optional (SEARCH_API_URL, SEARCH_API_KEY) bundle, if saved."""
    creds = kwargs.get("_credentials") or {}
    return (str(creds.get("SEARCH_API_URL") or ""), str(creds.get("SEARCH_API_KEY") or ""))


def _search_via_provider(query: str, max_results: int,
                         api_url: str, api_key: str) -> List[Dict[str, str]]:
    """POST a Tavily-compatible JSON search request to the configured provider."""
    url = external_http.normalize_url(api_url)
    resp = external_http.request(
        "POST", url,
        api_key=api_key,
        json_body={"query": query, "max_results": max_results},
        timeout=SEARCH_TIMEOUT_S,
        max_response_bytes=FETCH_MAX_BYTES,
    )
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    results: List[Dict[str, str]] = []
    for item in (payload.get("results") or [])[:max_results]:
        if not isinstance(item, dict):
            continue
        url_value = str(item.get("url") or "").strip()
        if not url_value:
            continue
        results.append({
            "title": str(item.get("title") or url_value).strip(),
            "url": url_value,
            "snippet": str(item.get("content") or "").strip(),
        })
    return results


def _ddg_get(url: str, query: str):
    """Egress-gated GET of one keyless DuckDuckGo endpoint (no redirects)."""
    return external_http.request(
        "GET", url,
        api_key="",
        params={"q": query},
        timeout=SEARCH_TIMEOUT_S,
        max_response_bytes=FETCH_MAX_BYTES,
        extra_headers={"User-Agent": USER_AGENT},
    )


def _is_ddg_challenge(status: int, html_text: str, results: List[Dict[str, str]]) -> bool:
    """True when a DDG response is the bot-challenge page, not a results page.

    HTTP 202 is the challenge (the HTML and Lite endpoints answer 200 for a
    real results page, empty or not). A 200 with zero result anchors is a
    challenge only when the body also carries the challenge markers — a
    well-formed empty page stays an honest "no results".
    """
    if status == 202:
        return True
    if results:
        return False
    lowered = (html_text or "").lower()
    return any(marker in lowered for marker in _DDG_CHALLENGE_MARKERS)


def _search_via_duckduckgo(query: str, max_results: int) -> Tuple[List[Dict[str, str]], str]:
    """Keyless DuckDuckGo search; returns (results, backend name).

    GETs the HTML endpoint first. When it answers with the bot challenge
    (a transient refusal of non-browser traffic, not an empty result set),
    retries ONCE against the Lite endpoint. If that is refused or fails too,
    raises :class:`DDGChallengeError` so the caller renders an actionable
    error — results are never fabricated (FR-012).
    """
    resp = _ddg_get(DDG_HTML_URL, query)
    results = _parse_ddg_html(resp.text, max_results)
    if not _is_ddg_challenge(resp.status_code, resp.text, results):
        return results, DDG_BACKEND

    logger.warning(
        "DuckDuckGo HTML endpoint answered its bot challenge (HTTP %s, %d bytes); "
        "retrying once via the Lite endpoint", resp.status_code, len(resp.content or b""))
    html_verdict = f"the HTML endpoint answered HTTP {resp.status_code} with a challenge page"
    try:
        lite = _ddg_get(DDG_LITE_URL, query)
    except ExternalHttpError as e:
        raise DDGChallengeError(
            f"DuckDuckGo's bot challenge blocked the keyless search: {html_verdict} "
            f"and the Lite endpoint retry failed ({e}). This is a transient refusal "
            "of non-browser traffic from this server's address, not an empty result "
            "set — retry shortly.") from e
    lite_results = _parse_ddg_html(lite.text, max_results, parser_cls=DDGLiteResultParser)
    if _is_ddg_challenge(lite.status_code, lite.text, lite_results):
        logger.warning("DuckDuckGo Lite endpoint also answered its bot challenge (HTTP %s)",
                       lite.status_code)
        raise DDGChallengeError(
            f"DuckDuckGo's bot challenge blocked the keyless search: {html_verdict} "
            f"and the Lite endpoint retry answered HTTP {lite.status_code} with a "
            "challenge page too. This is a transient refusal of non-browser traffic "
            "from this server's address, not an empty result set — retry shortly.")
    return lite_results, DDG_LITE_BACKEND


def _perform_search(query: str, max_results: int,
                    kwargs: Dict[str, Any]) -> Tuple[List[Dict[str, str]], str]:
    """Run the search on the preferred backend; returns (results, backend name)."""
    api_url, api_key = _search_credentials(kwargs)
    if api_url:
        return _search_via_provider(query, max_results, api_url, api_key), PROVIDER_BACKEND
    return _search_via_duckduckgo(query, max_results)


def _search_backend_name(kwargs: Dict[str, Any]) -> str:
    api_url, _ = _search_credentials(kwargs)
    return PROVIDER_BACKEND if api_url else DDG_BACKEND


def _search_failure_alert(backend: str, exc: Exception) -> Alert:
    """FR-012: actionable error naming the failed backend; never fabricate.

    A DuckDuckGo bot-challenge refusal gets its own title so the user can
    tell a transient upstream refusal apart from a misconfigured provider;
    the remedy (SEARCH_API_URL) is the same either way.
    """
    if isinstance(exc, DDGChallengeError):
        return Alert(
            variant="error",
            title="Search blocked by DuckDuckGo's bot challenge",
            message=f"{exc} No results were fabricated. {_CREDENTIAL_REMEDY}",
        )
    return Alert(
        variant="error",
        title="Search failed",
        message=(
            f"{backend} could not complete the search: {exc} "
            f"No results were fabricated. {_CREDENTIAL_REMEDY}"
        ),
    )


# ---------------------------------------------------------------------------
# Page fetching + readable-text extraction
# ---------------------------------------------------------------------------


def _fetch_url(url: str):
    """Egress-gated GET with bounded size/timeout and manual redirect follow.

    ``shared.external_http`` keeps ``allow_redirects=False`` so every hop is
    re-validated against the SSRF policy (a redirect into a private network is
    blocked just like a direct request).
    """
    current = external_http.normalize_url(url)
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        resp = external_http.request(
            "GET", current,
            api_key="",
            timeout=FETCH_TIMEOUT_S,
            max_response_bytes=FETCH_MAX_BYTES,
            extra_headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            headers = resp.headers or {}
            location = headers.get("Location") or headers.get("location")
            if not location:
                raise ServiceUnreachableError(
                    f"Redirect from {current} carried no Location header")
            current = external_http.normalize_url(urljoin(current + "/", location))
            continue
        return resp
    raise ServiceUnreachableError(f"Too many redirects while fetching {url}")


_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "head", "nav", "header",
    "footer", "aside", "svg", "iframe", "form", "select", "button",
})
_HEADING_TAGS = {f"h{i}": "#" * i for i in range(1, 7)}
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "br", "tr", "table", "ul", "ol",
    "blockquote", "pre", "main", "figure", "dd", "dt",
})


class PageTextExtractor(HTMLParser):
    """Extract readable text from HTML.

    - Skips script/style/nav/header/footer/aside and other chrome.
    - Keeps headings as markdown (``#``…``######``); list items as ``-``.
    - Collapses intra-block whitespace; blocks are joined by blank lines.
    - Captures the document ``<title>`` separately.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip: Dict[str, int] = {}
        self._attr_skip = 0
        self._prefix = ""
        self._buf: List[str] = []
        self._parts: List[str] = []

    @property
    def _skipping(self) -> bool:
        return self._attr_skip > 0 or any(depth > 0 for depth in self._skip.values())

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        prefix = self._prefix
        self._prefix = ""
        if text:
            self._parts.append(f"{prefix} {text}" if prefix else text)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "title" and not self.title:
            self._in_title = True
            return
        # Inside a subtree skipped by class/id/role: count depth on non-void
        # tags only (void tags have no end tag, so counting them never unwinds).
        if self._attr_skip > 0:
            if tag not in VOID_TAGS:
                self._attr_skip += 1
            return
        if tag in _SKIP_TAGS:
            self._skip[tag] = self._skip.get(tag, 0) + 1
            return
        if self._skipping:
            return
        if tag not in VOID_TAGS and should_skip_attrs(attrs):
            self._attr_skip = 1
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self._prefix = _HEADING_TAGS[tag]
        elif tag == "li":
            self._flush()
            self._prefix = "-"
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if self._attr_skip > 0:
            self._attr_skip = max(0, self._attr_skip - 1)
            return
        if tag in _SKIP_TAGS:
            if self._skip.get(tag, 0) > 0:
                self._skip[tag] -= 1
            return
        if self._skipping:
            return
        if tag in _HEADING_TAGS or tag == "li" or tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skipping or not data:
            return
        self._buf.append(data)

    def text(self) -> str:
        self._flush()
        return "\n\n".join(self._parts)


def _extract_readable(html_text: str) -> Tuple[str, str]:
    """Return (page title, readable markdown-ish text) for an HTML document."""
    parser = PageTextExtractor()
    parser.feed(html_text)
    parser.close()
    return re.sub(r"\s+", " ", parser.title).strip(), clean_page_text(parser.text())


def _looks_like_html(resp) -> bool:
    content_type = str((resp.headers or {}).get("Content-Type") or "").lower()
    if "html" in content_type:
        return True
    head = (resp.text or "")[:2048].lower()
    return "<html" in head or "<!doctype html" in head or "<body" in head


# ---------------------------------------------------------------------------
# Per-session LLM client resolution (mirrors agents/general/mcp_tools.py)
# ---------------------------------------------------------------------------


def _resolve_llm_client(kwargs: Dict[str, Any]) -> Tuple[Optional[OpenAI], str]:
    """Resolve the OpenAI-compatible client exactly like the general agent.

    Feature 054: the per-turn credentials the orchestrator injects
    (``_session_llm_credentials`` — the caller's persisted record, or the
    admin system record on system-context turns) are preferred, then the
    agent's own credential bundle. There is NO env fallback — the
    operator-default path was removed.
    """
    session_llm = kwargs.get("_session_llm_credentials") or {}
    creds = kwargs.get("_credentials", {}) or {}
    api_key = (
        session_llm.get("OPENAI_API_KEY")
        or (creds.get("OPENAI_API_KEY") if not kwargs.get("_credentials_encrypted") else None)
    )
    base_url = (
        session_llm.get("OPENAI_BASE_URL")
        or (creds.get("OPENAI_BASE_URL") if not kwargs.get("_credentials_encrypted") else None)
    )
    model = (
        session_llm.get("LLM_MODEL")
        or "gpt-4o"
    )
    if not api_key:
        return None, model
    return OpenAI(api_key=api_key, base_url=base_url), model


# ---------------------------------------------------------------------------
# Brief helpers
# ---------------------------------------------------------------------------


def _strip_out_of_range_citations(text: str, n_sources: int) -> str:
    """Remove ``[k]`` citation markers where k is outside 1..n (no fabrication)."""
    def _repl(match: "re.Match[str]") -> str:
        k = int(match.group(1))
        return match.group(0) if 1 <= k <= n_sources else ""
    return re.sub(r"\[(\d+)\]", _repl, text)


def _split_sections(markdown_text: str) -> List[Tuple[str, str]]:
    """Split a markdown brief into (heading, body) pairs on ``## `` headings."""
    sections: List[Tuple[str, str]] = []
    heading: Optional[str] = None
    lines: List[str] = []
    for line in markdown_text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line.strip())
        if match and not line.strip().startswith("###"):
            if heading is not None:
                sections.append((heading, "\n".join(lines).strip()))
            heading = match.group(1).strip()
            lines = []
        elif heading is not None:
            lines.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(lines).strip()))
    return sections


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _credentials_check(**kwargs) -> Dict[str, Any]:
    """Probe the optional search-provider bundle (invoked at credential save)."""
    api_url, api_key = _search_credentials(kwargs)
    if not api_url:
        return {
            "credential_test": "ok",
            "detail": ("No search provider configured; the keyless DuckDuckGo "
                       "path will be used."),
        }
    try:
        _search_via_provider("connectivity probe", 1, api_url, api_key)
    except AuthFailedError as e:
        return {"credential_test": "auth_failed", "detail": str(e)}
    except (ServiceUnreachableError, EgressBlockedError, RateLimitedError) as e:
        return {"credential_test": "unreachable", "detail": str(e)}
    except Exception as e:
        return {"credential_test": "unexpected", "detail": str(e)}
    return {"credential_test": "ok"}


def web_search(query: str = "", max_results: int = DEFAULT_MAX_RESULTS, **kwargs) -> Dict[str, Any]:
    """Search the web; prefer the configured provider, else keyless DuckDuckGo."""
    query = str(query or "").strip()
    if not query:
        return create_ui_response([
            Alert(variant="error", title="Search failed",
                  message="A non-empty 'query' is required."),
        ])
    try:
        n = int(max_results)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_RESULTS
    n = max(1, min(n, MAX_RESULTS_CAP))

    backend = _search_backend_name(kwargs)
    try:
        results, backend = _perform_search(query, n, kwargs)
    except ExternalHttpError as e:
        return create_ui_response([_search_failure_alert(backend, e)])
    except Exception as e:  # defensive: parser or payload surprises
        logger.error("web_search failed on %s: %s", backend, e)
        return create_ui_response([_search_failure_alert(backend, e)])

    if not results:
        return {
            "_ui_components": [Alert(
                variant="info",
                title=f"Search: {query}",
                message=f"No results found for '{query}'.",
            ).to_dict()],
            "_data": {"query": query, "backend": backend, "results": []},
        }

    items = [
        {"title": r["title"], "url": r["url"], "subtitle": r["snippet"]}
        for r in results
    ]
    card = Card(
        title=f"Search: {query}",
        content=[List_(variant="detailed", items=items)],
    )
    return {
        "_ui_components": [card.to_dict()],
        "_data": {"query": query, "backend": backend, "results": results},
    }


def fetch_page(url: str = "", **kwargs) -> Dict[str, Any]:
    """Fetch a page (egress-gated, 1 MB / 15 s) and extract readable text."""
    url = str(url or "").strip()
    if not url:
        return create_ui_response([
            Alert(variant="error", title="Fetch failed",
                  message="A non-empty 'url' is required."),
        ])
    try:
        resp = _fetch_url(url)
    except ResponseTooLargeError:
        return create_ui_response([
            Alert(variant="error", title="Page too large",
                  message=(f"The page at {url} exceeds the "
                           f"{FETCH_MAX_BYTES // (1024 * 1024)} MB fetch limit "
                           "and was not retrieved.")),
        ])
    except ExternalHttpError as e:
        return create_ui_response([
            Alert(variant="error", title="Fetch failed",
                  message=f"Could not fetch {url}: {e}"),
        ])

    if _looks_like_html(resp):
        title, text = _extract_readable(resp.text)
    else:
        title, text = "", (resp.text or "").strip()

    truncated = len(text) > PAGE_TEXT_CAP
    if truncated:
        text = text[:PAGE_TEXT_CAP]

    components: List[Any] = []
    if truncated:
        components.append(Alert(
            variant="info",
            title="Content truncated",
            message=(f"The extracted text was truncated to the first "
                     f"{PAGE_TEXT_CAP:,} characters."),
        ))
    components.append(Card(
        title=title or url,
        content=[
            Text(content=source_markdown(url), variant="markdown"),
            Text(content=text or "(no readable text found)", variant="markdown"),
        ],
    ))
    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {
            "url": url,
            "title": title,
            "truncated": truncated,
            "characters": len(text),
        },
    }


def research_brief(topic: str = "", depth: str = "standard", **kwargs) -> Dict[str, Any]:
    """Search, fetch the top sources, and synthesize one cited markdown brief."""
    topic = str(topic or "").strip()
    if not topic:
        return create_ui_response([
            Alert(variant="error", title="Research brief failed",
                  message="A non-empty 'topic' is required."),
        ])
    depth = str(depth or "standard").strip().lower()
    if depth not in BRIEF_FETCHES:
        depth = "standard"
    fetch_target = BRIEF_FETCHES[depth]

    backend = _search_backend_name(kwargs)
    try:
        results, backend = _perform_search(topic, DEFAULT_MAX_RESULTS, kwargs)
    except Exception as e:
        return create_ui_response([_search_failure_alert(backend, e)])
    if not results:
        return create_ui_response([
            Alert(variant="error", title="Research brief failed",
                  message=(f"{backend} returned no results for '{topic}'; no brief "
                           f"was generated (sources are never fabricated). "
                           f"{_CREDENTIAL_REMEDY}")),
        ])

    # Fetch up to `fetch_target` pages, skipping failures (each fetch
    # bounded) — and stop when the overall time budget is spent (030) so a
    # slow network yields a brief from fewer sources instead of a dispatch
    # timeout with nothing.
    deadline = time.monotonic() + BRIEF_TIME_BUDGET_S
    sources: List[Dict[str, str]] = []
    for result in results:
        if len(sources) >= fetch_target:
            break
        if time.monotonic() > deadline:
            logger.warning("research_brief: time budget spent after %d source(s) — "
                           "building brief from what was fetched", len(sources))
            break
        try:
            resp = _fetch_url(result["url"])
        except Exception as e:
            logger.warning("research_brief: skipping %s (%s)", result["url"], e)
            continue
        title, text = _extract_readable(resp.text) if _looks_like_html(resp) \
            else ("", (resp.text or "").strip())
        if not text:
            continue
        sources.append({
            "index": len(sources) + 1,
            "url": result["url"],
            "title": title or result["title"] or result["url"],
            "text": text[:BRIEF_SOURCE_CAP],
            "retrieved": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        })

    if not sources:
        return create_ui_response([
            Alert(variant="error", title="Research brief failed",
                  message=("None of the search results could be fetched, so no "
                           "brief was generated (the brief never cites a URL it "
                           "did not fetch).")),
        ])

    client, model = _resolve_llm_client(kwargs)
    if client is None:
        return create_ui_response([
            Alert(variant="error", title="LLM unavailable",
                  message=("No LLM credentials are configured, so the brief could "
                           "not be synthesized. Configure LLM settings and retry.")),
        ])

    source_block = "\n\n".join(
        f"[{s['index']}] {s['title']} ({s['url']})\n{s['text']}" for s in sources
    )
    system_prompt = (
        "You are a research analyst. Write a concise research brief on the given "
        "topic using ONLY the numbered sources provided. Cite claims with "
        f"bracketed source numbers like [1]..[{len(sources)}]. Never cite a number "
        "outside that range and never invent sources or URLs. Organize the brief "
        "into markdown sections, each starting with a '## ' heading."
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Topic: {topic}\n\nSources:\n\n{source_block}"},
            ],
            timeout=60,
        )
        brief = strip_reasoning_markup(response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error("research_brief synthesis failed: %s", e)
        return create_ui_response([
            Alert(variant="error", title="Synthesis failed",
                  message=f"The LLM synthesis call failed: {e}"),
        ])
    if not brief:
        return create_ui_response([
            Alert(variant="error", title="Synthesis failed",
                  message="The LLM returned an empty brief."),
        ])

    brief = _strip_out_of_range_citations(brief, len(sources))

    components: List[Any] = [
        Card(title=f"Research brief: {topic}",
             content=[Text(content=brief, variant="markdown")]),
        Table(
            headers=["#", "Source", "Title", "Retrieved"],
            rows=[[str(s["index"]), s["url"], s["title"], s["retrieved"]]
                  for s in sources],
        ),
    ]
    sections = _split_sections(brief)
    if len(sections) >= 2:
        components.append(Tabs(tabs=[
            TabItem(label=heading, content=[Text(content=body, variant="markdown")])
            for heading, body in sections
        ]))

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {
            "topic": topic,
            "depth": depth,
            "backend": backend,
            "sources": [{k: s[k] for k in ("index", "url", "title", "retrieved")}
                        for s in sources],
        },
    }


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "_credentials_check": {
        "function": _credentials_check,
        "description": ("Internal: probe the optional SEARCH_API_URL + SEARCH_API_KEY "
                        "bundle with a one-result search."),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
        "scope": "tools:read",
    },
    "web_search": {
        "function": web_search,
        "description": (
            "Search the web for a query. Uses the optional configured search "
            "provider (SEARCH_API_URL, Tavily-compatible) when present, otherwise "
            "the keyless DuckDuckGo HTML endpoint. Returns result titles, URLs, "
            "and snippets — never fabricated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (required)."},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default 8, max 20).",
                    "default": DEFAULT_MAX_RESULTS,
                    "minimum": 1,
                    "maximum": MAX_RESULTS_CAP,
                },
            },
            "required": ["query"],
        },
        "scope": "tools:search",
    },
    "fetch_page": {
        "function": fetch_page,
        "description": (
            "Fetch a web page through the egress-gated HTTP layer (1 MB cap, 15 s "
            "timeout) and extract its readable text as markdown (headings kept, "
            "scripts/styles/navigation stripped). Long pages are truncated with "
            "an explicit notice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL of the page to fetch (required)."},
            },
            "required": ["url"],
        },
        "scope": "tools:read",
    },
    "research_brief": {
        "function": research_brief,
        "description": (
            "Research a topic end-to-end: search the web, fetch the top sources "
            "(shallow=2, standard=5 pages), and synthesize a cited markdown brief. "
            "Citations [1]..[n] refer only to sources that were actually fetched; "
            "a sources table lists every cited URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic to research (required)."},
                "depth": {
                    "type": "string",
                    "enum": ["shallow", "standard"],
                    "default": "standard",
                    "description": "How many sources to fetch: shallow=2, standard=5.",
                },
            },
            "required": ["topic"],
        },
        "scope": "tools:search",
    },
}
