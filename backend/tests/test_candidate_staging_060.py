"""Fail-closed candidate-staging driver and topology contracts (T103/T107)."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_candidate_staging.py"
COMPOSE = REPO_ROOT / "docker-compose.staging.yml"
LIVEKIT_CONFIG = REPO_ROOT / "deploy" / "livekit" / "livekit.staging.yaml"
MANIFEST = (
    REPO_ROOT
    / "backend/tests/fixtures/runtime_reliability_060/staging/fixture-manifest.json"
)
SPEECH_PROFILE_SHA256 = (
    "9b857a3d788a5d6c4ff67278eca4a169028fb2e185ccaf0b01961283f629445b"
)
PINNED_LIVEKIT_IMAGE = (
    "livekit/livekit-server:v1.13.5@sha256:"
    "3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1"
)
LIVEKIT_CONFIG_SHA256 = hashlib.sha256(LIVEKIT_CONFIG.read_bytes()).hexdigest()


def _voice_args() -> dict[str, str]:
    return {
        "voice_worker_image": (
            "ghcr.io/astraldeep/voice-worker@sha256:" + "d" * 64
        ),
        "livekit_image": PINNED_LIVEKIT_IMAGE,
        "livekit_config_sha256": LIVEKIT_CONFIG_SHA256,
        "speech_inventory_sha256": "1" * 64,
        "speech_profile_sha256": SPEECH_PROFILE_SHA256,
    }


def _voice_cli() -> list[str]:
    args = _voice_args()
    return [
        "--voice-worker-image",
        args["voice_worker_image"],
        "--livekit-image",
        args["livekit_image"],
        "--livekit-config-sha256",
        args["livekit_config_sha256"],
        "--speech-inventory-sha256",
        args["speech_inventory_sha256"],
        "--speech-profile-sha256",
        args["speech_profile_sha256"],
    ]

if not (
    (REPO_ROOT / "scripts").is_dir() and (REPO_ROOT / "specs").is_dir()
):  # repo root absent inside the product image
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )


def _load_driver() -> Any:
    spec = importlib.util.spec_from_file_location("candidate_staging_060", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def driver() -> Any:
    return _load_driver()


def test_tracked_fixture_set_validates_and_reports_only_nonsecret_identity(
    driver: Any,
) -> None:
    result = driver.validate_fixtures(MANIFEST)
    assert result["source_schema_revision"] == "057.001"
    assert result["synthetic"] is True
    assert result["contains_credentials"] is False
    assert len(result["fixture_manifest_sha256"]) == 64
    assert len(result["representative_dataset_sha256"]) == 64
    assert len(result["keycloak_realm_sha256"]) == 64
    serialized = json.dumps(result, sort_keys=True).lower()
    assert "password" not in serialized
    assert "access_token" not in serialized


def test_fixture_validation_detects_manifest_fingerprint_drift(
    driver: Any, tmp_path: Path
) -> None:
    fixture_root = MANIFEST.parent
    copied = copy.deepcopy(json.loads(MANIFEST.read_text(encoding="utf-8")))
    copied["files"]["representative-057.sql"]["sha256"] = "0" * 64
    path = tmp_path / "fixture-manifest.json"
    path.write_text(json.dumps(copied), encoding="utf-8")
    # Keep the tampered manifest beside symlinks to the real public fixtures.
    for name in ("representative-057.sql", "keycloak-realm.json", "legacy-agent-root"):
        (tmp_path / name).symlink_to(fixture_root / name, target_is_directory=name.endswith("root"))
    with pytest.raises(driver.StagingError, match="fingerprint"):
        driver.validate_fixtures(path)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://stage.astraldeep.invalid", "HTTPS"),
        ("https://localhost:8001", "loopback"),
        ("https://user@stage.astraldeep.invalid", "userinfo"),
        ("https://stage.astraldeep.invalid?candidate=x", "query"),
        ("https://stage.astraldeep.invalid/#fragment", "fragment"),
        ("https:///missing-host", "no host"),
        ("https://stage.astraldeep.invalid/path\n", "whitespace"),
        ("https://[malformed", "malformed"),
    ],
)
def test_staging_endpoint_must_be_archivable_nonlocal_https(
    driver: Any, endpoint: str, message: str
) -> None:
    with pytest.raises(driver.StagingError, match=message):
        driver.validate_endpoint(endpoint)


@pytest.mark.parametrize(
    "reference",
    [
        "astraldeep:latest",
        "ghcr.io/AstralDeep/AstralDeep:060",
        "http://registry.invalid/image@sha256:" + "a" * 64,
        "ghcr.io/AstralDeep/AstralDeep@sha256:" + "A" * 64,
    ],
)
def test_candidate_and_dependency_images_must_be_digest_qualified(
    driver: Any, reference: str
) -> None:
    with pytest.raises(driver.StagingError, match="digest-qualified"):
        driver.validate_image_reference(reference)


def test_deploy_fails_before_docker_without_protected_runner_inputs() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "deploy",
            "--candidate-sha",
            "a" * 40,
            "--candidate-source-root",
            str(REPO_ROOT),
            "--candidate-image",
            "ghcr.io/AstralDeep/AstralDeep@sha256:" + "b" * 64,
            "--fixture-manifest",
            str(MANIFEST),
            "--environment-id",
            "stage-060-test",
            "--outputs",
            "/tmp/stage-060-output-must-not-exist.json",
            *_voice_cli(),
            "--leave-running",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert completed.returncode == 2
    assert "trusted staging runner" in completed.stderr
    assert not Path("/tmp/stage-060-output-must-not-exist.json").exists()


def test_compose_topology_has_no_retired_deep_schema_loader() -> None:
    source = COMPOSE.read_text(encoding="utf-8")
    for service in ("postgres:", "keycloak:", "astraldeep:"):
        assert service in source
    for variable in (
        "STAGING_POSTGRES_IMAGE",
        "STAGING_KEYCLOAK_IMAGE",
        "ASTRAL_CANDIDATE_IMAGE",
    ):
        assert "${" + variable in source
    assert "schema-baseline" not in source
    assert "from shared.database import Database" not in source
    assert "representative-057.sql" not in source
    assert "keycloak-realm.json" in source
    assert "latest" not in source.lower()
    assert "build:" not in source
    assert "mock" not in source.lower()


def test_cli_exposes_only_validate_deploy_and_scoped_cleanup_commands() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert "validate-fixtures" in completed.stdout
    assert "deploy" in completed.stdout
    assert "write-trusted-manifest" in completed.stdout
    assert "cleanup" in completed.stdout
    source = SCRIPT.read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "docker compose down" not in source
    assert "docker system prune" not in source


def _protected_environment(runtime_env: Path) -> dict[str, str]:
    image = "registry.example.invalid/astral/dependency@sha256:" + "a" * 64
    return {
        "ASTRAL_STAGING_ENDPOINT": "https://stage-060.example.invalid",
        "ASTRAL_STAGING_PROBE_TOKEN": "test-only-probe-value",
        "STAGING_POSTGRES_IMAGE": image,
        "STAGING_KEYCLOAK_IMAGE": image,
        "STAGING_RUNTIME_ENV_FILE": str(runtime_env),
        "STAGING_DB_USER": "astral",
        "STAGING_DB_PASSWORD": "test-only-database-value",
        "STAGING_DB_NAME": "astral",
        "STAGING_KEYCLOAK_DB_USER": "keycloak",
        "STAGING_KEYCLOAK_DB_PASSWORD": "test-only-keycloak-database-value",
        "STAGING_KEYCLOAK_DB_NAME": "keycloak",
        "STAGING_KEYCLOAK_ADMIN_USER": "bootstrap-admin",
        "STAGING_KEYCLOAK_ADMIN_PASSWORD": "test-only-bootstrap-value",
        "STAGING_BIND_PORT": "18061",
        "LIVEKIT_PUBLIC_URL": "wss://voice.stage-060.example.invalid",
        "LIVEKIT_API_KEY": "test-only-livekit-key",
        "LIVEKIT_API_SECRET": "test-only-livekit-secret",
        "LIVEKIT_TURN_DOMAIN": "turn.stage-060.example.invalid",
        "VOICE_CONTROL_SECRET": "test-only-control-secret",
        "OPENAI_BASE_URL": "https://speech.stage-060.example.invalid/v1",
        "OPENAI_API_KEY": "test-only-speech-key",
        "GITHUB_RUN_ID": "6001",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "stage-deploy",
        "RUNNER_NAME": "trusted-staging-1",
        "ASTRAL_STAGING_EXPECTED_RUNNER_NAME": "trusted-staging-1",
    }


def _ownership_labels(environment: dict[str, str]) -> dict[str, str]:
    return {
        "com.astraldeep.staging.managed": "true",
        "com.astraldeep.staging.project": environment["STAGING_PROJECT_NAME"],
        "com.astraldeep.staging.environment-id": environment[
            "STAGING_ENVIRONMENT_ID"
        ],
        "com.astraldeep.staging.run-id": environment["STAGING_RUN_ID"],
        "com.astraldeep.staging.run-attempt": environment["STAGING_RUN_ATTEMPT"],
    }


def _expected_service_images(environment: dict[str, str]) -> dict[str, str]:
    return {
        "postgres": environment["STAGING_POSTGRES_IMAGE"],
        "keycloak-postgres": environment["STAGING_POSTGRES_IMAGE"],
        "keycloak": environment["STAGING_KEYCLOAK_IMAGE"],
        "livekit": PINNED_LIVEKIT_IMAGE,
        "astraldeep": environment["ASTRAL_CANDIDATE_IMAGE"],
        "voice-worker": environment["ASTRAL_VOICE_WORKER_IMAGE"],
    }


def _rendered_compose_model(environment: dict[str, str]) -> bytes:
    ownership_labels = _ownership_labels(environment)
    images = _expected_service_images(environment)
    return json.dumps(
        {
            "name": environment["STAGING_PROJECT_NAME"],
            "services": {
                "postgres": {"image": images["postgres"], "labels": ownership_labels},
                "keycloak-postgres": {
                    "image": images["keycloak-postgres"],
                    "labels": ownership_labels,
                },
                "keycloak": {"image": images["keycloak"], "labels": ownership_labels},
                "astraldeep": {
                    "image": images["astraldeep"],
                    "labels": ownership_labels,
                    "ports": [
                        {
                            "host_ip": "127.0.0.1",
                            "target": 8001,
                            "published": environment["STAGING_BIND_PORT"],
                            "protocol": "tcp",
                            "mode": "ingress",
                        }
                    ],
                },
                "voice-worker": {
                    "image": images["voice-worker"],
                    "labels": ownership_labels,
                },
                "livekit": {
                    "image": images["livekit"],
                    "labels": ownership_labels,
                    "command": ["--config", "/etc/livekit.yaml"],
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(LIVEKIT_CONFIG.resolve()),
                            "target": "/etc/livekit.yaml",
                            "read_only": True,
                        }
                    ],
                    "ports": [
                        {
                            "host_ip": "127.0.0.1",
                            "target": 5349,
                            "published": "15349",
                            "protocol": "tcp",
                            "mode": "ingress",
                        }
                    ],
                },
            },
            "volumes": {
                "product-postgres": {"labels": ownership_labels},
                "keycloak-postgres": {"labels": ownership_labels},
            },
            "networks": {"default": {"labels": ownership_labels}},
        }
    ).encode()


def test_protected_environment_validation_accepts_only_private_complete_inputs(
    driver: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("ASTRAL_ENV=staging\n", encoding="utf-8")
    runtime_env.chmod(0o600)
    values = _protected_environment(runtime_env)

    class ProtectedPath:
        mode = 0o600

        def __init__(self, value: str) -> None:
            self.path = Path(value)

        def is_absolute(self) -> bool:
            return self.path.is_absolute()

        def is_file(self) -> bool:
            return self.path.is_file()

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_mode=self.mode)

    monkeypatch.setattr(driver, "Path", ProtectedPath)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ASTRAL_STAGING_RUNNER_TRUSTED", "true")
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert driver._required_environment() == values

    monkeypatch.setenv("ASTRAL_STAGING_EXPECTED_RUNNER_NAME", "different-runner")
    with pytest.raises(driver.StagingError, match="wrong runner"):
        driver._required_environment()
    monkeypatch.setenv(
        "ASTRAL_STAGING_EXPECTED_RUNNER_NAME",
        values["ASTRAL_STAGING_EXPECTED_RUNNER_NAME"],
    )

    runtime_env.chmod(0o644)
    ProtectedPath.mode = 0o644
    with pytest.raises(driver.StagingError, match="group/world"):
        driver._required_environment()
    runtime_env.chmod(0o600)
    monkeypatch.delenv("GITHUB_RUN_ID")
    with pytest.raises(driver.StagingError, match="absent"):
        driver._required_environment()

    monkeypatch.setenv("GITHUB_RUN_ID", "6001")
    monkeypatch.setenv("STAGING_POSTGRES_IMAGE", "postgres:mutable")
    with pytest.raises(driver.StagingError, match="digest-qualified"):
        driver._required_environment()
    monkeypatch.setenv(
        "STAGING_POSTGRES_IMAGE",
        "registry.example.invalid/astral/dependency@sha256:" + "a" * 64,
    )
    monkeypatch.setenv("STAGING_RUNTIME_ENV_FILE", "relative.env")
    with pytest.raises(driver.StagingError, match="absolute protected file"):
        driver._required_environment()


def test_git_identity_requires_exact_clean_head(
    driver: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = "b" * 40

    def clean(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        output = (
            f"{candidate}\n".encode()
            if arguments[-2:] == ["rev-parse", "HEAD"]
            else b""
        )
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(driver, "_run", clean)
    driver._git_identity(candidate, REPO_ROOT)
    with pytest.raises(driver.StagingError, match="candidate-sha"):
        driver._git_identity("not-a-sha", REPO_ROOT)

    def wrong(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(arguments, 0, ("c" * 40 + "\n").encode(), b"")

    monkeypatch.setattr(driver, "_run", wrong)
    with pytest.raises(driver.StagingError, match="differs"):
        driver._git_identity(candidate, REPO_ROOT)

    calls = 0

    def dirty(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        output = f"{candidate}\n".encode() if calls == 1 else b"?? generated.txt\n"
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(driver, "_run", dirty)
    with pytest.raises(driver.StagingError, match="clean checkout"):
        driver._git_identity(candidate, REPO_ROOT)


def test_probe_reads_real_readiness_and_authenticated_capability_shape(
    driver: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = {
        "supported": False,
        "runtime_contract_versions": [],
        "source_feature": None,
    }

    class Response:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self.body = body

        def read(self, _limit: int) -> bytes:
            return self.body

    class Connection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.responses = [
                Response(200, b"ready"),
                Response(200, json.dumps({"capabilities": {"personal_agent_host": {"macos": capability}}}).encode()),
            ]
            self.requests: list[tuple[Any, ...]] = []

        def request(self, *args: Any, **kwargs: Any) -> None:
            self.requests.append((*args, kwargs))

        def getresponse(self) -> Response:
            return self.responses.pop(0)

        def close(self) -> None:
            return None

    monkeypatch.setattr(driver.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(driver.ssl, "create_default_context", object)
    assert driver._probe("https://stage.example.invalid/request-1", "opaque") == capability


def test_authenticated_staging_request_is_bounded_and_never_follows_redirects(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 307

        @staticmethod
        def read(_limit: int) -> bytes:
            return b'{}'

    requests: list[tuple[Any, ...]] = []

    class Connection:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def request(self, *args: Any, **kwargs: Any) -> None:
            requests.append((*args, kwargs))

        @staticmethod
        def getresponse() -> Response:
            return Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(driver.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(driver.ssl, "create_default_context", object)
    with pytest.raises(driver.StagingError, match="redirects are forbidden"):
        driver._authenticated_json_request(
            "https://stage.example.invalid",
            "opaque",
            method="POST",
            suffix="/api/chats",
            expected_status=201,
            purpose="test probe",
        )
    assert len(requests) == 1

    class OversizedResponse:
        status = 201

        @staticmethod
        def read(limit: int) -> bytes:
            return b"x" * limit

    monkeypatch.setattr(Connection, "getresponse", staticmethod(OversizedResponse))
    with pytest.raises(driver.StagingError, match="oversized"):
        driver._authenticated_json_request(
            "https://stage.example.invalid",
            "opaque",
            method="POST",
            suffix="/api/chats",
            expected_status=201,
            purpose="test probe",
        )
    assert len(requests) == 2


def test_deploy_and_cleanup_execute_exact_request_scoped_sequence(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("ASTRAL_ENV=staging\n", encoding="utf-8")
    runtime_env.chmod(0o600)
    protected = _protected_environment(runtime_env)
    monkeypatch.setattr(driver, "_required_environment", lambda **_kwargs: protected)
    monkeypatch.setattr(driver, "_git_identity", lambda _candidate, _root: None)
    capability = {"supported": False, "runtime_contract_versions": [], "source_feature": None}
    monkeypatch.setattr(driver, "_probe", lambda _endpoint, _token: capability)
    calls: list[tuple[list[str], bytes | None, dict[str, str]]] = []

    def run(
        arguments: list[str],
        *,
        environment: dict[str, str],
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((list(arguments), input_bytes, dict(environment)))
        if "config" in arguments and "--format" in arguments:
            output = _rendered_compose_model(environment)
        elif "ps" in arguments and "--format" in arguments:
            images = _expected_service_images(environment)
            output = json.dumps(
                [
                    {
                        "Service": service,
                        "State": "running",
                        "Image": images[service],
                    }
                    for service in (
                        "postgres",
                        "keycloak-postgres",
                        "keycloak",
                        "livekit",
                        "astraldeep",
                        "voice-worker",
                    )
                ]
            ).encode()
        else:
            output = b""
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(driver, "_run", run)
    output_path = tmp_path / "outputs" / "stage.json"
    candidate = "b" * 40
    candidate_image = "ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64
    args = SimpleNamespace(
        leave_running=True,
        candidate_image=candidate_image,
        candidate_sha=candidate,
        candidate_source_root=str(REPO_ROOT),
        fixture_manifest=str(MANIFEST),
        environment_id="rr-6001-1",
        outputs=str(output_path),
        **_voice_args(),
    )
    with pytest.raises(driver.StagingError, match="no AstralPlane-owned"):
        driver._deploy(args)
    assert calls == []
    assert not output_path.exists()

    calls.clear()
    protected["GITHUB_JOB"] = "stage-cleanup"
    assert driver._cleanup(SimpleNamespace(environment_id="rr-6001-1")) == 0
    down_call = next(call for call in calls if "down" in call[0])
    assert down_call[0][-5:] == [
        "down",
        "--volumes",
        "--remove-orphans",
        "--timeout",
        "30",
    ]
    cleanup_environment = down_call[2]
    assert cleanup_environment["ASTRAL_CANDIDATE_IMAGE"].endswith(
        "@sha256:" + "0" * 64
    )
    assert cleanup_environment["ASTRAL_VOICE_WORKER_IMAGE"] == cleanup_environment[
        "ASTRAL_CANDIDATE_IMAGE"
    ]


def test_cleanup_rejects_wrong_job_and_run_identity_before_docker(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = {
        "GITHUB_RUN_ID": "6001",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_JOB": "stage-deploy",
        "RUNNER_NAME": "trusted-staging-1",
    }
    monkeypatch.setattr(driver, "_required_environment", lambda **_kwargs: protected)

    def forbidden(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("cleanup reached Docker before validating protected identity")

    monkeypatch.setattr(driver, "_run", forbidden)
    with pytest.raises(driver.StagingError, match="stage-cleanup"):
        driver._cleanup(SimpleNamespace(environment_id="rr-6001-2"))

    protected["GITHUB_JOB"] = "stage-cleanup"
    with pytest.raises(driver.StagingError, match="current protected run"):
        driver._cleanup(SimpleNamespace(environment_id="rr-6002-2"))

    protected["GITHUB_RUN_ID"] = "not-a-run"
    with pytest.raises(driver.StagingError, match="positive decimal"):
        driver._cleanup(SimpleNamespace(environment_id="rr-not-a-run-2"))


def test_cleanup_rejects_mixed_resource_ownership_before_down(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = {
        "GITHUB_RUN_ID": "6001",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "stage-cleanup",
        "RUNNER_NAME": "trusted-staging-1",
    }
    monkeypatch.setattr(driver, "_required_environment", lambda **_kwargs: protected)
    project = "astral060-rr-6001-1"
    labels = driver._staging_ownership_labels(
        project=project,
        environment_id="rr-6001-1",
        run_id="6001",
        run_attempt="1",
    )
    live_labels = {"com.docker.compose.project": project, **labels}
    wrong_labels = {**live_labels, "com.astraldeep.staging.run-id": "5999"}
    calls: list[list[str]] = []

    def run(
        arguments: list[str],
        *,
        environment: dict[str, str],
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        del environment, input_bytes
        calls.append(list(arguments))
        if arguments[:4] == ["docker", "container", "ls", "--all"]:
            output = b"container-a\n"
        elif arguments[:3] == ["docker", "container", "inspect"]:
            output = (json.dumps(live_labels) + "\n").encode()
        elif arguments[:3] == ["docker", "volume", "ls"]:
            output = b"volume-a\n"
        elif arguments[:3] == ["docker", "volume", "inspect"]:
            output = (json.dumps(wrong_labels) + "\n").encode()
        else:
            raise AssertionError(f"unexpected command before ownership rejection: {arguments}")
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(driver, "_run", run)
    with pytest.raises(driver.StagingError, match="mismatched protected ownership"):
        driver._cleanup(SimpleNamespace(environment_id="rr-6001-1"))
    assert not any("down" in command for command in calls)


def test_deploy_rejects_early_cleanup_and_untracked_fixture_root(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("ASTRAL_ENV=staging\n", encoding="utf-8")
    runtime_env.chmod(0o600)
    monkeypatch.setattr(
        driver,
        "_required_environment",
        lambda **_kwargs: _protected_environment(runtime_env),
    )
    common = {
        "candidate_image": "ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64,
        "candidate_sha": "b" * 40,
        "candidate_source_root": str(REPO_ROOT),
        "fixture_manifest": str(MANIFEST),
        "environment_id": "request-060-early",
        "outputs": str(tmp_path / "out.json"),
        **_voice_args(),
    }
    with pytest.raises(driver.StagingError, match="leave-running"):
        driver._deploy(SimpleNamespace(leave_running=False, **common))

    monkeypatch.setattr(driver, "_git_identity", lambda _candidate, _root: None)
    monkeypatch.setattr(
        driver,
        "validate_fixtures",
        lambda _manifest: {
            "representative_dataset_sha256": "1" * 64,
            "fixture_manifest_sha256": "2" * 64,
            "keycloak_realm_sha256": "3" * 64,
        },
    )
    common["fixture_manifest"] = str(tmp_path / "untracked-manifest.json")
    with pytest.raises(driver.StagingError, match="tracked fixture root"):
        driver._deploy(SimpleNamespace(leave_running=True, **common))


def test_run_wraps_command_failures_without_leaking_raw_control(
    driver: Any, tmp_path: Path
) -> None:
    with pytest.raises(driver.StagingError, match="command failed"):
        driver._run(
            [sys.executable, "-c", "import sys; print('bounded failure', file=sys.stderr); raise SystemExit(3)"],
            environment=os.environ,
        )
    assert driver._project_name("Request.060_A") == "astral060-request-060_a"
    with pytest.raises(driver.StagingError, match="deployment identity"):
        driver._project_name("x")


def test_main_dispatches_all_commands_and_normalizes_staging_errors(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert driver.main(["validate-fixtures", "--manifest", str(MANIFEST)]) == 0
    assert "fixture_manifest_sha256" in capsys.readouterr().out

    monkeypatch.setattr(driver, "_deploy", lambda args: 7 if args.leave_running else 8)
    assert driver.main(
        [
            "deploy",
            "--candidate-sha",
            "a" * 40,
            "--candidate-source-root",
            str(REPO_ROOT),
            "--candidate-image",
            "ghcr.io/astraldeep/astraldeep@sha256:" + "b" * 64,
            "--fixture-manifest",
            str(MANIFEST),
            "--environment-id",
            "request-060-main",
            "--outputs",
            str(tmp_path / "out.json"),
            *_voice_cli(),
            "--leave-running",
        ]
    ) == 7
    monkeypatch.setattr(
        driver,
        "_write_trusted_manifest_command",
        lambda args: 11 if args.stage_outputs_artifact_id == "7001" else 12,
    )
    assert driver.main(
        [
            "write-trusted-manifest",
            "--candidate-sha",
            "a" * 40,
            "--environment-id",
            "rr-6001-1",
            "--outputs",
            str(tmp_path / "out.json"),
            "--trusted-manifest",
            str(tmp_path / "manifest.json"),
            "--stage-outputs-artifact-id",
            "7001",
            "--stage-outputs-artifact-name",
            "stage-outputs-protected-6001",
            "--stage-outputs-member",
            "staging-outputs.json",
        ]
    ) == 11
    monkeypatch.setattr(driver, "_cleanup", lambda args: 9 if args.environment_id else 10)
    assert driver.main(["cleanup", "--environment-id", "request-060-main"]) == 9

    def reject(_manifest: str) -> dict[str, Any]:
        raise driver.StagingError("normalized fixture rejection")

    monkeypatch.setattr(driver, "validate_fixtures", reject)
    assert driver.main(["validate-fixtures", "--manifest", str(MANIFEST)]) == 2
    assert "candidate staging rejected: normalized fixture rejection" in capsys.readouterr().err


def test_strict_fixture_helpers_reject_malformed_and_secret_bearing_values(
    driver: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(driver.StagingError, match="size"):
        driver._strict_json(empty)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a":1,"a":2}', encoding="utf-8")
    with pytest.raises(driver.StagingError, match="duplicate"):
        driver._strict_json(duplicate)
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"a":NaN}', encoding="utf-8")
    with pytest.raises(driver.StagingError, match="non-finite"):
        driver._strict_json(nonfinite)
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(driver.StagingError, match="one JSON object"):
        driver._strict_json(array)
    with pytest.raises(driver.StagingError, match="secret-bearing"):
        driver._assert_no_secret_values({"nested": [{"access_token": "must-not-appear"}]})

    monkeypatch.setattr(driver, "MAX_JSON_BYTES", 1)
    with pytest.raises(driver.StagingError, match="size"):
        driver._strict_json(MANIFEST)


def test_fixture_manifest_revision_and_version_are_closed_contracts(
    driver: Any, tmp_path: Path
) -> None:
    manifest = tmp_path / "fixture-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(driver.StagingError, match="schema version"):
        driver.validate_fixtures(manifest)
    manifest.write_text(
        json.dumps({"schema_version": 1, "source_schema_revision": "056.001"}),
        encoding="utf-8",
    )
    with pytest.raises(driver.StagingError, match="057.001"):
        driver.validate_fixtures(manifest)


def _load_release_validator() -> Any:
    validator_path = REPO_ROOT / "scripts" / "validate_release_evidence.py"
    spec = importlib.util.spec_from_file_location(
        "candidate_staging_test_release_validator", validator_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _stage_deploy_github_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "AstralDeep/AstralDeep")
    monkeypatch.setenv("GITHUB_WORKFLOW", "release-readiness")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "AstralDeep/AstralDeep/.github/workflows/release-readiness.yml"
        "@refs/heads/060-runtime-reliability-hardening",
    )
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "d" * 40)
    monkeypatch.setenv("RELEASE_TRUSTED_BUILDER_SHA", "d" * 40)
    monkeypatch.setenv(
        "RELEASE_TRUSTED_BUILDER_IDENTITY",
        "https://github.com/AstralDeep/AstralDeep/.github/workflows/"
        "release-trusted-builder.yml@refs/heads/main",
    )


def _fake_docker_deploy(
    driver: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("ASTRAL_ENV=staging\n", encoding="utf-8")
    runtime_env.chmod(0o600)
    protected = _protected_environment(runtime_env)
    monkeypatch.setattr(driver, "_required_environment", lambda **_kwargs: protected)
    monkeypatch.setattr(driver, "_git_identity", lambda _candidate, _root: None)
    capability = {"supported": False, "runtime_contract_versions": [], "source_feature": None}
    monkeypatch.setattr(driver, "_probe", lambda _endpoint, _token: capability)

    def run(
        arguments: list[str],
        *,
        environment: dict[str, str],
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        del input_bytes
        if "config" in arguments and "--format" in arguments:
            output = _rendered_compose_model(environment)
        elif "ps" in arguments and "--format" in arguments:
            images = _expected_service_images(environment)
            output = json.dumps(
                [
                    {
                        "Service": service,
                        "State": "running",
                        "Image": images[service],
                    }
                    for service in (
                        "postgres",
                        "keycloak-postgres",
                        "keycloak",
                        "livekit",
                        "astraldeep",
                        "voice-worker",
                    )
                ]
            ).encode()
        else:
            output = b""
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(driver, "_run", run)


def test_deploy_and_post_upload_manifest_help_list_exact_inputs() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "deploy", "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    for option in (
        "--candidate-source-root",
        "--voice-worker-image",
        "--livekit-image",
        "--livekit-config-sha256",
        "--speech-inventory-sha256",
        "--speech-profile-sha256",
    ):
        assert option in completed.stdout
    manifest = subprocess.run(
        [sys.executable, str(SCRIPT), "write-trusted-manifest", "--help"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    for option in (
        "--trusted-manifest",
        "--stage-outputs-artifact-id",
        "--stage-outputs-artifact-name",
        "--stage-outputs-member",
    ):
        assert option in manifest.stdout


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("voice_worker_image", "voice-worker:latest", "digest-qualified"),
        ("livekit_image", "livekit:latest", "digest-qualified"),
        ("livekit_config_sha256", "not-a-digest", "livekit-config-sha256"),
        ("speech_inventory_sha256", "not-a-digest", "speech-inventory-sha256"),
        ("speech_profile_sha256", "0" * 64, "speech-profile-sha256"),
    ],
)
def test_voice_runtime_inputs_are_explicit_pinned_and_exact(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    _fake_docker_deploy(driver, monkeypatch, tmp_path)
    values = _voice_args()
    values[field] = value
    args = SimpleNamespace(
        leave_running=True,
        candidate_image="ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64,
        candidate_sha="b" * 40,
        candidate_source_root=str(REPO_ROOT),
        fixture_manifest=str(MANIFEST),
        environment_id="request-060-voice-input",
        outputs=str(tmp_path / "staging-outputs.json"),
        trusted_manifest=None,
        **values,
    )
    with pytest.raises(driver.StagingError, match=message):
        driver._deploy(args)


def test_deploy_binds_the_exact_tracked_livekit_config_before_compose(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_docker_deploy(driver, monkeypatch, tmp_path)
    values = _voice_args()
    values["livekit_config_sha256"] = "0" * 64
    args = SimpleNamespace(
        leave_running=True,
        candidate_image="ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64,
        candidate_sha="b" * 40,
        candidate_source_root=str(REPO_ROOT),
        fixture_manifest=str(MANIFEST),
        environment_id="request-060-config-drift",
        outputs=str(tmp_path / "staging-outputs.json"),
        trusted_manifest=None,
        **values,
    )
    with pytest.raises(driver.StagingError, match="tracked staging configuration"):
        driver._deploy(args)


def test_rendered_compose_identity_rejects_image_and_mount_substitution(
    driver: Any,
    tmp_path: Path,
) -> None:
    environment = {
        "STAGING_PROJECT_NAME": "astral060-compose-identity",
        "STAGING_ENVIRONMENT_ID": "compose-identity",
        "STAGING_RUN_ID": "6001",
        "STAGING_RUN_ATTEMPT": "1",
        "STAGING_BIND_PORT": "18061",
        "ASTRAL_CANDIDATE_IMAGE": (
            "ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64
        ),
        "ASTRAL_VOICE_WORKER_IMAGE": _voice_args()["voice_worker_image"],
        "STAGING_POSTGRES_IMAGE": (
            "registry.invalid/postgres@sha256:" + "1" * 64
        ),
        "STAGING_KEYCLOAK_IMAGE": (
            "registry.invalid/keycloak@sha256:" + "2" * 64
        ),
        "LIVEKIT_TURN_DOMAIN": "turn.stage-060.example.invalid",
    }
    model = json.loads(_rendered_compose_model(environment))
    identity = driver._compose_runtime_identity(
        json.dumps(model).encode(),
        project=environment["STAGING_PROJECT_NAME"],
        expected_images=_expected_service_images(environment),
        livekit_turn_domain=environment["LIVEKIT_TURN_DOMAIN"],
        ownership_labels=_ownership_labels(environment),
        staging_bind_port=environment["STAGING_BIND_PORT"],
    )
    assert identity["livekit_config"]["sha256"] == LIVEKIT_CONFIG_SHA256
    assert identity["astraldeep_host_route"] == {
        "host_ip": "127.0.0.1",
        "target": 8001,
        "published": "18061",
        "protocol": "tcp",
        "mode": "ingress",
    }
    assert identity["images"] == _expected_service_images(environment)
    assert identity["livekit_turn_tls"] == {
        "advertised_uri": "turns:turn.stage-060.example.invalid:443?transport=tcp",
        "public_port": 443,
        "external_tls": True,
        "terminator_upstream_host": "127.0.0.1",
        "terminator_upstream_port": 15349,
        "livekit_listener_port": 5349,
    }

    substituted_image = copy.deepcopy(model)
    substituted_image["services"]["voice-worker"]["image"] = (
        "registry.invalid/substitute@sha256:" + "9" * 64
    )
    with pytest.raises(driver.StagingError, match="approved image"):
        driver._compose_runtime_identity(
            json.dumps(substituted_image).encode(),
            project=environment["STAGING_PROJECT_NAME"],
            expected_images=_expected_service_images(environment),
            livekit_turn_domain=environment["LIVEKIT_TURN_DOMAIN"],
            ownership_labels=_ownership_labels(environment),
            staging_bind_port=environment["STAGING_BIND_PORT"],
        )

    substituted_mount = copy.deepcopy(model)
    replacement = tmp_path / "livekit.yaml"
    replacement.write_text("port: 7880\n", encoding="utf-8")
    substituted_mount["services"]["livekit"]["volumes"][0]["source"] = str(
        replacement
    )
    with pytest.raises(driver.StagingError, match="tracked read-only file"):
        driver._compose_runtime_identity(
            json.dumps(substituted_mount).encode(),
            project=environment["STAGING_PROJECT_NAME"],
            expected_images=_expected_service_images(environment),
            livekit_turn_domain=environment["LIVEKIT_TURN_DOMAIN"],
            ownership_labels=_ownership_labels(environment),
            staging_bind_port=environment["STAGING_BIND_PORT"],
        )

    substituted_route = copy.deepcopy(model)
    substituted_route["services"]["astraldeep"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(driver.StagingError, match="protected loopback binding"):
        driver._compose_runtime_identity(
            json.dumps(substituted_route).encode(),
            project=environment["STAGING_PROJECT_NAME"],
            expected_images=_expected_service_images(environment),
            livekit_turn_domain=environment["LIVEKIT_TURN_DOMAIN"],
            ownership_labels=_ownership_labels(environment),
            staging_bind_port=environment["STAGING_BIND_PORT"],
        )

    substituted_support_image = copy.deepcopy(model)
    substituted_support_image["services"]["keycloak"]["image"] = (
        "registry.invalid/substitute@sha256:" + "8" * 64
    )
    with pytest.raises(driver.StagingError, match="approved image"):
        driver._compose_runtime_identity(
            json.dumps(substituted_support_image).encode(),
            project=environment["STAGING_PROJECT_NAME"],
            expected_images=_expected_service_images(environment),
            livekit_turn_domain=environment["LIVEKIT_TURN_DOMAIN"],
            ownership_labels=_ownership_labels(environment),
            staging_bind_port=environment["STAGING_BIND_PORT"],
        )

    substituted_turn_route = copy.deepcopy(model)
    substituted_turn_route["services"]["livekit"]["ports"][0]["published"] = "443"
    with pytest.raises(driver.StagingError, match="TURN/TLS upstream"):
        driver._compose_runtime_identity(
            json.dumps(substituted_turn_route).encode(),
            project=environment["STAGING_PROJECT_NAME"],
            expected_images=_expected_service_images(environment),
            livekit_turn_domain=environment["LIVEKIT_TURN_DOMAIN"],
            ownership_labels=_ownership_labels(environment),
            staging_bind_port=environment["STAGING_BIND_PORT"],
        )


def test_running_compose_identity_rejects_runtime_image_substitution(
    driver: Any,
) -> None:
    expected_images = {
        "postgres": "registry.invalid/postgres@sha256:" + "3" * 64,
        "keycloak-postgres": "registry.invalid/postgres@sha256:" + "3" * 64,
        "keycloak": "registry.invalid/keycloak@sha256:" + "4" * 64,
        "astraldeep": "registry.invalid/astral@sha256:" + "1" * 64,
        "voice-worker": "registry.invalid/voice@sha256:" + "2" * 64,
        "livekit": PINNED_LIVEKIT_IMAGE,
    }
    records = [
        {
            "Service": service,
            "State": "running",
            "Image": expected_images[service],
        }
        for service in (
            "postgres",
            "keycloak-postgres",
            "keycloak",
            "livekit",
            "astraldeep",
            "voice-worker",
        )
    ]
    assert driver._running_compose_services(
        json.dumps(records).encode(), expected_images=expected_images
    ) == set(expected_images)

    records[-1]["Image"] = "registry.invalid/substitute@sha256:" + "9" * 64
    with pytest.raises(driver.StagingError, match="approved image reference"):
        driver._running_compose_services(
            json.dumps(records).encode(), expected_images=expected_images
        )

    records[-1]["Image"] = expected_images["voice-worker"]
    keycloak_database = next(
        record for record in records if record["Service"] == "keycloak-postgres"
    )
    keycloak_database["State"] = "exited"
    with pytest.raises(driver.StagingError, match="keycloak-postgres"):
        driver._running_compose_services(
            json.dumps(records).encode(), expected_images=expected_images
        )


def _post_upload_manifest_args(output_path: Path, manifest_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        candidate_sha="b" * 40,
        environment_id="rr-6001-1",
        outputs=str(output_path),
        trusted_manifest=str(manifest_path),
        stage_outputs_artifact_id="7001",
        stage_outputs_artifact_name="stage-outputs-protected-6001",
        stage_outputs_member=output_path.name,
    )


def test_post_upload_trusted_manifest_is_schema_valid_and_binds_actual_artifact(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_docker_deploy(driver, monkeypatch, tmp_path)
    _stage_deploy_github_identity(monkeypatch)
    output_path = tmp_path / "outputs" / "staging-outputs.json"
    manifest_path = tmp_path / "outputs" / "trusted-stage-deploy.json"
    args = SimpleNamespace(
        leave_running=True,
        candidate_image="ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64,
        candidate_sha="b" * 40,
        candidate_source_root=str(REPO_ROOT),
        fixture_manifest=str(MANIFEST),
        environment_id="rr-6001-1",
        outputs=str(output_path),
        **_voice_args(),
    )
    with pytest.raises(driver.StagingError, match="no AstralPlane-owned"):
        driver._deploy(args)
    capsys.readouterr()
    assert not output_path.exists()
    assert not manifest_path.exists()


def test_post_upload_manifest_requires_actual_id_stage_job_and_github_identity(
    driver: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert "ASTRAL_STAGE_OUTPUTS_ARTIFACT_ID" not in SCRIPT.read_text(encoding="utf-8")
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("ASTRAL_ENV=staging\n", encoding="utf-8")
    runtime_env.chmod(0o600)
    protected = _protected_environment(runtime_env)
    protected["GITHUB_JOB"] = "producer"
    monkeypatch.setattr(driver, "_required_environment", lambda **_kwargs: protected)
    _stage_deploy_github_identity(monkeypatch)
    manifest_path = tmp_path / "trusted-stage-deploy.json"
    output_path = tmp_path / "staging-outputs.json"
    output_path.write_text(
        json.dumps(
            {
                "environment_id": "rr-6001-1",
                "request_namespace": "astral060-rr-6001-1",
                "deployment_run_id": "6001",
            }
        ),
        encoding="utf-8",
    )
    args = _post_upload_manifest_args(output_path, manifest_path)
    with pytest.raises(driver.StagingError, match="stage-deploy"):
        driver._write_trusted_manifest_command(args)
    assert not manifest_path.exists()

    protected["GITHUB_JOB"] = "stage-deploy"
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(driver.StagingError, match="GITHUB_REPOSITORY"):
        driver._write_trusted_manifest_command(args)
    monkeypatch.setenv("GITHUB_REPOSITORY", "AstralDeep/AstralDeep")
    monkeypatch.delenv("GITHUB_WORKFLOW_SHA", raising=False)
    with pytest.raises(driver.StagingError, match="GITHUB_WORKFLOW_SHA"):
        driver._write_trusted_manifest_command(args)
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", "d" * 40)
    monkeypatch.delenv("RELEASE_TRUSTED_BUILDER_SHA", raising=False)
    with pytest.raises(driver.StagingError, match="RELEASE_TRUSTED_BUILDER_SHA"):
        driver._write_trusted_manifest_command(args)
    monkeypatch.setenv("RELEASE_TRUSTED_BUILDER_SHA", "d" * 40)
    protected["GITHUB_RUN_ATTEMPT"] = "not-a-number"
    with pytest.raises(driver.StagingError, match="GITHUB_RUN_ATTEMPT"):
        driver._write_trusted_manifest_command(args)
    protected["GITHUB_RUN_ATTEMPT"] = "1"
    args.stage_outputs_artifact_id = "0"
    with pytest.raises(driver.StagingError, match="stage-outputs-artifact-id"):
        driver._write_trusted_manifest_command(args)
    assert not manifest_path.exists()


def test_retired_deploy_cannot_create_post_upload_manifest_input(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_docker_deploy(driver, monkeypatch, tmp_path)
    _stage_deploy_github_identity(monkeypatch)
    manifest_path = tmp_path / "trusted-stage-deploy.json"
    output_path = tmp_path / "outputs" / "staging-outputs.json"
    deploy_args = SimpleNamespace(
        leave_running=True,
        candidate_image="ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64,
        candidate_sha="b" * 40,
        candidate_source_root=str(REPO_ROOT),
        fixture_manifest=str(MANIFEST),
        environment_id="rr-6001-1",
        outputs=str(output_path),
        **_voice_args(),
    )
    with pytest.raises(driver.StagingError, match="no AstralPlane-owned"):
        driver._deploy(deploy_args)
    capsys.readouterr()
    assert not output_path.exists()
    assert not manifest_path.exists()


def test_retired_deploy_writes_no_stage_outputs(
    driver: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_docker_deploy(driver, monkeypatch, tmp_path)
    output_path = tmp_path / "outputs" / "staging-outputs.json"
    args = SimpleNamespace(
        leave_running=True,
        candidate_image="ghcr.io/astraldeep/astraldeep@sha256:" + "c" * 64,
        candidate_sha="b" * 40,
        candidate_source_root=str(REPO_ROOT),
        fixture_manifest=str(MANIFEST),
        environment_id="request-060-1",
        outputs=str(output_path),
        **_voice_args(),
    )
    with pytest.raises(driver.StagingError, match="no AstralPlane-owned"):
        driver._deploy(args)
    capsys.readouterr()
    assert not output_path.exists()
