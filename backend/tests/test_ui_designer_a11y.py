"""Tests for the accessibility check wired into design_round in
orchestrator/ui_designer.py: with the flag on, webrender/a11y.py audits the chosen
arrangement and logs findings advisorily, never dropping a valid arrangement.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import ui_designer  # noqa: E402
from orchestrator.ui_designer import design_round  # noqa: E402
from webrender import a11y  # noqa: E402

ALLOWED = {
    "container", "text", "card", "table", "list", "alert", "metric", "grid",
    "tabs", "image", "button", "hero", "badge", "ref",
}

_COMPS = [
    {"type": "table", "component_id": "A", "title": "Tbl", "_source_agent": "a", "_source_tool": "t"},
    {"type": "line_chart", "component_id": "B", "title": "Chart", "_source_agent": "a", "_source_tool": "t"},
]

_DRAFT_BAD_A11Y = json.dumps({"layout": [
    {"type": "card", "content": [
        {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]},
]})

_DRAFT_CLEAN = json.dumps({"layout": [
    {"type": "card", "title": "Results", "content": [
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


def test_a11y_audit_flag_default_off(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER_A11Y", raising=False)
    assert ui_designer.a11y_audit_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "  TRUE  "])
def test_a11y_audit_flag_on(monkeypatch, value):
    monkeypatch.setenv("FF_UI_DESIGNER_A11Y", value)
    assert ui_designer.a11y_audit_enabled() is True


async def test_designer_a11y_flags_unlabelled_landmark(monkeypatch, caplog):
    monkeypatch.setenv("FF_UI_DESIGNER_A11Y", "true")
    monkeypatch.setenv("FF_UI_DESIGNER_LINT", "false")
    with caplog.at_level(logging.WARNING, logger="orchestrator.ui_designer"):
        out = await design_round(
            user_request="x", round_components=_COMPS, canvas_rows=[],
            chat_id="a1", layout_key="lk1", allowed_types=ALLOWED,
            llm_call=_stub_llm([_DRAFT_BAD_A11Y, "DONE"]), timeout_s=5, max_rounds=2,
        )
    assert out is not None
    assert any(n.get("type") == "card" for n in out)
    assert any("ui_designer.a11y_findings" in r.getMessage() for r in caplog.records)
    assert any("card" in r.getMessage() for r in caplog.records
               if "a11y_findings" in r.getMessage())


async def test_designer_a11y_runs_real_audit_on_final(monkeypatch):
    seen = {}
    real_audit = a11y.a11y_audit

    def _spy(components):
        result = real_audit(components)
        seen["components"] = components
        seen["result"] = result
        return result

    monkeypatch.setenv("FF_UI_DESIGNER_A11Y", "true")
    monkeypatch.setattr("webrender.a11y.a11y_audit", _spy)
    out = await design_round(
        user_request="x", round_components=_COMPS, canvas_rows=[],
        chat_id="a2", layout_key="lk2", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT_BAD_A11Y, "DONE"]), timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert "components" in seen, "design_round never called a11y_audit"
    assert any(isinstance(n, dict) and n.get("type") == "card" for n in seen["components"])
    assert any(f["type"] == "card" and "label" in f["issue"] for f in seen["result"])


async def test_designer_a11y_clean_arrangement_no_findings(monkeypatch, caplog):
    monkeypatch.setenv("FF_UI_DESIGNER_A11Y", "true")
    monkeypatch.setenv("FF_UI_DESIGNER_LINT", "false")
    with caplog.at_level(logging.WARNING, logger="orchestrator.ui_designer"):
        out = await design_round(
            user_request="x", round_components=_COMPS, canvas_rows=[],
            chat_id="a3", layout_key="lk3", allowed_types=ALLOWED,
            llm_call=_stub_llm([_DRAFT_CLEAN, "DONE"]), timeout_s=5, max_rounds=2,
        )
    assert out is not None
    assert not any("ui_designer.a11y_findings" in r.getMessage() for r in caplog.records)


async def test_designer_a11y_off_does_not_run(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER_A11Y", raising=False)

    def _boom(_components):
        raise AssertionError("a11y_audit must not run when the flag is off")

    monkeypatch.setattr("webrender.a11y.a11y_audit", _boom)
    out = await design_round(
        user_request="x", round_components=_COMPS, canvas_rows=[],
        chat_id="a4", layout_key="lk4", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT_BAD_A11Y, "DONE"]), timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert any(n.get("type") == "card" for n in out)


async def test_designer_a11y_failure_is_fail_open(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_A11Y", "true")

    def _boom(_components):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr("webrender.a11y.a11y_audit", _boom)
    out = await design_round(
        user_request="x", round_components=_COMPS, canvas_rows=[],
        chat_id="a5", layout_key="lk5", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT_BAD_A11Y, "DONE"]), timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert any(n.get("type") == "card" for n in out)
