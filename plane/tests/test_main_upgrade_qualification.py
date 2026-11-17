"""Verifies the exact-main PostgreSQL upgrade probe records independently built
baseline evidence and refuses invented predecessor digests. CLI failures must leave
explicit failed evidence without disclosing connection credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import qualify_main_upgrade as probe

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qualify_main_upgrade.py"


def test_cli_refuses_missing_database_and_records_failed_evidence(tmp_path):
    report = tmp_path / "upgrade.json"
    source_root, baseline = probe.baseline_input()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--baseline-sha",
            baseline,
            "--baseline-source-root",
            str(source_root),
            "--report",
            str(report),
        ],
        cwd=ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "ASTRALPLANE_TEST_POSTGRES_DSN"
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert report.is_file()
    assert json.loads(report.read_text())["status"] == "failed"
    assert "admission remains closed" in result.stderr


@pytest.fixture(scope="module")
def actual_main(tmp_path_factory):
    target = tmp_path_factory.mktemp("main-source")
    source_root, sha = probe.baseline_input()
    probe.archive_source(source_root, sha, target)
    code = (
        "import sys,json; sys.path.insert(0,sys.argv[1]); "
        "from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION as r, "
        "MIGRATION_REGISTRY as m; "
        "print(json.dumps({'revision':r.schema_revision,'digest':m.digest,"
        "'metadata':{'revision':r.schema_revision,'astralplane_migration_digest':m.digest},"
        "'migrations':[{'name':e.name,'sources':list(e.source_revisions),"
        "'target':e.target_revision,'checksum':e.checksum} for e in m.migrations]}))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code, str(target / "src")],
        cwd=target,
        capture_output=True,
        text=True,
        check=True,
    )
    return source_root, sha, json.loads(completed.stdout)


def test_future_candidate_cannot_invent_main_predecessor_digest(actual_main):
    _, _, baseline = actual_main
    original = probe.CURRENT_DATA_PLANE_REVISION
    retained = tuple(
        pair for pair in original.accepted_predecessor_digests if pair[0] != baseline["revision"]
    )
    future = replace(
        original,
        schema_revision="999.001",
        read_compatible_from=(*original.read_compatible_from, original.schema_revision),
        accepted_predecessor_digests=(
            *retained,
            (baseline["revision"], "0" * 64),
        ),
    )
    with pytest.raises(ValueError, match="actual main migration digest"):
        probe.validate_lineage(baseline, future)
    corrected = replace(
        future,
        accepted_predecessor_digests=(
            *retained,
            (baseline["revision"], baseline["digest"]),
        ),
    )
    probe.validate_lineage(baseline, corrected)


def test_candidate_cannot_rewrite_historical_edge(actual_main):
    _, _, baseline = actual_main
    registry = probe.MIGRATION_REGISTRY
    historical_name = baseline["migrations"][-1]["name"]
    damaged = type(
        "DamagedRegistry",
        (),
        {
            "migrations": (
                replace(item, checksum="0" * 64) if item.name == historical_name else item
                for item in registry.migrations
            )
        },
    )()
    with pytest.raises(ValueError, match="historical main migration edge"):
        probe.validate_lineage(baseline, registry=damaged)


def test_baseline_markers_must_match_its_own_registry(actual_main):
    _, _, baseline = actual_main
    changed = {**baseline, "metadata": {"revision": baseline["revision"]}}
    with pytest.raises(ValueError, match="baseline markers"):
        probe.validate_lineage(changed)


def test_archive_refuses_non_main_and_non_sha_inputs(tmp_path, actual_main):
    source_root, sha, _ = actual_main
    with pytest.raises(ValueError, match="exact commit"):
        probe.archive_source(source_root, "main", tmp_path)
    with pytest.raises(ValueError, match="fetched origin/main"):
        probe.archive_source(source_root, "0" * 40, tmp_path)
    assert len(sha) == 40


def test_exact_main_populated_upgrade_and_repeat(postgres_administrator_dsn, actual_main):
    source_root, sha, baseline = actual_main
    report = probe.qualify(
        root=ROOT,
        baseline_sha=sha,
        administrator_dsn=postgres_administrator_dsn,
        baseline_source_root=source_root,
    )
    assert report["status"] == "passed"
    assert report["baseline_sha"] == sha
    assert report["baseline_migration_digest"] == baseline["digest"]
    assert report["baseline_revision"] == baseline["revision"]
    assert report["repeat_applied_steps"] == []
    assert report["populated_owner_count"] == 2
    assert report["populated_draft_count"] == 2
    assert report["populated_share_count"] == 2
    assert report["release_authorized"] is False


def test_explicit_owner_baseline_inputs_are_required_as_a_pair(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRALPLANE_UPGRADE_BASELINE_ROOT", str(tmp_path))
    monkeypatch.delenv("ASTRALPLANE_UPGRADE_BASELINE_SHA", raising=False)
    with pytest.raises(ValueError, match="supplied together"):
        probe.baseline_input()
    monkeypatch.setenv("ASTRALPLANE_UPGRADE_BASELINE_SHA", "a" * 40)
    assert probe.baseline_input() == (tmp_path.resolve(), "a" * 40)


def test_main_outputs_passed_evidence(tmp_path, monkeypatch, capsys):
    path = tmp_path / "report.json"
    monkeypatch.setattr(probe, "qualify", lambda **_: {"status": "passed"})
    assert probe.main(["--baseline-sha", "a" * 40, "--report", str(path)]) == 0
    assert json.loads(path.read_text()) == {"status": "passed"}
    assert json.loads(capsys.readouterr().out) == {"status": "passed"}


def test_main_redacts_failed_connection(tmp_path, monkeypatch, capsys):
    path = tmp_path / "report.json"

    def fail(**_):
        raise RuntimeError("synthetic-secret-must-not-leak")

    monkeypatch.setattr(probe, "qualify", fail)
    assert probe.main(["--baseline-sha", "a" * 40, "--report", str(path)]) == 2
    assert "synthetic-secret" not in path.read_text() + capsys.readouterr().err
