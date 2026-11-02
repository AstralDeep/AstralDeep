"""Builds, bounds and gates the native `artifact` composition frame: ui_designer.py wraps a
designed round in it and orchestrator.py wraps substantial final answers as document frames.
Every client send path calls gate_components() so clients that cannot render frames or HTML
artifacts receive flattened children or a download instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from shared.feature_flags import flags

logger = logging.getLogger(__name__)

FRAME_TYPE = "artifact"
HTML_TYPE = "html_artifact"
ARTIFACT_TYPES = frozenset({FRAME_TYPE, HTML_TYPE})
CAPABILITY = "artifact_v1"

KINDS = ("report", "dashboard", "document", "app", "analysis")
LAYOUTS = ("stack", "grid-2", "grid-3")
DEFAULT_KIND = "report"
DEFAULT_LAYOUT = "stack"

MAX_SECTIONS = 12
MAX_DEPTH = 3
MAX_NODES = 60
MAX_TITLE_CHARS = 120
MAX_SUBTITLE_CHARS = 200
MAX_SUMMARY_CHARS = 400
MAX_HEADING_CHARS = 80
FALLBACK_TITLE_CHARS = 60
LEFTOVER_HEADING = "Also"

SUBSTANTIAL_TEXT_CHARS = 600
SUBSTANTIAL_STRUCTURES = 2

FRAME_HTML_BUDGET = 768 * 1024

REF_TYPE = "ref"
_CHILD_KEYS = ("children", "content")
_HEADING_VARIANTS = frozenset({"h1", "h2", "h3", "heading", "title"})
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_FENCE = re.compile(r"^\s*(```|~~~)")
_MARKDOWN_MARKS = re.compile(r"[*_`>#]+")


def enabled() -> bool:
    return flags.is_enabled("artifact_frame")


def accepted_types(profile: Any, capabilities: Any) -> frozenset:
    supported = getattr(profile, "supported_types", None)
    if supported is not None:
        return frozenset(t for t in ARTIFACT_TYPES if t in supported)
    offered = capabilities if isinstance(capabilities, (list, tuple, set, frozenset)) else ()
    return ARTIFACT_TYPES if CAPABILITY in offered else frozenset()


def socket_accepted_types(orch: Any, websocket: Any) -> frozenset:
    if websocket is None:
        return frozenset()
    rote = getattr(orch, "rote", None)
    profile = rote.get_profile(websocket) if rote is not None else None
    sessions = getattr(orch, "ui_sessions", None)
    claims = sessions.get(websocket) if isinstance(sessions, dict) else None
    capabilities = claims.get("_client_capabilities") if isinstance(claims, dict) else None
    accepted = accepted_types(profile, capabilities)
    logger.info("artifact.gate accepted=%s supported=%s capability=%s",
                sorted(accepted), getattr(profile, "supported_types", None) is not None,
                CAPABILITY in (capabilities or ()))
    return accepted


def artifact_capable(orch: Any, websocket: Any) -> bool:
    return FRAME_TYPE in socket_accepted_types(orch, websocket)


def frame_id(chat_id: str, key: str) -> str:
    return "art_" + hashlib.sha1(f"{chat_id or ''}|{key or ''}".encode()).hexdigest()[:16]


def is_artifact(component: Any) -> bool:
    return isinstance(component, dict) and component.get("type") in ARTIFACT_TYPES


def has_artifacts(components: Any) -> bool:
    return isinstance(components, list) and any(is_artifact(c) for c in components)


def designable(components: Optional[Sequence[Any]]) -> List[Any]:
    return [c for c in (components or []) if not is_artifact(c)]


def is_frame_layout(layout: Any) -> bool:
    return (isinstance(layout, (list, tuple)) and len(layout) == 1
            and isinstance(layout[0], dict) and layout[0].get("type") == FRAME_TYPE)


def inner_layout(layout: Optional[Sequence[Any]]) -> List[Any]:
    if not is_frame_layout(layout):
        return list(layout or [])
    nodes: List[Any] = []
    for section in layout[0].get("sections") or []:
        if isinstance(section, dict) and isinstance(section.get("children"), list):
            nodes.extend(section["children"])
    return nodes


def iter_nodes(nodes: Iterable[Any]) -> Iterator[Dict[str, Any]]:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        yield node
        for key in _CHILD_KEYS:
            if isinstance(node.get(key), list):
                yield from iter_nodes(node[key])
        for tab in node.get("tabs") or []:
            if isinstance(tab, dict) and isinstance(tab.get("content"), list):
                yield from iter_nodes(tab["content"])
        for section in node.get("sections") or []:
            if isinstance(section, dict) and isinstance(section.get("children"), list):
                yield from iter_nodes(section["children"])


def iter_refs(nodes: Iterable[Any]) -> Iterator[str]:
    for node in iter_nodes(nodes):
        if node.get("type") == REF_TYPE and node.get("component_id"):
            yield str(node["component_id"])


def count_nodes(nodes: Iterable[Any]) -> int:
    return sum(1 for _ in iter_nodes(nodes))


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _within(value: Any, limit: int) -> str:
    text = _one_line(value)
    return text if len(text) <= limit else ""


def _clip(value: Any, limit: int) -> str:
    text = _one_line(value)
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def _plain(text: str) -> str:
    return _one_line(_MARKDOWN_MARKS.sub(" ", text or ""))


def _heading_of(node: Dict[str, Any]) -> str:
    if node.get("type") != "text" or not isinstance(node.get("content"), str):
        return ""
    if str(node.get("variant") or "").lower() in _HEADING_VARIANTS:
        return _within(_plain(node["content"]), MAX_TITLE_CHARS)
    for line in node["content"].splitlines():
        if line.strip():
            match = _MARKDOWN_HEADING.match(line)
            return _within(_plain(match.group(1)), MAX_TITLE_CHARS) if match else ""
    return ""


def _short_text_of(node: Dict[str, Any]) -> str:
    if node.get("type") != "text" or not isinstance(node.get("content"), str):
        return ""
    if len(node["content"].strip().splitlines()) != 1:
        return ""
    return _within(_plain(node["content"]), FALLBACK_TITLE_CHARS)


def fallback_title(nodes: Sequence[Any], user_request: str) -> str:
    flat = list(iter_nodes(nodes))
    for pick in (
        lambda n: _within(n.get("title"), MAX_TITLE_CHARS) if n.get("type") == "hero" else "",
        _heading_of,
    ):
        for node in flat:
            title = pick(node)
            if title:
                return title
    request = _one_line(user_request)[:FALLBACK_TITLE_CHARS].strip()
    if request:
        return request
    for node in flat:
        title = _short_text_of(node)
        if title:
            return title
    return "Response"


def default_actions() -> List[Dict[str, str]]:
    actions = [{"label": "Copy", "action": "copy"}]
    if flags.is_enabled("artifact_export"):
        actions.append({"label": "Export", "action": "export"})
    return actions


def split_sections(data: Any) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    if isinstance(data, list):
        return ({}, [{"children": data}]) if data else None
    if not isinstance(data, dict):
        return None
    meta = {key: data.get(key) for key in ("title", "kind", "subtitle", "summary")}
    raw = data.get("sections")
    if isinstance(raw, list) and raw:
        sections: List[Dict[str, Any]] = []
        for entry in raw:
            if isinstance(entry, list):
                sections.append({"children": entry})
                continue
            if not isinstance(entry, dict):
                continue
            layout = entry.get("layout")
            children = next((entry[key] for key in ("children", "content")
                             if isinstance(entry.get(key), list)), None)
            if children is None and isinstance(layout, list):
                children, layout = layout, None
            if children is None:
                continue
            sections.append({"heading": entry.get("heading") or entry.get("title"),
                             "layout": layout, "children": children})
        return (meta, sections) if sections else None
    layout = data.get("layout")
    if isinstance(layout, list) and layout:
        return meta, [{"children": layout}]
    return None


def _prune_depth(nodes: Sequence[Any], depth: int) -> List[Any]:
    kept: List[Any] = []
    for node in nodes:
        if not isinstance(node, dict) or depth > MAX_DEPTH:
            continue
        if node.get("type") == REF_TYPE:
            kept.append(node)
            continue
        node = dict(node)
        for key in _CHILD_KEYS:
            if isinstance(node.get(key), list):
                node[key] = _prune_depth(node[key], depth + 1)
        if isinstance(node.get("tabs"), list):
            tabs = []
            for tab in node["tabs"]:
                if isinstance(tab, dict) and isinstance(tab.get("content"), list):
                    tab = {**tab, "content": _prune_depth(tab["content"], depth + 1)}
                tabs.append(tab)
            node["tabs"] = tabs
        kept.append(node)
    return kept


def validate_sections(
    sections: Sequence[Dict[str, Any]],
    allowed_refs: Set[str],
    validate: Callable[[List[Any], Set[str]], Tuple[List[Dict[str, Any]], List[str]]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    merged = [dict(s) for s in sections[:MAX_SECTIONS]]
    if len(sections) > MAX_SECTIONS:
        tail: List[Any] = []
        for section in sections[MAX_SECTIONS - 1:]:
            tail.extend(section.get("children") or [])
        merged[-1]["children"] = tail
    seen: Set[str] = set()
    referenced: List[str] = []
    clean_sections: List[Dict[str, Any]] = []
    for section in merged:
        clean, _ = validate(list(section.get("children") or []), set(allowed_refs) - seen)
        clean = _prune_depth(clean, 1)
        refs = list(iter_refs(clean))
        seen.update(refs)
        referenced.extend(refs)
        if not clean:
            continue
        layout = section.get("layout")
        clean_sections.append({
            "heading": _clip(section.get("heading"), MAX_HEADING_CHARS),
            "layout": layout if layout in LAYOUTS else DEFAULT_LAYOUT,
            "children": clean,
        })
    return clean_sections, referenced


def repair_sections(
    sections: List[Dict[str, Any]],
    referenced: Sequence[str],
    round_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    seen = set(referenced)
    missing = [cid for cid in dict.fromkeys(round_ids) if cid not in seen]
    if not missing:
        return sections
    refs = [{"type": REF_TYPE, "component_id": cid} for cid in missing]
    if len(sections) >= MAX_SECTIONS:
        last = dict(sections[-1])
        last["children"] = list(last["children"]) + refs
        return sections[:-1] + [last]
    return sections + [{"heading": LEFTOVER_HEADING, "layout": DEFAULT_LAYOUT, "children": refs}]


def flatten_sections(
    sections: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    layout: List[Dict[str, Any]] = []
    spans: List[Dict[str, Any]] = []
    for section in sections:
        children = list(section.get("children") or [])
        layout.extend(children)
        spans.append({"heading": section.get("heading") or "",
                      "layout": section.get("layout") or DEFAULT_LAYOUT,
                      "count": len(children)})
    return layout, spans


def section_view(meta: Dict[str, Any], spans: Sequence[Dict[str, Any]],
                 layout: Sequence[Any]) -> Dict[str, Any]:
    sections, start = [], 0
    for span in spans:
        children = list(layout[start:start + span["count"]])
        start += span["count"]
        if children:
            sections.append({"id": f"s{len(sections) + 1}", "heading": span["heading"],
                             "layout": span["layout"], "children": children})
    view = {"type": FRAME_TYPE, "title": _one_line(meta.get("title")),
            "kind": _one_line(meta.get("kind")), "sections": sections}
    for key in ("subtitle", "summary"):
        if _one_line(meta.get(key)):
            view[key] = _one_line(meta[key])
    return view


def assemble(
    meta: Dict[str, Any],
    spans: Sequence[Dict[str, Any]],
    layout: Sequence[Any],
    *,
    artifact_id: str,
    user_request: str,
    round_components: Sequence[Any] = (),
) -> Dict[str, Any]:
    view = section_view(meta, spans, layout)
    kind = _one_line(meta.get("kind")).lower()
    frame: Dict[str, Any] = {
        "type": FRAME_TYPE,
        "id": artifact_id,
        "component_id": artifact_id,
        "title": (_within(meta.get("title"), MAX_TITLE_CHARS)
                  or fallback_title(list(layout) + list(round_components), user_request)),
        "kind": kind if kind in KINDS else DEFAULT_KIND,
        "version": 1,
        "sections": view["sections"],
        "actions": default_actions(),
    }
    subtitle = _clip(meta.get("subtitle"), MAX_SUBTITLE_CHARS)
    summary = _clip(meta.get("summary"), MAX_SUMMARY_CHARS)
    if subtitle:
        frame["subtitle"] = subtitle
    if summary:
        frame["summary"] = summary
    return frame


def fallback_frame(
    round_components: Sequence[Any],
    *,
    chat_id: str,
    layout_key: str,
    user_request: str,
) -> Optional[List[Dict[str, Any]]]:
    ids = list(dict.fromkeys(
        str(c["component_id"]) for c in designable(round_components)
        if isinstance(c, dict) and c.get("component_id")))
    if not 2 <= len(ids) <= MAX_NODES:
        return None
    layout = [{"type": REF_TYPE, "component_id": cid} for cid in ids]
    spans = [{"heading": "", "layout": DEFAULT_LAYOUT, "count": len(layout)}]
    return [assemble({}, spans, layout, artifact_id=frame_id(chat_id, layout_key),
                     user_request=user_request, round_components=round_components)]


def substantial_text(text: str) -> bool:
    body = (text or "").strip()
    if len(body) >= SUBSTANTIAL_TEXT_CHARS:
        return True
    structures, in_fence, in_list = 0, False, False
    for line in body.splitlines():
        if _FENCE.match(line):
            if not in_fence:
                structures += 1
            in_fence = not in_fence
            in_list = False
            continue
        if in_fence:
            continue
        if _MARKDOWN_HEADING.match(line):
            structures += 1
            in_list = False
        elif _LIST_ITEM.match(line):
            if not in_list:
                structures += 1
            in_list = True
        elif line.strip():
            in_list = False
    return structures >= SUBSTANTIAL_STRUCTURES


def document_frame(chat_id: str, text: str, *, user_request: str = "") -> Dict[str, Any]:
    body = {"type": "text", "content": text, "variant": "markdown"}
    title = ""
    for line in (text or "").splitlines():
        match = _MARKDOWN_HEADING.match(line)
        if match:
            title = _within(_plain(match.group(1)), MAX_TITLE_CHARS)
            break
    if not title:
        first = next((ln for ln in (text or "").splitlines() if ln.strip()), "")
        title = _within(_plain(first), FALLBACK_TITLE_CHARS)
    artifact_id = "art_" + hashlib.sha1(f"{chat_id or ''}|{text or ''}".encode()).hexdigest()[:16]
    return {
        "type": FRAME_TYPE,
        "id": artifact_id,
        "component_id": artifact_id,
        "title": title or _one_line(user_request)[:FALLBACK_TITLE_CHARS].strip() or "Document",
        "kind": "document",
        "version": 1,
        "sections": [{"id": "s1", "heading": "", "layout": DEFAULT_LAYOUT, "children": [body]}],
        "actions": default_actions(),
    }


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return slug[:60] or "page"


def html_fallback(component: Dict[str, Any],
                  download: Optional[Callable[[Dict[str, Any]], Optional[str]]]) -> Dict[str, Any]:
    title = _within(component.get("title"), MAX_TITLE_CHARS) or "Page"
    base = {key: component[key] for key in ("id", "component_id", "provenance")
            if component.get(key)}
    url = download(component) if download is not None else None
    if url:
        return {**base, "type": "file_download", "label": f"Download {title}",
                "url": url, "filename": f"{_slug(title)}.html"}
    return {**base, "type": "text", "variant": "body",
            "content": f"{title} is an interactive page. Open this conversation in the Astral Mac app to view it."}


def flatten_frame(frame: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    owner = str(frame.get("id") or "")
    for section in frame.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = _one_line(section.get("heading"))
        if heading:
            digest = hashlib.sha1(f"{owner}|{section.get('id') or ''}".encode()).hexdigest()[:12]
            out.append({"type": "text", "content": heading, "variant": "h2", "id": f"dg_{digest}"})
        out.extend(c for c in section.get("children") or [] if isinstance(c, dict))
    return out


def _adapt_frame(frame: Dict[str, Any],
                 adapt: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]]) -> Dict[str, Any]:
    if adapt is None:
        return frame
    sections = []
    for section in frame.get("sections") or []:
        if isinstance(section, dict) and isinstance(section.get("children"), list):
            section = {**section, "children": adapt(section["children"])}
        sections.append(section)
    return {**frame, "sections": sections}


def _html_cost(component: Dict[str, Any]) -> int:
    html = component.get("html")
    return len(json.dumps(html)) if isinstance(html, str) else 0


def gate_components(
    components: Any,
    accepted: Iterable[str],
    *,
    adapt: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
    download: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    html_budget: int = FRAME_HTML_BUDGET,
) -> Any:
    if not has_artifacts(components):
        return components
    accepted = frozenset(accepted)
    keep: Set[int] = set()
    if HTML_TYPE in accepted:
        used = 0
        for index in range(len(components) - 1, -1, -1):
            component = components[index]
            if isinstance(component, dict) and component.get("type") == HTML_TYPE:
                cost = _html_cost(component)
                if not keep or used + cost <= html_budget:
                    keep.add(index)
                    used += cost
    out: List[Any] = []
    for index, component in enumerate(components):
        kind = component.get("type") if isinstance(component, dict) else None
        if kind == FRAME_TYPE:
            if FRAME_TYPE in accepted:
                out.append(_adapt_frame(component, adapt))
            else:
                out.extend(flatten_frame(component))
        elif kind == HTML_TYPE:
            out.append(component if index in keep else html_fallback(component, download))
        else:
            out.append(component)
    return out


def gate_history(
    components: Any,
    accepted: Iterable[str],
    *,
    download: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
) -> Any:
    if not has_artifacts(components):
        return components
    accepted = frozenset(accepted) - {HTML_TYPE}
    return gate_components(components, accepted, download=download)


def flatten_for_export(components: Any) -> Any:
    return gate_components(components, frozenset())
