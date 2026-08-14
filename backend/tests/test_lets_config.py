"""Strict, redacted LETS host-configuration and readiness tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.lets_config import (
    INITIAL_GOVERNED_COHORTS,
    LetsConfigError,
    LetsHostConfig,
    load_lets_config,
)

POLICY_DIGEST = "sha256:" + "1" * 64
MACHINE_DIGEST = "sha256:" + "2" * 64


def _authenticate(_raw: bytes, _document: object) -> bool:
    return True


def _write_manifest(path: Path, endpoint: str = "https://warden.example") -> None:
    resources = [{"id": f"scope-{index}", "unit": "call"} for index in range(6)]
    path.write_text(
        json.dumps(
            {
                "api_version": "lets.manifest/v1",
                "tenant_id": "tenant-a",
                "envelope_id": "agents-a",
                "config_epoch": 7,
                "created_at": "2026-08-14T00:00:00Z",
                "resources": resources,
                "initial_budget": [100] * 6,
                "wardens": [
                    {
                        "warden_id": "warden-a",
                        "peer_endpoint": endpoint,
                        "client_endpoint": endpoint,
                        "initial_share": [100] * 6,
                        "keys": [],
                    }
                ],
                "policies": [
                    {
                        "policy_id": "astral",
                        "policy_version": "1",
                        "policy_digest": POLICY_DIGEST,
                        "machine_digest": MACHINE_DIGEST,
                        "resources": resources,
                        "machine": {"machine_digest": MACHINE_DIGEST},
                        "max_lease_ttl_ns": 120_000_000_000,
                    }
                ],
                "signatures": [
                    {
                        "key_id": "operator-a",
                        "algorithm": "Ed25519",
                        "signature": "not-verified-by-the-parser",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _active_environment(
    tmp_path: Path,
    *,
    mode: str = "enforce",
    endpoint: str = "https://warden.example",
    environment: str = "production",
) -> dict[str, str]:
    token = tmp_path / "service-token"
    token.write_bytes(b"\xff\x00opaque-token")
    manifest = tmp_path / "trust-manifest.json"
    _write_manifest(manifest, endpoint)
    executor_db = tmp_path / "executor-db"
    authority = tmp_path / "executor-authority"
    executor_db.mkdir()
    authority.mkdir()
    return {
        "ASTRAL_ENV": environment,
        "FF_LETS_EXTERNAL_WARDEN": "true",
        "LETS_MODE": mode,
        "LETS_GOVERNED_COHORTS": "server_dynamic,byo_user",
        "LETS_GOVERNED_AGENT_ALLOWLIST": "agent-a,agent-b",
        "LETS_WARDEN_URL": endpoint,
        "LETS_SERVICE_TOKEN_FILE": str(token),
        "LETS_TENANT_ID": "tenant-a",
        "LETS_ENVELOPE_ID": "agents-a",
        "LETS_POLICY_DIGEST": POLICY_DIGEST,
        "LETS_MACHINE_DIGEST": MACHINE_DIGEST,
        "LETS_DEFAULT_ALLOCATION": "2,2,2,2,2,2",
        "LETS_DEFAULT_TTL_SECONDS": "60",
        "LETS_REQUEST_TIMEOUT_SECONDS": "2.5",
        "LETS_REQUEST_ATTEMPTS": "2",
        "LETS_SIGNED_TRUST_MANIFEST": str(manifest),
        "LETS_EXECUTOR_INSTANCE_ID": "astral-gateway-a",
        "LETS_EXECUTOR_DB_ROOT": str(executor_db),
        "LETS_EXECUTOR_AUTHORITY_ROOT": str(authority),
    }


def test_off_is_safe_default_and_ignores_dormant_values() -> None:
    loaded = load_lets_config(
        {
            "LETS_MODE": "off",
            "LETS_WARDEN_URL": "not a URL",
            "LETS_SERVICE_TOKEN_FILE": "missing",
        }
    )

    assert loaded.config is not None
    assert loaded.config.master_enabled is False
    assert loaded.config.governed_cohorts == INITIAL_GOVERNED_COHORTS
    assert loaded.readiness.status == "disabled"
    assert loaded.readiness.application_ready is True
    assert loaded.readiness.governed_effects_permitted is True


def test_valid_enforce_configuration_is_bounded_and_redacted(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)
    ca_bundle = tmp_path / "ca.pem"
    client_cert = tmp_path / "client.pem"
    client_key = tmp_path / "client.key"
    for path in (ca_bundle, client_cert, client_key):
        path.write_bytes(b"nonempty")
    values.update(
        {
            "LETS_CA_BUNDLE": str(ca_bundle),
            "LETS_CLIENT_CERT_FILE": str(client_cert),
            "LETS_CLIENT_KEY_FILE": str(client_key),
        }
    )

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)
    config = loaded.config

    assert config is not None
    assert config.mode == "enforce"
    assert config.warden_origin == "https://warden.example"
    assert config.default_allocation == (2, 2, 2, 2, 2, 2)
    assert config.default_ttl_seconds == 60
    assert config.request_timeout_seconds == 2.5
    assert config.request_attempts == 2
    assert config.follow_redirects is False
    assert config.tls_verification_required is True
    assert config.trust_manifest is not None
    assert config.trust_manifest.config_epoch == 7
    assert loaded.readiness.status == "configured"
    assert loaded.readiness.governed_effects_permitted is True

    rendered = repr(config) + repr(config.redacted())
    for sensitive in (
        values["LETS_SERVICE_TOKEN_FILE"],
        values["LETS_CLIENT_KEY_FILE"],
        values["LETS_SIGNED_TRUST_MANIFEST"],
        "opaque-token",
    ):
        assert sensitive not in rendered


@pytest.mark.parametrize("mode", ["", "ENFORCE", " enforce", "audit"])
def test_invalid_mode_is_a_startup_error(mode: str) -> None:
    with pytest.raises(LetsConfigError, match="^invalid_lets_mode$"):
        load_lets_config({"FF_LETS_EXTERNAL_WARDEN": "true", "LETS_MODE": mode})


def test_active_mode_requires_master_flag() -> None:
    with pytest.raises(LetsConfigError, match="^master_flag_disabled$"):
        load_lets_config({"LETS_MODE": "enforce"})
    with pytest.raises(LetsConfigError, match="^invalid_master_flag$"):
        load_lets_config({"FF_LETS_EXTERNAL_WARDEN": "sometimes", "LETS_MODE": "off"})


@pytest.mark.parametrize(
    ("mode", "application_ready", "effects_permitted", "status"),
    [
        ("shadow", True, True, "degraded"),
        ("enforce", False, False, "blocked"),
    ],
)
def test_missing_secret_has_mode_specific_readiness(
    tmp_path: Path,
    mode: str,
    application_ready: bool,
    effects_permitted: bool,
    status: str,
) -> None:
    values = _active_environment(tmp_path, mode=mode)
    secret_path = values["LETS_SERVICE_TOKEN_FILE"]
    Path(secret_path).unlink()

    with pytest.raises(LetsConfigError, match="^invalid_service_token_file$") as caught:
        LetsHostConfig.from_environ(values, authenticate_manifest=_authenticate)
    assert secret_path not in str(caught.value)

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)
    assert loaded.config is None
    assert loaded.readiness.reason == "invalid_service_token_file"
    assert loaded.readiness.status == status
    assert loaded.readiness.application_ready is application_ready
    assert loaded.readiness.governed_effects_permitted is effects_permitted


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://warden.example", "insecure_warden_url"),
        ("https://user:secret@warden.example", "invalid_warden_url"),
        ("https://warden.example/redirect", "invalid_warden_url"),
        ("https://warden.example?next=other", "invalid_warden_url"),
        ("https://warden.example#fragment", "invalid_warden_url"),
    ],
)
def test_production_url_denials_are_value_free(
    tmp_path: Path,
    url: str,
    reason: str,
) -> None:
    values = _active_environment(tmp_path)
    values["LETS_WARDEN_URL"] = url
    loaded = load_lets_config(values, authenticate_manifest=_authenticate)
    assert loaded.config is None
    assert loaded.readiness.reason == reason
    assert url not in repr(loaded.readiness)


def test_explicit_development_allows_http_but_never_redirects(tmp_path: Path) -> None:
    values = _active_environment(
        tmp_path,
        endpoint="http://127.0.0.1:8080",
        environment="development",
    )
    values.pop("LETS_EXECUTOR_AUTHORITY_ROOT")

    config = LetsHostConfig.from_environ(
        values,
        authenticate_manifest=_authenticate,
    )

    assert config.warden_origin == "http://127.0.0.1:8080"
    assert config.executor_authority_root is None
    assert config.follow_redirects is False


def test_production_requires_independent_executor_authority_root(
    tmp_path: Path,
) -> None:
    values = _active_environment(tmp_path)
    values.pop("LETS_EXECUTOR_AUTHORITY_ROOT")

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.config is None
    assert loaded.readiness.reason == "missing_executor_authority_root"
    assert loaded.readiness.status == "blocked"


def test_manifest_must_be_authenticated_and_identity_bound(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)

    denied = load_lets_config(values, authenticate_manifest=lambda _raw, _doc: False)
    assert denied.readiness.reason == "trust_manifest_authentication_failed"

    values["LETS_POLICY_DIGEST"] = "sha256:" + "3" * 64
    mismatched = load_lets_config(values, authenticate_manifest=_authenticate)
    assert mismatched.readiness.reason == "trust_manifest_policy_mismatch"


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        (
            "LETS_GOVERNED_COHORTS",
            "server_dynamic,external",
            "invalid_governed_cohorts",
        ),
        ("LETS_DEFAULT_ALLOCATION", "1,1,1,1,1", "invalid_default_allocation"),
        ("LETS_DEFAULT_ALLOCATION", "0,0,0,0,0,0", "invalid_default_allocation"),
        ("LETS_REQUEST_TIMEOUT_SECONDS", "31", "invalid_request_timeout"),
        ("LETS_REQUEST_ATTEMPTS", "4", "invalid_request_attempts"),
    ],
)
def test_malformed_active_settings_fail_closed(
    tmp_path: Path,
    name: str,
    value: str,
    reason: str,
) -> None:
    values = _active_environment(tmp_path)
    values[name] = value

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.config is None
    assert loaded.readiness.reason == reason
    assert loaded.readiness.governed_effects_permitted is False


def test_mtls_identity_requires_both_files(tmp_path: Path) -> None:
    values = _active_environment(tmp_path)
    cert = tmp_path / "client.pem"
    cert.write_bytes(b"certificate")
    values["LETS_CLIENT_CERT_FILE"] = str(cert)

    loaded = load_lets_config(values, authenticate_manifest=_authenticate)

    assert loaded.readiness.reason == "incomplete_mtls_identity"
    assert str(cert) not in repr(loaded.readiness)
