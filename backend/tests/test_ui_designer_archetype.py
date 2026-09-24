"""Tests for interaction-archetype selection in orchestrator/ui_designer.py: the
designer classifies a turn's archetype from text and component shape, then seeds both
a prompt prior and an additive scorer bonus toward the matching layout.
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
from orchestrator.ui_designer import (  # noqa: E402
    archetype_bonus,
    archetype_prior,
    build_design_messages,
    classify_archetype,
    design_round,
    score_arrangement,
)

ALLOWED = {
    "container", "text", "card", "table", "list", "alert", "metric", "grid",
    "tabs", "divider", "collapsible", "bar_chart", "line_chart", "pie_chart",
    "plotly_chart", "hero", "badge", "rating", "keyvalue", "timeline",
}


def test_archetype_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER_ARCHETYPE", raising=False)
    assert ui_designer.archetype_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_archetype_flag_off_values(monkeypatch, value):
    monkeypatch.setenv("FF_UI_DESIGNER_ARCHETYPE", value)
    assert ui_designer.archetype_enabled() is False


@pytest.mark.parametrize("request_text,expected", [
    ("Compare the Q2 and Q3 revenue side by side", "compare"),
    ("Show me a live dashboard of system health metrics", "monitor"),
    ("Explore all the products in the catalog", "explore"),
    ("Give me a summary / overview of the report", "summarize"),
    ("Which option should I choose? recommend the best one", "decide"),
    ("Create a form to register a new patient", "form"),
])
def test_classify_from_request_text(request_text, expected):
    assert classify_archetype(request_text, []) == expected


def test_classify_none_when_no_signal():
    assert classify_archetype("hello there", []) is None
    assert classify_archetype("", []) is None
    assert classify_archetype(None, None) is None


def test_shape_two_metrics_reads_as_monitor():
    comps = [{"type": "metric"}, {"type": "hero"}]
    assert classify_archetype("here you go", comps) == "monitor"


def test_shape_two_dataviews_reads_as_compare():
    comps = [{"type": "table"}, {"type": "line_chart"}]
    assert classify_archetype("here you go", comps) == "compare"


def test_shape_single_component_reads_as_summarize():
    assert classify_archetype("here", [{"type": "text"}]) == "summarize"


def test_text_intent_beats_shape():
    comps = [{"type": "metric"}, {"type": "metric"}]
    assert classify_archetype("compare A and B side by side", comps) == "compare"


def test_classify_is_deterministic():
    assert classify_archetype("dashboard", []) == classify_archetype("dashboard", [])


@pytest.mark.parametrize("arch", list(ui_designer.ARCHETYPES))
def test_prior_present_for_every_archetype(arch):
    p = archetype_prior(arch)
    assert p and arch.upper()[:4] in p.upper()


def test_prior_empty_for_none():
    assert archetype_prior(None) == ""
    assert archetype_prior("nonsense") == ""


def test_compare_rewards_side_by_side_grid():
    grid = [{"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]}]
    no_grid = [{"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]
    assert archetype_bonus(grid, "compare") == ui_designer.W_ARCH_COMPARE_GRID
    assert archetype_bonus(no_grid, "compare") == 0.0


def test_monitor_rewards_anchor_lead():
    anchored = [{"type": "hero", "title": "X"}, {"type": "ref", "component_id": "A"}]
    flat = [{"type": "ref", "component_id": "A"}, {"type": "hero"}]
    assert archetype_bonus(anchored, "monitor") == ui_designer.W_ARCH_MONITOR_ANCHOR
    assert archetype_bonus(flat, "monitor") == 0.0


def test_explore_rewards_titled_container():
    tabs = [{"type": "tabs", "tabs": [{"label": "x", "content": []}]}]
    assert archetype_bonus(tabs, "explore") == ui_designer.W_ARCH_EXPLORE_CONTAINER


def test_summarize_rewards_lead_penalizes_sprawl():
    lead = [{"type": "text", "content": "takeaway"}]
    assert archetype_bonus(lead, "summarize") == ui_designer.W_ARCH_SUMMARIZE_LEAD
    sprawl = [{"type": "ref", "component_id": str(i)} for i in range(5)]
    assert archetype_bonus(sprawl, "summarize") == ui_designer.W_ARCH_SUMMARIZE_SPRAWL


def test_form_penalizes_multicol_rewards_single_col():
    multicol = [{"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": "A"}]}]
    single = [{"type": "card", "title": "Section", "content": [
        {"type": "ref", "component_id": "A"}]}]
    assert archetype_bonus(multicol, "form") == ui_designer.W_ARCH_FORM_MULTICOL
    assert archetype_bonus(single, "form") == ui_designer.W_ARCH_FORM_SINGLE_COL


def test_bonus_zero_for_none_and_degenerate():
    layout = [{"type": "grid", "children": [{"type": "ref", "component_id": "A"}]}]
    assert archetype_bonus(layout, None) == 0.0
    assert archetype_bonus([], "compare") == 0.0
    assert archetype_bonus("x", "compare") == 0.0


def test_score_with_none_archetype_is_base_unchanged():
    layout = [{"type": "hero", "title": "X"},
              {"type": "grid", "columns": 2, "children": [
                  {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]}]
    rt = {"A": "table", "B": "line_chart"}
    base = score_arrangement(layout, ref_types=rt)
    assert score_arrangement(layout, ref_types=rt, archetype=None) == base


def test_score_adds_bonus_for_archetype():
    layout = [{"type": "hero", "title": "X"},
              {"type": "grid", "columns": 2, "children": [
                  {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]}]
    rt = {"A": "table", "B": "line_chart"}
    base = score_arrangement(layout, ref_types=rt)
    scored = score_arrangement(layout, ref_types=rt, archetype="compare")
    assert scored == round(base + ui_designer.W_ARCH_COMPARE_GRID, 4)


def test_archetype_flips_the_winner_for_form():
    multicol = [{"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]}]
    single = [{"type": "card", "title": "One", "content": [{"type": "ref", "component_id": "A"}]},
              {"type": "card", "title": "Two", "content": [{"type": "ref", "component_id": "B"}]}]
    rt = {"A": "text", "B": "text"}
    assert score_arrangement(single, ref_types=rt, archetype="form") > \
        score_arrangement(multicol, ref_types=rt, archetype="form")


def test_design_prompt_includes_prior_when_archetype_set():
    msgs = build_design_messages("compare a and b", [{"type": "table", "component_id": "A"}],
                                 [], ALLOWED, archetype="compare")
    assert "TASK SHAPE:" in msgs[1]["content"]
    assert "side by side" in msgs[1]["content"]


def test_design_prompt_omits_prior_when_none():
    msgs = build_design_messages("x", [{"type": "table", "component_id": "A"}],
                                 [], ALLOWED, archetype=None)
    assert "TASK SHAPE:" not in msgs[1]["content"]


_COMPS = [
    {"type": "table", "component_id": "A", "title": "T", "_source_agent": "a", "_source_tool": "t"},
    {"type": "table", "component_id": "B", "title": "T2", "_source_agent": "a", "_source_tool": "t"},
]
_SIDE_BY_SIDE = json.dumps({"layout": [
    {"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]}]})
_STACKED = json.dumps({"layout": [
    {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]})


def _stub_llm(replies, captured=None):
    it = iter(replies)

    async def _call(messages):
        if captured is not None:
            captured.append(messages)
        try:
            return next(it)
        except StopIteration:
            return "DONE"
    return _call


async def test_driver_classifies_and_seeds_prompt(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_ARCHETYPE", "true")
    captured: list = []
    out = await design_round(
        user_request="compare A versus B side by side",
        round_components=_COMPS, canvas_rows=[], chat_id="c1", layout_key="lk1",
        allowed_types=ALLOWED, llm_call=_stub_llm([_SIDE_BY_SIDE, "DONE"], captured),
        timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert any("TASK SHAPE:" in m[-1]["content"] for m in captured)


async def test_driver_archetype_off_seeds_no_prior(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_ARCHETYPE", "false")
    captured: list = []
    out = await design_round(
        user_request="compare A versus B side by side",
        round_components=_COMPS, canvas_rows=[], chat_id="c2", layout_key="lk2",
        allowed_types=ALLOWED, llm_call=_stub_llm([_SIDE_BY_SIDE, "DONE"], captured),
        timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert all("TASK SHAPE:" not in m[-1]["content"] for m in captured)


async def test_driver_classification_failure_is_fail_open(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_ARCHETYPE", "true")

    def _boom(*_a, **_k):
        raise RuntimeError("classify exploded")

    monkeypatch.setattr(ui_designer, "classify_archetype", _boom)
    out = await design_round(
        user_request="compare", round_components=_COMPS, canvas_rows=[],
        chat_id="c3", layout_key="lk3", allowed_types=ALLOWED,
        llm_call=_stub_llm([_SIDE_BY_SIDE, "DONE"]), timeout_s=5, max_rounds=2,
    )
    assert out is not None
