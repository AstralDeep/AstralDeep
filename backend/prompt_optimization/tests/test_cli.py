"""Exercises scripted CLI artifacts, explicit fixture designation, persistent reuse refusal and configuration failures.
No CLI test opens a third-party provider connection.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from prompt_optimization import __main__ as cli
from prompt_optimization.tests.support import holdout_seals, reset_holdouts

_CONFIGURED_SEALS = cli.configured_holdout_seals


@pytest.fixture(autouse=True)
def seals(monkeypatch):
    reset_holdouts()

    @contextmanager
    def configured(_root):
        yield holdout_seals

    monkeypatch.setattr(cli, "configured_holdout_seals", configured)


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
    assert report["model"]["actual_model_qualified"] is False
    assert report["budget"]["usage_complete"] is True
    assert report["optimizer"]["holdout_exposure_run_ids"]


def test_second_cli_invocation_cannot_reuse_final_inputs(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    assert cli.main(["--out", str(first)]) == 0
    assert cli.main(["--out", str(second)]) == 2
    assert not second.exists()


def test_raw_provider_config_cannot_open_a_live_route(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_PROMPT_OPTIMIZATION_LIVE", "1")
    config = tmp_path / "live.json"
    config.write_text('{"base_url":"https://example.invalid","model":"raw","api_key":"secret"}')
    with pytest.raises(SystemExit) as error:
        cli.main(["--live-config", str(config)])
    assert error.value.code == 2


def test_cli_new_holdout_does_not_reuse_exposed_cases(tmp_path):
    from prompt_optimization.fixture import build_fixture

    assert cli.main(["--out", str(tmp_path / "one")]) == 0
    cases = [case.to_dict() for case in build_fixture().cases("holdout")]
    for case in cases:
        case["request"] = "Fresh synthetic scenario " + case["request"]
    fixture = tmp_path / "new.json"
    fixture.write_text(json.dumps({"schema_version": 1, "holdout": cases}))
    assert cli.main(["--holdout", str(fixture), "--out", str(tmp_path / "two")]) == 0


def test_module_entry_point_exposes_scripted_help():
    import subprocess
    import sys

    completed = subprocess.run([sys.executable, "-m", "prompt_optimization", "--help"],
                               capture_output=True, text=True, timeout=30,
                               cwd=str(cli.repo_root() / "backend"))
    assert completed.returncode == 0, completed.stderr
    assert "--holdout" in completed.stdout


def test_configuration_or_persistence_failure_never_exports_success(tmp_path, monkeypatch):
    @contextmanager
    def unavailable(_root):
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(cli, "configured_holdout_seals", unavailable)
    out = tmp_path / "artifacts"
    assert cli.main(["--out", str(out)]) == 2
    assert not out.exists()


def test_invalid_fixture_never_exports_success(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{}")
    assert cli.main(["--holdout", str(path), "--out", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out").exists()


def test_rounds_bounds_are_clamped():
    assert cli.build_limits(99).max_rounds == 8
    assert cli.build_limits(0).max_rounds == 1


def test_parse_args_defaults():
    args = cli.parse_args([])
    assert args.rounds == 4
    assert args.holdout is None
    assert args.model_label == cli.DEFAULT_MODEL_LABEL
    assert args.out is None


def test_cli_requires_explicit_qualification_database_before_plane_initialization(monkeypatch):
    monkeypatch.delenv("ASTRAL_PROMPT_OPTIMIZATION_DATABASE_URL", raising=False)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("application database must not be opened")

    monkeypatch.setattr("orchestrator.plane_composition.compose_plane_from_environment", unexpected)
    with pytest.raises(ValueError, match="qualification database"):
        with _CONFIGURED_SEALS(cli.repo_root()):
            pass


def test_configured_seals_close_plane_after_failure_and_keep_application_config_out(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("ASTRAL_PROMPT_OPTIMIZATION_DATABASE_URL", "postgresql://qualification.invalid/synthetic")
    monkeypatch.setenv("DATABASE_URL", "postgresql://application.invalid/production")
    monkeypatch.setenv("ASTRAL_PROMPT_OPTIMIZATION_STATE_ROOT", str(tmp_path.resolve()))
    observed = {}
    closed = []

    def compose(_manifest, *, environ):
        observed.update(environ)
        return SimpleNamespace(runtime=holdout_seals.runtime, close=lambda: closed.append(True))

    monkeypatch.setattr("orchestrator.plane_composition.compose_plane_from_environment", compose)
    with pytest.raises(RuntimeError, match="cancelled"):
        with _CONFIGURED_SEALS(cli.repo_root()) as seals:
            from prompt_optimization.fixture import build_fixture

            seals.assert_available(build_fixture())
            raise RuntimeError("cancelled")
    assert observed["DATABASE_URL"] == "postgresql://qualification.invalid/synthetic"
    assert observed["ATTACHMENT_UPLOAD_ROOT"] == str(tmp_path.resolve() / "blobs")
    assert closed == [True]
