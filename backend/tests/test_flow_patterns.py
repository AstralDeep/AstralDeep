"""Tests for security-by-construction flow patterns (orchestrator/flow_patterns.py): the
enabling flag, turn classification precedence, per-pattern constraints, out-of-plan
refusal, and tool-budget boundaries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import flow_patterns as fp  # noqa: E402


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("FF_FLOW_PATTERNS", raising=False)
    assert fp.flow_patterns_enabled() is False


def test_flag_on_truthy_spellings(monkeypatch):
    for val in ("1", "true", "TRUE", "Yes", "on", "  on  "):
        monkeypatch.setenv("FF_FLOW_PATTERNS", val)
        assert fp.flow_patterns_enabled() is True, val


def test_flag_off_falsey_spellings(monkeypatch):
    for val in ("0", "false", "no", "off", "", "maybe"):
        monkeypatch.setenv("FF_FLOW_PATTERNS", val)
        assert fp.flow_patterns_enabled() is False, val


def test_classify_attachment_forces_parser():
    assert (
        fp.classify_flow(
            "what is this then summarize it",
            tool_count=3,
            has_attachment=True,
        )
        == fp.PARSER
    )


def test_classify_parser_keywords():
    assert fp.classify_flow("parse the budget file") == fp.PARSER
    assert fp.classify_flow("Please extract the tables") == fp.PARSER
    assert fp.classify_flow("read the file and tell me the totals") == fp.PARSER


def test_classify_lookup_question_is_read_only():
    assert fp.classify_flow("what is the capital of France?") == fp.READ_ONLY
    assert fp.classify_flow("Who owns this agent") == fp.READ_ONLY
    assert fp.classify_flow("list my chats", tool_count=1) == fp.READ_ONLY
    assert fp.classify_flow("really?") == fp.READ_ONLY


def test_classify_lookup_with_two_tools_is_multi_tool():
    assert (
        fp.classify_flow("what changed across these repos?", tool_count=2)
        == fp.MULTI_TOOL
    )


def test_classify_multi_step_keywords_is_multi_tool():
    assert (
        fp.classify_flow("fetch the data then chart it then email it")
        == fp.MULTI_TOOL
    )
    assert fp.classify_flow("do A, after that do B") == fp.MULTI_TOOL
    assert fp.classify_flow("run the steps to deploy") == fp.MULTI_TOOL


def test_classify_tool_count_two_is_multi_tool():
    assert fp.classify_flow("handle this request", tool_count=2) == fp.MULTI_TOOL


def test_classify_multi_tool_outranks_read_only():
    assert (
        fp.classify_flow("how do I build then ship this?", tool_count=4)
        == fp.MULTI_TOOL
    )


def test_classify_plain_statement_is_default():
    assert fp.classify_flow("Thanks, that looks great.") == fp.DEFAULT
    assert fp.classify_flow("Make it blue.") == fp.DEFAULT


def test_classify_empty_request_is_default():
    assert fp.classify_flow("") == fp.DEFAULT
    assert fp.classify_flow("   ") == fp.DEFAULT


def test_constraints_read_only():
    c = fp.constraints_for(fp.READ_ONLY)
    assert c.pattern == fp.READ_ONLY
    assert c.allow_free_tool_calls is False
    assert c.requires_plan is False
    assert c.max_tools == 1


def test_constraints_multi_tool():
    c = fp.constraints_for(fp.MULTI_TOOL)
    assert c.pattern == fp.MULTI_TOOL
    assert c.allow_free_tool_calls is False
    assert c.requires_plan is True
    assert c.max_tools == 12


def test_constraints_parser():
    c = fp.constraints_for(fp.PARSER)
    assert c.pattern == fp.PARSER
    assert c.allow_free_tool_calls is False
    assert c.requires_plan is False
    assert c.max_tools == 4


def test_constraints_default():
    c = fp.constraints_for(fp.DEFAULT)
    assert c.pattern == fp.DEFAULT
    assert c.allow_free_tool_calls is True
    assert c.requires_plan is False
    assert c.max_tools == 8


def test_constraints_unknown_falls_back_to_default_limits():
    c = fp.constraints_for("nonsense")
    base = fp.constraints_for(fp.DEFAULT)
    assert c.pattern == "nonsense"
    assert c.allow_free_tool_calls == base.allow_free_tool_calls
    assert c.requires_plan == base.requires_plan
    assert c.max_tools == base.max_tools


def test_constraints_are_frozen():
    import dataclasses

    c = fp.constraints_for(fp.READ_ONLY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.max_tools = 99  # type: ignore[misc]


def test_refuse_out_of_plan_in_plan_allowed():
    assert fp.refuse_out_of_plan(["search", "fetch_page"], "fetch_page") is False


def test_refuse_out_of_plan_out_of_plan_refused():
    assert fp.refuse_out_of_plan(["search", "fetch_page"], "delete_user") is True


def test_refuse_out_of_plan_empty_plan_refuses_everything():
    assert fp.refuse_out_of_plan([], "search") is True
    assert fp.refuse_out_of_plan(None, "search") is True  # type: ignore[arg-type]


def test_refuse_out_of_plan_case_insensitive():
    assert fp.refuse_out_of_plan(["Search", "Fetch_Page"], "search") is False
    assert fp.refuse_out_of_plan(["search"], "  SEARCH  ") is False


def test_within_tool_budget_read_only_boundary():
    assert fp.within_tool_budget(fp.READ_ONLY, 0) is True
    assert fp.within_tool_budget(fp.READ_ONLY, 1) is True
    assert fp.within_tool_budget(fp.READ_ONLY, 2) is False


def test_within_tool_budget_parser_boundary():
    assert fp.within_tool_budget(fp.PARSER, 4) is True
    assert fp.within_tool_budget(fp.PARSER, 5) is False


def test_within_tool_budget_multi_tool_boundary():
    assert fp.within_tool_budget(fp.MULTI_TOOL, 12) is True
    assert fp.within_tool_budget(fp.MULTI_TOOL, 13) is False


def test_within_tool_budget_default_boundary():
    assert fp.within_tool_budget(fp.DEFAULT, 8) is True
    assert fp.within_tool_budget(fp.DEFAULT, 9) is False
