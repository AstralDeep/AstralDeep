#!/usr/bin/env python3
"""Merges the raw coverage data of the backend-web whole-suite groups into the
backend-python.xml and tooling-python.xml reports one complete run_backend_web_tests.py pass
writes, for check_changed_coverage.py. It admits only passing evidence from every group, each
holding exactly its assigned suites of the checkout's complete plan, remaps the
container-recorded paths onto the checkout, and writes the reports with the runner's writer.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from coverage import CoverageData, CoverageException


RECORDED_ROOT = "/workspace"
PLAN_KEYS = {"schema_version", "group", "source_commit", "runner_sha256", "reporter_sha256", "suites"}
SUITE_KEYS = {"suite", "cwd", "path"}


def _load_runner() -> Any:
    path = Path(__file__).resolve().with_name("run_backend_web_tests.py")
    spec = importlib.util.spec_from_file_location("backend_web_group_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()
GROUPS = tuple(sorted(group for group in runner.GROUPS if group != "all"))


def _document(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not readable JSON evidence") from exc


def _group_plan(directory: Path) -> dict[str, Any]:
    plan = _document(directory / "suite-plan.json")
    results = _document(directory / "test-results.json")
    if (type(plan) is not dict or set(plan) != PLAN_KEYS or type(plan["schema_version"]) is not int
            or plan["schema_version"] != 1
            or any(type(plan[key]) is not str for key in PLAN_KEYS - {"schema_version", "suites"})
            or plan["group"] not in GROUPS or type(plan["suites"]) is not list
            or not all(type(item) is dict and set(item) == SUITE_KEYS
                       and all(type(value) is str for value in item.values())
                       for item in plan["suites"])):
        raise ValueError(f"{directory} does not hold a backend-web suite group plan")
    if (type(results) is not dict or results.get("group") != plan["group"]
            or results.get("source_commit") != plan["source_commit"] or results.get("status") != "pass"
            or type(results.get("suites")) is not list
            or [item.get("suite") if type(item) is dict else None for item in results["suites"]]
            != [item["suite"] for item in plan["suites"]]):
        raise ValueError(f"{directory} does not hold passing results for its whole suite group")
    return plan


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _suites(root: Path, commands: Sequence[tuple[Path, str, str]]) -> list[tuple[str, str, str]]:
    return sorted((name, cwd.relative_to(root).as_posix(), path) for cwd, path, name in commands)


def _planned(plan: dict[str, Any]) -> list[tuple[str, str, str]]:
    return sorted((item["suite"], item["cwd"], item["path"]) for item in plan["suites"])


def validate_groups(root: Path, directories: Sequence[Path]) -> None:
    plans = [_group_plan(directory) for directory in directories]
    if sorted(plan["group"] for plan in plans) != list(GROUPS):
        raise ValueError("exactly one evidence directory per suite group is required: " + ", ".join(GROUPS))
    identities = {(plan["source_commit"], plan["runner_sha256"], plan["reporter_sha256"]) for plan in plans}
    if len(identities) != 1:
        raise ValueError("suite groups come from different sources, suite runners or reporters")
    runner_path = Path(runner.__file__)
    if (plans[0]["runner_sha256"], plans[0]["reporter_sha256"]) != (
            _digest(runner_path), _digest(runner_path.with_name("backend_web_test_reporter.py"))):
        raise ValueError("suite groups were not produced by this suite runner and reporter")
    if plans[0]["source_commit"] != runner.source_identity(root):
        raise ValueError("suite groups were not produced from this checkout")
    commands = runner.suite_commands(root)
    if sorted(suite for plan in plans for suite in _planned(plan)) != _suites(root, commands):
        raise ValueError("suite groups do not partition one complete suite plan")
    for plan in plans:
        if _planned(plan) != _suites(root, runner.group_commands(commands, plan["group"])):
            raise ValueError(f"the {plan['group']} group does not hold exactly its assigned suites")


def _group_data(directory: Path, prefix: str) -> CoverageData:
    path = directory / ".coverage"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path} is not a coverage data file")
    data = CoverageData(basename=str(path))
    try:
        data.read()
        files = data.measured_files()
        branches = data.has_arcs()
    except CoverageException as exc:
        raise ValueError(f"{path} is not readable coverage data") from exc
    if not files or not branches:
        raise ValueError(f"{path} holds no branch coverage data")
    outside = sorted(source for source in files
                     if not source.startswith(prefix) or os.path.normpath(source) != source)
    if outside:
        raise ValueError(f"{path} records {outside[0]} outside the recorded checkout")
    return data


def merge(root: Path, directories: Sequence[Path], output: Path, *,
          recorded_root: str = RECORDED_ROOT) -> None:
    validate_groups(root, directories)
    prefix = recorded_root.rstrip("/") + "/"
    groups = [_group_data(directory, prefix) for directory in directories]
    output.mkdir(parents=True, exist_ok=True)
    combined_path = output / ".coverage"
    combined_path.unlink(missing_ok=True)
    combined = CoverageData(basename=str(combined_path))
    target = f"{root}/"
    try:
        for data in groups:
            combined.update(data, map_path=lambda source: target + source[len(prefix):])
        combined.write()
    except CoverageException as exc:
        raise ValueError(f"group coverage data could not be combined: {exc}") from exc
    environ = dict(os.environ) | {"COVERAGE_FILE": str(combined_path), "PYTHONDONTWRITEBYTECODE": "1"}
    if not runner.write_coverage_reports(root, output, environ):
        raise ValueError("coverage reports could not be written from the merged group data")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, action="append", required=True,
                        help="one downloaded backend-web suite group evidence directory")
    parser.add_argument("--recorded-root", default=RECORDED_ROOT,
                        help="checkout root the suite groups recorded their coverage under")
    args = parser.parse_args(argv)
    try:
        merge(args.root.resolve(), [directory.resolve() for directory in args.evidence],
              args.output.resolve(), recorded_root=args.recorded_root)
    except (OSError, ValueError) as exc:
        print(f"backend-web coverage merge refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
