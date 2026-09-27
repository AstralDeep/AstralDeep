"""Tests for scripts/merge_backend_web_coverage.py: merged tests and modules group coverage
equals one complete run_backend_web_tests.py pass, and incomplete, inconsistent or unusable
group evidence is refused.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path

import pytest
from coverage import CoverageData

from scripts import merge_backend_web_coverage as merge
from scripts import run_backend_web_tests as gate


WORKSPACE = {
    ".gitignore": "build/\n",
    "pytest.ini": "[pytest]\n",
    "backend/service/__init__.py": "",
    "backend/service/decide.py": (
        "def decide(value):\n    if value > 0:\n        return 'positive'\n    return 'other'\n"
    ),
    "backend/service/idle.py": "def idle():\n    return 0\n",
    "backend/tests/test_decide.py": (
        "from service.decide import decide\n\n\ndef test_positive():\n"
        "    assert decide(1) == 'positive'\n"
    ),
    "backend/tests/perf/concurrent_surfaces.py": "def test_surfaces():\n    assert True\n",
    "backend/tests/perf/voice_concurrent_turns.py": "def test_turns():\n    assert True\n",
    "backend/persistent_agents/tests/test_agents.py": (
        "from service.decide import decide\n\n\ndef test_other():\n"
        "    assert decide(-1) == 'other'\n"
    ),
    "scripts/tool.py": "def tool(flag):\n    if flag:\n        return 1\n    return 0\n",
    "scripts/tests/test_tool.py": (
        "from scripts.tool import tool\n\n\ndef test_tool():\n    assert tool(True) == 1\n"
    ),
}


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=merge@example.invalid",
         "-c", "user.name=Merge Fixture", *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _workspace(root: Path) -> Path:
    for relative, content in WORKSPACE.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "workspace")
    return root


def _report_facts(path: Path) -> tuple[object, ...]:
    document = ET.parse(path).getroot()
    return (
        sorted((key, value) for key, value in document.attrib.items() if key != "timestamp"),
        sorted(sorted(node.attrib.items()) for node in document.iter("package")),
        sorted(
            (sorted(node.attrib.items()), [sorted(line.attrib.items()) for line in node.iter("line")])
            for node in document.iter("class")
        ),
    )


def _sources(path: Path) -> list[str]:
    return [node.text for node in ET.parse(path).getroot().iter("source")]


def _load_collector():
    spec = importlib.util.spec_from_file_location(
        "merge_changed_coverage", gate.POLICY_ROOT / "scripts/check_changed_coverage.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_collector()


def _semantic_identity(path: Path, target: str) -> str:
    return collector.coverage_report_identity(path.read_bytes(), target)["semantic_sha256"]


def test_merged_group_reports_equal_one_complete_run(tmp_path, monkeypatch):
    workspace = _workspace((tmp_path / "workspace").resolve())
    monkeypatch.setattr(gate.sys, "version_info", (3, 11))
    monkeypatch.setattr(gate, "require_isolated_postgres", lambda environ: None)
    monkeypatch.setattr(gate, "isolated_suite_database", lambda environ: nullcontext(environ))
    for key in ("COVERAGE_FILE", "COVERAGE_PROCESS_START", "COVERAGE_RCFILE"):
        monkeypatch.delenv(key, raising=False)
    evidence = workspace / "build"
    for group in ("all", "tests", "modules"):
        assert gate.run(workspace, evidence / group, group=group) == 0
    repository = (tmp_path / "repository").resolve()
    subprocess.run(["git", "clone", "-q", str(workspace), str(repository)], check=True)

    output = repository / "build/backend-web"
    merge.merge(repository, [evidence / "tests", evidence / "modules"], output,
                recorded_root=str(workspace))

    for report, target in (("backend-python.xml", "backend_python"),
                           ("tooling-python.xml", "tooling_python")):
        complete = evidence / "all" / report
        merged = output / report
        assert _report_facts(merged) == _report_facts(complete)
        assert _semantic_identity(merged, target) == _semantic_identity(complete, target)
        assert _sources(complete) == [str(workspace)]
        assert _sources(merged) == [str(repository)]
    assert _report_facts(evidence / "tests/backend-python.xml") != _report_facts(
        evidence / "all/backend-python.xml")
    measured = CoverageData(basename=str(output / ".coverage"))
    measured.read()
    assert measured.measured_files() == {
        str(repository / relative) for relative in (
            "backend/service/__init__.py", "backend/service/decide.py",
            "backend/service/idle.py", "scripts/tool.py",
        )
    }


def _digest(name: str) -> str:
    return hashlib.sha256((gate.POLICY_ROOT / "scripts" / name).read_bytes()).hexdigest()


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for relative in ("backend/tests/test_one.py", "backend/persistent_agents/tests/test_two.py",
                     "backend/service.py"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n", encoding="utf-8")
    return root


def _evidence(tmp_path: Path, root: Path, recorded: Path) -> dict[str, Path]:
    commands = gate.suite_commands(root)
    directories = {}
    for group in ("tests", "modules"):
        directory = tmp_path / group
        directory.mkdir()
        suites = [{"suite": name, "cwd": cwd.relative_to(root).as_posix(), "path": path}
                  for cwd, path, name in gate.group_commands(commands, group)]
        (directory / "suite-plan.json").write_text(json.dumps({
            "schema_version": 1, "group": group, "source_commit": "a" * 40,
            "runner_sha256": _digest("run_backend_web_tests.py"),
            "reporter_sha256": _digest("backend_web_test_reporter.py"), "suites": suites,
        }), encoding="utf-8")
        (directory / "test-results.json").write_text(json.dumps({
            "scope": "backend-web-ci", "status": "pass", "group": group, "source_commit": "a" * 40,
            "suites": [{"suite": item["suite"], "status": "pass"} for item in suites],
        }), encoding="utf-8")
        data = CoverageData(basename=str(directory / ".coverage"))
        data.add_arcs({str(recorded / "backend/service.py"): {(-1, 1), (1, -1)}})
        data.write()
        directories[group] = directory
    return directories


def _rewrite(directory: Path, name: str, change) -> None:
    path = directory / name
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def _replace_data(directory: Path, change) -> None:
    (directory / ".coverage").unlink()
    data = CoverageData(basename=str(directory / ".coverage"))
    change(data)
    data.write()


EVIDENCE_REFUSALS = {
    "one-group": lambda groups, recorded: groups.pop("modules"),
    "duplicate-group": lambda groups, recorded: [
        _rewrite(groups["modules"], name, lambda document: document.update(group="tests"))
        for name in ("suite-plan.json", "test-results.json")],
    "complete-group": lambda groups, recorded: [
        _rewrite(groups["modules"], name, lambda document: document.update(group="all"))
        for name in ("suite-plan.json", "test-results.json")],
    "result-group": lambda groups, recorded: _rewrite(
        groups["tests"], "test-results.json", lambda document: document.update(group="modules")),
    "failed-results": lambda groups, recorded: _rewrite(
        groups["modules"], "test-results.json", lambda document: document.update(status="fail")),
    "partial-results": lambda groups, recorded: _rewrite(
        groups["modules"], "test-results.json", lambda document: document["suites"].pop()),
    "result-source": lambda groups, recorded: _rewrite(
        groups["tests"], "test-results.json", lambda document: document.update(source_commit="b" * 40)),
    "group-source": lambda groups, recorded: [
        _rewrite(groups["modules"], name, lambda document: document.update(source_commit="b" * 40))
        for name in ("suite-plan.json", "test-results.json")],
    "different-runner": lambda groups, recorded: _rewrite(
        groups["modules"], "suite-plan.json", lambda document: document.update(runner_sha256="0" * 64)),
    "stale-runner": lambda groups, recorded: [
        _rewrite(directory, "suite-plan.json", lambda document: document.update(runner_sha256="0" * 64))
        for directory in groups.values()],
    "missing-suite": lambda groups, recorded: [
        _rewrite(groups["modules"], name, lambda document: document["suites"].pop())
        for name in ("suite-plan.json", "test-results.json")],
    "duplicated-suite": lambda groups, recorded: (
        _rewrite(groups["tests"], "suite-plan.json", lambda document: document["suites"].append(
            {"suite": "tooling", "cwd": ".", "path": "scripts/tests"})),
        _rewrite(groups["tests"], "test-results.json", lambda document: document["suites"].append(
            {"suite": "tooling", "status": "pass"}))),
    "malformed-plan": lambda groups, recorded: _rewrite(
        groups["tests"], "suite-plan.json", lambda document: document["suites"].append("tooling")),
    "unversioned-plan": lambda groups, recorded: _rewrite(
        groups["tests"], "suite-plan.json", lambda document: document.pop("schema_version")),
    "boolean-version": lambda groups, recorded: _rewrite(
        groups["tests"], "suite-plan.json", lambda document: document.update(schema_version=True)),
    "untyped-identity": lambda groups, recorded: _rewrite(
        groups["modules"], "suite-plan.json", lambda document: document.update(runner_sha256=["0" * 64])),
    "untyped-suite": lambda groups, recorded: _rewrite(
        groups["tests"], "suite-plan.json", lambda document: document["suites"][0].update(path=None)),
    "unreadable-plan": lambda groups, recorded: (groups["tests"] / "suite-plan.json").write_text(
        "{", encoding="utf-8"),
    "missing-results": lambda groups, recorded: (groups["modules"] / "test-results.json").unlink(),
    "missing-data": lambda groups, recorded: (groups["modules"] / ".coverage").unlink(),
    "corrupt-data": lambda groups, recorded: (groups["tests"] / ".coverage").write_bytes(
        b"not a coverage database"),
    "statement-data": lambda groups, recorded: _replace_data(
        groups["tests"], lambda data: data.add_lines({str(recorded / "backend/service.py"): [1]})),
    "empty-data": lambda groups, recorded: _replace_data(
        groups["tests"], lambda data: data.add_arcs({})),
    "foreign-root": lambda groups, recorded: _replace_data(
        groups["tests"], lambda data: data.add_arcs({"/elsewhere/backend/service.py": {(-1, 1)}})),
    "escaping-path": lambda groups, recorded: _replace_data(
        groups["tests"], lambda data: data.add_arcs(
            {str(recorded / "backend/../../outside.py"): {(-1, 1)}})),
}
REFUSAL_REASONS = {
    "one-group": "exactly one tests group and one modules group",
    "duplicate-group": "exactly one tests group and one modules group",
    "complete-group": "does not hold a backend-web suite group plan",
    "malformed-plan": "does not hold a backend-web suite group plan",
    "unversioned-plan": "does not hold a backend-web suite group plan",
    "boolean-version": "does not hold a backend-web suite group plan",
    "untyped-identity": "does not hold a backend-web suite group plan",
    "untyped-suite": "does not hold a backend-web suite group plan",
    "result-group": "does not hold passing results",
    "failed-results": "does not hold passing results",
    "partial-results": "does not hold passing results",
    "result-source": "does not hold passing results",
    "group-source": "different sources or suite runners",
    "different-runner": "different sources or suite runners",
    "stale-runner": "different sources or suite runners",
    "missing-suite": "do not partition one complete suite plan",
    "duplicated-suite": "do not partition one complete suite plan",
    "unreadable-plan": "is not readable JSON evidence",
    "missing-results": "is not readable JSON evidence",
    "missing-data": "is not a coverage data file",
    "corrupt-data": "is not readable coverage data",
    "statement-data": "holds no branch coverage data",
    "empty-data": "holds no branch coverage data",
    "foreign-root": "outside the recorded checkout",
    "escaping-path": "outside the recorded checkout",
}


@pytest.mark.parametrize("refusal", sorted(EVIDENCE_REFUSALS))
def test_merge_refuses_incomplete_inconsistent_or_unusable_group_evidence(tmp_path, monkeypatch, refusal):
    root = _checkout(tmp_path)
    recorded = tmp_path / "recorded"
    groups = _evidence(tmp_path, root, recorded)
    EVIDENCE_REFUSALS[refusal](groups, recorded)
    monkeypatch.setattr(merge.runner, "source_identity", lambda checkout: "a" * 40)
    written = []
    monkeypatch.setattr(merge.runner, "write_coverage_reports",
                        lambda *arguments: written.append(arguments) or True)
    with pytest.raises(ValueError, match=REFUSAL_REASONS[refusal]):
        merge.merge(root, list(groups.values()), tmp_path / "merged", recorded_root=str(recorded))
    assert written == []


def test_merge_refuses_group_evidence_from_another_checkout(tmp_path, monkeypatch):
    root = _checkout(tmp_path)
    recorded = tmp_path / "recorded"
    groups = _evidence(tmp_path, root, recorded)
    monkeypatch.setattr(merge.runner, "source_identity", lambda checkout: "c" * 40)
    with pytest.raises(ValueError, match="this checkout"):
        merge.merge(root, list(groups.values()), tmp_path / "merged", recorded_root=str(recorded))


def test_merge_writes_reports_from_remapped_data_and_fails_when_they_cannot_be_written(
    tmp_path, monkeypatch,
):
    root = _checkout(tmp_path)
    recorded = tmp_path / "recorded"
    groups = _evidence(tmp_path, root, recorded)
    output = tmp_path / "merged"
    (output / ".coverage").parent.mkdir()
    (output / ".coverage").write_bytes(b"stale")
    monkeypatch.setattr(merge.runner, "source_identity", lambda checkout: "a" * 40)
    written = []

    def write(checkout, destination, environ):
        data = CoverageData(basename=environ["COVERAGE_FILE"])
        data.read()
        written.append((checkout, destination, data.measured_files(), data.has_arcs()))
        return len(written) == 1

    monkeypatch.setattr(merge.runner, "write_coverage_reports", write)
    merge.merge(root, [groups["modules"], groups["tests"]], output, recorded_root=str(recorded) + "/")
    assert written == [(root, output, {str(root / "backend/service.py")}, True)]
    with pytest.raises(ValueError, match="could not be written"):
        merge.merge(root, list(groups.values()), output, recorded_root=str(recorded))


def test_cli_merges_or_reports_a_refusal(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(merge, "merge", lambda root, groups, output, *, recorded_root: calls.append(
        (root, groups, output, recorded_root)))
    arguments = ["--root", str(tmp_path), "--output", str(tmp_path / "out"),
                 "--evidence", str(tmp_path / "tests"), "--evidence", str(tmp_path / "modules")]
    assert merge.main(arguments) == 0
    assert calls == [(tmp_path.resolve(), [(tmp_path / "tests").resolve(), (tmp_path / "modules").resolve()],
                      (tmp_path / "out").resolve(), "/workspace")]

    def refuse(*_arguments, **_keywords):
        raise ValueError("suite groups do not partition one complete suite plan")

    monkeypatch.setattr(merge, "merge", refuse)
    assert merge.main([*arguments, "--recorded-root", "/container"]) == 1
    assert "suite groups do not partition one complete suite plan" in capsys.readouterr().err
