"""Dual-ledger stall detection for multi-step turns: TaskLedger separates given,
recalled, and derived facts and guesses from the plan; ProgressLedger tracks step
outcomes and should_replan() signals when to abandon a stuck plan. Used by
turn_hooks.py.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def ledger_enabled() -> bool:
    return os.getenv("FF_DUAL_LEDGER", "false").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class TaskLedger:
    request: str
    given_facts: List[str] = field(default_factory=list)
    recalled_facts: List[str] = field(default_factory=list)
    derived_facts: List[str] = field(default_factory=list)
    guesses: List[str] = field(default_factory=list)
    plan: List[str] = field(default_factory=list)

    @classmethod
    def from_request(
        cls,
        request: str,
        *,
        given: Optional[List[str]] = None,
        recalled: Optional[List[str]] = None,
    ) -> "TaskLedger":
        return cls(
            request=request,
            given_facts=list(given) if given else [],
            recalled_facts=list(recalled) if recalled else [],
            derived_facts=[],
            guesses=[],
            plan=[],
        )

    def to_audit_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request,
            "given_facts": list(self.given_facts),
            "recalled_facts": list(self.recalled_facts),
            "derived_facts": list(self.derived_facts),
            "guesses": list(self.guesses),
            "plan": list(self.plan),
        }

    def revise_plan(self, new_plan: List[str]) -> "TaskLedger":
        return dataclasses.replace(self, plan=list(new_plan))


@dataclass
class StepRecord:
    name: str
    complete: bool
    stalled: bool
    note: str = ""


class ProgressLedger:
    def __init__(self) -> None:
        self.steps: List[StepRecord] = []

    def record(
        self,
        name: str,
        *,
        complete: bool,
        stalled: bool = False,
        note: str = "",
    ) -> None:
        self.steps.append(
            StepRecord(name=name, complete=complete, stalled=stalled, note=note)
        )

    def completed_count(self) -> int:
        return sum(1 for s in self.steps if s.complete)

    def is_complete(self, total_steps: int) -> bool:
        return self.completed_count() >= total_steps

    def consecutive_stalls(self) -> int:
        count = 0
        for s in reversed(self.steps):
            if s.stalled:
                count += 1
            else:
                break
        return count

    def next_incomplete(self, plan: List[str]) -> Optional[str]:
        done = {s.name for s in self.steps if s.complete}
        for step in plan:
            if step not in done:
                return step
        return None


def should_replan(progress: ProgressLedger, *, stall_threshold: int = 3) -> bool:
    return progress.consecutive_stalls() >= stall_threshold
