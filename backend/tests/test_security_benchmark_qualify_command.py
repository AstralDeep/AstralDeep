"""Drives the real-dispatch qualification command in security_benchmark/__main__.py with stand-in driver and report functions.
It checks output-path resolution, the incomplete-report fallback and the exit codes the CI gate reads."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from security_benchmark import __main__ as cli
from security_benchmark import qualification_report as qr


@pytest.fixture
def pd(monkeypatch):
    module = ModuleType("security_benchmark.drivers.personal_dispatch")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


@pytest.fixture
def stand_ins(monkeypatch, pd):
    calls = {"written": [], "validated": []}

    async def run_qualification(run_id):
        calls["run_id"] = run_id
        return ["evidence"], True

    monkeypatch.setattr(qr, "candidate_identity", lambda root: {"root": str(root)})
    pd.run_personal_qualification = run_qualification
    monkeypatch.setattr(qr, "build_report", lambda candidate, evidence, teardown_verified: {
        "candidate": candidate, "evidence": evidence, "teardown": teardown_verified})
    monkeypatch.setattr(qr, "write_report", lambda path, report: calls["written"].append((path, report)))
    monkeypatch.setattr(qr, "validate_report", lambda report, expected_candidate: calls["validated"].append(
        (report, expected_candidate)))
    return calls


def test_relative_output_resolves_under_repository_root(stand_ins, capsys):
    assert cli.main(["--qualify-real-dispatch", "--out", "artifacts-dir", "--run-id", "r1"]) == 0
    root = Path(cli.__file__).resolve().parents[2]
    (path, report), = stand_ins["written"]
    assert path == root / "artifacts-dir" / "real-dispatch-qualification.json"
    assert stand_ins["run_id"] == "r1"
    assert stand_ins["validated"] == [(report, {"root": str(root)})]
    assert f"qualification passed: {path}" in capsys.readouterr().out


def test_absolute_output_is_kept(stand_ins, tmp_path):
    assert cli.main(["--qualify-real-dispatch", "--out", str(tmp_path)]) == 0
    assert stand_ins["written"][0][0] == tmp_path / "real-dispatch-qualification.json"


def test_validation_failure_keeps_written_report_and_fails(stand_ins, monkeypatch, capsys):
    def refuse(report, expected_candidate):
        raise ValueError("candidate drift")

    monkeypatch.setattr(qr, "validate_report", refuse)
    assert cli.main(["--qualify-real-dispatch"]) == 1
    assert len(stand_ins["written"]) == 1
    assert "incomplete: ValueError: candidate drift" in capsys.readouterr().err


def test_execution_failure_writes_incomplete_report(stand_ins, pd, monkeypatch, capsys, tmp_path):
    async def broken(run_id):
        raise RuntimeError("node down")

    pd.run_personal_qualification = broken
    assert cli.main(["--qualify-real-dispatch", "--out", str(tmp_path)]) == 1
    (path, report), = stand_ins["written"]
    assert report["status"] == "incomplete"
    assert report["errors"] == ["execution_failed:RuntimeError"]
    assert report["teardown"] == {"status": "unverified"}
    assert report["candidate"] == {"root": str(Path(cli.__file__).resolve().parents[2])}
    assert "RuntimeError: node down" in capsys.readouterr().err


def test_incomplete_report_write_failure_is_still_reported(stand_ins, monkeypatch, capsys):
    def candidate_failure(root):
        raise RuntimeError("no git")

    def unwritable(path, report):
        raise OSError("read-only")

    monkeypatch.setattr(qr, "candidate_identity", candidate_failure)
    monkeypatch.setattr(qr, "write_report", unwritable)
    assert cli.main(["--qualify-real-dispatch"]) == 1
    assert "RuntimeError: no git" in capsys.readouterr().err
