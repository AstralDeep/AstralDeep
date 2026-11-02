"""Validates and sanitizes model-authored HTML pages and builds the html_artifact component that
agents/artifacts publishes and versions through the ordinary render path via artifact_versions.py.
Pages keep inline scripts, load resources only from the CDN allowlist and always carry the injected CSP.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from shared.feature_flags import flags
from shared.protocol import MCPResponse

logger = logging.getLogger("orchestrator.artifact_html")

AGENT_ID = "artifacts-1"
PUBLISH_TOOL = "publish_artifact"
UPDATE_TOOL = "update_artifact"
HTML_TYPE = "html_artifact"

KINDS = ("page", "app", "visualization")
DEFAULT_KIND = "page"
MAX_TITLE_CHARS = 120
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_WIRE_BYTES = 640 * 1024
MAX_ERRORS = 20
ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{16}$")

CDN_HOSTS = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "esm.sh")
FONT_CSS_HOST = "fonts.googleapis.com"
FONT_FILE_HOST = "fonts.gstatic.com"
ALLOWED_HOSTS = CDN_HOSTS + (FONT_CSS_HOST, FONT_FILE_HOST)
_STYLE_HOSTS = CDN_HOSTS + (FONT_CSS_HOST,)
_FONT_HOSTS = CDN_HOSTS + (FONT_FILE_HOST,)


def _sources(hosts: Tuple[str, ...]) -> str:
    return " ".join(f"https://{host}" for host in hosts)


CSP = (
    "default-src 'none'; "
    f"script-src 'unsafe-inline' {_sources(CDN_HOSTS)}; "
    f"style-src 'unsafe-inline' {_sources(_STYLE_HOSTS)}; "
    f"img-src data: blob: {_sources(CDN_HOSTS)}; "
    f"font-src data: {_sources(_FONT_HOSTS)}; "
    f"media-src data: blob: {_sources(CDN_HOSTS)}; "
    "connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"
)
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
VIEWPORT_META = '<meta name="viewport" content="width=device-width, initial-scale=1">'

_DROP_VOID = frozenset({"base", "embed"})
_DROP_TREE = frozenset({"object", "iframe", "frame", "frameset"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "frame", "hr", "img", "input", "link",
                   "meta", "param", "source", "track", "wbr"})
_RESOURCE_ATTRS = {
    "script": ("src", "href", "xlink:href"),
    "link": ("href",),
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "font": ("src",),
}
_TAG_HOSTS = {"script": CDN_HOSTS, "font": _FONT_HOSTS}
_CSS_URL = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.I | re.S)
_CSS_IMPORT = re.compile(r"""@import\s+(['"])(.*?)\1""", re.I | re.S)
_SLOT = object()

SYSTEM_PROMPT_ADDENDUM = (
    "\n\n## Artifacts\n"
    "When the deliverable is a page, an app, a visualization or an interactive tool, build it as one "
    "self-contained HTML document and call `publish_artifact`; revise it with `update_artifact` and the "
    "returned artifact_id. Do not publish ordinary answers, explanations or summaries.\n"
    "Design rules:\n"
    "- Title: 2-4 words, a name rather than a sentence.\n"
    "- Colors: define every color as a CSS custom property on `:root`; redefine them for dark mode under "
    "`@media (prefers-color-scheme: dark)` guarded by `:root:not([data-theme=\"light\"])`, and again under "
    "`:root[data-theme=\"dark\"]`.\n"
    "- Give `body` an explicit background color.\n"
    "- Use 16 px side gutters, never scroll horizontally, and make the layout work at 375 px wide.\n"
    "- Load scripts only from cdnjs.cloudflare.com, cdn.jsdelivr.net, unpkg.com or esm.sh, pinned to an "
    "exact version released at least two weeks ago (for example `react@18.3.1`, never `react` or "
    "`react@18`).\n"
    "- Load stylesheets only from Google Fonts; put all other CSS, JavaScript, images (as data: URLs) and "
    "data inline.\n"
    "- The page cannot make network requests, navigate, submit forms or embed frames.\n"
    "- Keep the page under 640 KiB.\n"
)


def _url_error(url: str, where: str, hosts: Tuple[str, ...], *,
               inline_only: bool) -> Optional[str]:
    value = (url or "").strip()
    if not value or value.startswith("#"):
        return None
    lowered = value.lower()
    if lowered.startswith(("data:", "blob:")):
        if inline_only:
            return (f"{where}: {value[:60]} cannot load here; inline the code in the element "
                    "or load it from an allowlisted CDN over https")
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return f"{where}: {value[:200]} is not a valid URL"
    allowed = bool(host) and host.lower() in hosts
    if parts.scheme.lower() == "https" and allowed:
        return None
    if parts.scheme.lower() == "http" and allowed:
        return f"{where}: {value[:200]} must use https"
    if not parts.scheme and not host:
        return (f"{where}: relative URL {value[:200]} cannot load inside the sandboxed viewer; "
                "inline it (data: URL for images) or use an allowlisted CDN")
    return (f"{where}: {value[:200]} is not allowed here; load it only from "
            f"{', '.join(hosts)}, or inline it")


def _srcset_urls(value: str) -> List[str]:
    urls: List[str] = []
    text, pos = value or "", 0
    while pos < len(text):
        while pos < len(text) and (text[pos].isspace() or text[pos] == ","):
            pos += 1
        start = pos
        while pos < len(text) and not text[pos].isspace():
            pos += 1
        url = text[start:pos]
        if not url:
            break
        if url.endswith(","):
            urls.append(url.rstrip(","))
            continue
        urls.append(url)
        while pos < len(text) and text[pos] != ",":
            pos += 1
    return urls


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: List[Any] = []
        self.errors: List[str] = []
        self.removed: List[str] = []
        self.skip: List[str] = []
        self.forms: List[bool] = []
        self.raw_tag: Optional[str] = None
        self.has_doctype = self.has_viewport = False
        self.head_seen = False
        self.slot_placed = False
        self.html_index: Optional[int] = None
        self.body_index: Optional[int] = None

    def _error(self, message: Optional[str]) -> None:
        if message and message not in self.errors:
            self.errors.append(message)

    def _check_css(self, css: str, where: str) -> None:
        for pattern, hosts in ((_CSS_URL, _FONT_HOSTS), (_CSS_IMPORT, _STYLE_HOSTS)):
            for match in pattern.finditer(css or ""):
                url = match.group(2).strip()
                if url.lower().startswith(("http:", "https:", "//")):
                    self._error(_url_error(url, where, hosts, inline_only=False))

    def _check_resources(self, tag: str, attrs: Dict[str, str]) -> None:
        rel = attrs.get("rel", "").lower().split()
        hosts = _TAG_HOSTS.get(tag, CDN_HOSTS)
        if tag == "link":
            hosts = _STYLE_HOSTS if "stylesheet" in rel else ALLOWED_HOSTS
        inline_only = tag == "script" or (tag == "link" and "stylesheet" in rel)
        for name in _RESOURCE_ATTRS.get(tag, ()):
            if name not in attrs:
                continue
            values = _srcset_urls(attrs[name]) if name == "srcset" else [attrs[name]]
            for value in values:
                self._error(_url_error(value, f"<{tag} {name}>", hosts, inline_only=inline_only))

    def _start(self, tag: str, attr_list: List[Tuple[str, Optional[str]]], closed: bool) -> None:
        if self.skip:
            if tag == self.skip[-1] and not closed:
                self.skip.append(tag)
            return
        attrs: Dict[str, str] = {}
        for key, value in attr_list:
            attrs.setdefault(key, value or "")
        raw = self.get_starttag_text() or f"<{tag}>"
        if tag in _DROP_VOID:
            self.removed.append(f"<{tag}>")
            return
        if tag == "meta":
            equiv = attrs.get("http-equiv", "").strip().lower()
            if equiv == "refresh":
                self.removed.append('<meta http-equiv="refresh">')
                return
            if equiv == "content-security-policy" and attrs.get("content") == CSP:
                return
            if attrs.get("name", "").strip().lower() == "viewport":
                self.has_viewport = True
        if tag in _DROP_TREE:
            self.removed.append(f"<{tag}>")
            if not closed:
                self.skip.append(tag)
            return
        if tag == "form":
            action = attrs.get("action", "").strip()
            external = action.lower().startswith(("http:", "https:", "//", "javascript:", "data:"))
            if external:
                self.removed.append(f'<form action="{action[:120]}">')
            if not closed:
                self.forms.append(external)
            if external:
                return
        if tag in _RESOURCE_ATTRS:
            self._check_resources(tag, attrs)
        if "style" in attrs:
            self._check_css(attrs["style"], f"<{tag} style>")
        if tag == "html" and self.html_index is None:
            self.out.append(raw)
            self.html_index = len(self.out)
            return
        if tag == "body" and self.body_index is None:
            self.body_index = len(self.out)
        if not self.slot_placed and tag != "head":
            self.out.append(_SLOT)
            self.slot_placed = True
        self.out.append(raw)
        if tag == "head" and not self.head_seen:
            self.head_seen = True
            if not self.slot_placed:
                self.out.append(_SLOT)
                self.slot_placed = True
        if tag in ("script", "style") and not closed:
            self.raw_tag = tag

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, tag in _VOID)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == self.raw_tag:
            self.raw_tag = None
        if tag == "form" and self.forms and self.forms.pop():
            return
        if tag in _DROP_VOID or tag in _DROP_TREE or tag in _VOID:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.skip:
            return
        if self.raw_tag == "style":
            self._check_css(data, "<style>")
        self.out.append(data)

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip:
            self.out.append(f"&#{name};")

    def handle_comment(self, data):
        if not self.skip:
            self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        if self.skip:
            return
        if decl.strip().lower().startswith("doctype"):
            self.has_doctype = True
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data):
        if not self.skip:
            self.out.append(f"<?{data}>")

    def unknown_decl(self, data):
        if not self.skip:
            close = "]>" if data.lower().startswith(("if", "else", "endif")) else "]]>"
            self.out.append(f"<![{data}{close}")


def sanitize_html(html: Any, *, title: Any, kind: Any) -> Tuple[str, List[str], List[str]]:
    errors: List[str] = []
    clean_title = " ".join(title.split()) if isinstance(title, str) else ""
    if not clean_title:
        errors.append("title is required: give the page a 2-4 word name")
    elif len(clean_title) > MAX_TITLE_CHARS:
        errors.append(f"title is {len(clean_title)} characters; the limit is {MAX_TITLE_CHARS}")
    if kind not in KINDS:
        errors.append(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(html, str) or not html.strip():
        return "", errors + ["html must be a non-empty string containing the page"], []
    size = len(html.encode("utf-8"))
    if size > MAX_INPUT_BYTES:
        return "", errors + [f"html is {size} bytes; the input limit is {MAX_INPUT_BYTES} bytes"], []
    parser = _Sanitizer()
    parser.feed(html)
    parser.close()
    if parser.skip:
        errors.append(f"<{parser.skip[0]}> is not allowed and was never closed; remove it")
    errors.extend(parser.errors)
    inject = CSP_META + ("" if parser.has_viewport else VIEWPORT_META)
    document_mode = parser.html_index is not None or parser.head_seen or parser.body_index is not None
    parts = list(parser.out)
    if document_mode and parser.slot_placed:
        wrapped = inject if parser.head_seen else f"<head>{inject}</head>"
        parts = [wrapped if part is _SLOT else part for part in parts]
    elif document_mode:
        parts.insert(parser.html_index or 0, f"<head>{inject}</head>")
    else:
        parts = [part for part in parts if part is not _SLOT]
        heading = html_lib.escape(clean_title or "Artifact")
        parts = ['<!doctype html>\n<html lang="en"><head>', inject, '<meta charset="utf-8">',
                 f"<title>{heading}</title></head><body>", *parts, "</body></html>"]
    document = "".join(parts)
    if (parser.head_seen or parser.html_index is not None or parser.body_index is not None) \
            and not parser.has_doctype:
        document = "<!doctype html>\n" + document
    wire = len(json.dumps(document))
    if wire > MAX_WIRE_BYTES:
        errors.append(
            f"the page is {wire} bytes once encoded for delivery; the limit is {MAX_WIRE_BYTES} bytes "
            "(non-ASCII characters cost up to 12 bytes each); remove inline data or large assets")
    if len(errors) > MAX_ERRORS:
        errors = errors[:MAX_ERRORS] + [f"and {len(errors) - MAX_ERRORS} more problems"]
    return document, errors, parser.removed


def validate_html(html: Any, *, title: Any, kind: Any) -> Tuple[str, List[str]]:
    document, errors, _removed = sanitize_html(html, title=title, kind=kind)
    return document, errors


def new_artifact_id() -> str:
    return "art_" + secrets.token_hex(8)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_component(artifact_id: str, *, title: str, kind: str, html: str,
                    version: int, created_at: Optional[str] = None) -> Dict[str, Any]:
    return {
        "type": HTML_TYPE,
        "id": artifact_id,
        "component_id": artifact_id,
        "title": title,
        "kind": kind,
        "version": version,
        "html": html,
        "created_at": created_at or _now(),
    }


def source_params(agent_id: Any, params: Any) -> Any:
    if agent_id != AGENT_ID or not isinstance(params, dict) or not isinstance(params.get("html"), str):
        return params
    clean = {key: value for key, value in params.items() if key != "html"}
    clean["html_sha256"] = hashlib.sha256(params["html"].encode("utf-8")).hexdigest()
    return clean


def download_link(chat_id: Optional[str]) -> Optional[Callable[[Dict[str, Any]], Optional[str]]]:
    if not chat_id or not flags.is_enabled("artifact_export"):
        return None

    def link(component: Dict[str, Any]) -> Optional[str]:
        cid = component.get("component_id")
        if not isinstance(cid, str) or not ARTIFACT_ID.match(cid):
            return None
        return f"/api/export/component/{quote(cid)}.html?chat_id={quote(str(chat_id))}"

    return link


def _rejected(errors: List[str], *, tool: str) -> MCPResponse:
    verb = "published" if tool == PUBLISH_TOOL else "updated"
    return MCPResponse(result={
        "status": "rejected",
        "message": f"The artifact was not {verb}; fix every listed problem and call {tool} again.",
        "errors": errors,
    })


def _accepted(component: Dict[str, Any], removed: List[str]) -> MCPResponse:
    result: Dict[str, Any] = {
        "artifact_id": component["id"],
        "version": component["version"],
        "title": component["title"],
    }
    if removed:
        result["removed"] = sorted(set(removed))
    return MCPResponse(result=result, ui_components=[component])


def publish(args: Dict[str, Any]) -> MCPResponse:
    title = " ".join(args.get("title").split()) if isinstance(args.get("title"), str) else args.get("title")
    kind = args.get("kind") or DEFAULT_KIND
    document, errors, removed = sanitize_html(args.get("html"), title=title, kind=kind)
    if errors:
        return _rejected(errors, tool=PUBLISH_TOOL)
    return _accepted(build_component(new_artifact_id(), title=title, kind=kind,
                                     html=document, version=1), removed)


def update(orch: Any, *, owner: str, chat_id: str, args: Dict[str, Any],
           plane_source: Any) -> MCPResponse:
    from orchestrator import artifact_versions

    artifact_id = args.get("artifact_id")
    if not isinstance(artifact_id, str) or not ARTIFACT_ID.match(artifact_id):
        return _rejected(["artifact_id must be the id returned by publish_artifact (art_ followed by "
                          "16 hex characters)"], tool=UPDATE_TOOL)
    row = orch.workspace.get_by_component_id(chat_id, owner, artifact_id)
    current = row.get("component_data") if isinstance(row, dict) else None
    if not isinstance(current, dict) or current.get("type") != HTML_TYPE:
        return _rejected([f"{artifact_id} is not an artifact in this conversation; publish it first "
                          "with publish_artifact"], tool=UPDATE_TOOL)
    title = current.get("title")
    kind = current.get("kind") if current.get("kind") in KINDS else DEFAULT_KIND
    document, errors, removed = sanitize_html(args.get("html"), title=title, kind=kind)
    if errors:
        return _rejected(errors, tool=UPDATE_TOOL)
    previous = current.get("version")
    version = (previous if type(previous) is int and previous >= 1 else 1) + 1
    archived = artifact_versions.archive(plane_source, chat_id, owner, artifact_id, current, "refine")
    component = build_component(artifact_id, title=title, kind=kind, html=document, version=version)
    component["versions"] = artifact_versions.list_versions(plane_source, chat_id, owner, artifact_id)
    logger.info("artifact.updated chat=%s artifact=%s version=%d archived=%s",
                chat_id, artifact_id, version, archived)
    return _accepted(component, removed)
