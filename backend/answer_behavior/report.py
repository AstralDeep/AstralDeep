"""Aggregates rubric results into per-dimension, per-identity, and per-class statistics
with the baseline-vs-candidate comparison, rendering the markdown report; unscorable
cases stay out of every denominator and are listed separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from answer_behavior.models import ACTIONS, DIMENSIONS, Verdict
from answer_behavior.run_record import CaseScore, RunRecord

CLASS_ORDER = (
    Verdict.WRONG_CLAIM,
    Verdict.OMISSION,
    Verdict.FABRICATED_COMPLETION,
    Verdict.UNTRUSTED_FOLLOWED,
    Verdict.WARRANTED_ABSTENTION,
    Verdict.TRUTHFUL_REPORT,
    Verdict.UNSCOREABLE,
)


@dataclass
class DimensionStats:
    actor: str
    dimension: str
    scorable: int
    successes: int
    class_counts: Dict[str, int]

    @property
    def success_rate(self) -> Optional[float]:
        return (self.successes / self.scorable) if self.scorable else None

    def to_dict(self) -> dict:
        return {
            "actor": self.actor,
            "dimension": self.dimension,
            "scorable": self.scorable,
            "successes": self.successes,
            "success_rate": None if self.success_rate is None else round(self.success_rate, 4),
            "class_counts": dict(self.class_counts),
        }


def scorable_success_rate(scores: List[CaseScore]) -> Optional[float]:
    selected = [score for score in scores if score.result.verdict is not Verdict.UNSCOREABLE]
    if not selected:
        return None
    successes = sum(1 for score in selected if score.result.verdict.is_success)
    return successes / len(selected)


def dimension_stats(scores: List[CaseScore], actor: str, dimension: str) -> DimensionStats:
    selected = [
        score for score in scores
        if score.actor == actor and score.dimension == dimension
        and score.result.verdict is not Verdict.UNSCOREABLE
    ]
    counts = {verdict.value: 0 for verdict in CLASS_ORDER}
    successes = 0
    for score in selected:
        counts[score.result.verdict.value] = counts.get(score.result.verdict.value, 0) + 1
        if score.result.verdict.is_success:
            successes += 1
    return DimensionStats(
        actor=actor,
        dimension=dimension,
        scorable=len(selected),
        successes=successes,
        class_counts=counts,
    )


def class_totals(scores: List[CaseScore], actor: str) -> Dict[str, int]:
    totals = {verdict.value: 0 for verdict in CLASS_ORDER}
    for score in scores:
        if score.actor != actor:
            continue
        for fired in score.result.fired_classes:
            totals[fired.value] = totals.get(fired.value, 0) + 1
        totals[score.result.verdict.value] = totals.get(score.result.verdict.value, 0) + 1
    return totals


def unscorable_scores(scores: List[CaseScore]) -> List[CaseScore]:
    return [score for score in scores if score.result.verdict is Verdict.UNSCOREABLE]


def baseline_comparison(scores: List[CaseScore], dimension: str) -> Optional[Dict[str, float]]:
    baseline = dimension_stats(scores, "baseline", dimension)
    candidate = dimension_stats(scores, "candidate", dimension)
    if baseline.success_rate is None or candidate.success_rate is None:
        return None
    return {
        "baseline": round(baseline.success_rate, 4),
        "candidate": round(candidate.success_rate, 4),
        "delta": round(candidate.success_rate - baseline.success_rate, 4),
    }


def render_markdown(record: RunRecord, cases_count: int) -> str:
    scores = record.scores
    lines: List[str] = []
    lines.append("# Answer-Behavior Diagnostic Report")
    lines.append("")
    lines.append(
        f"Run `{record.run_id}` · mode `{record.mode}` · model `{record.key.model}` · "
        f"corpus `{record.key.corpus_version}` · suite `{record.key.suite_version}` · "
        f"seed `{record.key.seed}` · {cases_count} frozen cases"
    )
    lines.append("")
    lines.append(
        "> Verdicts come from the deterministic Astral-owned rubric over visible "
        "receipts and evidence only. Wrong claims, omissions, fabricated completions, "
        "untrusted-instruction compliance, warranted abstentions, and truthful reports "
        "are counted separately; unscorable outcomes are excluded from every denominator."
    )
    lines.append("")
    lines.append("## Per-dimension success rates (primary verdict)")
    lines.append("")
    lines.append("| Dimension | " + " | ".join(ACTIONS) + " | Δ candidate−baseline |")
    lines.append("|---|---:|---:|---:|")
    for dimension in DIMENSIONS:
        cells = []
        for actor in ACTIONS:
            rate = dimension_stats(scores, actor, dimension).success_rate
            cells.append("—" if rate is None else f"{rate:.3f}")
        comparison = baseline_comparison(scores, dimension)
        delta = "—" if comparison is None else f"{comparison['delta']:+.3f}"
        lines.append(f"| {dimension} | " + " | ".join(cells) + f" | {delta} |")
    lines.append("")
    lines.append("## Failure and success classes (primary verdict + every fired class)")
    lines.append("")
    lines.append("| Actor | " + " | ".join(verdict.value for verdict in CLASS_ORDER) + " |")
    lines.append("|---|" + "---:|" * len(CLASS_ORDER))
    for actor in ACTIONS:
        totals = class_totals(scores, actor)
        lines.append(
            f"| {actor} | "
            + " | ".join(str(totals[verdict.value]) for verdict in CLASS_ORDER) + " |"
        )
    lines.append("")
    lines.append("## Identity scopes")
    lines.append("")
    lines.append("| Scope | Actor | scorable | success rate |")
    lines.append("|---|---|---:|---:|")
    for scope in ("development", "held_out"):
        scope_scores = [score for score in scores if score.identity_scope == scope]
        for actor in ACTIONS:
            rate = scorable_success_rate(
                [score for score in scope_scores if score.actor == actor]
            )
            scorable = len([
                score for score in scope_scores
                if score.actor == actor and score.result.verdict is not Verdict.UNSCOREABLE
            ])
            lines.append(
                f"| {scope} | {actor} | {scorable} | "
                + ("—" if rate is None else f"{rate:.3f}") + " |"
            )
    lines.append("")
    unscorable = unscorable_scores(scores)
    if unscorable:
        lines.append("## Unscorable outcomes (excluded from all rates)")
        lines.append("")
        for score in unscorable:
            lines.append(f"- `{score.case_id}` ({score.actor}): {score.result.reason}")
        lines.append("")
    return "\n".join(lines)
