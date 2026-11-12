"""Covers the personal-profile startup posture in orchestrator/session_store.py: no Keycloak client secret needed, a node key created or refused, and exit 78 for everything still missing."""

from __future__ import annotations

import stat
import uuid

import pytest

from orchestrator.session_store import assert_production_posture
from shared import node_identity as ni


@pytest.fixture
def production(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "x" * 44)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", f"high-entropy-{uuid.uuid4()}")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/r")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    monkeypatch.delenv(ni.ISSUERS_ENV, raising=False)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "node"))
    ni.reset_cache()
    yield tmp_path / "node"
    ni.reset_cache()


def test_personal_node_boots_without_a_client_secret_and_creates_its_key(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    assert_production_posture()
    assert stat.S_IMODE((production / ni.KEY_FILE).stat().st_mode) == 0o600


def test_the_shared_deployment_still_requires_the_client_secret(production, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        assert_production_posture()
    assert exit_info.value.code == 78
    assert not production.exists()


@pytest.mark.parametrize("missing", ["WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_AUTHORITY"])
def test_personal_node_still_fails_closed_on_every_other_missing_setting(production, monkeypatch, missing):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit) as exit_info:
        assert_production_posture()
    assert exit_info.value.code == 78


def test_personal_node_refuses_mock_auth_and_placeholder_secrets(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv("USE_MOCK_AUTH", "true")
    with pytest.raises(SystemExit):
        assert_production_posture()
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "change-me")
    with pytest.raises(SystemExit):
        assert_production_posture()


def test_personal_node_refuses_an_unusable_key_directory(production, monkeypatch, tmp_path):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    (tmp_path / "blocker").write_text("x")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "blocker" / "node"))
    with pytest.raises(SystemExit) as exit_info:
        assert_production_posture()
    assert exit_info.value.code == 78


def test_personal_node_refuses_a_world_readable_key(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    assert_production_posture()
    ni.reset_cache()
    (production / ni.KEY_FILE).chmod(0o644)
    with pytest.raises(SystemExit):
        assert_production_posture()


def test_personal_node_needs_an_accepted_issuer_and_a_well_formed_owner(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.OWNER_ENV, "not-an-owner-id")
    with pytest.raises(SystemExit):
        assert_production_posture()
    monkeypatch.setenv(ni.OWNER_ENV, ni.canonical_owner_id("https://idp.example/realms/r", "owner"))
    assert_production_posture()


def test_development_posture_does_not_touch_the_node_directory(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert_production_posture()
    assert not production.exists()
