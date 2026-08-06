"""Server-side web renderer.

The orchestrator renders ``astralprims`` primitive dicts (already ROTE-adapted)
into web HTML. astralprims *defines* primitives + the structured representation;
this module (in the orchestrator) *renders* them; ROTE *adapts* per device.

Implementation note: pure-Python render functions with explicit ``html.escape``
give a hard escape-by-default guarantee and are deterministic / golden-testable.
Markup parity targets the live ``frontend/src/components/DynamicRenderer.tsx``
(Tailwind class strings reproduced verbatim; the shell self-hosts Tailwind +
Plotly). Text is always escaped; rich text goes through the narrow, sanitized
markdown path in :mod:`webrender.sanitize`.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import re as _re
from typing import Any, Callable, Dict, List
from urllib.parse import quote

logger = logging.getLogger("webrender")

# type -> render function. Populated at the bottom of this module.
PRIMITIVE_RENDERERS: Dict[str, Callable[[Dict[str, Any]], str]] = {}


# Escaping & safe helpers (escape-by-default)

def esc(value: Any) -> str:
    """HTML-escape any value's string form (quotes included)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _attr(value: Any) -> str:
    """Escape a value for use inside a double-quoted HTML attribute."""
    return html.escape(str(value), quote=True)


def safe_url(url: Any) -> str:
    """Return the URL only if its scheme is safe, else '#'. Prevents
    javascript:/data: URL injection in href/src."""
    if not url:
        return "#"
    s = str(url).strip()
    low = s.lower()
    if low.startswith(("http://", "https://", "mailto:", "/")):
        return s
    if low.startswith("data:audio/") or low.startswith("data:image/"):
        return s  # inline media payloads are allowed (audio/image primitives)
    if ":" not in low.split("/", 1)[0]:
        return s  # relative path, no scheme
    return "#"


from .sanitize import inline_md, block_md  # noqa: E402  (after esc/safe_url defined)


# Recursion

def _children(comp: Dict[str, Any]) -> List[Dict[str, Any]]:
    return comp.get("children") or comp.get("content") or []


def render_children(items: List[Any]) -> str:
    return "".join(render_one(c) for c in items if isinstance(c, dict))


_SAFE_DATA_ATTR = _re.compile(r"^data-[a-z0-9-]+$")
# ``data-ui-*`` is the client's generic chrome-dispatch contract: client.js
# delegates on ``[data-ui-action]`` document-wide with NO class or container
# guard, so an author-supplied one would be a one-click trigger for any chrome
# action, fired as the viewing user. Refused as a FAMILY (not a fixed name
# list) so a future ``data-ui-*`` contract is closed on arrival. The chat
# dispatch path needs no such rule — it requires the ``.astral-action`` class,
# and ``class`` is already refused here, so it cannot be forged.
_REFUSED_DATA_ATTR_PREFIX = "data-ui-"
# Accessibility pass-through: every WAI-ARIA attribute is ``aria-`` followed by
# lowercase letters only (aria-label, aria-hidden, aria-describedby,
# aria-valuemin, ...), so the key whitelist is exact.
_SAFE_ARIA_ATTR = _re.compile(r"^aria-[a-z]+$")
# ``role`` values are restricted to a small allowlist of non-interactive
# naming/grouping roles — enough to label and structure content, never to
# retarget widgets (no button/link/checkbox/... that could misrepresent
# behavior to assistive tech).
_SAFE_ROLES = frozenset({"img", "list", "listitem", "status", "note", "group", "region"})


def _base_attrs(comp: Dict[str, Any]) -> str:
    """Render id plus whitelisted entries from ``attributes``.

    ``attributes`` is astralprims' documented free-form escape hatch; the web
    renderer honors only:

    * ``data-*`` keys EXCEPT the ``data-ui-*`` client-dispatch family — the
      adaptive UI designer relies on this for nested morph anchors: the
      materializer stamps ``attributes["data-component-id"]`` on refs nested
      inside arrangements so ``ui_upsert`` morphs keep finding them in the DOM;
    * ``aria-*`` keys and ``role`` — aria values are attribute-escaped like any
      other text; ``role`` is value-validated against the non-interactive
      ``_SAFE_ROLES`` allowlist and silently dropped otherwise.

    Everything else (onclick/style/src/href/class/...) is refused by design
    so authors cannot inject event handlers or override structural
    attributes. Escape-by-default is non-negotiable: every emitted value
    passes through :func:`_attr`.
    """
    parts = []
    cid = comp.get("id")
    if cid:
        parts.append(f' id="{_attr(cid)}"')
    for key_s, value in _explicit_attrs(comp).items():
        if key_s == "role":
            role = str(value).strip().lower()
            if role in _SAFE_ROLES:
                parts.append(f' role="{_attr(role)}"')
        else:
            parts.append(f' {key_s}="{_attr(value)}"')
    return "".join(parts)


def _explicit_attrs(comp: Dict[str, Any]) -> Dict[str, Any]:
    """Author-supplied whitelisted attributes from BOTH wire shapes.

    astralprims ``to_dict()`` MERGES ``attributes`` at the TOP LEVEL of the
    serialized dict (base.py ``_serialize``), while hand-built dicts and the
    designer's materializer set a nested ``"attributes"`` dict — so whitelisted
    keys must be honored wherever they appear (the welcome buttons' aria-labels
    arrived flattened and were silently dropped otherwise). Nested entries win
    on conflict. Only ``data-*`` (minus the ``data-ui-*`` dispatch family),
    ``aria-*`` and ``role`` are ever collected; everything else stays refused.
    """
    found: Dict[str, Any] = {}
    sources = [comp]
    nested = comp.get("attributes")
    if isinstance(nested, dict):
        sources.append(nested)
    for source in sources:
        for key, value in source.items():
            key_s = str(key).lower()
            if key_s.startswith(_REFUSED_DATA_ATTR_PREFIX):
                continue
            if (_SAFE_DATA_ATTR.match(key_s) or _SAFE_ARIA_ATTR.match(key_s)
                    or key_s == "role"):
                found[key_s] = value
    return found


def _has_explicit_attr(comp: Dict[str, Any], name: str) -> bool:
    """True when the author already supplied ``name`` (either wire shape) —
    used to suppress a renderer-generated default (e.g. the metric tile's
    aria-label) so the output never carries a duplicate attribute."""
    return name in _explicit_attrs(comp)


# Primitive renderers (parity with DynamicRenderer.tsx)

def render_container(c):
    # Live container emits no wrapper — just its children.
    return render_children(_children(c))


def render_text(c):
    variant = c.get("variant") or "body"
    content = c.get("content", "")
    if variant == "markdown":
        cls = ("astral-md prose prose-invert max-w-none text-sm text-astral-text leading-relaxed "
               "prose-headings:text-astral-text prose-a:text-astral-primary prose-strong:text-astral-text "
               "prose-code:text-astral-accent")
        return f'<div{_base_attrs(c)} class="{cls}">{block_md(content)}</div>'
    classes = {
        "h1": "text-2xl font-bold text-astral-text",
        "h2": "text-xl font-semibold text-astral-text",
        "h3": "text-lg font-medium text-astral-text",
        "body": "text-sm text-astral-text leading-relaxed",
        "caption": "text-xs text-astral-muted",
    }
    tag = {"h1": "h1", "h2": "h2", "h3": "h3"}.get(variant, "p")
    cls = classes.get(variant, classes["body"])
    return f'<{tag}{_base_attrs(c)} class="{cls}">{esc(content)}</{tag}>'


def render_button(c):
    label = c.get("label", "")
    action = c.get("action", "")
    payload = c.get("payload", {}) or {}
    variant = c.get("variant", "primary")
    # primary = accent gradient (via .astral-btn-primary in astral.css, layered
    # over the bg utility), secondary = outline, ghost = text.
    # `.astral-action` stays the FIRST class — client.js dispatches on it.
    vcls = {
        "primary": "astral-btn-primary bg-astral-primary text-white",
        "secondary": "astral-btn-secondary bg-transparent hover:bg-astral-primary/10 text-astral-text border border-astral-primary/40",
        "ghost": "astral-btn-ghost bg-transparent hover:bg-white/5 text-astral-muted hover:text-astral-text",
        # danger = solid --color-error via .astral-btn-danger (astral.css; the
        # error token is not a Tailwind astral-* color, so the class owns the bg)
        "danger": "astral-btn-danger text-white",
    }.get(variant, "astral-btn-primary bg-astral-primary text-white")
    data = _attr(json.dumps(payload))
    # Buttons honor the attributes whitelist too, so the orchestrator can supply
    # per-button aria-labels. ``_base_attrs`` comes AFTER data-action/data-payload
    # — HTML keeps the first occurrence of a duplicated attribute, so
    # passed-through data-* can never retarget the client.js dispatch contract.
    return (
        f'<button type="button" data-action="{_attr(action)}" data-payload="{data}"{_base_attrs(c)} '
        f'class="astral-action astral-btn px-4 py-2 rounded-lg text-sm font-medium transition-colors {vcls}">'
        f'{esc(label)}</button>'
    )


def render_input(c):
    # No live renderer; provide a basic standalone input for completeness.
    return (
        f'<input type="text" name="{_attr(c.get("name",""))}" value="{_attr(c.get("value",""))}" '
        f'placeholder="{_attr(c.get("placeholder",""))}" '
        f'class="astral-field rounded bg-white/10 border border-white/10 px-2 py-1 text-astral-text w-full">'
    )


def _param_field(field: Dict[str, Any]) -> str:
    name = field.get("name", "")
    label = field.get("label") or name
    kind = field.get("kind", "text")
    help_ = field.get("help")
    default = field.get("default")
    help_html = f'<span class="text-xs text-astral-muted">{esc(help_)}</span>' if help_ else ""
    if kind == "boolean":
        checked = " checked" if default else ""
        help_html = f'<div class="text-xs text-astral-muted">{esc(help_)}</div>' if help_ else ""
        return (
            f'<label class="flex items-start gap-3 text-sm">'
            f'<input type="checkbox" data-field="{_attr(name)}" data-kind="boolean"{checked} '
            f'class="astral-pp-field mt-1 h-4 w-4 rounded border-white/20 bg-white/10">'
            f'<div class="flex-1"><div class="text-astral-text font-medium">{esc(label)}</div>'
            f'{help_html}</div></label>'
        )
    if kind == "number":
        step = field.get("step", 1)
        val = "" if default is None else _attr(default)
        return (
            f'<label class="flex flex-col gap-1 text-sm"><span class="text-astral-text font-medium">{esc(label)}</span>'
            f'{help_html}<input type="number" step="{_attr(step)}" value="{val}" data-field="{_attr(name)}" data-kind="number" '
            f'class="astral-pp-field astral-field rounded bg-white/10 border border-white/10 px-2 py-1 text-astral-text w-40"></label>'
        )
    if kind == "checklist":
        opts = field.get("options") or []
        if not opts:
            inner = '<span class="text-xs text-astral-muted italic">(no options provided)</span>'
        else:
            sel = set(default or []) if isinstance(default, list) else set()
            btns = []
            for opt in opts:
                on = opt in sel
                ocls = ("bg-astral-primary/30 border-astral-primary text-white" if on
                        else "bg-white/5 border-white/10 text-astral-muted hover:bg-white/10")
                btns.append(
                    f'<button type="button" data-field="{_attr(name)}" data-kind="checklist" data-value="{_attr(opt)}" '
                    f'aria-pressed="{"true" if on else "false"}" '
                    f'class="astral-pp-field px-2 py-1 rounded text-xs border transition-colors {ocls}">{esc(opt)}</button>'
                )
            inner = f'<div class="flex flex-wrap gap-2 mt-1">{"".join(btns)}</div>'
        return (
            f'<div class="flex flex-col gap-1 text-sm"><span class="text-astral-text font-medium">{esc(label)}</span>'
            f'{help_html}{inner}</div>'
        )
    if kind == "select":
        opts = field.get("options") or []
        options = "".join(
            f'<option value="{_attr(o)}"{" selected" if o == default else ""}>{esc(o)}</option>' for o in opts
        )
        return (
            f'<label class="flex flex-col gap-1 text-sm"><span class="text-astral-text font-medium">{esc(label)}</span>'
            f'{help_html}<select data-field="{_attr(name)}" data-kind="select" '
            f'class="astral-pp-field astral-field rounded bg-white/10 border border-white/10 px-2 py-1 text-astral-text w-60">{options}</select></label>'
        )
    # text (default)
    val = "" if default is None else _attr(default)
    return (
        f'<label class="flex flex-col gap-1 text-sm"><span class="text-astral-text font-medium">{esc(label)}</span>'
        f'{help_html}<input type="text" value="{val}" data-field="{_attr(name)}" data-kind="text" '
        f'class="astral-pp-field astral-field rounded bg-white/10 border border-white/10 px-2 py-1 text-astral-text w-full"></label>'
    )


def render_param_picker(c):
    title = c.get("title")
    description = c.get("description")
    fields = c.get("fields") or []
    submit_label = c.get("submit_label", "Submit")
    template = c.get("submit_message_template", "")
    title_html = f'<div class="text-base font-semibold text-astral-text mb-1">{esc(title)}</div>' if title else ""
    desc_html = (f'<div class="text-sm text-astral-muted mb-3 whitespace-pre-wrap">{esc(description)}</div>'
                 if description else "")
    rows = "".join(_param_field(f) for f in fields)
    return (
        f'<div{_base_attrs(c)} class="astral-param-picker bg-white/5 rounded-lg border border-white/10 p-4 my-2" '
        f'data-template="{_attr(template)}">{title_html}{desc_html}'
        f'<div class="flex flex-col gap-3 max-h-[28rem] overflow-y-auto pr-1">{rows}</div>'
        f'<div class="mt-4 flex items-center justify-end gap-2">'
        f'<button type="button" class="astral-pp-submit astral-btn astral-btn-primary px-4 py-2 rounded-lg text-sm '
        f'font-medium transition-colors bg-astral-primary text-white">{esc(submit_label)}</button></div></div>'
    )


def render_card(c):
    title = c.get("title")
    title_html = ""
    if title:
        title_html = (
            '<div class="mb-3"><h3 class="text-base font-semibold text-astral-text flex items-center gap-2">'
            '<span class="w-1 h-4 rounded-full bg-astral-primary inline-block"></span>'
            f'{inline_md(title)}</h3></div>'
        )
    # .astral-card carries the layered surface/elevation (astral.css); the
    # children wrapper class stays exactly "space-y-3" (golden-pinned).
    return f'<div{_base_attrs(c)} class="astral-card">{title_html}<div class="space-y-3">{render_children(_children(c))}</div></div>'


def _cell(cell: Any) -> str:
    s = str(cell) if cell is not None else ""
    if s in ("Critical", "Severe"):
        return f'<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-red-500/20 text-red-400">{esc(s)}</span>'
    if s == "Moderate":
        return f'<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-500/20 text-yellow-400">{esc(s)}</span>'
    if s in ("Mild", "Stable"):
        return f'<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-green-500/20 text-green-400">{esc(s)}</span>'
    if s.startswith("http://") or s.startswith("https://"):
        return (f'<a href="{_attr(safe_url(s))}" target="_blank" rel="noopener noreferrer" '
                f'class="text-astral-primary hover:underline inline-flex items-center gap-1">View</a>')
    return esc(s)


def render_table(c):
    headers = c.get("headers") or []
    rows = c.get("rows") or []
    if not headers and not rows:
        return ""
    explicit_title = c.get("title") or c.get("label")
    title = explicit_title or "Table"
    total = c.get("total_rows")
    page_size = c.get("page_size")
    offset = c.get("page_offset") or 0
    paginated = total is not None and page_size is not None
    showing = ""
    if paginated:
        frm = offset + 1
        to = min(offset + page_size, total)
        showing = f'<div class="text-xs text-astral-muted">{frm}–{to} of {esc(total)}</div>'
    # a11y: this renderer only ever emits column headers (rows are plain <td>),
    # so every <th> is scope="col"; a future row-header variant must emit
    # scope="row" on its own cells.
    head = "".join(
        f'<th scope="col" class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-astral-muted whitespace-nowrap">{esc(h)}</th>'
        for h in headers)
    body = "".join(
        '<tr class="border-b border-white/5 hover:bg-white/5 transition-colors">'
        + "".join(f'<td class="px-4 py-3 text-astral-text">{_cell(cell)}</td>' for cell in row)
        + "</tr>"
        for row in rows)
    footer = ""
    if paginated and c.get("source_tool") and c.get("source_agent"):
        sizes = c.get("page_sizes") or [25, 50, 100, 200]
        opts = "".join(f'<option value="{_attr(s)}"{" selected" if s == page_size else ""}>{esc(s)}</option>' for s in sizes)
        prev_dis = " disabled" if offset == 0 else ""
        next_dis = " disabled" if (offset + page_size) >= total else ""
        ctx = _attr(json.dumps({"source_tool": c.get("source_tool"), "source_agent": c.get("source_agent"),
                                "source_params": c.get("source_params") or {}, "page_size": page_size, "page_offset": offset,
                                "total_rows": total}))
        footer = (
            f'<div class="astral-pagination p-3 border-t border-white/5 bg-astral-primary/5 flex items-center justify-between gap-4" data-ctx="{ctx}">'
            f'<div class="flex items-center gap-2"><span class="text-xs text-astral-muted">Rows per page:</span>'
            f'<select class="astral-page-size text-xs bg-astral-surface border border-white/10 rounded px-2 py-1 text-astral-text">{opts}</select></div>'
            f'<div class="flex items-center gap-2">'
            f'<button class="astral-page-prev text-xs px-3 py-1 rounded border border-white/10 text-astral-text hover:bg-white/10 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"{prev_dis}>Prev</button>'
            f'<button class="astral-page-next text-xs px-3 py-1 rounded border border-white/10 text-astral-text hover:bg-white/10 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"{next_dis}>Next</button>'
            f'</div></div>'
        )
    # a11y: an explicit title names the table for assistive tech (aria-label on
    # <table> is announced on entry); the default "Table" placeholder adds
    # nothing, so it is not emitted.
    table_aria = f' aria-label="{_attr(explicit_title)}"' if explicit_title else ""
    return (
        f'<div{_base_attrs(c)} class="astral-table-wrap rounded-lg border border-white/10">'
        f'<div class="p-3 border-b border-white/5 bg-astral-primary/5 flex items-center justify-between">'
        f'<div class="text-sm font-medium text-astral-text">{inline_md(title)}</div>{showing}</div>'
        f'<div class="overflow-x-auto"><table class="w-full text-sm"{table_aria}>'
        f'<thead><tr class="bg-astral-primary/10 border-b border-white/5">{head}</tr></thead>'
        f'<tbody>{body}</tbody></table></div>{footer}</div>'
    )


def render_list(c):
    items = c.get("items") or []
    if not items:
        return ""
    if c.get("variant") == "detailed":
        out = [f'<div{_base_attrs(c)} class="space-y-3">']
        for it in items:
            if not isinstance(it, dict):
                it = {"title": str(it)}
            title = it.get("title", "")
            url = it.get("url")
            if url:
                title_node = (f'<a href="{_attr(safe_url(url))}" target="_blank" rel="noopener noreferrer" '
                              f'class="hover:text-astral-primary hover:underline flex items-center gap-2">{inline_md(title)}</a>')
            else:
                title_node = inline_md(title)
            sub = f'<p class="text-xs text-astral-muted">{inline_md(it.get("subtitle",""))}</p>' if it.get("subtitle") else ""
            desc = f'<div class="text-sm text-astral-text/80 line-clamp-2">{block_md(it.get("description",""))}</div>' if it.get("description") else ""
            out.append(
                '<div class="astral-list-item p-3 hover:bg-white/5 transition-colors"><div class="flex justify-between items-start gap-4">'
                f'<div class="space-y-1 w-full"><h4 class="text-sm font-semibold text-astral-text flex items-center justify-between">{title_node}</h4>{sub}{desc}</div></div></div>'
            )
        out.append("</div>")
        return "".join(out)
    ordered = bool(c.get("ordered"))
    tag = "ol" if ordered else "ul"
    lcls = "list-decimal" if ordered else "list-disc"
    lis = "".join(
        f'<li class="leading-relaxed">{inline_md(it) if isinstance(it, str) else esc(json.dumps(it))}</li>'
        for it in items)
    return f'<{tag}{_base_attrs(c)} class="space-y-2 text-sm {lcls} list-inside text-astral-text">{lis}</{tag}>'


def _alert_icon(variant: str) -> str:
    # minimal inline SVGs (~16px), one per variant
    paths = {
        "info": "M12 2a10 10 0 100 20 10 10 0 000-20zm0 9v5m0-8h.01",
        "success": "M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3",
        "warning": "M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0zM12 9v4m0 4h.01",
        "error": "M12 2a10 10 0 100 20 10 10 0 000-20zm0 6v4m0 4h.01",
    }
    d = paths.get(variant, paths["info"])
    return (f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="{d}"></path></svg>')


def render_alert(c):
    variant = c.get("variant", "info")
    cfg = {
        "info": ("bg-blue-500/10", "border-blue-500/20", "text-blue-400"),
        "success": ("bg-green-500/10", "border-green-500/20", "text-green-400"),
        "warning": ("bg-yellow-500/10", "border-yellow-500/20", "text-yellow-400"),
        "error": ("bg-red-500/10", "border-red-500/20", "text-red-400"),
    }
    vkey = variant if variant in cfg else "info"  # whitelisted — safe in a class name
    bg, border, txt = cfg[vkey]
    title = c.get("title")
    title_html = f'<p class="font-medium text-sm {txt}">{inline_md(title)}</p>' if title else ""
    msg = c.get("message", "")
    return (
        f'<div{_base_attrs(c)} class="astral-alert astral-alert--{vkey} {bg} {border} border rounded-lg p-4 flex items-start gap-3">'
        f'<span class="{txt}">{_alert_icon(variant)}</span>'
        f'<div class="flex-1"><div>{title_html}<div class="text-sm text-astral-text/80">{block_md(msg)}</div></div></div></div>'
    )


def render_progress(c):
    value = c.get("value") or 0.0
    label = c.get("label")
    show_pct = c.get("show_percentage", True)
    pct = max(0, min(value * 100, 100))
    header = ""
    if label:
        pct_span = f'<span>{round(value * 100)}%</span>' if show_pct is not False else ""
        header = (f'<div class="flex justify-between text-xs text-astral-muted w-full mb-1"><span>{inline_md(label)}</span>{pct_span}</div>')
    return (
        f'<div{_base_attrs(c)} class="astral-progress">{header}'
        f'<div class="astral-progress-track h-2 bg-white/10 rounded-full overflow-hidden">'
        f'<div class="h-full bg-gradient-to-r from-astral-primary to-astral-secondary rounded-full" style="width:{pct}%"></div></div></div>'
    )


def render_metric(c):
    variant = c.get("variant", "default")
    vmap = {
        "default": "from-astral-primary/20 to-astral-primary/5",
        "warning": "from-yellow-500/20 to-yellow-500/5",
        "error": "from-red-500/20 to-red-500/5",
        "success": "from-green-500/20 to-green-500/5",
    }
    vkey = variant if variant in vmap else "default"  # whitelisted — safe in a class name
    vbg = vmap[vkey]
    subtitle = c.get("subtitle")
    sub_html = f'<p class="text-xs text-astral-muted mt-1">{inline_md(subtitle)}</p>' if subtitle else ""
    progress = c.get("progress")
    prog_html = ""
    if progress is not None:
        color = "bg-red-500" if progress > 0.9 else "bg-yellow-500" if progress > 0.7 else "bg-astral-primary"
        pw = max(0, min(progress * 100, 100))
        prog_html = (f'<div class="mt-3 h-1.5 bg-white/10 rounded-full overflow-hidden">'
                     f'<div class="h-full rounded-full {color}" style="width:{pw}%"></div></div>')
    title = c.get("title", "")
    value = c.get("value", "")
    # a11y: name the whole tile "<label>: <value>" so assistive tech announces
    # the pairing the visual layout only implies. An author-supplied
    # attributes["aria-label"] wins (no duplicate attribute emitted).
    name = f"{title}: {value}" if str(title).strip() else str(value)
    aria = ""
    if name.strip() and not _has_explicit_attr(c, "aria-label"):
        aria = f' aria-label="{_attr(name)}"'
    return (
        f'<div{_base_attrs(c)}{aria} class="astral-metric astral-metric--{vkey} rounded-xl p-4 bg-gradient-to-br {vbg} border border-white/5 relative">'
        f'<p class="text-xs text-astral-muted font-medium uppercase tracking-wider mb-1">{inline_md(title)}</p>'
        f'<div class="flex-1"><p class="text-2xl font-bold text-astral-text">{esc(value)}</p>{sub_html}</div>{prog_html}</div>'
    )


def render_code(c):
    language = c.get("language")
    lang_html = f'<div class="px-4 py-2 border-b border-white/5 text-xs text-astral-muted">{esc(language)}</div>' if language else ""
    return (
        f'<div{_base_attrs(c)} class="astral-code rounded-lg bg-black/40 border border-white/5 overflow-hidden">{lang_html}'
        f'<pre class="p-4 text-sm overflow-x-auto" style="font-family:\'JetBrains Mono\',monospace">'
        f'<code class="text-green-400">{esc(c.get("code",""))}</code></pre></div>'
    )


def render_image(c):
    # No live renderer; basic standalone image for completeness.
    url = c.get("url")
    if not url:
        return ""
    attrs = f'src="{_attr(safe_url(url))}" alt="{_attr(c.get("alt",""))}"'
    if c.get("width"):
        attrs += f' width="{_attr(c.get("width"))}"'
    if c.get("height"):
        attrs += f' height="{_attr(c.get("height"))}"'
    return f'<img{_base_attrs(c)} {attrs} class="max-w-full rounded-lg">'


def render_grid(c):
    cols = c.get("columns", 2)
    gap = c.get("gap", 16)
    n = min(int(cols) if isinstance(cols, (int, float)) else 2, 6)
    col_map = {
        1: "grid-cols-1",
        2: "grid-cols-1 sm:grid-cols-2",
        3: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3",
        4: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4",
        5: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5",
        6: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6",
    }.get(n, f"grid-cols-1 sm:grid-cols-2 lg:grid-cols-{n}")
    return f'<div{_base_attrs(c)} class="grid {col_map}" style="gap:{int(gap)}px">{render_children(_children(c))}</div>'


def render_tabs(c):
    # No live renderer; provide a basic <details>-based fallback for completeness.
    tabs = c.get("tabs") or []
    out = [f'<div{_base_attrs(c)} class="astral-tabs space-y-2">']
    for i, t in enumerate(tabs):
        if not isinstance(t, dict):
            continue
        label = t.get("label", f"Tab {i+1}")
        body = render_children(t.get("content") or [])
        out.append(f'<details class="astral-tab"{" open" if i == 0 else ""}>'
                   f'<summary class="text-sm font-medium text-astral-text cursor-pointer">{esc(label)}</summary>'
                   f'<div class="space-y-3 pt-2">{body}</div></details>')
    out.append("</div>")
    return "".join(out)


def render_divider(c):
    return '<hr class="border-white/10 my-3"/>'


def render_collapsible(c):
    title = c.get("title") or "Details"
    is_open = bool(c.get("default_open"))
    body = render_children(_children(c))
    return (
        f'<details{_base_attrs(c)} class="astral-collapsible overflow-hidden"{" open" if is_open else ""}>'
        f'<summary class="flex items-center w-full gap-2 px-3 py-2 hover:bg-white/[0.03] transition-colors text-left cursor-pointer list-none">'
        f'<span class="text-[11px] font-medium text-astral-muted/70 uppercase tracking-wider flex-1 truncate">{esc(title)}</span></summary>'
        f'<div class="px-3 pb-3 pt-1.5 border-t border-white/[0.04] space-y-2 max-h-[420px] overflow-y-auto scrollbar-thin">{body}</div></details>'
    )


_CHART_KINDS = {"bar": "Bar chart", "line": "Line chart", "pie": "Pie chart"}


def _chart_summary(chart_type: str, payload: Dict[str, Any]) -> str:
    """Cheap, deterministic text alternative for a chart's data.

    Not a full description — just enough for a screen-reader user to gauge
    what sighted users see: point/series count plus the numeric range."""
    if chart_type == "plotly":
        n = len(payload.get("data") or [])
        return f"{n} data series" if n != 1 else "1 data series"
    data = payload.get("data") or []
    n = len(data)
    summary = f"{n} data points" if n != 1 else "1 data point"
    nums = []
    for v in data:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                f = float(v)
            except OverflowError:
                continue
            if math.isfinite(f):
                nums.append(f)
    if nums:
        summary += f", range {min(nums):g} to {max(nums):g}"
    return summary


def _chart_div(c, chart_type, payload):
    title = c.get("title")
    title_html = f'<p class="text-sm font-medium text-astral-text mb-3">{esc(title)}</p>' if title else ""
    data = _attr(json.dumps(payload))
    # a11y: the chart node is an empty div until client-side Plotly draws into
    # it — name it like an image (type + title, falling back to the data
    # summary). The sr-only summary sits OUTSIDE the role="img" element
    # (children of role="img" are presentational to AT) and therefore also
    # survives Plotly.newPlot() replacing the chart div's contents in the
    # browser.
    summary = _chart_summary(chart_type, payload)
    kind = _CHART_KINDS.get(chart_type, "Chart")
    name = f"{kind}: {title}" if title else f"{kind}: {summary}"
    return (f'<div{_base_attrs(c)} class="astral-chart-card w-full">{title_html}'
            f'<div class="astral-chart" data-chart-type="{chart_type}" data-chart="{data}" '
            f'role="img" aria-label="{_attr(name)}" style="min-height:320px"></div>'
            f'<span class="astral-sr-only">{esc(summary)}</span></div>')


def render_bar_chart(c):
    datasets = c.get("datasets") or []
    if not datasets:
        return ""
    data = (datasets[0] or {}).get("data", []) if isinstance(datasets[0], dict) else []
    return _chart_div(c, "bar", {"labels": c.get("labels", []), "data": data})


def render_line_chart(c):
    datasets = c.get("datasets") or []
    if not datasets:
        return ""
    data = (datasets[0] or {}).get("data", []) if isinstance(datasets[0], dict) else []
    return _chart_div(c, "line", {"labels": c.get("labels", []), "data": data})


def render_pie_chart(c):
    data = c.get("data") or []
    if not data:
        return ""
    return _chart_div(c, "pie", {"labels": c.get("labels", []), "data": data, "colors": c.get("colors", [])})


def _scrub_plotly_html(obj):
    """Defang HTML in agent-supplied Plotly text fields before it reaches
    ``Plotly.newPlot`` on the client.

    Plotly renders some text (titles, ticktext, annotations, hovertext) as
    HTML, so the agent-emitted spec is a trust boundary that bypasses the
    renderer's escape-by-default discipline. Strip script/embed tags, inline
    event handlers, and ``javascript:`` URIs while preserving the benign
    formatting tags Plotly legitimately uses (``<br>``/``<b>``/``<i>``)."""
    if isinstance(obj, dict):
        return {k: _scrub_plotly_html(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_plotly_html(v) for v in obj]
    if isinstance(obj, str):
        s = _re.sub(r"(?is)<\s*script.*?>.*?<\s*/\s*script\s*>", "", obj)
        s = _re.sub(r"(?is)<\s*/?\s*(script|iframe|object|embed|svg|img|link|meta|style)\b[^>]*>", "", s)
        s = _re.sub(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", s)
        s = _re.sub(r"(?i)javascript:", "", s)
        return s
    return obj


def render_plotly_chart(c):
    data = c.get("data") or []
    if not data:
        return ""
    return _chart_div(c, "plotly", {
        "data": _scrub_plotly_html(data),
        "layout": _scrub_plotly_html(c.get("layout", {})),
        "config": c.get("config", {}),
    })


def render_color_picker(c):
    label = c.get("label", "")
    key = c.get("color_key", "")
    value = c.get("value") or "#000000"
    return (
        f'<div{_base_attrs(c)} class="flex items-center gap-3 py-1">'
        f'<label class="text-sm text-astral-text font-medium min-w-[100px]">{esc(label)}</label>'
        f'<div class="relative"><input type="color" value="{_attr(value)}" data-color-key="{_attr(key)}" '
        f'class="astral-color-picker w-10 h-10 rounded-lg border-2 border-white/10 bg-transparent cursor-pointer"></div>'
        f'<span class="text-xs text-astral-muted font-mono">{esc(value)}</span></div>'
    )


def render_theme_apply(c):
    message = c.get("message") or "Theme updated"
    spec = {k: c.get(k) for k in ("preset", "colors", "color_key", "color_value") if c.get(k) is not None}
    data = _attr(json.dumps(spec))
    return (
        f'<div{_base_attrs(c)} class="astral-theme-apply bg-green-500/10 border border-green-500/20 rounded-lg p-3 flex items-center gap-2" data-theme="{data}">'
        f'<span class="text-green-400">{_alert_icon("success")}</span>'
        f'<span class="text-sm text-astral-text/80">{esc(message)}</span></div>'
    )


def render_file_upload(c):
    label = c.get("label", "Upload File")
    accept = c.get("accept", "*/*")
    return (
        f'<div{_base_attrs(c)} class="flex items-center gap-3 py-2">'
        f'<label class="astral-btn cursor-pointer inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors '
        f'bg-astral-primary/20 hover:bg-astral-primary/30 text-astral-primary border border-astral-primary/30">'
        f'<span>{esc(label)}</span>'
        f'<input type="file" class="astral-file-upload hidden" accept="{_attr(accept)}"></label></div>'
    )


def render_file_download(c):
    label = c.get("label", "Download File")
    url = c.get("url")
    filename = c.get("filename")
    # Root-relative URLs (/api/download/...) are valid: the browser resolves
    # them against the serving origin (no hard-coded host).
    valid = bool(url) and url != "#" and str(url).startswith(("http", "/"))
    # Built outside the f-string: escaped quotes inside an f-string expression
    # are a SyntaxError on Python <=3.11 (the container runtime).
    download_attr = f' download="{_attr(filename)}"' if filename else ""
    if valid:
        return (
            f'<div{_base_attrs(c)} class="flex items-center gap-3 py-2">'
            f'<a href="{_attr(safe_url(url))}"{download_attr} '
            f'class="astral-btn inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors '
            f'bg-astral-secondary/20 hover:bg-astral-secondary/30 text-astral-secondary border border-astral-secondary/30">'
            f'<span>{esc(label)}</span></a></div>'
        )
    return (
        f'<div{_base_attrs(c)} class="flex items-center gap-3 py-2">'
        f'<button disabled class="inline-flex items-center gap-2 px-4 py-2 bg-gray-500/20 text-gray-400 '
        f'border border-gray-500/30 rounded-lg text-sm font-medium cursor-not-allowed opacity-50">'
        f'<span>{esc(label or "Download File (unavailable)")}</span></button></div>'
    )


def render_audio(c):
    src = c.get("src", "")
    label = c.get("label")
    description = c.get("description")
    label_html = f'<div class="text-sm font-medium text-white/90 mb-2">{esc(label)}</div>' if label else ""
    desc_html = f'<div class="text-xs text-white/50 mt-2">{esc(description)}</div>' if description else ""
    if src:
        controls = "" if c.get("showControls") is False else " controls"
        autoplay = " autoplay" if c.get("autoplay") is True else ""
        loop = " loop" if c.get("loop") is True else ""
        ctype = c.get("contentType")
        type_attr = f' type="{_attr(ctype)}"' if ctype else ""
        media = (f'<audio{controls}{autoplay}{loop} class="w-full" style="min-height:40px">'
                 f'<source src="{_attr(safe_url(src))}"{type_attr}>Your browser does not support the audio element.</audio>')
    else:
        media = '<div class="text-white/40 text-xs italic">No audio source provided</div>'
    return (f'<div{_base_attrs(c)} class="audio-component rounded-lg border border-white/10 bg-white/5 p-3">'
            f'{label_html}{media}{desc_html}</div>')


# Dashboard & status primitives (astralprims >= 0.2.0)

_BADGE_VARIANTS = {
    "default": "bg-white/10 text-astral-text border-white/15",
    "success": "bg-green-500/15 text-green-400 border-green-500/25",
    "warning": "bg-yellow-500/15 text-yellow-400 border-yellow-500/25",
    "error": "bg-red-500/15 text-red-400 border-red-500/25",
    "info": "bg-blue-500/15 text-blue-400 border-blue-500/25",
    "accent": "bg-astral-primary/15 text-astral-primary border-astral-primary/25",
}


def _badge_span(label, variant, icon=None, extra_attrs=""):
    try:
        vkey = variant if variant in _BADGE_VARIANTS else "default"
    except TypeError:  # unhashable variant from raw LLM/agent JSON
        vkey = "default"
    # decorative emoji/symbol — hidden from assistive tech
    icon_html = f'<span class="astral-badge-icon" aria-hidden="true">{esc(icon)}</span>' if icon else ""
    return (
        f'<span{extra_attrs} class="astral-badge astral-badge--{vkey} inline-flex items-center gap-1 '
        f'px-2 py-0.5 rounded-full border text-xs font-medium {_BADGE_VARIANTS[vkey]}">'
        f'{icon_html}<span class="astral-badge-label">{esc(label)}</span></span>'
    )


def render_badge(c):
    return _badge_span(c.get("label", ""), c.get("variant", "default"),
                       icon=c.get("icon"), extra_attrs=_base_attrs(c))


def render_hero(c):
    variant = c.get("variant", "default")
    vkey = variant if variant in ("default", "gradient", "subtle") else "default"
    eyebrow = c.get("eyebrow")
    subtitle = c.get("subtitle")
    icon = c.get("icon")
    badges = [b for b in (c.get("badges") or []) if isinstance(b, str) and b.strip()]
    eyebrow_html = (f'<p class="text-xs font-semibold uppercase tracking-widest text-astral-primary mb-1">'
                    f'{esc(eyebrow)}</p>') if eyebrow else ""
    # decorative emoji next to the h2 — hidden from assistive tech
    icon_html = f'<span class="astral-hero-icon text-3xl mr-3" aria-hidden="true">{esc(icon)}</span>' if icon else ""
    subtitle_html = f'<p class="text-sm text-astral-muted mt-1">{inline_md(subtitle)}</p>' if subtitle else ""
    badges_html = ""
    if badges:
        badges_html = ('<div class="flex flex-wrap items-center gap-2 mt-3">'
                       + "".join(_badge_span(b, "accent") for b in badges) + "</div>")
    return (
        f'<div{_base_attrs(c)} class="astral-hero astral-hero--{vkey} rounded-xl p-5">'
        f'{eyebrow_html}<div class="flex items-center">{icon_html}'
        f'<h2 class="text-2xl font-bold text-astral-text tracking-tight">{inline_md(c.get("title", ""))}</h2></div>'
        f'{subtitle_html}{badges_html}</div>'
    )


def render_keyvalue(c):
    items = [i for i in (c.get("items") or []) if isinstance(i, dict)]
    if not items:
        return ""
    cols = c.get("columns", 2)
    try:
        n = int(cols)
    except (TypeError, ValueError, OverflowError):  # NaN/Infinity/non-numeric
        n = 2
    n = min(max(n, 1), 4)
    col_map = {
        1: "grid-cols-1",
        2: "grid-cols-1 sm:grid-cols-2",
        3: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3",
        4: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-4",
    }[n]
    title = c.get("title")
    title_html = f'<p class="text-sm font-semibold text-astral-text mb-3">{inline_md(title)}</p>' if title else ""
    rows = []
    for item in items:
        hint = item.get("hint")
        hint_html = f'<p class="text-xs text-astral-muted mt-0.5">{inline_md(str(hint))}</p>' if hint else ""
        rows.append(
            f'<div class="astral-kv-item">'
            f'<dt class="text-xs text-astral-muted font-medium uppercase tracking-wider">{inline_md(str(item.get("label", "")))}</dt>'
            f'<dd class="text-sm font-semibold text-astral-text mt-0.5">{esc(item.get("value", ""))}</dd>'
            f'{hint_html}</div>'
        )
    return (f'<div{_base_attrs(c)} class="astral-kv rounded-lg p-4">{title_html}'
            f'<dl class="grid {col_map} gap-3">{"".join(rows)}</dl></div>')


_TIMELINE_VARIANTS = ("default", "success", "warning", "error", "info")


def render_timeline(c):
    items = [i for i in (c.get("items") or []) if isinstance(i, dict)]
    if not items:
        return ""
    title = c.get("title")
    title_html = f'<p class="text-sm font-semibold text-astral-text mb-3">{inline_md(title)}</p>' if title else ""
    rows = []
    for item in items:
        ivar = item.get("variant", "default")
        ikey = ivar if ivar in _TIMELINE_VARIANTS else "default"
        time = item.get("time")
        desc = item.get("description")
        time_html = (f'<span class="astral-tl-time text-xs font-mono text-astral-muted whitespace-nowrap">'
                     f'{esc(str(time))}</span>') if time else ""
        desc_html = f'<p class="text-xs text-astral-muted mt-0.5">{inline_md(str(desc))}</p>' if desc else ""
        rows.append(
            f'<li class="astral-tl-item astral-tl-item--{ikey}">'
            f'<div class="flex items-baseline gap-3">{time_html}'
            f'<div class="min-w-0"><p class="text-sm font-medium text-astral-text">{inline_md(str(item.get("title", "")))}</p>'
            f'{desc_html}</div></div></li>'
        )
    # a11y: markup is already a real <ol>/<li> list, but astral.css sets
    # list-style:none on .astral-tl-list, which strips the implicit list role in
    # WebKit/VoiceOver — restore it explicitly (the <li> children keep their
    # implicit listitem role).
    return (f'<div{_base_attrs(c)} class="astral-timeline rounded-lg p-4">{title_html}'
            f'<ol class="astral-tl-list space-y-3" role="list">{"".join(rows)}</ol></div>')


def render_rating(c):
    try:
        max_value = int(c.get("max_value", 5))
    except (TypeError, ValueError, OverflowError):  # incl. float Infinity
        max_value = 5
    max_value = min(max(max_value, 1), 10)
    try:
        value = float(c.get("value", 0.0))
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value):  # NaN survives min/max clamping
        value = 0.0
    value = min(max(value, 0.0), float(max_value))
    filled = int(round(value))
    stars = "".join(
        f'<span class="astral-star{" astral-star--filled" if i < filled else ""}">★</span>'
        for i in range(max_value)
    )
    label = c.get("label")
    subtitle = c.get("subtitle")
    label_html = (f'<p class="text-xs text-astral-muted font-medium uppercase tracking-wider mb-1">'
                  f'{inline_md(label)}</p>') if label else ""
    # value formatted from validated floats — no escaping needed
    value_html = ""
    if c.get("show_value", True) is not False:
        value_html = (f'<span class="text-sm font-semibold text-astral-text ml-2">'
                      f'{value:g}/{max_value}</span>')
    sub_html = f'<p class="text-xs text-astral-muted mt-1">{inline_md(subtitle)}</p>' if subtitle else ""
    return (f'<div{_base_attrs(c)} class="astral-rating rounded-lg p-4">{label_html}'
            f'<div class="flex items-center"><span class="astral-stars text-lg leading-none">{stars}</span>'
            f'{value_html}</div>{sub_html}</div>')


def render_chat_history(c: Dict[str, Any]) -> str:
    """Render the recent-chats surface.

    A scannable list of conversation rows. Each row is a real ``<button>`` that
    carries the ``astral-action`` dispatch contract (``data-action=load_chat`` +
    ``data-payload``) so the existing client.js delegation opens it — no client
    change. Per item the builder may supply ``title`` (required for the label),
    ``preview`` (last-message snippet), ``time`` (pre-formatted relative time),
    ``icon`` (decorative agent glyph, hidden from assistive tech) and ``saved``
    (truthy → a saved-components marker). Everything is escaped by construction;
    an item with no ``chat_id`` is skipped (it cannot be opened). With no
    openable items the surface shows a friendly empty state.
    """
    title = c.get("title") or "Recent chats"
    raw_items = c.get("items") or []
    rows: List[str] = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        cid = it.get("chat_id") or it.get("id")
        if not cid:
            continue
        name = str(it.get("title") or "Untitled chat").strip() or "Untitled chat"
        payload = _attr(json.dumps({"chat_id": str(cid)}))
        icon = it.get("icon")
        icon_html = (f'<span class="astral-history-avatar" aria-hidden="true">{esc(icon)}</span>'
                     if icon else '<span class="astral-history-avatar astral-history-avatar--blank" aria-hidden="true"></span>')
        time_html = (f'<span class="astral-history-time">{esc(it.get("time"))}</span>'
                     if it.get("time") else "")
        preview = str(it.get("preview") or "").strip()
        preview_html = (f'<span class="astral-history-preview">{esc(preview)}</span>'
                        if preview else "")
        saved_html = ('<span class="astral-history-saved" title="Has saved components" '
                      'aria-hidden="true">★</span>') if it.get("saved") else ""
        aria = esc(f"Open chat: {name}" + (f", {it.get('time')}" if it.get("time") else ""))
        rows.append(
            f'<button type="button" class="astral-action astral-history-item" '
            f'data-action="load_chat" data-payload="{payload}" aria-label="{aria}">'
            f'{icon_html}'
            f'<span class="astral-history-body">'
            f'<span class="astral-history-row1">'
            f'<span class="astral-history-name">{esc(name)}</span>{time_html}</span>'
            f'{preview_html}</span>{saved_html}</button>'
        )
    if not rows:
        body = ('<div class="astral-history-empty">'
                '<span class="astral-history-empty-icon" aria-hidden="true">\U0001F4AC</span>'
                '<span class="astral-history-empty-text">No conversations yet.</span>'
                '<span class="astral-history-empty-hint">Start one below.</span></div>')
        return f'<div{_base_attrs(c)} class="astral-history">{body}</div>'
    count_html = f'<span class="astral-history-count">{len(rows)}</span>'
    head = (f'<div class="astral-history-head">'
            f'<span class="astral-history-title">{esc(title)}</span>{count_html}</div>')
    return (f'<div{_base_attrs(c)} class="astral-history">{head}'
            f'<div class="astral-history-list">{"".join(rows)}</div></div>')


_SKELETON_WIDTHS = ("w-3/4", "w-1/2", "w-2/3", "w-5/6", "w-1/3")
_SKELETON_MAX_ROWS = 12


def render_skeleton(c: Dict[str, Any]) -> str:
    """Render a loading-skeleton placeholder.

    A server-driven, content-free shimmer placeholder shown while a surface
    (e.g. the chat-history list) loads. Carries NO user data. ``role=status`` +
    an ``sr-only`` label expose it to assistive tech; the shimmer lives in
    ``.astral-skeleton-line`` CSS, which honours ``prefers-reduced-motion``.
    ``variant`` ∈ {``list``/``chat-history``, ``card``, ``lines``}; ``count`` is
    the number of placeholder rows (bounded). All class names come from a fixed
    whitelist, and ``label`` is escaped — safe by construction.
    """
    variant = str(c.get("variant", "list"))
    try:
        count = int(c.get("count", 4))
    except (TypeError, ValueError):
        count = 4
    count = max(1, min(count, _SKELETON_MAX_ROWS))
    label = esc(c.get("label") or "Loading…")
    rows: List[str] = []
    for i in range(count):
        w = _SKELETON_WIDTHS[i % len(_SKELETON_WIDTHS)]
        if variant in ("list", "chat-history"):
            rows.append(
                '<div class="flex items-center gap-3 py-2">'
                '<div class="astral-skeleton-line h-8 w-8 rounded-full shrink-0"></div>'
                '<div class="flex-1 space-y-2">'
                f'<div class="astral-skeleton-line h-3 {w}"></div>'
                '<div class="astral-skeleton-line h-2 w-1/3"></div>'
                '</div></div>'
            )
        elif variant == "card":
            rows.append('<div class="astral-skeleton-line h-20 w-full mb-3"></div>')
        else:  # "lines"
            rows.append(f'<div class="astral-skeleton-line h-3 {w} mb-2"></div>')
    return (
        '<div class="astral-skeleton" role="status" aria-busy="true" aria-live="polite">'
        f'<span class="sr-only">{label}</span>{"".join(rows)}</div>'
    )


def skeleton_component(variant: str = "list", count: int = 4, label: str = "Loading…") -> Dict[str, Any]:
    """Build a skeleton primitive dict. Use ``variant='chat-history'`` for the
    chat-list loading state. Callers emit this like any astralprims primitive;
    ROTE adapts it per device and the orchestrator renders it."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 4
    return {"type": "skeleton", "variant": str(variant), "count": n, "label": str(label)}


def _is_github_release_url(url: str) -> bool:
    """Only a real GitHub Release asset/release URL may render as a download link.

    Defense-in-depth: the card is built from the GitHub Releases API, but the
    renderer never trusts an arbitrary URL — anything else renders as a disabled
    'unavailable' button so a malformed/crafted value can't become a clickable
    link to an attacker host.
    """
    s = str(url or "").strip()
    return s.startswith("https://github.com/")


def render_download_card(c: Dict[str, Any]) -> str:
    """Render a desktop-app download card with an integrity block.

    The primary button links to the GitHub Release asset (validated). A
    monospace block shows the version, platform and SHA-256 so the user can
    verify integrity; a collapsible note explains SHA-256 + sigstore. When the
    download URL is absent/unavailable, a disabled button + a link to the
    releases page is rendered instead — never a fabricated link.
    """
    title = c.get("title") or "Astral desktop app"
    description = c.get("description") or ""
    download_url = c.get("download_url") or ""
    sha256 = (c.get("sha256") or "").lower()
    bundle_url = c.get("sigstore_bundle_url") or ""
    version = c.get("version") or ""
    platform = c.get("platform") or "windows-x64"
    html_url = c.get("html_url") or ""
    variant = c.get("variant") or ("available" if download_url else "unavailable")

    desc_html = f'<p class="text-xs text-white/60 mt-1 mb-3">{esc(description)}</p>' if description else ""
    ver_html = (f'<span class="inline-block text-[11px] px-2 py-0.5 rounded-full '
                f'bg-white/10 text-white/70 mr-2">v{esc(version)}</span>'
                if version else "")
    plat_html = (f'<span class="inline-block text-[11px] px-2 py-0.5 rounded-full '
                 f'bg-white/10 text-white/70">{esc(platform)}</span>')

    # Integrity block (only meaningful when we have a hash).
    sha_html = ""
    if sha256:
        sha_html = (
            '<div class="mt-3 rounded-md bg-black/30 border border-white/10 p-2">'
            '<div class="text-[10px] uppercase tracking-wide text-white/40 mb-1">SHA-256</div>'
            f'<code class="text-[11px] text-astral-secondary break-all">{esc(sha256)}</code></div>'
        )

    note_html = (
        '<details class="mt-2"><summary class="text-[11px] text-white/40 cursor-pointer '
        'hover:text-white/60">How integrity is verified</summary>'
        '<p class="text-[11px] text-white/50 mt-1">The app is built and signed by GitHub '
        'Actions. The desktop client checks the SHA-256 against a published checksum file '
        'and verifies a sigstore signature before it ever runs the downloaded binary.</p></details>'
    )

    if variant == "available" and _is_github_release_url(download_url):
        btn = (
            f'<a href="{_attr(safe_url(download_url))}" '
            f'class="astral-btn inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm '
            f'font-medium bg-astral-secondary/20 hover:bg-astral-secondary/30 text-astral-secondary '
            f'border border-astral-secondary/30">⬇ Download for Windows</a>'
        )
    else:
        btn = ('<button disabled class="inline-flex items-center gap-2 px-4 py-2 bg-gray-500/20 '
               'text-gray-400 border border-gray-500/30 rounded-lg text-sm font-medium '
               'cursor-not-allowed opacity-50">Download temporarily unavailable</button>')

    releases_link = ""
    if _is_github_release_url(html_url):
        releases_link = (f'<a href="{_attr(safe_url(html_url))}" target="_blank" rel="noopener" '
                         'class="text-[11px] text-astral-secondary/80 hover:text-astral-secondary '
                         'underline ml-3">GitHub Releases ↗</a>')

    bundle_attr = f' data-bundle-url="{_attr(bundle_url)}"' if bundle_url else ""
    return (
        f'<div{_base_attrs(c)} class="astral-download-card rounded-xl border border-white/10 '
        f'bg-white/5 p-4"{bundle_attr}>'
        f'<div class="flex items-center justify-between gap-3">'
        f'<h4 class="text-sm font-semibold text-white/90">{esc(title)}</h4>'
        f'<div>{ver_html}{plat_html}</div></div>'
        f'{desc_html}'
        f'<div class="flex items-center">{btn}{releases_link}</div>'
        f'{sha_html}{note_html}</div>'
    )


PRIMITIVE_RENDERERS.update({
    "container": render_container, "text": render_text, "button": render_button, "input": render_input,
    "param_picker": render_param_picker, "card": render_card, "table": render_table, "list": render_list,
    "alert": render_alert, "progress": render_progress, "metric": render_metric, "code": render_code,
    "image": render_image, "grid": render_grid, "tabs": render_tabs, "divider": render_divider,
    "collapsible": render_collapsible, "bar_chart": render_bar_chart, "line_chart": render_line_chart,
    "pie_chart": render_pie_chart, "plotly_chart": render_plotly_chart, "color_picker": render_color_picker,
    "theme_apply": render_theme_apply, "file_upload": render_file_upload, "file_download": render_file_download,
    "audio": render_audio,
    "badge": render_badge, "hero": render_hero, "keyvalue": render_keyvalue,
    "timeline": render_timeline, "rating": render_rating,
    "skeleton": render_skeleton, "chat_history": render_chat_history,
    "download_card": render_download_card,
})


def render_generative(component: Dict[str, Any]) -> str:
    """Render a model-composed generative widget from its constrained grammar
    ``spec`` — escape-by-default, structurally bounded. Flag-gated: when
    FF_GENERATIVE_PRIMITIVES is off the type renders the standard unsupported
    placeholder (the behavior for an unknown type)."""
    from webrender import generative
    if not generative.generative_enabled():
        return ('<div class="astral-unsupported text-xs text-astral-muted italic '
                'border border-white/10 rounded p-2">[unsupported component: '
                'generative]</div>')
    return generative.render(component.get("spec"))


PRIMITIVE_RENDERERS["generative"] = render_generative


# Public API

def allowed_primitive_types() -> frozenset:
    """The authoritative renderable-type set.

    Single source of truth for every LLM-output validator (combine/condense,
    final-response parsing, the adaptive UI designer): a type the renderer
    registry can render is valid; anything else is not. Hand-copied
    whitelists drift — import this instead.
    """
    return frozenset(PRIMITIVE_RENDERERS.keys())


def render_one(component: Dict[str, Any]) -> str:
    """Render a single primitive dict to an HTML fragment. Never raises on an
    unknown/unsupported type — emits a readable placeholder."""
    if not isinstance(component, dict):
        return ""
    ctype = component.get("type", "")
    fn = PRIMITIVE_RENDERERS.get(ctype)
    if fn is None:
        logger.warning("webrender: no renderer for primitive type %r — placeholder emitted", ctype)
        return (f'<div class="astral-unsupported text-xs text-astral-muted italic border border-white/10 '
                f'rounded p-2">[unsupported component: {esc(ctype) or "unknown"}]</div>')
    try:
        return fn(component)
    except Exception:  # pragma: no cover - defensive; a bad component must not kill the page
        logger.exception("webrender: failed rendering %r", ctype)
        return (f'<div class="astral-render-error text-xs text-red-400 border border-red-500/20 '
                f'rounded p-2">[failed to render {esc(ctype)}]</div>')


def render(components: List[Dict[str, Any]], profile: Any = None) -> str:
    """Render a list of ROTE-adapted primitive dicts into a web HTML fragment."""
    inner = "".join(render_one(c) for c in (components or []) if isinstance(c, dict))
    return f'<div class="dynamic-renderer space-y-3">{inner}</div>'


# Provenance / grounding surfacing

def provenance_enabled() -> bool:
    """FF_PROVENANCE_SURFACING (default ON).

    When on, each top-level canvas component gets a subtle footer showing
    whether its content is GROUNDED (traces to an agent tool result) or
    AI-GENERATED (model-authored designer garnish) — so a hallucinated card no
    longer looks identical to a verified one. Surfaced selectively (skipped on
    decorative types and space/audio-constrained surfaces) per the source's
    "don't overload" guidance. Fail-open: off ⇒ legacy markup unchanged."""
    return os.getenv("FF_PROVENANCE_SURFACING", "true").strip().lower() not in ("0", "false", "no", "off")


#: Decorative / structural types that assert no facts — never footed.
_PROV_SKIP_TYPES = frozenset({"divider", "skeleton"})
#: The canonical server-stamped vocabulary (055 US4, wire-contract §6). The
#: orchestrator stamps exactly these values; anything else on a component is
#: untrusted and re-derived.
_PROV_KINDS = frozenset({"grounded", "estimated", "generated"})


def _subtree_tool_source(comp: Dict[str, Any]) -> str:
    """First ``_source_tool`` found anywhere in the component subtree (itself or
    a nested content/children/tabs descendant), else ''. A designed garnish
    container that wraps tool refs is thus correctly read as grounded."""
    if not isinstance(comp, dict):
        return ""
    st = comp.get("_source_tool")
    if isinstance(st, str) and st.strip():
        return st.strip()
    for key in ("content", "children"):
        nested = comp.get(key)
        if isinstance(nested, list):
            for ch in nested:
                found = _subtree_tool_source(ch)
                if found:
                    return found
    tabs = comp.get("tabs")
    if isinstance(tabs, list):
        for tab in tabs:
            if isinstance(tab, dict) and isinstance(tab.get("content"), list):
                for ch in tab["content"]:
                    found = _subtree_tool_source(ch)
                    if found:
                        return found
    return ""


def provenance_of(component: Dict[str, Any]) -> str:
    """The effective provenance kind for a component.

    The server-stamped ``provenance`` field (055 US4) is authoritative when it
    holds a canonical value — the orchestrator writes it AFTER agent/designer
    output is final and always overwrites agent-supplied values (FR-026).
    Anything else (legacy rows, flag-off deliveries, agent-supplied synonyms —
    deliberately NOT honored so trust cannot be self-upgraded through the
    renderer) falls back to the stamp's own derivation: ``grounded`` when the
    subtree traces to a tool result, else ``generated``.
    """
    if not isinstance(component, dict):
        return "generated"
    stamped = component.get("provenance")
    if isinstance(stamped, str):
        kind = stamped.strip().lower()
        if kind in _PROV_KINDS:
            return kind
    return "grounded" if _subtree_tool_source(component) else "generated"


def _provenance_footer(component: Dict[str, Any]) -> str:
    """Subtle grounding footer for one top-level component (or '' when off /
    decorative)."""
    if not provenance_enabled() or not isinstance(component, dict):
        return ""
    ctype = str(component.get("type", "")).strip().lower()
    if ctype in _PROV_SKIP_TYPES:
        return ""
    # 066 (FR-024): static welcome content is chrome, not model output — an
    # "AI-generated" marker on it would be a false provenance claim.
    _cid = component.get("component_id") or component.get("id") or ""
    if str(_cid).startswith("wel_"):
        return ""
    kind = provenance_of(component)
    if kind == "grounded":
        tool = _subtree_tool_source(component)
        agent = component.get("_source_agent")
        title = ("Data from the %s agent%s" % (agent, f" ({tool})" if tool else "")
                 if agent else "Sourced from a tool result")
        icon, label, tone = "✓", "tool data", "text-green-400/70"
    elif kind == "estimated":
        title = "Estimated / low-confidence value"
        icon, label, tone = "≈", "estimated", "text-yellow-400/70"
    else:
        title = "Written by the assistant — not sourced from a tool"
        icon, label, tone = "✦", "AI-generated", "text-astral-muted/70"
    return (
        f'<div class="astral-provenance astral-provenance--{kind} mt-1 flex justify-end" '
        f'title="{_attr(title)}">'
        f'<span class="inline-flex items-center gap-1 text-[10px] {tone}">'
        f'<span aria-hidden="true">{esc(icon)}</span>'
        f'<span class="astral-provenance-label">{esc(label)}</span></span></div>'
    )


# Component chrome — refine / history / export / share affordances (055 US4+US5)

def _flag_on(env_var: str, default: bool) -> bool:
    """Live-read a feature flag with ``shared.feature_flags._read`` semantics
    (same truthy set + stringified default) so the rendered chrome always
    matches the orchestrator/API gates for the same environment."""
    return os.getenv(env_var, str(default)).lower() in ("true", "1", "yes")


#: Identity prefixes that never take chrome affordances: designer garnish and
#: layout keys are rebuilt from layout JSON (no restorable ``saved_components``
#: row), welcome components are ephemeral by contract (data-model.md identity
#: registry).
_CHROME_SKIP_ID_PREFIXES = ("dg_", "ly_", "wel_")

_CHROME_BTN_CLS = ("inline-flex items-center gap-1 text-[10px] text-astral-muted/70 "
                   "hover:text-astral-text transition-colors")


def _versions_attr(component: Dict[str, Any]) -> str:
    """Bounded, escaped ``data-versions`` payload for the history affordance.

    Reads an optional server-attached ``versions`` list on the component dict
    (the ``artifact_versions.list_versions`` metadata shape). Only
    whitelisted, length-capped scalar fields survive into the attribute, so a
    ``versions`` value smuggled in by an agent cannot inject markup or bloat
    the fragment. Absent/empty history renders no attribute (the client shows
    an honest empty state).
    """
    raw = component.get("versions")
    if not isinstance(raw, list):
        return ""
    entries = []
    for v in raw[:5]:
        if not isinstance(v, dict):
            continue
        try:
            no = int(v.get("version_no"))
        except (TypeError, ValueError):
            continue
        entries.append({
            "version_no": no,
            "reason": str(v.get("reason") or "")[:32],
            "created_at": str(v.get("created_at") or "")[:64],
            "title": str(v.get("title") or "")[:120],
        })
    if not entries:
        return ""
    return f' data-versions="{_attr(json.dumps(entries))}"'


def _component_chrome(component: Dict[str, Any], profile: Any) -> str:
    """Per-component affordance row (055 US4/US5), appended after the
    provenance footer inside the identity wrapper.

    Emitted only for identified, non-decorative components on interactive host
    profiles (``supports_interactivity`` — the same ROTE rule that strips
    action buttons); a ``None`` profile means a static rendition (exports,
    share snapshots, legacy call sites) and gets no chrome. Every entry is
    flag-gated server-side so each off state is byte-identical to pre-055
    markup: refine + history under FF_COMPONENT_REFINE, the CSV link under
    FF_ARTIFACT_EXPORT (tables only), share under FF_ARTIFACT_SHARING
    (fail-closed default off). client.js owns the click behavior
    (``.astral-refine-btn`` / ``.astral-vhistory-btn`` / ``.astral-export-csv``
    / ``.astral-share-btn``).
    """
    if profile is None or not getattr(profile, "supports_interactivity", True):
        return ""
    cid = component.get("component_id")
    if not cid or str(cid).startswith(_CHROME_SKIP_ID_PREFIXES):
        return ""
    ctype = str(component.get("type", "")).strip().lower()
    if ctype in _PROV_SKIP_TYPES:
        return ""
    parts = []
    if _flag_on("FF_COMPONENT_REFINE", True):
        parts.append(
            f'<button type="button" class="astral-refine-btn {_CHROME_BTN_CLS}" '
            f'title="Refine this component with an instruction">'
            f'<span aria-hidden="true">✎</span> refine</button>')
        parts.append(
            f'<button type="button" class="astral-vhistory-btn {_CHROME_BTN_CLS}"'
            f'{_versions_attr(component)} title="Version history">'
            f'<span aria-hidden="true">⟲</span> history</button>')
    if ctype == "table" and _flag_on("FF_ARTIFACT_EXPORT", True):
        href = "/api/export/component/" + quote(str(cid), safe="") + ".csv"
        parts.append(
            f'<a class="astral-export-csv {_CHROME_BTN_CLS}" href="{_attr(href)}" '
            f'title="Download the full table as CSV">'
            f'<span aria-hidden="true">⬇</span> csv</a>')
    if _flag_on("FF_ARTIFACT_SHARING", False):
        parts.append(
            f'<button type="button" class="astral-share-btn {_CHROME_BTN_CLS}" '
            f'data-share-scope="component" '
            f'title="Create a revocable read-only share link">'
            f'<span aria-hidden="true">↗</span> share</button>')
    if not parts:
        return ""
    return ('<div class="astral-component-chrome mt-0.5 flex justify-end gap-3">'
            + "".join(parts) + "</div>")


def _workspace_flag_attrs(profile: Any) -> str:
    """US5 data-flag attributes on the canvas root for the client's toolbar.

    The server is the only party that knows the export/share flags, so it
    stamps them here (``data-astral-export`` / ``data-astral-share``) and
    client.js builds the canvas toolbar from them. Stamped only for
    interactive, non-watch/voice host profiles so static renditions (exports,
    share snapshots, profile-less legacy calls) keep the bare wrapper
    byte-identical.
    """
    if profile is None or not getattr(profile, "supports_interactivity", True):
        return ""
    dtype = getattr(getattr(profile, "device_type", None), "value", "")
    if dtype in ("watch", "voice"):
        return ""
    attrs = ""
    if _flag_on("FF_ARTIFACT_EXPORT", True):
        attrs += ' data-astral-export="1"'
    if _flag_on("FF_ARTIFACT_SHARING", False):
        attrs += ' data-astral-share="1"'
    return attrs


def render_component_fragment(component: Dict[str, Any], profile: Any = None) -> str:
    """Render one top-level workspace component wrapped in its identity anchor.

    The ``data-component-id`` wrapper is the morph target for ``ui_upsert``
    ops on the web client (replace-node-by-id, else append). Components
    without an identity render unwrapped (legacy behavior preserved).

    A subtle provenance footer is appended inside the wrapper — skipped on
    space/audio-constrained surfaces (watch/voice) given the ``profile``, and
    when FF_PROVENANCE_SURFACING is off. The 055 refine/history/export/share
    chrome row follows it (:func:`_component_chrome` — flag- and
    interactivity-gated, so legacy contexts stay byte-identical).
    """
    if not isinstance(component, dict):
        return ""
    cid = component.get("component_id")
    inner = render_one(component)
    dtype = getattr(getattr(profile, "device_type", None), "value", "")
    if dtype not in ("watch", "voice"):
        inner += _provenance_footer(component)
        inner += _component_chrome(component, profile)
    if not cid:
        return inner
    # WCAG-by-construction — wrap each top-level component as a labelled ARIA
    # landmark so a screen reader can navigate between them.
    attrs = f' data-component-id="{_attr(cid)}"'
    from webrender import a11y
    if a11y.a11y_enabled():
        role = a11y.landmark_role(component)
        if role:
            attrs += f' role="{_attr(role)}" aria-label="{_attr(a11y.landmark_label(component))}"'
    return f'<div class="astral-component"{attrs}>{inner}</div>'


def render_workspace(components: List[Dict[str, Any]], profile: Any = None) -> str:
    """Render the full workspace with per-component identity wrappers.

    Used for canvas-targeted full renders (re-hydration, timeline views,
    device re-adapt) so every top-level component remains an upsert target.
    Markup is identical to :func:`render` except for the wrappers and the
    055 US5 export/share data-flags on the root
    (:func:`_workspace_flag_attrs` — absent for static/non-interactive
    renditions and when the flags are off).
    """
    inner = "".join(render_component_fragment(c, profile) for c in (components or []) if isinstance(c, dict))
    return f'<div class="dynamic-renderer space-y-3"{_workspace_flag_attrs(profile)}>{inner}</div>'


# Standalone export document (055 US5, T043 — the render half)

# A deliberately small, self-contained stylesheet for exported documents:
# light/print-friendly, readable tables/cards/lists, dead interactive chrome
# suppressed, and chart placeholders surfaced as their accessible text.
_EXPORT_CSS = (
    ":root{color-scheme:light}"
    "body{margin:0;background:#f8fafc;color:#1e293b;"
    "font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}"
    ".export-head,main,.export-foot{max-width:60rem;margin:0 auto;padding:0 1.25rem}"
    ".export-head{padding-top:2rem}"
    ".export-head h1{margin:0 0 .25rem;font-size:1.5rem;color:#0f172a}"
    ".export-meta{margin:0;color:#64748b;font-size:.8rem}"
    "main{padding-top:1rem;padding-bottom:1rem}"
    ".dynamic-renderer>*{margin:0 0 1rem}"
    "h1,h2,h3,h4{color:#0f172a;margin:.25rem 0}"
    "p{margin:.25rem 0}"
    "table{border-collapse:collapse;width:100%;font-size:.85rem}"
    "th,td{border:1px solid #e2e8f0;padding:.4rem .6rem;text-align:left;vertical-align:top}"
    "thead th{background:#f1f5f9}"
    ".astral-card,.astral-kv,.astral-timeline,.astral-rating,.astral-hero,"
    ".astral-alert,.astral-metric,.audio-component,.astral-download-card"
    "{border:1px solid #e2e8f0;border-radius:.5rem;padding:1rem;background:#fff}"
    ".astral-table-wrap{border:1px solid #e2e8f0;border-radius:.5rem;background:#fff;overflow-x:auto}"
    ".astral-table-wrap>div:first-child{padding:.5rem .75rem;border-bottom:1px solid #e2e8f0;font-weight:600}"
    "pre{background:#0f172a;color:#4ade80;padding:.75rem;border-radius:.5rem;"
    "overflow-x:auto;font-size:.8rem}"
    "code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
    "ul,ol{padding-left:1.25rem}"
    "dl{display:grid;grid-template-columns:repeat(auto-fill,minmax(12rem,1fr));gap:.75rem;margin:0}"
    "dt{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:#64748b}"
    "dd{margin:0;font-weight:600}"
    "hr{border:0;border-top:1px solid #e2e8f0;margin:1rem 0}"
    "a{color:#4f46e5}"
    "img{max-width:100%}"
    ".astral-badge{display:inline-flex;gap:.25rem;padding:.1rem .5rem;"
    "border:1px solid #cbd5e1;border-radius:999px;font-size:.7rem}"
    ".astral-provenance{display:flex;justify-content:flex-end;font-size:.65rem;"
    "color:#64748b;margin-top:.25rem}"
    ".astral-provenance--grounded{color:#15803d}"
    ".astral-provenance--estimated{color:#a16207}"
    ".astral-chart{border:1px dashed #cbd5e1;border-radius:.5rem;padding:.75rem;"
    "color:#475569;font-size:.85rem;min-height:0!important}"
    ".astral-chart::before{content:attr(aria-label)}"
    ".astral-sr-only{display:block;font-size:.75rem;color:#64748b;margin-top:.25rem}"
    "button,.astral-pagination,.astral-component-chrome,.astral-skeleton{display:none!important}"
    ".export-foot{border-top:1px solid #e2e8f0;color:#64748b;font-size:.75rem;"
    "padding-top:.75rem;padding-bottom:2rem;margin-top:1rem}"
)


def render_export_document(components_html: str, title: str,
                           provenance_note: str, generated_at: Any) -> str:
    """Wrap rendered component HTML in a SELF-CONTAINED export document
    (055 US5, T043 — the body of ``GET /api/export/canvas/{chat_id}.html``).

    Returns a complete standalone page with an inline stylesheet only — no
    scripts, no WebSocket, no external asset URLs — so the saved file opens
    offline. Charts should be pre-degraded by the caller (the ROTE
    chart→table fallback ladder via a chart-less profile); any chart
    placeholder that slips through still reads as its accessible text (the
    stylesheet surfaces the ``aria-label`` + sr-only data summary instead of
    the empty Plotly mount). Interactive chrome (buttons, pagination,
    skeletons, the 055 affordance row) is display-suppressed.

    ``components_html`` is trusted renderer output (escape-by-default
    upstream — pass ``render_workspace(...)``/``render(...)`` results only);
    ``title``, ``provenance_note`` and ``generated_at`` are escaped here. An
    empty ``provenance_note`` omits its line; a falsy ``generated_at`` omits
    the date from the footer.
    """
    note_html = (f'<p class="export-meta">{esc(provenance_note)}</p>'
                 if provenance_note else "")
    stamp = f"Generated {esc(generated_at)} by AstralDeep" if generated_at else "Generated by AstralDeep"
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title><style>{_EXPORT_CSS}</style></head><body>'
        f'<header class="export-head"><h1>{esc(title)}</h1>{note_html}</header>'
        f'<main>{components_html or ""}</main>'
        f'<footer class="export-foot">{stamp}</footer>'
        '</body></html>'
    )
