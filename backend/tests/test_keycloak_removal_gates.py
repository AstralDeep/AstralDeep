"""Shows the identity-server removal through the real app and boot path: other profiles exit 78 and leave a database byte-identical, every route refuses a signed bearer and a session cookie with no outbound connection, agent tokens keep their golden bytes, a placeholder secret exits 78 with ASTRAL_ENV unset, and the search check fails on a planted file.
It reuses the route census and golden-vector suites so one run reports the whole exit criteria."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import jose.jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from orchestrator import boot_posture, orchestrator as orchestrator_module
from shared import node_identity
from tests import test_node_route_census as census_suite
from tests.helpers import orchestrator_app
from tests.test_agent_authority import (  # noqa: F401
    node,
    test_a_child_matches_the_golden_claims_and_bytes,
    test_encoded_tokens_match_the_golden_bytes,
    test_the_node_identity_is_the_golden_one,
)
from tests.test_node_route_census import app, plane_runtime  # noqa: F401

REPO = Path(__file__).resolve().parents[2]


def _signed_jwt() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    now = int(time.time())
    return jose.jwt.encode(
        {"sub": "owner", "iss": "https://idp.invalid/realms/r", "aud": "account", "azp": "astral-web",
         "iat": now, "exp": now + 300, "realm_access": {"roles": ["user", "admin"]}},
        key, algorithm="RS256", headers={"kid": "k"})


def _first(row):
    return next(iter(row.values())) if hasattr(row, "values") else row[0]


def _database_digest(runtime) -> str:
    digest = hashlib.sha256()
    with runtime.transaction() as tx:
        tables = [_first(row) for row in tx.fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename")]
        for table in tables:
            rows = tx.fetch_all(f'SELECT md5(t::text) FROM "{table}" t ORDER BY 1')
            digest.update(table.encode() + json.dumps([_first(row) for row in rows]).encode())
    return digest.hexdigest()


@pytest.mark.parametrize("profile", [None, "server", "institution", "Personal "])
def test_a_non_personal_boot_exits_78_and_leaves_the_database_byte_identical(
        profile, plane_runtime, tmp_path, monkeypatch):  # noqa: F811
    before = _database_digest(plane_runtime)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path / "node"))
    if profile is None:
        monkeypatch.delenv(node_identity.PROFILE_ENV, raising=False)
    else:
        monkeypatch.setenv(node_identity.PROFILE_ENV, profile)
    with pytest.raises(SystemExit) as exited:
        orchestrator_module.main()
    assert exited.value.code == 78
    assert _database_digest(plane_runtime) == before
    assert not (tmp_path / "node").exists()


def test_every_route_names_a_door_and_refuses_a_signed_bearer_and_a_session_cookie(app, monkeypatch):  # noqa: F811
    findings, seen = census_suite.census(app)
    assert findings == []
    attempts = []

    def refuse_outbound(self, *args, **kwargs):
        attempts.append(args)
        raise AssertionError("no outbound connection may be opened")

    monkeypatch.setattr(socket.socket, "connect", refuse_outbound)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    token = _signed_jwt()
    client = TestClient(app, base_url=orchestrator_app.PUBLIC_BASE_URL, raise_server_exceptions=False)
    gated = [(method, path) for method, path in seen
             if method not in ("WS", "MOUNT") and "{" not in path
             and (method, path) not in census_suite.PUBLIC and (method, path) not in census_suite.PERSONAL_REFUSED
             and (method, path) not in census_suite.IN_BAND]
    assert len(gated) > 80
    for method, path in sorted(gated):
        for verb in method.split(","):
            if verb in ("HEAD", "OPTIONS"):
                continue
            response = client.request(
                verb, path, headers={"Authorization": f"Bearer {token}", "Cookie": "astral_session=v.s"},
                follow_redirects=False)
            assert response.status_code in (401, 403, 404, 405, 422), (verb, path, response.status_code)
            assert token not in response.text
    assert attempts == []


def test_a_placeholder_secret_exits_78_with_astral_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv(node_identity.PROFILE_ENV, node_identity.PERSONAL)
    monkeypatch.setenv(node_identity.DATA_DIR_ENV, str(tmp_path / "node"))
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "change-me")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "k" * 44)
    with pytest.raises(SystemExit) as exited:
        boot_posture.assert_production_posture()
    assert exited.value.code == 78


def test_the_search_check_passes_on_the_tree_and_fails_on_a_planted_file(tmp_path):
    clean = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_no_keycloak.py"), "--repo", str(REPO),
         "--config", str(REPO / "scripts" / "no_keycloak.json")], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "planted.py").write_text("ISSUER = 'keycloak'\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"exclude": [], "pending": []}))
    planted = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check_no_keycloak.py"), "--repo", str(tmp_path),
         "--config", str(config)], capture_output=True, text=True)
    assert planted.returncode != 0 and "planted.py" in planted.stdout + planted.stderr
