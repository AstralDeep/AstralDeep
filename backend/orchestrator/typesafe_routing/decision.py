"""Parsing a System One response into a routing decision (feature 089).

The model's answer is a set of typed judgments with confidences. This module
turns them into something the turn can act on, and its governing rule is that
uncertainty degrades to today's behavior rather than to a guess.

That is what the tiers encode. **High** means the model was confident about
both the agent and the tool, and confident by a margin over its second choice:
the first round gets one tool and, where the provider supports it, a forced
call. **Medium** means it was confident about the agent but spread across that
agent's tools: the first round gets a shortlist. **Low** -- and every
malformed, missing or unrecognized answer -- means the round is exactly what it
would have been with no TypeSafe key at all.

Nothing here raises. A decision that cannot be parsed is a ``None`` decision,
and a ``None`` decision is the unkeyed path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import Any, Mapping, Optional, Sequence

from .questions import (
    NONE_FIT,
    NO_TOOL_NEEDED,
    PRESENTATION_STYLES,
    QUESTION_HARM_SCORE,
    QUESTION_IS_JAILBREAK,
    QUESTION_PRESENTATION_STYLE,
    QUESTION_TARGET_AGENT,
    QUESTION_THREAT_CATEGORY,
    QuestionSet,
    THREAT_CATEGORIES,
)

logger = logging.getLogger("Orchestrator.TypeSafe.Decision")

# -- tier thresholds (provisional; T028 sets the final values) --------------

HIGH_AGENT_CONFIDENCE = 0.80
HIGH_TOOL_CONFIDENCE = 0.75
HIGH_MARGIN = 0.30

MEDIUM_AGENT_CONFIDENCE = 0.55
MEDIUM_MASS = 0.80
MEDIUM_MAX_TOOLS = 6

#: Provider presets that accept a forced ``tool_choice``. Everything else --
#: ``custom``, ``ollama``, ``lmstudio`` -- gets ``"auto"``, because a forced
#: choice an endpoint does not understand turns a narrowed round into a failed
#: one. Confirmed in T015.
FORCED_CHOICE_PROVIDERS = frozenset(
    {"openai", "anthropic", "openrouter", "groq", "together", "mistral", "xai"}
)


class Tier(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class SecurityJudgment:
    """The three security answers, with safe defaults."""

    jailbreak_probability: float = 0.0
    harm_score: float = 0.0
    threat_category: str = "none"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """One turn's parsed routing answer.

    Carries no request text: a decision is passed around a turn, logged in
    metric labels and audited, so it holds identifiers and numbers only.
    """

    tier: Tier = Tier.LOW
    agent_id: Optional[str] = None
    agent_confidence: float = 0.0
    tool_name: Optional[str] = None
    tool_confidence: float = 0.0
    tool_margin: float = 0.0
    shortlist: tuple[str, ...] = ()
    style: str = "as_delivered"
    security: SecurityJudgment = SecurityJudgment()
    no_tool_needed: bool = False

    @property
    def narrows_round_one(self) -> bool:
        return self.tier in (Tier.HIGH, Tier.MEDIUM) and bool(self.shortlist)


@dataclass(frozen=True, slots=True)
class RoundOnePlan:
    """What round one should actually send."""

    tools_desc: Any
    tool_choice: Any = "auto"
    tier: Tier = Tier.LOW
    narrowed: bool = False


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _answer(response: Any, bucket: str, question_id: str) -> Any:
    container = getattr(response, bucket, None)
    if container is None:
        return None
    try:
        return container.get(question_id) if hasattr(container, "get") else container[question_id]
    except (KeyError, TypeError, IndexError):
        return None


def _choice_and_confidence(answer: Any) -> tuple[Optional[str], float, float]:
    """Return ``(choice, confidence, margin)`` from a Choice answer.

    ``margin`` is the gap to the runner-up when the SDK exposes a distribution,
    and ``0.0`` when it does not. A missing margin only ever demotes a tier, so
    an SDK that stops reporting one degrades safely.
    """
    if answer is None:
        return None, 0.0, 0.0
    choice = getattr(answer, "choice", None)
    if not isinstance(choice, str) or not choice:
        return None, 0.0, 0.0
    confidence = _as_float(
        getattr(answer, "confidence", None)
        if getattr(answer, "confidence", None) is not None
        else getattr(answer, "probability", None)
    )
    distribution = _distribution(answer)
    if distribution:
        confidence = confidence or _as_float(distribution.get(choice))
        ranked = sorted(distribution.values(), reverse=True)
        margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
    else:
        margin = 0.0
    return choice, confidence, margin


def _distribution(answer: Any) -> dict[str, float]:
    for attribute in ("probabilities", "distribution", "scores", "choices"):
        raw = getattr(answer, attribute, None)
        if isinstance(raw, Mapping):
            parsed = {
                str(key): _as_float(value)
                for key, value in raw.items()
                if _as_float(value, -1.0) >= 0.0
            }
            if parsed:
                return parsed
    return {}


def _shortlist_from(distribution: Mapping[str, float], eligible: Sequence[str]) -> tuple[str, ...]:
    """Smallest top-k (k <= MEDIUM_MAX_TOOLS) whose mass reaches MEDIUM_MASS."""
    allowed = {name for name in eligible}
    ranked = sorted(
        ((name, value) for name, value in distribution.items() if name in allowed),
        key=lambda item: item[1],
        reverse=True,
    )
    mass = 0.0
    chosen: list[str] = []
    for name, value in ranked[:MEDIUM_MAX_TOOLS]:
        chosen.append(name)
        mass += value
        if mass >= MEDIUM_MASS:
            return tuple(chosen)
    return ()


def parse_security(response: Any) -> SecurityJudgment:
    """Read the three security answers, defaulting to benign on anything odd."""
    jailbreak = _answer(response, "nouls", QUESTION_IS_JAILBREAK)
    harm = _answer(response, "scores", QUESTION_HARM_SCORE)
    threat_answer = _answer(response, "choices", QUESTION_THREAT_CATEGORY)
    threat, _, _ = _choice_and_confidence(threat_answer)
    if threat not in THREAT_CATEGORIES:
        threat = "none"
    return SecurityJudgment(
        jailbreak_probability=_as_float(getattr(jailbreak, "noul", None)),
        harm_score=_as_float(getattr(harm, "score", None)),
        threat_category=threat,
    )


def parse_decision(response: Any, question_set: QuestionSet) -> RoutingDecision:
    """Turn a System One response into a :class:`RoutingDecision`.

    Never raises. Anything unexpected -- a missing answer, an option the model
    invented, a tool that is not in the eligible set -- lands in the low tier,
    which is the unkeyed behavior.
    """
    security = parse_security(response)

    agent_answer = _answer(response, "choices", QUESTION_TARGET_AGENT)
    agent_id, agent_confidence, _ = _choice_and_confidence(agent_answer)

    style_answer = _answer(response, "choices", QUESTION_PRESENTATION_STYLE)
    style, _, _ = _choice_and_confidence(style_answer)
    if style not in PRESENTATION_STYLES:
        style = "as_delivered"

    if agent_id == NO_TOOL_NEEDED:
        return RoutingDecision(
            tier=Tier.LOW,
            agent_confidence=agent_confidence,
            style=style,
            security=security,
            no_tool_needed=True,
        )

    if agent_id is None or agent_id not in question_set.tool_names:
        # An unknown or hallucinated agent id.
        return RoutingDecision(tier=Tier.LOW, style=style, security=security)

    question_id = question_set.tool_question_id(agent_id)
    tool_answer = _answer(response, "choices", question_id) if question_id else None
    tool_name, tool_confidence, tool_margin = _choice_and_confidence(tool_answer)
    eligible = question_set.tool_names.get(agent_id, ())

    if tool_name == NONE_FIT or tool_name is None or tool_name not in eligible:
        return RoutingDecision(
            tier=Tier.LOW,
            agent_id=agent_id,
            agent_confidence=agent_confidence,
            style=style,
            security=security,
        )

    if (
        agent_confidence >= HIGH_AGENT_CONFIDENCE
        and tool_confidence >= HIGH_TOOL_CONFIDENCE
        and tool_margin >= HIGH_MARGIN
    ):
        return RoutingDecision(
            tier=Tier.HIGH,
            agent_id=agent_id,
            agent_confidence=agent_confidence,
            tool_name=tool_name,
            tool_confidence=tool_confidence,
            tool_margin=tool_margin,
            shortlist=(tool_name,),
            style=style,
            security=security,
        )

    if agent_confidence >= MEDIUM_AGENT_CONFIDENCE:
        shortlist = _shortlist_from(_distribution(tool_answer), eligible)
        if shortlist:
            return RoutingDecision(
                tier=Tier.MEDIUM,
                agent_id=agent_id,
                agent_confidence=agent_confidence,
                tool_name=tool_name,
                tool_confidence=tool_confidence,
                tool_margin=tool_margin,
                shortlist=shortlist,
                style=style,
                security=security,
            )

    return RoutingDecision(
        tier=Tier.LOW,
        agent_id=agent_id,
        agent_confidence=agent_confidence,
        tool_name=tool_name,
        tool_confidence=tool_confidence,
        tool_margin=tool_margin,
        style=style,
        security=security,
    )


def _tool_name_of(entry: Any) -> Optional[str]:
    """Read a tool's name out of an OpenAI-shaped tool definition."""
    if isinstance(entry, Mapping):
        function = entry.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            if isinstance(name, str):
                return name
        name = entry.get("name")
        if isinstance(name, str):
            return name
    return None


def apply_round_one(
    decision: Optional[RoutingDecision],
    tools_desc: Any,
    provider_preset: Optional[str] = None,
) -> RoundOnePlan:
    """Return the round-one tool list and ``tool_choice`` for ``decision``.

    A pure function, so the orchestrator seam is one call and the tier rules
    are testable without a turn. Any decision that does not narrow returns the
    inputs unchanged, which is what makes invariant 1 -- byte-identical
    round-one arguments without a key -- hold by construction.
    """
    if decision is None or not decision.narrows_round_one or not tools_desc:
        return RoundOnePlan(tools_desc=tools_desc, tool_choice="auto")

    wanted = set(decision.shortlist)
    narrowed = [entry for entry in tools_desc if _tool_name_of(entry) in wanted]
    if not narrowed:
        # The shortlist did not survive contact with the real catalog.
        return RoundOnePlan(tools_desc=tools_desc, tool_choice="auto")

    if decision.tier is Tier.HIGH and decision.tool_name:
        preset = (provider_preset or "").strip().lower()
        if preset in FORCED_CHOICE_PROVIDERS:
            choice: Any = {
                "type": "function",
                "function": {"name": decision.tool_name},
            }
        else:
            choice = "auto"
        return RoundOnePlan(
            tools_desc=narrowed, tool_choice=choice, tier=Tier.HIGH, narrowed=True
        )

    return RoundOnePlan(
        tools_desc=narrowed, tool_choice="auto", tier=decision.tier, narrowed=True
    )


__all__ = (
    "FORCED_CHOICE_PROVIDERS",
    "HIGH_AGENT_CONFIDENCE",
    "HIGH_MARGIN",
    "HIGH_TOOL_CONFIDENCE",
    "MEDIUM_AGENT_CONFIDENCE",
    "MEDIUM_MASS",
    "MEDIUM_MAX_TOOLS",
    "RoundOnePlan",
    "RoutingDecision",
    "SecurityJudgment",
    "Tier",
    "apply_round_one",
    "parse_decision",
    "parse_security",
)
