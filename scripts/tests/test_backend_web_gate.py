"""Tests for scripts/run_backend_web_tests.py and backend_web_test_reporter.py: suite
inventory, database preflight, JUnit reporting, and source-identity requirements.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from scripts import run_backend_web_tests as gate
from scripts import backend_web_test_reporter


def _tree(tmp_path):
    for name in ("tests", "persistent_agents/tests", "audit/tests", "qual_audit/suites",
                 "voice_agent/tests", "agents/journal_review/tests"):
        path = tmp_path / "backend" / name / "test_example.py"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


def test_inventory_includes_module_and_academic_suites_and_separates_worker(tmp_path):
    paths = {path.as_posix() for path in gate.suite_paths(_tree(tmp_path))}
    assert paths == {"tests", "persistent_agents/tests", "audit/tests",
                     "qual_audit/suites", "agents/journal_review/tests"}


def test_inventory_rejects_missing_core_suite(tmp_path):
    with pytest.raises(ValueError, match="required backend"):
        gate.suite_paths(tmp_path)


def test_inventory_rejects_orphan_test(tmp_path):
    root = _tree(tmp_path)
    (root / "backend/test_orphan.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="unclassified"):
        gate.suite_paths(root)


def _environment():
    return dict.fromkeys(("DATABASE_URL", "ASTRALPLANE_TEST_DATABASE_URL",
                          "ASTRALPLANE_TEST_POSTGRES_DSN"),
                         "postgresql://test:synthetic@isolated/ad_gate_unit") | {
        "ASTRAL_TEST_ISOLATED": "1",
    }


@pytest.mark.parametrize("change", [
    {"DATABASE_URL": ""}, {"ASTRALPLANE_TEST_DATABASE_URL": "different"},
    {"ASTRAL_TEST_ISOLATED": "0"},
    dict.fromkeys(("DATABASE_URL", "ASTRALPLANE_TEST_DATABASE_URL",
                   "ASTRALPLANE_TEST_POSTGRES_DSN"), "postgresql://host/production"),
])
def test_database_preflight_refuses_unsafe_or_ambiguous_targets(change):
    with pytest.raises(ValueError):
        gate.require_isolated_postgres(_environment() | change)


@pytest.mark.parametrize("actual,version,accepted", [
    ("ad_gate_unit", "170000", True), ("production", "170000", False),
    ("ad_gate_unit", "160009", False),
])
def test_database_preflight_checks_real_identity_and_closes(monkeypatch, actual, version, accepted):
    import psycopg2

    events = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            events.append(sql)

        def fetchone(self):
            return actual, version

    connection = SimpleNamespace(cursor=Cursor, close=lambda: events.append("closed"))
    monkeypatch.setattr(psycopg2, "connect", lambda *args, **kwargs: connection)
    if accepted:
        gate.require_isolated_postgres(_environment())
    else:
        with pytest.raises(ValueError, match="preflight failed"):
            gate.require_isolated_postgres(_environment())
    assert events[-1] == "closed"
    assert "current_database" in events[0]


def test_junit_keeps_skips_separate_and_blocks_missing_database(tmp_path):
    report = tmp_path / "result.xml"
    report.write_text('''<testsuites><testsuite>
      <testcase name="ok"/><testcase name="failure"><failure/></testcase>
      <testcase name="error"><error/></testcase>
      <testcase name="db"><skipped message="Postgres unavailable"/></testcase>
      <testcase name="external"><skipped>requires real authentication</skipped></testcase>
    </testsuite></testsuites>''', encoding="utf-8")
    result = gate.junit_result(report)
    assert result["tests"] == 5
    assert result["failures"] == result["errors"] == 1
    assert len(result["skipped"]) == 2
    assert len(result["blocking_skips"]) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_live_failure_reporter_preserves_node_and_reason(capsys, failed):
    backend_web_test_reporter.pytest_runtest_logreport(SimpleNamespace(
        failed=failed, nodeid="suite::critical_denial", when="setup", longrepr="authority missing"))
    output = capsys.readouterr().out
    assert bool(output) is failed
    if failed:
        assert "critical_denial (setup)" in output
        assert "authority missing" in output


def test_run_rejects_nonproduction_python(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.sys, "version_info", (3, 14))
    with pytest.raises(ValueError, match="Python 3.11"):
        gate.run(tmp_path, tmp_path / "evidence")


@pytest.mark.parametrize("scenario", ["pass", "test_failure", "empty", "missing", "malformed",
                                      "timeout", "coverage_failure", "db_skip", "missing_inventory",
                                      "partial_inventory", "wrong_inventory_source", "bad_inventory"])
def test_run_never_masks_failure_and_preserves_evidence(tmp_path, monkeypatch, scenario):
    root = _tree(tmp_path)
    output = root / "evidence"
    monkeypatch.setattr(gate.sys, "version_info", (3, 11))
    monkeypatch.setattr(gate, "require_isolated_postgres", lambda env: None)
    monkeypatch.setattr(gate, "source_identity", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "isolated_suite_database", lambda env: nullcontext(env))
    seen = []

    def execute(command, **kwargs):
        seen.append(command)
        report = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        if report:
            assert kwargs["env"]["PYTHON_DOTENV_DISABLED"] == "1"
            assert kwargs["env"]["COVERAGE_FILE"] == str(output / ".coverage")
            assert kwargs["env"]["PYTHONPATH"].split(gate.os.pathsep) == [
                str(gate.POLICY_ROOT / "scripts"), str(root / "backend"), str(root),
            ]
            assert "backend_web_test_reporter" in command
            if scenario == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            if scenario != "missing":
                from pathlib import Path
                case = '<testcase name="one"/>'
                if scenario == "empty":
                    case = ""
                if scenario == "db_skip":
                    case = '<testcase name="db"><skipped message="database unavailable"/></testcase>'
                Path(report).write_text("bad xml" if scenario == "malformed" else
                                        f"<testsuites><testsuite>{case}</testsuite></testsuites>",
                                        encoding="utf-8")
                environment = kwargs["env"]
                inventory = {
                    "schema_version": 1, "source_commit": environment["BQ_SOURCE_COMMIT"],
                    "suite": environment["BQ_SUITE_NAME"], "tests": ["::one"],
                }
                if scenario == "partial_inventory":
                    inventory["tests"].append("::never-executed")
                if scenario == "wrong_inventory_source":
                    inventory["source_commit"] = "b" * 40
                if scenario != "missing_inventory":
                    Path(environment["BQ_SUITE_INVENTORY_PATH"]).write_text(
                        "{}" if scenario == "bad_inventory" else json.dumps(inventory),
                        encoding="utf-8",
                    )
        failed = (report and scenario == "test_failure") or (
            "xml" in command and scenario == "coverage_failure")
        return SimpleNamespace(returncode=int(bool(failed)))

    monkeypatch.setattr(gate.subprocess, "run", execute)
    result = gate.run(root, output, timeout=1)
    assert result == (0 if scenario == "pass" else 1)
    evidence = json.loads((output / "test-results.json").read_text(encoding="utf-8"))
    assert evidence["production_qualified"] is False
    assert len(evidence["suites"]) == 8
    assert seen[0][-1] == "erase"
    assert "xml" in seen[-1]
    assert evidence["source_commit"] == "a" * 40
    assert json.loads((output / "suite-plan.json").read_text())["source_commit"] == "a" * 40


def test_collection_inventory_precedes_execution_and_uses_junit_identity(tmp_path, monkeypatch):
    path = tmp_path / "inventory.json"
    monkeypatch.setenv("BQ_SUITE_INVENTORY_PATH", str(path))
    monkeypatch.setenv("BQ_SUITE_NAME", "backend-tests")
    monkeypatch.setenv("BQ_SOURCE_COMMIT", "a" * 40)
    session = SimpleNamespace(items=[SimpleNamespace(nodeid="tests/test_one.py::TestOne::test_deny[param/a]")])
    backend_web_test_reporter.pytest_collection_finish(session)
    assert json.loads(path.read_text())["tests"] == ["tests.test_one.TestOne::test_deny[param/a]"]
    with pytest.raises(FileExistsError):
        backend_web_test_reporter.pytest_collection_finish(session)
    monkeypatch.setenv("BQ_SOURCE_COMMIT", "bad")
    with pytest.raises(ValueError, match="exact source"):
        backend_web_test_reporter.pytest_collection_finish(session)
    monkeypatch.delenv("BQ_SUITE_INVENTORY_PATH")
    backend_web_test_reporter.pytest_collection_finish(session)


def test_collection_duplicate_identities_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("BQ_SUITE_INVENTORY_PATH", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("BQ_SUITE_NAME", "backend-tests")
    monkeypatch.setenv("BQ_SOURCE_COMMIT", "a" * 40)
    item = SimpleNamespace(nodeid="test_one.py::test_one")
    with pytest.raises(ValueError, match="duplicate"):
        backend_web_test_reporter.pytest_collection_finish(SimpleNamespace(items=[item, item]))


@pytest.mark.parametrize("identity,dirty", [("a" * 40, ""), ("bad", ""),
                                            ("a" * 40, " M backend/runtime.py"),
                                            ("a" * 40, "?? temp.html")])
def test_source_identity_requires_exact_clean_git_commit(tmp_path, monkeypatch, identity, dirty):
    monkeypatch.setattr(gate.subprocess, "run", lambda command, **kw:
                        SimpleNamespace(stdout=dirty if "status" in command else identity))
    if identity == "bad":
        with pytest.raises(ValueError, match="exact source"):
            gate.source_identity(tmp_path)
    elif dirty:
        with pytest.raises(ValueError, match="clean candidate"):
            gate.source_identity(tmp_path)
    else:
        assert gate.source_identity(tmp_path) == identity


def test_main_resolves_paths_and_propagates_gate_result(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.sys, "argv", ["gate", "--root", str(tmp_path),
                                          "--output", str(tmp_path / "out")])
    monkeypatch.setattr(gate, "run", lambda root, output: 23)
    assert gate.main() == 23


@pytest.mark.parametrize("raise_inside", [False, True])
def test_suite_database_is_new_and_dropped_even_on_failure(monkeypatch, raise_inside):
    import psycopg2

    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query):
            statements.append(str(query))

    connection = SimpleNamespace(cursor=Cursor, close=lambda: statements.append("closed"))
    monkeypatch.setattr(psycopg2, "connect", lambda **kwargs: connection)
    original = _environment()
    try:
        with gate.isolated_suite_database(original) as environment:
            assert environment["DATABASE_URL"] != original["DATABASE_URL"]
            assert environment["DATABASE_URL"] == environment["ASTRALPLANE_TEST_POSTGRES_DSN"]
            assert "ad_gate_" in environment["DATABASE_URL"]
            if raise_inside:
                raise RuntimeError("test producer failed")
    except RuntimeError:
        assert raise_inside
    assert "CREATE DATABASE" in statements[0]
    assert "DROP DATABASE IF EXISTS" in statements[1]
    assert "WITH (FORCE)" in statements[1]
    assert statements[2] == "closed"
    assert original == _environment()
