"""Runs scripts/node/install-personal-node.sh against a temporary home: files and modes, no secrets printed, safe to rerun, removed sign-in flags refused, a legacy env file left alone, and an env file the production posture accepts."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from shared import node_identity as ni

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "node" / "install-personal-node.sh"
LEGACY_OWNER = "ASTRAL_NODE_OWNER=oidc.0123456789abcdef.owner-sub-1\n"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("openssl") and SCRIPT.exists()),
    reason="installer needs bash and openssl",
)


def run(home, *extra):
    args = ["bash", str(SCRIPT), "--home", str(home)]
    return subprocess.run(args + list(extra), capture_output=True, text=True, check=False)


def env_values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def test_install_creates_private_files_and_a_loadable_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    result = run(home, "--public-url", "https://node.example/", "--database-url", "postgresql://u:p@db/n")
    assert result.returncode == 0, result.stderr
    values = env_values(home / "node.env")
    assert values["ASTRAL_NODE_PROFILE"] == "personal"
    assert not any(name.startswith("KEYCLOAK_") for name in values)
    assert "ASTRAL_NODE_OWNER" not in values
    assert values["PUBLIC_BASE_URL"] == "https://node.example"
    assert "KEYCLOAK_CLIENT_SECRET" not in values
    for name in ("WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET", "AGENT_API_KEY"):
        assert len(values[name]) >= 32
    assert stat.S_IMODE((home / "node.env").stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "data").stat().st_mode) == 0o700
    from cryptography.fernet import Fernet

    Fernet(values["WEB_SESSION_ENC_KEY"].encode())
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, values["ASTRAL_NODE_DATA_DIR"])
    ni.reset_cache()
    key = home / "data" / ni.KEY_FILE
    if key.exists():
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert ni.load_node_identity(create=False).node_id.startswith(ni.NODE_ID_PREFIX)
    ni.reset_cache()


def test_output_never_prints_a_secret_or_database_credentials(tmp_path):
    home = tmp_path / "home"
    result = run(home, "--database-url", "postgresql://user:hunter2@db/n")
    values = env_values(home / "node.env")
    for name in ("WEB_SESSION_ENC_KEY", "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET", "AGENT_API_KEY"):
        assert values[name] not in result.stdout + result.stderr
    assert "hunter2" not in result.stdout + result.stderr


def test_rerun_keeps_every_existing_value_and_adds_only_what_is_missing(tmp_path):
    home = tmp_path / "home"
    run(home)
    before = (home / "node.env").read_text()
    key = (home / "data" / ni.KEY_FILE)
    key_before = key.read_bytes() if key.exists() else None
    again = run(home)
    assert again.returncode == 0
    assert (home / "node.env").read_text() == before
    assert "none, everything was already present" in again.stdout
    if key_before is not None:
        assert key.read_bytes() == key_before
    added = run(home, "--database-url", "postgresql://db/n")
    assert "DATABASE_URL" in added.stdout
    assert env_values(home / "node.env")["DATABASE_URL"] == "postgresql://db/n"


def test_the_written_env_passes_the_production_posture_without_keycloak(tmp_path, monkeypatch):
    from orchestrator.session_store import assert_production_posture

    home = tmp_path / "home"
    assert run(home).returncode == 0
    for name in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET", ni.OWNER_ENV, ni.ISSUERS_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env_values(home / "node.env").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ASTRAL_ENV", "production")
    ni.reset_cache()
    try:
        assert_production_posture()
    finally:
        ni.reset_cache()


@pytest.mark.parametrize(
    "flag, replacement",
    [
        (["--issuer", "https://idp.example/realms/astral"], "Keycloak"),
        (["--issuer=https://idp.example/realms/astral"], "Keycloak"),
        (["--client-id", "astral-app"], "Keycloak"),
        (["--client-id=astral-app"], "Keycloak"),
        (["--owner-subject", "owner-sub-1"], "mesh you pair"),
        (["--owner-subject=owner-sub-1"], "mesh you pair"),
    ],
)
def test_removed_sign_in_flags_are_refused_and_name_their_replacement(tmp_path, flag, replacement):
    result = run(tmp_path / "home", *flag)
    assert result.returncode == 2
    assert flag[0].split("=")[0] + " was removed" in result.stderr
    assert replacement in result.stderr
    assert not (tmp_path / "home").exists()


def test_an_existing_legacy_owner_env_file_is_left_byte_identical(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = home / "node.env"
    legacy = "KEYCLOAK_AUTHORITY=https://idp.example/realms/astral\nKEYCLOAK_CLIENT_ID=astral-app\n" + LEGACY_OWNER
    complete = legacy + "".join(
        f"{name}=v\n"
        for name in (
            "ASTRAL_NODE_PROFILE", "ASTRAL_NODE_DATA_DIR", "WEB_SESSION_ENC_KEY",
            "CREDENTIAL_ENCRYPTION_KEY", "AUDIT_HMAC_SECRET", "AGENT_API_KEY",
        )
    )
    env.write_text(complete)
    result = run(home)
    assert result.returncode == 0, result.stderr
    assert env.read_bytes() == complete.encode()
    assert "old sign-in owner will be retired when the node starts" in result.stdout
    assert "refuses" not in result.stdout
    assert "oidc.0123456789abcdef" not in result.stdout


def test_a_legacy_owner_file_gets_the_missing_settings_added_and_the_owner_line_kept(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "node.env").write_text(LEGACY_OWNER)
    result = run(home)
    assert result.returncode == 0
    assert (home / "node.env").read_text().startswith(LEGACY_OWNER)
    assert env_values(home / "node.env")["ASTRAL_NODE_PROFILE"] == "personal"
    assert "old sign-in owner will be retired when the node starts" in result.stdout
    assert "refuses" not in result.stdout


def test_a_mesh_owner_line_is_not_reported_as_legacy(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "node.env").write_text("ASTRAL_NODE_OWNER=sha256:" + "a" * 64 + "\n")
    assert "legacy" not in run(home).stdout


def test_next_steps_describe_pairing_without_a_sign_in(tmp_path):
    out = run(tmp_path / "home").stdout
    assert "POST /api/node/mesh/offer" in out
    assert "Astral-Pairing-Secret" in out
    assert "pairing-secret" in out
    assert "Sign in" not in out and "issuer" not in out


def test_next_steps_name_the_pack_launcher_and_no_container(tmp_path):
    out = run(tmp_path / "home").stdout
    assert "astral_node_launch.py" in out
    assert "docker" not in out.lower()


def test_unknown_options_and_help(tmp_path):
    bad = subprocess.run(["bash", str(SCRIPT), "--nope"], capture_output=True, text=True, check=False)
    assert bad.returncode == 2
    helped = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert helped.returncode == 0 and "--issuer" not in helped.stdout and "--home" in helped.stdout
