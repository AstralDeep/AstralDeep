"""Contract tests for the feature-065 LiveKit/voice Compose topology."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
if not (REPO_ROOT / "docker-compose.yml").is_file():
    pytest.skip("repository deployment files are absent", allow_module_level=True)

LOCAL_COMPOSE = REPO_ROOT / "docker-compose.yml"
STAGING_COMPOSE = REPO_ROOT / "docker-compose.staging.yml"
INTEGRATION_COMPOSE = REPO_ROOT / "docker-compose.voice-integration.yml"
LIVEKIT_ROOT = REPO_ROOT / "deploy" / "livekit"
PINNED_LIVEKIT = (
    "livekit/livekit-server:v1.13.5@sha256:"
    "3497163e15c48fef6e7830c78716f9e9d5edc28abf7aa90b61c86e93bbc306b1"
)


def _service(document: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|^volumes:\n|\Z)",
        document,
    )
    assert match, f"Compose service is missing: {name}"
    return match.group("body")


def test_livekit_configs_are_secret_free_strict_single_node_profiles() -> None:
    expected = {
        "livekit.local.yaml": ("use_external_ip: false", "enabled: false"),
        "livekit.staging.yaml": (
            "use_external_ip: true",
            "enabled: true",
            "udp_port: 3478",
            "external_tls: true",
            "relay_range_start: 51000",
            "relay_range_end: 51099",
        ),
        "livekit.production.yaml": (
            "use_external_ip: true",
            "enabled: true",
            "udp_port: 3478",
            "external_tls: true",
            "relay_range_start: 51000",
            "relay_range_end: 51099",
        ),
    }
    for name, environment_markers in expected.items():
        path = LIVEKIT_ROOT / name
        assert path.is_file(), f"missing LiveKit profile: {path}"
        content = path.read_text(encoding="utf-8")
        assert "${" not in content, f"{name} must be deterministic configuration bytes"
        assert not re.search(
            r"(?m)^\s*(?:keys|key_file|cert_file|secret|password):",
            content,
        ), f"{name} must not contain credential material"
        assert not re.search(r"(?m)^redis:", content), "launch topology is single-node"
        for marker in (
            "port: 7880",
            "tcp_port: 7881",
            "port_range_start: 50000",
            "port_range_end: 50099",
            "auto_create: false",
            "max_participants: 16",
            "level: warn",
            "pion_level: error",
            *environment_markers,
        ):
            assert marker in content, f"{name} lacks {marker}"
        assert "level: info" not in content
    local = (LIVEKIT_ROOT / "livekit.local.yaml").read_text(encoding="utf-8")
    assert "advertise_internal_ip: false" in local
    assert "allow_restricted_peer_cidrs" not in local
    assert "udp_port:" not in local
    assert "relay_range_" not in local


@pytest.mark.parametrize(
    ("compose_path", "config_name"),
    (
        (LOCAL_COMPOSE, "livekit.local.yaml"),
        (STAGING_COMPOSE, "livekit.staging.yaml"),
    ),
)
def test_compose_pins_livekit_and_keeps_speech_inputs_worker_local(
    compose_path: Path,
    config_name: str,
) -> None:
    document = compose_path.read_text(encoding="utf-8")
    livekit = _service(document, "livekit")
    worker = _service(document, "voice-worker")
    orchestrator = _service(document, "astraldeep")

    assert f"image: {PINNED_LIVEKIT}" in livekit
    assert f"deploy/livekit/{config_name}" in livekit
    assert "/etc/livekit.yaml:ro" in livekit
    assert "--config" in livekit and "/etc/livekit.yaml" in livekit
    assert "LIVEKIT_KEYS:" in livekit
    assert "OPENAI_BASE_URL" not in livekit
    assert "OPENAI_API_KEY" not in livekit
    if compose_path == LOCAL_COMPOSE:
        assert "NODE_IP: ${LIVEKIT_NODE_IP:?" in livekit

    assert "VOICE_SPEECH_BASE_URL: ${OPENAI_BASE_URL:?" in worker
    assert "VOICE_SPEECH_API_KEY: ${OPENAI_API_KEY:?" in worker
    assert 'OPENAI_BASE_URL: ""' in worker
    assert 'OPENAI_API_KEY: ""' in worker
    if compose_path == LOCAL_COMPOSE:
        assert "ASTRAL_ENV: development" in worker
        assert (
            "ASTRAL_VOICE_CONTROL_URL: "
            "ws://astraldeep:8001/api/voice/worker-control"
        ) in worker
        assert "VOICE_WORKER_CLOSURE_SHA256: ${VOICE_WORKER_CLOSURE_SHA256:-000" in worker
    else:
        assert "ASTRAL_ENV: staging" in worker
        assert "ASTRAL_VOICE_CONTROL_URL: ${ASTRAL_VOICE_CONTROL_URL:?" in worker
        assert "VOICE_WORKER_CLOSURE_SHA256: ${VOICE_WORKER_CLOSURE_SHA256:?" in worker
        assert "0000000000000000000000000000000000000000000000000000000000000000" not in worker
    assert "VOICE_WORKER_IDENTITY:" in worker
    assert "VOICE_WORKER_MAX_SESSIONS:" in worker
    assert "VOICE_WATCH_BRIDGE_LISTEN_HOST: 0.0.0.0" in worker
    assert "VOICE_WATCH_BRIDGE_LISTEN_PORT: 7890" in worker
    assert "VOICE_CONTROL_SECRET:" in worker
    assert "VOICE_UI_BINDING_SECRET:" not in worker
    assert "LIVEKIT_URL:" not in worker
    assert "LIVEKIT_API_KEY:" not in worker
    assert "LIVEKIT_API_SECRET:" not in worker
    assert "VOICE_WORKER_BIND:" not in worker
    assert "ports:" in worker
    assert ":7890" in worker
    assert "127.0.0.1:" in worker

    # The existing whole-file env_file remains for the main service, so explicit
    # blank overrides are mandatory to keep deployment speech inputs inert there.
    assert 'OPENAI_BASE_URL: ""' in orchestrator
    assert 'OPENAI_API_KEY: ""' in orchestrator
    assert "VOICE_SPEECH_BASE_URL" not in orchestrator
    assert "VOICE_SPEECH_API_KEY" not in orchestrator
    # Env-overridable with the local plaintext default: production must point
    # this at the LiveKit TLS vhost (https → derived wss) or every session
    # start fails closed with invalid_livekit_url.
    assert (
        "LIVEKIT_INTERNAL_URL: ${LIVEKIT_INTERNAL_URL:-http://livekit:7880}"
        in orchestrator
    )
    assert "LIVEKIT_API_KEY:" in orchestrator
    assert "LIVEKIT_API_SECRET:" in orchestrator
    assert "VOICE_UI_BINDING_SECRET:" in orchestrator
    assert "VOICE_WATCH_BRIDGE_PUBLIC_URL:" in orchestrator


def test_staging_requires_candidate_bound_worker_and_no_literal_credentials() -> None:
    document = STAGING_COMPOSE.read_text(encoding="utf-8")
    livekit = _service(document, "livekit")
    worker = _service(document, "voice-worker")
    assert '"127.0.0.1:7880:7880/tcp"' in livekit
    assert '"127.0.0.1:15349:5349/tcp"' in livekit
    assert '\n      - "5349:5349/tcp"' not in livekit
    assert '"51000-51099:51000-51099/udp"' in livekit
    assert '\n      - "7880:7880/tcp"' not in livekit
    assert "image: ${ASTRAL_VOICE_WORKER_IMAGE:?" in worker
    assert "STAGING_RUNTIME_ENV_FILE" not in worker
    for label in (
        "com.astraldeep.staging.managed",
        "com.astraldeep.staging.project",
        "com.astraldeep.staging.environment-id",
        "com.astraldeep.staging.run-id",
        "com.astraldeep.staging.run-attempt",
    ):
        assert label in document
    # Six services, two named volumes, and the explicit default network all
    # inherit the same protected-run ownership map.
    assert document.count("labels: *staging-ownership") == 9

    for compose_path in (LOCAL_COMPOSE, STAGING_COMPOSE):
        content = compose_path.read_text(encoding="utf-8")
        assert "devkey" not in content
        assert "secret1" not in content
        assert not re.search(r"(?i)(?:sk-|gsk_|xai-|AIza)[A-Za-z0-9_-]{20,}", content)


def test_local_test_profile_is_networkless_and_credential_free() -> None:
    document = LOCAL_COMPOSE.read_text(encoding="utf-8")
    test_worker = _service(document, "voice-worker-test")

    for marker in (
        "profiles: [test]",
        "dockerfile: Dockerfile.voice",
        "target: test",
        "entrypoint: []",
        "network_mode: none",
        "read_only: true",
        "no-new-privileges:true",
        "restart: \"no\"",
    ):
        assert marker in test_worker
    assert "environment:" not in test_worker
    assert "env_file:" not in test_worker
    assert "ports:" not in test_worker
    assert "volumes:" not in test_worker
    assert "depends_on:" not in test_worker


def test_explicit_livekit_integration_lane_is_isolated_and_ephemeral() -> None:
    document = INTEGRATION_COMPOSE.read_text(encoding="utf-8")
    livekit = _service(document, "livekit-integration")
    worker = _service(document, "voice-worker-livekit-integration")
    integration_config = (
        LIVEKIT_ROOT / "livekit.integration.yaml"
    ).read_text(encoding="utf-8")
    runner = (
        REPO_ROOT / "tooling" / "voice-worker" / "run_livekit_integration.py"
    ).read_text(encoding="utf-8")

    assert f"image: {PINNED_LIVEKIT}" in livekit
    assert "deploy/livekit/livekit.integration.yaml" in livekit
    assert "VOICE_INTEGRATION_LIVEKIT_API_KEY:?" in livekit
    assert "VOICE_INTEGRATION_LIVEKIT_API_SECRET:?" in livekit
    assert "ports:" not in livekit
    assert "networks:" in livekit
    assert "target: test" in worker
    assert 'ASTRAL_VOICE_LIVEKIT_INTEGRATION: "1"' in worker
    assert "test_livekit_integration_065.py" in worker
    assert "VOICE_INTEGRATION_WORKER_TOKEN:" in worker
    assert "VOICE_INTEGRATION_CLIENT_TOKEN:" in worker
    assert "VOICE_INTEGRATION_LIVEKIT_API_KEY:" not in worker
    assert "VOICE_INTEGRATION_LIVEKIT_API_SECRET:" not in worker
    assert "network_mode: none" not in worker
    assert "read_only: true" in worker
    assert "no-new-privileges:true" in worker
    assert "internal: true" in document
    assert "auto_create: true" in integration_config
    assert "advertise_internal_ip: true" in integration_config
    assert "level: warn" in integration_config
    assert "${" not in integration_config
    assert "secrets.token_hex(32)" in runner
    assert '"down", "--volumes", "--remove-orphans"' in runner
    assert "subprocess.run" in runner
    runner_main = runner.split("def main() -> int:", maxsplit=1)[1]
    mint_offset = runner_main.index('api_key = "voice_int_"')
    assert runner_main.index("pull = subprocess.run") < mint_offset
    assert runner_main.index("build = subprocess.run") < mint_offset
    assert '"--pull",\n                "never"' in runner_main
    assert "build-placeholder-worker-token" in runner_main

    # The ordinary unit profile remains independently networkless even after
    # the opt-in integration lane is added.
    ordinary_worker = _service(LOCAL_COMPOSE.read_text(encoding="utf-8"), "voice-worker-test")
    assert "network_mode: none" in ordinary_worker
    assert "VOICE_INTEGRATION" not in ordinary_worker


def test_livekit_1135_external_tls_keeps_public_443_and_plaintext_5349_distinct() -> None:
    """Guard the pinned server's advertised-port/listener-port distinction."""

    for name in ("livekit.staging.yaml", "livekit.production.yaml"):
        config = (LIVEKIT_ROOT / name).read_text(encoding="utf-8")
        assert "LiveKit v1.13.5 advertises turns:<domain>:443" in config
        assert "tls_port: 5349" in config
        assert "external_tls: true" in config
        assert "tls_port: 443" not in config

    compose = STAGING_COMPOSE.read_text(encoding="utf-8")
    assert "public port and forwards plaintext to loopback" in compose
    assert '"127.0.0.1:15349:5349/tcp"' in compose
    guide = (LIVEKIT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "turns:<LIVEKIT_TURN_DOMAIN>:443?transport=tcp" in guide
    assert "must not be changed to 443" in guide


def test_operator_guide_documents_reachable_media_and_turn_boundaries() -> None:
    guide = (LIVEKIT_ROOT / "README.md").read_text(encoding="utf-8")
    for marker in (
        PINNED_LIVEKIT,
        "7880/tcp",
        "7881/tcp",
        "50000-50099/udp",
        "3478/udp",
        "443/tcp",
        "5349/tcp",
        "WSS",
        "TURN",
        "OPENAI_BASE_URL",
        "VOICE_SPEECH_BASE_URL",
        "OPENAI_API_KEY",
        "VOICE_SPEECH_API_KEY",
        "VOICE_UI_BINDING_SECRET",
        "LIVEKIT_NODE_IP",
        "vendor logging at `warn`",
        "ephemeral ICE credentials",
        "no audio is retained",
        "Do not commit",
    ):
        assert marker in guide
    assert "localhost is not a valid production advertisement" in guide
