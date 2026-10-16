"""Tests for the answer-behavior CLI: frozen default run, gate exit codes, and the
fail-closed live opt-in.
"""

from __future__ import annotations


from answer_behavior.__main__ import main
from answer_behavior.drivers import LIVE_ENV_FLAG


def test_frozen_default_run_exits_zero(tmp_path, capsys):
    exit_code = main(["--out", str(tmp_path), "--run-id", "cli-ok", "--stamp", "s"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "ANSWER_BEHAVIOR_REPORT.md" in output
    assert "OK" in output


def test_frozen_run_trips_gate_with_unreachable_floor(tmp_path, capsys):
    exit_code = main(["--out", str(tmp_path), "--run-id", "cli-trip", "--floor", "1.01"])
    assert exit_code == 1
    assert "CANDIDATE GATE TRIPPED" in capsys.readouterr().out


def test_frozen_run_can_score_baseline_only(tmp_path, capsys):
    exit_code = main(["--out", str(tmp_path), "--actor", "baseline", "--run-id", "cli-base"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "VIOLATION" in output


def test_live_mode_refuses_without_opt_in_env(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(LIVE_ENV_FLAG, raising=False)
    assert main(["--mode", "live", "--out", str(tmp_path)]) == 2
    assert "refusing to run live mode" in capsys.readouterr().out


def test_frozen_run_survives_invalid_corpus_path(tmp_path):
    assert main(["--corpus", str(tmp_path / "missing.json")]) == 2


def test_live_mode_runs_end_to_end_with_opt_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(LIVE_ENV_FLAG, "1")
    exit_code = main([
        "--mode", "live", "--out", str(tmp_path), "--run-id", "cli-live",
        "--live-max-cases", "1", "--limit", "1",
    ])
    assert exit_code == 2
    output = capsys.readouterr().out
    assert "runtime config file" in output
