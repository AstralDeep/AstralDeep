#!/usr/bin/env python3
"""Read-only AstralDeep installation and readiness doctor: verifies host prerequisites,
exact component composition, required configuration, and composed service health without
signing in or mutating anything, reporting redacted ready/repair/unsupported outcomes.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

CONTRACT = "astraldeep.install-doctor/v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
READY = "ready"
REPAIR = "repair"
UNSUPPORTED = "unsupported"
EXIT_CODES = {READY: 0, REPAIR: 1, UNSUPPORTED: 2}
PROBLEM = "problem"
ADVISORY = "advisory"
COMPOSE_FILE = "docker-compose.yml"
DEFAULT_ENV_NAME = ".env"
COMPOSE_COMMAND_TIMEOUT = 30.0
DEFAULT_HTTP_TIMEOUT = 5.0
APP_SERVICE = "astraldeep"
APP_TARGET_PORT = 8001
LIVEKIT_TARGET_PORT = 7880
REVISION_LABEL = "org.opencontainers.image.revision"
DEV_VALUES = ("development", "dev")
PLACEHOLDER_SECRETS = ("dev-audit-hmac-secret-change-me-in-prod", "change-me")
TYPESAFE_VARS = ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL")
MOCK_AUTH_VALUES = ("1", "true", "yes")
SENSITIVE_KEY = re.compile(
    r"secret|password|token|(?:api|enc|encryption|private)[_-]?key|hmac|authorization|credential|(?:^|_)pass$",
    re.IGNORECASE,
)
REQUIRED_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?([^}]*)\}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CHECKOUT_ARTIFACTS = ("pyproject.toml", COMPOSE_FILE)
DISCOVERY_SUFFIX = "/.well-known/openid-configuration"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    body: bytes

    @property
    def reason(self) -> str:
        return "connection or protocol failure"


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    severity: str
    code: str
    message: str
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class Report:
    contract: str
    root: str
    env_file: str | None
    platform: str
    python: str
    outcome: str
    findings: tuple[Finding, ...]

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "root": self.root,
            "env_file": self.env_file,
            "platform": self.platform,
            "python": self.python,
            "outcome": self.outcome,
            "findings": [
                {
                    "check": finding.check,
                    "severity": finding.severity,
                    "code": finding.code,
                    "message": finding.message,
                    "remediation": finding.remediation,
                }
                for finding in self.findings
            ],
        }


def _python_version() -> tuple[int, int, int]:
    return sys.version_info[:3]


def _run_command(
    argv: Sequence[str], cwd: Path, timeout: float = COMPOSE_COMMAND_TIMEOUT
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:g}s")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _http_get(url: str, timeout: float) -> HttpResult:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return HttpResult(int(response.status), response.read(65536))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65536)
        except OSError:
            body = b""
        return HttpResult(int(exc.code), body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return HttpResult(0, f"{type(exc).__name__}: {exc}".encode("utf-8", "replace"))


def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_env_file(path: Path) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, [f"could not read {path.name}: {exc}"]
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not ENV_NAME.fullmatch(name):
            problems.append(f"{path.name} line {number}: expected NAME=value")
            continue
        values[name] = _unquote(value)
    return values, problems


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return re.split(r"\s#", value, maxsplit=1)[0].strip()


def required_compose_variables(
    compose_path: Path,
) -> tuple[list[tuple[str, str]], str | None]:
    try:
        text = compose_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"could not read {COMPOSE_FILE}: {exc}"
    required: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in REQUIRED_INTERPOLATION.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        required.append((name, match.group(2).strip()))
    return required, None


def evaluate_posture(values: Mapping[str, str]) -> tuple[list[str], list[str]]:
    def raw(name: str) -> str:
        return str(values.get(name, "") or "")

    def get(name: str) -> str:
        return raw(name).strip()

    mock_on = get("USE_MOCK_AUTH").lower() in MOCK_AUTH_VALUES
    problems: list[str] = []
    advisories: list[str] = []
    typesafe = [name for name in TYPESAFE_VARS if get(name)]
    if get("ASTRAL_ENV").lower() in DEV_VALUES:
        if typesafe:
            advisories.append(
                f"{', '.join(typesafe)} set: TypeSafe routing configuration is never read "
                "from the environment; users save their own key in LLM settings."
            )
        return problems, advisories
    if typesafe:
        problems.append(
            f"{', '.join(typesafe)} set outside development: there is no deployment-wide "
            "TypeSafe credential and these variables have no effect. Unset them; users "
            "save their own key in LLM settings."
        )
    if mock_on:
        problems.append(
            "USE_MOCK_AUTH is enabled outside development: mock authentication accepts "
            "any token as an admin user. Set USE_MOCK_AUTH=false and configure "
            "KEYCLOAK_*, or set ASTRAL_ENV=development (local use only)."
        )
    if not (raw("WEB_SESSION_ENC_KEY") or raw("OFFLINE_GRANT_ENC_KEY")):
        problems.append(
            "WEB_SESSION_ENC_KEY (or OFFLINE_GRANT_ENC_KEY) is unset: durable web "
            "sessions cannot be encrypted at rest."
        )
    if not get("CREDENTIAL_ENCRYPTION_KEY"):
        problems.append(
            "CREDENTIAL_ENCRYPTION_KEY is unset: stored OAuth/Fernet credentials would "
            "be encrypted under an auto-generated key that is lost on an ephemeral "
            "volume (silent fail-open)."
        )
    audit_secret = get("AUDIT_HMAC_SECRET")
    if not audit_secret or audit_secret in PLACEHOLDER_SECRETS:
        problems.append(
            "AUDIT_HMAC_SECRET is unset or still a shipped development placeholder: the "
            "audit hash chain would be forgeable. Set a high-entropy value."
        )
    if not mock_on:
        for variable in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET"):
            if not get(variable):
                problems.append(f"{variable} is unset: the OIDC sign-in flow cannot operate.")
    agent_key = get("AGENT_API_KEY")
    if agent_key and (agent_key in PLACEHOLDER_SECRETS or len(agent_key) < 16):
        problems.append(
            "AGENT_API_KEY is a shipped placeholder or too short (under 16 characters): "
            "set a high-entropy value so agent registrations cannot be forged."
        )
    elif not agent_key and not mock_on:
        advisories.append(
            "AGENT_API_KEY is unset: agent registrations will be refused (fail closed). "
            "Configure it if this deployment should run specialist agents."
        )
    return problems, advisories


def _redact(text: str, values: Mapping[str, str]) -> str:
    redacted = text
    for name, value in values.items():
        if SENSITIVE_KEY.search(name) and value:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def _default_composition_verifier(root: Path):
    spec = importlib.util.spec_from_file_location(
        "astraldeep_doctor_verify_composition",
        REPOSITORY_ROOT / "scripts" / "verify_composition.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.verify_composition(root)


def _default_contract_validator(root: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "astraldeep_doctor_local_components",
        REPOSITORY_ROOT / "scripts" / "install_local_components.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.load_contract(root, require_sources=False, require_gitlinks=True)


def _host_findings(root: Path) -> tuple[list[Finding], bool]:
    findings: list[Finding] = []
    if platform.system() not in {"Linux", "Darwin", "Windows"}:
        return [Finding("host.platform", PROBLEM, "host.platform-unsupported",
            "this host platform is outside the supported Linux, macOS and Windows Compose path.",
            "Use a supported host with Docker Compose v2 and Python 3.11 or newer.")], False
    version = _python_version()
    if version < (3, 11, 0):
        findings.append(
            Finding(
                "host.python",
                PROBLEM,
                "host.python-too-old",
                f"Python {version[0]}.{version[1]} is older than the required 3.11 runtime.",
                "Run the doctor with Python 3.11 or newer.",
            )
        )
        return findings, False
    missing = [name for name in CHECKOUT_ARTIFACTS if not (root / name).is_file()]
    if missing:
        findings.append(
            Finding(
                "host.checkout",
                PROBLEM,
                "host.checkout-missing",
                f"{root} is not an AstralDeep checkout; missing {', '.join(missing)}.",
                "Clone the repository with --recurse-submodules and run the doctor from "
                "the repository root.",
            )
        )
        return findings, False
    if shutil.which("git") is None:
        findings.append(
            Finding(
                "host.git",
                PROBLEM,
                "host.git-missing",
                "git was not found on PATH.",
                "Install git; component pins and composition verification require it.",
            )
        )
    return findings, True


def _docker_findings(root: Path, run: Callable[..., CommandResult]) -> tuple[list[Finding], bool]:
    findings: list[Finding] = []
    version = run(["docker", "--version"], root, COMPOSE_COMMAND_TIMEOUT)
    if version.returncode != 0:
        findings.append(
            Finding(
                "host.docker",
                PROBLEM,
                "host.docker-missing",
                "the Docker CLI was not found or could not run.",
                "Install Docker Desktop or Docker Engine with the Docker Compose v2 "
                "plugin, then re-run the doctor.",
            )
        )
        return findings, False
    compose = run(["docker", "compose", "version"], root, COMPOSE_COMMAND_TIMEOUT)
    if compose.returncode != 0:
        detail = (compose.stderr or compose.stdout).strip().splitlines()
        findings.append(
            Finding(
                "host.docker",
                PROBLEM,
                "host.compose-missing",
                "the Docker Compose v2 plugin is unavailable"
                + (f": {detail[0]}" if detail else ""),
                "Install the docker-compose-plugin package for your Docker distribution.",
            )
        )
        return findings, False
    return findings, True


def _composition_findings(
    root: Path, verifier: Callable[[Path], object]
) -> list[Finding]:
    try:
        report = verifier(root)
    except Exception as exc:
        return [
            Finding(
                "repository.composition",
                PROBLEM,
                "repository.composition-error",
                f"composition verification could not run: {type(exc).__name__}",
                "Restore the pinned components with "
                "`git submodule update --init --recursive`, then re-run the doctor.",
            )
        ]
    diagnostics = tuple(getattr(report, "diagnostics", ()) or ())
    findings = []
    for diagnostic in diagnostics:
        findings.append(
            Finding(
                "repository.composition",
                PROBLEM,
                f"repository.composition.{getattr(diagnostic, 'code', 'diagnostic')}",
                str(getattr(diagnostic, "message", "composition verification failed")),
                str(getattr(diagnostic, "remediation", "")),
            )
        )
    return findings


def _contract_findings(root: Path, validator: Callable[[Path], None]) -> list[Finding]:
    try:
        validator(root)
    except Exception as exc:
        return [
            Finding(
                "repository.component-contract",
                PROBLEM,
                "repository.component-contract-invalid",
                f"the local component contract is invalid: {type(exc).__name__}",
                "Restore the exact component declarations with "
                "`git submodule update --init --recursive`, then re-run the doctor.",
            )
        ]
    return []


def _configuration_findings(
    root: Path,
    env_file: Path,
    environ: Mapping[str, str],
) -> tuple[list[Finding], dict[str, str], dict[str, str], bool, bool]:
    findings: list[Finding] = []
    if env_file.resolve() != (root / DEFAULT_ENV_NAME).resolve():
        return [Finding("configuration.env-file", PROBLEM, "configuration.env-file-unsupported",
            "the supported Compose deployment reads ROOT/.env; another file cannot establish its readiness.",
            "Use --root for the existing deployment and inspect its .env configuration.")], {}, {}, False, False
    if not env_file.is_file():
        findings.append(
            Finding(
                "configuration.env-file",
                PROBLEM,
                "configuration.env-file-missing",
                f"no environment file was found at {env_file}.",
                "Copy .env.example to .env in the repository root and fill in the "
                "required values, then re-run the doctor.",
            )
        )
        return findings, {}, {}, False, False
    values, parse_problems = parse_env_file(env_file)
    for problem in parse_problems:
        findings.append(
            Finding(
                "configuration.env-file",
                PROBLEM,
                "configuration.env-file-invalid",
                problem,
                "Fix the malformed line so every entry is NAME=value.",
            )
        )
    required, compose_error = required_compose_variables(root / COMPOSE_FILE)
    if compose_error:
        findings.append(
            Finding(
                "configuration.compose-required",
                PROBLEM,
                "configuration.compose-unreadable",
                compose_error,
                "Restore the committed docker-compose.yml.",
            )
        )
        return findings, values, dict(values), True, False
    merged = dict(values)
    for name, _message in required:
        if name in environ:
            merged[name] = environ[name]
    for name, message in required:
        if not merged.get(name, "").strip():
            findings.append(
                Finding(
                    "configuration.compose-required",
                    PROBLEM,
                    "configuration.compose-variable-missing",
                    f"{name} is required by {COMPOSE_FILE}: {message or 'a non-empty value'}.",
                    f"Set {name}=<value> in .env (or export it before running make up).",
                )
            )
    if not values.get("DB_PASSWORD", "").strip():
        findings.append(
            Finding(
                "configuration.compose-required",
                ADVISORY,
                "configuration.postgres-init",
                "DB_PASSWORD is empty: the PostgreSQL container initializes its "
                "administrator password from it and will fail to start with an empty "
                "value.",
                "Set DB_PASSWORD=<value> in .env.",
            )
        )
    posture_problems, posture_advisories = evaluate_posture(values)
    for problem in posture_problems:
        findings.append(
            Finding(
                "configuration.production-posture",
                PROBLEM,
                "configuration.posture",
                problem,
                "Fix the configuration in .env; a production-posture boot refuses to "
                "start (exit code 78) until every listed item is resolved.",
            )
        )
    for advisory in posture_advisories:
        findings.append(
            Finding(
                "configuration.production-posture",
                ADVISORY,
                "configuration.posture-advisory",
                advisory,
                "",
            )
        )
    return findings, values, merged, True, not any(
        finding.code == "configuration.compose-variable-missing" for finding in findings
    )


def _parse_compose_ps(stdout: str) -> list[dict[str, object]] | None:
    text = stdout.strip()
    if not text:
        return []
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        try:
            document = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return None
    entries = [document] if isinstance(document, dict) else document
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        return None
    return entries


def _published_port(entry: Mapping[str, object], target: int) -> tuple[str, int] | None:
    publishers = entry.get("Publishers") or []
    if not isinstance(publishers, list):
        return None
    for publisher in publishers:
        if not isinstance(publisher, dict):
            continue
        if publisher.get("TargetPort") != target:
            continue
        if str(publisher.get("Protocol") or "tcp").lower() not in ("", "tcp"):
            continue
        port = publisher.get("PublishedPort")
        if isinstance(port, int) and port > 0:
            host = str(publisher.get("URL") or "").strip()
            if host in ("", "0.0.0.0", "::"):
                host = "127.0.0.1"
            return host, port
    return None


def _service_findings(
    root: Path,
    values: Mapping[str, str],
    redaction_values: Mapping[str, str],
    run: Callable[..., CommandResult],
    http_probe: Callable[[str, float], HttpResult],
    tcp_probe: Callable[[str, int, float], bool],
    http_timeout: float,
) -> list[Finding]:
    findings: list[Finding] = []
    services = run(["docker", "compose", "config", "--services"], root, COMPOSE_COMMAND_TIMEOUT)
    if services.returncode != 0:
        findings.append(
            Finding(
                "services.compose-config",
                PROBLEM,
                "services.compose-config-failed",
                "docker compose could not describe the deployment: "
                + _redact((services.stderr or services.stdout).strip()[:400], redaction_values),
                "Fix docker-compose.yml and .env so the deployment can be described, "
                "then re-run the doctor.",
            )
        )
        return findings
    expected = [name for name in services.stdout.split() if name]
    if not expected:
        findings.append(
            Finding(
                "services.compose-config",
                PROBLEM,
                "services.compose-config-empty",
                "the compose file declares no default services.",
                "Restore the committed docker-compose.yml.",
            )
        )
        return findings
    state = run(
        ["docker", "compose", "ps", "--all", "--format", "json"],
        root,
        COMPOSE_COMMAND_TIMEOUT,
    )
    if state.returncode != 0:
        findings.append(
            Finding(
                "services.compose-state",
                PROBLEM,
                "services.compose-state-failed",
                "docker compose ps failed: "
                + _redact((state.stderr or state.stdout).strip()[:400], redaction_values),
                "Start the Docker daemon, then run `make up` to create the deployment.",
            )
        )
        return findings
    entries = _parse_compose_ps(state.stdout)
    if entries is None:
        findings.append(
            Finding(
                "services.compose-state",
                PROBLEM,
                "services.compose-state-unparseable",
                "docker compose ps produced output this doctor could not parse.",
                "Re-run with docker compose ps --all --format json and inspect the "
                "output manually.",
            )
        )
        return findings
    by_service: dict[str, dict[str, object]] = {}
    for entry in entries:
        name = str(entry.get("Service") or "")
        if name in by_service:
            findings.append(Finding("services.compose-state", PROBLEM, "services.service-duplicate",
                f"required service {name} has multiple container records; readiness is ambiguous.",
                "Inspect docker compose ps --all and resolve the duplicate containers."))
        if name:
            by_service[name] = entry
    app_entry: dict[str, object] | None = None
    for name in expected:
        entry = by_service.get(name)
        if entry is None:
            findings.append(
                Finding(
                    "services.compose-state",
                    PROBLEM,
                    "services.service-missing",
                    f"required service {name} has no container in this deployment.",
                    "Run `make up` to build and start the composed deployment.",
                )
            )
            continue
        service_state = str(entry.get("State") or "").lower()
        health = str(entry.get("Health") or "").lower()
        exit_code = entry.get("ExitCode")
        if service_state != "running":
            detail = f" (exit code {exit_code})" if exit_code not in (None, 0, "0") else ""
            findings.append(
                Finding(
                    "services.compose-state",
                    PROBLEM,
                    "services.service-not-running",
                    f"required service {name} is {service_state or 'in an unknown state'}"
                    + detail
                    + ".",
                    "Inspect it with `make logs` (or docker compose logs "
                    f"{name}) and recreate it with `make up`.",
                )
            )
        elif health and health != "healthy":
            findings.append(
                Finding(
                    "services.compose-state",
                    PROBLEM,
                    "services.service-unhealthy",
                    f"required service {name} is running but its health check reports "
                    f"'{health}'.",
                    "Inspect it with `make logs`; the service health gate must pass "
                    "before the deployment is ready.",
                )
            )
        if name == APP_SERVICE:
            app_entry = entry
    if app_entry is not None and str(app_entry.get("State") or "").lower() == "running":
        findings.extend(_image_findings(app_entry, run, root, redaction_values))
        findings.extend(_app_http_findings(app_entry, http_probe, http_timeout))
    if "livekit" in expected:
        findings.extend(
            _livekit_findings(by_service.get("livekit"), tcp_probe, http_timeout)
        )
    findings.extend(
        _keycloak_findings(values, http_probe, http_timeout)
    )
    return findings


def _app_http_findings(
    entry: Mapping[str, object],
    http_probe: Callable[[str, float], HttpResult],
    http_timeout: float,
) -> list[Finding]:
    findings: list[Finding] = []
    published = _published_port(entry, APP_TARGET_PORT)
    if published is None:
        findings.append(
            Finding(
                "services.app-http",
                PROBLEM,
                "services.app-port-unpublished",
                f"{APP_SERVICE} does not publish host port {APP_TARGET_PORT}.",
                "Recreate the deployment with `make up` so the app port is published.",
            )
        )
        return findings
    host, port = published
    base = f"http://{host}:{port}"
    health = http_probe(f"{base}/healthz", http_timeout)
    if health.status != 200:
        detail = f" (HTTP {health.status})" if health.status else f" ({health.reason})"
        findings.append(
            Finding(
                "services.app-http",
                PROBLEM,
                "services.app-not-live",
                f"the app did not answer GET /healthz on {base}{detail}.",
                "Confirm the container is running with `docker compose ps` and inspect "
                "it with `make logs`.",
            )
        )
        return findings
    ready = http_probe(f"{base}/readyz", http_timeout)
    if ready.status == 200:
        pass
    elif ready.status == 503:
        findings.append(
            Finding(
                "services.app-http",
                PROBLEM,
                "services.app-not-ready",
                f"the app is live on {base} but GET /readyz returned HTTP 503.",
                "Inspect `make logs` for the reported readiness posture, resolve it, "
                "then re-run the doctor.",
            )
        )
    else:
        detail = f" (HTTP {ready.status})" if ready.status else f" ({ready.reason})"
        findings.append(
            Finding(
                "services.app-http",
                PROBLEM,
                "services.app-readyz-failed",
                f"GET /readyz on {base} failed{detail}.",
                "Inspect it with `make logs`, then re-run the doctor.",
            )
        )
    return findings


def _image_findings(
    entry: Mapping[str, object],
    run: Callable[..., CommandResult],
    root: Path,
    values: Mapping[str, str],
) -> list[Finding]:
    container = str(entry.get("ID") or "")
    if not container:
        return [
            Finding(
                "services.image-freshness",
                PROBLEM,
                "services.image-unverified",
                "the running app container has no inspectable ID; image freshness "
                "could not be verified.",
                "",
            )
        ]
    labels = run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", container],
        root,
        COMPOSE_COMMAND_TIMEOUT,
    )
    revision = ""
    if labels.returncode == 0:
        try:
            document = json.loads(labels.stdout.strip() or "{}")
            if isinstance(document, dict):
                revision = str(document.get(REVISION_LABEL) or "")
        except json.JSONDecodeError:
            revision = ""
    head = run(["git", "rev-parse", "HEAD"], root, COMPOSE_COMMAND_TIMEOUT)
    head_revision = head.stdout.strip() if head.returncode == 0 else ""
    if revision and head_revision and revision != head_revision:
        return [
            Finding(
                "services.image-freshness",
                PROBLEM,
                "services.image-stale",
                f"the running app image was built from {revision[:12]} but the checkout "
                f"is at {head_revision[:12]}.",
                "Run `make up` to rebuild and recreate the app with the current source "
                "(or `make sync` for backend-only edits).",
            )
        ]
    if (not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not re.fullmatch(r"[0-9a-f]{40}", head_revision)):
        return [
            Finding(
                "services.image-freshness",
                PROBLEM,
                "services.image-unverified",
                "the running app image revision or checkout identity could not be verified.",
                f"Build the image with its exact source commit in the {REVISION_LABEL} label, "
                "then recreate the app and re-run the doctor.",
            )
        ]
    return []


def _livekit_findings(
    entry: Mapping[str, object] | None,
    tcp_probe: Callable[[str, int, float], bool],
    http_timeout: float,
) -> list[Finding]:
    if entry is None:
        return []
    published = _published_port(entry, LIVEKIT_TARGET_PORT)
    if published is None:
        return [
            Finding(
                "services.livekit",
                ADVISORY,
                "services.livekit-port-unpublished",
                f"livekit does not publish host port {LIVEKIT_TARGET_PORT}; media "
                "clients are expected to reach it through LIVEKIT_PUBLIC_URL.",
                "",
            )
        ]
    host, port = published
    if tcp_probe(host, port, http_timeout):
        return []
    return [
        Finding(
            "services.livekit",
            PROBLEM,
            "services.livekit-unreachable",
            f"the livekit media port {host}:{port} is not reachable from this host.",
            "Confirm the livekit container is running (`docker compose ps`) and that "
            "LIVEKIT_BIND_IP publishes port 7880, then re-run the doctor.",
        )
    ]


def _keycloak_findings(
    values: Mapping[str, str],
    http_probe: Callable[[str, float], HttpResult],
    http_timeout: float,
) -> list[Finding]:
    mock_on = str(values.get("USE_MOCK_AUTH", "") or "").strip().lower() in MOCK_AUTH_VALUES
    if mock_on:
        return []
    authority = str(values.get("KEYCLOAK_AUTHORITY", "") or "").strip()
    if not authority:
        return [
            Finding(
                "services.keycloak",
                PROBLEM,
                "services.keycloak-unconfigured",
                "mock authentication is off but KEYCLOAK_AUTHORITY is empty: there is "
                "no configured sign-in path.",
                "Set KEYCLOAK_AUTHORITY, KEYCLOAK_CLIENT_ID, and KEYCLOAK_CLIENT_SECRET "
                "in .env (or set USE_MOCK_AUTH=true with ASTRAL_ENV=development for "
                "local development).",
            )
        ]
    try:
        parsed = urlsplit(authority)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment and parsed.port != 0)
    except ValueError:
        valid = False
    if not valid:
        return [Finding("services.keycloak", PROBLEM, "services.keycloak-authority-invalid",
            "KEYCLOAK_AUTHORITY must be an HTTP(S) realm URL without user credentials, query or fragment.",
            "Set the public realm issuer URL in .env and re-run the doctor.")]
    discovery = http_probe(authority.rstrip("/") + DISCOVERY_SUFFIX, http_timeout)
    if discovery.status == 0:
        return [
            Finding(
                "services.keycloak",
                PROBLEM,
                "services.keycloak-unreachable",
                f"the authentication service at {authority} is unreachable from this "
                f"host ({discovery.reason}).",
                "Verify DNS and firewall access; the browser and the app must both "
                "reach KEYCLOAK_AUTHORITY, then re-run the doctor.",
            )
        ]
    if discovery.status != 200:
        return [
            Finding(
                "services.keycloak",
                PROBLEM,
                "services.keycloak-discovery-failed",
                f"OIDC discovery at {authority} returned HTTP {discovery.status}.",
                "Verify the realm URL in KEYCLOAK_AUTHORITY and that the "
                "authentication service is running, then re-run the doctor.",
            )
        ]
    try:
        document = json.loads(discovery.body.decode("utf-8", "replace"))
        issuer = str(document.get("issuer") or "") if isinstance(document, dict) else ""
    except json.JSONDecodeError:
        issuer = ""
    if not issuer:
        return [
            Finding(
                "services.keycloak",
                PROBLEM,
                "services.keycloak-discovery-invalid",
                f"OIDC discovery at {authority} returned a response without an issuer.",
                "Verify that the URL points at a Keycloak realm, then re-run the "
                "doctor.",
            )
        ]
    if issuer.rstrip("/") != authority.rstrip("/"):
        return [
            Finding(
                "services.keycloak",
                PROBLEM,
                "services.keycloak-issuer-mismatch",
                f"OIDC discovery at {authority} advertises issuer {issuer}: token "
                "validation would fail.",
                "Set KEYCLOAK_AUTHORITY to the advertised realm issuer URL, then "
                "re-run the doctor.",
            )
        ]
    return []


def collect(
    root: Path | str,
    *,
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    command_runner: Callable[..., CommandResult] = _run_command,
    http_probe: Callable[[str, float], HttpResult] = _http_get,
    tcp_probe: Callable[[str, int, float], bool] = _tcp_reachable,
    composition_verifier: Callable[[Path], object] = _default_composition_verifier,
    contract_validator: Callable[[Path], None] = _default_contract_validator,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> Report:
    root = Path(root)
    environment = os.environ if environ is None else environ
    env_path = Path(env_file) if env_file is not None else root / DEFAULT_ENV_NAME
    findings: list[Finding] = []
    host_findings, supported = _host_findings(root)
    findings.extend(host_findings)
    if supported:
        docker_ready = False
        services_block: str | None = None
        if shutil.which("git") is None:
            services_block = "git is unavailable, so component composition cannot be verified"
        else:
            docker_findings, docker_ready = _docker_findings(root, command_runner)
            findings.extend(docker_findings)
            if not docker_ready:
                services_block = "Docker Compose is unavailable on this host"
        findings.extend(_composition_findings(root, composition_verifier))
        findings.extend(_contract_findings(root, contract_validator))
        configuration_findings, values, merged, configured, interpolation_ok = (
            _configuration_findings(root, env_path, environment)
        )
        findings.extend(configuration_findings)
        if services_block is None and (not configured or not interpolation_ok):
            services_block = (
                "the deployment configuration is incomplete, so Docker Compose "
                "cannot describe the services"
            )
        if services_block is not None:
            findings.append(
                Finding(
                    "services.skipped",
                    ADVISORY,
                    "services.skipped",
                    f"service health checks skipped: {services_block}.",
                    "",
                )
            )
        else:
            findings.extend(
                _service_findings(
                    root,
                    values,
                    merged,
                    command_runner,
                    http_probe,
                    tcp_probe,
                    http_timeout,
                )
            )
    outcome = UNSUPPORTED if not supported else (
        REPAIR if any(finding.severity == PROBLEM for finding in findings) else READY
    )
    redaction = dict(environment)
    if supported:
        redaction.update(merged)
        redaction.update(values)
    findings = [Finding(f.check, f.severity, f.code,
        _redact(f.message, redaction), _redact(f.remediation, redaction)) for f in findings]
    return Report(
        contract=CONTRACT,
        root=str(root),
        env_file=str(env_path) if supported else None,
        platform=f"{platform.system()} {platform.release()}".strip(),
        python=".".join(str(part) for part in _python_version()),
        outcome=outcome,
        findings=tuple(findings),
    )


def render_text(report: Report) -> str:
    lines = [
        f"AstralDeep installation doctor — outcome: {report.outcome}",
        f"root: {report.root} ({report.platform}, Python {report.python})",
    ]
    if report.env_file:
        lines.append(f"environment: {report.env_file} (values are never printed)")
    if not report.findings:
        lines.append("no findings: prerequisites, composition, configuration, and "
                     "composed services are ready.")
    else:
        lines.append("findings:")
        for finding in report.findings:
            lines.append(f"  [{finding.severity}] {finding.check} ({finding.code})")
            lines.append(f"    {finding.message}")
            if finding.remediation:
                lines.append(f"    remediation: {finding.remediation}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_doctor",
        description=(
            "Read-only AstralDeep installation and readiness doctor. It verifies host "
            "prerequisites, exact component composition, required configuration, and "
            "composed service health. It never signs in, never mutates anything, never "
            "prints secret values, and never reports ready while a required service is "
            "missing. Exit codes: 0 ready, 1 repair, 2 unsupported."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="AstralDeep checkout to diagnose (default: the checkout containing this script)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="environment file to inspect (default: ROOT/.env)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT,
        help="seconds allowed for each HTTP service probe (default: 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable report instead of human-readable text",
    )
    arguments = parser.parse_args(argv)
    report = collect(
        arguments.root,
        env_file=arguments.env_file,
        http_timeout=arguments.http_timeout,
    )
    if arguments.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_text(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
