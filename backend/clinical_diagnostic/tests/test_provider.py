"""Tests for clinical_diagnostic.provider: reply/config bounds, deterministic doubles,
frozen answer producers, and the opt-in OpenAI-compatible error mapping.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clinical_diagnostic.models import AnswerSource, ConversationMessage, Partition, RubricItem, ClinicalCase, Setting
from clinical_diagnostic.provider import (
    BareModelProducer,
    CallableProvider,
    CompletionConfig,
    FrozenAnswerProducer,
    OpenAICompletionProvider,
    ProducerError,
    ProviderReply,
    ProviderTimeout,
    RateLimited,
    ScriptedCompletionProvider,
    load_completion_config,
)


def case(**over):
    fields = dict(
        case_id="case-1",
        partition=Partition.DEVELOPMENT,
        setting=Setting.CONSULTATION,
        specialty="primary care",
        conversation=(ConversationMessage("user", "Synthetic question?"),),
        rubric_items=(RubricItem("c1", "Criterion.", 1.0),),
        frozen_answers={"candidate": "Candidate answer.", "baseline": "Baseline answer."},
    )
    fields.update(over)
    return ClinicalCase(**fields)


def test_provider_reply_validates_text_identity_and_cost():
    with pytest.raises(ValueError, match="non-empty"):
        ProviderReply("", "m")
    with pytest.raises(ValueError, match="model identity"):
        ProviderReply("x", "")
    with pytest.raises(ValueError, match="cost"):
        ProviderReply("x", "m", cost=-1)
    assert ProviderReply("x", "m", cost=0.1).to_dict()["cost"] == 0.1


def test_completion_config_validates_and_identifies():
    config = CompletionConfig(base_url="http://127.0.0.1:9/v1", model="synthetic")
    assert config.identity == "openai-compatible/synthetic"
    assert config.to_dict()["model"] == "synthetic"
    with pytest.raises(ValueError, match="base_url"):
        CompletionConfig(base_url="", model="m")
    with pytest.raises(ValueError, match="max_tokens"):
        CompletionConfig(base_url="u", model="m", max_tokens=0)
    with pytest.raises(ValueError, match="temperature"):
        CompletionConfig(base_url="u", model="m", temperature=3)
    with pytest.raises(ValueError, match="timeout_seconds"):
        CompletionConfig(base_url="u", model="m", timeout_seconds=0)
    with pytest.raises(ValueError, match="input_cost_per_token"):
        CompletionConfig(base_url="u", model="m", input_cost_per_token=-1)


def test_load_completion_config_reads_only_declared_fields(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "base_url": "http://127.0.0.1:9/v1", "model": "synthetic", "api_key": "key"
    }), encoding="utf-8")
    assert load_completion_config(str(path)).api_key == "key"
    path.write_text(json.dumps({"base_url": "u", "model": "m", "secret": "x"}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        load_completion_config(str(path))


def test_callable_and_scripted_providers_are_deterministic():
    provider = CallableProvider(lambda messages: ProviderReply(
        messages[-1]["content"], "callable"), "callable")
    assert provider.complete([{"role": "user", "content": "x"}]).text == "x"
    with pytest.raises(TypeError):
        CallableProvider(None, "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="identity"):
        CallableProvider(lambda _m: ProviderReply("x", "m"), "")

    scripted = ScriptedCompletionProvider(replies=["one"])
    assert scripted.complete([{"role": "user", "content": "x"}]).text == "one"
    with pytest.raises(ProducerError, match="exhausted"):
        scripted.complete([{"role": "user", "content": "x"}])
    scripted.fail_next(RateLimited())
    with pytest.raises(RateLimited):
        scripted.complete([{"role": "user", "content": "x"}])


def test_frozen_answer_producer_selects_actor_and_reports_missing_answers():
    candidate = FrozenAnswerProducer("candidate")
    baseline = FrozenAnswerProducer("baseline")
    assert candidate.produce(case()).text == "Candidate answer."
    assert baseline.produce(case()).text == "Baseline answer."
    assert candidate.source is AnswerSource.FROZEN_CANDIDATE
    assert baseline.source is AnswerSource.FROZEN_BASELINE
    with pytest.raises(ValueError):
        FrozenAnswerProducer("other")
    with pytest.raises(ProducerError) as caught:
        candidate.produce(case(frozen_answers={"baseline": "x"}))
    assert caught.value.code.value == "malformed_answer"


def test_bare_model_producer_uses_the_provider_reply():
    producer = BareModelProducer(
        CallableProvider(lambda _m: ProviderReply("answer", "model-1", cost=0.03),
                         "model-1")
    )
    answer = producer.produce(case())
    assert answer.text == "answer"
    assert answer.source is AnswerSource.BARE
    assert answer.model_identity == "model-1"
    assert answer.cost == 0.03


class _Choice:
    def __init__(self, content):
        self.message = SimpleNamespace(content=content)


class _Completion:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _Client:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_Completion(response))

    def close(self):
        pass


def _patch_client(monkeypatch, response):
    import llm_config.client_factory as factory
    from clinical_diagnostic.budget import use_control
    from clinical_diagnostic.execution import execute_inline

    def inline(kind, payload, control):
        with use_control(control):
            return execute_inline(kind, payload)

    monkeypatch.setattr("clinical_diagnostic.provider.supervise", inline)

    monkeypatch.setattr(factory, "build_llm_client",
                        lambda *_a, **_k: (_Client(response), object(), object()))


def test_openai_provider_returns_text_and_costs(monkeypatch):
    _patch_client(monkeypatch, SimpleNamespace(
        choices=[_Choice("Synthetic answer.")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    ))
    provider = OpenAICompletionProvider(CompletionConfig(
        base_url="http://127.0.0.1:9/v1", model="m",
        input_cost_per_token=0.01, output_cost_per_token=0.02,
    ))
    reply = provider.complete([{"role": "user", "content": "q"}])
    assert reply.text == "Synthetic answer."
    assert reply.cost == pytest.approx(0.2)
    assert reply.model_identity == "m"


def test_openai_provider_maps_rate_limit_timeout_and_other_failures(monkeypatch):
    class RateLimitError(Exception):
        status_code = 429

    provider = OpenAICompletionProvider(
        CompletionConfig(base_url="http://127.0.0.1:9/v1", model="m")
    )
    for error, expected in ((RateLimitError(), RateLimited), (TimeoutError(), ProviderTimeout),
                            (RuntimeError("boom"), ProducerError)):
        _patch_client(monkeypatch, error)
        with pytest.raises(expected):
            provider.complete([{"role": "user", "content": "q"}])


def test_openai_provider_refuses_empty_choices(monkeypatch):
    provider = OpenAICompletionProvider(
        CompletionConfig(base_url="http://127.0.0.1:9/v1", model="m")
    )
    for response in (SimpleNamespace(choices=[]), SimpleNamespace(choices=[_Choice(None)])):
        _patch_client(monkeypatch, response)
        with pytest.raises(ProducerError):
            provider.complete([{"role": "user", "content": "q"}])
