"""Tests for the pilot CLI: report writing and validation, profile selection, honest
exit codes, and configuration errors.
"""

from __future__ import annotations

import json

import pytest

from personalization.semantic_pilot import __main__ as cli_module
from personalization.semantic_pilot.__main__ import EXIT_CANCELLED, EXIT_CONFIG_ERROR, \
    EXIT_ERROR, EXIT_OK, main


@pytest.fixture()
def defenses_on(monkeypatch):
    monkeypatch.setenv("FF_MEMORY_TEMPORAL", "1")
    monkeypatch.setenv("FF_PROJECT_MEMORY", "1")
    monkeypatch.setenv("FF_MEMORY_GUARD", "1")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "cli-test-key")
    yield


def test_cli_stdout_report(defenses_on, capsys):
    code = main(["--profile", "offline-diagnostic"])
    assert code == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["report_kind"] == "semantic_retrieval_qualification_pilot"
    assert report["provider"]["embedding_qualification"]["state"] == \
        "executed_diagnostic_only"


def test_cli_writes_report_file(defenses_on, tmp_path, capsys):
    path = tmp_path / "reports" / "pilot.json"
    code = main(["--profile", "offline-diagnostic", "--report", str(path)])
    assert code == EXIT_OK
    output = capsys.readouterr().out
    assert "report written" in output
    report = json.loads(path.read_text())
    assert report["run"]["production_defaults_changed"] is False


def test_cli_tei_http_unavailable_writes_report(defenses_on, tmp_path):
    path = tmp_path / "tei.json"
    code = main(["--profile", "tei-http", "--report", str(path)])
    assert code == EXIT_OK
    report = json.loads(path.read_text())
    assert report["provider"]["status"]["state"] == "unavailable"
    assert all("semantic" not in result["variants"]
               for result in report["final"]["case_results"])


def test_cli_run_config_file(defenses_on, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from personalization.semantic_pilot.providers import scripted_transport

    transport = scripted_transport(dimension=384)

    def request(method, url, **kwargs):
        return SimpleNamespace(content=json.dumps(
            transport("/v1/embeddings", kwargs["json_body"])).encode())

    monkeypatch.setattr("personalization.semantic_pilot.providers.external_http.request", request)
    config_path = tmp_path / "tei-config.json"
    config_path.write_text(json.dumps({
        "base_url": "http://tei.internal:8080",
        "embed_model": "model-a",
        "embed_revision": "sha-a",
        "tokenizer_revision": "tok-a",
        "dimension": 384,
        "license": "Apache-2.0",
        "embedding_runtime_digest": "sha256:" + "a" * 64,
        "embedding_assets_digest": "sha256:" + "b" * 64,
    }))
    path = tmp_path / "tei.json"
    code = main(["--profile", "tei-http", "--run-config", str(config_path),
                 "--report", str(path)])
    assert code == EXIT_OK
    report = json.loads(path.read_text())
    assert report["provider"]["status"]["state"] == "available"
    assert report["provider"]["embedding_qualification"]["state"] == "executed_unattested"


def test_cli_missing_run_config_file_is_config_error(defenses_on, capsys):
    code = main(["--profile", "tei-http", "--run-config", "/nonexistent/config.json"])
    assert code == EXIT_CONFIG_ERROR
    assert "configuration error" in capsys.readouterr().err


def test_cli_invalid_json_config_is_config_error(defenses_on, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code = main(["--profile", "tei-http", "--run-config", str(bad)])
    assert code == EXIT_CONFIG_ERROR


def test_cli_unknown_profile_rejected(defenses_on, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--profile", "quantum-embedder"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_forced_k(defenses_on, tmp_path):
    path = tmp_path / "forced.json"
    code = main(["--profile", "offline-diagnostic", "--k", "3", "--report", str(path)])
    assert code == EXIT_OK
    report = json.loads(path.read_text())
    assert report["run"]["chosen_k"] == 3
    assert report["selection"]["decision"]["rule"] == "forced"


def test_cli_zero_k_is_config_error(defenses_on, capsys):
    code = main(["--profile", "offline-diagnostic", "--k", "0"])
    assert code == EXIT_CONFIG_ERROR
    assert "configuration error" in capsys.readouterr().err


def test_cli_cancellation_exit_code(defenses_on, monkeypatch, capsys):
    from personalization.semantic_pilot import runner as runner_module

    def cancelled_run(config, *, cancel_check=None):
        raise runner_module.RunCancelled("cancelled")

    monkeypatch.setattr(cli_module, "run_pilot", cancelled_run)
    code = main(["--profile", "offline-diagnostic"])
    assert code == EXIT_CANCELLED
    assert "cancelled" in capsys.readouterr().err


def test_cli_unexpected_failure_exit_code(defenses_on, monkeypatch, capsys):

    def broken_run(config, *, cancel_check=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "run_pilot", broken_run)
    code = main(["--profile", "offline-diagnostic"])
    assert code == EXIT_ERROR
    assert "boom" in capsys.readouterr().err


def test_cli_deadline_flag(defenses_on, tmp_path):
    path = tmp_path / "deadline.json"
    code = main(["--profile", "offline-diagnostic", "--deadline-seconds", "120",
                 "--report", str(path)])
    assert code == EXIT_OK
    report = json.loads(path.read_text())
    assert report["bounds"]["runtime_deadline_seconds"] == 120
