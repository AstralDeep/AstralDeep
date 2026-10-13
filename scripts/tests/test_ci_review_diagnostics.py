"""Tests for scripts/ci_review_diagnostics.py: deterministic SHA-bound review
diagnostics over a synthetic repository, covering classification, suite plans,
refusals, and byte-identical reruns. The script runs in-process so the changed-line
coverage gate observes every branch; one CLI subprocess test pins the entry point.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_review_diagnostics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ci_review_diagnostics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_module()


def _git(repo, *arguments):
    subprocess.run(["git", "-C", str(repo), *arguments], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def candidate_repo(tmp_path, monkeypatch):
    (tmp_path / "backend" / "tests" / "perf").mkdir(parents=True)
    (tmp_path / "backend" / "persistent_agents" / "tests").mkdir(parents=True)
    (tmp_path / "scripts" / "tests").mkdir(parents=True)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "candidate@example.invalid")
    _git(tmp_path, "config", "user.name", "Candidate")
    (tmp_path / "backend" / "tests" / "test_base.py").write_text("def test_base():\n    pass\n")
    (tmp_path / "backend" / "tests" / "perf" / "voice_concurrent_turns.py").write_text("")
    (tmp_path / "backend" / "persistent_agents" / "tests" / "test_base.py").write_text(
        "def test_base():\n    pass\n")
    (tmp_path / "scripts" / "tests" / "test_base.py").write_text("def test_base():\n    pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "backend" / "orchestrator").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "backend" / "orchestrator" / "feature.py").write_text("value = 1\n")
    (tmp_path / "scripts" / "tool.py").write_text("value = 2\n")
    (tmp_path / "docs" / "note.md").write_text("note\n")
    (tmp_path / "backend" / "tests" / "test_added.py").write_text("def test_added():\n    pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "head")
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    monkeypatch.chdir(tmp_path)
    return tmp_path, base, head


def _run(base, head, output):
    return module.main(["--base", base, "--head", head, "--output", str(output)])


def test_golden_diagnostics_classify_changes_and_plan(candidate_repo, tmp_path):
    repo, base, head = candidate_repo
    output = tmp_path / "review-diagnostics.json"
    assert _run(base, head, output) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["base_sha"] == base and document["head_sha"] == head
    assert document["repository"] == repo.name
    targets = {item["path"]: item["coverage_target"] for item in document["changed_paths"]}
    assert targets["backend/orchestrator/feature.py"] == "backend_python"
    assert targets["scripts/tool.py"] == "tooling_python"
    assert targets["docs/note.md"] is None
    assert targets["backend/tests/test_added.py"] is None
    statuses = {item["path"]: item["status"] for item in document["changed_paths"]}
    assert statuses["backend/orchestrator/feature.py"] == "A"
    suite_names = {entry["suite"] for entry in document["suite_plan"]}
    assert {"backend-tests", "backend-persistent_agents-tests", "tooling"} <= suite_names
    files = document["suite_test_files"]
    assert "backend/tests/test_added.py" in files["backend-tests"]
    assert files["tooling"] == ["scripts/tests/test_base.py"]
    policy = document["qualification_policy"]
    assert policy["repository_profile"] == "deep"
    assert set(policy["required_producer_keys"]) == {"backend", "voice_worker", "tooling"}


def test_reruns_are_byte_identical(candidate_repo, tmp_path):
    _repo, base, head = candidate_repo
    first, second = Path("first.json"), Path("second.json")
    assert _run(base, head, first) == 0
    assert _run(base, head, second) == 0
    assert first.read_bytes() == second.read_bytes()


def test_refuses_unknown_non_hex_or_zero_revisions(candidate_repo, tmp_path):
    _repo, base, head = candidate_repo
    output = tmp_path / "diagnostics.json"
    unknown = "a" * 40
    for bad, expected in (("0" * 40, "invalid_revision"), ("not-a-sha", "invalid_revision"),
                          (base[:-1] + "g", "invalid_revision"),
                          (unknown, "unknown_revision"), ("head", "invalid_revision")):
        assert _run(bad, head, output) == 1, bad
        document = json.loads(output.read_text(encoding="utf-8"))
        assert document["status"] == "error"
        assert document["error"]["code"] == expected, bad


def test_refuses_equal_or_non_ancestor_base(candidate_repo, tmp_path):
    repo, base, head = candidate_repo
    output = tmp_path / "diagnostics.json"
    assert _run(head, head, output) == 1
    assert json.loads(output.read_text())["error"]["code"] == "empty_revision_range"
    _git(repo, "checkout", "-q", "-b", "sibling")
    (repo / "docs" / "other.md").write_text("other\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "sibling")
    sibling = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert _run(sibling, head, output) == 1
    assert json.loads(output.read_text())["error"]["code"] == "non_ancestor_base"


def test_refuses_empty_suite_and_writes_error_document(candidate_repo, tmp_path):
    repo, base, head = candidate_repo
    (repo / "scripts" / "tests" / "test_base.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break-tooling-suite")
    broken = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    output = tmp_path / "nested" / "diagnostics.json"
    assert _run(base, broken, output) == 1
    document = json.loads(output.read_text())
    assert document["status"] == "error"
    assert document["error"]["code"] == "empty_suite"
    assert output.exists()


def test_refuses_missing_suite_path_in_the_working_tree(candidate_repo, tmp_path):
    _repo, base, head = candidate_repo
    Path.cwd().joinpath(
        "backend", "tests", "perf", "voice_concurrent_turns.py").unlink()
    output = tmp_path / "diagnostics.json"
    assert _run(base, head, output) == 1
    assert json.loads(output.read_text())["error"]["code"] == "missing_suite"


def test_git_failures_fail_closed(candidate_repo, tmp_path, monkeypatch):
    repo, base, head = candidate_repo
    output = tmp_path / "diagnostics.json"
    with pytest.raises(module.ReviewDiagnosticsError) as raised:
        module._git(repo, ["rev-parse", "--verify", "not-a-revision^{}"])
    assert raised.value.code == "git_error"
    real_run = subprocess.run

    def failing_ancestry(arguments, **kwargs):
        if "--is-ancestor" in arguments:
            return subprocess.CompletedProcess(arguments, 2, b"", b"")
        return real_run(arguments, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", failing_ancestry)
    assert _run(base, head, output) == 1
    assert json.loads(output.read_text())["error"]["code"] == "git_error"


def test_refuses_tag_object_base_that_resolves_elsewhere(candidate_repo, tmp_path):
    repo, base, head = candidate_repo
    _git(repo, "tag", "-a", "reviewed", "-m", "reviewed base", base)
    tag_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "reviewed"],
                             capture_output=True, text=True, check=True).stdout.strip()
    output = tmp_path / "diagnostics.json"
    assert _run(tag_sha, head, output) == 1
    assert json.loads(output.read_text())["error"]["code"] == "revision_mismatch"


def test_refuses_oversized_changes_and_suites(candidate_repo, tmp_path, monkeypatch):
    _repo, base, head = candidate_repo
    output = tmp_path / "diagnostics.json"
    monkeypatch.setattr(module, "MAX_CHANGED_PATHS", 2)
    assert _run(base, head, output) == 1
    assert json.loads(output.read_text())["error"]["code"] == "changed_path_overflow"
    monkeypatch.setattr(module, "MAX_CHANGED_PATHS", 10_000)
    monkeypatch.setattr(module, "MAX_SUITE_TEST_FILES", 0)
    assert _run(base, head, output) == 1
    assert json.loads(output.read_text())["error"]["code"] == "suite_overflow"


def test_command_line_entry_point_runs_the_real_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    resolved = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    base = "16438c926fe5acfcdd640c6f96fc3ac6e4455ff6"
    output = tmp_path / "review-diagnostics.json"
    assert module.main(["--base", base, "--head", resolved,
                        "--output", str(output)]) == 0
    document = json.loads(output.read_text())
    assert document["head_sha"] == resolved and document["repository"] == ROOT.name
    changed = {item["path"] for item in document["changed_paths"]}
    assert "scripts/ci_review_diagnostics.py" in changed
    assert "backend/orchestrator/user_skill_import.py" in changed
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", resolved,
         "--output", str(tmp_path / "cli.json")], cwd=ROOT, capture_output=True, text=True)
    assert process.returncode == 0
    assert json.loads((tmp_path / "cli.json").read_text()) == document
