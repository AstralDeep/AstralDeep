"""Tests for orchestrator/task_model.py: the typed-attribute-to-primitive rule table,
schema-to-layout-skeleton derivation, the schema parser, and its fail-open
integration as a structural prior into ui_designer.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import task_model as tm  # noqa: E402
from orchestrator.ui_designer import build_design_messages, design_round  # noqa: E402

ALLOWED = {
    "container", "text", "card", "table", "list", "alert", "metric", "grid",
    "tabs", "divider", "collapsible", "bar_chart", "line_chart", "pie_chart",
    "plotly_chart", "hero", "badge", "rating", "keyvalue", "timeline",
}


@pytest.mark.parametrize("attr_type,role,card,expected", [
    ("SVAL", "metric", "one", "metric"),
    ("SVAL", "kpi", "one", "metric"),
    ("SVAL", "rating", "one", "rating"),
    ("SVAL", "status", "one", "badge"),
    ("SVAL", None, "one", "text"),
    ("DICT", None, "one", "keyvalue"),
    ("ARRY", "table", "one", "table"),
    ("ARRY", None, "many", "table"),
    ("ARRY", "timeline", "one", "timeline"),
    ("ARRY", None, "one", "list"),
    ("PNTR", None, "one", "card"),
    ("TEMPORAL", None, "one", "timeline"),
    ("WHATSIT", None, "one", "text"),
])
def test_attr_to_primitive_rules(attr_type, role, card, expected):
    assert tm.attr_to_primitive(attr_type, role=role, cardinality=card) == expected


def test_attr_to_primitive_is_case_insensitive():
    assert tm.attr_to_primitive("sval", role="METRIC") == "metric"


def test_attr_to_primitive_only_emits_real_primitives():
    for t in ("SVAL", "DICT", "ARRY", "PNTR", "TEMPORAL"):
        assert tm.attr_to_primitive(t) in ALLOWED


def test_derive_layout_builds_hero_and_entity_cards():
    schema = {"task": "Quarterly sales", "entities": [
        {"name": "Q3", "attributes": [
            {"name": "revenue", "type": "SVAL", "role": "metric"},
            {"name": "line items", "type": "ARRY", "role": "table"},
        ]},
    ]}
    layout = tm.derive_layout(schema)
    assert layout[0] == {"type": "hero", "title": "Quarterly sales"}
    card = layout[1]
    assert card["type"] == "card" and card["title"] == "Q3"
    kinds = [c["type"] for c in card["content"]]
    assert kinds == ["metric", "table"]


def test_derive_layout_skips_empty_entities():
    schema = {"task": "T", "entities": [{"name": "E", "attributes": []}]}
    assert tm.derive_layout(schema) == [{"type": "hero", "title": "T"}]


def test_derive_layout_degenerate_is_empty():
    assert tm.derive_layout({}) == []
    assert tm.derive_layout("nope") == []
    assert tm.derive_layout({"entities": []}) == []


def test_schema_prior_outlines_structure():
    schema = {"task": "Compare", "entities": [
        {"name": "A", "attributes": [{"name": "score", "type": "SVAL", "role": "rating"}]}]}
    prior = tm.schema_prior(schema)
    assert "Derived structure" in prior
    assert "hero" in prior and "Compare" in prior and "rating" in prior


def test_schema_prior_empty_for_degenerate():
    assert tm.schema_prior({}) == ""


def test_parse_valid_schema():
    s = tm.parse_task_schema('{"task":"T","entities":[{"name":"E","attributes":[]}]}')
    assert s["task"] == "T" and s["entities"][0]["name"] == "E"


def test_parse_tolerates_fence_and_prose():
    s = tm.parse_task_schema('ok:\n```json\n{"task":"T","entities":[{"name":"E"}]}\n```')
    assert s is not None and s["task"] == "T"


@pytest.mark.parametrize("bad", [
    "", "not json", "{}", '{"task":"T"}', '{"task":"T","entities":[]}', None,
])
def test_parse_rejects_unusable(bad):
    assert tm.parse_task_schema(bad) is None


def test_build_schema_messages_lists_request_and_types():
    msgs = tm.build_schema_messages("compare A and B",
                                    [{"type": "table"}, {"type": "metric"}])
    blob = msgs[0]["content"] + msgs[1]["content"]
    assert "compare A and B" in blob
    assert "table" in blob and "metric" in blob
    assert "SVAL" in blob and "entities" in blob


def test_taskmodel_default_off(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER_TASKMODEL", raising=False)
    assert tm.taskmodel_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_taskmodel_on_values(monkeypatch, value):
    monkeypatch.setenv("FF_UI_DESIGNER_TASKMODEL", value)
    assert tm.taskmodel_enabled() is True


def test_design_prompt_includes_task_prior_when_set():
    msgs = build_design_messages("x", [{"type": "table", "component_id": "A"}],
                                 [], ALLOWED, task_prior="Derived structure for this task:\n- hero")
    assert "Derived structure" in msgs[1]["content"]


def test_design_prompt_omits_task_prior_when_empty():
    msgs = build_design_messages("x", [{"type": "table", "component_id": "A"}],
                                 [], ALLOWED, task_prior="")
    assert "Derived structure" not in msgs[1]["content"]


_COMPS = [
    {"type": "table", "component_id": "A", "title": "T", "_source_agent": "a", "_source_tool": "t"},
    {"type": "metric", "component_id": "B", "title": "M", "_source_agent": "a", "_source_tool": "t"},
]
_SCHEMA = '{"task":"Show the table","entities":[{"name":"Report","attributes":[{"name":"rows","type":"ARRY","role":"table"}]}]}'
_DRAFT = json.dumps({"layout": [{"type": "grid", "columns": 2, "children": [
    {"type": "ref", "component_id": "A"}, {"type": "ref", "component_id": "B"}]}]})


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


async def test_driver_taskmodel_prepass_seeds_prior(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_TASKMODEL", "true")
    captured: list = []
    out = await design_round(
        user_request="show me the report", round_components=_COMPS, canvas_rows=[],
        chat_id="c1", layout_key="lk1", allowed_types=ALLOWED,
        llm_call=_stub_llm([_SCHEMA, _DRAFT, "DONE"], captured), timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert any("Derived structure" in m[-1]["content"] for m in captured)


async def test_driver_taskmodel_off_no_prepass(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_TASKMODEL", "false")
    captured: list = []
    out = await design_round(
        user_request="show me the report", round_components=_COMPS, canvas_rows=[],
        chat_id="c2", layout_key="lk2", allowed_types=ALLOWED,
        llm_call=_stub_llm([_DRAFT, "DONE"], captured), timeout_s=5, max_rounds=2,
    )
    assert out is not None
    assert all("Derived structure" not in m[-1]["content"] for m in captured)


async def test_driver_taskmodel_bad_schema_is_fail_open(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_TASKMODEL", "true")
    out = await design_round(
        user_request="show me", round_components=_COMPS, canvas_rows=[],
        chat_id="c3", layout_key="lk3", allowed_types=ALLOWED,
        llm_call=_stub_llm(["not a schema", _DRAFT, "DONE"]), timeout_s=5, max_rounds=2,
    )
    assert out is not None
