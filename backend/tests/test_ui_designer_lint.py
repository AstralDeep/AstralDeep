"""Tests for the dark-pattern lint in orchestrator/ui_designer.py: lint_arrangement
strips false-urgency, scarcity, and confirmshaming language from designer-authored
garnish only, never from ref tool output, and fails open on error.
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
from orchestrator.ui_designer import design_round, lint_arrangement  # noqa: E402

ALLOWED = {"container", "text", "card", "grid", "tabs", "hero", "badge", "metric",
           "ref", "table", "line_chart"}


def test_lint_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER_LINT", raising=False)
    assert ui_designer.lint_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_lint_flag_off_values(monkeypatch, value):
    monkeypatch.setenv("FF_UI_DESIGNER_LINT", value)
    assert ui_designer.lint_enabled() is False


def test_lint_strips_false_urgency():
    layout = [{"type": "hero", "title": "Hurry, last chance!", "subtitle": "clean"}]
    cleaned, flags = lint_arrangement(layout)
    assert any(f["rule"] == "false_urgency" for f in flags)
    assert "hurry" not in cleaned[0]["title"].lower()
    assert "last chance" not in cleaned[0]["title"].lower()
    assert cleaned[0]["subtitle"] == "clean"


def test_lint_never_touches_refs():
    layout = [{"type": "ref", "component_id": "act now last chance"}]
    cleaned, flags = lint_arrangement(layout)
    assert cleaned[0] == {"type": "ref", "component_id": "act now last chance"}
    assert flags == []


def test_lint_clean_garnish_no_flags():
    layout = [{"type": "text", "content": "Here is your summary."},
              {"type": "metric", "title": "Revenue", "value": "$5"}]
    cleaned, flags = lint_arrangement(layout)
    assert flags == []
    assert cleaned == layout


def test_lint_confirmshaming_and_scarcity():
    layout = [{"type": "text", "content": "No thanks, I don't want to save money"},
              {"type": "badge", "label": "Only 2 left in stock"}]
    cleaned, flags = lint_arrangement(layout)
    rules = {f["rule"] for f in flags}
    assert "confirmshaming" in rules and "forced_scarcity" in rules
    assert "i don't want" not in cleaned[0]["content"].lower()
    assert "only 2 left" not in cleaned[1]["label"].lower()


def test_lint_scrubs_badges_and_nested_content():
    layout = [{"type": "card", "title": "clean", "content": [
        {"type": "hero", "title": "ok", "badges": ["Act now", "Verified"]}]}]
    cleaned, flags = lint_arrangement(layout)
    assert any(f["rule"] == "false_urgency" for f in flags)
    badges = cleaned[0]["content"][0]["badges"]
    assert all("act now" not in b.lower() for b in badges)
    assert "Verified" in badges


_COMPS = [
    {"type": "table", "component_id": "A", "title": "Tbl", "_source_agent": "a", "_source_tool": "t"},
    {"type": "line_chart", "component_id": "B", "title": "Chart", "_source_agent": "a", "_source_tool": "t"},
]
_DRAFT_URGENCY = json.dumps({"layout": [
    {"type": "hero", "title": "Act now! Limited time deal", "subtitle": "Overview"},
    {"type": "grid", "columns": 2, "children": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]},
]})


def _stub_llm(replies):
    it = iter(replies)

    async def _call(_messages):
        try:
            return next(it)
        except StopIteration:
            return "DONE"
    return _call


async def test_driver_lint_strips_dark_patterns(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_LINT", "true")
    out = await design_round(
        user_request="x", round_components=_COMPS, canvas_rows=[],
        chat_id="d1", layout_key="lk1", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT_URGENCY, "DONE"]), timeout_s=5, max_rounds=2,
    )
    hero = next(n for n in out if n.get("type") == "hero")
    assert "act now" not in hero["title"].lower()
    assert "limited time" not in hero["title"].lower()


async def test_driver_lint_off_preserves_garnish(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_LINT", "false")
    out = await design_round(
        user_request="x", round_components=_COMPS, canvas_rows=[],
        chat_id="d2", layout_key="lk2", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT_URGENCY, "DONE"]), timeout_s=5, max_rounds=2,
    )
    hero = next(n for n in out if n.get("type") == "hero")
    assert "act now" in hero["title"].lower()


async def test_driver_lint_failure_is_fail_open(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_LINT", "true")

    def _boom(*_a, **_k):
        raise RuntimeError("lint exploded")

    monkeypatch.setattr(ui_designer, "lint_arrangement", _boom)
    out = await design_round(
        user_request="x", round_components=_COMPS, canvas_rows=[],
        chat_id="d3", layout_key="lk3", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT_URGENCY, "DONE"]), timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert any(n.get("type") == "hero" for n in out)
