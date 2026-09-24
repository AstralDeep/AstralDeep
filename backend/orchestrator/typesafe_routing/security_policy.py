"""The additive-only security verdict layered in front of the gate stack: a TypeSafe
judgment can require approval or refuse a turn but can never grant, satisfy, or
inspect any existing permission gate.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .decision import RoutingDecision, SecurityJudgment

REFUSE_TIER_ENABLED = False

REFUSE_JAILBREAK = 0.70
REFUSE_HARM = 2.5
REFUSE_TARGETED_JAILBREAK = 0.50
REFUSE_THREATS = frozenset({"credential_access", "network_manipulation"})

CONFIRM_JAILBREAK = 0.40

CONFIRM_HARM = 1.80

CONFIRM_TARGETED_JAILBREAK = 0.25
CONFIRM_THREATS = frozenset(
    {"destructive", "data_egress", "code_execution", "privilege_escalation"}
)


class Verdict(str, Enum):
    PASS = "pass"
    CONFIRM_TOOLS = "confirm_tools"
    REFUSE = "refuse"

    @property
    def requires_confirmation(self) -> bool:
        return self is Verdict.CONFIRM_TOOLS

    @property
    def refuses(self) -> bool:
        return self is Verdict.REFUSE


def verdict_for(
    security: Optional[SecurityJudgment],
    *,
    refuse_enabled: bool = REFUSE_TIER_ENABLED,
) -> Verdict:
    if security is None:
        return Verdict.PASS

    jailbreak = security.jailbreak_probability
    harm = security.harm_score
    threat = security.threat_category

    would_refuse = (jailbreak >= REFUSE_JAILBREAK and harm >= REFUSE_HARM) or (
        threat in REFUSE_THREATS and jailbreak >= REFUSE_TARGETED_JAILBREAK
    )
    if would_refuse:
        # When disabled, downgrade to confirm_tools — never a silent pass
        return Verdict.REFUSE if refuse_enabled else Verdict.CONFIRM_TOOLS

    if (
        jailbreak >= CONFIRM_JAILBREAK
        or harm >= CONFIRM_HARM
        or (threat in CONFIRM_THREATS and jailbreak >= CONFIRM_TARGETED_JAILBREAK)
    ):
        return Verdict.CONFIRM_TOOLS

    return Verdict.PASS


def security_verdict(
    decision: Optional[RoutingDecision],
    *,
    refuse_enabled: bool = REFUSE_TIER_ENABLED,
) -> Verdict:
    if decision is None:
        return Verdict.PASS
    return verdict_for(decision.security, refuse_enabled=refuse_enabled)


def tighten(existing: Verdict, additional: Verdict) -> Verdict:
    order = (Verdict.PASS, Verdict.CONFIRM_TOOLS, Verdict.REFUSE)
    return max(existing, additional, key=order.index)


__all__ = (
    "CONFIRM_HARM",
    "CONFIRM_JAILBREAK",
    "CONFIRM_TARGETED_JAILBREAK",
    "CONFIRM_THREATS",
    "REFUSE_HARM",
    "REFUSE_JAILBREAK",
    "REFUSE_TARGETED_JAILBREAK",
    "REFUSE_THREATS",
    "REFUSE_TIER_ENABLED",
    "Verdict",
    "security_verdict",
    "tighten",
    "verdict_for",
)
