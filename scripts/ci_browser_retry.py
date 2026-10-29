"""Validates the single pinned sign-out retry and preserves its ordinary and passing
target coverage arrays for Projection's authoritative browser converter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


TITLE = "sign-out erases the selection immediately"
FILE = "tests/selection-088.spec.js"


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _read(path: Path) -> Any:
    def refuse(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=refuse)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )


def merge_observations(ordinary: Path, retried: Path, output: Path) -> int:
    paths = [ordinary.resolve(), retried.resolve(), output.resolve()]
    _require(len(set(paths)) == 3, "coverage inputs and output must be distinct")
    records = []
    for path in (ordinary, retried):
        value = _read(path)
        _require(
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value),
            "browser observations must be non-empty arrays of records",
        )
        records.extend(value)
    _write(output, records)
    return len(records)


def selected_test(report: Any) -> dict[str, Any]:
    _require(
        isinstance(report, dict) and report.get("errors", []) == [],
        "browser report contains errors",
    )
    suites = report.get("suites")
    _require(isinstance(suites, list), "browser report has no suites")
    pending, specs = list(suites), []
    while pending:
        suite = pending.pop()
        _require(isinstance(suite, dict), "invalid browser suite")
        children, cases = suite.get("suites", []), suite.get("specs", [])
        _require(
            isinstance(children, list) and isinstance(cases, list),
            "invalid browser suite children",
        )
        pending.extend(children)
        specs.extend(cases)
    _require(
        len(specs) == 1 and isinstance(specs[0], dict),
        "retry must select exactly one test",
    )
    spec = specs[0]
    _require(
        spec.get("title") == TITLE and spec.get("file") == FILE,
        "retry selected another test",
    )
    tests = spec.get("tests")
    _require(
        isinstance(tests, list) and len(tests) == 1 and isinstance(tests[0], dict),
        "retry must select exactly one browser project",
    )
    test = tests[0]
    _require(
        test.get("projectName") == "chromium"
        and test.get("expectedStatus") == "passed",
        "retry must require a passing Chromium test",
    )
    return test


def disposition(report: Any) -> dict[str, Any]:
    results = selected_test(report).get("results")
    _require(
        isinstance(results, list) and 1 <= len(results) <= 2,
        "retry has invalid attempt count",
    )
    attempts = []
    for index, result in enumerate(results):
        _require(
            isinstance(result, dict)
            and type(result.get("retry")) is int
            and result["retry"] == index,
            "retry attempt identity is invalid",
        )
        _require(
            result.get("status") in {"passed", "failed", "timedOut", "interrupted"},
            "retry attempt did not execute",
        )
        _require(
            index == 0 or attempts[-1]["status"] != "passed",
            "a passing test was repeated",
        )
        attempts.append({"retry": index, "status": result["status"]})
    status = "failed"
    if attempts[-1]["status"] == "passed":
        status = "first attempt passed" if len(attempts) == 1 else "passed after retry"
    return {"file": FILE, "title": TITLE, "disposition": status, "attempts": attempts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    selection = commands.add_parser("select")
    selection.add_argument("--report", required=True, type=Path)
    merge = commands.add_parser("merge")
    for name in ("report", "ordinary", "retried", "output", "diagnostics"):
        merge.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = _read(args.report)
        if args.command == "select":
            selected_test(report)
            print("exactly one pinned sign-out test selected")
            return 0
        observed = disposition(report)
        _require(
            args.diagnostics.resolve()
            not in {
                args.report.resolve(),
                args.ordinary.resolve(),
                args.retried.resolve(),
                args.output.resolve(),
            },
            "retry diagnostics must not overwrite evidence",
        )
        _write(args.diagnostics, observed)
        print(observed["disposition"])
        _require(
            observed["disposition"] != "failed", "sign-out failed after bounded retry"
        )
        merge_observations(args.ordinary, args.retried, args.output)
        return 0
    except (OSError, ValueError) as exc:
        print(f"browser retry evidence invalid: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
