"""Tests for the deterministic metrics: independent schema, answer, and evidence
dimensions over raw planner output, with golden, edge, and failure inputs.
"""

from __future__ import annotations

import json

from prompt_optimization.fixture import PlannerCase
from prompt_optimization.metrics import CaseMetrics, score_output


def _case(**overrides) -> PlannerCase:
    defaults = dict(
        case_id="t-x",
        request="List the projects for the Atlas team and build one combined report.",
        available_tools=("list_projects", "build_report", "fetch_record"),
        min_subtasks=2,
        max_subtasks=2,
        expected_tools=frozenset({"list_projects", "build_report"}),
    )
    defaults.update(overrides)
    return PlannerCase(**defaults)


def _plan(subtasks, synthesis="Combine results."):
    return json.dumps({"subtasks": subtasks, "synthesis": synthesis})


def _subtask(index, hint, depends=None, subtype="parallel"):
    return {
        "id": f"st_{index}",
        "description": f"Step {index}",
        "tool_hints": [hint],
        "depends_on": depends or [],
        "type": subtype,
    }


def test_golden_plan_scores_full():
    case = _case()
    text = _plan(
        [
            _subtask(1, "list_projects"),
            _subtask(2, "build_report", depends=["st_1"], subtype="sequential"),
        ]
    )
    metrics, components = score_output(case, text)
    assert metrics == CaseMetrics(schema=1.0, answer=1.0, evidence=1.0)
    assert components.to_dict() == {
        "schema": [],
        "answer": [],
        "evidence": [],
    }
    assert metrics.combined == 1.0


def test_non_json_output_scores_zero_schema_and_zero_rest():
    metrics, components = score_output(_case(), "I will plan this now.")
    assert metrics.schema == 0.0
    assert metrics.answer == 0.0
    assert metrics.evidence == 0.0
    assert components.schema == ["plan_not_json_object"]


def test_json_non_object_scores_zero():
    metrics, _ = score_output(_case(), json.dumps([1, 2]))
    assert metrics.schema == 0.0


def test_missing_synthesis_fails_schema_component():
    text = _plan(
        [
            _subtask(1, "list_projects"),
            _subtask(2, "build_report"),
        ],
        synthesis="",
    )
    metrics, components = score_output(_case(), text)
    assert metrics.schema == 0.75
    assert "synthesis_missing" in components.schema


def test_subtask_count_out_of_range_fails_answer():
    text = _plan([_subtask(1, "list_projects")])
    metrics, components = score_output(_case(), text)
    assert "subtask_count_out_of_range" in components.answer
    assert "expected_tools_not_covered" in components.answer
    assert metrics.answer == 0.6


def test_expected_tools_missing_fails_answer():
    text = _plan([_subtask(1, "list_projects"), _subtask(2, "fetch_record")])
    metrics, components = score_output(_case(), text)
    assert "expected_tools_not_covered" in components.answer


def test_forbidden_tool_zeroes_answer():
    case = _case(forbidden_tools=frozenset({"fetch_record"}))
    text = _plan([_subtask(1, "list_projects"), _subtask(2, "fetch_record")])
    metrics, components = score_output(case, text)
    assert "forbidden_tool_used" in components.answer
    assert metrics.answer == 0.0


def test_simple_case_requires_single_subtask():
    case = _case(
        simple=True,
        min_subtasks=1,
        max_subtasks=1,
        expected_tools=frozenset({"list_projects"}),
    )
    text = _plan(
        [
            _subtask(1, "list_projects"),
            _subtask(2, "list_projects"),
        ]
    )
    metrics, components = score_output(case, text)
    assert "simple_request_not_single_subtask" in components.answer
    assert "subtask_count_out_of_range" in components.answer
    assert metrics.answer == 0.6


def test_expected_dependency_missing_fails_answer():
    case = _case(expect_dependency=True)
    text = _plan([_subtask(1, "list_projects"), _subtask(2, "build_report")])
    metrics, components = score_output(case, text)
    assert "expected_dependency_missing" in components.answer
    assert metrics.answer == 0.8


def test_ungrounded_hint_reduces_evidence():
    text = _plan(
        [
            _subtask(1, "list_projects"),
            _subtask(2, "ghost_analyzer"),
        ]
    )
    metrics, components = score_output(_case(), text)
    assert "tool_hints_not_in_available_tools" in components.evidence
    assert metrics.evidence == 0.5


def test_all_hints_ungrounded_zeroes_evidence():
    text = _plan(
        [
            _subtask(1, "ghost_one"),
            _subtask(2, "ghost_two"),
        ]
    )
    metrics, _ = score_output(_case(), text)
    assert metrics.evidence == 0.0


def test_unknown_dependency_reference_reduces_evidence():
    text = _plan(
        [
            _subtask(1, "list_projects"),
            _subtask(2, "build_report", depends=["st_9"], subtype="sequential"),
        ]
    )
    metrics, components = score_output(_case(), text)
    assert "subtask_entries_or_dependencies_invalid" in components.schema
    assert metrics.schema == 0.75


def test_duplicate_ids_fail_schema():
    subtasks = [
        {"id": "st_1", "description": "a", "tool_hints": ["list_projects"],
         "depends_on": [], "type": "parallel"},
        {"id": "st_1", "description": "b", "tool_hints": ["build_report"],
         "depends_on": [], "type": "parallel"},
    ]
    metrics, components = score_output(_case(), _plan(subtasks))
    assert "subtask_entries_or_dependencies_invalid" in components.schema


def test_invalid_plan_type_fails_schema():
    subtasks = [_subtask(1, "list_projects", subtype="sideways")]
    metrics, _ = score_output(_case(), _plan(subtasks))
    assert metrics.schema < 1.0


def test_oversized_subtask_list_fails_schema():
    subtasks = [_subtask(i, "list_projects") for i in range(1, 10)]
    metrics, components = score_output(_case(), _plan(subtasks))
    assert "subtasks_missing_or_out_of_bounds" in components.schema


def test_too_many_hints_fails_schema():
    entry = {
        "id": "st_1",
        "description": "a",
        "tool_hints": ["a", "b", "c", "d", "e"],
        "depends_on": [],
        "type": "parallel",
    }
    metrics, _ = score_output(_case(), _plan([entry]))
    assert metrics.schema == 0.75


def test_empty_hints_are_grounded_trivially():
    entry = _subtask(1, "list_projects")
    entry["tool_hints"] = []
    entry2 = _subtask(2, "build_report")
    entry2["tool_hints"] = []
    text = _plan([entry, entry2])
    metrics, components = score_output(_case(), text)
    assert "tool_hints_not_in_available_tools" not in components.evidence
    assert metrics.evidence == 1.0


def test_malformed_subtask_entries_fail_schema():
    case = _case()
    variants = [
        ["not-a-dict", _subtask(2, "build_report")],
        [{"id": "  ", "description": "a", "tool_hints": [], "depends_on": [],
          "type": "parallel"}],
        [{"id": "st_1", "description": 5, "tool_hints": [], "depends_on": [],
          "type": "parallel"}],
        [{"id": "st_1", "description": "a", "tool_hints": "nope",
          "depends_on": [], "type": "parallel"}],
        [{"id": "st_1", "description": "a", "tool_hints": ["list", 5],
          "depends_on": [], "type": "parallel"}],
    ]
    for subtasks in variants:
        metrics, components = score_output(case, _plan(subtasks))
        assert "subtask_entries_or_dependencies_invalid" in components.schema
        assert metrics.schema == 0.75


def test_combined_weighting():
    metrics = CaseMetrics(schema=0.5, answer=0.5, evidence=1.0)
    assert metrics.combined == round(0.4 * 0.5 + 0.4 * 0.5 + 0.2 * 1.0, 4)


def test_metrics_to_dict_roundtrip():
    metrics = CaseMetrics(schema=1.0, answer=0.5, evidence=0.25)
    assert metrics.to_dict() == {
        "schema": 1.0,
        "answer": 0.5,
        "evidence": 0.25,
        "combined": metrics.combined,
    }
