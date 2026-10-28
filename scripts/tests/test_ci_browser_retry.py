"""Verifies that ci_browser_retry preserves both browser observations and refuses
missing, broadened, skipped, or failed sign-out qualification records.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import ci_browser_retry as retry


def report(results=None):
    return {
        "errors": [],
        "suites": [
            {
                "specs": [
                    {
                        "title": "sign-out erases the selection immediately",
                        "file": "tests/selection-088.spec.js",
                        "tests": [
                            {
                                "projectName": "chromium",
                                "expectedStatus": "passed",
                                "results": results or [],
                            }
                        ],
                    }
                ]
            }
        ],
    }


def write_json(path: Path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_merge_preserves_every_record_and_both_originals(tmp_path):
    first = [{"source": "exact source", "functions": [{"count": 0}], "scriptId": "12"}]
    second = [{"source": "exact source", "functions": [{"count": 2}], "scriptId": "28"}]
    ordinary = write_json(tmp_path / "ordinary.json", first)
    targeted = write_json(tmp_path / "targeted.json", second)
    originals = ordinary.read_bytes(), targeted.read_bytes()
    output = tmp_path / "merged.json"
    assert retry.merge_observations(ordinary, targeted, output) == 2
    assert json.loads(output.read_text()) == first + second
    assert (ordinary.read_bytes(), targeted.read_bytes()) == originals


@pytest.mark.parametrize("bad", [[], {}, None, [None], [[]], ["source"]])
def test_merge_rejects_invalid_arrays_before_replacing_output(tmp_path, bad):
    ordinary = write_json(tmp_path / "ordinary.json", [{"source": "kept"}])
    targeted = write_json(tmp_path / "targeted.json", bad)
    output = tmp_path / "merged.json"
    output.write_text("prior output", encoding="utf-8")
    with pytest.raises(ValueError):
        retry.merge_observations(ordinary, targeted, output)
    assert output.read_text() == "prior output"


@pytest.mark.parametrize("destination", ["ordinary.json", "targeted.json"])
def test_merge_never_overwrites_a_raw_input(tmp_path, destination):
    ordinary = write_json(tmp_path / "ordinary.json", [{"source": "first"}])
    targeted = write_json(tmp_path / "targeted.json", [{"source": "second"}])
    with pytest.raises(ValueError):
        retry.merge_observations(ordinary, targeted, tmp_path / destination)


def test_merge_refuses_the_same_input_twice(tmp_path):
    raw = write_json(tmp_path / "raw.json", [{"source": "kept"}])
    with pytest.raises(ValueError):
        retry.merge_observations(raw, raw, tmp_path / "output.json")


def test_selection_accepts_exactly_one_expected_chromium_test():
    assert retry.selected_test(report())["results"] == []


@pytest.mark.parametrize(
    "damage",
    [
        lambda value: value.update(errors=[{"message": "collection failed"}]),
        lambda value: value.update(suites=[]),
        lambda value: value["suites"].append(deepcopy(value["suites"][0])),
        lambda value: value["suites"][0]["specs"][0].update(title="another test"),
        lambda value: value["suites"][0]["specs"][0].update(file="tests/other.spec.js"),
        lambda value: value["suites"][0]["specs"][0]["tests"].append({}),
        lambda value: value["suites"][0]["specs"][0]["tests"][0].update(
            projectName="firefox"
        ),
        lambda value: value["suites"][0]["specs"][0]["tests"][0].update(
            expectedStatus="skipped"
        ),
    ],
)
def test_selection_rejects_broadened_or_missing_target(damage):
    value = report()
    damage(value)
    with pytest.raises(ValueError):
        retry.selected_test(value)


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([{"retry": 0, "status": "passed"}], "first attempt passed"),
        (
            [{"retry": 0, "status": "failed"}, {"retry": 1, "status": "passed"}],
            "passed after retry",
        ),
        (
            [{"retry": 0, "status": "timedOut"}, {"retry": 1, "status": "passed"}],
            "passed after retry",
        ),
        (
            [{"retry": 0, "status": "failed"}, {"retry": 1, "status": "failed"}],
            "failed",
        ),
    ],
)
def test_disposition_retains_all_attempt_outcomes(results, expected):
    observed = retry.disposition(report(results))
    assert observed == {
        "file": "tests/selection-088.spec.js",
        "title": retry.TITLE,
        "disposition": expected,
        "attempts": results,
    }


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"retry": 1, "status": "passed"}],
        [{"retry": 0, "status": "skipped"}],
        [{"retry": 0, "status": "passed"}, {"retry": 1, "status": "passed"}],
        [{"retry": 0, "status": "failed"}, {"retry": 2, "status": "passed"}],
        [{"retry": 0, "status": "failed"}] * 3,
    ],
)
def test_disposition_rejects_skips_and_unbounded_attempts(results):
    with pytest.raises(ValueError):
        retry.disposition(report(results))


def test_nested_suites_preserve_unique_selection():
    value = {"errors": [], "suites": [{"suites": report()["suites"]}]}
    assert retry.selected_test(value)["projectName"] == "chromium"


def test_cli_retains_failed_retry_diagnostic_without_merging(tmp_path, capsys):
    path = write_json(
        tmp_path / "report.json",
        report(
            [
                {"retry": 0, "status": "failed"},
                {"retry": 1, "status": "failed"},
            ]
        ),
    )
    diagnostics = tmp_path / "diagnostics.json"
    output = tmp_path / "merged.json"
    code = retry.main(
        [
            "merge",
            "--report",
            str(path),
            "--diagnostics",
            str(diagnostics),
            "--ordinary",
            "missing-first",
            "--retried",
            "missing-second",
            "--output",
            str(output),
        ]
    )
    assert code == 2
    assert json.loads(diagnostics.read_text())["disposition"] == "failed"
    assert not output.exists()
    assert "failed" in capsys.readouterr().err


def test_cli_select_and_merge_success(tmp_path, capsys):
    path = write_json(tmp_path / "report.json", report())
    assert retry.main(["select", "--report", str(path)]) == 0
    write_json(path, report([{"retry": 0, "status": "passed"}]))
    ordinary = write_json(tmp_path / "ordinary.json", [{"source": "first"}])
    targeted = write_json(tmp_path / "targeted.json", [{"source": "second"}])
    diagnostics = tmp_path / "diagnostics.json"
    output = tmp_path / "merged.json"
    assert (
        retry.main(
            [
                "merge",
                "--report",
                str(path),
                "--diagnostics",
                str(diagnostics),
                "--ordinary",
                str(ordinary),
                "--retried",
                str(targeted),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "first attempt passed" in capsys.readouterr().out
    assert json.loads(output.read_text()) == [{"source": "first"}, {"source": "second"}]


@pytest.mark.parametrize("contents", ["not json", "[NaN]", "[Infinity]"])
def test_cli_refuses_invalid_json(tmp_path, contents):
    path = tmp_path / "report.json"
    path.write_text(contents)
    assert retry.main(["select", "--report", str(path)]) == 2


def test_cli_refuses_missing_report(tmp_path):
    assert retry.main(["select", "--report", str(tmp_path / "missing.json")]) == 2
