"""Shows a personal node data directory and Plane store written before the identity-server removal open unchanged: every store row stays byte-identical, the node key is reused, and legacy connector rows and oidc owner rows are kept and never read, rewritten or deleted.
It drives plane_composition.py, shared/node_identity.py and orchestrator/node_connections.py against a real SQLite store."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from orchestrator import node_connections, plane_composition
from shared import node_identity as ni
from tests.helpers.owner_file import seed_owner

MANIFEST = Path(__file__).resolve().parents[2] / "config" / "astral-composition.json"
LEGACY_OWNER = "oidc.0123456789abcdef.owner-1"
CONNECTOR_KEY = "remote_mcp_" + "a" * 32
CONNECTOR_VALUE = "sealed-legacy-connector-bytes"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(root / "node"))
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ROOT", str(root / "blobs"))
    monkeypatch.setenv("PERSONAL_AGENT_ARTIFACT_ROOT", str(root / "artifacts"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    ni.reset_cache()
    yield root / "node"
    ni.reset_cache()


def snapshot(database: Path) -> str:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()
    finally:
        connection.close()


def credential_rows(database: Path) -> list:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return [(table, row) for table in tables if "credential" in table
                for row in connection.execute(f'SELECT * FROM "{table}"')]
    finally:
        connection.close()


def test_a_store_and_data_dir_from_before_the_removal_open_unchanged_and_legacy_rows_are_kept(home):
    composition = plane_composition.compose_plane_from_environment(MANIFEST)
    try:
        with composition.runtime.transaction() as tx:
            composition.repositories.credentials.upsert_credential(
                tx, owner_id=LEGACY_OWNER, agent_id=node_connections.LEGACY_AGENT_ID,
                credential_key=CONNECTOR_KEY, encrypted_value=CONNECTOR_VALUE, updated_at=1)
    finally:
        composition.close()
    database = home / plane_composition.SQLITE_DATABASE_FILE
    key_bytes = ni.load_node_identity() and (home / ni.KEY_FILE).read_bytes()
    stored_owner = seed_owner(LEGACY_OWNER)
    ni.reset_cache()
    before, rows_before = snapshot(database), credential_rows(database)
    assert any(CONNECTOR_VALUE in row for _, row in rows_before)

    assert ni.retire_legacy_owner() is True
    ni.load_node_identity()
    reopened = plane_composition.compose_plane_from_environment(MANIFEST)
    try:
        with reopened.runtime.transaction() as tx:
            keys = reopened.repositories.credentials.list_credential_keys(
                tx, owner_id=LEGACY_OWNER, agent_id=node_connections.LEGACY_AGENT_ID)
        assert node_connections.legacy_count(list(keys)) == 1
    finally:
        reopened.close()

    assert snapshot(database) == before
    assert credential_rows(database) == rows_before
    assert (home / ni.KEY_FILE).read_bytes() == key_bytes
    assert not (home / ni.OWNER_FILE).exists()
    (kept,) = home.glob(ni.OWNER_FILE + ".legacy-*")
    assert kept.read_bytes() == stored_owner
    assert ni.legacy_owners() == [LEGACY_OWNER]
