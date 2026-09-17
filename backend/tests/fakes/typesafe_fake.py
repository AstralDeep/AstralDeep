"""A deterministic TypeSafe stand-in for every 089 test (T013).

Substituted at exactly one boundary -- :class:`TypeSafeAdapterClient` -- so
everything above it, the question builder, the parser, the budget, the circuit,
the seams, runs its real code. No test in this feature touches the network.

Determinism is the point. A recorded answer set per fixture prompt means a tier
assertion is a statement about the tiering rules, not about what a model
happened to say that afternoon. Failures are injected by script rather than by
probability, so "the second attempt times out and the third succeeds" is a test
you can write, not one you wait for.
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

# -- error classes ------------------------------------------------------
#
# Named to match the SDK's classes, because the budget classifies by class
# name. Using the real classes would make the fake depend on the package
# being installed, which is the one thing the fake must not require.


class FakeTypeSafeError(Exception):
    """Base for every injected failure."""

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


#: Every error class a fault name can produce, by the short name tests use.
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


# -- answers ------------------------------------------------------------


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
    """The three answer buckets the parser reads."""

    nouls: Mapping[str, NoulAnswer] = field(default_factory=dict)
    scores: Mapping[str, ScoreAnswer] = field(default_factory=dict)
    choices: Mapping[str, ChoiceAnswer] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnswerSet:
    """A recorded answer for one fixture prompt, independent of question ids.

    The agent and tool are named, not indexed, so a recording survives a change
    in catalog ordering -- the fake resolves the right ``tool_for_<n>`` id from
    the question set it is handed.
    """

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
    """A recorded answer that lands in the high tier."""
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
    """A recorded answer that lands in the medium tier (a shortlist)."""
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
    """A recorded answer that lands in the low tier: nothing is narrowed."""
    return AnswerSet(
        agent_id=NO_TOOL_NEEDED,
        agent_confidence=0.41,
        agent_probabilities={NO_TOOL_NEEDED: 0.41},
        style=style,
    )


def none_fit(agent_id: str, *, style: str = "as_delivered") -> AnswerSet:
    """The model picked an agent but said none of its tools apply."""
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
    """Render one answer set against the question ids actually asked."""
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
        # Find the tool question whose options contain the recorded tool.
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


# -- the fake SDK surface and client -------------------------------------


@dataclass
class _FakeQuestion:
    """Stands in for ``Noul`` / ``Score`` / ``Choice``.

    It keeps ``criteria`` so the fake can resolve a recorded tool name back to
    the question that offered it.
    """

    kind: str
    instructions: str = ""
    criteria: Any = None


def fake_sdk_surface() -> SdkSurface:
    """An :class:`SdkSurface` that needs no installed package."""
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
    """One recorded request. Kept so tests can assert on what was sent."""

    api_key: str = field(repr=False)
    state: Mapping[str, Any]
    questions: Mapping[str, Any]
    timeout: float


class FakeTypeSafeClient:
    """Drop-in replacement for :class:`TypeSafeAdapterClient`.

    ``answers`` maps a fixture prompt (exactly, or by a prefix match) to the
    recorded :class:`AnswerSet`. ``faults`` is a script consumed one entry per
    attempt: an exception class, an instance, a float (seconds of latency), or
    ``None`` to answer normally.
    """

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

    # The adapter reads this to build questions.
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
        """Simulate latency, honoring the per-attempt timeout the SDK enforces.

        Without this the fake would answer a call the real client would have
        abandoned, and a test asserting "the budget stopped the attempt" would
        pass for the wrong reason.
        """
        if timeout is not None and seconds > timeout:
            await self._sleep(timeout)
            raise TypeSafeAPITimeoutError(
                "simulated attempt exceeded its per-attempt timeout"
            )
        await self._sleep(seconds)

    async def aclose(self) -> None:
        self.closed = True


# -- local fault injection for the quickstart ----------------------------
#
# A switch a developer can flip while walking through quickstart.md, without
# editing code. It deliberately does NOT use the TYPESAFE_ prefix: those three
# names are refused at boot (FR-005), and reusing the prefix for a test switch
# would undermine the rule.

FAULT_ENV_NAME = "ASTRAL_TEST_TYPESAFE_FAULT"

FAULT_ALIASES: Mapping[str, str] = {
    "timeout": "timeout",
    "auth": "auth",
    "ratelimit": "ratelimit",
    "5xx": "5xx",
}


def configured_fault(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the configured fault name, honoring it only outside production.

    Production posture ignores the switch entirely. A fault injector that a
    production process respects is a denial-of-service control with a friendly
    name.
    """
    import os

    environ = environ if environ is not None else os.environ
    raw = (environ.get(FAULT_ENV_NAME) or "").strip().lower()
    if not raw:
        return None
    try:
        from orchestrator.session_store import is_dev_mode
    except ImportError:  # pragma: no cover - the module is always present
        return None
    if not is_dev_mode():
        return None
    return FAULT_ALIASES.get(raw)


class FaultInjectingClient:
    """Wraps a real adapter client and raises the configured fault instead.

    Used only by the quickstart walkthrough. With no fault configured, or in
    production posture, every call passes straight through.
    """

    def __init__(self, inner: Any, *, environ: Optional[Mapping[str, str]] = None) -> None:
        self._inner = inner
        self._environ = environ

    def _surface(self) -> SdkSurface:
        return self._inner._surface()  # noqa: SLF001 - delegation to the wrapped client

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
