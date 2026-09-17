"""Deterministic, style-driven arrangement of a round's components (feature 089).

The LLM designer pass composes a layout by asking a model to arrange the
round's output. That costs a call and a second or two, and its answer varies
between identical rounds. When TypeSafe has already told us *how* the result
should read -- a dashboard, a table, an alert, a conversation -- the
arrangement is a lookup, not a judgment.

So this module is a pure function. Given a style and the round's components it
returns a layout in the designer's existing format, and the orchestrator
validates it with the designer's existing validator before sending. There is no
model call and no randomness: the same round always arranges the same way.

Three rules make it safe to substitute for the designer:

* **Every delivered component appears exactly once.** Not fewer -- losing a
  tool's output is worse than a plain stack -- and not more.
* **Nothing is invented.** Components are placed by reference; their contents
  are never copied, rewritten or summarized (FR-029).
* **Anything unmapped returns ``None``**, which puts the turn back on the
  designer path unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger("Orchestrator.TypeSafe.Layout")

REF_TYPE = "ref"

#: Below this, an arrangement is not an arrangement. One component is already
#: as arranged as it can be, and two stacked is what a flat send does.
MIN_COMPONENTS = 2

#: Which class each component type belongs to. The classes are about how a
#: reader uses the thing, not what it is made of: a gauge and a metric are both
#: "one number you glance at", so they group together regardless of shape.
CLASS_OF: Mapping[str, str] = {
    # headline
    "hero": "headline",
    "alert": "headline",
    # kpi
    "metric": "kpi",
    "stat_group": "kpi",
    "gauge": "kpi",
    "badge": "kpi",
    "rating": "kpi",
    # chart
    "bar_chart": "chart",
    "line_chart": "chart",
    "pie_chart": "chart",
    "donut_chart": "chart",
    "radar_chart": "chart",
    "plotly_chart": "chart",
    # record
    "table": "record",
    "keyvalue": "record",
    "list": "record",
    "timeline": "record",
    "pipeline_stepper": "record",
}

#: Everything not named above.
DEFAULT_CLASS = "prose"

CLASSES = ("headline", "kpi", "chart", "record", "prose")

#: Styles this module can arrange. ``as_delivered`` is deliberately absent:
#: it means "do not rearrange", which is a decision, not a gap.
SUPPORTED_STYLES = ("dashboard", "detailed_table", "alert_focused", "conversational")


def class_of(component: Mapping[str, Any]) -> str:
    """The layout class of one component."""
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
    """Arrange ``components`` for ``style``, or return ``None``.

    ``None`` means "use the designer", and it is returned for
    ``as_delivered``, for an unknown style, for fewer than
    :data:`MIN_COMPONENTS` components, and for any component the round did not
    give an id -- an arrangement that cannot reference something cannot place
    it, and a layout that silently omits a component is worse than no layout.
    """
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
            # One unreferenceable component means the whole arrangement would
            # drop it. Hand the round back to the designer instead.
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
        # A builder that loses or duplicates a component is a bug, and the
        # right response to a bug in an optimization is to not use it.
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


# -- the four arrangements -----------------------------------------------


def _dashboard(grouped: Mapping[str, list]) -> list:
    """Glanceable first: headline, a KPI row, charts side by side, then detail."""
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
    """The records are the answer; everything else is supporting context.

    Charts go into a collapsible rather than above the table: someone who
    asked for the rows wants the rows first, and a chart above them pushes the
    thing they asked for below the fold.
    """
    layout: list = list(grouped["headline"])
    layout.extend(grouped["record"])
    if grouped["kpi"]:
        layout.append(_grid(grouped["kpi"], min(4, len(grouped["kpi"]))))
    if grouped["chart"]:
        layout.append(_collapsible("Charts", grouped["chart"]))
    layout.extend(grouped["prose"])
    return layout


def _alert_focused(grouped: Mapping[str, list]) -> list:
    """One thing must be seen. Everything else folds away under it."""
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
    """Prose leads, structure supports, nothing is side by side.

    Full width throughout is the point: a conversational answer read on a
    phone should not become two columns of half-legible tiles.
    """
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
