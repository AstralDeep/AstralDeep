"""Runs a bounded, fail-open LLM conversation that arranges a chat round's rich
components into a ref-based layout tree (never rewriting tool output);
orchestrator.py invokes design_round() when 2+ components render.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("orchestrator.ui_designer")

MIN_DESIGN_COMPONENTS = 2

DEFAULT_TIMEOUT_SECONDS = 8.0

DEFAULT_MAX_ROUNDS = 1

REF_TYPE = "ref"
GARNISH_ID_PREFIX = "dg_"

_CHILD_KEYS = ("children", "content")

_MAX_REQUEST_CHARS = 1000
_MAX_COMPONENT_EXCERPT_CHARS = 1500
_MAX_CANVAS_LINES = 30
_MAX_SKETCH_LINES = 60
_MAX_LAYOUT_JSON_CHARS = 4000


def designer_enabled() -> bool:
    return os.getenv("FF_UI_DESIGNER", "true").strip().lower() not in ("0", "false", "no", "off")


def designer_timeout_seconds() -> float:
    raw = os.getenv("UI_DESIGNER_TIMEOUT_SECONDS", "")
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def designer_max_rounds() -> int:
    raw = os.getenv("UI_DESIGNER_MAX_ROUNDS", "")
    try:
        value = int(raw)
        return value if value >= 1 else DEFAULT_MAX_ROUNDS
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS


def scorer_enabled() -> bool:
    return os.getenv("FF_UI_DESIGNER_SCORER", "true").strip().lower() not in ("0", "false", "no", "off")


def lint_enabled() -> bool:
    return os.getenv("FF_UI_DESIGNER_LINT", "true").strip().lower() not in ("0", "false", "no", "off")


def conservative_enabled() -> bool:
    return os.getenv("FF_UI_DESIGNER_CONSERVATIVE", "true").strip().lower() not in ("0", "false", "no", "off")


def archetype_enabled() -> bool:
    return os.getenv("FF_UI_DESIGNER_ARCHETYPE", "true").strip().lower() not in ("0", "false", "no", "off")


def objectives_bias_enabled() -> bool:
    try:
        from rote import objectives as _obj
        return _obj.objectives_enabled()
    except Exception:
        return False


def a11y_audit_enabled() -> bool:
    return os.getenv("FF_UI_DESIGNER_A11Y", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def adopt_margin() -> float:
    raw = os.getenv("UI_DESIGNER_ADOPT_MARGIN", "")
    try:
        value = float(raw)
        return value if value >= 0 else 0.5
    except (TypeError, ValueError):
        return 0.5


def should_design(round_components: Sequence[Dict[str, Any]], *, timeline_mode: bool = False) -> bool:
    if timeline_mode or not designer_enabled():
        return False
    rich = [c for c in round_components if isinstance(c, dict) and c.get("type")]
    return len(rich) >= MIN_DESIGN_COMPONENTS


_LAYOUT_GUIDANCE ="""LAYOUT VOCABULARY (compose freely from these astralprims types):
- {"type": "ref", "component_id": "<id>"} — places one of the round's components. THE ONLY way to include tool output.
- "hero": {"type": "hero", "title": "...", "subtitle": "...", "eyebrow": "...", "variant": "default|gradient|subtle", "badges": ["..."]} — ONE anchoring masthead at the top of dashboard/report rounds.
- "grid": {"type": "grid", "columns": 2, "children": [...]} — side-by-side groups (2-3 columns max).
- "card": {"type": "card", "title": "...", "content": [...]} — titled grouping with an accent bar.
- "tabs": {"type": "tabs", "tabs": [{"label": "...", "content": [...]}]} — alternate views of related detail.
- "collapsible": {"type": "collapsible", "title": "...", "content": [...], "default_open": false} — secondary detail.
- "text": {"type": "text", "content": "...", "variant": "h2|h3|body|caption|markdown"} — short connective narrative.
- "metric": {"type": "metric", "title": "...", "value": "...", "subtitle": "...", "variant": "default|success|warning|error"} — headline takeaway.
- "keyvalue": {"type": "keyvalue", "title": "...", "items": [{"label": "...", "value": "...", "hint": "..."}], "columns": 2} — compact fact sheet.
- "timeline": {"type": "timeline", "title": "...", "items": [{"time": "...", "title": "...", "description": "...", "variant": "default|success|warning|error|info"}]} — schedules and event sequences.
- "badge": {"type": "badge", "label": "...", "variant": "default|success|warning|error|info|accent"} — small status chip.
- "rating": {"type": "rating", "value": 4.5, "max_value": 5, "label": "..."} — star score.
- "alert", "divider", "list", "progress" — as in the standard palette.

DESIGN RULES:
1. Reference EVERY round component exactly once via a "ref" node. Never copy, rewrite, or summarize their internal data — place them.
2. Lead with a visual anchor: a hero masthead for dashboard/report rounds, or a headline metric/one-line takeaway when the data supports one.
3. Establish hierarchy: headline strip first (metrics/ratings in a 2-4 column grid), main visuals next (charts/tables side-by-side in grids, max 3 columns), raw or secondary detail last (collapsibles/tabs).
4. Give every card/tabs/collapsible a meaningful title — small screens flatten layouts to titles and text.
5. Garnish must be brief and grounded ONLY in the component digests shown; never invent numbers or facts.
6. Vary the texture: never stack a run of same-type components; break them up with metrics, badges, keyvalue rows or grids.
7. Output ONLY JSON: {"layout": [ ...nodes... ]}. No markdown fences, no commentary.
8. If the components genuinely cannot be improved by arrangement, reply exactly: ERROR: <brief reason>."""


_REFINE_GUIDANCE = """REVIEW CHECKLIST — judge the CURRENT ARRANGEMENT against it:
1. Anchor — does the round open with a hero masthead or headline takeaway?
2. Hierarchy — headline strip, then main visuals, then secondary detail; is anything important buried, or anything trivial leading?
3. Balance — are charts/tables grouped side-by-side in grids instead of one full-width stack? Do grids avoid lonely single cells?
4. Scannability — meaningful titles everywhere; garnish brief and grounded in the digests?
5. Texture — are runs of identical component types broken up?

RESPONSE FORMAT:
- If the arrangement can be meaningfully improved, output ONLY the complete improved JSON {"layout": [ ...nodes... ]} — same rules as before: reference every round component exactly once via "ref" nodes, garnish grounded only in the digests.
- Keep EVERY component the current arrangement references (including any marked "(canvas)") — an improved layout that loses one is rejected.
- If the arrangement is already well-designed, reply exactly: DONE"""


def _component_digest(comp: Dict[str, Any]) -> str:
    cid = comp.get("component_id", "?")
    public = {k: v for k, v in comp.items() if not str(k).startswith("_")}
    excerpt = json.dumps(public, default=str)
    if len(excerpt) > _MAX_COMPONENT_EXCERPT_CHARS:
        excerpt = excerpt[:_MAX_COMPONENT_EXCERPT_CHARS] + "…(truncated)"
    return (
        f"- component_id: {cid} | type: {comp.get('type', '?')} | "
        f"title: {comp.get('title') or '—'} | tool: {comp.get('_source_tool', '?')}"
        f" (agent {comp.get('_source_agent', '?')})\n  data: {excerpt}"
    )


def build_design_messages(
    user_request: str,
    round_components: Sequence[Dict[str, Any]],
    canvas_rows: Sequence[Dict[str, Any]],
    allowed_types: Set[str],
    archetype: Optional[str] = None,
    task_prior: str = "",
) -> List[Dict[str, str]]:
    request = (user_request or "").strip()
    if len(request) > _MAX_REQUEST_CHARS:
        request = request[:_MAX_REQUEST_CHARS] + "…"

    digests = "\n".join(_component_digest(c) for c in round_components if isinstance(c, dict))

    canvas_lines = []
    round_ids = {c.get("component_id") for c in round_components if isinstance(c, dict)}
    for row in list(canvas_rows or [])[:_MAX_CANVAS_LINES]:
        cid = row.get("component_id")
        if not cid or cid in round_ids:
            continue
        canvas_lines.append(
            f"- component_id: {cid} | title: {row.get('title') or '—'} | type: {row.get('component_type', '?')}"
        )
    canvas_block = (
        "\nALREADY ON THE CANVAS (context only — do NOT reference unless the user asked to rearrange them):\n"
        + "\n".join(canvas_lines) + "\n"
    ) if canvas_lines else ""

    palette = ", ".join(sorted(allowed_types))
    prior = archetype_prior(archetype)
    prior_block = f"\nTASK SHAPE: {prior}\n" if prior else ""
    task_block = f"\n{task_prior.strip()}\n" if task_prior and task_prior.strip() else ""
    user_prompt = f"""USER REQUEST FOR THIS ROUND:
{request or '(not provided)'}

THIS ROUND'S COMPONENTS (place each exactly once via a ref node):
{digests}
{canvas_block}
FULL ALLOWED TYPE PALETTE: {palette}
{prior_block}{task_block}
{_LAYOUT_GUIDANCE}"""

    return [
        {
            "role": "system",
            "content": (
                "You are an expert interface designer for a server-driven UI. "
                "You arrange pre-built components into one cohesive, scannable round of UI. "
                "Output ONLY valid JSON or an ERROR message."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]


def _layout_sketch(
    layout: Sequence[Any],
    labels_by_id: Dict[str, str],
    indent: int = 0,
) -> List[str]:
    lines: List[str] = []
    for node in layout:
        if not isinstance(node, dict):
            continue
        pad = "  " * indent
        ntype = str(node.get("type", "?"))
        if ntype == REF_TYPE:
            cid = str(node.get("component_id") or "?")
            lines.append(f"{pad}- [component {cid}] {labels_by_id.get(cid, 'unknown')}")
            continue
        bits = [ntype]
        if node.get("title"):
            bits.append(f'title="{str(node["title"])[:60]}"')
        if ntype == "grid":
            bits.append(f"columns={node.get('columns', 2)}")
        if ntype == "text":
            bits.append(f'content="{str(node.get("content", ""))[:80]}"')
        if ntype == "metric":
            bits.append(f'value="{str(node.get("value", ""))[:40]}"')
        lines.append(f"{pad}- {' '.join(bits)}")
        for key in _CHILD_KEYS:
            nested = node.get(key)
            if isinstance(nested, list):
                lines.extend(_layout_sketch(nested, labels_by_id, indent + 1))
        tabs = node.get("tabs")
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict):
                    lines.append(f'{pad}  - tab "{str(tab.get("label", ""))[:60]}"')
                    content = tab.get("content")
                    if isinstance(content, list):
                        lines.extend(_layout_sketch(content, labels_by_id, indent + 2))
    return lines


def build_refine_messages(
    user_request: str,
    current_layout: Sequence[Dict[str, Any]],
    round_components: Sequence[Dict[str, Any]],
    canvas_rows: Sequence[Dict[str, Any]],
    allowed_types: Set[str],
    archetype: Optional[str] = None,
    task_prior: str = "",
) -> List[Dict[str, str]]:
    request = (user_request or "").strip()
    if len(request) > _MAX_REQUEST_CHARS:
        request = request[:_MAX_REQUEST_CHARS] + "…"

    digests = "\n".join(_component_digest(c) for c in round_components if isinstance(c, dict))

    labels_by_id = {
        str(r.get("component_id")): f"{r.get('component_type', '?')} — {r.get('title') or 'untitled'} (canvas)"
        for r in (canvas_rows or [])
        if isinstance(r, dict) and r.get("component_id")
    }
    labels_by_id.update({
        str(c.get("component_id")): f"{c.get('type', '?')} — {c.get('title') or 'untitled'}"
        for c in round_components
        if isinstance(c, dict) and c.get("component_id")
    })
    sketch_lines = _layout_sketch(current_layout, labels_by_id)
    if len(sketch_lines) > _MAX_SKETCH_LINES:
        sketch_lines = sketch_lines[:_MAX_SKETCH_LINES] + ["  …(truncated)"]
    layout_json = json.dumps(list(current_layout), default=str)
    if len(layout_json) > _MAX_LAYOUT_JSON_CHARS:
        layout_json = layout_json[:_MAX_LAYOUT_JSON_CHARS] + "…(truncated)"

    palette = ", ".join(sorted(allowed_types))
    prior = archetype_prior(archetype)
    prior_block = f"\nTASK SHAPE: {prior}\n" if prior else ""
    task_block = f"\n{task_prior.strip()}\n" if task_prior and task_prior.strip() else ""
    user_prompt = f"""USER REQUEST FOR THIS ROUND:
{request or '(not provided)'}

THIS ROUND'S COMPONENTS (each must appear exactly once via a ref node):
{digests}

CURRENT ARRANGEMENT (structural outline):
{chr(10).join(sketch_lines)}

CURRENT ARRANGEMENT (JSON):
{layout_json}

FULL ALLOWED TYPE PALETTE: {palette}
{prior_block}{task_block}
{_LAYOUT_GUIDANCE}

{_REFINE_GUIDANCE}"""

    return [
        {
            "role": "system",
            "content": (
                "You are an expert interface designer for a server-driven UI. "
                "You critique your own arrangements and improve them. "
                "Output ONLY valid JSON, the single word DONE, or an ERROR message."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]


def _is_done_reply(content: str) -> bool:
    text = (content or "").strip()
    if text.startswith("```"):
        text = "\n".join(
            ln for ln in text.splitlines() if not ln.strip().startswith("```")
        ).strip()
    return text.upper().rstrip(".!").strip() == "DONE"


class DesignRejected(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


def parse_design_response(content: str) -> List[Any]:
    text = (content or "").strip()
    if not text:
        raise DesignRejected("empty")
    if text.upper().startswith("ERROR"):
        detail = text.split(":", 1)[1].strip() if ":" in text else text
        raise DesignRejected("refusal", detail)
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if not match:
            raise DesignRejected("parse", "no JSON found in design response")
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError as e:
            raise DesignRejected("parse", str(e))
    if isinstance(data, dict):
        layout = data.get("layout")
    else:
        layout = data
    if not isinstance(layout, list) or not layout:
        raise DesignRejected("invalid", "missing or empty 'layout' array")
    return layout


def _coerce_node(node: Any) -> Optional[Dict[str, Any]]:
    if isinstance(node, dict):
        return node
    if isinstance(node, str) and node.strip():
        return {"type": "text", "content": node, "variant": "body"}
    return None


# Refs inside non-structural types never reach the DOM — must strip them
_STRUCTURAL_TYPES = frozenset({"container", "card", "grid", "collapsible"})


def validate_layout(
    layout: List[Any],
    allowed_ref_ids: Set[str],
    allowed_types: Set[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    seen_refs: List[str] = []

    def walk(node: Any) -> Optional[Dict[str, Any]]:
        node = _coerce_node(node)
        if node is None:
            return None
        node = dict(node)
        ntype = str(node.get("type", "")).strip().lower()
        if ntype == REF_TYPE:
            cid = str(node.get("component_id") or "")
            if not cid or cid not in allowed_ref_ids:
                logger.info("ui_designer: dropping ref to unknown component %r", cid)
                return None
            if cid in seen_refs:
                logger.info("ui_designer: dropping duplicate ref %r (first wins)", cid)
                return None
            seen_refs.append(cid)
            return {"type": REF_TYPE, "component_id": cid}
        if ntype == "chart":
            node["type"] = ntype = "plotly_chart"
        if ntype and ntype not in allowed_types:
            logger.info("ui_designer: rewriting unknown type %r -> container", ntype)
            node["type"] = ntype = "container"
        structural = ntype in _STRUCTURAL_TYPES or not ntype
        for key in _CHILD_KEYS:
            nested = node.get(key)
            if not isinstance(nested, list):
                continue
            if structural:
                node[key] = [w for w in (walk(c) for c in nested) if w is not None]
            else:
                logger.info("ui_designer: stripping nested %s from leaf type %r", key, ntype)
                node.pop(key)
        tabs = node.get("tabs")
        if isinstance(tabs, list):
            if ntype != "tabs":
                logger.info("ui_designer: stripping tabs from non-tabs type %r", ntype)
                node.pop("tabs")
            else:
                new_tabs = []
                for tab in tabs:
                    if isinstance(tab, dict):
                        tab = dict(tab)
                        content = tab.get("content")
                        if isinstance(content, list):
                            tab["content"] = [w for w in (walk(c) for c in content) if w is not None]
                        new_tabs.append(tab)
                node["tabs"] = new_tabs
        return node

    clean = [w for w in (walk(n) for n in layout) if w is not None]
    return clean, seen_refs


def repair_layout(
    layout: List[Dict[str, Any]],
    referenced: Sequence[str],
    round_ids_in_order: Sequence[str],
) -> List[Dict[str, Any]]:
    seen = set(referenced)
    missing = [cid for cid in dict.fromkeys(round_ids_in_order) if cid not in seen]
    if missing:
        logger.info("ui_designer: repairing layout — appending %d omitted component(s): %s",
                    len(missing), missing)
        layout = list(layout) + [{"type": REF_TYPE, "component_id": cid} for cid in missing]
    return layout


def stamp_garnish_ids(layout: List[Dict[str, Any]], chat_id: str, layout_key: str) -> List[Dict[str, Any]]:
    stamped = []
    for ordinal, node in enumerate(layout):
        node = dict(node)
        if node.get("type") != REF_TYPE:
            gid = GARNISH_ID_PREFIX + hashlib.sha1(
                f"{chat_id}|{layout_key}|{ordinal}".encode()
            ).hexdigest()[:12]
            node["id"] = gid
            attrs = dict(node.get("attributes") or {})
            attrs["data-component-id"] = gid
            node["attributes"] = attrs
        stamped.append(node)
    return stamped


_ANCHOR_TYPES = frozenset({"hero", "metric"})
_HEADLINE_TYPES = frozenset({"metric", "rating", "badge"})
_SCORE_CONTAINERS = frozenset({"grid", "card", "tabs", "collapsible", "container"})
_TITLED_CONTAINERS = frozenset({"card", "tabs", "collapsible"})

W_ANCHOR = 2.0
W_HEADLINE_NEAR_TOP = 1.0
W_GRID_GROUPED = 0.5
W_GRID_LONELY = -1.0
W_TITLED_CONTAINER = 0.25
W_UNTITLED_CONTAINER = -0.75
W_SAME_TYPE_ADJACENT = -0.5
W_WALL_OF_COMPONENTS = -1.5
W_GROUPING_PRESENT = 0.5
WALL_THRESHOLD = 6


ARCHETYPES = ("compare", "monitor", "explore", "summarize", "decide", "form")

_ARCHETYPE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "compare": ("compare", "comparison", "versus", " vs ", " vs.", "difference",
                "differ", "side by side", "side-by-side", "against", "benchmark",
                "head to head", "head-to-head", "which is better", "better than"),
    "monitor": ("monitor", "status", "dashboard", "health", "uptime", "live",
                "real-time", "real time", "realtime", "track", "metrics", "kpi",
                "current state", "at a glance"),
    "explore": ("explore", "browse", "list all", "show all", "all the", "find ",
                "look through", "what are", "which ", "discover", "catalog"),
    "summarize": ("summarize", "summary", "overview", "tl;dr", "tldr", "brief",
                  "recap", "key points", "key takeaways", "in short", "gist",
                  "high level", "high-level"),
    "decide": ("should i", "recommend", "recommendation", "decide", "decision",
               "choose", "which should", "best option", "pros and cons",
               "trade-off", "tradeoff", "worth it", "or should"),
    "form": ("create a", "add a", "edit ", "fill ", "a form", "input ",
             "configure", "set up", "setup", "enter ", "register", "sign up",
             "sign-up", "update my", "change my"),
}

W_ARCH_COMPARE_GRID = 1.0
W_ARCH_MONITOR_ANCHOR = 1.0
W_ARCH_EXPLORE_CONTAINER = 0.75
W_ARCH_SUMMARIZE_LEAD = 1.0
W_ARCH_SUMMARIZE_SPRAWL = -0.75
W_ARCH_DECIDE_ANCHOR = 1.0
W_ARCH_FORM_SINGLE_COL = 1.0
W_ARCH_FORM_MULTICOL = -1.0

_DASHBOARDISH = frozenset({"metric", "hero", "gauge", "keyvalue", "badge"})
_DATAVIEWISH = frozenset({"table", "bar_chart", "line_chart", "pie_chart", "plotly_chart"})

_ARCHETYPE_PRIORS = {
    "compare": "This looks like a COMPARE task: place the key items side by side "
               "(e.g. a 2-column grid) so they can be scanned against each other.",
    "monitor": "This looks like a MONITOR task: lead with the headline metric(s) as "
               "an anchor row at the top, dashboard-style, with detail beneath.",
    "explore": "This looks like an EXPLORE task: group related items into titled "
               "containers or tabs so the user can browse them without a wall of cards.",
    "summarize": "This looks like a SUMMARIZE task: lead with the single takeaway and "
                 "keep the arrangement tight and scannable.",
    "decide": "This looks like a DECIDE task: surface the recommendation up top, with "
              "the supporting options or trade-offs grouped beneath it.",
    "form": "This looks like a FORM/INPUT task: use a single vertical column with "
            "clearly titled sections; avoid multi-column grids.",
}


def classify_archetype(
    user_request: Optional[str],
    components: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[str]:
    text = f" {(user_request or '').lower()} "
    scores: Dict[str, int] = {a: 0 for a in ARCHETYPES}
    for arch, kws in _ARCHETYPE_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                scores[arch] += 1
    types = [str(c.get("type", "")).strip().lower()
             for c in (components or []) if isinstance(c, dict)]
    if sum(1 for t in types if t in _DASHBOARDISH) >= 2:
        scores["monitor"] += 1
    if sum(1 for t in types if t in _DATAVIEWISH) >= 2:
        scores["compare"] += 1
    if 0 < len(types) <= 1:
        scores["summarize"] += 1
    best = max(ARCHETYPES, key=lambda a: scores[a])
    return best if scores[best] > 0 else None


def archetype_prior(archetype: Optional[str]) -> str:
    return _ARCHETYPE_PRIORS.get(archetype or "", "")


def archetype_bonus(
    layout: Sequence[Any],
    archetype: Optional[str],
) -> float:
    if not archetype or not isinstance(layout, list) or not layout:
        return 0.0
    top = [n for n in layout if isinstance(n, dict)]
    if not top:
        return 0.0
    top_types = [str(n.get("type", "")).strip().lower() for n in top]
    first = top_types[0] if top_types else ""
    bonus = 0.0
    if archetype == "compare":
        for n in top:
            if str(n.get("type", "")).strip().lower() == "grid":
                kids = [c for c in (n.get("children") or []) if isinstance(c, dict)]
                if len(kids) >= 2:
                    bonus += W_ARCH_COMPARE_GRID
                    break
    elif archetype == "monitor":
        if first in _ANCHOR_TYPES:
            bonus += W_ARCH_MONITOR_ANCHOR
    elif archetype == "explore":
        if any(t in _TITLED_CONTAINERS for t in top_types):
            bonus += W_ARCH_EXPLORE_CONTAINER
    elif archetype == "summarize":
        if first in ("text", "hero", "alert"):
            bonus += W_ARCH_SUMMARIZE_LEAD
        if len(top) > 3:
            bonus += W_ARCH_SUMMARIZE_SPRAWL
    elif archetype == "decide":
        if first in (_ANCHOR_TYPES | {"alert", "text"}):
            bonus += W_ARCH_DECIDE_ANCHOR
    elif archetype == "form":
        multicol = any(
            str(n.get("type", "")).strip().lower() == "grid"
            and int(n.get("columns", 2) or 2) >= 2
            for n in top
        )
        bonus += W_ARCH_FORM_MULTICOL if multicol else W_ARCH_FORM_SINGLE_COL
    return round(bonus, 4)


def _texture_key(node: Dict[str, Any], ref_types: Optional[Dict[str, str]]) -> str:
    t = str(node.get("type", "")).strip().lower()
    if t == REF_TYPE:
        cid = str(node.get("component_id") or "")
        rt = (ref_types or {}).get(cid)
        return f"ref:{rt}" if rt else f"ref:{cid or id(node)}"
    return t


def _score_siblings(
    siblings: Sequence[Any],
    ref_types: Optional[Dict[str, str]],
    acc: List[float],
) -> None:
    prev_key: Optional[str] = None
    for node in siblings:
        if not isinstance(node, dict):
            prev_key = None
            continue
        ntype = str(node.get("type", "")).strip().lower()
        key = _texture_key(node, ref_types)
        if prev_key is not None and key == prev_key:
            acc[0] += W_SAME_TYPE_ADJACENT
        prev_key = key

        if ntype == "grid":
            children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
            acc[0] += W_GRID_GROUPED if len(children) >= 2 else W_GRID_LONELY
        if ntype in _TITLED_CONTAINERS:
            title = node.get("title")
            acc[0] += W_TITLED_CONTAINER if (isinstance(title, str) and title.strip()) else W_UNTITLED_CONTAINER

        for ckey in _CHILD_KEYS:
            nested = node.get(ckey)
            if isinstance(nested, list):
                _score_siblings(nested, ref_types, acc)
        tabs = node.get("tabs")
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict) and isinstance(tab.get("content"), list):
                    _score_siblings(tab["content"], ref_types, acc)


def score_arrangement(
    layout: Sequence[Any],
    *,
    ref_types: Optional[Dict[str, str]] = None,
    archetype: Optional[str] = None,
) -> float:
    if not isinstance(layout, list) or not layout:
        return 0.0
    top = [n for n in layout if isinstance(n, dict)]
    if not top:
        return 0.0
    acc: List[float] = [0.0]

    first_type = str(top[0].get("type", "")).strip().lower()
    if first_type in _ANCHOR_TYPES:
        acc[0] += W_ANCHOR
    for node in top[:2]:
        if str(node.get("type", "")).strip().lower() in _HEADLINE_TYPES:
            acc[0] += W_HEADLINE_NEAR_TOP
            break
    if any(str(n.get("type", "")).strip().lower() in ("grid", "card") for n in top):
        acc[0] += W_GROUPING_PRESENT
    has_structural_top = any(
        str(n.get("type", "")).strip().lower() in _SCORE_CONTAINERS for n in top
    )
    if len(top) > WALL_THRESHOLD and not has_structural_top:
        acc[0] += W_WALL_OF_COMPONENTS

    _score_siblings(top, ref_types, acc)
    if archetype:
        acc[0] += archetype_bonus(layout, archetype)
    return round(acc[0], 4)


W_OBJECTIVES_BIAS = 2.0


def _objective_node_types(
    layout: Sequence[Any],
    ref_types: Dict[str, str],
) -> List[str]:
    types: List[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        ntype = str(node.get("type", "")).strip().lower()
        if ntype == REF_TYPE:
            cid = str(node.get("component_id") or "")
            types.append(ref_types.get(cid, ""))
            return
        types.append(ntype)
        for key in _CHILD_KEYS:
            nested = node.get(key)
            if isinstance(nested, list):
                for child in nested:
                    walk(child)
        tabs = node.get("tabs")
        if isinstance(tabs, list):
            for tab in tabs:
                if isinstance(tab, dict) and isinstance(tab.get("content"), list):
                    for child in tab["content"]:
                        walk(child)

    for n in layout:
        walk(n)
    return types


def objectives_bias(
    layout: Sequence[Any],
    device: Optional[Dict[str, Any]],
    ref_types: Optional[Dict[str, str]] = None,
) -> float:
    try:
        from rote import objectives as _obj
    except Exception:
        return 0.0
    ref_types = ref_types or {}
    scores: List[float] = []
    for ctype in _objective_node_types(layout, ref_types):
        try:
            scores.append(_obj.score_adaptation({"type": ctype}, device))
        except Exception:
            continue
    if not scores:
        return 0.0
    return round(W_OBJECTIVES_BIAS * (sum(scores) / len(scores)), 4)


def should_adopt(
    new_layout: Sequence[Dict[str, Any]],
    current_layout: Optional[Sequence[Dict[str, Any]]],
    *,
    ref_types: Optional[Dict[str, str]] = None,
    margin: Optional[float] = None,
) -> bool:
    if not current_layout:
        return True
    try:
        if set(iter_refs(new_layout)) != set(iter_refs(current_layout)):
            return True
    except Exception:
        return True
    m = adopt_margin() if margin is None else margin
    try:
        new_score = score_arrangement(new_layout, ref_types=ref_types)
        cur_score = score_arrangement(current_layout, ref_types=ref_types)
    except Exception:
        return True
    return new_score > cur_score + m


_DARK_PATTERN_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("false_urgency", re.compile(
        r"\b(?:act now|act fast|hurry|limited[- ]time(?:\s+offer)?|last chance|"
        r"ends?\s+(?:in|today|soon)|ending soon|don'?t miss(?:\s+out)?|"
        r"while supplies last|today only|buy now before|countdown)\b", re.I)),
    ("forced_scarcity", re.compile(
        r"\bonly\s+\d+\s+(?:left|remaining|in stock|spots?\s+left)\b", re.I)),
    ("confirmshaming", re.compile(
        r"\bno,?\s*(?:thanks,?\s*)?i\s+(?:don'?t|do not)\s+(?:want|need|care)\b", re.I)),
]

_GARNISH_TEXT_KEYS = ("title", "subtitle", "eyebrow", "content", "label",
                      "value", "hint", "description")


def lint_arrangement(layout: Sequence[Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    flags: List[Dict[str, str]] = []

    def _scrub(text: str, where: str) -> str:
        out = text
        for name, rx in _DARK_PATTERN_RULES:
            if rx.search(out):
                flags.append({"rule": name, "where": where})
                out = rx.sub("", out)
        return re.sub(r"\s{2,}", " ", out).strip()

    def walk(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        if node.get("type") == REF_TYPE:
            return node
        node = dict(node)
        for key in _GARNISH_TEXT_KEYS:
            if isinstance(node.get(key), str):
                node[key] = _scrub(node[key], key)
        if isinstance(node.get("badges"), list):
            node["badges"] = [
                _scrub(b, "badges") if isinstance(b, str) else b for b in node["badges"]
            ]
        for ckey in _CHILD_KEYS:
            nested = node.get(ckey)
            if isinstance(nested, list):
                node[ckey] = [walk(c) for c in nested]
        tabs = node.get("tabs")
        if isinstance(tabs, list):
            new_tabs = []
            for tab in tabs:
                if isinstance(tab, dict) and isinstance(tab.get("content"), list):
                    tab = dict(tab)
                    tab["content"] = [walk(c) for c in tab["content"]]
                new_tabs.append(tab)
            node["tabs"] = new_tabs
        return node

    cleaned = [walk(n) for n in (layout or [])]
    return cleaned, flags


def materialize(
    layout: Sequence[Dict[str, Any]],
    components_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    def walk(node: Any, depth: int) -> Optional[Dict[str, Any]]:
        if not isinstance(node, dict):
            return node if node else None
        if node.get("type") == REF_TYPE:
            cid = str(node.get("component_id") or "")
            comp = components_by_id.get(cid)
            if not isinstance(comp, dict):
                logger.info("ui_designer: ref %r has no live component — dropped", cid)
                return None
            comp = copy.deepcopy(comp)
            comp["component_id"] = cid
            if depth > 0:
                attrs = dict(comp.get("attributes") or {})
                attrs["data-component-id"] = cid
                comp["attributes"] = attrs
            return comp
        node = dict(node)
        for key in _CHILD_KEYS:
            nested = node.get(key)
            if isinstance(nested, list):
                node[key] = [w for w in (walk(c, depth + 1) for c in nested) if w is not None]
        tabs = node.get("tabs")
        if isinstance(tabs, list):
            new_tabs = []
            for tab in tabs:
                if isinstance(tab, dict):
                    tab = dict(tab)
                    content = tab.get("content")
                    if isinstance(content, list):
                        tab["content"] = [w for w in (walk(c, depth + 1) for c in content) if w is not None]
                new_tabs.append(tab)
            node["tabs"] = new_tabs
        return node

    return [w for w in (walk(n, 0) for n in (layout or [])) if w is not None]


async def design_round(
    *,
    user_request: str,
    round_components: Sequence[Dict[str, Any]],
    canvas_rows: Sequence[Dict[str, Any]],
    chat_id: str,
    layout_key: str,
    allowed_types: Set[str],
    llm_call: Callable[[List[Dict[str, str]]], Awaitable[Optional[str]]],
    current_layout: Optional[Sequence[Dict[str, Any]]] = None,
    timeout_s: Optional[float] = None,
    max_rounds: Optional[int] = None,
    device: Optional[Dict[str, Any]] = None,
) -> Optional[List[Dict[str, Any]]]:
    budget = timeout_s if timeout_s is not None else designer_timeout_seconds()
    rounds = max_rounds if max_rounds is not None else designer_max_rounds()
    rounds = max(1, rounds)
    round_ids = [c.get("component_id") for c in round_components
                 if isinstance(c, dict) and c.get("component_id")]
    canvas_ids = {r.get("component_id") for r in (canvas_rows or []) if r.get("component_id")}
    allowed_refs = set(round_ids) | canvas_ids

    logger.info("ui_designer.invoked chat=%s components=%d budget_s=%.1f max_rounds=%d",
                chat_id, len(round_ids), budget, rounds)
    started = time.monotonic()

    def _fallback(reason: str, detail: str = "") -> None:
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.warning("ui_designer.fallback chat=%s reason=%s latency_ms=%d %s",
                       chat_id, reason, latency_ms, detail)

    use_scorer = scorer_enabled()
    use_objectives = False
    try:
        use_objectives = objectives_bias_enabled()
    except Exception:
        logger.debug("ui_designer: objectives_bias_enabled() failed — no objectives bias",
                     exc_info=True)
    archetype: Optional[str] = None
    if archetype_enabled():
        try:
            archetype = classify_archetype(user_request, round_components)
        except Exception:
            logger.debug("ui_designer: classify_archetype failed — no archetype", exc_info=True)
        if archetype:
            logger.info("ui_designer.archetype chat=%s archetype=%s", chat_id, archetype)
    task_prior = ""
    try:
        from orchestrator import task_model
        if task_model.taskmodel_enabled():
            schema_content = await asyncio.wait_for(
                llm_call(task_model.build_schema_messages(user_request, round_components)),
                timeout=budget)
            schema = task_model.parse_task_schema(schema_content) if schema_content else None
            if schema:
                task_prior = task_model.schema_prior(schema)
                if task_prior:
                    logger.info("ui_designer.taskmodel chat=%s entities=%d",
                                chat_id, len(schema.get("entities", [])))
    except Exception:
        logger.debug("ui_designer: task-model pre-pass failed — no prior", exc_info=True)
    ref_types: Dict[str, str] = {}
    for _c in round_components:
        if isinstance(_c, dict) and _c.get("component_id"):
            ref_types[str(_c["component_id"])] = str(_c.get("type") or "")
    for _r in (canvas_rows or []):
        if isinstance(_r, dict) and _r.get("component_id"):
            ref_types.setdefault(str(_r["component_id"]), str(_r.get("component_type") or ""))
    best_layout: Optional[List[Dict[str, Any]]] = None
    best_score: Optional[float] = None

    def _consider(candidate: List[Dict[str, Any]]) -> None:
        nonlocal best_layout, best_score
        if not (use_scorer or use_objectives):
            return
        s = 0.0
        scored = False
        if use_scorer:
            try:
                s += score_arrangement(candidate, ref_types=ref_types, archetype=archetype)
                scored = True
            except Exception:
                logger.debug("ui_designer: score_arrangement failed — keeping last-wins",
                             exc_info=True)
        if use_objectives:
            try:
                s += objectives_bias(candidate, device, ref_types)
                scored = True
            except Exception:
                logger.debug("ui_designer: objectives_bias failed — no objectives bias",
                             exc_info=True)
        if not scored:
            return
        if best_score is None or s > best_score:
            best_layout, best_score = candidate, s

    current: Optional[List[Dict[str, Any]]] = None
    passes_used = 0
    messages = build_design_messages(user_request, round_components, canvas_rows,
                                     allowed_types, archetype=archetype, task_prior=task_prior)

    for attempt in range(1, rounds + 1):
        passes_used = attempt
        try:
            content = await asyncio.wait_for(llm_call(messages), timeout=budget)
        except asyncio.TimeoutError:
            if current is None:
                _fallback("timeout", f"budget_s={budget}")
                return None
            logger.info("ui_designer.refine chat=%s round=%d outcome=timeout — keeping best",
                        chat_id, attempt)
            break
        except Exception as e:
            if current is None:
                _fallback("llm_error", str(e))
                return None
            logger.info("ui_designer.refine chat=%s round=%d outcome=llm_error — keeping best",
                        chat_id, attempt)
            break

        if not content:
            if current is None:
                _fallback("llm_error", "empty response")
                return None
            logger.info("ui_designer.refine chat=%s round=%d outcome=empty — keeping best",
                        chat_id, attempt)
            break

        if current is not None and _is_done_reply(content):
            logger.info("ui_designer.refine chat=%s round=%d outcome=done", chat_id, attempt)
            break

        rejection: Optional[Tuple[str, str]] = None
        layout: Optional[List[Dict[str, Any]]] = None
        try:
            parsed = parse_design_response(content)
        except DesignRejected as e:
            rejection = (e.reason, str(e))
        else:
            clean, referenced = validate_layout(parsed, allowed_refs, allowed_types)
            if current is None:
                layout = repair_layout(clean, referenced, round_ids) or None
                if layout is None:
                    rejection = ("invalid", "layout empty after validation")
            else:
                placed = set(iter_refs(current))
                if clean and placed <= set(referenced):
                    layout = clean
                else:
                    rejection = ("incomplete", "refinement lost placed components")

        if layout is None:
            reason, detail = rejection
            if current is not None:
                logger.info("ui_designer.refine chat=%s round=%d outcome=rejected:%s — keeping best",
                            chat_id, attempt, reason)
                break
            if reason == "refusal" or attempt >= rounds:
                _fallback(reason, detail)
                return None
            messages = messages + [
                {"role": "assistant", "content": str(content)[:2000]},
                {"role": "user", "content": (
                    f"Your previous response could not be used ({reason}: "
                    f"{detail or 'no detail'}). Reply with ONLY the JSON object "
                    '{"layout": [ ...nodes... ]} following the rules above — no markdown '
                    "fences, no commentary."
                )},
            ]
            continue

        if current is not None and layout == current:
            logger.info("ui_designer.refine chat=%s round=%d outcome=stable", chat_id, attempt)
            break
        improved = current is not None
        current = layout
        _consider(current)
        if improved:
            logger.info("ui_designer.refine chat=%s round=%d outcome=improved", chat_id, attempt)
        if attempt < rounds:
            messages = build_refine_messages(user_request, current, round_components,
                                             canvas_rows, allowed_types,
                                             archetype=archetype, task_prior=task_prior)

    if current is None:
        _fallback("invalid", "no usable arrangement produced")
        return None

    final = best_layout if ((use_scorer or use_objectives) and best_layout is not None) else current

    if lint_enabled():
        try:
            linted, dp_flags = lint_arrangement(final)
            if dp_flags:
                logger.warning("ui_designer.dark_pattern_blocked chat=%s rules=%s",
                               chat_id, sorted({f["rule"] for f in dp_flags}))
                final = linted
        except Exception:
            logger.debug("ui_designer: lint_arrangement failed — keeping unlinted", exc_info=True)

    a11y_findings: List[Dict[str, str]] = []
    if a11y_audit_enabled():
        try:
            from webrender.a11y import a11y_audit
            a11y_findings = a11y_audit(final) or []
            if a11y_findings:
                logger.warning(
                    "ui_designer.a11y_findings chat=%s layout_key=%s count=%d kinds=%s",
                    chat_id, layout_key, len(a11y_findings),
                    sorted({f.get("type", "?") for f in a11y_findings}),
                )
        except Exception:
            logger.debug("ui_designer: a11y_audit failed — skipping", exc_info=True)

    if conservative_enabled() and current_layout:
        try:
            if not should_adopt(final, current_layout, ref_types=ref_types):
                logger.info("ui_designer.conservative chat=%s layout_key=%s — kept existing "
                            "(re-design not worth the disruption)", chat_id, layout_key)
                final = list(current_layout)
        except Exception:
            logger.debug("ui_designer: conservative check failed — adopting new", exc_info=True)

    layout = stamp_garnish_ids(final, chat_id, layout_key)

    garnish_count = sum(1 for n in layout if n.get("type") != REF_TYPE)
    latency_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "ui_designer.designed chat=%s layout_key=%s refs=%d garnish=%d rounds=%d "
        "score=%s archetype=%s a11y=%s latency_ms=%d",
        chat_id, layout_key, len(set(iter_refs(layout))), garnish_count, passes_used,
        ("%.3f" % best_score) if best_score is not None else "off",
        archetype or "none",
        len(a11y_findings) if a11y_audit_enabled() else "off",
        latency_ms,
    )
    return layout


def iter_refs(layout: Sequence[Dict[str, Any]]):
    from orchestrator.workspace import iter_layout_refs
    yield from iter_layout_refs(list(layout))
