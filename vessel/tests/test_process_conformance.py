# Tests that every container probe has a process-mode home, and that process-mode check prints one line per case.

from __future__ import annotations

import re
from pathlib import Path

import pytest

from astralvessel import conformance
from astralvessel.probes import Report

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def container_probe_names() -> set[str]:
    return set(re.findall(r'\bout\(f?"([^"]+)"', (ROOT / "astralvessel" / "probes.py").read_text()))


def case_names() -> set[str]:
    pytest.importorskip("astral_exec")
    from astral_exec.conformance import cases
    return {case.name for case in cases.CASES}


def test_every_container_probe_maps_to_a_case_a_test_or_a_documented_weaker_line():
    names = container_probe_names()
    assert names and names <= set(conformance.PROBE_MAP), sorted(names - set(conformance.PROBE_MAP))
    assert set(conformance.PROBE_MAP) <= names


def test_mapped_cases_exist_and_mapped_tests_are_real():
    cases = case_names()
    for name, (kind, detail) in conformance.PROBE_MAP.items():
        assert detail.strip(), name
        if kind == conformance.CASE:
            assert detail in cases, (name, detail)
        elif kind == conformance.TESTED and detail.endswith(".py"):
            assert (TESTS / detail).is_file(), (name, detail)
        else:
            assert kind in (conformance.TESTED, conformance.WEAKER)


def test_the_executor_checks_named_in_the_map_are_run_over_loopback():
    named = {detail.removeprefix("executor: ") for kind, detail in conformance.PROBE_MAP.values()
             if detail.startswith("executor: ")}
    assert named == {"refuses a caller without its token", "lists what its agents may run", "keeps its keys private",
                     "refuses a forged ticket", "refuses an unknown agent", "operator token cannot run a call"}


def test_the_weaker_lines_print_once_each_and_failures_are_reported(monkeypatch, capsys):
    class Bundle:
        python = Path("/nonexistent/python")

    class Layout:
        code = Path("/nonexistent/code")
        generated = Path("/nonexistent/gen")

    class Deployment:
        agents = {}

    class Runtime:
        bundle, layout, deployment = Bundle(), Layout(), Deployment()

    report = conformance.run_process(Runtime())
    out = capsys.readouterr().out
    assert report.failed and "FAIL  isolation: the check ran" in out
    weak = [line for line in out.splitlines() if line.startswith("WEAK  ")]
    wanted = {n for n, (k, _) in conformance.PROBE_MAP.items() if k == conformance.WEAKER and "{" not in n}
    assert len(weak) == len(wanted)
    assert isinstance(report, Report)
