#!/usr/bin/env python3
"""
MCP Tools for the Summarizer agent — tool functions that return UI Primitives.

Includes:
- summarize_text: structured TL;DR / key points / notable quotes Tabs
- summarize_url: egress-gated fetch (1 MB / 15 s) then the summarize_text path
- compare_documents: side-by-side summary Grid plus a key-differences Table

LLM access uses the per-session OpenAI-compatible client pattern (same
credential resolution as the general agent). All outbound HTTP goes through
``shared.external_http``. Inputs are capped at ``INPUT_CAP`` characters with
an explicit truncation notice when the cap applies. The small HTML→text
extraction helper is intentionally duplicated from the web_research agent —
agent packages never cross-import each other.
"""
import json
import logging
import os
import re
import sys
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from openai import OpenAI  # noqa: E402

from astralprims import (  # noqa: E402
    Alert, Card, Grid, List_, TabItem, Table, Tabs, Text, create_ui_response,
)
from shared import external_http  # noqa: E402
from shared.external_http import (  # noqa: E402
    AuthFailedError,
    EgressBlockedError,
    ExternalHttpError,
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
# Bounds (FR-014: explicit truncation notices when inputs exceed limits)
# ---------------------------------------------------------------------------

INPUT_CAP = 24_000              # chars of input text per document
FETCH_MAX_BYTES = 1024 * 1024   # 1 MB hard cap per fetch
FETCH_TIMEOUT_S = 15
MAX_REDIRECT_HOPS = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_LABELS = ("Document A", "Document B")


class LlmUnavailableError(Exception):
    """No LLM credentials could be resolved for this call."""


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
# Defensive JSON parsing of LLM output
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Remove a surrounding markdown code fence (```json … ```), if present."""
    stripped = (text or "").strip()
    match = re.match(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def _parse_llm_json(raw: str) -> Optional[Dict[str, Any]]:
    """Parse LLM output into a dict: fence-strip + json.loads, then a regex
    fallback that extracts the first ``{…}`` block. Returns None on failure."""
    cleaned = _strip_fences(strip_reasoning_markup(raw or "").strip())
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return None


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_summary(parsed: Dict[str, Any], raw_fallback: str = "") -> Dict[str, Any]:
    """Coerce a parsed summary payload into {tldr: str, key_points: [], quotes: []}."""
    tldr = str(parsed.get("tldr") or "").strip()
    return {
        "tldr": tldr or raw_fallback or "(no summary returned)",
        "key_points": _as_str_list(parsed.get("key_points")),
        "quotes": _as_str_list(parsed.get("quotes")),
    }


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------


def _call_summary_llm(text: str, focus: Optional[str],
                      kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """ONE LLM call returning strict JSON {tldr, key_points, quotes}."""
    client, model = _resolve_llm_client(kwargs)
    if client is None:
        raise LlmUnavailableError(
            "No LLM credentials are configured, so the text could not be "
            "summarized. Configure LLM settings and retry."
        )
    focus_clause = f" Focus especially on: {focus.strip()}." if focus and str(focus).strip() else ""
    system_prompt = (
        "You are an expert summarizer. Respond with STRICT JSON only — no prose, "
        "no code fences — with exactly these keys: "
        '{"tldr": str, "key_points": [str], "quotes": [str]}. '
        '"tldr" is a 2-4 sentence summary, "key_points" lists the main '
        'takeaways, and "quotes" lists notable verbatim quotes from the text '
        "(may be empty)."
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Summarize the following text.{focus_clause}\n\n{text}"},
        ],
        timeout=60,
    )
    raw = response.choices[0].message.content or ""
    parsed = _parse_llm_json(raw)
    if parsed is None:
        # Malformed output: fall back to using the cleaned text as the TL;DR.
        cleaned = _strip_fences(strip_reasoning_markup(raw).strip())
        return _normalize_summary({}, raw_fallback=cleaned)
    return _normalize_summary(parsed)


def _call_comparison_llm(doc_a: Tuple[str, str], doc_b: Tuple[str, str],
                         kwargs: Dict[str, Any]) -> List[Dict[str, str]]:
    """ONE LLM call returning strict JSON {differences: [{aspect, a, b}]}."""
    client, model = _resolve_llm_client(kwargs)
    if client is None:
        raise LlmUnavailableError(
            "No LLM credentials are configured, so the documents could not be "
            "compared. Configure LLM settings and retry."
        )
    label_a, text_a = doc_a
    label_b, text_b = doc_b
    system_prompt = (
        "You compare two documents. Respond with STRICT JSON only — no prose, "
        "no code fences — with exactly this shape: "
        '{"differences": [{"aspect": str, "a": str, "b": str}]}. '
        'Each entry names an "aspect" on which the documents differ, with "a" '
        "describing the first document's position and \"b\" the second's."
    )
    user_prompt = (
        f"Document A ({label_a}):\n{text_a}\n\n"
        f"Document B ({label_b}):\n{text_b}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        timeout=60,
    )
    raw = response.choices[0].message.content or ""
    parsed = _parse_llm_json(raw) or {}
    differences: List[Dict[str, str]] = []
    for item in parsed.get("differences") or []:
        if not isinstance(item, dict):
            continue
        aspect = str(item.get("aspect") or "").strip()
        if not aspect:
            continue
        differences.append({
            "aspect": aspect,
            "a": str(item.get("a") or "").strip(),
            "b": str(item.get("b") or "").strip(),
        })
    return differences


# ---------------------------------------------------------------------------
# Egress-gated fetch + small HTML→text extraction helper (deliberately
# duplicated from web_research: agent packages do not cross-import)
# ---------------------------------------------------------------------------


def _fetch_url(url: str):
    """Egress-gated GET with bounded size/timeout and manual redirect follow."""
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
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "br", "li", "tr", "table", "ul", "ol",
    "blockquote", "pre", "main", "figure", "h1", "h2", "h3", "h4", "h5", "h6",
})


class _HtmlTextExtractor(HTMLParser):
    """Compact readable-text extractor: skips page chrome, keeps paragraphs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip: Dict[str, int] = {}
        self._attr_skip = 0
        self._buf: List[str] = []
        self._parts: List[str] = []

    @property
    def _skipping(self) -> bool:
        return self._attr_skip > 0 or any(depth > 0 for depth in self._skip.values())

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if text:
            self._parts.append(text)

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
        if tag in _BLOCK_TAGS:
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
        if tag in _BLOCK_TAGS:
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


def _extract_text(resp) -> Tuple[str, str]:
    """Return (title, readable text) for a fetched response."""
    body = resp.text or ""
    content_type = str((resp.headers or {}).get("Content-Type") or "").lower()
    head = body[:2048].lower()
    looks_html = ("html" in content_type or "<html" in head
                  or "<!doctype html" in head or "<body" in head)
    if not looks_html:
        return "", body.strip()
    parser = _HtmlTextExtractor()
    parser.feed(body)
    parser.close()
    return re.sub(r"\s+", " ", parser.title).strip(), clean_page_text(parser.text())


# ---------------------------------------------------------------------------
# Component builders
# ---------------------------------------------------------------------------


def _truncation_alert(label: str, original_length: int) -> Alert:
    return Alert(
        variant="info",
        title="Input truncated",
        message=(f"{label} was truncated from {original_length:,} characters to "
                 f"the first {INPUT_CAP:,} before summarization."),
    )


def _summary_tabs(summary: Dict[str, Any]) -> Tabs:
    return Tabs(tabs=[
        TabItem(label="TL;DR",
                content=[Text(content=summary["tldr"], variant="markdown")]),
        TabItem(label="Key points",
                content=[List_(items=summary["key_points"] or ["(none identified)"])]),
        TabItem(label="Notable quotes",
                content=[List_(items=summary["quotes"] or ["(none identified)"])]),
    ])


def _summary_card(label: str, summary: Dict[str, Any]) -> Card:
    return Card(
        title=label,
        content=[
            Text(content=summary["tldr"], variant="markdown"),
            List_(items=summary["key_points"] or ["(none identified)"]),
        ],
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _tool_failure(code: str, message: str, title: str) -> Dict[str, Any]:
    """Return a terminal, fixed public error without upstream response details."""
    return {
        **create_ui_response([Alert(variant="error", title=title, message=message)]),
        "_error": {"code": code, "message": message, "retryable": False},
    }


def summarize_text(text: str = "", focus: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Summarize text into TL;DR / key points / notable quotes Tabs."""
    text = str(text or "")
    if not text.strip():
        return create_ui_response([
            Alert(variant="error", title="Summarization failed",
                  message="A non-empty 'text' is required."),
        ])
    original_length = len(text)
    capped = original_length > INPUT_CAP
    if capped:
        text = text[:INPUT_CAP]

    try:
        summary = _call_summary_llm(text, focus, kwargs)
    except LlmUnavailableError as e:
        return create_ui_response([
            Alert(variant="error", title="LLM unavailable", message=str(e)),
        ])
    except Exception as e:
        logger.warning("summary_failed error_type=%s", type(e).__name__)
        return _tool_failure(
            "RESEARCH_SUMMARY_UNAVAILABLE",
            "The summary could not be generated. Try again or check your LLM settings.",
            "Summarization failed",
        )

    components: List[Any] = []
    if capped:
        components.append(_truncation_alert("The input text", original_length))
    components.append(_summary_tabs(summary))
    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {
            "summary": summary,
            "truncated": capped,
            "input_characters": original_length,
            "focus": focus,
        },
    }


def _fetch_via_peer(url: str, kwargs: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Fetch a page by a MEDIATED hop to web_research's fetch_page (056 US1).

    web_research owns the product's page-retrieval capability (egress policy,
    redirect re-validation, readability extraction). Rather than maintaining a
    second copy of it, the summarizer asks for that capability by name — the
    orchestrator mediates the hop under a strictly-narrower child delegation
    and the same gate stack a direct call would face.

    Returns ``(title, text)`` on success, or ``None`` when chaining is
    unavailable or the hop is refused — the caller then falls back to its own
    local fetch, so behavior with ``FF_RECURSIVE_DELEGATION`` off (or for a
    user without web_research permission) is exactly what it is today.
    """
    import asyncio

    runtime = kwargs.get("_runtime")
    if runtime is None or not hasattr(runtime, "call_agent_tool"):
        return None
    try:
        # Tools run in a worker thread (mcp_server dispatches via to_thread);
        # bridge to the agent's event loop the same way long-running jobs do.
        future = asyncio.run_coroutine_threadsafe(
            runtime.call_agent_tool(
                "web-research-1", "fetch_page", {"url": url}, timeout=30.0),
            runtime.loop)
        resp = future.result(timeout=35)
    except Exception as e:
        logger.info("peer_fetch_unavailable error_type=%s; using local fetch", type(e).__name__)
        return None
    if resp is None or getattr(resp, "error", None):
        logger.info("peer_fetch_refused; using local fetch")
        return None
    data = resp.result if isinstance(resp.result, dict) else {}
    text = ""
    for comp in (resp.ui_components or []):
        # fetch_page returns a Card whose second Text child is the page text.
        if isinstance(comp, dict) and comp.get("type") == "card":
            children = comp.get("content") or []
            texts = [c.get("content", "") for c in children
                     if isinstance(c, dict) and c.get("type") == "text"]
            if len(texts) >= 2:
                text = texts[1]
                break
    if not text.strip():
        return None
    return str(data.get("title") or ""), text


def summarize_url(url: str = "", **kwargs) -> Dict[str, Any]:
    """Fetch a URL (egress-gated, 1 MB / 15 s) and summarize its readable text.

    056 US1: the fetch is delegated to web_research's ``fetch_page`` through an
    orchestrator-mediated hop when chaining is available, so the product has
    ONE page-retrieval capability rather than two. Falls back to the local
    fetch when the hop is unavailable or refused (fail-open — flag-off behavior
    is unchanged).
    """
    url = str(url or "").strip()
    if not url:
        return create_ui_response([
            Alert(variant="error", title="Summarization failed",
                  message="A non-empty 'url' is required."),
        ])

    hopped = _fetch_via_peer(url, kwargs)
    if hopped is not None:
        title, text = hopped
        return _summarize_fetched(url, title, text, kwargs)

    try:
        resp = _fetch_url(url)
    except ResponseTooLargeError:
        return _tool_failure(
            "UPSTREAM_TOO_LARGE",
            f"This page exceeds the {FETCH_MAX_BYTES // (1024 * 1024)} MB limit. Choose a smaller source.",
            "Page too large",
        )
    except ExternalHttpError as e:
        code = "UPSTREAM_UNAVAILABLE"
        message = "This page could not be retrieved. Try later or choose another source."
        if isinstance(e, EgressBlockedError):
            code, message = "UPSTREAM_BLOCKED", "This page is blocked by network policy. Choose another source."
        elif isinstance(e, AuthFailedError):
            code, message = "UPSTREAM_ACCESS_DENIED", "This page requires access that is unavailable here. Choose a public source."
        elif str(e).startswith("Upstream returned 404:"):
            code, message = "UPSTREAM_NOT_FOUND", "This page was not found. Check the link or choose another source."
        logger.warning("summary_page_fetch_failed code=%s error_type=%s", code, type(e).__name__)
        return _tool_failure(code, message, "Fetch failed")

    title, text = _extract_text(resp)
    return _summarize_fetched(url, title, text, kwargs)


def _summarize_fetched(url: str, title: str, text: str,
                       kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize page text retrieved for ``url`` (by hop or local fetch)."""
    if not text.strip():
        return create_ui_response([
            Alert(variant="error", title="Summarization failed",
                  message=f"No readable text was found at {url}."),
        ])

    forward = {k: v for k, v in kwargs.items() if k != "text"}
    result = summarize_text(text=text, **forward)
    comps = result.get("_ui_components")
    if isinstance(comps, list):
        comps.insert(0, Text(content=source_markdown(url), variant="markdown").to_dict())
    if isinstance(result.get("_data"), dict):
        result["_data"].update({"url": url, "title": title})
    return result


def compare_documents(text_a: str = "", text_b: str = "",
                      labels: Optional[List[str]] = None, **kwargs) -> Dict[str, Any]:
    """Compare two documents: side-by-side summaries + a key-differences table."""
    text_a = str(text_a or "")
    text_b = str(text_b or "")
    if not text_a.strip() or not text_b.strip():
        return create_ui_response([
            Alert(variant="error", title="Comparison failed",
                  message="Both 'text_a' and 'text_b' are required and must be non-empty."),
        ])

    label_a, label_b = DEFAULT_LABELS
    if isinstance(labels, (list, tuple)):
        if len(labels) >= 1 and str(labels[0]).strip():
            label_a = str(labels[0]).strip()
        if len(labels) >= 2 and str(labels[1]).strip():
            label_b = str(labels[1]).strip()

    notices: List[Any] = []
    docs: List[Tuple[str, str]] = []
    for label, text in ((label_a, text_a), (label_b, text_b)):
        original_length = len(text)
        if original_length > INPUT_CAP:
            notices.append(_truncation_alert(label, original_length))
            text = text[:INPUT_CAP]
        docs.append((label, text))

    try:
        # Two summary calls + ONE comparison call.
        summary_a = _call_summary_llm(docs[0][1], None, kwargs)
        summary_b = _call_summary_llm(docs[1][1], None, kwargs)
        differences = _call_comparison_llm(docs[0], docs[1], kwargs)
    except LlmUnavailableError as e:
        return create_ui_response([
            Alert(variant="error", title="LLM unavailable", message=str(e)),
        ])
    except Exception as e:
        logger.warning("document_comparison_failed error_type=%s", type(e).__name__)
        return _tool_failure(
            "RESEARCH_SUMMARY_UNAVAILABLE",
            "The documents could not be compared. Try again or check your LLM settings.",
            "Comparison failed",
        )

    rows = [[d["aspect"], d["a"], d["b"]] for d in differences]
    if not rows:
        rows = [["(no notable differences identified)", "—", "—"]]

    components: List[Any] = list(notices)
    components.append(Grid(columns=2, children=[
        _summary_card(label_a, summary_a),
        _summary_card(label_b, summary_b),
    ]))
    components.append(Table(headers=["Aspect", label_a, label_b], rows=rows))

    return {
        "_ui_components": [c.to_dict() for c in components],
        "_data": {
            "labels": [label_a, label_b],
            "summaries": {label_a: summary_a, label_b: summary_b},
            "differences": differences,
        },
    }


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "summarize_text": {
        "function": summarize_text,
        "description": (
            "Summarize provided text into a structured multi-section result: "
            "TL;DR, key points, and notable quotes (rendered as tabs). Inputs "
            "over 24,000 characters are truncated with an explicit notice. "
            "Optionally focus the summary on a particular aspect."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to summarize (required)."},
                "focus": {
                    "type": "string",
                    "description": "Optional aspect to focus the summary on.",
                },
            },
            "required": ["text"],
        },
        "scope": "tools:read",
    },
    "summarize_url": {
        "function": summarize_url,
        "description": (
            "Fetch a web page through the egress-gated HTTP layer (1 MB cap, "
            "15 s timeout), extract its readable text, and summarize it into "
            "TL;DR / key points / notable quotes tabs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL of the page to summarize (required)."},
            },
            "required": ["url"],
        },
        "scope": "tools:read",
    },
    "compare_documents": {
        "function": compare_documents,
        "description": (
            "Compare two documents: per-document summary cards side by side "
            "plus a table of key differences by aspect. Optional 'labels' "
            "names the two documents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text_a": {"type": "string", "description": "First document text (required)."},
                "text_b": {"type": "string", "description": "Second document text (required)."},
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "Optional display labels for the two documents.",
                },
            },
            "required": ["text_a", "text_b"],
        },
        "scope": "tools:read",
    },
}
