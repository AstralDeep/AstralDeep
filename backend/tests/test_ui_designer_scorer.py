"""Tests for orchestrator/ui_designer.py's score_arrangement layout scorer and its
fail-open integration into design_round: anchor, grouping, and texture rules, plus
legacy last-wins behavior when disabled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import ui_designer  # noqa: E402
from orchestrator.ui_designer import design_round, score_arrangement  # noqa: E402

ALLOWED = {
    "container", "text", "card", "table", "list", "alert", "metric", "grid",
    "tabs", "divider", "collapsible", "bar_chart", "line_chart", "pie_chart",
    "plotly_chart", "hero", "badge", "rating", "keyvalue", "timeline",
}


def test_scorer_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER_SCORER", raising=False)
    assert ui_designer.scorer_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_scorer_flag_off_values(monkeypatch, value):
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", value)
    assert ui_designer.scorer_enabled() is False


@pytest.mark.parametrize("bad", [[], "not-a-list", None, [1, 2, "x"], [{}]])
def test_score_degenerate_is_zero(bad):
    assert score_arrangement(bad) == 0.0


def test_score_is_deterministic():
    layout = [{"type": "hero", "title": "X"},
              {"type": "grid", "children": [{"type": "ref", "component_id": "A"}]}]
    assert score_arrangement(layout) == score_arrangement(list(layout))


def test_anchor_rewards_hero_first():
    rt = {"A": "table", "B": "line_chart"}
    anchored = [
        {"type": "hero", "title": "Overview"},
        {"type": "grid", "columns": 2, "children": [
            {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]},
    ]
    flat = [{"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]
    assert score_arrangement(anchored, ref_types=rt) == 3.0
    assert score_arrangement(flat, ref_types=rt) == 0.0
    assert score_arrangement(anchored, ref_types=rt) > score_arrangement(flat, ref_types=rt)


def test_texture_penalizes_same_type_run():
    two_metrics = [{"type": "metric", "title": "a"}, {"type": "metric", "title": "b"}]
    broken_up = [{"type": "metric", "title": "a"}, {"type": "text", "content": "—"},
                 {"type": "metric", "title": "b"}]
    assert score_arrangement(two_metrics) == 2.5
    assert score_arrangement(broken_up) == 3.0
    assert score_arrangement(broken_up) > score_arrangement(two_metrics)


def test_refs_of_different_types_not_a_run():
    rt = {"A": "table", "B": "line_chart"}
    layout = [{"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]
    assert score_arrangement(layout, ref_types=rt) == 0.0


def test_unknown_refs_get_unique_keys_no_penalty():
    layout = [{"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]
    assert score_arrangement(layout) == 0.0


def test_grid_grouped_beats_lonely_cell():
    grouped = [{"type": "grid", "children": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]}]
    lonely = [{"type": "grid", "children": [{"type": "ref", "component_id": "A"}]}]
    assert score_arrangement(grouped, ref_types={"A": "t", "B": "c"}) == 1.0
    assert score_arrangement(lonely, ref_types={"A": "t"}) == -0.5
    assert score_arrangement(grouped, ref_types={"A": "t", "B": "c"}) > score_arrangement(lonely)


def test_titled_container_beats_untitled():
    titled = [{"type": "card", "title": "Summary",
               "content": [{"type": "ref", "component_id": "A"}]}]
    untitled = [{"type": "card", "content": [{"type": "ref", "component_id": "A"}]}]
    assert score_arrangement(titled, ref_types={"A": "t"}) == 0.75
    assert score_arrangement(untitled, ref_types={"A": "t"}) == -0.25
    assert score_arrangement(titled) > score_arrangement(untitled)


def test_wall_of_components_penalized_vs_grouped():
    flat_wall = [{"type": "ref", "component_id": f"C{i}"} for i in range(7)]
    grouped = [{"type": "grid", "children": flat_wall}]
    assert score_arrangement(flat_wall) == -1.5
    assert score_arrangement(grouped) == 1.0
    assert score_arrangement(grouped) > score_arrangement(flat_wall)


def test_score_tabs_recursion_scores_nested_containers():
    layout = [{"type": "tabs", "title": "Views", "tabs": [
        {"label": "One", "content": [{"type": "card", "content": [
            {"type": "ref", "component_id": "A"}]}]}]}]
    assert score_arrangement(layout, ref_types={"A": "table"}) == -0.5


_COMPS = [
    {"type": "table", "component_id": "A", "title": "Tbl", "_source_agent": "a", "_source_tool": "t"},
    {"type": "line_chart", "component_id": "B", "title": "Chart", "_source_agent": "a", "_source_tool": "t"},
]
_DRAFT = json.dumps({"layout": [
    {"type": "hero", "title": "Overview"},
    {"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]},
]})
_FLAT = json.dumps({"layout": [
    {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]})


def _stub_llm(replies):
    it = iter(replies)

    async def _call(_messages):
        try:
            return next(it)
        except StopIteration:
            return "DONE"
    return _call


async def test_driver_scorer_keeps_higher_scoring_draft(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "true")
    canvas = [{"component_id": "Z", "component_type": "text", "title": "prior"}]
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=canvas,
        chat_id="c1", layout_key="lk1", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, _FLAT, "DONE"]), timeout_s=5, max_rounds=3,
    )
    assert out is not None
    assert any(n.get("type") == "hero" for n in out)


async def test_driver_scorer_off_is_legacy_last_wins(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "false")
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=[],
        chat_id="c2", layout_key="lk2", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, _FLAT, "DONE"]), timeout_s=5, max_rounds=3,
    )
    assert out is not None
    assert not any(n.get("type") == "hero" for n in out)
    assert [n.get("component_id") for n in out if n.get("type") == "ref"] == ["A", "B"]


async def test_driver_scorer_failure_is_fail_open(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "true")

    def _boom(*_a, **_k):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(ui_designer, "score_arrangement", _boom)
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=[],
        chat_id="c3", layout_key="lk3", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, _FLAT, "DONE"]), timeout_s=5, max_rounds=3,
    )
    assert out is not None
    assert not any(n.get("type") == "hero" for n in out)
