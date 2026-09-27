"""Tests for scripts/run_backend_web_tests.py, backend_web_test_reporter.py and
backend_web_image_gate.sh: suite inventory and groups, per-suite time bounds, database
preflight, JUnit reporting, and source-identity requirements.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_backend_web_tests as gate
from scripts import backend_web_test_reporter


def _tree(tmp_path):
    for name in ("tests", "persistent_agents/tests", "audit/tests", "evaluation/suites",
                 "voice_agent/tests", "agents/journal_review/tests"):
        path = tmp_path / "backend" / name / "test_example.py"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


def test_inventory_includes_module_and_suites_directories_and_separates_worker(tmp_path):
    paths = {path.as_posix() for path in gate.suite_paths(_tree(tmp_path))}
    assert paths == {"tests", "persistent_agents/tests", "audit/tests",
                     "evaluation/suites", "agents/journal_review/tests"}


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
                str(root / "components/AstralPlane/src"), str(root / "components/LETS/src"),
            ]
            assert "backend_web_test_reporter" in command
            if scenario == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            if scenario != "missing":
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
    calls = []
    monkeypatch.setattr(gate, "run", lambda root, output, *, group: calls.append(
        (root, output, group)) or 23)
    arguments = ["gate", "--root", str(tmp_path), "--output", str(tmp_path / "out")]
    monkeypatch.setattr(gate.sys, "argv", arguments)
    assert gate.main() == 23
    monkeypatch.setattr(gate.sys, "argv", [*arguments, "--group", "modules"])
    assert gate.main() == 23
    expected = (tmp_path.resolve(), (tmp_path / "out").resolve())
    assert calls == [(*expected, "all"), (*expected, "modules")]


ALL_SUITES = [
    "backend-agents-journal_review-tests", "backend-audit-tests", "backend-evaluation-suites",
    "backend-persistent_agents-tests", "backend-tests",
    "perf-concurrent_surfaces.py", "perf-voice_concurrent_turns.py", "tooling",
]


def _passing_suites(monkeypatch, *, timeout_suite=None):
    monkeypatch.setattr(gate.sys, "version_info", (3, 11))
    monkeypatch.setattr(gate, "require_isolated_postgres", lambda env: None)
    monkeypatch.setattr(gate, "source_identity", lambda root: "a" * 40)
    monkeypatch.setattr(gate, "isolated_suite_database", lambda env: nullcontext(env))
    timeouts = {}

    def execute(command, **kwargs):
        report = next((arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml=")), None)
        if report:
            environment = kwargs["env"]
            timeouts[environment["BQ_SUITE_NAME"]] = kwargs["timeout"]
            if environment["BQ_SUITE_NAME"] == timeout_suite:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            Path(report).write_text('<testsuites><testsuite><testcase name="one"/></testsuite></testsuites>',
                                    encoding="utf-8")
            Path(environment["BQ_SUITE_INVENTORY_PATH"]).write_text(json.dumps({
                "schema_version": 1, "source_commit": environment["BQ_SOURCE_COMMIT"],
                "suite": environment["BQ_SUITE_NAME"], "tests": ["::one"],
            }), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, "run", execute)
    return timeouts


GROUP_SUITES = {
    "tests": ["backend-tests"],
    "persistent_agents": ["backend-persistent_agents-tests"],
    "modules": [name for name in ALL_SUITES
                if name not in {"backend-tests", "backend-persistent_agents-tests"}],
}


@pytest.mark.parametrize("group,expected", [
    (None, ALL_SUITES), ("all", ALL_SUITES), *GROUP_SUITES.items(),
])
def test_suite_groups_plan_whole_suites_and_record_the_group(tmp_path, monkeypatch, group, expected):
    root = _tree(tmp_path)
    output = root / "evidence"
    _passing_suites(monkeypatch)
    assert (gate.run(root, output) if group is None else gate.run(root, output, group=group)) == 0
    plan = json.loads((output / "suite-plan.json").read_text(encoding="utf-8"))
    evidence = json.loads((output / "test-results.json").read_text(encoding="utf-8"))
    assert plan["group"] == evidence["group"] == (group or "all")
    assert [item["suite"] for item in plan["suites"]] == expected
    assert [item["suite"] for item in evidence["suites"]] == expected
    assert evidence["status"] == "pass"


def test_whole_suite_groups_partition_the_complete_plan(tmp_path):
    commands = gate.suite_commands(_tree(tmp_path))
    groups = {group: gate.group_commands(commands, group) for group in GROUP_SUITES}
    assert gate.GROUPS == ("all", "tests", "persistent_agents", "modules")
    assert gate.group_commands(commands, "all") == commands
    assert {group: [name for _cwd, _path, name in selected] for group, selected in groups.items()} == GROUP_SUITES
    assert sorted(command for selected in groups.values() for command in selected) == sorted(commands)
    with pytest.raises(ValueError, match="unknown suite group"):
        gate.group_commands(commands, "everything")


def test_unknown_group_is_rejected_before_database_or_source_access(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.sys, "version_info", (3, 11))
    touched = []
    monkeypatch.setattr(gate, "require_isolated_postgres", lambda env: touched.append("database"))
    monkeypatch.setattr(gate, "source_identity", lambda root: touched.append("source"))
    with pytest.raises(ValueError, match="unknown suite group"):
        gate.run(_tree(tmp_path), tmp_path / "evidence", group="everything")
    assert touched == []
    assert not (tmp_path / "evidence").exists()
    monkeypatch.setattr(gate.sys, "argv", ["gate", "--output", str(tmp_path / "out"),
                                          "--group", "everything"])
    with pytest.raises(SystemExit) as failure:
        gate.main()
    assert failure.value.code == 2


def test_each_suite_is_bounded_by_thirty_minutes_and_a_timeout_fails(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    output = root / "evidence"
    timeouts = _passing_suites(monkeypatch, timeout_suite="backend-persistent_agents-tests")
    assert gate.run(root, output) == 1
    assert gate.SUITE_TIMEOUT_SECONDS == 1800
    assert timeouts == dict.fromkeys(ALL_SUITES, 1800)
    evidence = json.loads((output / "test-results.json").read_text(encoding="utf-8"))
    results = {item["suite"]: item for item in evidence["suites"]}
    assert results["backend-persistent_agents-tests"]["exit_code"] == 124
    assert results["backend-persistent_agents-tests"]["status"] == "fail"
    assert evidence["status"] == "fail"
    assert all(item["status"] == "pass" for name, item in results.items()
               if name != "backend-persistent_agents-tests")


def _fake_docker(tmp_path):
    directory = tmp_path / "fake-bin"
    directory.mkdir()
    docker = directory / "docker"
    docker.write_text(f"""#!{sys.executable}
import json, os, sys
arguments = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")
if arguments[:1] == ["exec"]:
    print("{{}}")
if arguments[:1] == ["run"] and any(argument.endswith("-negative") for argument in arguments):
    sys.exit(78)
""", encoding="utf-8")
    docker.chmod(0o755)
    (directory / "python").symlink_to(sys.executable)
    return directory


@pytest.mark.parametrize("group,expected", [
    (None, "all"), ("all", "all"), ("tests", "tests"), ("persistent_agents", "persistent_agents"),
    ("modules", "modules"), ("everything", None),
])
def test_image_gate_passes_only_a_known_group_to_the_suite_runner(tmp_path, group, expected):
    log = tmp_path / "docker.jsonl"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ASTRAL_GATE_")}
    environment.update({
        "PATH": str(_fake_docker(tmp_path)) + os.pathsep + environment["PATH"],
        "FAKE_DOCKER_LOG": str(log), "ASTRAL_GATE_POLICY_ROOT": str(gate.POLICY_ROOT),
    })
    if group is not None:
        environment["ASTRAL_GATE_GROUP"] = group
    completed = subprocess.run(
        ["bash", str(gate.POLICY_ROOT / "scripts/backend_web_image_gate.sh"), "sha256:" + "c" * 64, "tests"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False,
    )
    if expected is None:
        assert completed.returncode != 0
        assert not log.exists()
        return
    assert completed.returncode == 0, completed.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    runner = next(call for call in calls if call[:1] == ["run"]
                  and any(argument.endswith("-test") for argument in call))
    assert runner[runner.index(f"ASTRAL_GATE_GROUP={expected}") - 1] == "--env"
    assert '--group "$ASTRAL_GATE_GROUP"' in runner[-1]
    assert "/qualification-policy/scripts/run_backend_web_tests.py" in runner[-1]


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
