"""Tests for the production boot gate (orchestrator/auth.py, session_store.py):
mock-auth refusal outside declared dev mode, required production secrets (session
key, audit secret, Keycloak config), and agent-key fail-closed posture.
"""

import uuid

import pytest

from orchestrator.auth import validate_agent_api_key
from orchestrator.session_store import assert_production_posture, is_dev_mode


@pytest.mark.parametrize("posture", [None, "production", "unknown"])
def test_entrypoint_refuses_missing_secrets_before_durable_construction(monkeypatch, posture):
    from orchestrator import orchestrator as entrypoint

    if posture is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", posture)
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)

    def forbidden_runtime():
        pytest.fail("production refusal must precede durable runtime construction")

    monkeypatch.setattr(entrypoint, "Orchestrator", forbidden_runtime)
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 78


@pytest.mark.parametrize("posture", ["development", "production"])
def test_entrypoint_admits_configured_startup(monkeypatch, posture):
    from orchestrator import orchestrator as entrypoint

    monkeypatch.setenv("ASTRAL_ENV", posture)
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    _configure_production_secrets(monkeypatch)
    observed = []

    class Runtime:
        def __init__(self):
            observed.append("constructed")

        async def start(self):
            observed.append("started")

    monkeypatch.setattr(entrypoint, "Orchestrator", Runtime)
    entrypoint.main()
    assert observed == ["constructed", "started"]


def _configure_production_secrets(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "x" * 44)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", f"high-entropy-{uuid.uuid4()}")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/r")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", f"secret-{uuid.uuid4()}")


def test_mock_auth_with_env_unset_refuses_boot(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_mock_auth_in_production_refuses_boot(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_mock_auth_in_development_boots(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert_production_posture()


def test_real_auth_with_env_unset_boots(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    _configure_production_secrets(monkeypatch)
    assert_production_posture()


def test_mock_auth_unset_entirely_boots(monkeypatch):
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    _configure_production_secrets(monkeypatch)
    assert_production_posture()


def test_production_without_session_key_refuses(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    _configure_production_secrets(monkeypatch)
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_production_with_placeholder_audit_secret_refuses(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "dev-audit-hmac-secret-change-me-in-prod")
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_production_without_keycloak_config_refuses(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    for var in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_AUTHORITY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_dev_mode_skips_production_secret_checks(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    for var in ("WEB_SESSION_ENC_KEY", "OFFLINE_GRANT_ENC_KEY",
                "AUDIT_HMAC_SECRET", "KEYCLOAK_AUTHORITY"):
        monkeypatch.delenv(var, raising=False)
    assert_production_posture()


def test_production_without_credential_key_refuses(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_production_with_weak_agent_key_refuses(monkeypatch):
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    monkeypatch.setenv("AGENT_API_KEY", "short")
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_agent_key_unset_in_dev_mode_allows(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert validate_agent_api_key("anything") is True


def test_agent_key_unset_outside_dev_mode_refuses(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert validate_agent_api_key("anything") is False
    assert validate_agent_api_key("") is False


def test_agent_key_matching_allows(monkeypatch):
    key = f"agent-key-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_API_KEY", key)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert validate_agent_api_key(key) is True


def test_agent_key_mismatch_refuses(monkeypatch):
    key = f"agent-key-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_API_KEY", key)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert validate_agent_api_key("wrong-key") is False
    assert validate_agent_api_key("") is False
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert validate_agent_api_key("wrong-key") is False


def test_is_dev_mode_true_values(monkeypatch):
    for value in ("development", "dev", "Development", "DEV", "  dev  "):
        monkeypatch.setenv("ASTRAL_ENV", value)
        assert is_dev_mode() is True, value


def test_is_dev_mode_false_values(monkeypatch):
    for value in ("production", "", "prod", "staging", "true"):
        monkeypatch.setenv("ASTRAL_ENV", value)
        assert is_dev_mode() is False, value


def test_is_dev_mode_unset_is_production(monkeypatch):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert is_dev_mode() is False
