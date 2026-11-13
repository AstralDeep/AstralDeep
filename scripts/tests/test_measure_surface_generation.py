"""Checks that the surface diagnostic validates real trees and counts failures without leaking provider data.
Only the outbound transport is replaced; parsing, primitive validation, rate calculation and report writing run unchanged.
"""

from __future__ import annotations

import json

import pytest

from scripts.verification import measure_surface_generation as probe


def reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_invalid_outputs_and_transport_failures_stay_in_the_denominator(monkeypatch):
    secret = "private-probe-credential"
    replies = iter([
        reply('{"type":"text","content":"Hello"}'),
        reply('{"type":"text","content":"first","content":"second"}'),
        reply('{"type":"text","content":{"$bind":{"event":"missing","path":"/value"}}}'),
        reply('{"type":"text","content":"[Link](https://example.test)"}'),
        RuntimeError(secret),
    ])

    def request(*args, **kwargs):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return type("Response", (), {"json": lambda self: value})()

    monkeypatch.setattr(probe.external_http, "request", request)
    config = probe.ProviderConfig("https://models.example.test/v1", "test-model", secret)
    report = probe.measure(config, 5)
    assert (report["attempted"], report["valid"], report["valid_rate"]) == (5, 1, 0.2)
    assert [row["outcome"] for row in report["samples"]] == [
        "valid", "invalid_json", "invalid_tree", "invalid_tree", "transport_error",
    ]
    rendered = json.dumps(report)
    assert secret not in rendered and "first" not in rendered and "missing" not in rendered
    assert all(len(row["response_sha256"]) == 64 for row in report["samples"][:-1])
    assert secret not in repr(config)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "{} trailing", "null"])
def test_non_json_and_non_components_never_count_as_valid(value):
    assert probe.check_response(reply(value))["outcome"] in {"invalid_json", "invalid_tree"}


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {"content": []}}]}])
def test_provider_protocol_failures_are_distinct(payload):
    assert probe.check_response(payload) == {"outcome": "protocol_error"}


@pytest.mark.parametrize("count", [0, -1, 51])
def test_probe_sample_count_is_bounded(count):
    with pytest.raises(ValueError, match="1 to 50"):
        probe.measure(probe.ProviderConfig("https://models.example.test/v1", "m", "secret"), count)


def test_configuration_requires_explicit_file_and_safe_endpoint(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_BASE_URL=https://models.example.test/v1\nLLM_MODEL=m\nOPENAI_API_KEY=hidden\n")
    config = probe.load_config(path)
    assert (config.base_url, config.model) == ("https://models.example.test/v1", "m")
    path.write_text("OPENAI_BASE_URL=https://user:hidden@models.example.test/v1\nLLM_MODEL=m\nOPENAI_API_KEY=hidden\n")
    with pytest.raises(ValueError, match="endpoint"):
        probe.load_config(path)
    path.write_text("OPENAI_BASE_URL=https://models.example.test/v1\nLLM_MODEL=m\n")
    with pytest.raises(ValueError, match="configuration is incomplete"):
        probe.load_config(path)


def test_report_is_private_and_contains_only_the_summary(tmp_path):
    path = tmp_path / "report.json"
    probe.write_report(path, {"attempted": 2, "valid": 1})
    assert json.loads(path.read_text()) == {"attempted": 2, "valid": 1}
    assert path.stat().st_mode & 0o777 == 0o600


def test_oversized_and_deeply_nested_responses_are_refused():
    for content in ("x" * 65537, "[" * 2000 + "]" * 2000):
        assert probe.check_response(reply(content))["outcome"] == "invalid_json"


def test_cli_runs_validation_and_writes_the_count_without_credentials(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("OPENAI_BASE_URL=https://models.example.test/v1\nLLM_MODEL=m\nOPENAI_API_KEY=hidden\n")
    report = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["probe", "--env-file", str(env), "--report", str(report), "--samples", "1"])
    monkeypatch.setattr(probe.external_http, "request", lambda *args, **kwargs: type("Response", (), {
        "json": lambda self: reply('{"type":"text","content":"Hello"}'),
    })())
    assert probe.main() == 0
    assert json.loads(report.read_text())["valid"] == 1
    assert capsys.readouterr().out == "1/1 strict primitive trees valid\n"


def test_cli_failure_reports_only_the_error_class(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=private-credential\n")
    report = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["probe", "--env-file", str(env), "--report", str(report)])
    assert probe.main() == 2 and not report.exists()
    assert capsys.readouterr().err == "surface diagnostic failed: ValueError\n"
