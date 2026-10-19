"""Tests for the clinical diagnostic CLI: frozen execution, argument bounds, and the
fail-closed public-mode opt-in/configuration refusal paths.
"""

from __future__ import annotations

import json

import pytest

from clinical_diagnostic.__main__ import arm_cancellation, main, parse_args
from clinical_diagnostic.runner import EXIT_CONFIG, EXIT_OK, PUBLIC_RUN_ENV


def test_frozen_cli_writes_artifacts_and_returns_zero(tmp_path, capsys):
    code = main(["--run-id", "cli", "--out", str(tmp_path), "--limit", "4"])
    assert code == EXIT_OK
    output = capsys.readouterr().out
    assert "scored 8" in output
    assert "clinician-reviewed cases 0" in output
    assert (tmp_path / "cli" / "clinical_diagnostic.record.json").exists()
    assert (tmp_path / "cli" / "CLINICAL_DIAGNOSTIC_REPORT.md").exists()


def test_cli_no_gate_and_single_partition(tmp_path):
    code = main([
        "--run-id", "held", "--out", str(tmp_path), "--partition", "held_out",
        "--limit", "2", "--no-gate", "--concurrency", "2",
    ])
    assert code == EXIT_OK
    payload = json.loads((tmp_path / "held" / "clinical_diagnostic.record.json").read_text())
    assert {result["partition"] for result in payload["results"]} == {"held_out"}
    assert payload["identity"]["config"]["enforce_frozen_gate"] is False


def test_cli_refuses_invalid_limits(tmp_path, capsys):
    assert main(["--run-id", "bad", "--out", str(tmp_path), "--limit", "0"]) == EXIT_CONFIG
    assert "configuration refused" in capsys.readouterr().out
    assert main(["--run-id", "bad", "--out", str(tmp_path), "--max-retries", "99"]) == EXIT_CONFIG


def test_cli_refuses_unknown_partition():
    with pytest.raises(SystemExit):
        main(["--partition", "training"])


def test_public_mode_requires_opt_in(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(PUBLIC_RUN_ENV, raising=False)
    assert main(["--mode", "public", "--run-id", "public", "--out", str(tmp_path)]) == EXIT_CONFIG
    assert "refusing public mode" in capsys.readouterr().out


def test_public_mode_requires_explicit_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(PUBLIC_RUN_ENV, "1")
    code = main(["--mode", "public", "--run-id", "public", "--out", str(tmp_path)])
    assert code == EXIT_CONFIG
    assert "requires --bare-config" in capsys.readouterr().out


def test_public_mode_refuses_missing_dispatcher_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(PUBLIC_RUN_ENV, "1")
    monkeypatch.delenv("ASTRAL_CLINICAL_DISPATCHER_TOKEN", raising=False)
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "model": "m"}))
    grader = tmp_path / "grader.json"
    grader.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "model": "g"}))
    code = main([
        "--mode", "public", "--run-id", "public", "--out", str(tmp_path),
        "--bare-config", str(bare), "--grader-config", str(grader),
        "--ws-uri", "ws://127.0.0.1:8001/ws", "--http-uri", "http://127.0.0.1:8001",
    ])
    assert code == EXIT_CONFIG
    assert "bearer token" in capsys.readouterr().out


def test_parse_args_defaults_are_bounded():
    args = parse_args([])
    assert args.mode == "frozen"
    assert args.partitions is None
    assert args.max_retries == 2
    assert args.concurrency == 1
    assert args.asset_sha256.startswith("a5286ead")

def test_public_mode_builds_bounded_producers_and_reports_infrastructure(monkeypatch, capsys):
    calls = []

    class FakeProvider:
        def __init__(self, config):
            self.config = config

    class FakeDispatcher:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    from types import SimpleNamespace

    monkeypatch.setattr("clinical_diagnostic.__main__.OpenAICompletionProvider", FakeProvider)
    monkeypatch.setattr("clinical_diagnostic.__main__.load_completion_config",
                        lambda path: {"path": path})
    monkeypatch.setattr("clinical_diagnostic.__main__.build_public_grader",
                        lambda provider: SimpleNamespace(identity="grader"))
    monkeypatch.setattr("clinical_diagnostic.__main__.build_public_producers",
                        lambda bare, dispatcher: (bare, dispatcher))
    monkeypatch.setattr("clinical_diagnostic.dispatcher.WebSocketDispatcher", FakeDispatcher)
    monkeypatch.setattr(
        "clinical_diagnostic.__main__.run",
        lambda config: SimpleNamespace(
            record=SimpleNamespace(summary={"scored": 2, "infrastructure_failures": 1,
                                            "clinician_reviewed": 0}),
            record_path="record.json", report_path="report.md",
            offenders=("candidate fixture is incomplete",), exit_code=3,
        ),
    )
    monkeypatch.setenv(PUBLIC_RUN_ENV, "1")
    monkeypatch.setenv("ASTRAL_CLINICAL_DISPATCHER_TOKEN", "token")
    code = main([
        "--mode", "public", "--run-id", "public",
        "--bare-config", "bare.json", "--grader-config", "grader.json",
        "--ws-uri", "ws://127.0.0.1:8001/ws", "--http-uri", "http://127.0.0.1:8001",
    ])
    assert code == 3
    output = capsys.readouterr().out
    assert "gate: candidate fixture is incomplete" in output
    assert "infrastructure failures are reported separately" in output
    assert calls and calls[0]["ws_uri"] == "ws://127.0.0.1:8001/ws"


def test_cli_maps_run_input_errors_to_config_exit(monkeypatch, capsys):
    def fail(_config):
        raise ValueError("bad corpus")

    monkeypatch.setattr("clinical_diagnostic.__main__.run", fail)
    assert main(["--run-id", "bad"]) == EXIT_CONFIG
    assert "run failed: bad corpus" in capsys.readouterr().out


def test_cli_returns_zero_for_a_successful_outcome_without_gate_offenders(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "clinical_diagnostic.__main__.run",
        lambda config: SimpleNamespace(
            record=SimpleNamespace(summary={}), record_path="r", report_path="p",
            offenders=(), exit_code=EXIT_OK,
        ),
    )
    assert main([]) == EXIT_OK


def test_arm_cancellation_sets_the_event(monkeypatch):
    import threading

    event = threading.Event()
    monkeypatch.setattr("clinical_diagnostic.__main__.signal.signal",
                        lambda _signal, handler: handler)
    previous = arm_cancellation(event)
    previous(None, None)
    assert event.is_set()


def test_cli_applies_a_review_overlay(tmp_path):
    from clinical_diagnostic.corpus import load_synthetic_corpus

    case = load_synthetic_corpus().cases[0]
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"reviews": [{
        "case_id": case.case_id,
        "status": "clinician_reviewed",
        "reviewer_token": "reviewer-1",
        "reviewed_at": "2026-10-20",
        "observations": [],
    }]}), encoding="utf-8")
    code = main([
        "--run-id", "reviewed", "--out", str(tmp_path), "--review-file", str(review),
        "--partition", "development", "--limit", "1",
    ])
    assert code == EXIT_OK
    payload = json.loads((tmp_path / "reviewed" / "clinical_diagnostic.record.json").read_text())
    assert payload["review_counts"]["clinician_reviewed"] == 1
    assert payload["identity"]["corpus"]["review_file_digest"]
