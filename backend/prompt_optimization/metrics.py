"""Deterministic, dependency-free metrics scoring a planner's raw output against one
fixture case on three independent dimensions: schema conformance, answer/decomposition
match against declared labels, and evidence grounding of tool hints in the visible tool
list. No model call and no hidden reasoning participate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

MAX_SUBTASKS = 8
MAX_HINTS_PER_SUBTASK = 4
PLAN_TYPES = frozenset({"parallel", "sequential"})

SCHEMA_WEIGHT = 0.4
ANSWER_WEIGHT = 0.4
EVIDENCE_WEIGHT = 0.2


@dataclass(frozen=True)
class CaseMetrics:
    schema: float
    answer: float
    evidence: float

    @property
    def combined(self) -> float:
        return round(
            SCHEMA_WEIGHT * self.schema
            + ANSWER_WEIGHT * self.answer
            + EVIDENCE_WEIGHT * self.evidence,
            4,
        )

    def to_dict(self) -> Dict[str, float]:
        return {
            "schema": self.schema,
            "answer": self.answer,
            "evidence": self.evidence,
            "combined": self.combined,
        }


@dataclass
class MetricComponents:
    schema: List[str] = field(default_factory=list)
    answer: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, List[str]]:
        return {"schema": self.schema, "answer": self.answer, "evidence": self.evidence}


def _fraction(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 1.0


def _plan_valid(plan: Any) -> Tuple[bool, bool, bool, bool]:
    if not isinstance(plan, dict):
        return False, False, False, False
    subtasks = plan.get("subtasks")
    subtasks_ok = isinstance(subtasks, list) and 1 <= len(subtasks) <= MAX_SUBTASKS
    entries_ok = False
    deps_ok = False
    synthesis_ok = isinstance(plan.get("synthesis"), str) and bool(plan.get("synthesis"))
    if subtasks_ok:
        entries_ok = True
        seen: List[str] = []
        deps_ok = True
        for index, entry in enumerate(subtasks):
            if not isinstance(entry, dict):
                entries_ok = False
                deps_ok = False
                break
            identifier = entry.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                entries_ok = False
                break
            if not isinstance(entry.get("description"), str):
                entries_ok = False
                break
            hints = entry.get("tool_hints")
            if not isinstance(hints, list) or len(hints) > MAX_HINTS_PER_SUBTASK:
                entries_ok = False
                break
            if not all(isinstance(hint, str) and hint.strip() for hint in hints):
                entries_ok = False
                break
            if entry.get("type") not in PLAN_TYPES:
                entries_ok = False
                break
            if identifier in seen:
                entries_ok = False
                break
            seen.append(identifier)
            depends = entry.get("depends_on")
            if not isinstance(depends, list) or not all(
                isinstance(dep, str) and dep in seen[:index] for dep in depends
            ):
                deps_ok = False
    return bool(plan), subtasks_ok, entries_ok and deps_ok and subtasks_ok, synthesis_ok


def _parse_plan(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _hint_grounded(case_tools: Tuple[str, ...], hints: List[str]) -> Tuple[float, List[str]]:
    allowed = set(case_tools)
    ungrounded = [hint for hint in hints if hint not in allowed]
    if not hints:
        return 1.0, []
    return _fraction(len(hints) - len(ungrounded), len(hints)), ungrounded


def score_output(case: Any, text: str) -> Tuple[CaseMetrics, MetricComponents]:
    components = MetricComponents()
    plan = _parse_plan(text or "")
    is_object, subtasks_ok, entries_ok, synthesis_ok = _plan_valid(plan)

    schema_checks = [is_object, subtasks_ok, entries_ok, synthesis_ok]
    schema_score = _fraction(sum(1 for check in schema_checks if check), len(schema_checks))
    if not is_object:
        components.schema.append("plan_not_json_object")
    if is_object and not subtasks_ok:
        components.schema.append("subtasks_missing_or_out_of_bounds")
    if is_object and subtasks_ok and not entries_ok:
        components.schema.append("subtask_entries_or_dependencies_invalid")
    if is_object and not synthesis_ok:
        components.schema.append("synthesis_missing")

    answer_score = 0.0
    evidence_score = 0.0
    if is_object and subtasks_ok and entries_ok:
        subtasks = plan["subtasks"]
        hints = [
            hint
            for entry in subtasks
            for hint in entry.get("tool_hints", [])
        ]
        count = len(subtasks)
        count_ok = case.min_subtasks <= count <= case.max_subtasks
        if not count_ok:
            components.answer.append("subtask_count_out_of_range")
        tools_ok = all(
            hint in set.union(set(case.expected_tools), set(case.available_tools))
            for hint in hints
        ) and set(case.expected_tools) <= set(hints)
        if not tools_ok:
            components.answer.append("expected_tools_not_covered")
        forbidden = sorted(set(hints) & set(case.forbidden_tools))
        if forbidden:
            components.answer.append("forbidden_tool_used")
        simple_ok = (count == 1) if case.simple else True
        if case.simple and not simple_ok:
            components.answer.append("simple_request_not_single_subtask")
        dependency_ok = True
        if case.expect_dependency:
            dependency_ok = any(entry.get("depends_on") for entry in subtasks)
            if not dependency_ok:
                components.answer.append("expected_dependency_missing")
        answer_checks = [count_ok, tools_ok, not forbidden, simple_ok, dependency_ok]
        answer_score = _fraction(
            sum(1 for check in answer_checks if check), len(answer_checks)
        )
        if forbidden:
            answer_score = 0.0

        evidence_fraction, ungrounded = _hint_grounded(case.available_tools, hints)
        if ungrounded:
            components.evidence.append("tool_hints_not_in_available_tools")
        evidence_score = evidence_fraction

    return (
        CaseMetrics(
            schema=schema_score, answer=answer_score, evidence=evidence_score
        ),
        components,
    )
