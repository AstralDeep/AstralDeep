"""Tests for the CLI entry point: deterministic scripted run exit codes and artifacts,
and the explicit environment-flag gate for opt-in live qualification.
"""

from __future__ import annotations

import json

from prompt_optimization import __main__ as cli


def test_scripted_run_writes_artifacts_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(cli.repo_root().parent)
    out = tmp_path / "artifacts"
    code = cli.main(["--out", str(out)])
    assert code == 0
    candidate = json.loads((out / "candidate_prompt.json").read_text(encoding="utf-8"))
    report = json.loads((out / "optimization_report.json").read_text(encoding="utf-8"))
    assert candidate["adoption_status"] == "proposed_not_applied"
    assert report["status"] == "completed"
    assert report["model"]["mode"].startswith("scripted:")
    assert report["budget"]["usage_complete"] is True


def test_scripted_run_is_deterministic(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    assert cli.main(["--out", str(first)]) == 0
    assert cli.main(["--out", str(second)]) == 0
    report_one = json.loads((first / "optimization_report.json").read_text(encoding="utf-8"))
    report_two = json.loads((second / "optimization_report.json").read_text(encoding="utf-8"))
    assert report_one["selected_digest"] == report_two["selected_digest"]
    assert report_one["budget"]["calls"] == report_two["budget"]["calls"]
    assert report_one["fixture"] == report_two["fixture"]


def test_live_config_requires_explicit_env_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRAL_PROMPT_OPTIMIZATION_LIVE", raising=False)
    config = tmp_path / "live.json"
    config.write_text(
        json.dumps({"base_url": "http://127.0.0.1:9999/v1", "model": "synthetic"}),
        encoding="utf-8",
    )
    assert cli.main(["--live-config", str(config)]) == 2


def test_live_flag_with_closed_local_endpoint_completes_unqualified(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ASTRAL_PROMPT_OPTIMIZATION_LIVE", "1")
    config = tmp_path / "live.json"
    config.write_text(
        json.dumps({"base_url": "http://127.0.0.1:9/v1", "model": "synthetic"}),
        encoding="utf-8",
    )
    out = tmp_path / "artifacts"
    code = cli.main(["--live-config", str(config), "--out", str(out)])
    assert code == 0
    report = json.loads((out / "optimization_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert report["qualified"] is False
    assert report["recommendation"] == "inconclusive"
    assert report["model"]["mode"].startswith("live:")
    assert report["model"]["egress"] == "explicit-config-endpoint-only"


def test_module_entry_point_runs_full_pipeline(tmp_path):
    import subprocess
    import sys

    out = tmp_path / "artifacts"
    completed = subprocess.run(
        [sys.executable, "-m", "prompt_optimization", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(cli.repo_root() / "backend"),
    )
    assert completed.returncode == 0, completed.stderr
    assert (out / "candidate_prompt.json").exists()
    assert (out / "optimization_report.json").exists()


def test_rounds_bounds_are_clamped():
    limits = cli.build_limits(99)
    assert limits.max_rounds == 8
    assert cli.build_limits(0).max_rounds == 1


def test_parse_args_defaults():
    args = cli.parse_args([])
    assert args.rounds == 4
    assert args.live_config is None
    assert args.model_label == cli.DEFAULT_MODEL_LABEL
    assert args.out is None
