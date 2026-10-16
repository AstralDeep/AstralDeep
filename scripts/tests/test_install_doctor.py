"""Tests for the read-only installation doctor: outcome mapping, redaction, service
probe fakes, read-only/no-sign-in guarantees, and deterministic recovery for every
failure class it diagnoses."""

from __future__ import annotations

import importlib.util
import itertools
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "install_doctor.py"

assert SCRIPT.is_file(), "installation doctor script is required in the source checkout"

spec = importlib.util.spec_from_file_location("install_doctor_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
doctor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = doctor
spec.loader.exec_module(doctor)

HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40

COMPOSE_TEXT = """
services:
  astraldeep:
    image: astraldeep:latest
    ports:
      - "127.0.0.1:8001:8001"
    environment:
      LIVEKIT_API_KEY: ${LIVEKIT_API_KEY:?set the LiveKit API key}
      VOICE_CONTROL_SECRET: ${VOICE_CONTROL_SECRET:?set the voice-control secret}
  postgres:
    image: postgres:17-alpine
  livekit:
    image: livekit/livekit-server:v1
    environment:
      NODE_IP: ${LIVEKIT_NODE_IP:?set the Mac host LAN address advertised for WebRTC media}
    ports:
      - "7880:7880"
  voice-worker:
    environment:
      VOICE_SPEECH_BASE_URL: ${OPENAI_BASE_URL:?set the operator speech base URL}
      VOICE_SPEECH_API_KEY: ${OPENAI_API_KEY:?set the operator speech API key}
      VOICE_CONTROL_SECRET: ${VOICE_CONTROL_SECRET:?set the voice-control secret}
"""

DEV_ENV = {
    "ASTRAL_ENV": "development",
    "USE_MOCK_AUTH": "true",
    "DB_PASSWORD": "astral_dev",
    "DB_USER": "astral",
    "DB_NAME": "astraldeep",
    "LIVEKIT_API_KEY": "devlivekitkey",
    "LIVEKIT_API_SECRET": "devlivekitsecret-value",
    "LIVEKIT_NODE_IP": "192.168.1.50",
    "VOICE_CONTROL_SECRET": "dev-voice-control-secret",
    "OPENAI_BASE_URL": "http://127.0.0.1:9999/v1",
    "OPENAI_API_KEY": "sk-test-doctor-0000000",
}

PRODUCTION_ENV = {
    "ASTRAL_ENV": "production",
    "USE_MOCK_AUTH": "false",
    "DB_PASSWORD": "pg-password-value",
    "WEB_SESSION_ENC_KEY": "x" * 44,
    "CREDENTIAL_ENCRYPTION_KEY": "y" * 44,
    "AUDIT_HMAC_SECRET": "high-entropy-audit-secret-value",
    "AGENT_API_KEY": "high-entropy-agent-key-value",
    "KEYCLOAK_AUTHORITY": "http://127.0.0.1:8080/realms/Astral",
    "KEYCLOAK_CLIENT_ID": "astral-frontend",
    "KEYCLOAK_CLIENT_SECRET": "high-entropy-client-secret-value",
    "LIVEKIT_API_KEY": "prodlivekitkey",
    "LIVEKIT_API_SECRET": "prodlivekitsecret-value",
    "LIVEKIT_NODE_IP": "192.168.1.50",
    "VOICE_CONTROL_SECRET": "prod-voice-control-secret",
    "OPENAI_BASE_URL": "https://speech.example/v1",
    "OPENAI_API_KEY": "sk-prod-doctor-0000000",
}

ALLOWED_COMMAND_PREFIXES = (
    ("docker", "--version"),
    ("docker", "compose", "version"),
    ("docker", "compose", "config", "--services"),
    ("docker", "compose", "ps", "--all", "--format", "json"),
    ("docker", "inspect", "--format", "{{json .Config.Labels}}"),
    ("git", "rev-parse", "HEAD"),
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_checkout_sequence = itertools.count()


def make_checkout(tmp_path: Path, *, env: dict[str, str] | None = None) -> Path:
    root = tmp_path / f"AstralDeep-{next(_checkout_sequence)}"
    root.mkdir(parents=True)
    _write(root / "pyproject.toml", '[project]\nname = "astraldeep"\n')
    _write(root / "docker-compose.yml", COMPOSE_TEXT)
    _write(root / ".env.example", "ASTRAL_ENV=development\n")
    if env is not None:
        _write(
            root / ".env",
            "".join(f"{name}={value}\n" for name, value in env.items()),
        )
    return root


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd, timeout=None):
        self.calls.append(list(argv))
        response = self.responses.get(tuple(argv))
        if response is None:
            return doctor.CommandResult(1, "", f"unexpected command: {argv}")
        return doctor.CommandResult(*response)


def running_services(head_revision: str = HEAD_SHA) -> tuple[FakeRunner, list[dict]]:
    app_publisher = {
        "URL": "127.0.0.1",
        "TargetPort": 8001,
        "PublishedPort": 8001,
        "Protocol": "tcp",
    }
    livekit_publisher = {
        "URL": "127.0.0.1",
        "TargetPort": 7880,
        "PublishedPort": 7880,
        "Protocol": "tcp",
    }
    entries = [
        {
            "ID": "app-container-id",
            "Name": "proj-astraldeep-1",
            "Service": "astraldeep",
            "State": "running",
            "Health": "healthy",
            "ExitCode": 0,
            "Publishers": [app_publisher],
        },
        {
            "ID": "pg-container-id",
            "Name": "proj-postgres-1",
            "Service": "postgres",
            "State": "running",
            "Health": "healthy",
            "ExitCode": 0,
            "Publishers": [],
        },
        {
            "ID": "livekit-container-id",
            "Name": "proj-livekit-1",
            "Service": "livekit",
            "State": "running",
            "Health": "",
            "ExitCode": 0,
            "Publishers": [livekit_publisher],
        },
        {
            "ID": "worker-container-id",
            "Name": "proj-voice-worker-1",
            "Service": "voice-worker",
            "State": "running",
            "Health": "",
            "ExitCode": 0,
            "Publishers": [],
        },
    ]
    runner = FakeRunner(
        {
            ("docker", "--version"): (0, "Docker version 27.0.0", ""),
            ("docker", "compose", "version"): (0, "Docker Compose version v2.32.0", ""),
            ("docker", "compose", "config", "--services"): (
                0,
                "astraldeep\nlivekit\npostgres\nvoice-worker\n",
                "",
            ),
            ("docker", "compose", "ps", "--all", "--format", "json"): (
                0,
                json.dumps(entries),
                "",
            ),
            ("docker", "inspect", "--format", "{{json .Config.Labels}}", "app-container-id"): (
                0,
                json.dumps({"org.opencontainers.image.revision": head_revision}),
                "",
            ),
            ("git", "rev-parse", "HEAD"): (0, head_revision + "\n", ""),
        }
    )
    return runner, entries


class FakeHttp:
    def __init__(self, responses: dict[str, tuple[int, bytes]]):
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> doctor.HttpResult:
        self.urls.append(url)
        status, body = self.responses.get(url, (0, b"connection refused"))
        return doctor.HttpResult(status, body)


def ok_http() -> FakeHttp:
    return FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b'{"status":"ok"}'),
            "http://127.0.0.1:8001/readyz": (200, b'{"status":"ok"}'),
            "http://127.0.0.1:8080/realms/Astral/.well-known/openid-configuration": (
                200,
                json.dumps(
                    {"issuer": "http://127.0.0.1:8080/realms/Astral"}
                ).encode(),
            ),
        }
    )


def ok_tcp() -> object:
    return lambda host, port, timeout: True


def ok_verifier():
    def verify(root):
        return SimpleNamespace(ok=True, diagnostics=())

    return verify


def ok_contract():
    return lambda root: None


def collect(
    root,
    *,
    env=None,
    runner=None,
    http=None,
    tcp=None,
    verifier=None,
    contract=None,
    environ=None,
):
    return doctor.collect(
        root,
        environ=environ or {},
        command_runner=runner or running_services()[0],
        http_probe=http or ok_http(),
        tcp_probe=tcp or ok_tcp(),
        composition_verifier=verifier or ok_verifier(),
        contract_validator=contract or ok_contract(),
    )


def problem_codes(report) -> list[str]:
    return [finding.code for finding in report.findings if finding.severity == "problem"]


def sensitive_values(env: dict[str, str]) -> list[str]:
    return [
        value
        for name, value in env.items()
        if doctor.SENSITIVE_KEY.search(name) and len(value) >= 8
    ]


def assert_no_secrets(report, env: dict[str, str]) -> None:
    rendered = doctor.render_text(report) + json.dumps(report.to_dict())
    for value in sensitive_values(env):
        assert value not in rendered


def test_ready_outcome_for_complete_install(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    http = ok_http()
    report = collect(root, env=DEV_ENV, runner=runner, http=http)
    assert report.outcome == "ready"
    assert report.exit_code == 0
    assert problem_codes(report) == []
    assert sorted(url.rsplit("/", 1)[-1] for url in http.urls) == ["healthz", "readyz"]
    for call in runner.calls:
        assert any(tuple(call[: len(prefix)]) == prefix for prefix in ALLOWED_COMMAND_PREFIXES)
    assert_no_secrets(report, DEV_ENV)


def test_report_json_payload_shape(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    report = collect(root, env=DEV_ENV)
    document = report.to_dict()
    assert document["contract"] == "astraldeep.install-doctor/v1"
    assert document["outcome"] == "ready"
    assert document["root"] == str(root)
    assert document["env_file"] == str(root / ".env")
    assert document["platform"]
    assert document["python"].startswith("3.")
    assert document["findings"] == []


def test_python_older_than_311_is_unsupported(tmp_path, monkeypatch):
    root = make_checkout(tmp_path)
    monkeypatch.setattr(doctor, "_python_version", lambda: (3, 10, 4))
    report = collect(root)
    assert report.outcome == "unsupported"
    assert report.exit_code == 2
    assert problem_codes(report) == ["host.python-too-old"]
    assert report.env_file is None


def test_missing_checkout_artifacts_are_unsupported(tmp_path):
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()
    report = collect(empty)
    assert report.outcome == "unsupported"
    assert problem_codes(report) == ["host.checkout-missing"]
    assert "pyproject.toml" in report.findings[0].message


def test_missing_git_is_repair_and_skips_services(tmp_path, monkeypatch):
    root = make_checkout(tmp_path, env=DEV_ENV)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    report = collect(root, env=DEV_ENV)
    assert report.outcome == "repair"
    assert "host.git-missing" in problem_codes(report)
    assert any(
        finding.check == "services.skipped" and "git" in finding.message
        for finding in report.findings
    )


def test_missing_docker_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner = FakeRunner({})
    report = collect(root, env=DEV_ENV, runner=runner)
    assert report.outcome == "repair"
    assert "host.docker-missing" in problem_codes(report)
    assert any(
        finding.check == "services.skipped" and "Docker Compose" in finding.message
        for finding in report.findings
    )


def test_missing_compose_plugin_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner = FakeRunner(
        {
            ("docker", "--version"): (0, "Docker version 27.0.0", ""),
            ("docker", "compose", "version"): (1, "", "docker: 'compose' is not a docker command."),
        }
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    assert "host.compose-missing" in problem_codes(report)
    assert report.outcome == "repair"


def test_missing_env_file_is_repair_with_example_remediation(tmp_path):
    root = make_checkout(tmp_path)
    report = collect(root)
    assert report.outcome == "repair"
    finding = next(f for f in report.findings if f.code == "configuration.env-file-missing")
    assert ".env.example" in finding.remediation
    assert any(finding.check == "services.skipped" for finding in report.findings)


def test_malformed_env_line_is_repair(tmp_path):
    root = make_checkout(tmp_path)
    _write(root / ".env", "ASTRAL_ENV=development\nthis line has no equals\n")
    report = collect(root)
    assert report.outcome == "repair"
    assert "configuration.env-file-invalid" in problem_codes(report)


def test_env_parsing_supports_quotes_comments_and_export(tmp_path):
    path = tmp_path / ".env"
    _write(
        path,
        "".join(
            [
                "# comment\n",
                "\n",
                "PLAIN=value\n",
                "QUOTED=\"quoted value\"\n",
                "SINGLE='single value'\n",
                "COMMENTED=value # trailing comment\n",
                "HASHED=a#b\n",
                "EMPTY=\n",
                "export EXPORTED=exported-value\n",
                "BAD LINE\n",
            ]
        ),
    )
    values, problems = doctor.parse_env_file(path)
    assert values == {
        "PLAIN": "value",
        "QUOTED": "quoted value",
        "SINGLE": "single value",
        "COMMENTED": "value",
        "HASHED": "a#b",
        "EMPTY": "",
        "EXPORTED": "exported-value",
    }
    assert len(problems) == 1 and "line 10" in problems[0]


def test_required_compose_variables_are_extracted(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    _write(compose, COMPOSE_TEXT)
    required, error = doctor.required_compose_variables(compose)
    assert error is None
    assert dict(required) == {
        "LIVEKIT_API_KEY": "set the LiveKit API key",
        "VOICE_CONTROL_SECRET": "set the voice-control secret",
        "LIVEKIT_NODE_IP": "set the Mac host LAN address advertised for WebRTC media",
        "OPENAI_BASE_URL": "set the operator speech base URL",
        "OPENAI_API_KEY": "set the operator speech API key",
    }


def test_missing_required_variable_blocks_ready_and_skips_services(tmp_path):
    env = dict(DEV_ENV)
    del env["LIVEKIT_NODE_IP"]
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env)
    assert report.outcome == "repair"
    finding = next(
        f for f in report.findings if f.code == "configuration.compose-variable-missing"
    )
    assert "LIVEKIT_NODE_IP" in finding.message
    assert "advertised for WebRTC media" in finding.message
    assert any(finding.check == "services.skipped" for finding in report.findings)


def test_shell_environment_overrides_env_file_for_interpolation(tmp_path):
    env = dict(DEV_ENV)
    del env["LIVEKIT_NODE_IP"]
    root = make_checkout(tmp_path, env=env)
    report = collect(
        root,
        env=env,
        runner=running_services()[0],
        environ={"LIVEKIT_NODE_IP": "10.1.2.3"},
    )
    assert problem_codes(report) == []
    assert report.outcome == "ready"


def test_empty_db_password_is_advisory_only(tmp_path):
    env = dict(DEV_ENV)
    env["DB_PASSWORD"] = ""
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env, runner=running_services()[0])
    advisory = next(
        f for f in report.findings if f.code == "configuration.postgres-init"
    )
    assert advisory.severity == "advisory"
    assert report.outcome == "ready"


def test_production_with_missing_secrets_is_repair_and_redacted(tmp_path):
    env = dict(PRODUCTION_ENV)
    env.update(
        {
            "ASTRAL_ENV": "production",
            "USE_MOCK_AUTH": "false",
            "WEB_SESSION_ENC_KEY": "",
            "AUDIT_HMAC_SECRET": "change-me",
            "KEYCLOAK_AUTHORITY": "",
            "KEYCLOAK_CLIENT_ID": "",
            "KEYCLOAK_CLIENT_SECRET": "",
        }
    )
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env)
    assert report.outcome == "repair"
    for name in (
        "WEB_SESSION_ENC_KEY",
        "AUDIT_HMAC_SECRET",
        "KEYCLOAK_AUTHORITY",
        "KEYCLOAK_CLIENT_ID",
        "KEYCLOAK_CLIENT_SECRET",
    ):
        assert any(name in finding.message for finding in report.findings)
    assert not any(
        finding.code == "configuration.compose-variable-missing" for finding in report.findings
    )
    assert_no_secrets(report, env)


def test_production_ready_with_services_and_keycloak(tmp_path):
    root = make_checkout(tmp_path, env=PRODUCTION_ENV)
    runner, _ = running_services()
    report = collect(root, env=PRODUCTION_ENV, runner=runner)
    assert report.outcome == "ready"
    assert problem_codes(report) == []
    assert_no_secrets(report, PRODUCTION_ENV)


def test_keycloak_discovery_failures_are_repair(tmp_path):
    env = dict(PRODUCTION_ENV)
    root = make_checkout(tmp_path, env=env)
    runner, _ = running_services()
    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (200, b"ok"),
            "http://127.0.0.1:8080/realms/Astral/.well-known/openid-configuration": (
                404,
                b"realm not found",
            ),
        }
    )
    report = collect(root, env=env, runner=runner, http=http)
    assert "services.keycloak-discovery-failed" in problem_codes(report)

    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (200, b"ok"),
        }
    )
    report = collect(root, env=env, runner=runner, http=http)
    assert "services.keycloak-unreachable" in problem_codes(report)

    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (200, b"ok"),
            "http://127.0.0.1:8080/realms/Astral/.well-known/openid-configuration": (
                200,
                json.dumps({"issuer": "http://127.0.0.1:8080/realms/Other"}).encode(),
            ),
        }
    )
    report = collect(root, env=env, runner=runner, http=http)
    assert "services.keycloak-issuer-mismatch" in problem_codes(report)

    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (200, b"ok"),
            "http://127.0.0.1:8080/realms/Astral/.well-known/openid-configuration": (
                200,
                b"not json",
            ),
        }
    )
    report = collect(root, env=env, runner=runner, http=http)
    assert "services.keycloak-discovery-invalid" in problem_codes(report)


def test_mock_off_without_authority_is_problem_even_in_development(tmp_path):
    env = dict(DEV_ENV)
    env["USE_MOCK_AUTH"] = "false"
    env.pop("KEYCLOAK_AUTHORITY", None)
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env, runner=running_services()[0])
    assert "services.keycloak-unconfigured" in problem_codes(report)
    assert report.outcome == "repair"


def test_dev_mock_auth_skips_keycloak_probe(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (200, b"ok"),
        }
    )
    report = collect(root, env=DEV_ENV, runner=running_services()[0], http=http)
    assert report.outcome == "ready"
    assert all(":8080" not in url for url in http.urls)


def test_typesafe_environment_is_advisory_in_dev_and_problem_in_production(tmp_path):
    env = dict(DEV_ENV)
    env["TYPESAFE_API_KEY"] = "ts-key-value-0001"
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env, runner=running_services()[0])
    advisory = next(
        f for f in report.findings if f.code == "configuration.posture-advisory"
    )
    assert advisory.severity == "advisory"
    assert report.outcome == "ready"

    env = dict(PRODUCTION_ENV)
    env["TYPESAFE_API_KEY"] = "ts-key-value-0001"
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env, runner=running_services()[0])
    assert "configuration.posture" in problem_codes(report)
    assert report.outcome == "repair"


def test_production_agent_key_advisories(tmp_path):
    env = dict(PRODUCTION_ENV)
    del env["AGENT_API_KEY"]
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env, runner=running_services()[0])
    assert any(
        f.code == "configuration.posture-advisory" and "AGENT_API_KEY" in f.message
        for f in report.findings
    )
    assert report.outcome == "ready"

    env["AGENT_API_KEY"] = "change-me"
    root = make_checkout(tmp_path, env=env)
    report = collect(root, env=env, runner=running_services()[0])
    assert "configuration.posture" in problem_codes(report)
    assert report.outcome == "repair"


def test_compose_failure_stderr_is_redacted(tmp_path):
    env = dict(PRODUCTION_ENV)
    root = make_checkout(tmp_path, env=env)
    runner, _ = running_services()
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        1,
        "",
        "cannot connect: prodlivekitsecret-value was here",
    )
    report = collect(root, env=env, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.compose-state-failed")
    assert "[redacted]" in finding.message
    assert "prodlivekitsecret-value" not in doctor.render_text(report)


def test_exited_service_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "postgres":
            entry["State"] = "exited"
            entry["ExitCode"] = 1
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.service-not-running")
    assert "postgres" in finding.message and "exited" in finding.message
    assert report.outcome == "repair"


def test_unhealthy_service_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "astraldeep":
            entry["Health"] = "unhealthy"
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.service-unhealthy")
    assert "astraldeep" in finding.message and "unhealthy" in finding.message
    assert report.outcome == "repair"


def test_no_ready_when_required_service_has_no_container(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, entries = running_services()
    remaining = [entry for entry in entries if entry["Service"] != "livekit"]
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(remaining),
        "",
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.service-missing")
    assert "livekit" in finding.message
    assert report.outcome == "repair"
    assert report.exit_code == 1


def test_compose_ps_json_lines_are_parsed(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, entries = running_services()
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        "\n".join(json.dumps(entry) for entry in entries),
        "",
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    assert report.outcome == "ready"


def test_unparseable_compose_ps_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        "not json at all\n{broken",
        "",
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    assert "services.compose-state-unparseable" in problem_codes(report)


def test_empty_service_list_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    runner.responses[("docker", "compose", "config", "--services")] = (0, "", "")
    report = collect(root, env=DEV_ENV, runner=runner)
    assert "services.compose-config-empty" in problem_codes(report)


def test_compose_config_failure_is_redacted_and_repair(tmp_path):
    env = dict(PRODUCTION_ENV)
    root = make_checkout(tmp_path, env=env)
    runner, _ = running_services()
    runner.responses[("docker", "compose", "config", "--services")] = (
        1,
        "",
        "validation error near prod-voice-control-secret",
    )
    report = collect(root, env=env, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.compose-config-failed")
    assert "[redacted]" in finding.message
    assert report.outcome == "repair"


def test_app_http_probes(tmp_path):
    env = dict(DEV_ENV)
    root = make_checkout(tmp_path, env=env)
    runner, _ = running_services()

    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (503, b"postgres unavailable"),
        }
    )
    report = collect(root, env=env, runner=runner, http=http)
    finding = next(f for f in report.findings if f.code == "services.app-not-ready")
    assert "503" in finding.message
    assert report.outcome == "repair"

    http = FakeHttp({"http://127.0.0.1:8001/healthz": (0, b"refused")})
    report = collect(root, env=env, runner=runner, http=http)
    finding = next(f for f in report.findings if f.code == "services.app-not-live")
    assert "healthz" in finding.message
    assert report.outcome == "repair"

    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "astraldeep":
            entry["Publishers"] = []
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    report = collect(root, env=env, runner=runner)
    assert "services.app-port-unpublished" in problem_codes(report)

    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "astraldeep":
            entry["Publishers"] = [
                {"URL": "127.0.0.1", "TargetPort": 9999, "PublishedPort": 9999, "Protocol": "tcp"}
            ]
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    report = collect(root, env=env, runner=runner)
    assert "services.app-port-unpublished" in problem_codes(report)


def test_app_http_skipped_when_container_not_running(tmp_path):
    env = dict(DEV_ENV)
    root = make_checkout(tmp_path, env=env)
    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "astraldeep":
            entry["State"] = "created"
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    http = FakeHttp({})
    report = collect(root, env=env, runner=runner, http=http)
    assert "services.service-not-running" in problem_codes(report)
    assert http.urls == []


def test_stale_image_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services(head_revision=HEAD_SHA)
    runner.responses[("git", "rev-parse", "HEAD")] = (0, OTHER_SHA + "\n", "")
    report = collect(root, env=DEV_ENV, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.image-stale")
    assert OTHER_SHA[:12] in finding.message and HEAD_SHA[:12] in finding.message
    assert report.outcome == "repair"


def test_image_without_revision_label_requires_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    runner.responses[("docker", "inspect", "--format", "{{json .Config.Labels}}", "app-container-id")] = (
        0,
        "{}",
        "",
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.image-unverified")
    assert finding.severity == "problem"
    assert report.outcome == "repair"


def test_livekit_tcp_probes(tmp_path):
    env = dict(DEV_ENV)
    root = make_checkout(tmp_path, env=env)
    runner, _ = running_services()
    report = collect(root, env=env, runner=runner, tcp=lambda host, port, timeout: True)
    assert problem_codes(report) == []

    report = collect(root, env=env, runner=running_services()[0], tcp=lambda host, port, timeout: False)
    finding = next(f for f in report.findings if f.code == "services.livekit-unreachable")
    assert "7880" in finding.message
    assert report.outcome == "repair"

    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "livekit":
            entry["Publishers"] = []
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    report = collect(root, env=env, runner=runner)
    advisory = next(f for f in report.findings if f.code == "services.livekit-port-unpublished")
    assert advisory.severity == "advisory"
    assert report.outcome == "ready"


def test_composition_diagnostics_become_repair_findings(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)

    def verifier(target):
        return SimpleNamespace(
            ok=False,
            diagnostics=(
                SimpleNamespace(
                    code="E_PIN",
                    component="astral-plane",
                    message="component astral-plane is not at its pinned commit",
                    remediation="run git submodule update --init --recursive",
                ),
            ),
        )

    report = collect(root, env=DEV_ENV, verifier=verifier)
    finding = next(
        f for f in report.findings if f.check == "repository.composition"
    )
    assert finding.severity == "problem"
    assert "not at its pinned commit" in finding.message
    assert "git submodule update" in finding.remediation
    assert report.outcome == "repair"


def test_composition_verifier_crash_is_contained(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)

    def verifier(target):
        raise OSError("verify_composition.py missing")

    report = collect(root, env=DEV_ENV, verifier=verifier)
    finding = next(
        f for f in report.findings if f.code == "repository.composition-error"
    )
    assert "OSError" in finding.message
    assert report.outcome == "repair"


def test_component_contract_failure_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)

    def validator(target):
        raise RuntimeError("component astral-plane is not initialized")

    report = collect(root, env=DEV_ENV, contract=validator)
    finding = next(
        f for f in report.findings if f.check == "repository.component-contract"
    )
    assert "RuntimeError" in finding.message
    assert "not initialized" not in finding.message
    assert report.outcome == "repair"


def test_doctor_mutates_nothing(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    snapshot_before = {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    collect(root, env=DEV_ENV, runner=runner)
    snapshot_after = {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    assert snapshot_after == snapshot_before
    assert list((root / ".env").read_text(encoding="utf-8").splitlines()) == [
        f"{name}={value}" for name, value in DEV_ENV.items()
    ]


def test_real_http_get_is_get_without_credentials_or_signin():
    observed: list[tuple[str, str, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.command, self.path, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"issuer":"x"}')

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = doctor._http_get(f"http://127.0.0.1:{server.server_port}/readyz", 5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.status == 200
    assert observed == [("GET", "/readyz", observed[0][2])]
    assert "authorization" not in observed[0][2]
    assert "cookie" not in observed[0][2]


def test_published_port_parsing_rules():
    entry = {
        "Publishers": [
            {"URL": "", "TargetPort": 8001, "PublishedPort": 8001, "Protocol": "tcp"},
        ]
    }
    assert doctor._published_port(entry, 8001) == ("127.0.0.1", 8001)
    entry = {
        "Publishers": [
            {"URL": "192.168.1.5", "TargetPort": 8001, "PublishedPort": 8001, "Protocol": "udp"},
            {"URL": "0.0.0.0", "TargetPort": 8001, "PublishedPort": 0, "Protocol": "tcp"},
            {"URL": "::", "TargetPort": 8001, "PublishedPort": 8001},
        ]
    }
    assert doctor._published_port(entry, 8001) == ("127.0.0.1", 8001)
    assert doctor._published_port({"Publishers": []}, 8001) is None
    assert doctor._published_port({}, 8001) is None


def test_parse_compose_ps_accepts_array_lines_and_rejects_garbage():
    entries = [{"Service": "astraldeep", "State": "running"}]
    assert doctor._parse_compose_ps(json.dumps(entries)) == entries
    assert doctor._parse_compose_ps("\n".join(json.dumps(item) for item in entries)) == entries
    assert doctor._parse_compose_ps("") == []
    assert doctor._parse_compose_ps("garbage {") is None


def test_redaction_covers_sensitive_values_only():
    values = {
        "LIVEKIT_API_SECRET": "sentinel-secret-value-1",
        "DB_USER": "short",
        "DB_PASSWORD": "sentinel-password-value",
        "NOTE": "sentinel-not-sensitive-value",
    }
    text = "leak sentinel-secret-value-1 and sentinel-password-value and sentinel-not-sensitive-value"
    redacted = doctor._redact(text, values)
    assert "sentinel-secret-value-1" not in redacted
    assert "sentinel-password-value" not in redacted
    assert "sentinel-not-sensitive-value" in redacted
    assert redacted.count("[redacted]") == 2


def test_recovery_after_environment_fix(tmp_path):
    env = dict(DEV_ENV)
    del env["OPENAI_API_KEY"]
    root = make_checkout(tmp_path, env=env)
    first = collect(root, env=env, runner=running_services()[0])
    assert first.outcome == "repair"
    env["OPENAI_API_KEY"] = "sk-test-doctor-0000000"
    _write(root / ".env", "".join(f"{k}={v}\n" for k, v in env.items()))
    second = collect(root, env=env, runner=running_services()[0])
    assert second.outcome == "ready"


def test_recovery_after_service_restart(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "voice-worker":
            entry["State"] = "exited"
            entry["ExitCode"] = 1
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    first = collect(root, env=DEV_ENV, runner=runner)
    assert first.outcome == "repair"
    runner, entries = running_services()
    second = collect(root, env=DEV_ENV, runner=runner)
    assert second.outcome == "ready"


def test_recovery_after_readiness_and_composition_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (503, b"starting"),
        }
    )
    first = collect(root, env=DEV_ENV, runner=running_services()[0], http=http)
    assert first.outcome == "repair"

    def verifier(target):
        return SimpleNamespace(
            ok=False,
            diagnostics=(
                SimpleNamespace(
                    code="E_DIRTY", component=None, message="component worktree dirty", remediation="clean"
                ),
            ),
        )

    second = collect(root, env=DEV_ENV, runner=running_services()[0], verifier=verifier)
    assert second.outcome == "repair"
    third = collect(root, env=DEV_ENV, runner=running_services()[0])
    assert third.outcome == "ready"


def test_real_checkout_composition_and_contract_pass():
    report = doctor._composition_findings(
        REPOSITORY_ROOT, doctor._default_composition_verifier
    )
    assert report == []
    doctor._default_contract_validator(REPOSITORY_ROOT)


def test_cli_reports_json_and_repair_exit_code(tmp_path, capsys):
    root = make_checkout(tmp_path)
    exit_code = doctor.main(["--json", "--root", str(root)])
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["contract"] == "astraldeep.install-doctor/v1"
    assert document["outcome"] == "repair"
    assert exit_code == 1


def test_cli_text_output_mentions_remediation(tmp_path, capsys):
    root = make_checkout(tmp_path)
    exit_code = doctor.main(["--root", str(root)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "outcome: repair" in captured.out
    assert "remediation:" in captured.out
    assert ".env.example" in captured.out


def test_run_command_timeout_and_missing_binary(tmp_path):
    result = doctor._run_command([sys.executable, "-c", "print('ready')"], tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "ready"
    result = doctor._run_command([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, 0.2)
    assert result.returncode == 124
    assert "timed out" in result.stderr
    result = doctor._run_command(["definitely-not-a-real-binary-xyz"], tmp_path)
    assert result.returncode == 127


def test_http_get_handles_error_status_refused_and_bad_target():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"nope")

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        error_result = doctor._http_get(f"http://127.0.0.1:{server.server_port}/x", 5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert error_result.status == 404
    assert error_result.reason == "connection or protocol failure"

    refused = doctor._http_get("http://127.0.0.1:1/healthz", 0.5)
    assert refused.status == 0
    assert refused.reason
    out_of_range = doctor._http_get("http://127.0.0.1:99999/healthz", 5.0)
    assert out_of_range.status == 0


def test_tcp_reachable_false_for_closed_port():
    assert doctor._tcp_reachable("127.0.0.1", 1, 0.5) is False


def test_tcp_reachable_true_for_listening_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        assert doctor._tcp_reachable("127.0.0.1", server.getsockname()[1], 5.0) is True
    finally:
        server.close()


def test_missing_env_file_on_disk_is_reported(tmp_path):
    values, problems = doctor.parse_env_file(tmp_path / "absent.env")
    assert values == {}
    assert problems and "absent.env" in problems[0]


def test_unreadable_compose_file_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    (root / "docker-compose.yml").chmod(0)
    try:
        report = collect(root, env=DEV_ENV)
    finally:
        (root / "docker-compose.yml").chmod(0o644)
    assert "configuration.compose-unreadable" in problem_codes(report)
    assert report.outcome == "repair"


def test_compose_ps_line_garbage_and_blank_lines():
    text = "\n{\"Service\": \"a\", \"State\": \"running\"}\n\nnot-json\n"
    assert doctor._parse_compose_ps(text) is None


def test_published_port_ignores_malformed_publishers():
    assert doctor._published_port({"Publishers": "bogus"}, 8001) is None
    assert (
        doctor._published_port({"Publishers": ["bogus", 5, None]}, 8001) is None
    )


def test_readyz_other_error_status_is_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    http = FakeHttp(
        {
            "http://127.0.0.1:8001/healthz": (200, b"ok"),
            "http://127.0.0.1:8001/readyz": (500, b"boom"),
        }
    )
    report = collect(root, env=DEV_ENV, runner=running_services()[0], http=http)
    assert "services.app-readyz-failed" in problem_codes(report)
    assert report.outcome == "repair"


def test_image_inspect_garbage_requires_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    runner.responses[
        ("docker", "inspect", "--format", "{{json .Config.Labels}}", "app-container-id")
    ] = (0, "not json", "")
    report = collect(root, env=DEV_ENV, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.image-unverified")
    assert finding.severity == "problem"
    assert report.outcome == "repair"


def test_git_head_failure_with_labeled_image_requires_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services(head_revision=HEAD_SHA)
    runner.responses[("git", "rev-parse", "HEAD")] = (128, "", "fatal: not a git repository")
    report = collect(root, env=DEV_ENV, runner=runner)
    assert report.outcome == "repair"
    assert "services.image-unverified" in problem_codes(report)
    assert report.outcome == "repair"


def test_running_app_without_container_id_requires_repair(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, entries = running_services()
    for entry in entries:
        if entry["Service"] == "astraldeep":
            entry["ID"] = ""
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (
        0,
        json.dumps(entries),
        "",
    )
    report = collect(root, env=DEV_ENV, runner=runner)
    finding = next(f for f in report.findings if f.code == "services.image-unverified")
    assert finding.severity == "problem"
    assert report.outcome == "repair"


def test_render_text_for_unsupported_report_has_no_environment_line(tmp_path):
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()
    report = collect(empty)
    rendered = doctor.render_text(report)
    assert "outcome: unsupported" in rendered
    assert "environment:" not in rendered
    assert "host.checkout-missing" in rendered


def test_render_text_for_ready_report_mentions_no_findings(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    report = collect(root, env=DEV_ENV, runner=running_services()[0])
    rendered = doctor.render_text(report)
    assert "no findings" in rendered


@pytest.mark.parametrize("document", ['[1]', '{}\n[]', '[null]', '{"Service":"astraldeep"}\ntrue'])
def test_malformed_service_records_are_repair(tmp_path, document):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (0, document, "")
    report = collect(root, env=DEV_ENV, runner=runner)
    assert report.outcome == "repair"
    assert "services.compose-state-unparseable" in problem_codes(report)


def test_duplicate_unhealthy_service_cannot_be_hidden_by_healthy_record(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, entries = running_services()
    entries.insert(0, {**entries[0], "ID": "failed-container", "State": "exited", "ExitCode": 78})
    runner.responses[("docker", "compose", "ps", "--all", "--format", "json")] = (0, json.dumps(entries), "")
    report = collect(root, env=DEV_ENV, runner=runner)
    assert report.outcome == "repair"
    assert "services.service-duplicate" in problem_codes(report)


@pytest.mark.parametrize("document", [b'[]', b'null', b'7'])
def test_non_object_oidc_document_requires_repair(tmp_path, document):
    root = make_checkout(tmp_path, env=PRODUCTION_ENV)
    http = ok_http()
    http.responses[PRODUCTION_ENV["KEYCLOAK_AUTHORITY"] + doctor.DISCOVERY_SUFFIX] = (200, document)
    report = collect(root, env=PRODUCTION_ENV, http=http)
    assert report.outcome == "repair"
    assert "services.keycloak-discovery-invalid" in problem_codes(report)


@pytest.mark.parametrize("document", ['[]', 'null', '7'])
def test_non_object_image_labels_require_repair(tmp_path, document):
    root = make_checkout(tmp_path, env=DEV_ENV)
    runner, _ = running_services()
    runner.responses[("docker", "inspect", "--format", "{{json .Config.Labels}}", "app-container-id")] = (0, document, "")
    report = collect(root, env=DEV_ENV, runner=runner)
    assert report.outcome == "repair"
    assert "services.image-unverified" in problem_codes(report)


def test_transport_diagnostics_never_emit_server_supplied_secrets(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    http = ok_http()
    sensitive = "synthetic-private-diagnostic"
    http.responses["http://127.0.0.1:8001/healthz"] = (0, sensitive.encode())
    report = collect(root, env=DEV_ENV, http=http)
    assert report.outcome == "repair"
    assert sensitive not in doctor.render_text(report)
    assert sensitive not in json.dumps(report.to_dict())


def test_short_sensitive_values_are_redacted_before_output():
    assert doctor._redact("synthetic short q7 value", {"CLIENT_SECRET": "q7"}) == "synthetic short [redacted] value"


def test_alternate_env_file_cannot_claim_the_default_compose_install_is_ready(tmp_path):
    root = make_checkout(tmp_path, env=DEV_ENV)
    alternate = root / "unrelated.env"
    _write(alternate, "".join(f"{k}={v}\n" for k, v in DEV_ENV.items()))
    report = doctor.collect(root, env_file=alternate, environ={}, command_runner=running_services()[0],
        http_probe=ok_http(), tcp_probe=ok_tcp(), composition_verifier=lambda _: SimpleNamespace(diagnostics=()),
        contract_validator=lambda _: None)
    assert report.outcome == "repair"
    assert "configuration.env-file-unsupported" in problem_codes(report)



def test_unknown_host_platform_is_unsupported(tmp_path, monkeypatch):
    root = make_checkout(tmp_path, env=DEV_ENV)
    monkeypatch.setattr(doctor.platform, "system", lambda: "Plan9")
    report = collect(root, env=DEV_ENV)
    assert report.outcome == "unsupported"
    assert "host.platform-unsupported" in problem_codes(report)



def test_credential_encryption_key_is_redacted_before_output():
    sensitive = "synthetic-encryption-diagnostic"
    assert doctor._redact(sensitive, {"CREDENTIAL_ENCRYPTION_KEY": sensitive}) == "[redacted]"


def test_keycloak_userinfo_is_refused_without_probe_or_value_emission(tmp_path):
    env = dict(PRODUCTION_ENV)
    env["KEYCLOAK_AUTHORITY"] = "http://synthetic-user:synthetic-password@127.0.0.1:8080/realms/Astral"
    root = make_checkout(tmp_path, env=env)
    http = ok_http()
    report = collect(root, env=env, http=http)
    assert "services.keycloak-authority-invalid" in problem_codes(report)
    assert not any("synthetic-password" in url for url in http.urls)
    assert "synthetic-password" not in doctor.render_text(report)



def test_http_probe_does_not_follow_redirect_into_sign_in():
    visited = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            visited.append(self.path)
            if self.path == "/readyz":
                self.send_response(302)
                self.send_header("Location", "/sign-in")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = doctor._http_get(f"http://127.0.0.1:{server.server_port}/readyz", 5.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert visited == ["/readyz"]
    assert result.status == 302
