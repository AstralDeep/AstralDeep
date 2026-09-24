"""Tests for orchestrator/typesafe_routing/layout.py's deterministic layout composer:
every delivered component is placed exactly once, only ref and structural nodes
appear, and anything unarrangeable returns None to defer to the designer.
"""

from __future__ import annotations

import itertools
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.typesafe_routing.layout import (  # noqa: E402
    MIN_COMPONENTS,
    SUPPORTED_STYLES,
    class_of,
    compose,
)

STRUCTURAL = {"grid", "collapsible", "card", "tabs", "container"}


def _component(wire_type: str, index: int) -> dict:
    return {"type": wire_type, "component_id": f"c{index}"}


def _round(*types: str) -> list:
    return [_component(t, i) for i, t in enumerate(types)]


def _refs(node) -> list:
    out: list = []
    if isinstance(node, list):
        for item in node:
            out.extend(_refs(item))
        return out
    if not isinstance(node, dict):
        return out
    if node.get("type") == "ref":
        return [node["component_id"]]
    for key in ("children", "content"):
        out.extend(_refs(node.get(key) or []))
    for tab in node.get("tabs") or []:
        if isinstance(tab, dict):
            out.extend(_refs(tab.get("content") or []))
    return out


def _node_types(node) -> list:
    out: list = []
    if isinstance(node, list):
        for item in node:
            out.extend(_node_types(item))
        return out
    if not isinstance(node, dict):
        return out
    out.append(node.get("type"))
    for key in ("children", "content"):
        out.extend(_node_types(node.get(key) or []))
    for tab in node.get("tabs") or []:
        if isinstance(tab, dict):
            out.extend(_node_types(tab.get("content") or []))
    return out


@pytest.mark.parametrize(
    "wire_type,expected",
    [
        ("hero", "headline"), ("alert", "headline"),
        ("metric", "kpi"), ("stat_group", "kpi"), ("gauge", "kpi"),
        ("badge", "kpi"), ("rating", "kpi"),
        ("bar_chart", "chart"), ("line_chart", "chart"), ("pie_chart", "chart"),
        ("donut_chart", "chart"), ("radar_chart", "chart"), ("plotly_chart", "chart"),
        ("table", "record"), ("keyvalue", "record"), ("list", "record"),
        ("timeline", "record"), ("pipeline_stepper", "record"),
        ("text", "prose"), ("card", "prose"), ("code", "prose"),
        ("collapsible", "prose"), ("tabs", "prose"),
        ("action_group", "prose"),
        ("a_type_that_does_not_exist", "prose"),
    ],
)
def test_the_class_map_matches_the_contract(wire_type: str, expected: str) -> None:
    assert class_of({"type": wire_type}) == expected


def test_as_delivered_defers_to_the_designer() -> None:
    assert compose("as_delivered", _round("metric", "table")) is None


@pytest.mark.parametrize("style", [None, "", "interpretive_dance", "DASHBOARD "])
def test_an_unknown_style_defers(style) -> None:
    result = compose(style, _round("metric", "table"))
    if style == "DASHBOARD ":
        assert result is not None
    else:
        assert result is None


def test_fewer_than_two_components_defers() -> None:
    assert compose("dashboard", []) is None
    assert compose("dashboard", _round("metric")) is None
    assert MIN_COMPONENTS == 2


def test_a_component_without_an_id_defers() -> None:
    components = [{"type": "metric"}, {"type": "table", "component_id": "c1"}]
    assert compose("dashboard", components) is None


def test_an_id_field_is_accepted_as_well_as_component_id() -> None:
    components = [{"type": "metric", "id": "a"}, {"type": "table", "id": "b"}]
    assert compose("dashboard", components) is not None


@pytest.mark.parametrize("style", SUPPORTED_STYLES)
def test_every_component_is_placed_exactly_once(style: str) -> None:
    components = _round(
        "hero", "alert", "metric", "stat_group", "gauge", "bar_chart",
        "donut_chart", "radar_chart", "table", "timeline", "pipeline_stepper",
        "text", "code", "action_group",
    )
    layout = compose(style, components)
    assert layout is not None
    placed = _refs(layout)
    expected = [c["component_id"] for c in components]
    assert sorted(placed) == sorted(expected)
    assert len(placed) == len(set(placed))


@pytest.mark.parametrize(
    "style,types",
    list(
        itertools.product(
            SUPPORTED_STYLES,
            [
                ("metric", "table"),
                ("alert", "text"),
                ("gauge", "gauge", "gauge"),
                ("bar_chart", "line_chart"),
                ("text", "text", "text"),
                ("stat_group", "radar_chart", "pipeline_stepper"),
            ],
        )
    ),
)
def test_exactly_once_holds_across_shapes(style: str, types: tuple) -> None:
    components = _round(*types)
    layout = compose(style, components)
    assert layout is not None
    assert sorted(_refs(layout)) == sorted(c["component_id"] for c in components)


@pytest.mark.parametrize("style", SUPPORTED_STYLES)
def test_only_refs_and_structural_containers_appear(style: str) -> None:
    components = _round("hero", "metric", "bar_chart", "table", "text")
    layout = compose(style, components)
    for node_type in _node_types(layout):
        assert node_type in STRUCTURAL | {"ref"}, node_type


@pytest.mark.parametrize("style", SUPPORTED_STYLES)
def test_no_component_content_is_copied_into_the_layout(style: str) -> None:
    components = [
        {"type": "table", "component_id": "c0", "rows": [["secret-row-value"]]},
        {"type": "metric", "component_id": "c1", "value": "secret-metric-value"},
    ]
    layout = compose(style, components)
    rendered = repr(layout)
    assert "secret-row-value" not in rendered
    assert "secret-metric-value" not in rendered


@pytest.mark.parametrize("style", SUPPORTED_STYLES)
def test_compose_does_not_mutate_its_input(style: str) -> None:
    components = _round("metric", "table", "text")
    before = [dict(c) for c in components]
    compose(style, components)
    assert components == before


def test_compose_is_deterministic() -> None:
    components = _round("hero", "metric", "gauge", "bar_chart", "table", "text")
    first = compose("dashboard", components)
    for _ in range(5):
        assert compose("dashboard", components) == first


def test_dashboard_leads_with_the_headline_then_a_kpi_grid() -> None:
    components = _round("hero", "metric", "gauge", "bar_chart", "table", "text")
    layout = compose("dashboard", components)
    assert layout[0] == {"type": "ref", "component_id": "c0"}
    grid = layout[1]
    assert grid["type"] == "grid"
    assert _refs(grid) == ["c1", "c2"]
    assert grid["columns"] == 2


def test_dashboard_puts_several_charts_side_by_side() -> None:
    layout = compose("dashboard", _round("bar_chart", "line_chart", "table"))
    charts = [n for n in layout if isinstance(n, dict) and n.get("type") == "grid"]
    assert charts and charts[0]["columns"] == 2


def test_dashboard_does_not_grid_a_single_chart() -> None:
    layout = compose("dashboard", _round("bar_chart", "table"))
    assert layout[0] == {"type": "ref", "component_id": "c0"}


def test_detailed_table_puts_the_records_first() -> None:
    layout = compose("detailed_table", _round("metric", "bar_chart", "table"))
    assert _refs(layout)[0] == "c2"


def test_detailed_table_folds_the_charts_away() -> None:
    layout = compose("detailed_table", _round("table", "bar_chart"))
    collapsibles = [
        n for n in layout if isinstance(n, dict) and n.get("type") == "collapsible"
    ]
    assert collapsibles and collapsibles[0]["title"] == "Charts"
    assert collapsibles[0]["default_open"] is False


def test_alert_focused_leads_with_the_alert_and_folds_the_rest() -> None:
    layout = compose("alert_focused", _round("alert", "metric", "table", "text"))
    assert layout[0] == {"type": "ref", "component_id": "c0"}
    assert layout[1]["type"] == "collapsible"
    assert layout[1]["title"] == "Details"
    assert sorted(_refs(layout[1])) == ["c1", "c2", "c3"]


def test_alert_focused_with_nothing_to_fold_is_just_the_alerts() -> None:
    layout = compose("alert_focused", _round("alert", "hero"))
    assert all(n.get("type") == "ref" for n in layout)


def test_conversational_leads_with_prose_and_stacks_full_width() -> None:
    layout = compose("conversational", _round("metric", "text", "table"))
    assert _refs(layout)[0] == "c1"
    assert not [n for n in _node_types(layout) if n == "grid"]


@pytest.mark.parametrize("style", SUPPORTED_STYLES)
def test_delivered_order_is_preserved_within_a_class(style: str) -> None:
    components = _round("metric", "metric", "metric")
    layout = compose(style, components)
    assert _refs(layout) == ["c0", "c1", "c2"]
