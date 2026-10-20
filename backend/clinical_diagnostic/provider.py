"""Supplies bounded answer producers for the clinical diagnostic: frozen corpus answers,
an injected deterministic provider double, and an opt-in OpenAI-compatible completion path
that never reads ambient environment credentials.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from clinical_diagnostic.models import AnswerSource, CaseAnswer, ClinicalCase, FailureCode
from clinical_diagnostic.budget import BudgetExhausted, RunCancelled, current_control, operation_control
from clinical_diagnostic.execution import supervise
from clinical_diagnostic.identity import public_url

MAX_COMPLETION_CHARS = 64 * 1024
DEFAULT_MAX_TOKENS = 900
DEFAULT_TIMEOUT_SECONDS = 120.0


class ProducerError(RuntimeError):
    def __init__(self, detail: str, *, code: FailureCode = FailureCode.PROVIDER_ERROR,
                 retryable: bool = False, completed_cost: Optional[float] = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable
        if completed_cost is not None and (isinstance(completed_cost, bool)
                or not isinstance(completed_cost, (int, float))
                or not math.isfinite(completed_cost) or completed_cost < 0):
            raise ValueError("completed cost must be finite and non-negative")
        self.completed_cost = completed_cost


class RateLimited(ProducerError):
    def __init__(self, detail: str = "provider rate limited the request") -> None:
        super().__init__(detail, code=FailureCode.RATE_LIMITED, retryable=True)


class ProviderTimeout(ProducerError):
    def __init__(self, detail: str = "provider request timed out") -> None:
        super().__init__(detail, code=FailureCode.TIMEOUT, retryable=True)


@dataclass(frozen=True)
class ProviderReply:
    text: str
    model_identity: str
    cost: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("provider reply text must be non-empty")
        if len(self.text) > MAX_COMPLETION_CHARS:
            raise ValueError(f"provider reply exceeds {MAX_COMPLETION_CHARS} characters")
        if (not isinstance(self.model_identity, str) or not self.model_identity.strip()
                or len(self.model_identity) > 64):
            raise ValueError("provider reply must name its model identity")
        if isinstance(self.cost, bool) or not isinstance(self.cost, (int, float)):
            raise ValueError("provider reply cost must be a finite number")
        if not math.isfinite(float(self.cost)) or float(self.cost) < 0:
            raise ValueError("provider reply cost must be finite and non-negative")

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "model_identity": self.model_identity,
            "cost": float(self.cost),
        }


@dataclass(frozen=True)
class CompletionConfig:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.0
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0

    def __post_init__(self) -> None:
        for name in ("base_url", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"completion config must declare {name}")
        if not 1 <= self.max_tokens <= 4096:
            raise ValueError("max_tokens must be within 1..4096")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be within 0..2")
        if not 1.0 <= self.timeout_seconds <= 600.0:
            raise ValueError("timeout_seconds must be within 1..600")
        for name in ("input_cost_per_token", "output_cost_per_token"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite number")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        public_url(self.base_url, ("http", "https"))

    @property
    def identity(self) -> str:
        return f"openai-compatible/{self.model}"

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
            "input_cost_per_token": float(self.input_cost_per_token),
            "output_cost_per_token": float(self.output_cost_per_token),
        }


def load_completion_config(path: str) -> CompletionConfig:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError("completion config must be a JSON object")
    allowed = {
        "base_url", "model", "api_key", "max_tokens", "temperature",
        "timeout_seconds", "input_cost_per_token", "output_cost_per_token",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"completion config has unknown fields: {sorted(unknown)}")
    return CompletionConfig(**{key: payload[key] for key in payload})


class CompletionProvider:
    identity = "completion"

    def complete(self, messages: Sequence[dict]) -> ProviderReply:
        raise NotImplementedError


class CallableProvider(CompletionProvider):
    def __init__(self, completion: Callable[[Sequence[dict]], ProviderReply],
                 identity: str) -> None:
        if not callable(completion):
            raise TypeError("completion callable is required")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("provider identity is required")
        self._completion = completion
        self.identity = identity

    def complete(self, messages: Sequence[dict]) -> ProviderReply:
        return self._completion(messages)


class OpenAICompletionProvider(CompletionProvider):
    def __init__(self, config: CompletionConfig) -> None:
        self.config = config
        self.identity = config.identity

    def complete(self, messages: Sequence[dict]) -> ProviderReply:
        control = operation_control(self.config.timeout_seconds)
        return supervise("completion", {"config": asdict(self.config), "messages": list(messages)}, control)

    def _complete(self, messages: Sequence[dict]) -> ProviderReply:
        from llm_config.client_factory import build_llm_client
        from llm_config.types import CredentialSource

        class _Config:
            base_url = self.config.base_url
            model = self.config.model
            api_key = self.config.api_key

        control = current_control() or operation_control(self.config.timeout_seconds)
        client = None
        reply = None
        try:
            client, _source, _resolved = build_llm_client(
                _Config(), CredentialSource.USER, timeout=control.remaining_seconds(self.config.timeout_seconds)
            )
            response = client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": message["role"], "content": message["content"]}
                    for message in messages
                ],
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                timeout=control.remaining_seconds(self.config.timeout_seconds),
            )
            reply = self._reply(response)
            return reply
        except (BudgetExhausted, RunCancelled, ProducerError):
            raise
        except Exception as exc:
            raise _provider_error(exc) from exc
        finally:
            if client is not None:
                failure = sys.exc_info()[1]
                try:
                    client.close()
                except Exception as exc:
                    if failure is None:
                        error = _provider_error(exc)
                        error.completed_cost = reply.cost if reply is not None else None
                        raise error from exc

    def _reply(self, response: object) -> ProviderReply:
        cost = None
        try:
            usage = getattr(response, "usage", None)
            counts = [getattr(usage, key, 0) for key in ("prompt_tokens", "completion_tokens")]
            if any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in counts):
                raise ValueError("invalid usage counters")
            cost = counts[0] * self.config.input_cost_per_token + counts[1] * self.config.output_cost_per_token
            if not math.isfinite(cost):
                cost = None
                raise ValueError("usage cost overflow")
            return ProviderReply(text=_message_text(response),
                                 model_identity=getattr(response, "model", None) or "unknown", cost=cost)
        except (ValueError, TypeError, OverflowError, ProducerError) as exc:
            raise ProducerError("provider returned a malformed response", completed_cost=cost) from exc


class FrozenAnswerProducer:
    source = AnswerSource.FROZEN_CANDIDATE

    def __init__(self, answer_key: str = "candidate") -> None:
        if answer_key not in ("baseline", "candidate"):
            raise ValueError("frozen answer key must be baseline or candidate")
        self.answer_key = answer_key
        self.agreement_key = answer_key
        self.identity = f"frozen-corpus/{answer_key}"
        self.source = (
            AnswerSource.FROZEN_CANDIDATE if answer_key == "candidate"
            else AnswerSource.FROZEN_BASELINE
        )

    def produce(self, case: ClinicalCase) -> CaseAnswer:
        try:
            text = case.frozen_answers[self.answer_key]
        except KeyError as exc:
            raise ProducerError(
                f"{case.case_id}: no frozen {self.answer_key} answer",
                code=FailureCode.MALFORMED_ANSWER,
            ) from exc
        return CaseAnswer(
            case_id=case.case_id,
            source=self.source,
            text=text,
            model_identity=self.identity,
        )


class BareModelProducer:
    source = AnswerSource.BARE

    def __init__(self, provider: CompletionProvider) -> None:
        self.provider = provider
        self.identity = provider.identity

    def produce(self, case: ClinicalCase) -> CaseAnswer:
        reply = self.provider.complete(case.prompt_messages())
        return CaseAnswer(
            case_id=case.case_id,
            source=self.source,
            text=reply.text,
            model_identity=reply.model_identity,
            cost=reply.cost,
        )


def _provider_error(exc: Exception) -> ProducerError:
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None)
    if status == 429 or "ratelimit" in name:
        return RateLimited()
    if "timeout" in name or "timeout" in str(exc).lower():
        return ProviderTimeout()
    from httpx import HTTPError
    from openai import OpenAIError

    code = (FailureCode.PROVIDER_ERROR if isinstance(exc, (OSError, HTTPError, OpenAIError))
            else FailureCode.INTERNAL_ERROR)
    return ProducerError(type(exc).__name__, code=code)


def _message_text(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, (list, tuple)) or not choices:
        raise ProducerError("provider returned no choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ProducerError("provider returned an empty answer")
    return content


@dataclass
class ScriptedCompletionProvider(CompletionProvider):
    replies: list[str]
    identity: str = "scripted-provider/1"
    calls: int = field(default=0, init=False)
    _failures: list[Optional[Exception]] = field(default_factory=list, init=False)

    def complete(self, messages: Sequence[dict]) -> ProviderReply:
        if self._failures:
            failure = self._failures.pop(0)
            if failure is not None:
                raise failure
        if self.calls >= len(self.replies):
            raise ProducerError("scripted provider exhausted its replies")
        text = self.replies[self.calls]
        self.calls += 1
        return ProviderReply(text=text, model_identity=self.identity)

    def fail_next(self, failure: Exception) -> None:
        self._failures.append(failure)


__all__ = [
    "BareModelProducer",
    "CallableProvider",
    "CompletionConfig",
    "CompletionProvider",
    "FrozenAnswerProducer",
    "OpenAICompletionProvider",
    "ProducerError",
    "ProviderReply",
    "ProviderTimeout",
    "RateLimited",
    "ScriptedCompletionProvider",
    "load_completion_config",
]
