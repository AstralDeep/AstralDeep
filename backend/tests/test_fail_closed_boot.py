"""Feature 028 (workspace-auth-revival) — fail-closed posture tests.

FR-015: mock authentication refused outside explicitly declared development
mode (boot gate exits with EX_CONFIG). FR-016: agent/automation connections
fail closed when AGENT_API_KEY is unset outside dev mode, replacing the
pre-028 fail-open behavior. ``is_dev_mode`` is the shared posture primitive:
unset/unknown ASTRAL_ENV means production.

Production hardening extended the boot gate beyond mock auth: a
production-mode start now also requires a session encryption key, a real
(non-placeholder) AUDIT_HMAC_SECRET, and the KEYCLOAK_* client config.
"""
import uuid

import pytest

from orchestrator.auth import validate_agent_api_key
from orchestrator.session_store import assert_production_posture, is_dev_mode


@pytest.mark.parametrize("posture", [None, "production", "unknown"])
def test_entrypoint_refuses_missing_secrets_before_durable_construction(monkeypatch, posture):
    """An unreachable database must not mask the production configuration error."""
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
    """The early gate preserves development and explicitly configured production."""
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
    """Minimal valid production config for the extended boot gate."""
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "x" * 44)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", f"high-entropy-{uuid.uuid4()}")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/r")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", f"secret-{uuid.uuid4()}")


# ---------------------------------------------------------------------------
# assert_production_posture — boot gate (FR-015)
# ---------------------------------------------------------------------------

def test_mock_auth_with_env_unset_refuses_boot(monkeypatch):
    """028 FR-015: mock auth on + ASTRAL_ENV unset (default = production)
    must fail fast with SystemExit(78) (EX_CONFIG)."""
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_mock_auth_in_production_refuses_boot(monkeypatch):
    """028 FR-015: mock auth on + ASTRAL_ENV=production must fail fast."""
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_mock_auth_in_development_boots(monkeypatch):
    """028 FR-015: explicitly declared development mode keeps mock auth
    usable — no raise."""
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert_production_posture()  # must not raise


def test_real_auth_with_env_unset_boots(monkeypatch):
    """028 FR-015 + hardening: with mock auth off and the production secrets
    configured, ASTRAL_ENV may stay unset — the gate passes."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    _configure_production_secrets(monkeypatch)
    assert_production_posture()  # must not raise


def test_mock_auth_unset_entirely_boots(monkeypatch):
    """028 FR-015: USE_MOCK_AUTH absent counts as mock-off — no raise
    when the production secrets are configured."""
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    _configure_production_secrets(monkeypatch)
    assert_production_posture()  # must not raise


def test_production_without_session_key_refuses(monkeypatch):
    """Hardening: a production boot without any session encryption key is
    refused (sessions must never hit disk unencrypted)."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    _configure_production_secrets(monkeypatch)
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_production_with_placeholder_audit_secret_refuses(monkeypatch):
    """Hardening: the shipped dev AUDIT_HMAC_SECRET placeholder is refused in
    production (the audit hash chain would be forgeable)."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "dev-audit-hmac-secret-change-me-in-prod")
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_production_without_keycloak_config_refuses(monkeypatch):
    """Hardening: production with mock off but no KEYCLOAK_* cannot serve a
    sign-in at all — refused with EX_CONFIG."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    for var in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_AUTHORITY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_dev_mode_skips_production_secret_checks(monkeypatch):
    """Hardening keeps spec A13: development mode boots with no secrets at
    all (the dev carve-outs cover encryption + audit placeholders)."""
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    for var in ("WEB_SESSION_ENC_KEY", "OFFLINE_GRANT_ENC_KEY",
                "AUDIT_HMAC_SECRET", "KEYCLOAK_AUTHORITY"):
        monkeypatch.delenv(var, raising=False)
    assert_production_posture()  # must not raise


def test_production_without_credential_key_refuses(monkeypatch):
    """Hardening: a production boot without CREDENTIAL_ENCRYPTION_KEY is refused
    — OAuth/Fernet credentials must not fall back to an auto-generated key that
    is lost on an ephemeral volume (silent fail-open)."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


def test_production_with_weak_agent_key_refuses(monkeypatch):
    """Hardening: a shipped-placeholder or too-short AGENT_API_KEY is refused in
    production (a forgeable agent registration secret)."""
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("ASTRAL_ENV", "production")
    _configure_production_secrets(monkeypatch)
    monkeypatch.setenv("AGENT_API_KEY", "short")
    with pytest.raises(SystemExit) as exc:
        assert_production_posture()
    assert exc.value.code == 78


# ---------------------------------------------------------------------------
# validate_agent_api_key — A2A fail-closed (FR-016)
# ---------------------------------------------------------------------------

def test_agent_key_unset_in_dev_mode_allows(monkeypatch):
    """028 FR-016: keyless local dev remains supported — unset AGENT_API_KEY
    with ASTRAL_ENV=development accepts the connection."""
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert validate_agent_api_key("anything") is True


def test_agent_key_unset_outside_dev_mode_refuses(monkeypatch):
    """028 FR-016 (THE fail-closed change): unset AGENT_API_KEY with
    ASTRAL_ENV unset (default = production) refuses the connection.
    Pre-028 this returned True, silently allowing unauthenticated agents."""
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert validate_agent_api_key("anything") is False
    assert validate_agent_api_key("") is False


def test_agent_key_matching_allows(monkeypatch):
    """028 FR-016: a configured key accepts only the exact matching key
    (works without any ASTRAL_ENV declaration)."""
    key = f"agent-key-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_API_KEY", key)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert validate_agent_api_key(key) is True


def test_agent_key_mismatch_refuses(monkeypatch):
    """028 FR-016: a configured key refuses a wrong/empty presented key,
    even in development mode."""
    key = f"agent-key-{uuid.uuid4()}"
    monkeypatch.setenv("AGENT_API_KEY", key)
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert validate_agent_api_key("wrong-key") is False
    assert validate_agent_api_key("") is False
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert validate_agent_api_key("wrong-key") is False


# ---------------------------------------------------------------------------
# is_dev_mode — shared posture primitive (FR-015/FR-016)
# ---------------------------------------------------------------------------

def test_is_dev_mode_true_values(monkeypatch):
    """028 FR-015/FR-016: only explicit 'development'/'dev' declare dev mode
    (case-insensitive, whitespace-tolerant)."""
    for value in ("development", "dev", "Development", "DEV", "  dev  "):
        monkeypatch.setenv("ASTRAL_ENV", value)
        assert is_dev_mode() is True, value


def test_is_dev_mode_false_values(monkeypatch):
    """028 FR-015/FR-016: production / empty / unknown values (including
    'prod', which is NOT in the allow-list) all mean production — every
    posture check fails closed by default."""
    for value in ("production", "", "prod", "staging", "true"):
        monkeypatch.setenv("ASTRAL_ENV", value)
        assert is_dev_mode() is False, value


def test_is_dev_mode_unset_is_production(monkeypatch):
    """028 FR-015/FR-016: unset ASTRAL_ENV defaults to production."""
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert is_dev_mode() is False
