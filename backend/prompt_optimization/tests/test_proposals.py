"""Tests for proposal plumbing: the frozen variant library, the scripted no-model
source, and bounded parsing of reflection replies that treats candidates as untrusted
text.
"""

from __future__ import annotations

import json

import pytest

from prompt_optimization import task
from prompt_optimization.proposals import (
    GROUNDING_REPLACEMENT,
    GROUNDING_SENTENCE,
    ProposalError,
    ReflectionProposalSource,
    ScriptedProposalSource,
    parse_reflection,
    scripted_variants,
)

BASELINE_RULES = (
    '- Use "parallel" type when subtasks are independent.\n'
    '- Use "sequential" type and specify "depends_on" when a subtask needs '
    "results from another.\n"
    "- Each subtask should map to 1-2 tool calls.\n"
    "- If the request is simple enough for a single tool call, return a single "
    "subtask.\n"
    "- tool_hints should reference actual tool names from the available tools "
    "list.\n"
)


def test_variant_library_is_cumulative_and_deterministic():
    variants = scripted_variants(BASELINE_RULES)
    assert len(variants) == 4
    assert variants == scripted_variants(BASELINE_RULES)
    assert variants[1].startswith(variants[0].rstrip("\n"))
    assert variants[2].startswith(variants[1].rstrip("\n"))
    assert "synthesis field" in variants[0]
    assert "at most 3 subtasks" in variants[1]
    assert "explicit exclusions" in variants[2]
    for variant in variants[:3]:
        assert "available tools list" in variant
    assert "available tools list" not in variants[3]
    assert "Prefer broadly relevant tool hints" in variants[3]
    assert variants[3] == variants[2].replace(
        GROUNDING_SENTENCE, GROUNDING_REPLACEMENT
    )


def test_variant_library_fails_closed_without_grounding_sentence():
    stripped = BASELINE_RULES.replace(
        "- tool_hints should reference actual tool names from the available "
        "tools list.\n",
        "",
    )
    with pytest.raises(task.TaskSeamError):
        scripted_variants(stripped)


def test_all_variants_pass_instruction_validation():
    for variant in scripted_variants(BASELINE_RULES):
        assert task.validate_instruction(variant) == variant.strip()


def test_scripted_source_emits_each_variant_once():
    variants = scripted_variants(BASELINE_RULES)
    source = ScriptedProposalSource(variants)
    first = source.propose(0, "feedback", BASELINE_RULES)
    second = source.propose(1, "feedback", BASELINE_RULES)
    assert first == list(variants)
    assert second == []
    assert source.proposed == list(variants)


def test_parse_reflection_golden():
    text = json.dumps(
        {
            "candidates": ["Instruction one.", "Instruction two."],
            "rationale": "because",
        }
    )
    parsed, rationale = parse_reflection(text, 4)
    assert parsed == ["Instruction one.", "Instruction two."]
    assert rationale == "because"


def test_parse_reflection_deduplicates_and_validates():
    text = json.dumps(
        {
            "candidates": ["Same text.", "  Same text.  ", "Same text."],
            "rationale": "dedupe",
        }
    )
    parsed, _ = parse_reflection(text, 4)
    assert parsed == ["Same text."]


def test_parse_reflection_rejects_malformed_shapes():
    with pytest.raises(ProposalError):
        parse_reflection("not json at all", 4)
    with pytest.raises(ProposalError):
        parse_reflection(json.dumps(["list"]), 4)
    with pytest.raises(ProposalError):
        parse_reflection(json.dumps({"rationale": "r"}), 4)
    with pytest.raises(ProposalError):
        parse_reflection(json.dumps({"candidates": "nope", "rationale": "r"}), 4)
    with pytest.raises(ProposalError):
        parse_reflection(json.dumps({"candidates": [], "rationale": 7}), 4)
    with pytest.raises(ProposalError):
        parse_reflection(json.dumps({"candidates": [], "rationale": "x" * 501}), 4)


def test_parse_reflection_rejects_over_budget_candidate_count():
    text = json.dumps(
        {"candidates": ["a", "b", "c"], "rationale": "r"}
    )
    with pytest.raises(ProposalError):
        parse_reflection(text, 2)


def test_parse_reflection_rejects_untrusted_candidate_text():
    for bad in ("", "bad\x00text", "x" * 2001, 42, ["list"], None):
        text = json.dumps({"candidates": [bad], "rationale": "r"})
        with pytest.raises(ProposalError):
            parse_reflection(text, 4)


def test_reflection_messages_are_bounded_and_labeled():
    source = _source()
    messages = source.reflection_messages("BASELINE", "FEEDBACK", 3)
    assert messages[0]["role"] == "system"
    assert "allowlisted" in messages[0]["content"]
    assert "Never emit executable" in messages[0]["content"]
    assert "[baseline instruction]" in messages[1]["content"]
    assert "BASELINE" in messages[1]["content"]
    assert "FEEDBACK" in messages[1]["content"]
    assert "at most 3" in messages[1]["content"]


def _source() -> ReflectionProposalSource:
    from prompt_optimization import budget as budget_module
    from prompt_optimization import model_io

    limits = budget_module.BudgetLimits(
        max_reflection_calls=4,
        max_evaluation_calls=8,
        max_total_tokens=100000,
    )
    adapter = _JsonReflectionStub()
    return ReflectionProposalSource(
        adapter=adapter,
        budget=budget_module.Budget(limits),
        controls=model_io.Controls(lambda: False),
        max_candidates=3,
        max_output_chars=4000,
        max_attempts=1,
    )


class _JsonReflectionStub:
    def __init__(self, payload=None, faults=None):
        self.payload = payload or {"candidates": ["Instruction A."], "rationale": "ok"}
        self.faults = dict(faults or {})
        self.calls = []

    def complete(self, kind, subject, messages, max_output_chars, max_attempts):
        self.calls.append((kind, subject, messages))
        fault = self.faults.get(subject)
        if fault == "deny":
            return _Result("denied", None, 1, None, True, "policy_denied")
        text = json.dumps(self.payload)
        return _Result("succeeded", text, 1, 42, True)


class _Result:
    def __init__(self, outcome, text, attempts, total_tokens, usage_complete,
                 error_class=None):
        self.outcome = outcome
        self.text = text
        self.attempts = attempts
        self.total_tokens = total_tokens
        self.usage_complete = usage_complete
        self.error_class = error_class


def test_reflection_source_spends_budgeted_calls_and_parses():
    source = _source()
    candidates = source.propose(0, "feedback", "BASELINE")
    assert candidates == ["Instruction A."]
    assert source.adapter.calls[0][0] == "reflection"
    assert source.adapter.calls[0][1] == "round-0"
    assert source.budget.reflection_calls == 1
    assert source.rationales == ["ok"]


def test_reflection_source_propagates_denial():
    from prompt_optimization import model_io

    source = _source()
    source.adapter.faults["round-0"] = "deny"
    with pytest.raises(model_io.ProviderDenied):
        source.propose(0, "feedback", "BASELINE")
