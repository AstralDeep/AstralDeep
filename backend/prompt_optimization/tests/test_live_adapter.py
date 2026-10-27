"""Focused tests for uncovered bounded-adapter branches: live client completion paths
via injected stub clients (never the network), degenerate scripted-double inputs, and
fallback error classification.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io
from prompt_optimization.fixture import PlannerCase

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

HEAD = "You are a task planner.\nReturn a JSON object.\n\nRules:\n"


def _limits(**overrides) -> budget_module.BudgetLimits:
    defaults = dict(
        max_reflection_calls=8,
        max_evaluation_calls=64,
        max_total_tokens=200000,
        max_retries_per_call=1,
        max_output_chars=4000,
    )
    defaults.update(overrides)
    return budget_module.BudgetLimits(**defaults)


def _case() -> PlannerCase:
    return PlannerCase(
        case_id="t-x",
        request="List the projects for the Atlas team and build one combined report.",
        available_tools=("list_projects", "build_report", "fetch_record"),
        min_subtasks=2,
        max_subtasks=2,
        expected_tools=frozenset({"list_projects", "build_report"}),
    )


class _StubResponse:
    def __init__(self, content, total_tokens=42):
        self.choices = [
            SimpleNamespace(message=SimpleNamespace(content=content))
        ]
        self.usage = SimpleNamespace(total_tokens=total_tokens)


class _StubClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, model, messages, temperature):
        self.calls += 1
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def _live(tmp_path, responses):
    config = tmp_path / "live.json"
    config.write_text(
        json.dumps({"base_url": "http://127.0.0.1:9999/v1", "model": "synthetic"}),
        encoding="utf-8",
    )
    live = model_io.LiveCompletion(str(config))
    live._client = _StubClient(responses)
    return live


def test_live_completion_success_with_usage(tmp_path):
    live = _live(tmp_path, [_StubResponse("answer text", 55)])
    result = live.complete(
        "evaluation", "t-x", [{"role": "user", "content": "hi"}], 4000, 1
    )
    assert result.outcome == "succeeded"
    assert result.attempts == 1
    assert result.total_tokens == 55
    assert result.usage_complete is True
    assert result.text == "answer text"


def test_live_completion_truncates_and_flags_unknown_usage(tmp_path):
    live = _live(tmp_path, [_StubResponse("x" * 100, None)])
    result = live.complete(
        "evaluation", "t-x", [{"role": "user", "content": "hi"}], 20, 1
    )
    assert len(result.text) == 20
    assert result.total_tokens is None
    assert result.usage_complete is False


def test_live_completion_retries_then_succeeds(tmp_path):
    error = RuntimeError("provider hiccup")
    live = _live(tmp_path, [error, _StubResponse("recovered", 10)])
    result = live.complete(
        "evaluation", "t-x", [{"role": "user", "content": "hi"}], 4000, 2
    )
    assert result.outcome == "succeeded"
    assert result.attempts == 2


def test_live_completion_exhausts_retries(tmp_path):
    live = _live(tmp_path, [RuntimeError("down"), RuntimeError("still down")])
    result = live.complete(
        "evaluation", "t-x", [{"role": "user", "content": "hi"}], 4000, 2
    )
    assert result.outcome == "failed"
    assert result.attempts == 2
    assert result.error_class == "RuntimeError"
    assert result.usage_complete is False


def test_live_build_uses_declared_config_only(tmp_path, monkeypatch):
    config = tmp_path / "live.json"
    config.write_text(
        json.dumps({"base_url": "http://127.0.0.1:9999/v1", "model": "synthetic"}),
        encoding="utf-8",
    )
    live = model_io.LiveCompletion(str(config))
    seen = {}

    def fake_factory(config_obj, source):
        seen["base_url"] = config_obj.base_url
        seen["model"] = config_obj.model
        seen["api_key"] = config_obj.api_key
        seen["source"] = source
        return object(), "resolved", SimpleNamespace(model=config_obj.model)

    monkeypatch.setattr(
        "llm_config.client_factory.build_llm_client", fake_factory
    )
    client = live._build()
    assert client is not None
    assert live._build() is client
    assert seen["base_url"] == "http://127.0.0.1:9999/v1"
    assert seen["model"] == "synthetic"
    assert seen["api_key"] == ""
    assert seen["source"].value == "USER" or "USER" in str(seen["source"])


def test_double_degenerate_user_content_produces_bounded_plan():
    double = model_io.ProviderDouble()
    messages = [
        {"role": "system", "content": HEAD + BASELINE_RULES},
        {"role": "user", "content": "no case markers here"},
    ]
    result = double.complete("evaluation", "t-x", messages, 4000, 1)
    plan = json.loads(result.text)
    assert len(plan["subtasks"]) == 1
    assert plan["subtasks"][0]["tool_hints"] == ["unlisted_tool"]


def test_bounded_complete_classifies_unknown_error_class():
    class _FailingAdapter:
        def complete(self, kind, subject, messages, max_output_chars, max_attempts):
            return model_io.ModelCallResult(
                "failed", None, 1, None, False, None
            )

    budget = budget_module.Budget(_limits())
    with pytest.raises(model_io.ProviderFailed) as excinfo:
        model_io.bounded_complete(
            budget,
            _FailingAdapter(),
            kind="evaluation",
            phase="search",
            subject="t-x",
            messages=[{"role": "user", "content": "hi"}],
            controls=model_io.Controls(lambda: False),
            max_output_chars=4000,
            max_attempts=1,
        )
    assert excinfo.value.record.error_class is None
