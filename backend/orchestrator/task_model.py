"""Deterministic rule table for task-model-first generative UI: typed, role-annotated
entity attributes map to astralprims primitives, deriving a layout skeleton without
relying on the LLM's layout choices. Integrated into ui_designer.py.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

ATTR_TYPES = ("SVAL", "DICT", "ARRY", "PNTR", "TEMPORAL")

_METRIC_ROLES = frozenset({"metric", "kpi", "measure", "count", "total", "amount", "stat"})
_RATING_ROLES = frozenset({"rating", "score", "stars", "grade"})
_STATUS_ROLES = frozenset({"status", "state", "badge", "flag", "label"})
_TABLE_ROLES = frozenset({"table", "tabular", "rows", "records", "grid"})
_TIMELINE_ROLES = frozenset({"timeline", "events", "history", "schedule", "log"})


def attr_to_primitive(attr_type: str, *, role: Optional[str] = None,
                      cardinality: str = "one") -> str:
    t = (attr_type or "").strip().upper()
    r = (role or "").strip().lower()
    if t == "SVAL":
        if r in _METRIC_ROLES:
            return "metric"
        if r in _RATING_ROLES:
            return "rating"
        if r in _STATUS_ROLES:
            return "badge"
        return "text"
    if t == "DICT":
        return "keyvalue"
    if t == "ARRY":
        if r in _TIMELINE_ROLES:
            return "timeline"
        if r in _TABLE_ROLES or cardinality in ("many", "table"):
            return "table"
        return "list"
    if t == "PNTR":
        return "card"
    if t == "TEMPORAL":
        return "timeline"
    return "text"


def _attr_spec(attr: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not isinstance(attr, dict):
        return None
    name = str(attr.get("name") or "").strip()
    ptype = attr_to_primitive(attr.get("type", ""), role=attr.get("role"),
                              cardinality=str(attr.get("cardinality") or "one"))
    spec: Dict[str, str] = {"type": ptype}
    if name:
        spec["title"] = name
    return spec


def derive_layout(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    layout: List[Dict[str, Any]] = []
    task = str(schema.get("task") or "").strip()
    entities = [e for e in (schema.get("entities") or []) if isinstance(e, dict)]
    if task:
        layout.append({"type": "hero", "title": task})
    for ent in entities:
        attrs = [a for a in (ent.get("attributes") or []) if isinstance(a, dict)]
        children = [s for s in (_attr_spec(a) for a in attrs) if s]
        if not children:
            continue
        name = str(ent.get("name") or "").strip()
        if len(children) == 1 and not name:
            layout.append(children[0])
        else:
            card: Dict[str, Any] = {"type": "card", "content": children}
            if name:
                card["title"] = name
            layout.append(card)
    return layout


def _outline(nodes: List[Dict[str, Any]], indent: int = 0) -> List[str]:
    lines = []
    for n in nodes:
        pad = "  " * indent
        bits = [n.get("type", "?")]
        if n.get("title"):
            bits.append(f'"{n["title"]}"')
        lines.append(f"{pad}- {' '.join(bits)}")
        kids = n.get("content")
        if isinstance(kids, list):
            lines.extend(_outline([k for k in kids if isinstance(k, dict)], indent + 1))
    return lines


def schema_prior(schema: Dict[str, Any]) -> str:
    layout = derive_layout(schema)
    if not layout:
        return ""
    return "Derived structure for this task (follow it):\n" + "\n".join(_outline(layout))


def parse_task_schema(content: str) -> Optional[Dict[str, Any]]:
    if not isinstance(content, str):
        return None
    s = content.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])
                except (ValueError, TypeError):
                    return None
                break
    else:
        return None
    if not isinstance(obj, dict):
        return None
    entities = [e for e in (obj.get("entities") or []) if isinstance(e, dict)]
    if not entities:
        return None
    return obj


_MAX_REQUEST_CHARS = 800


def build_schema_messages(user_request: str,
                          components: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    request = (user_request or "").strip()
    if len(request) > _MAX_REQUEST_CHARS:
        request = request[:_MAX_REQUEST_CHARS] + "…"
    kinds = sorted({str(c.get("type", "")).strip().lower()
                    for c in components if isinstance(c, dict) and c.get("type")})
    system = (
        "You model a UI task as typed data BEFORE it is laid out. Reply with "
        "ONLY a JSON object:\n"
        '{"task":"<short title>","entities":[{"name":"<entity>","attributes":'
        '[{"name":"<attr>","type":"SVAL|DICT|ARRY|PNTR|TEMPORAL","role":"<metric|'
        'rating|status|table|timeline|…>"}]}]}\n'
        "SVAL=one scalar value, DICT=key/value facts, ARRY=a collection, "
        "PNTR=a reference to another thing, TEMPORAL=time/events. Keep it small "
        "(1–3 entities). No prose."
    )
    user = (f"USER REQUEST:\n{request or '(not provided)'}\n\n"
            f"COMPONENTS PRESENT (types): {', '.join(kinds) or '(none)'}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def taskmodel_enabled() -> bool:
    return os.getenv("FF_UI_DESIGNER_TASKMODEL", "false").strip().lower() in ("1", "true", "yes", "on")
