"""Verifies the installation doctor's production-posture evaluation agrees with the
real fail-closed boot gate across development, production, placeholder, and mock-auth
scenarios, so the doctor never contradicts the documented exit-78 posture."""

from __future__ import annotations

import base64
import importlib.util
import secrets
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "install_doctor.py"

if not SCRIPT.is_file():
    pytest.skip(
        "repo-root tooling files are not part of the product image",
        allow_module_level=True,
    )

spec = importlib.util.spec_from_file_location("install_doctor_posture_drift", SCRIPT)
assert spec is not None and spec.loader is not None
doctor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = doctor
spec.loader.exec_module(doctor)

from orchestrator.boot_posture import assert_production_posture  # noqa: E402

POSTURE_VARIABLES = (
    "ASTRAL_ENV",
    "USE_MOCK_AUTH",
    "ASTRAL_NODE_PROFILE",
    "TYPESAFE_API_KEY",
    "TYPESAFE_BASE_URL",
    "TYPESAFE_DEFAULT_MODEL",
    "WEB_SESSION_ENC_KEY",
    "OFFLINE_GRANT_ENC_KEY",
    "CREDENTIAL_ENCRYPTION_KEY",
    "AUDIT_HMAC_SECRET",
    "KEYCLOAK_AUTHORITY",
    "KEYCLOAK_CLIENT_ID",
    "KEYCLOAK_CLIENT_SECRET",
    "AGENT_API_KEY",
)

PRODUCTION_BASE = {
    "ASTRAL_ENV": "production",
    "ASTRAL_NODE_PROFILE": "personal",
    "CREDENTIAL_ENCRYPTION_KEY": "",
    "AUDIT_HMAC_SECRET": "",
    "AGENT_API_KEY": "",
}


@pytest.fixture(autouse=True)
def _authentication_values(monkeypatch):
    for name in ('PRODUCTION_BASE',):
        values = dict(globals()[name])
        for field in ('AGENT_API_KEY', 'AUDIT_HMAC_SECRET', 'CREDENTIAL_ENCRYPTION_KEY', 'DB_PASSWORD', 'KEYCLOAK_CLIENT_SECRET', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET', 'OPENAI_API_KEY', 'VOICE_CONTROL_SECRET', 'WEB_SESSION_ENC_KEY'):
            if field not in values:
                continue
            values[field] = (
                base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
                if field in {"WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY"}
                else secrets.token_urlsafe(32)
            )
        monkeypatch.setitem(globals(), name, values)


def _apply(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name in POSTURE_VARIABLES:
        if name in env:
            monkeypatch.setenv(name, env[name])
        else:
            monkeypatch.delenv(name, raising=False)


def _boot_refuses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> bool:
    _apply(monkeypatch, env)
    try:
        assert_production_posture()
    except SystemExit as exc:
        assert exc.code == 78
        return True
    return False


def _assert_agreement(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    refuses = _boot_refuses(monkeypatch, env)
    problems, _advisories = doctor.evaluate_posture(env)
    assert refuses == bool(problems), (refuses, problems)


def test_production_with_every_secret_configured_boots_and_is_ready(monkeypatch):
    env = dict(PRODUCTION_BASE)
    _assert_agreement(monkeypatch, env)
    assert not _boot_refuses(monkeypatch, env)
    problems, advisories = doctor.evaluate_posture(env)
    assert problems == [] and advisories == []


def test_mock_auth_outside_development_refuses_boot(monkeypatch):
    env = dict(PRODUCTION_BASE)
    env["USE_MOCK_AUTH"] = "true"
    _assert_agreement(monkeypatch, env)


def test_placeholder_audit_secret_refuses_boot(monkeypatch):
    env = dict(PRODUCTION_BASE)
    env["AUDIT_HMAC_SECRET"] = "dev-audit-hmac-secret-change-me-in-prod"
    _assert_agreement(monkeypatch, env)


def test_missing_credential_encryption_key_refuses_boot(monkeypatch):
    env = dict(PRODUCTION_BASE)
    env["CREDENTIAL_ENCRYPTION_KEY"] = ""
    _assert_agreement(monkeypatch, env)


def test_short_agent_key_refuses_boot(monkeypatch):
    env = dict(PRODUCTION_BASE)
    env["AGENT_API_KEY"] = "change-me"
    _assert_agreement(monkeypatch, env)


def test_unset_agent_key_warns_but_boots(monkeypatch):
    env = dict(PRODUCTION_BASE)
    del env["AGENT_API_KEY"]
    _assert_agreement(monkeypatch, env)
    problems, advisories = doctor.evaluate_posture(env)
    assert problems == []
    assert any("AGENT_API_KEY" in advisory for advisory in advisories)


def test_typesafe_environment_refuses_production_boot(monkeypatch):
    env = dict(PRODUCTION_BASE)
    env["TYPESAFE_API_KEY"] = "ts-key-value-0001"
    _assert_agreement(monkeypatch, env)


def test_unset_astral_env_defaults_to_production_and_refuses(monkeypatch):
    env: dict[str, str] = {}
    _assert_agreement(monkeypatch, env)


def test_development_ignores_removed_mock_auth_with_an_advisory(monkeypatch):
    env = {
        "ASTRAL_ENV": "development", "ASTRAL_NODE_PROFILE": "personal",
        "USE_MOCK_AUTH": "true",
        "AUDIT_HMAC_SECRET": "dev-audit-hmac-secret-change-me-in-prod",
    }
    _assert_agreement(monkeypatch, env)
    problems, advisories = doctor.evaluate_posture(env)
    assert problems == []
    assert any("mock auth was removed" in advisory for advisory in advisories)


def test_development_typesafe_variables_stay_advisory(monkeypatch):
    env = {
        "ASTRAL_ENV": "development", "ASTRAL_NODE_PROFILE": "personal",
        "TYPESAFE_BASE_URL": "https://typesafe.example",
    }
    _assert_agreement(monkeypatch, env)
    problems, advisories = doctor.evaluate_posture(env)
    assert problems == []
    assert any("TYPESAFE_BASE_URL" in advisory for advisory in advisories)


def test_development_with_real_auth_boots_without_keycloak(monkeypatch):
    env = {"ASTRAL_ENV": "development", "ASTRAL_NODE_PROFILE": "personal", "USE_MOCK_AUTH": "false"}
    _assert_agreement(monkeypatch, env)
    problems, _advisories = doctor.evaluate_posture(env)
    assert problems == []


def test_dev_alias_posture_matches_the_boot_gate(monkeypatch):
    env = dict(PRODUCTION_BASE)
    env["ASTRAL_ENV"] = "dev"
    _assert_agreement(monkeypatch, env)
    problems, _advisories = doctor.evaluate_posture(env)
    assert problems == []


def test_a_leftover_identity_setting_is_advisory_never_required():
    env = dict(PRODUCTION_BASE, KEYCLOAK_AUTHORITY="https://idp.example/realms/r", WEB_SESSION_ENC_KEY="x")
    env.update(CREDENTIAL_ENCRYPTION_KEY="c" * 44, AUDIT_HMAC_SECRET="a" * 40, AGENT_API_KEY="k" * 40)
    problems, advisories = doctor.evaluate_posture(env)
    assert problems == []
    assert any("KEYCLOAK_AUTHORITY" in advisory and "unused" in advisory for advisory in advisories)


def test_a_missing_or_other_profile_is_a_problem():
    for profile in ("", "server", "institution"):
        problems, _ = doctor.evaluate_posture(dict(PRODUCTION_BASE, ASTRAL_NODE_PROFILE=profile))
        assert any("ASTRAL_NODE_PROFILE" in problem for problem in problems)
