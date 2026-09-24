"""Closed-loop runner: plan, act, observe, verify (backend/verification/checks/base.py,
scenarios.py, verdict.py): drives each scenario under a time budget with informed
retries, then reconciles checks and counters into a definite verdict.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from verification.checks.base import Check
from verification.config import RunConfig
from verification.evidence import CapturedEvidence
from verification.scenarios import Scenario
from verification.verdict import Outcome, Verdict, reconcile

logger = logging.getLogger("verification.runner")


class Runner:
    def __init__(
        self,
        driver: Any,
        config: RunConfig,
        llm_judge: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.driver = driver
        self.config = config
        self.llm_judge = llm_judge
        self.verdicts: List[Verdict] = []
        self.evidence: Dict[str, CapturedEvidence] = {}

    async def _observe(self, scenario: Scenario) -> Optional[CapturedEvidence]:
        last_err: Optional[str] = None
        for attempt in range(1, self.config.max_retries + 2):
            try:
                ev = await asyncio.wait_for(
                    self.driver.run_scenario(scenario), timeout=self.config.timeout_s
                )
                if last_err:
                    ev.extra["prior_attempt_error"] = last_err
                return ev
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "scenario %s attempt %d failed: %s",
                    scenario.scenario_id, attempt, last_err,
                )
        return None

    def _inputs_for(self, scenario: Scenario) -> Dict[str, Any]:
        return {
            "warrants_ui": scenario.warrants_ui,
            "known_markers": list(scenario.persona.fixture.known_markers),
            "query": scenario.query,
        }

    async def verify_scenario(self, scenario: Scenario, checks: List[Check]) -> None:
        ev = await self._observe(scenario)
        if ev is None:
            self.verdicts.append(
                Verdict(
                    verdict_id=f"{scenario.scenario_id}:observe",
                    scope="scenario",
                    outcome=Outcome.UNCERTAIN,
                    run_mode=scenario.auth_mode,
                    confidence="low",
                    refs={"persona": scenario.persona.key, "scenario": scenario.scenario_id},
                    reason="harness could not observe the system under test",
                    adversarial={"errored_observation": True},
                )
            )
            return

        ev = ev.redacted(self.config.secret_values())
        self.evidence[scenario.scenario_id] = ev
        inputs = self._inputs_for(scenario)

        for check in checks:
            result = check.run(ev, inputs)
            counter_refuted = (
                check.counter_refutes(ev, inputs)
                if result.outcome == Outcome.PASS else False
            )
            judge: Optional[Outcome] = None
            if self.llm_judge is not None and result.outcome == Outcome.PASS:
                try:
                    judge = await self.llm_judge(check, ev, inputs)
                except Exception:
                    logger.debug("llm_judge failed; treating as na", exc_info=True)
                    judge = None
            outcome, confidence, adversarial = reconcile(result.outcome, counter_refuted, judge)
            self.verdicts.append(
                Verdict(
                    verdict_id=f"{scenario.scenario_id}:{check.check_id}",
                    scope="check",
                    outcome=outcome,
                    run_mode=scenario.auth_mode,
                    confidence=confidence,
                    evidence_ref=ev.evidence_id,
                    refs={
                        "persona": scenario.persona.key,
                        "scenario": scenario.scenario_id,
                        "check": check.check_id,
                        "property": check.property,
                        "counter_check": f"{check.check_id}.counter",
                    },
                    adversarial=adversarial,
                    reason=result.reason,
                )
            )

    async def run(self, scenarios: List[Scenario], checks: List[Check]) -> List[Verdict]:
        for scenario in scenarios:
            await self.verify_scenario(scenario, checks)
        return self.verdicts

    def has_failures(self) -> bool:
        return any(v.outcome == Outcome.FAIL for v in self.verdicts)

    def uncertain_ratio(self) -> float:
        if not self.verdicts:
            return 0.0
        n = sum(1 for v in self.verdicts if v.outcome == Outcome.UNCERTAIN)
        return round(n / len(self.verdicts), 4)
