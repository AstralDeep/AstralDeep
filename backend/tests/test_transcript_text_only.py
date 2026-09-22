"""Feature 045 — the chat transcript is TEXT ONLY.

Exercises ``Orchestrator._transcript_html`` directly: text primitives render
into the chat rail, rich components (tables/charts/metrics/dashboards) are
dropped (they live on the canvas). Pure classmethod — no DB or instance.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.orchestrator import Orchestrator  # noqa: E402

_th = Orchestrator._transcript_html


def test_text_primitive_renders():
    html = _th([{"type": "text", "content": "hello there"}])
    assert "hello there" in html


@pytest.mark.parametrize("component", [
    {"type": "text", "content": "Source: [Dog grooming](https://en.wikipedia.org/wiki/Dog_grooming)"},
    {"type": "card", "content": [{"type": "text", "content": "Canvas document"}]},
])
def test_workspace_component_does_not_get_a_second_transcript_rendition(component):
    canvas_ids = frozenset({"wc_source"})
    assert _th([{**component, "component_id": "wc_source"}],
               canvas_component_ids=canvas_ids) == ""
    assert "Narrative reply" in _th([
        {**component, "component_id": "wc_source"},
        {"type": "text", "content": "Narrative reply", "component_id": "authored_answer"},
    ], canvas_component_ids=canvas_ids)


def test_alert_renders():
    html = _th([{"type": "alert", "message": "heads up", "variant": "info"}])
    assert "heads up" in html


def test_rich_table_is_dropped():
    assert _th([{"type": "table", "title": "DROPME",
                 "headers": ["h"], "rows": [["cellX"]]}]) == ""


def test_mixed_keeps_only_text():
    html = _th([
        {"type": "alert", "message": "keepme", "variant": "info"},
        {"type": "table", "title": "DROPME", "headers": ["h"], "rows": [["cellX"]]},
        {"type": "metric", "title": "DROPME2", "value": "99"},
    ])
    assert "keepme" in html
    assert "DROPME" not in html and "cellX" not in html and "DROPME2" not in html


def test_text_only_container_with_text_child_kept():
    html = _th([{"type": "card", "title": "Summary",
                 "children": [{"type": "text", "content": "inner words"}]}])
    assert "inner words" in html


def test_container_wrapping_rich_child_is_dropped():
    # A text-only container TYPE that wraps a rich child is not text-only.
    assert _th([{"type": "card", "title": "x",
                 "children": [{"type": "table", "headers": ["h"], "rows": [["1"]]}]}]) == ""


def test_non_list_inputs_are_empty():
    assert _th("just a string") == ""
    assert _th(None) == ""
    assert _th({"type": "text", "content": "x"}) == ""  # a dict, not a list


def test_empty_list_is_empty():
    assert _th([]) == ""
