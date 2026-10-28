"""Runs deterministic scripted model doubles for the offline workbench.
The bounded wrapper reserves each attempt and retains incomplete accounting; no live provider or credential adapter is exposed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from prompt_optimization import budget as budget_module
from prompt_optimization import task as task_module
from prompt_optimization.fixture import PlannerCase, render_case_block

SYSTEM_FLAG_CAP = re.compile(r"at most (\d+) subtasks")


class RunCancelled(RuntimeError):
    pass


class ProviderDenied(RuntimeError):
    def __init__(self, message: str, record: object) -> None:
        super().__init__(message)
        self.record = record


class ProviderFailed(RuntimeError):
    def __init__(self, message: str, record: object) -> None:
        super().__init__(message)
        self.record = record


@dataclass(frozen=True)
class ModelCallResult:
    outcome: str
    text: Optional[str]
    attempts: int
    total_tokens: Optional[int]
    usage_complete: bool
    error_class: Optional[str] = None


@dataclass(frozen=True)
class Controls:
    is_cancelled: Any

    def cancelled(self) -> bool:
        return bool(self.is_cancelled())


def evaluation_messages(prompt_text: str, case: PlannerCase) -> List[dict]:
    return [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": render_case_block(case)},
    ]


def _tool_tokens(tool: str) -> set:
    return {part for part in tool.split("_") if part}


def _excluded_tools(available: Tuple[str, ...], request: str) -> set:
    excluded = set()
    for tool in available:
        for keyword in _tool_tokens(tool):
            pattern = (
                rf"(?:\b(?:no|not|never|without)\b)[^.;]*?\b{re.escape(keyword)}\b"
            )
            if re.search(pattern, request):
                excluded.add(tool)
                break
    return excluded


def _matched_tools(available: Tuple[str, ...], request: str) -> List[str]:
    tokens = set(re.findall(r"[a-z0-9]+", request.lower()))
    return [
        tool
        for tool in available
        if _tool_tokens(tool) & tokens
    ]


class ProviderDouble:
    def __init__(
        self,
        *,
        variants: Tuple[str, ...] = (),
        faults: Optional[Dict[str, str]] = None,
        usage_mode: str = "complete",
    ) -> None:
        self.variants = tuple(variants)
        self.faults = dict(faults or {})
        self.usage_mode = usage_mode
        self._emitted = 0
        self._transients: set = set()

    def complete(
        self,
        kind: str,
        subject: str,
        messages: List[dict],
        max_output_chars: int,
        max_attempts: int,
    ) -> ModelCallResult:
        fault = self.faults.get(f"{kind}:{subject}") or self.faults.get(kind)
        if fault == "deny":
            return ModelCallResult("denied", None, 1, 0, True, "policy_denied")
        if fault == "transient" and (kind, subject) not in self._transients:
            self._transients.add((kind, subject))
            if max_attempts < 2:
                return ModelCallResult(
                    "failed", None, 1, None, False, "transient_timeout"
                )
            result = self._produce(kind, subject, messages, max_output_chars)
            return ModelCallResult(
                "succeeded",
                result[0],
                2,
                result[1],
                result[2],
            )
        text, tokens, usage_complete = self._produce(
            kind, subject, messages, max_output_chars
        )
        text = text[:max_output_chars]
        if fault == "malformed":
            text = '{"subtasks": [{"id": "st_1"'
        elif fault == "empty":
            text = ""
        if self.usage_mode == "incomplete":
            tokens, usage_complete = None, False
        return ModelCallResult("succeeded", text, 1, tokens, usage_complete)

    def _produce(
        self,
        kind: str,
        subject: str,
        messages: List[dict],
        max_output_chars: int,
    ) -> Tuple[str, int, bool]:
        system = messages[0]["content"] if messages else ""
        user = messages[-1]["content"] if len(messages) > 1 else ""
        if kind == "reflection":
            batch = self.variants[self._emitted : self._emitted + 2]
            self._emitted += len(batch)
            text = json.dumps(
                {"candidates": list(batch), "rationale": "scripted variant batch"},
                sort_keys=True,
            )
        else:
            _, rules = task_module.split_rendered(system)
            text = self._plan(rules, user)
        tokens = (len(system) + len(user) + len(text)) // 4
        usage_complete = True
        return text, tokens, usage_complete

    def _plan(self, rules: str, user: str) -> str:
        lowered = rules.lower()
        synthesis_rule = "synthesis" in lowered
        single_rule = "single subtask" in lowered
        grounding_rule = "available tools" in lowered
        dependency_rule = "depends_on" in lowered
        cap_match = SYSTEM_FLAG_CAP.search(lowered)
        cap = int(cap_match.group(1)) if cap_match else 4

        request = self._section(user, "[request]")
        available = tuple(
            line.strip()
            for line in self._section(user, "[available tools]").splitlines()
            if line.strip()
        )
        exclusion_rule = "exclude" in lowered
        excluded = _excluded_tools(available, request) if exclusion_rule else set()
        matched = [
            tool for tool in _matched_tools(available, request) if tool not in excluded
        ]
        if not matched:
            matched = [tool for tool in available if tool not in excluded]
        if not matched:
            matched = ["unlisted_tool"]
        simple = len(request.split()) <= 8
        if simple and single_rule:
            count = 1
        elif simple:
            count = 2
        else:
            count = max(1, min(len(matched), cap))

        subtasks = []
        for index in range(count):
            hint = matched[index % len(matched)]
            if index == 0 and not grounding_rule and matched:
                hint = matched[0] + "_pro"
            entry = {
                "id": f"st_{index + 1}",
                "description": f"Use {hint} for this part of the request",
                "tool_hints": [hint],
                "depends_on": [],
                "type": "parallel",
            }
            if index == count - 1 and count > 1 and dependency_rule:
                entry["type"] = "sequential"
                entry["depends_on"] = ["st_1"]
            subtasks.append(entry)
        plan = {
            "subtasks": subtasks,
            "synthesis": (
                "Combine the subtask results into one answer."
                if synthesis_rule
                else ""
            ),
        }
        return json.dumps(plan, sort_keys=True)

    @staticmethod
    def _section(user: str, marker: str) -> str:
        if marker not in user:
            return ""
        tail = user.split(marker, 1)[1]
        next_marker = tail.find("\n[")
        return tail[:next_marker] if next_marker >= 0 else tail


def bounded_complete(
    budget: budget_module.Budget,
    adapter: Any,
    *,
    kind: str,
    phase: str,
    subject: str,
    messages: List[dict],
    controls: Controls,
    max_output_chars: int,
    max_attempts: int,
) -> Tuple[str, budget_module.CallRecord]:
    if (type(max_output_chars) is not int or not 1 <= max_output_chars <= budget.limits.max_output_chars
            or type(max_attempts) is not int or not 1 <= max_attempts <= budget.limits.max_retries_per_call + 1):
        raise ValueError("call bounds exceed configured budget")
    prompt = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    allowance = budget_module.estimate_tokens(prompt) + max_output_chars * 4
    for attempt in range(max_attempts):
        if controls.cancelled():
            reservation = budget.reserve(kind, phase, subject, 1)
            budget.reconcile(reservation, outcome="cancelled", attempts=0,
                             actual_tokens=0, usage_complete=True)
            raise RunCancelled(f"cancelled before {kind} call {subject!r}")
        reservation = budget.reserve(kind, phase, subject, allowance)
        try:
            result = adapter.complete(kind, subject, messages, max_output_chars, 1)
            if (result.outcome not in budget_module.OUTCOMES or type(result.attempts) is not int or result.attempts not in (0, 1)
                    or result.outcome == "succeeded" and result.attempts != 1
                    or result.text is not None and type(result.text) is not str):
                raise ValueError("adapter violated single-attempt contract")
        except BaseException as error:
            budget.reconcile(reservation, outcome="failed", attempts=1,
                             actual_tokens=None, usage_complete=False,
                             error_class=type(error).__name__[:128])
            if not isinstance(error, Exception):
                raise
            if attempt + 1 < max_attempts:
                continue
            raise ProviderFailed(f"provider failed {kind} call {subject!r}", budget.records[-1]) from None
        record = budget.reconcile(
            reservation, outcome=result.outcome, attempts=result.attempts,
            actual_tokens=result.total_tokens, usage_complete=result.usage_complete,
            error_class=result.error_class,
        )
        if result.outcome == "succeeded":
            return (result.text or "")[:max_output_chars], record
        if result.outcome == "denied":
            raise ProviderDenied(f"provider denied {kind} call {subject!r}", record)
        if result.outcome == "cancelled":
            raise RunCancelled(f"cancelled during {kind} call {subject!r}")
        if attempt + 1 == max_attempts:
            raise ProviderFailed(f"provider failed {kind} call {subject!r}", record)
    raise AssertionError("unreachable call bounds")
