"""Tests for skill-memory recipe induction and matching (orchestrator/skill_memory.py):
the on/off flag, trace-to-recipe induction, keyword-overlap matching with tie-breaks,
and parameterizing a recipe into per-tool steps.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import skill_memory as sm  # noqa: E402
from orchestrator.skill_memory import Recipe  # noqa: E402


def test_skill_memory_default_off(monkeypatch):
    monkeypatch.delenv("FF_SKILL_MEMORY", raising=False)
    assert sm.skill_memory_enabled() is False


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "TRUE", "On", "  YES  "])
def test_skill_memory_on_values(monkeypatch, v):
    monkeypatch.setenv("FF_SKILL_MEMORY", v)
    assert sm.skill_memory_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off", "", "maybe"])
def test_skill_memory_off_values(monkeypatch, v):
    monkeypatch.setenv("FF_SKILL_MEMORY", v)
    assert sm.skill_memory_enabled() is False


def _csv_trace():
    return [
        {"tool": "read_csv", "args": {"path": "/x.csv"}},
        {"tool": "summarize", "args": {"path": "/x.csv", "max_words": 50}},
        {"tool": "write_report", "args": {"dest": "/out.md", "max_words": 50}},
    ]


def test_induce_recipe_orders_tools_with_duplicates():
    trace = [
        {"tool": "read_csv", "args": {"path": "/a"}},
        {"tool": "transform", "args": {"path": "/a"}},
        {"tool": "read_csv", "args": {"path": "/b"}},
    ]
    recipe = sm.induce_recipe("dup", trace)
    assert recipe.tools == ("read_csv", "transform", "read_csv")


def test_induce_recipe_params_are_sorted_and_unique():
    recipe = sm.induce_recipe("csv->report", _csv_trace())
    assert recipe.params == ("dest", "max_words", "path")


def test_induce_recipe_keywords_lowercased_and_deduped():
    recipe = sm.induce_recipe(
        "csv->report",
        _csv_trace(),
        trigger_keywords=["CSV", "Report", "csv", "Summarize"],
    )
    assert recipe.trigger_keywords == ("csv", "report", "summarize")


def test_induce_recipe_no_keywords_is_empty_tuple():
    recipe = sm.induce_recipe("csv->report", _csv_trace())
    assert recipe.trigger_keywords == ()


def test_induce_recipe_handles_step_without_args():
    trace = [
        {"tool": "ping"},
        {"tool": "read_csv", "args": {"path": "/x"}},
    ]
    recipe = sm.induce_recipe("partial", trace)
    assert recipe.tools == ("ping", "read_csv")
    assert recipe.params == ("path",)


def test_induce_recipe_returns_recipe_instance():
    recipe = sm.induce_recipe("csv->report", _csv_trace())
    assert isinstance(recipe, Recipe)
    assert recipe.name == "csv->report"


def test_induce_recipe_empty_trace_raises():
    with pytest.raises(ValueError):
        sm.induce_recipe("empty", [])


def test_recipe_is_frozen():
    recipe = sm.induce_recipe("csv->report", _csv_trace())
    with pytest.raises(Exception):
        recipe.name = "mutated"  # type: ignore[misc]


def _recipes():
    return [
        Recipe("weather", ("get_weather",), ("city",),
                ("weather", "forecast", "temperature")),
        Recipe("csv", ("read_csv", "summarize"), ("path",),
                ("csv", "spreadsheet")),
        Recipe("email", ("send_email",), ("to", "body"),
                ("email", "send")),
    ]


def test_match_recipe_picks_best_overlap():
    recipes = _recipes()
    assert sm.match_recipe(recipes, "what's the weather forecast today") is recipes[0]


def test_match_recipe_case_insensitive():
    recipes = _recipes()
    assert sm.match_recipe(recipes, "Read my CSV Spreadsheet") is recipes[1]


def test_match_recipe_none_when_below_min_overlap():
    recipes = _recipes()
    assert sm.match_recipe(recipes, "hello there friend") is None


def test_match_recipe_respects_min_overlap_threshold():
    recipes = _recipes()
    assert sm.match_recipe(recipes, "the weather is nice") is recipes[0]
    assert sm.match_recipe(recipes, "the weather is nice", min_overlap=2) is None


def test_match_recipe_tie_break_prefers_more_keywords():
    few = Recipe("few", ("t",), ("p",), ("apple",))
    many = Recipe("many", ("t",), ("p",), ("apple", "banana", "cherry"))
    assert sm.match_recipe([few, many], "i want an apple") is many
    assert sm.match_recipe([many, few], "i want an apple") is many


def test_match_recipe_tie_break_falls_back_to_order():
    first = Recipe("first", ("t",), ("p",), ("apple",))
    second = Recipe("second", ("t",), ("p",), ("apple",))
    assert sm.match_recipe([first, second], "an apple please") is first
    assert sm.match_recipe([second, first], "an apple please") is second


def test_match_recipe_empty_recipes_or_request():
    assert sm.match_recipe([], "anything") is None
    assert sm.match_recipe(_recipes(), "") is None


def test_parameterize_builds_one_step_per_tool():
    recipe = sm.induce_recipe("csv->report", _csv_trace())
    plan = sm.parameterize(
        recipe,
        {"path": "/in.csv", "max_words": 80, "dest": "/out.md"},
    )
    assert [s["tool"] for s in plan] == ["read_csv", "summarize", "write_report"]
    for step in plan:
        assert step["args"] == {"path": "/in.csv", "max_words": 80, "dest": "/out.md"}


def test_parameterize_only_includes_recipe_params():
    recipe = Recipe("r", ("toolA", "toolB"), ("path",), ())
    plan = sm.parameterize(recipe, {"path": "/x", "extra": "ignored"})
    assert plan == [
        {"tool": "toolA", "args": {"path": "/x"}},
        {"tool": "toolB", "args": {"path": "/x"}},
    ]
    assert all("extra" not in s["args"] for s in plan)


def test_parameterize_omits_missing_params():
    recipe = Recipe("r", ("toolA",), ("path", "limit"), ())
    plan = sm.parameterize(recipe, {"path": "/x"})
    assert plan == [{"tool": "toolA", "args": {"path": "/x"}}]


def test_parameterize_steps_have_independent_arg_dicts():
    recipe = Recipe("r", ("toolA", "toolB"), ("path",), ())
    plan = sm.parameterize(recipe, {"path": "/x"})
    plan[0]["args"]["path"] = "/mutated"
    assert plan[1]["args"]["path"] == "/x"


def test_parameterize_with_no_args_yields_empty_arg_steps():
    recipe = Recipe("r", ("toolA", "toolB"), ("path",), ())
    plan = sm.parameterize(recipe, {})
    assert plan == [
        {"tool": "toolA", "args": {}},
        {"tool": "toolB", "args": {}},
    ]


def test_missing_params_lists_absent_in_order():
    recipe = sm.induce_recipe("csv->report", _csv_trace())
    assert sm.missing_params(recipe, {"path": "/x"}) == ["dest", "max_words"]


def test_missing_params_empty_when_all_present():
    recipe = Recipe("r", ("toolA",), ("path", "limit"), ())
    assert sm.missing_params(recipe, {"path": "/x", "limit": 5}) == []


def test_missing_params_all_when_none_provided():
    recipe = Recipe("r", ("toolA",), ("path", "limit"), ())
    assert sm.missing_params(recipe, {}) == ["path", "limit"]
