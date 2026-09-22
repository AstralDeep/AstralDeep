#!/usr/bin/env python3
"""Run every backend/module suite in isolated processes and retain honest results.

This is a CI test producer, not a production-readiness decision. Live-provider,
real-authentication, LETS-enforce and media acceptance remain staging gates.
The caller must provision a disposable PostgreSQL server; no default DSN exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


POLICY_ROOT = Path(__file__).resolve().parents[1]


def source_identity(root: Path) -> str:
    """Bind a clean checkout; a commit ID alone cannot identify local edits."""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True)
    import re

    value = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("complete suites require an exact source commit")
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"],
        capture_output=True, text=True, check=True,
    )
    if status.stdout.strip():
        raise ValueError("complete suites require a clean candidate checkout")
    return value


def suite_commands(root: Path) -> list[tuple[Path, str, str]]:
    commands = [(root / "backend", str(suite), f"backend-{suite.as_posix().replace('/', '-')}")
                for suite in suite_paths(root)]
    for filename in ("concurrent_surfaces.py", "voice_concurrent_turns.py"):
        commands.append((root / "backend", f"tests/perf/{filename}", f"perf-{filename}"))
    commands.append((root, "scripts/tests", "tooling"))
    return commands


def suite_paths(root: Path) -> list[Path]:
    """Discover nested suites that backend/pytest.ini does not collect."""
    suites = set()
    for test in (root / "backend").rglob("test_*.py"):
        relative = test.relative_to(root / "backend")
        if relative.parts[0] == "voice_agent":
            continue  # Dedicated locked worker image; not a backend dependency.
        for index, part in enumerate(relative.parts[:-1]):
            if part in {"tests", "suites"}:
                suites.add(Path(*relative.parts[: index + 1]))
                break
        else:
            raise ValueError(f"unclassified backend test: {relative.as_posix()}")
    if Path("tests") not in suites or Path("persistent_agents/tests") not in suites:
        raise ValueError("required backend suites are missing")
    return sorted(suites)


def require_isolated_postgres(environ: dict[str, str]) -> None:
    """Refuse absent, mismatched or non-disposable database targets."""
    import psycopg2

    url = environ.get("DATABASE_URL", "")
    if not url or any(
        environ.get(name) != url
        for name in ("ASTRALPLANE_TEST_DATABASE_URL", "ASTRALPLANE_TEST_POSTGRES_DSN")
    ):
        raise ValueError("all three test database URLs must name the isolated database")
    database = psycopg2.extensions.parse_dsn(url).get("dbname", "")
    if not database.startswith("ad_gate_") or environ.get("ASTRAL_TEST_ISOLATED") != "1":
        raise ValueError("requires an explicitly disposable ad_gate_ database")
    connection = psycopg2.connect(url, connect_timeout=10)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_setting('server_version_num')")
            actual, version = cursor.fetchone()
            if actual != database or int(version) < 170000:
                raise ValueError("isolated PostgreSQL 17+ preflight failed")
    finally:
        connection.close()


def junit_result(path: Path) -> dict:
    """Keep every skip visible; database-unavailable skips can never pass CI."""
    document = ET.parse(path).getroot()
    cases = document.findall(".//testcase")
    skipped = []
    for case in cases:
        skip = case.find("skipped")
        if skip is not None:
            skipped.append({
                "test": f"{case.get('classname', '')}.{case.get('name', '')}",
                "reason": skip.get("message", skip.text or ""),
            })
    blocked_skips = [
        item for item in skipped
        if any(fragment in item["reason"].lower() for fragment in (
            "postgres unavailable", "database unavailable", "orchestrator/database",
            "cannot create isolated postgresql", "postgres_dsn", "test_database_url",
            "bg_continuity", "astralprims <", "unimportable", "astral_skip_perf",
        ))
    ]
    return {
        "tests": len(cases),
        "failures": len(document.findall(".//failure")),
        "errors": len(document.findall(".//error")),
        "skipped": skipped,
        "blocking_skips": blocked_skips,
    }


def verify_inventory(path: Path, junit: Path, *, source_commit: str, suite: str) -> None:
    """Require the completed JUnit cases to equal the pre-execution collection."""
    inventory = json.loads(path.read_text(encoding="utf-8"))
    if (type(inventory) is not dict
            or set(inventory) != {"schema_version", "suite", "source_commit", "tests"}
            or type(inventory["schema_version"]) is not int or inventory["schema_version"] != 1
            or inventory["suite"] != suite or inventory["source_commit"] != source_commit
            or type(inventory["tests"]) is not list
            or not all(type(case) is str for case in inventory["tests"])):
        raise ValueError("suite collection identity is invalid")
    expected = inventory["tests"]
    actual = [f"{case.get('classname', '')}::{case.get('name', '')}"
              for case in ET.parse(junit).getroot().findall(".//testcase")]
    if not expected or len(expected) != len(set(expected)) or sorted(actual) != sorted(expected):
        raise ValueError("completed JUnit cases differ from the collected suite")


@contextmanager
def isolated_suite_database(environ: dict[str, str]):
    """Give each process a new database; never reuse another suite's fixtures."""
    import psycopg2
    from psycopg2 import sql

    parameters = psycopg2.extensions.parse_dsn(environ["DATABASE_URL"])
    database = f"ad_gate_{uuid4().hex}"
    connection = psycopg2.connect(**parameters)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        parameters["dbname"] = database
        target = psycopg2.extensions.make_dsn(**parameters)
        yield environ | dict.fromkeys(("DATABASE_URL", "ASTRALPLANE_TEST_DATABASE_URL",
                                       "ASTRALPLANE_TEST_POSTGRES_DSN"), target)
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database)))
        finally:
            connection.close()


def run(root: Path, output: Path, *, timeout: int = 10800) -> int:
    if sys.version_info[:2] != (3, 11):
        raise ValueError("backend release tests require Python 3.11")
    environ = dict(os.environ)
    require_isolated_postgres(environ)
    source_commit = source_identity(root)
    commands = suite_commands(root)
    output.mkdir(parents=True, exist_ok=True)
    environ.update({
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        # The producer and immediate-failure plugin come from the separately
        # mounted protected policy when this is a protected qualification run.
        "PYTHONPATH": os.pathsep.join((str(POLICY_ROOT / "scripts"),
                                      str(root / "backend"), str(root))),
        "COVERAGE_FILE": str(output / ".coverage"),
        "ASTRAL_ENV": "development",
    })
    # A fresh report is mandatory, even when reusing the output directory.
    subprocess.run([sys.executable, "-m", "coverage", "erase"], env=environ, check=True)
    results = []
    plan = {"schema_version": 1, "source_commit": source_commit,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "reporter_sha256": hashlib.sha256((POLICY_ROOT / "scripts/backend_web_test_reporter.py").read_bytes()).hexdigest(),
            "suites": [{"suite": name, "cwd": cwd.relative_to(root).as_posix(), "path": suite}
                       for cwd, suite, name in commands]}
    (output / "suite-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    for cwd, suite, name in commands:
        name = name.replace("\\", "-")
        junit = output / f"{name}.xml"
        # Prevent a failed producer from reusing the previous run's result.
        junit.unlink(missing_ok=True)
        inventory = output / f"{name}.inventory.json"
        inventory.unlink(missing_ok=True)
        command = [
            sys.executable, "-m", "coverage", "run", "--append", "--branch",
            f"--source={root / 'backend'},{root / 'scripts'}",
            "--omit=*/tests/*,*/test_*,*/voice_agent/*",
            "-m", "pytest", suite, "-q", "-ra", "--tb=short", "-p", "no:cacheprovider",
            "-p", "backend_web_test_reporter",
            "-o", "faulthandler_timeout=120",
            f"--junitxml={junit}",
        ]
        print(f"Running {name}", flush=True)
        with isolated_suite_database(environ) as suite_environment, (
            output / f"{name}.log"
        ).open("w", encoding="utf-8") as log:
            suite_environment.update({"BQ_SUITE_INVENTORY_PATH": str(inventory),
                                      "BQ_SUITE_NAME": name, "BQ_SOURCE_COMMIT": source_commit})
            try:
                process = subprocess.run(command, cwd=cwd, env=suite_environment, stdout=log,
                                         stderr=subprocess.STDOUT, timeout=timeout, check=False)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
        result = {"suite": name, "command": command, "exit_code": exit_code,
                  "inventory": inventory.name, "junit": junit.name}
        try:
            result.update(junit_result(junit))
            verify_inventory(inventory, junit, source_commit=source_commit, suite=name)
        except (OSError, ET.ParseError, ValueError) as exc:
            result["report_error"] = type(exc).__name__
        result["status"] = "fail" if (
            exit_code or result.get("report_error") or not result.get("tests")
            or result.get("failures") or result.get("errors") or result.get("blocking_skips")
        ) else "pass"
        results.append(result)
        print(f"{name}: {result['status']}; {result.get('tests', 0)} collected; "
              f"{len(result.get('skipped', []))} skipped", flush=True)
    coverage_failed = False
    for domain, source in (("backend", "backend"), ("tooling", "scripts")):
        coverage = subprocess.run(
            [sys.executable, "-m", "coverage", "xml", f"--include={root / source}/*",
             "-o", str(output / f"{domain}-python.xml")],
            cwd=root, env=environ, check=False,
        )
        coverage_failed |= coverage.returncode != 0
    failed = coverage_failed or any(item["status"] != "pass" for item in results)
    document = {
        "scope": "backend-web-ci", "status": "fail" if failed else "pass",
        "source_commit": source_commit,
        "production_qualified": False,
        "separate_required_gates": ["projection-python-web", "worker-image",
                                    "protected-real-auth-lets-staging"],
        "deferred_native_qualification": ["Windows", "Android", "iOS", "macOS", "watchOS"],
        "suites": results,
    }
    (output / "test-results.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return run(args.root.resolve(), args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
