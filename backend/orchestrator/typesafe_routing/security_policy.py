"""The additive security verdict (feature 089, US4).

TypeSafe is a screen in front of the gate stack, never a replacement for it.
The one property this module must have is that it can only ever make a turn
*more* restrictive. A verdict can require human approval for a tool call, or
refuse the turn outright, and that is the whole range: it cannot allow a call
the gate stack denies, satisfy an approval the gate stack requires, or set,
clear or inspect any existing gate state.

The thresholds below were calibrated on 2026-09-17 against the security
benchmark's 23 cases and the 20-case benign corpus, using the real service.

**What the confirm tier used to do wrong.** The first version confirmed on the
threat category alone. Measured, that fired on three ordinary actions --
deleting your own chat, dropping a column from a table, exporting a CSV -- for
a false-positive rate of 15% against SC-006's 3% bound. Threat category on its
own is a topic label, not a judgment: "this request is about deletion" is true
of both an attack and a user tidying up. The rule now needs a harm score or a
jailbreak probability to go with it, and at those thresholds it fires on 0 of
20 benign prompts while still flagging 18 of 18 attack cases.

**Why the refuse tier is still disabled.** It produced zero false refusals on
the benign corpus and correctly refused two genuine attacks. That is the right
shape, but 20 benign prompts cannot demonstrate a false-positive rate of 0.5%:
zero events in 20 trials only bounds the rate at roughly 14% with any
confidence. Enabling it needs a benign corpus of a few hundred prompts drawn
from real traffic, which is a data-collection task, not a code change. Until
then a would-be refusal is served one step down, so the signal is not lost.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .decision import RoutingDecision, SecurityJudgment

#: Still off after the first calibration: the benign corpus is too small to
#: demonstrate SC-006's 0.5% refusal false-positive rate, not because the tier
#: misbehaved. See the module docstring. While it is off, a would-be refusal is
#: served as ``confirm_tools``: the turn still runs, but every tool call needs
#: a human.
REFUSE_TIER_ENABLED = False

# -- thresholds measured 2026-09-17 ----------------------------------------
#
# Benign corpus (20 prompts): 0 refusals, 0 confirmations.
# Benchmark corpus (18 attack cases): 18 flagged, of which 2 would refuse.
# Benchmark controls (5 benign cases): 0 flagged.

REFUSE_JAILBREAK = 0.70
REFUSE_HARM = 2.5
REFUSE_TARGETED_JAILBREAK = 0.50
REFUSE_THREATS = frozenset({"credential_access", "network_manipulation"})

#: A jailbreak probability this high is itself the finding. The highest any
#: benign prompt reached was 0.20.
CONFIRM_JAILBREAK = 0.40

#: Every attack case scored at least 1.90; the highest benign prompt scored
#: 1.60 (asking to drop a column from a table). 1.80 sits in that gap.
CONFIRM_HARM = 1.80

#: A threat category only counts alongside a jailbreak signal. Alone it is a
#: topic label: "about deletion" describes an attack and a user tidying up
#: equally well, and treating it as a verdict cost a 15% false-positive rate.
#: No benign prompt exceeded 0.20 here either.
CONFIRM_TARGETED_JAILBREAK = 0.25
CONFIRM_THREATS = frozenset(
    {"destructive", "data_egress", "code_execution", "privilege_escalation"}
)


class Verdict(str, Enum):
    """What the screen concluded. Ordered from least to most restrictive."""

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
    """Classify one security judgment.

    ``None`` is ``PASS``: no judgment means no additional restriction, which
    keeps an unkeyed user's turn identical to today.
    """
    if security is None:
        return Verdict.PASS

    jailbreak = security.jailbreak_probability
    harm = security.harm_score
    threat = security.threat_category

    would_refuse = (jailbreak >= REFUSE_JAILBREAK and harm >= REFUSE_HARM) or (
        threat in REFUSE_THREATS and jailbreak >= REFUSE_TARGETED_JAILBREAK
    )
    if would_refuse:
        # While the refuse tier is disabled a refusal is served one step down,
        # so the calibration work can happen without either shipping an
        # uncalibrated refusal or silently dropping the signal.
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
    """The verdict for a turn's decision. ``None`` decision means ``PASS``."""
    if decision is None:
        return Verdict.PASS
    return verdict_for(decision.security, refuse_enabled=refuse_enabled)


def tighten(existing: Verdict, additional: Verdict) -> Verdict:
    """Combine two verdicts by taking the stricter one.

    Exists so a caller cannot accidentally write ``verdict = additional`` and
    relax something. Combination is always monotone.
    """
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
