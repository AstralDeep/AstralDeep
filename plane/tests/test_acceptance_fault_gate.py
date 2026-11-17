"""Checks that the bounded acceptance fault gate kills each declared guard defect
and refuses survivors, skipped cases, and collection/import errors. The real
PostgreSQL run clones this session's current template in every child run and emits evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import check_acceptance_faults as gate
from tests.fixtures.migrated_template import SHARED_CURRENT_TEMPLATE_ENV

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_acceptance_faults.py"


def test_fault_cli_missing_database_records_failure(tmp_path):
    report = tmp_path / "faults.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--report", str(report)],
        cwd=ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "ASTRALPLANE_TEST_POSTGRES_DSN"
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert report.is_file()
    assert json.loads(report.read_text())["status"] == "failed"


@pytest.mark.parametrize(
    ("returncode", "body", "case", "marker", "expected"),
    (
        (
            1,
            '<testcase name="target"><failure type="AssertionError" message="assert 2 == 1">'
            "assert 2 == 1</failure></testcase>",
            "target",
            "assert 2 == 1",
            "killed",
        ),
        (0, '<testcase name="target"/>', "target", "wrong", "survived"),
        (
            2,
            '<testcase name="target"><error>ImportError</error></testcase>',
            "target",
            "ImportError",
            "invalid",
        ),
        (
            1,
            '<testcase name="target"><failure>ImportError</failure></testcase>',
            "target",
            "assert 2 == 1",
            "invalid",
        ),
        (1, '<testcase name="target"><skipped/></testcase>', "target", "", "invalid"),
        (
            1,
            '<testcase name="other"><failure>assert 2 == 1</failure></testcase>',
            "target",
            "assert 2 == 1",
            "invalid",
        ),
        (
            3,
            '<testcase name="target"><failure>assert 2 == 1</failure></testcase>',
            "target",
            "assert 2 == 1",
            "invalid",
        ),
        (1, "", "target", "", "invalid"),
        (0, '<testcase name="target"/>', None, "", "invalid"),
        (0, '<testcase name="target"/>' * 6, "target", "", "invalid"),
    ),
)
def test_only_intended_assertion_failures_count_as_kills(
    tmp_path,
    returncode,
    body,
    case,
    marker,
    expected,
):
    report = tmp_path / "pytest.xml"
    report.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    assert gate.classify(returncode, report, case=case, marker=marker) == expected


def test_mutation_refuses_changed_guard_bytes(tmp_path):
    fault = replace(gate.FAULTS[0], before="guard that no longer exists")
    with pytest.raises(ValueError, match="exactly one guard"):
        gate.apply_fault(ROOT, tmp_path, fault)


def test_traceback_source_marker_cannot_turn_an_unrelated_error_into_a_kill(tmp_path):
    report = tmp_path / "result.xml"
    marker = "valid proposal rejected by a NUL advisory lock key"
    report.write_text(
        '<testsuites><testsuite><testcase name="target">'
        '<failure type="ValueError" message="ValueError: unrelated">'
        f"Traceback source contains pytest.fail('{marker}')\nValueError: unrelated"
        "</failure></testcase></testsuite></testsuites>",
    )
    assert gate.classify(1, report, case="target", marker=marker) == "invalid"


@pytest.mark.parametrize("control", (False, True), ids=("stale-kill", "stale-control"))
def test_launch_without_pytest_cannot_reuse_a_previous_receipt(tmp_path, monkeypatch, control):
    receipt = tmp_path / "result.xml"
    body = (
        '<testcase name="target"/>' * 6
        if control
        else '<testcase name="target"><failure type="AssertionError" message="assert 2 == 1">'
        "assert 2 == 1</failure></testcase>"
    )
    receipt.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    launcher = tmp_path / "python-before-pytest"
    launcher.write_text(f"#!/bin/sh\nexit {0 if control else 1}\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(gate.sys, "executable", str(launcher))
    result = gate.run_case(
        ROOT,
        ROOT,
        receipt,
        case=None if control else "target",
        marker="assert 2 == 1",
    )
    assert result == "invalid"


def test_actual_pytest_replaces_old_failure_with_current_survivor_receipt(tmp_path):
    receipt = tmp_path / "result.xml"
    receipt.write_text(
        '<testsuites><testsuite><testcase name="test_target">'
        '<failure type="AssertionError" message="assert 2 == 1">'
        "assert 2 == 1</failure></testcase></testsuite></testsuites>",
    )
    (tmp_path / "test_current.py").write_text(
        '"""Synthetic child test for current-launch receipt binding."""\n'
        "def test_target():\n    assert True\n",
    )
    result = gate.run_case(
        tmp_path,
        tmp_path,
        receipt,
        case="test_target",
        marker="assert 2 == 1",
        test_path="test_current.py",
    )
    assert result == "survived"
    assert not gate.ElementTree.parse(receipt).findall(".//failure")


@pytest.mark.parametrize(
    "error",
    (
        subprocess.TimeoutExpired("pytest", 120),
        FileNotFoundError("missing result"),
        gate.ElementTree.ParseError("malformed result"),
    ),
)
def test_watchdog_or_unreadable_execution_is_invalid(tmp_path, monkeypatch, error):
    def fail(*_, **__):
        raise error

    monkeypatch.setattr(gate.subprocess, "run", fail)
    assert gate.run_case(ROOT, ROOT, tmp_path / "result.xml", case="target") == "invalid"


def test_surviving_fault_fails_gate_and_control_error_is_not_a_kill(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALPLANE_TEST_POSTGRES_DSN", "synthetic-unopened-dsn")
    monkeypatch.setattr(gate, "apply_fault", lambda *_: "a" * 64)
    monkeypatch.setattr(
        gate, "run_case", lambda *_, **args: "passed" if args["case"] is None else "survived"
    )
    report = gate.check(root=ROOT)
    assert report["status"] == "failed"
    assert {fault["status"] for fault in report["faults"]} == {"survived"}
    output = tmp_path / "report.json"
    assert gate.main(["--report", str(output)]) == 2
    monkeypatch.setattr(gate, "run_case", lambda *_, **__: "invalid")
    with pytest.raises(ValueError, match="control"):
        gate.check(root=ROOT)
    assert gate.main(["--report", str(output)]) == 2
    assert json.loads(output.read_text())["status"] == "failed"


def test_real_postgresql_control_and_all_declared_faults(migrated_template, tmp_path, monkeypatch):
    monkeypatch.setenv(SHARED_CURRENT_TEMPLATE_ENV, migrated_template.name)
    output = Path(os.environ.get("ASTRALPLANE_FAULT_REPORT", str(tmp_path / "faults.json")))
    assert gate.main(["--report", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["control"] == "passed"
    assert len(report["faults"]) == 6
    assert {fault["status"] for fault in report["faults"]} == {"killed"}
