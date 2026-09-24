#!/usr/bin/env python3
"""Web search (DuckDuckGo or a configured provider), egress-gated page fetches, and
research_brief's cite-only-fetched-sources synthesis, all HTTP routed through
shared/external_http.py.
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

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FETCH_MAX_BYTES = 1024 * 1024
FETCH_TIMEOUT_S = 15
FETCH_TOTAL_TIMEOUT_S = 30
SEARCH_TIMEOUT_S = 15
DEFAULT_MAX_RESULTS = 8
MAX_RESULTS_CAP = 20
PAGE_TEXT_CAP = 20_000
BRIEF_SOURCE_CAP = 4_000
BRIEF_FETCHES = {"shallow": 2, "standard": 5}
BRIEF_TIME_BUDGET_S = 110
MAX_REDIRECT_HOPS = 3

DDG_BACKEND = "DuckDuckGo HTML search"
DDG_LITE_BACKEND = "DuckDuckGo Lite search"
PROVIDER_BACKEND = "the configured search provider (SEARCH_API_URL)"

_CREDENTIAL_REMEDY = (
    "Add a search provider API key in agent settings for reliable/higher-limit search."
)

# DDG's 202 challenge page has no anchors — reads as empty, not blocked
_DDG_CHALLENGE_MARKERS = (
    "anomaly-modal",
    "js-anomaly",
    "challenge-form",
    "bots use duckduckgo",
)


class DDGChallengeError(ServiceUnreachableError):
    pass


def _decode_ddg_href(href: str) -> str:
    if not href:
        return ""
    candidate = href.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlparse(candidate)
    if parsed.path == "/l" or parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return target
    return href.strip()


class DDGResultParser(HTMLParser):
    LINK_CLASS = "result__a"
    SNIPPET_CLASS = "result__snippet"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._capture: Optional[str] = None
        self._capture_tag: Optional[str] = None
        self._depth = 0

    def _skip_element(self, tag: str, classes: List[str]) -> bool:
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


def _search_credentials(kwargs: Dict[str, Any]) -> Tuple[str, str]:
    creds = kwargs.get("_credentials") or {}
    return (str(creds.get("SEARCH_API_URL") or ""), str(creds.get("SEARCH_API_KEY") or ""))


def _search_via_provider(query: str, max_results: int,
                         api_url: str, api_key: str) -> List[Dict[str, str]]:
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
        raise ServiceUnreachableError("invalid_search_provider_response") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ServiceUnreachableError("invalid_search_provider_response")
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
    return external_http.request(
        "GET", url,
        api_key="",
        params={"q": query},
        timeout=SEARCH_TIMEOUT_S,
        max_response_bytes=FETCH_MAX_BYTES,
        extra_headers={"User-Agent": USER_AGENT},
    )


def _is_ddg_challenge(status: int, html_text: str, results: List[Dict[str, str]]) -> bool:
    if status == 202:
        return True
    if results:
        return False
    lowered = (html_text or "").lower()
    return any(marker in lowered for marker in _DDG_CHALLENGE_MARKERS)


def _search_via_duckduckgo(query: str, max_results: int) -> Tuple[List[Dict[str, str]], str]:
    resp = _ddg_get(DDG_HTML_URL, query)
    results = _parse_ddg_html(resp.text, max_results)
    if not _is_ddg_challenge(resp.status_code, resp.text, results):
        return results, DDG_BACKEND

    logger.warning("keyless_search_blocked status=%s bytes=%d",
                   resp.status_code, len(resp.content or b""))
    raise DDGChallengeError("keyless_search_blocked")


def _perform_search(query: str, max_results: int,
                    kwargs: Dict[str, Any]) -> Tuple[List[Dict[str, str]], str]:
    api_url, api_key = _search_credentials(kwargs)
    if api_url:
        return _search_via_provider(query, max_results, api_url, api_key), PROVIDER_BACKEND
    return _search_via_duckduckgo(query, max_results)


def _search_backend_name(kwargs: Dict[str, Any]) -> str:
    api_url, _ = _search_credentials(kwargs)
    return PROVIDER_BACKEND if api_url else DDG_BACKEND


def _search_failure_alert(backend: str, exc: Exception) -> Alert:
    logger.warning("search_failed backend=%s error_type=%s status=%s",
                   backend, type(exc).__name__, _http_status(exc))
    if isinstance(exc, DDGChallengeError):
        message = f"Keyless search is blocked. {_CREDENTIAL_REMEDY}"
    elif isinstance(exc, EgressBlockedError):
        message = "The search provider address is blocked by network policy. Check its URL in agent settings."
    elif isinstance(exc, AuthFailedError) and backend == PROVIDER_BACKEND:
        message = "The search provider rejected its credentials. Check the API key in agent settings."
    elif _http_status(exc) == 429:
        message = "The search provider's rate limit was reached. Try later or check your provider plan."
    elif backend == PROVIDER_BACKEND:
        message = "The search provider is unavailable. Try later or check the provider URL and API key in agent settings."
    else:
        message = f"Keyless search is unavailable. {_CREDENTIAL_REMEDY}"
    return Alert(variant="error", title="Search unavailable", message=message)


def _http_status(exc: Exception) -> Optional[int]:
    match = re.match(
        r"^(?:Authentication failed \(|Rate-limited by upstream \(|"
        r"Upstream server error \(|Upstream returned )(\d{3})(?:\)|:)", str(exc),
    )
    return int(match[1]) if match else None


def _tool_failure(alert: Alert, code: str) -> Dict[str, Any]:
    return {
        **create_ui_response([alert]),
        "_error": {"code": code, "message": alert.message, "retryable": False},
    }


def _search_failure(backend: str, exc: Exception) -> Dict[str, Any]:
    code = "SEARCH_UNAVAILABLE"
    if isinstance(exc, DDGChallengeError):
        code = "SEARCH_BLOCKED"
    elif isinstance(exc, EgressBlockedError):
        code = "SEARCH_EGRESS_BLOCKED"
    elif isinstance(exc, AuthFailedError) and backend == PROVIDER_BACKEND:
        code = "SEARCH_AUTH_FAILED"
    elif _http_status(exc) == 429:
        code = "SEARCH_RATE_LIMITED"
    return _tool_failure(_search_failure_alert(backend, exc), code)


def _fetch_failure_message(exc: Exception) -> str:
    if isinstance(exc, EgressBlockedError):
        return "This page is blocked by network policy. Choose another source."
    if isinstance(exc, AuthFailedError):
        return "This page requires access that is unavailable here. Choose a public source."
    if _http_status(exc) == 404:
        return "This page was not found. Check the link or choose another source."
    return "This page could not be retrieved. Try later or choose another source."


# Redirects are followed manually so each hop hits the SSRF gate
def _fetch_url(url: str):
    current = external_http.normalize_url(url, preserve_trailing_slash=True)
    deadline = time.monotonic() + FETCH_TOTAL_TIMEOUT_S
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceUnreachableError("Page fetch exceeded its timeout budget")
        resp = external_http.request(
            "GET", current,
            api_key="",
            timeout=min(FETCH_TIMEOUT_S, remaining),
            max_response_bytes=FETCH_MAX_BYTES,
            extra_headers={"User-Agent": USER_AGENT},
        )
        if time.monotonic() >= deadline:
            raise ServiceUnreachableError("Page fetch exceeded its timeout budget")
        if resp.status_code in (301, 302, 303, 307, 308):
            headers = resp.headers or {}
            location = headers.get("Location") or headers.get("location")
            if not location:
                raise ServiceUnreachableError(
                    f"Redirect from {current} carried no Location header")
            current = external_http.normalize_url(
                urljoin(current, location), preserve_trailing_slash=True,
            )
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


def _resolve_llm_client(kwargs: Dict[str, Any]) -> Tuple[Optional[OpenAI], str]:
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


def _strip_out_of_range_citations(text: str, n_sources: int) -> str:
    def _repl(match: "re.Match[str]") -> str:
        k = int(match.group(1))
        return match.group(0) if 1 <= k <= n_sources else ""
    return re.sub(r"\[(\d+)\]", _repl, text)


def _split_sections(markdown_text: str) -> List[Tuple[str, str]]:
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


def _credentials_check(**kwargs) -> Dict[str, Any]:
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
        return {"credential_test": "auth_failed", "detail": _search_failure_alert(PROVIDER_BACKEND, e).message}
    except (ServiceUnreachableError, EgressBlockedError, RateLimitedError) as e:
        return {"credential_test": "unreachable", "detail": _search_failure_alert(PROVIDER_BACKEND, e).message}
    except Exception as e:
        return {"credential_test": "unexpected", "detail": _search_failure_alert(PROVIDER_BACKEND, e).message}
    return {"credential_test": "ok"}


def web_search(query: str = "", max_results: int = DEFAULT_MAX_RESULTS, **kwargs) -> Dict[str, Any]:
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
        return _search_failure(backend, e)
    except Exception as e:
        return _search_failure(backend, e)

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
    url = str(url or "").strip()
    if not url:
        return create_ui_response([
            Alert(variant="error", title="Fetch failed",
                  message="A non-empty 'url' is required."),
        ])
    try:
        resp = _fetch_url(url)
    except ResponseTooLargeError:
        return _tool_failure(
            Alert(variant="error", title="Page too large",
                  message=f"This page exceeds the {FETCH_MAX_BYTES // (1024 * 1024)} MB limit. Choose a smaller source."),
            "UPSTREAM_TOO_LARGE",
        )
    except ExternalHttpError as e:
        logger.warning("page_fetch_failed error_type=%s status=%s", type(e).__name__, _http_status(e))
        code = "UPSTREAM_UNAVAILABLE"
        if isinstance(e, EgressBlockedError):
            code = "UPSTREAM_BLOCKED"
        elif isinstance(e, AuthFailedError):
            code = "UPSTREAM_ACCESS_DENIED"
        elif _http_status(e) == 404:
            code = "UPSTREAM_NOT_FOUND"
        return _tool_failure(
            Alert(variant="error", title="Fetch failed",
                  message=_fetch_failure_message(e)),
            code,
        )

    retrieved_at = datetime.now(timezone.utc).isoformat()
    is_html = _looks_like_html(resp)
    if is_html:
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
            "page_observation": {
                "version": 1,
                "requested_url": url,
                "final_url": getattr(resp, "url", None),
                "retrieved_at": retrieved_at,
                "media_type": str((resp.headers or {}).get("Content-Type", ""))
                    .split(";", 1)[0].strip().lower(),
                "extraction_profile": "html_readable_v1" if is_html else "plain_text_v1",
                "title": title,
                "text": text,
                "body_complete": getattr(resp, "status_code", None) == 200
                    and not (resp.headers or {}).get("Content-Range"),
                "extraction_complete": True,
                "excerpt_complete": not truncated,
                "redacted": False,
            },
        },
    }


def research_brief(topic: str = "", depth: str = "standard", **kwargs) -> Dict[str, Any]:
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
        return _search_failure(backend, e)
    if not results:
        return create_ui_response([
            Alert(variant="error", title="Research brief failed",
                  message=(f"{backend} returned no results for '{topic}'; no brief "
                           f"was generated (sources are never fabricated). "
                           f"{_CREDENTIAL_REMEDY}")),
        ])

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
            logger.warning("research_source_skipped error_type=%s", type(e).__name__)
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
        logger.warning("research_synthesis_failed error_type=%s", type(e).__name__)
        return _tool_failure(
            Alert(variant="error", title="Synthesis failed",
                  message="The research summary could not be generated. Try again or check your LLM settings."),
            "RESEARCH_SUMMARY_UNAVAILABLE",
        )
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
