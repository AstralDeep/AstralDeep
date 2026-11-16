"""Covers the personal-profile startup posture in orchestrator/session_store.py: no Keycloak setting or accepted issuer needed, a mesh id as the only configurable owner, a node key created or refused, and exit 78 for everything still missing."""

from __future__ import annotations

import logging
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


KEYCLOAK = ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET")
MESH = "sha256:" + "a" * 64


@pytest.fixture
def personal(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    for name in KEYCLOAK:
        monkeypatch.delenv(name, raising=False)
    return production


def refused(caplog):
    with pytest.raises(SystemExit) as stopped:
        assert_production_posture()
    assert stopped.value.code == 78
    return caplog.text


def test_a_personal_node_passes_production_posture_with_no_keycloak_setting_or_issuer(personal, monkeypatch):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    assert_production_posture()
    monkeypatch.setenv("ASTRAL_ENV", "production")
    assert_production_posture()
    assert stat.S_IMODE((personal / ni.KEY_FILE).stat().st_mode) == 0o600


@pytest.mark.parametrize("missing", KEYCLOAK)
def test_the_server_profile_still_requires_each_keycloak_setting(production, monkeypatch, missing, caplog):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", f"secret-{uuid.uuid4()}")
    assert_production_posture()
    monkeypatch.delenv(missing)
    assert f"{missing} is unset" in refused(caplog)


@pytest.mark.parametrize("owner", [
    "oidc.0123456789abcdef.owner", "not-an-owner-id", "sha256:" + "A" * 64, "mesh." + "a" * 63, "mesh:" + "a" * 64,
])
@pytest.mark.parametrize("environment", [None, "production", "development"])
def test_a_configured_owner_that_is_not_a_mesh_id_exits_78_in_every_posture(personal, monkeypatch, caplog, owner, environment):
    if environment is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", environment)
    monkeypatch.setenv(ni.OWNER_ENV, owner)
    caplog.set_level(logging.ERROR, logger="orchestrator.session_store")
    assert f"{ni.OWNER_ENV} must be the owner's mesh id" in refused(caplog)
    assert not (personal / ni.OWNER_FILE).exists()


@pytest.mark.parametrize("owner", [MESH, "mesh." + "a" * 64])
@pytest.mark.parametrize("environment", [None, "development"])
def test_a_configured_mesh_owner_in_either_form_passes(personal, monkeypatch, owner, environment):
    if environment is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", environment)
    monkeypatch.setenv(ni.OWNER_ENV, owner)
    assert_production_posture()
    assert not (personal / ni.OWNER_FILE).exists()


def test_a_legacy_owner_file_is_not_a_boot_failure_and_is_left_alone(personal, monkeypatch):
    from tests.helpers.owner_file import seed_owner

    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    stored = seed_owner("oidc.0123456789abcdef.owner")
    assert_production_posture()
    assert (personal / ni.OWNER_FILE).read_bytes() == stored


@pytest.mark.parametrize("name,value", [
    ("AUDIT_HMAC_SECRET", "dev-audit-hmac-secret-change-me-in-prod"),
    ("AUDIT_HMAC_SECRET", "change-me"),
    ("AGENT_API_KEY", "change-me"),
    ("AGENT_API_KEY", "short"),
])
def test_a_personal_node_with_a_placeholder_secret_and_no_environment_exits_78(personal, monkeypatch, caplog, name, value):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv(name, value)
    assert f"{name} is" in refused(caplog)


@pytest.mark.parametrize("missing", ["WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET"])
def test_a_personal_node_with_a_missing_secret_and_no_environment_exits_78(personal, monkeypatch, caplog, missing):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.delenv(missing)
    assert missing in refused(caplog)


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


@pytest.mark.parametrize("missing", ["WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET"])
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


def test_development_personal_node_creates_the_key_before_dispatch(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert_production_posture()
    assert stat.S_IMODE((production / ni.KEY_FILE).stat().st_mode) == 0o600
    ni.reset_cache()
    identity = ni.load_node_identity(create=False)
    assert identity.node_id


def test_development_personal_node_refuses_an_existing_unusable_key(production, monkeypatch):
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    production.mkdir()
    key = production / ni.KEY_FILE
    key.write_text("corrupt")
    key.chmod(0o600)
    with pytest.raises(SystemExit) as stopped:
        assert_production_posture()
    assert stopped.value.code == 78 and key.read_text() == "corrupt"


def test_development_shared_posture_does_not_create_a_node_key(production, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    monkeypatch.setenv("ASTRAL_ENV", "development")
    assert_production_posture()
    assert not production.exists()


def test_a_bind_host_that_is_not_an_address_refuses_to_start(production, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOST", "not a host")
    with pytest.raises(SystemExit) as stopped:
        assert_production_posture()
    assert stopped.value.code == 78


def test_the_bind_host_defaults_by_profile_and_can_be_set(monkeypatch):
    monkeypatch.delenv(ni.HOST_ENV, raising=False)
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    assert ni.bind_host() == "0.0.0.0"
    monkeypatch.setenv(ni.PROFILE_ENV, "institution")
    assert ni.bind_host() == "0.0.0.0"
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    assert ni.bind_host() == "127.0.0.1"
    monkeypatch.setenv(ni.HOST_ENV, " localhost ")
    assert ni.bind_host() == "localhost"
    monkeypatch.setenv(ni.HOST_ENV, "10.0.0.7")
    assert ni.bind_host() == "10.0.0.7"
    monkeypatch.setenv(ni.HOST_ENV, "example.com")
    with pytest.raises(ni.NodeIdentityError):
        ni.bind_host()
