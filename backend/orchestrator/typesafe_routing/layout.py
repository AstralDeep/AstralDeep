"""Pure, deterministic alternative to ui_designer.py's LLM pass: given a presentation
style and a round's components, returns a layout in the designer's own format, which
orchestrator.py validates before sending.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("Orchestrator.TypeSafe.Layout")

REF_TYPE = "ref"

MIN_COMPONENTS = 2

CLASS_OF: Mapping[str, str] = {
    "hero": "headline",
    "alert": "headline",
    "metric": "kpi",
    "stat_group": "kpi",
    "gauge": "kpi",
    "badge": "kpi",
    "rating": "kpi",
    "bar_chart": "chart",
    "line_chart": "chart",
    "pie_chart": "chart",
    "donut_chart": "chart",
    "radar_chart": "chart",
    "plotly_chart": "chart",
    "table": "record",
    "keyvalue": "record",
    "list": "record",
    "timeline": "record",
    "pipeline_stepper": "record",
}

DEFAULT_CLASS = "prose"

CLASSES = ("headline", "kpi", "chart", "record", "prose")

SUPPORTED_STYLES = ("dashboard", "detailed_table", "alert_focused", "conversational")


def class_of(component: Mapping[str, Any]) -> str:
    wire_type = str((component or {}).get("type") or "").strip().lower()
    return CLASS_OF.get(wire_type, DEFAULT_CLASS)


def _component_id(component: Mapping[str, Any]) -> Optional[str]:
    for key in ("component_id", "id"):
        value = (component or {}).get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _ref(component_id: str) -> dict:
    return {"type": REF_TYPE, "component_id": component_id}


def _grid(children: Sequence[dict], columns: int) -> dict:
    return {"type": "grid", "columns": max(1, min(int(columns), 4)),
            "children": list(children)}


def _collapsible(title: str, children: Sequence[dict]) -> dict:
    return {"type": "collapsible", "title": title, "default_open": False,
            "content": list(children)}


def compose(
    style: Optional[str],
    components: Sequence[Mapping[str, Any]],
) -> Optional[list]:
    name = str(style or "").strip().lower()
    if name not in SUPPORTED_STYLES:
        return None
    if not components or len(components) < MIN_COMPONENTS:
        return None

    grouped: dict[str, list[dict]] = {key: [] for key in CLASSES}
    total = 0
    for component in components:
        component_id = _component_id(component)
        if component_id is None:
            logger.debug("typesafe layout: component without an id; deferring")
            return None
        grouped[class_of(component)].append(_ref(component_id))
        total += 1

    builder = {
        "dashboard": _dashboard,
        "detailed_table": _detailed_table,
        "alert_focused": _alert_focused,
        "conversational": _conversational,
    }[name]
    layout = builder(grouped)

    placed = _count_refs(layout)
    if placed != total:
        logger.warning(
            "typesafe layout: %s placed %d of %d components; deferring",
            name, placed, total,
        )
        return None
    return layout


def _count_refs(node: Any) -> int:
    if isinstance(node, list):
        return sum(_count_refs(item) for item in node)
    if not isinstance(node, dict):
        return 0
    if str(node.get("type") or "").lower() == REF_TYPE:
        return 1
    count = 0
    for key in ("children", "content"):
        count += _count_refs(node.get(key) or [])
    for tab in node.get("tabs") or []:
        if isinstance(tab, dict):
            count += _count_refs(tab.get("content") or [])
    return count


def _dashboard(grouped: Mapping[str, list]) -> list:
    layout: list = list(grouped["headline"])
    if grouped["kpi"]:
        layout.append(_grid(grouped["kpi"], min(4, len(grouped["kpi"]))))
    if grouped["chart"]:
        layout.append(_grid(grouped["chart"], 2) if len(grouped["chart"]) > 1
                      else grouped["chart"][0])
    layout.extend(grouped["record"])
    layout.extend(grouped["prose"])
    return layout


def _detailed_table(grouped: Mapping[str, list]) -> list:
    layout: list = list(grouped["headline"])
    layout.extend(grouped["record"])
    if grouped["kpi"]:
        layout.append(_grid(grouped["kpi"], min(4, len(grouped["kpi"]))))
    if grouped["chart"]:
        layout.append(_collapsible("Charts", grouped["chart"]))
    layout.extend(grouped["prose"])
    return layout


def _alert_focused(grouped: Mapping[str, list]) -> list:
    layout: list = list(grouped["headline"])
    rest = (
        list(grouped["kpi"])
        + list(grouped["chart"])
        + list(grouped["record"])
        + list(grouped["prose"])
    )
    if rest:
        layout.append(_collapsible("Details", rest))
    return layout


def _conversational(grouped: Mapping[str, list]) -> list:
    layout: list = list(grouped["prose"])
    layout.extend(grouped["headline"])
    layout.extend(grouped["kpi"])
    layout.extend(grouped["chart"])
    layout.extend(grouped["record"])
    return layout


__all__ = (
    "CLASSES",
    "CLASS_OF",
    "DEFAULT_CLASS",
    "MIN_COMPONENTS",
    "REF_TYPE",
    "SUPPORTED_STYLES",
    "class_of",
    "compose",
)
