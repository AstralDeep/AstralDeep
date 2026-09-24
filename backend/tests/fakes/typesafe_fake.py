"""Deterministic TypeSafe SDK stand-in substituted at TypeSafeAdapterClient so the
routing, parsing, and budget code above it runs unmodified; provides scripted
answers, injected faults, and a dev-only fault-injection switch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from orchestrator.typesafe_routing.client import SdkSurface
from orchestrator.typesafe_routing.questions import (
    NONE_FIT,
    NO_TOOL_NEEDED,
    QUESTION_HARM_SCORE,
    QUESTION_IS_JAILBREAK,
    QUESTION_PRESENTATION_STYLE,
    QUESTION_TARGET_AGENT,
    QUESTION_THREAT_CATEGORY,
    TOOL_QUESTION_PREFIX,
)


class FakeTypeSafeError(Exception):
    def __init__(self, message: str = "", *, status_code: Optional[int] = None) -> None:
        super().__init__(message or type(self).__name__)
        self.status_code = status_code


class TypeSafeAPITimeoutError(FakeTypeSafeError):
    pass


class TypeSafeAPIConnectionError(FakeTypeSafeError):
    pass


class TypeSafeRateLimitError(FakeTypeSafeError):
    def __init__(self, message: str = "", *, retry_after: Optional[float] = None) -> None:
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class TypeSafeInternalServerError(FakeTypeSafeError):
    def __init__(self, message: str = "") -> None:
        super().__init__(message, status_code=500)


class TypeSafeAuthenticationError(FakeTypeSafeError):
    def __init__(self, message: str = "") -> None:
        super().__init__(message, status_code=401)


class TypeSafePermissionDeniedError(FakeTypeSafeError):
    def __init__(self, message: str = "") -> None:
        super().__init__(message, status_code=403)


class TypeSafeUnprocessableEntityError(FakeTypeSafeError):
    def __init__(self, message: str = "") -> None:
        super().__init__(message, status_code=422)


class TypeSafeAPIResponseValidationError(FakeTypeSafeError):
    pass


class TypeSafeAPIError(FakeTypeSafeError):
    pass


ERROR_CLASSES: Mapping[str, type[FakeTypeSafeError]] = {
    "timeout": TypeSafeAPITimeoutError,
    "connection": TypeSafeAPIConnectionError,
    "ratelimit": TypeSafeRateLimitError,
    "5xx": TypeSafeInternalServerError,
    "auth": TypeSafeAuthenticationError,
    "permission": TypeSafePermissionDeniedError,
    "unprocessable": TypeSafeUnprocessableEntityError,
    "malformed": TypeSafeAPIResponseValidationError,
}


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    noul: float = 0.0


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str = ""
    confidence: float = 0.0
    probabilities: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FakeResponse:
    nouls: Mapping[str, NoulAnswer] = field(default_factory=dict)
    scores: Mapping[str, ScoreAnswer] = field(default_factory=dict)
    choices: Mapping[str, ChoiceAnswer] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnswerSet:
    agent_id: Optional[str] = None
    agent_confidence: float = 0.0
    agent_probabilities: Mapping[str, float] = field(default_factory=dict)
    tool_name: Optional[str] = None
    tool_confidence: float = 0.0
    tool_probabilities: Mapping[str, float] = field(default_factory=dict)
    style: str = "as_delivered"
    jailbreak_probability: float = 0.0
    harm_score: float = 0.0
    threat_category: str = "none"

    def with_security(
        self,
        *,
        jailbreak_probability: float = 0.0,
        harm_score: float = 0.0,
        threat_category: str = "none",
    ) -> "AnswerSet":
        return replace(
            self,
            jailbreak_probability=jailbreak_probability,
            harm_score=harm_score,
            threat_category=threat_category,
        )


def high_confidence(agent_id: str, tool_name: str, *, style: str = "dashboard") -> AnswerSet:
    return AnswerSet(
        agent_id=agent_id,
        agent_confidence=0.94,
        agent_probabilities={agent_id: 0.94},
        tool_name=tool_name,
        tool_confidence=0.91,
        tool_probabilities={tool_name: 0.91, NONE_FIT: 0.05},
        style=style,
    )


def medium_confidence(
    agent_id: str, tools: Sequence[str], *, style: str = "detailed_table"
) -> AnswerSet:
    if not tools:
        raise ValueError("a medium answer needs at least one tool")
    spread = {name: 0.85 / len(tools) for name in tools}
    return AnswerSet(
        agent_id=agent_id,
        agent_confidence=0.72,
        agent_probabilities={agent_id: 0.72},
        tool_name=tools[0],
        tool_confidence=spread[tools[0]],
        tool_probabilities=spread,
        style=style,
    )


def low_confidence(*, style: str = "conversational") -> AnswerSet:
    return AnswerSet(
        agent_id=NO_TOOL_NEEDED,
        agent_confidence=0.41,
        agent_probabilities={NO_TOOL_NEEDED: 0.41},
        style=style,
    )


def none_fit(agent_id: str, *, style: str = "as_delivered") -> AnswerSet:
    return AnswerSet(
        agent_id=agent_id,
        agent_confidence=0.81,
        agent_probabilities={agent_id: 0.81},
        tool_name=NONE_FIT,
        tool_confidence=0.77,
        tool_probabilities={NONE_FIT: 0.77},
        style=style,
    )


def _response_for(answers: AnswerSet, questions: Mapping[str, Any]) -> FakeResponse:
    choices: dict[str, ChoiceAnswer] = {
        QUESTION_THREAT_CATEGORY: ChoiceAnswer(
            choice=answers.threat_category,
            confidence=1.0,
            probabilities={answers.threat_category: 1.0},
        )
    }
    if QUESTION_TARGET_AGENT in questions and answers.agent_id:
        choices[QUESTION_TARGET_AGENT] = ChoiceAnswer(
            choice=answers.agent_id,
            confidence=answers.agent_confidence,
            probabilities=dict(answers.agent_probabilities),
        )
    if QUESTION_PRESENTATION_STYLE in questions:
        choices[QUESTION_PRESENTATION_STYLE] = ChoiceAnswer(
            choice=answers.style, confidence=1.0, probabilities={answers.style: 1.0}
        )
    if answers.tool_name:
        for question_id, question in questions.items():
            if not question_id.startswith(TOOL_QUESTION_PREFIX):
                continue
            options = getattr(question, "criteria", {}) or {}
            if answers.tool_name in options:
                choices[question_id] = ChoiceAnswer(
                    choice=answers.tool_name,
                    confidence=answers.tool_confidence,
                    probabilities=dict(answers.tool_probabilities),
                )
                break

    return FakeResponse(
        nouls=(
            {QUESTION_IS_JAILBREAK: NoulAnswer(answers.jailbreak_probability)}
            if QUESTION_IS_JAILBREAK in questions
            else {}
        ),
        scores=(
            {QUESTION_HARM_SCORE: ScoreAnswer(answers.harm_score)}
            if QUESTION_HARM_SCORE in questions
            else {}
        ),
        choices=choices,
    )


@dataclass
class _FakeQuestion:
    kind: str
    instructions: str = ""
    criteria: Any = None


def fake_sdk_surface() -> SdkSurface:
    return SdkSurface(
        client_class=None,
        retry_policy=lambda **kwargs: kwargs,
        noul=lambda **kwargs: _FakeQuestion("noul", **kwargs),
        score=lambda **kwargs: _FakeQuestion("score", **kwargs),
        choice=lambda **kwargs: _FakeQuestion("choice", **kwargs),
        errors=dict(ERROR_CLASSES),
    )


@dataclass
class Call:
    api_key: str = field(repr=False)
    state: Mapping[str, Any]
    questions: Mapping[str, Any]
    timeout: float


class FakeTypeSafeClient:
    def __init__(
        self,
        *,
        answers: Optional[Mapping[str, AnswerSet]] = None,
        default: Optional[AnswerSet] = None,
        faults: Optional[Iterable[Any]] = None,
        latency: float = 0.0,
        sdk: Optional[SdkSurface] = None,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self._answers = dict(answers or {})
        self._default = default
        self._faults = list(faults or [])
        self._latency = latency
        self._sdk = sdk or fake_sdk_surface()
        self._sleep = sleep or asyncio.sleep
        self.calls: list[Call] = []
        self.closed = False

    def _surface(self) -> SdkSurface:
        return self._sdk

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def answer_for(self, request_text: str) -> Optional[AnswerSet]:
        if request_text in self._answers:
            return self._answers[request_text]
        for prompt, answers in self._answers.items():
            if request_text.startswith(prompt) or prompt in request_text:
                return answers
        return self._default

    async def system_one(
        self,
        *,
        api_key: str,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        timeout: float,
    ) -> FakeResponse:
        self.calls.append(
            Call(api_key=api_key, state=dict(state), questions=dict(questions), timeout=timeout)
        )

        fault = self._faults.pop(0) if self._faults else None
        if isinstance(fault, (int, float)) and not isinstance(fault, bool):
            await self._elapse(float(fault), timeout)
        elif isinstance(fault, BaseException):
            raise fault
        elif isinstance(fault, type) and issubclass(fault, BaseException):
            raise fault()
        elif isinstance(fault, str):
            raise ERROR_CLASSES[fault]()

        if self._latency:
            await self._elapse(self._latency, timeout)

        answers = self.answer_for(str(state.get("current_request") or ""))
        if answers is None:
            raise TypeSafeAPIResponseValidationError("no recorded answer for this prompt")
        return _response_for(answers, questions)

    async def _elapse(self, seconds: float, timeout: float) -> None:
        if timeout is not None and seconds > timeout:
            await self._sleep(timeout)
            raise TypeSafeAPITimeoutError(
                "simulated attempt exceeded its per-attempt timeout"
            )
        await self._sleep(seconds)

    async def aclose(self) -> None:
        self.closed = True


FAULT_ENV_NAME = "ASTRAL_TEST_TYPESAFE_FAULT"

FAULT_ALIASES: Mapping[str, str] = {
    "timeout": "timeout",
    "auth": "auth",
    "ratelimit": "ratelimit",
    "5xx": "5xx",
}


# Ignored outside dev: honoring it in prod is a DoS vector
def configured_fault(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    import os

    environ = environ if environ is not None else os.environ
    raw = (environ.get(FAULT_ENV_NAME) or "").strip().lower()
    if not raw:
        return None
    try:
        from orchestrator.session_store import is_dev_mode
    except ImportError:  # pragma: no cover
        return None
    if not is_dev_mode():
        return None
    return FAULT_ALIASES.get(raw)


class FaultInjectingClient:
    def __init__(self, inner: Any, *, environ: Optional[Mapping[str, str]] = None) -> None:
        self._inner = inner
        self._environ = environ

    def _surface(self) -> SdkSurface:
        return self._inner._surface()  # noqa: SLF001

    async def system_one(self, **kwargs: Any) -> Any:
        fault = configured_fault(self._environ)
        if fault is not None:
            raise ERROR_CLASSES[fault]()
        return await self._inner.system_one(**kwargs)

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = (
    "ERROR_CLASSES",
    "FAULT_ALIASES",
    "FAULT_ENV_NAME",
    "AnswerSet",
    "Call",
    "ChoiceAnswer",
    "FakeResponse",
    "FakeTypeSafeClient",
    "FaultInjectingClient",
    "NoulAnswer",
    "ScoreAnswer",
    "TypeSafeAPIConnectionError",
    "TypeSafeAPIError",
    "TypeSafeAPIResponseValidationError",
    "TypeSafeAPITimeoutError",
    "TypeSafeAuthenticationError",
    "TypeSafeInternalServerError",
    "TypeSafePermissionDeniedError",
    "TypeSafeRateLimitError",
    "TypeSafeUnprocessableEntityError",
    "configured_fault",
    "fake_sdk_surface",
    "high_confidence",
    "low_confidence",
    "medium_confidence",
    "none_fit",
)
