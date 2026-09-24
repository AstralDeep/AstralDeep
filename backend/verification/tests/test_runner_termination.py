"""Tests for runner termination and uncertainty handling
(backend/verification/runner.py): bounded retries on observation failure, and
reconcile() rules for counter refutation and judge disagreement.
"""

from __future__ import annotations

from verification.checks.base import Check, ok
from verification.config import RunConfig
from verification.evidence import CapturedEvidence
from verification.runner import Runner
from verification.scenarios import build_scenarios
from verification.tests.conftest import run_async
from verification.verdict import Outcome, reconcile


class _FakeDriver:
    mode = "in_process"
    auth_mode = "mock_inprocess"

    def __init__(self, behavior: str):
        self.behavior = behavior
        self.calls = 0

    async def run_scenario(self, scenario):
        self.calls += 1
        if self.behavior == "raise":
            raise RuntimeError("boom")
        return CapturedEvidence(
            evidence_id="e", scenario_id=scenario.scenario_id, run_mode="mock_inprocess",
            components=[{"type": "table"}],
            workspace_state=[{"component_id": "wc_x", "_source_agent": "a", "_source_tool": "t"}],
            audit_rows=[{"event_class": "agent_tool_call", "outcome": "success",
                         "action_type": "tool.read_x.end"}],
        )


def _cfg():
    return RunConfig(mode="in_process", run_id="__verif__term", max_retries=2, timeout_s=5.0)


def _scenario():
    return build_scenarios("__verif__term", "mock_inprocess", ["everyday"])[0]


def test_errored_observation_is_uncertain_and_bounded():
    cfg = _cfg()
    driver = _FakeDriver("raise")
    runner = Runner(driver, cfg)
    run_async(runner.run([_scenario()], [Check("c", "tangible_ui",
                                              lambda e, i: ok("c"))]))
    assert len(runner.verdicts) == 1
    assert runner.verdicts[0].outcome == Outcome.UNCERTAIN
    assert runner.verdicts[0].adversarial.get("errored_observation") is True
    assert driver.calls == cfg.max_retries + 1


def test_happy_path_produces_pass_verdict():
    cfg = _cfg()
    runner = Runner(_FakeDriver("ok"), cfg)
    check = Check("c", "tangible_ui", lambda e, i: ok("c", "fine"))
    run_async(runner.run([_scenario()], [check]))
    assert len(runner.verdicts) == 1
    assert runner.verdicts[0].outcome == Outcome.PASS


def test_reconcile_rules():
    assert reconcile(Outcome.PASS, False, None)[0] == Outcome.PASS
    assert reconcile(Outcome.PASS, True, None)[0] == Outcome.UNCERTAIN
    assert reconcile(Outcome.PASS, False, Outcome.FAIL)[0] == Outcome.UNCERTAIN
    assert reconcile(Outcome.PASS, False, Outcome.PASS)[0] == Outcome.PASS
    assert reconcile(Outcome.FAIL, False, None)[0] == Outcome.FAIL
    assert reconcile(Outcome.UNCERTAIN, False, None)[0] == Outcome.UNCERTAIN


def test_uncertain_ratio():
    cfg = _cfg()
    runner = Runner(_FakeDriver("ok"), cfg)
    run_async(runner.run([_scenario()], [Check("c", "tangible_ui", lambda e, i: ok("c"))]))
    assert 0.0 <= runner.uncertain_ratio() <= 1.0
