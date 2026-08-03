"""Feature 026 — the narrow, sanitized rich-text opt-in (FR-017 / SC-008).

Everything here is **escape-first**: input is HTML-escaped before any markdown
transform, so untrusted text can never inject markup. Only a small, fixed set of
safe inline/block markdown constructs is then re-introduced as known-safe tags.
This is the ONLY path in the renderer that emits formatting from text content;
all other text goes through ``html.escape`` directly.
"""
from __future__ import annotations

import html
import re
from typing import Any

_ALLOWED_URL = re.compile(r"^(https?://|mailto:|/)", re.IGNORECASE)


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _safe_url(url: str) -> str:
    s = (url or "").strip()
    return s if _ALLOWED_URL.match(s) else "#"


# Inline patterns operate on ALREADY-ESCAPED text (so markup chars are inert).
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_STRIKE = re.compile(r"~~([^~]+)~~")
_EM = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)")

# Block patterns (GFM subset).
_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_TABLE_SEP_CELL = re.compile(r"^:?-+:?$")
_LIST_START = re.compile(r"^(?:[-*]|\d+\.)\s+")
# A table body ends where any other block construct begins (GFM: tables break
# at the start of another block-level structure).
_TABLE_BODY_BREAK = re.compile(r"^(?:#{1,6}\s|>|```|(?:[-*]|\d+\.)\s)")
# GFM's only mechanism for a literal "|" inside a cell is "\|".
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def inline_md(text: Any) -> str:
    """Render a small set of inline markdown on escaped text:
    `code`, [text](url), **bold**/__bold__, ~~strike~~, *em*/_em_."""
    s = _esc(text)
    s = _CODE.sub(lambda m: f'<code class="text-astral-accent bg-white/5 px-1 rounded">{m.group(1)}</code>', s)

    def _link(m):
        label, url = m.group(1), _safe_url(m.group(2))
        # url came from the original (pre-escape) string already escaped by _esc; it's safe in an attr
        return (f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
                f'class="text-astral-primary hover:text-astral-secondary hover:underline">{label}</a>')

    s = _LINK.sub(_link, s)
    s = _BOLD.sub(lambda m: f'<strong class="text-astral-text">{m.group(1) or m.group(2)}</strong>', s)
    s = _STRIKE.sub(lambda m: f"<del>{m.group(1)}</del>", s)
    s = _EM.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", s)
    return s


def _split_table_row(line: str) -> list[str]:
    """Split a GFM pipe-table row into trimmed cell strings (honoring \\|)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in _UNESCAPED_PIPE.split(s)]


def _table_aligns(sep_line: str, n_cols: int):
    """Parse a GFM table delimiter row into per-column alignments.

    Returns a list like ``["left", "center", "right"]`` when ``sep_line`` is a
    valid delimiter row with exactly ``n_cols`` cells, else None (the caller
    falls back to paragraph handling). A delimiter row must contain a pipe —
    a bare dashes line is a thematic break (or setext underline), never a
    table delimiter.
    """
    if "|" not in sep_line:
        return None
    cells = _split_table_row(sep_line)
    if len(cells) != n_cols:
        return None
    aligns = []
    for cell in cells:
        if not _TABLE_SEP_CELL.match(cell):
            return None
        if cell.startswith(":") and cell.endswith(":"):
            aligns.append("center")
        elif cell.endswith(":"):
            aligns.append("right")
        else:
            aligns.append("left")
    return aligns


def _render_md_table(headers: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    """Emit a styled table; every cell goes through ``inline_md`` (escaped)."""
    align_cls = {"left": "text-left", "center": "text-center", "right": "text-right"}
    # a11y (feature 030): GFM tables only have a column-header row, so every
    # <th> carries scope="col".
    ths = "".join(
        f'<th scope="col" class="px-3 py-2 {align_cls[a]} text-xs font-semibold uppercase '
        f'tracking-wider text-astral-muted whitespace-nowrap">{inline_md(h)}</th>'
        for h, a in zip(headers, aligns)
    )
    trs = []
    for row in rows:
        cells = list(row[: len(headers)]) + [""] * (len(headers) - len(row))
        tds = "".join(
            f'<td class="px-3 py-2 {align_cls[a]} text-astral-text align-top">{inline_md(c)}</td>'
            for c, a in zip(cells, aligns)
        )
        trs.append(f'<tr class="border-t border-white/5">{tds}</tr>')
    return (
        '<div class="my-2 overflow-x-auto rounded-lg border border-white/10">'
        f'<table class="w-full text-sm"><thead class="bg-white/5"><tr>{ths}</tr></thead>'
        f'<tbody>{"".join(trs)}</tbody></table></div>'
    )


def block_md(text: Any) -> str:
    """Render a compact, safe subset of block markdown: fenced code blocks,
    ATX headings, unordered/ordered lists, blockquotes, pipe tables,
    horizontal rules, and paragraphs with inline markdown.
    Escape-by-default throughout."""
    if text is None or text == "":
        return ""
    src = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    def flush_para(buf: list[str]):
        if buf:
            joined = "<br>".join(inline_md(b) for b in buf)
            out.append(f'<p class="mb-2">{joined}</p>')
            buf.clear()

    para: list[str] = []
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_para(para)
            lang = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            lang_html = (f'<div class="px-4 py-2 border-b border-white/5 text-xs text-astral-muted">{_esc(lang)}</div>'
                         if lang else "")
            code = _esc("\n".join(code_lines))
            out.append(f'<div class="rounded-lg bg-black/40 border border-white/5 overflow-hidden my-2">{lang_html}'
                       f'<pre class="p-4 text-sm overflow-x-auto"><code class="text-green-400">{code}</code></pre></div>')
            continue

        # headings (ATX, all six levels)
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para(para)
            level = len(m.group(1))
            cls = {1: "text-2xl font-bold text-astral-text",
                   2: "text-xl font-semibold text-astral-text",
                   3: "text-lg font-medium text-astral-text",
                   4: "text-base font-semibold text-astral-text",
                   5: "text-sm font-semibold text-astral-text",
                   6: "text-sm font-medium text-astral-muted"}[level]
            out.append(f'<h{level} class="{cls} mb-2">{inline_md(m.group(2))}</h{level}>')
            i += 1
            continue

        # blockquote
        if stripped.startswith(">"):
            flush_para(para)
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            inner = "<br>".join(inline_md(q) for q in quote_lines)
            out.append(f'<blockquote class="border-l-2 border-astral-primary/40 pl-3 text-astral-text/80 my-2">{inner}</blockquote>')
            continue

        # pipe table: header row + delimiter row (|---|:--:|...) with the
        # same cell count, then body rows until a blank line, a pipe-less
        # line, or the start of another block construct. A list-marker line
        # can never open a table (the list wins, as in GFM).
        if "|" in stripped and i + 1 < n and not _LIST_START.match(stripped):
            headers = _split_table_row(stripped)
            aligns = _table_aligns(lines[i + 1].strip(), len(headers))
            if aligns is not None:
                flush_para(para)
                i += 2
                rows = []
                while (i < n and "|" in lines[i] and lines[i].strip()
                       and not _TABLE_BODY_BREAK.match(lines[i].strip())):
                    rows.append(_split_table_row(lines[i]))
                    i += 1
                out.append(_render_md_table(headers, aligns, rows))
                continue

        # horizontal rule
        if _HR.match(stripped):
            flush_para(para)
            out.append('<hr class="border-white/10 my-3">')
            i += 1
            continue

        # lists
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            flush_para(para)
            ordered = bool(re.match(r"^\d+\.\s+", stripped))
            items = []
            while i < n:
                ls = lines[i].strip()
                mm = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", ls)
                if not mm:
                    break
                items.append(f'<li class="leading-relaxed">{inline_md(mm.group(1))}</li>')
                i += 1
            tag = "ol" if ordered else "ul"
            lcls = "list-decimal" if ordered else "list-disc"
            out.append(f'<{tag} class="space-y-1 text-sm {lcls} list-inside text-astral-text my-2">{"".join(items)}</{tag}>')
            continue

        # blank line ends a paragraph
        if stripped == "":
            flush_para(para)
            i += 1
            continue

        para.append(stripped)
        i += 1

    flush_para(para)
    return "".join(out)


def plain_md(text: Any) -> str:
    """Strip the markdown constructs :func:`block_md` / :func:`inline_md`
    recognize, returning RAW plain text (feature 066).

    The inverse of the renderers, for contexts that show a short excerpt of
    assistant prose as text — chat-list previews — where the markup itself
    was leaking to the user as literal ``**asterisks**``. It reuses the same
    patterns as the renderers so the two cannot drift, and deliberately does
    NOT escape: every consumer escapes at render time (escape-first is
    preserved end to end).
    """
    if text is None or text == "":
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    fenced = False
    for line in s.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if _HR.match(stripped):
            continue
        stripped = re.sub(r"^#{1,6}\s*", "", stripped)   # ATX headings
        stripped = re.sub(r"^>\s?", "", stripped)         # blockquote
        stripped = _LIST_START.sub("", stripped)          # list markers
        if stripped and all(
            _TABLE_SEP_CELL.match(cell.strip())
            for cell in _UNESCAPED_PIPE.split(stripped)
            if cell.strip()
        ):
            continue  # a table's ---|--- separator row
        stripped = _UNESCAPED_PIPE.sub(" ", stripped)     # table cell pipes
        if stripped:
            kept.append(stripped)
    out = " ".join(kept)
    out = _CODE.sub(lambda m: m.group(1), out)
    out = _LINK.sub(lambda m: m.group(1), out)
    out = _BOLD.sub(lambda m: m.group(1) or m.group(2), out)
    out = _STRIKE.sub(lambda m: m.group(1), out)
    out = _EM.sub(lambda m: m.group(1) or m.group(2), out)
    return re.sub(r"\s+", " ", out).strip()
