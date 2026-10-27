"""Bounded model I/O for the workbench: a deterministic scripted provider double for
offline CI, an optional live adapter that reaches real models only through an explicit
runtime config file via the product client factory, and a reserve-then-reconcile call
wrapper that records denials, failures, cancellation, and incomplete usage accounting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
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
            return ModelCallResult("denied", None, 1, None, True, "policy_denied")
        if fault == "transient":
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


class LiveCompletion:
    def __init__(self, config_path: str) -> None:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload.get("base_url") or not payload.get("model"):
            raise ValueError("live config file must declare base_url and model")
        self.base_url = str(payload["base_url"])
        self.model = str(payload["model"])
        self.api_key = str(payload.get("api_key") or "")
        self._client: Any = None

    def _build(self) -> Any:
        if self._client is None:
            from llm_config.client_factory import build_llm_client
            from llm_config.types import CredentialSource

            class _Config:
                pass

            config = _Config()
            config.base_url = self.base_url
            config.model = self.model
            config.api_key = self.api_key
            self._client, _source, _resolved = build_llm_client(
                config, CredentialSource.USER
            )
        return self._client

    def complete(
        self,
        kind: str,
        subject: str,
        messages: List[dict],
        max_output_chars: int,
        max_attempts: int,
    ) -> ModelCallResult:
        del kind, subject
        client = self._build()
        attempts = 0
        last_error: Optional[str] = None
        while attempts < max(1, max_attempts):
            attempts += 1
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0,
                )
            except Exception as error:
                last_error = type(error).__name__[:128]
                continue
            text = (response.choices[0].message.content or "")[:max_output_chars]
            total = getattr(getattr(response, "usage", None), "total_tokens", None)
            usage_complete = type(total) is int and total >= 0
            return ModelCallResult(
                "succeeded", text, attempts, total if usage_complete else None,
                usage_complete,
            )
        return ModelCallResult("failed", None, attempts, None, False, last_error)


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
    if controls.cancelled():
        reservation = budget.reserve(
            kind, phase, subject, budget_module.estimate_tokens("cancelled")
        )
        record = budget.reconcile(
            reservation,
            outcome="cancelled",
            attempts=0,
            actual_tokens=None,
            usage_complete=True,
            error_class=None,
        )
        raise RunCancelled(f"cancelled before {kind} call {subject!r}")
    prompt_chars = sum(len(message.get("content") or "") for message in messages)
    reservation = budget.reserve(
        kind, phase, subject, prompt_chars // 2 + 64
    )
    result = adapter.complete(
        kind, subject, messages, max_output_chars, max_attempts
    )
    if result.outcome == "succeeded":
        record = budget.reconcile(
            reservation,
            outcome="succeeded",
            attempts=result.attempts,
            actual_tokens=result.total_tokens,
            usage_complete=result.usage_complete,
        )
        return result.text or "", record
    if result.outcome == "denied":
        record = budget.reconcile(
            reservation,
            outcome="denied",
            attempts=result.attempts,
            actual_tokens=None,
            usage_complete=True,
            error_class=result.error_class,
        )
        raise ProviderDenied(f"provider denied {kind} call {subject!r}", record)
    record = budget.reconcile(
        reservation,
        outcome="failed",
        attempts=result.attempts,
        actual_tokens=result.total_tokens,
        usage_complete=result.usage_complete and result.total_tokens is not None,
        error_class=result.error_class,
    )
    raise ProviderFailed(f"provider failed {kind} call {subject!r}", record)
