"""Runs scripts/node/install-personal-node.sh against a temporary home: files and modes, no secrets printed, safe to rerun, and a key and owner binding the node accepts."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from shared import node_identity as ni

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "node" / "install-personal-node.sh"
ISSUER = "https://idp.example/realms/astral"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("openssl") and SCRIPT.exists()),
    reason="installer needs bash and openssl",
)


def run(home, *extra, issuer=ISSUER, client="astral-app"):
    args = ["bash", str(SCRIPT), "--home", str(home)]
    if issuer is not None:
        args += ["--issuer", issuer]
    if client is not None:
        args += ["--client-id", client]
    return subprocess.run(args + list(extra), capture_output=True, text=True, check=False)


def env_values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def test_install_creates_private_files_and_a_loadable_key(tmp_path, monkeypatch):
    home = tmp_path / "home"
    result = run(home, "--public-url", "https://node.example/", "--database-url", "postgresql://u:p@db/n")
    assert result.returncode == 0, result.stderr
    values = env_values(home / "node.env")
    assert values["ASTRAL_NODE_PROFILE"] == "personal"
    assert values["KEYCLOAK_AUTHORITY"] == ISSUER
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
    again = run(home, issuer="https://another.example/realms/x", client="other-client")
    assert again.returncode == 0
    assert (home / "node.env").read_text() == before
    assert "none, everything was already present" in again.stdout
    if key_before is not None:
        assert key.read_bytes() == key_before
    added = run(home, "--database-url", "postgresql://db/n")
    assert "DATABASE_URL" in added.stdout
    assert env_values(home / "node.env")["DATABASE_URL"] == "postgresql://db/n"


def test_owner_binding_matches_the_node_owner_id(tmp_path):
    home = tmp_path / "home"
    run(home, "--owner-subject", "owner-sub-1")
    assert env_values(home / "node.env")["ASTRAL_NODE_OWNER"] == ni.canonical_owner_id(ISSUER, "owner-sub-1")


def test_a_trailing_slash_on_the_issuer_is_dropped(tmp_path):
    home = tmp_path / "home"
    run(home, issuer=ISSUER + "/")
    assert env_values(home / "node.env")["KEYCLOAK_AUTHORITY"] == ISSUER


@pytest.mark.parametrize("missing", ["issuer", "client"])
def test_required_options_are_enforced(tmp_path, missing):
    result = run(tmp_path / "home", issuer=None if missing == "issuer" else ISSUER, client=None if missing == "client" else "c")
    assert result.returncode == 2
    assert not (tmp_path / "home").exists()


def test_unknown_options_and_help(tmp_path):
    bad = subprocess.run(["bash", str(SCRIPT), "--nope"], capture_output=True, text=True, check=False)
    assert bad.returncode == 2
    helped = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert helped.returncode == 0 and "--issuer" in helped.stdout
