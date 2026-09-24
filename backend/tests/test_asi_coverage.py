"""Tests for orchestrator/asi_coverage.py: plan-to-audit record shape and JSON
round-tripping, intended-vs-actual deviation detection, and the OWASP ASI01–ASI10
coverage matrix.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import asi_coverage as asi  # noqa: E402

_CAP_RE = re.compile(r"^C-[SMN]\d+$")


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("FF_ASI_COVERAGE", raising=False)
    assert asi.asi_coverage_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on", "  on  "])
def test_flag_on_truthy_spellings(monkeypatch, val):
    monkeypatch.setenv("FF_ASI_COVERAGE", val)
    assert asi.asi_coverage_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "maybe"])
def test_flag_off_falsy_spellings(monkeypatch, val):
    monkeypatch.setenv("FF_ASI_COVERAGE", val)
    assert asi.asi_coverage_enabled() is False


def test_plan_record_shape():
    rec = asi.plan_record(
        ["search", "summarize"], request="find me grants", correlation_id="abc"
    )
    assert rec == {
        "kind": "intended_plan",
        "request": "find me grants",
        "planned_tools": ["search", "summarize"],
        "correlation_id": "abc",
        "step_count": 2,
    }


def test_plan_record_is_json_serializable():
    rec = asi.plan_record([1, "two", 3.0], request="r", correlation_id="cid")
    blob = json.dumps(rec)
    back = json.loads(blob)
    assert back["planned_tools"] == ["1", "two", "3.0"]
    assert back["step_count"] == 3


def test_plan_record_truncates_request():
    long_req = "x" * 1000
    rec = asi.plan_record([], request=long_req)
    assert len(rec["request"]) == 500
    assert rec["request"] == "x" * 500


def test_plan_record_step_count_matches_planned_tools():
    assert asi.plan_record([]).step_count if False else True
    assert asi.plan_record([])["step_count"] == 0
    assert asi.plan_record(["a", "b", "c", "d"])["step_count"] == 4


def test_plan_record_defaults():
    rec = asi.plan_record(["only"])
    assert rec["request"] == ""
    assert rec["correlation_id"] == ""


def test_detect_deviation_clean():
    plan = ["a", "b", "c"]
    dev = asi.detect_deviation(plan, ["a", "b", "c"])
    assert dev.extra_calls == ()
    assert dev.skipped_steps == ()
    assert dev.out_of_order is False
    assert asi.has_deviation(dev) is False


def test_detect_deviation_extra_calls_deduped():
    dev = asi.detect_deviation(["a", "b"], ["a", "x", "b", "x", "y"])
    assert dev.extra_calls == ("x", "y")
    assert dev.skipped_steps == ()
    assert asi.has_deviation(dev) is True


def test_detect_deviation_skipped_steps():
    dev = asi.detect_deviation(["a", "b", "c"], ["a", "c"])
    assert dev.skipped_steps == ("b",)
    assert dev.extra_calls == ()
    assert dev.out_of_order is False
    assert asi.has_deviation(dev) is True


def test_detect_deviation_skipped_steps_deduped():
    dev = asi.detect_deviation(["a", "a", "b"], ["nothing"])
    assert dev.skipped_steps == ("a", "b")
    assert dev.extra_calls == ("nothing",)


def test_detect_deviation_out_of_order():
    plan = ["a", "b", "c"]
    dev = asi.detect_deviation(plan, ["c", "b", "a"])
    assert dev.out_of_order is True
    assert dev.extra_calls == ()
    assert dev.skipped_steps == ()
    assert asi.has_deviation(dev) is True


def test_detect_deviation_out_of_order_partial_swap():
    dev = asi.detect_deviation(["a", "b", "c"], ["a", "c", "b"])
    assert dev.out_of_order is True


def test_detect_deviation_extra_calls_do_not_force_out_of_order():
    dev = asi.detect_deviation(["a", "b"], ["x", "a", "y", "b", "z"])
    assert dev.out_of_order is False
    assert dev.extra_calls == ("x", "y", "z")
    assert dev.skipped_steps == ()
    assert asi.has_deviation(dev) is True


def test_detect_deviation_stringifies_ids():
    dev = asi.detect_deviation([1, 2], ["1", "2"])
    assert asi.has_deviation(dev) is False


def test_has_deviation_false_only_when_fully_clean():
    clean = asi.Deviation(extra_calls=(), skipped_steps=(), out_of_order=False)
    assert asi.has_deviation(clean) is False
    assert asi.has_deviation(asi.Deviation(("x",), (), False)) is True
    assert asi.has_deviation(asi.Deviation((), ("y",), False)) is True
    assert asi.has_deviation(asi.Deviation((), (), True)) is True


def test_deviation_is_frozen():
    dev = asi.Deviation(extra_calls=(), skipped_steps=(), out_of_order=False)
    with pytest.raises(Exception):
        dev.out_of_order = True  # type: ignore[misc]


def test_asi_risks_has_all_ten_codes_in_order():
    codes = [code for code, _title in asi.ASI_RISKS]
    assert codes == [f"ASI{n:02d}" for n in range(1, 11)]
    assert all(title for _code, title in asi.ASI_RISKS)


def test_coverage_has_entry_for_every_risk():
    assert set(asi.COVERAGE) == {code for code, _ in asi.ASI_RISKS}


def test_coverage_report_ten_entries_all_covered():
    report = asi.coverage_report()
    assert len(report) == 10
    assert [r["code"] for r in report] == [f"ASI{n:02d}" for n in range(1, 11)]
    for r in report:
        assert r["covered"] is True
        assert r["capabilities"]
        assert isinstance(r["title"], str) and r["title"]


def test_coverage_report_capabilities_are_copies():
    report = asi.coverage_report()
    report[0]["capabilities"].append("C-HACK")
    assert "C-HACK" not in asi.COVERAGE[report[0]["code"]]


def test_uncovered_risks_is_empty():
    assert asi.uncovered_risks() == []


def test_coverage_ratio_is_one():
    assert asi.coverage_ratio() == 1.0
    assert 0.0 <= asi.coverage_ratio() <= 1.0


def test_every_capability_looks_like_a_valid_id():
    for code, caps in asi.COVERAGE.items():
        assert caps, f"{code} has no capabilities"
        for cap in caps:
            assert _CAP_RE.match(cap), f"{code} → bad capability id {cap!r}"


def test_c_s12_self_references_auditability():
    assert "C-S12" in asi.COVERAGE["ASI10"]
