"""Tests for orchestrator/ledger.py: TaskLedger construction and JSON-safe audit
serialization, and ProgressLedger's completion tracking, consecutive-stall counting,
and should_replan threshold logic.
"""

from __future__ import annotations
import sys
from pathlib import Path
import pytest
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
from orchestrator import ledger  # noqa: E402

import json  # noqa: E402


def test_ledger_enabled_default_off(monkeypatch):
    monkeypatch.delenv("FF_DUAL_LEDGER", raising=False)
    assert ledger.ledger_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " On ", "TrUe"])
def test_ledger_enabled_truthy_values(monkeypatch, value):
    monkeypatch.setenv("FF_DUAL_LEDGER", value)
    assert ledger.ledger_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe", "2"])
def test_ledger_enabled_falsy_values(monkeypatch, value):
    monkeypatch.setenv("FF_DUAL_LEDGER", value)
    assert ledger.ledger_enabled() is False


def test_from_request_defaults_empty_lists():
    tl = ledger.TaskLedger.from_request("do the thing")
    assert tl.request == "do the thing"
    assert tl.given_facts == []
    assert tl.recalled_facts == []
    assert tl.derived_facts == []
    assert tl.guesses == []
    assert tl.plan == []


def test_from_request_seeds_given_and_recalled():
    tl = ledger.TaskLedger.from_request(
        "summarize", given=["a", "b"], recalled=["c"]
    )
    assert tl.given_facts == ["a", "b"]
    assert tl.recalled_facts == ["c"]
    assert tl.derived_facts == []
    assert tl.guesses == []
    assert tl.plan == []


def test_from_request_copies_inputs():
    given = ["a"]
    recalled = ["c"]
    tl = ledger.TaskLedger.from_request("req", given=given, recalled=recalled)
    given.append("mutated")
    recalled.append("mutated")
    assert tl.given_facts == ["a"]
    assert tl.recalled_facts == ["c"]


def test_to_audit_dict_shape_and_json_safe():
    tl = ledger.TaskLedger.from_request(
        "build report", given=["fy2025"], recalled=["last run failed"]
    )
    tl.derived_facts.append("3 sources needed")
    tl.guesses.append("user wants PDF")
    tl.plan.extend(["fetch", "render"])
    d = tl.to_audit_dict()
    assert set(d.keys()) == {
        "request", "given_facts", "recalled_facts",
        "derived_facts", "guesses", "plan",
    }
    assert d["request"] == "build report"
    assert d["given_facts"] == ["fy2025"]
    assert d["recalled_facts"] == ["last run failed"]
    assert d["derived_facts"] == ["3 sources needed"]
    assert d["guesses"] == ["user wants PDF"]
    assert d["plan"] == ["fetch", "render"]
    assert json.loads(json.dumps(d)) == d


def test_to_audit_dict_lists_are_copies():
    tl = ledger.TaskLedger.from_request("req", given=["a"])
    d = tl.to_audit_dict()
    d["given_facts"].append("tampered")
    d["plan"].append("tampered")
    assert tl.given_facts == ["a"]
    assert tl.plan == []


def test_revise_plan_returns_copy_without_mutating_original():
    tl = ledger.TaskLedger.from_request("req")
    tl.plan.extend(["old1", "old2"])
    revised = tl.revise_plan(["new1", "new2", "new3"])
    assert tl.plan == ["old1", "old2"]
    assert revised.plan == ["new1", "new2", "new3"]
    assert revised is not tl
    assert revised.request == "req"


def test_revise_plan_copies_new_plan_argument():
    tl = ledger.TaskLedger.from_request("req")
    new_plan = ["x", "y"]
    revised = tl.revise_plan(new_plan)
    new_plan.append("z")
    assert revised.plan == ["x", "y"]


def test_progress_ledger_starts_empty():
    pl = ledger.ProgressLedger()
    assert pl.steps == []
    assert pl.completed_count() == 0
    assert pl.consecutive_stalls() == 0


def test_record_appends_step_records():
    pl = ledger.ProgressLedger()
    pl.record("fetch", complete=True)
    pl.record("render", complete=False, stalled=True, note="timed out")
    assert len(pl.steps) == 2
    assert pl.steps[0] == ledger.StepRecord(
        name="fetch", complete=True, stalled=False, note=""
    )
    assert pl.steps[1].name == "render"
    assert pl.steps[1].complete is False
    assert pl.steps[1].stalled is True
    assert pl.steps[1].note == "timed out"


def test_completed_count_and_is_complete():
    pl = ledger.ProgressLedger()
    pl.record("a", complete=True)
    pl.record("b", complete=False, stalled=True)
    pl.record("c", complete=True)
    assert pl.completed_count() == 2
    assert pl.is_complete(3) is False
    assert pl.is_complete(2) is True
    assert pl.is_complete(1) is True


def test_consecutive_stalls_counts_only_trailing_run():
    pl = ledger.ProgressLedger()
    pl.record("a", complete=False, stalled=True)
    pl.record("b", complete=False, stalled=True)
    pl.record("c", complete=True)
    pl.record("d", complete=False, stalled=True)
    pl.record("e", complete=False, stalled=True)
    assert pl.consecutive_stalls() == 2


def test_consecutive_stalls_resets_after_non_stalled_record():
    pl = ledger.ProgressLedger()
    pl.record("a", complete=False, stalled=True)
    pl.record("b", complete=False, stalled=True)
    assert pl.consecutive_stalls() == 2
    pl.record("c", complete=False, stalled=False)
    assert pl.consecutive_stalls() == 0


def test_next_incomplete_returns_first_uncompleted_step():
    pl = ledger.ProgressLedger()
    plan = ["fetch", "parse", "render"]
    pl.record("fetch", complete=True)
    assert pl.next_incomplete(plan) == "parse"
    pl.record("parse", complete=True)
    assert pl.next_incomplete(plan) == "render"


def test_next_incomplete_ignores_stalled_non_complete_steps():
    pl = ledger.ProgressLedger()
    plan = ["fetch", "parse", "render"]
    pl.record("fetch", complete=True)
    pl.record("parse", complete=False, stalled=True)
    assert pl.next_incomplete(plan) == "parse"


def test_next_incomplete_returns_none_when_all_done():
    pl = ledger.ProgressLedger()
    plan = ["fetch", "render"]
    pl.record("fetch", complete=True)
    pl.record("render", complete=True)
    assert pl.next_incomplete(plan) is None


def test_next_incomplete_empty_plan_is_none():
    pl = ledger.ProgressLedger()
    assert pl.next_incomplete([]) is None


def test_should_replan_false_below_threshold():
    pl = ledger.ProgressLedger()
    pl.record("a", complete=False, stalled=True)
    pl.record("b", complete=False, stalled=True)
    assert pl.consecutive_stalls() == 2
    assert ledger.should_replan(pl) is False


def test_should_replan_true_at_threshold():
    pl = ledger.ProgressLedger()
    for name in ("a", "b", "c"):
        pl.record(name, complete=False, stalled=True)
    assert pl.consecutive_stalls() == 3
    assert ledger.should_replan(pl) is True


def test_should_replan_respects_custom_threshold():
    pl = ledger.ProgressLedger()
    pl.record("a", complete=False, stalled=True)
    pl.record("b", complete=False, stalled=True)
    assert ledger.should_replan(pl, stall_threshold=2) is True
    assert ledger.should_replan(pl, stall_threshold=3) is False


def test_should_replan_false_when_no_stalls():
    pl = ledger.ProgressLedger()
    pl.record("a", complete=True)
    pl.record("b", complete=True)
    assert ledger.should_replan(pl) is False
