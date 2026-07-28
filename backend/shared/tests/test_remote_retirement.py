"""Feature 063 US7 — retirement & revocation purge (SC-014, FR-015/FR-053).

Drives ``Database._cleanup_retire_063`` and the per-user credential revocation
sweep against a uniquely named, throwaway PostgreSQL database (the same
isolation pattern as ``backend/tests/test_migrations_060.py``): the configured
AstralDeep database is never seeded, reset, or otherwise mutated. Each scenario
runs the purge TWICE — SC-014 requires that re-running retirement changes
nothing further.
"""
from __future__ import annotations

import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import sql  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from shared.database import Database, _build_database_url  # noqa: E402

RETIRED_IDS = ("remote-observe-1", "remote-control-1")
MERGED_ID = "remote-compute-1"
# Every per-agent row family the purge must clear (the 040-pattern table set).
AGENT_TABLES = ("agent_scopes", "tool_overrides", "tool_permissions",
                "agent_trust", "agent_ownership", "user_credentials")
FEATURE_TABLES = ("machine_credential", "remote_operation_proposal",
                  "tracked_job", "remote_machine")


class _Sandbox:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return psycopg2.connect(self.dsn, cursor_factory=RealDictCursor)


@pytest.fixture(autouse=True)
def _direct_connections_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_DISABLE", "1")


@pytest.fixture
def sandbox():
    base_dsn = _build_database_url()
    try:
        params = psycopg2.extensions.parse_dsn(base_dsn)
        admin = psycopg2.connect(**params)
        admin.autocommit = True
    except Exception as exc:  # pragma: no cover - environment gate
        pytest.skip(f"PostgreSQL unavailable for isolated retirement tests: {exc}")
    name = f"astraldeep_063_retire_{uuid.uuid4().hex}"
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except Exception as exc:  # pragma: no cover - privilege gate
        admin.close()
        pytest.skip(f"cannot create isolated PostgreSQL database: {exc}")
    database_params = dict(params)
    database_params["dbname"] = name
    yield _Sandbox(psycopg2.extensions.make_dsn(**database_params))
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
    admin.close()


def _seed(sandbox: _Sandbox) -> None:
    """A representative populated capability: two owners, machines + secrets,
    a pending proposal, an open and a finished tracked job, and per-agent
    permission/trust/ownership/credential rows for all three 063 agent ids —
    plus an unrelated agent's rows that retirement must never touch."""
    now = int(time.time())
    with sandbox.connect() as conn, conn.cursor() as c:
        for owner in ("user-a", "user-b"):
            c.execute(
                "INSERT INTO remote_machine (machine_id, owner_user_id, label, address,"
                " port, username, os_family, role, created_at, updated_at)"
                " VALUES (%s, %s, %s, '10.0.0.5', 22, 'me', 'linux', 'cluster', %s, %s)",
                (f"m-{owner}", owner, f"box-{owner}", now, now))
            c.execute(
                "INSERT INTO machine_credential (machine_id, owner_user_id, cred_type,"
                " encrypted_secret, created_at, updated_at)"
                " VALUES (%s, %s, 'password', 'enc-blob', %s, %s)",
                (f"m-{owner}", owner, now, now))
        c.execute(
            "INSERT INTO remote_operation_proposal (proposal_id, owner_user_id,"
            " machine_id, agent_id, verb, args_json, args_fingerprint, summary,"
            " status, created_at, expires_at)"
            " VALUES ('p1', 'user-a', 'm-user-a', %s, 'remove_path', '{}', 'fp',"
            " 'seeded', 'pending', %s, %s)",
            (MERGED_ID, now, now + 900))
        c.execute(
            "INSERT INTO tracked_job (tracked_job_id, owner_user_id, machine_id,"
            " scheduler_job_id, state, terminal, created_at)"
            " VALUES ('t-open', 'user-a', 'm-user-a', '101', 'running', FALSE, %s)",
            (now,))
        c.execute(
            "INSERT INTO tracked_job (tracked_job_id, owner_user_id, machine_id,"
            " scheduler_job_id, state, terminal, exit_code, created_at)"
            " VALUES ('t-done', 'user-b', 'm-user-b', '102', 'completed', TRUE,"
            " '0:0', %s)",
            (now,))
        for agent_id in RETIRED_IDS + (MERGED_ID, "weather-1"):
            c.execute(
                "INSERT INTO agent_scopes (user_id, agent_id, scope, enabled, updated_at)"
                " VALUES ('user-a', %s, 'tools:read', TRUE, %s)", (agent_id, now))
            c.execute(
                "INSERT INTO tool_overrides (user_id, agent_id, tool_name, enabled,"
                " updated_at) VALUES ('user-a', %s, 'list_queue', FALSE, %s)",
                (agent_id, now))
            c.execute(
                "INSERT INTO tool_permissions (user_id, agent_id, tool_name, allowed,"
                " updated_at) VALUES ('user-a', %s, 'list_queue', TRUE, %s)",
                (agent_id, now))
            c.execute(
                "INSERT INTO agent_trust (agent_id, is_safe) VALUES (%s, TRUE)"
                " ON CONFLICT (agent_id) DO NOTHING", (agent_id,))
            c.execute(
                "INSERT INTO agent_ownership (agent_id, owner_email, is_public,"
                " created_at, updated_at) VALUES (%s, 'op@example.com', TRUE, %s, %s)"
                " ON CONFLICT (agent_id) DO NOTHING", (agent_id, now, now))
            c.execute(
                "INSERT INTO user_credentials (user_id, agent_id, credential_key,"
                " encrypted_value, created_at, updated_at)"
                " VALUES ('user-a', %s, 'K', 'enc', %s, %s)", (agent_id, now, now))
        conn.commit()


def _retire(db: Database, sandbox: _Sandbox, *, full_retire: bool) -> None:
    with sandbox.connect() as conn, conn.cursor() as cursor:
        db._cleanup_retire_063(cursor, full_retire=full_retire)
        conn.commit()


def _table_exists(sandbox: _Sandbox, name: str) -> bool:
    with sandbox.connect() as conn, conn.cursor() as c:
        c.execute("SELECT to_regclass(%s) AS reg", (f"public.{name}",))
        return c.fetchone()["reg"] is not None


def _agent_rows(sandbox: _Sandbox, agent_id: str) -> dict:
    out = {}
    with sandbox.connect() as conn, conn.cursor() as c:
        for table in AGENT_TABLES:
            c.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE agent_id = %s",
                      (agent_id,))
            out[table] = c.fetchone()["n"]
    return out


def _fetch_all(sandbox: _Sandbox, query: str, params: tuple = ()) -> list:
    with sandbox.connect() as conn, conn.cursor() as c:
        c.execute(query, params)
        return [dict(r) for r in c.fetchall()]


def test_full_retirement_purges_everything_and_reruns_clean(sandbox):
    db = Database(sandbox.dsn)
    _seed(sandbox)

    for _ in range(2):  # SC-014: the second run must change nothing further
        _retire(db, sandbox, full_retire=True)
        for table in FEATURE_TABLES:
            assert not _table_exists(sandbox, table), table
        for agent_id in RETIRED_IDS + (MERGED_ID,):
            assert set(_agent_rows(sandbox, agent_id).values()) == {0}, agent_id

    # Unrelated agents keep every row family — the purge is id-targeted.
    assert set(_agent_rows(sandbox, "weather-1").values()) == {1}


def test_soft_retirement_destroys_secrets_orphans_jobs_keeps_inventory(sandbox):
    db = Database(sandbox.dsn)
    _seed(sandbox)

    for _ in range(2):
        _retire(db, sandbox, full_retire=False)
        # Secrets destroyed for every owner; inventory + schema stay in place.
        assert _fetch_all(sandbox, "SELECT 1 FROM machine_credential") == []
        assert len(_fetch_all(sandbox, "SELECT 1 FROM remote_machine")) == 2
        for table in FEATURE_TABLES:
            assert _table_exists(sandbox, table), table
        # Open jobs close honestly; already-terminal rows keep their outcome.
        jobs = {r["tracked_job_id"]: r for r in _fetch_all(
            sandbox, "SELECT tracked_job_id, state, terminal, exit_code FROM tracked_job")}
        assert jobs["t-open"]["state"] == "orphaned" and jobs["t-open"]["terminal"] is True
        assert jobs["t-done"]["state"] == "completed" and jobs["t-done"]["exit_code"] == "0:0"
        # The merged-away split ids purge; the live unified agent id survives a
        # soft retire (only a FULL retire removes remote-compute-1's rows).
        for agent_id in RETIRED_IDS:
            assert set(_agent_rows(sandbox, agent_id).values()) == {0}, agent_id
        assert set(_agent_rows(sandbox, MERGED_ID).values()) == {1}

    assert set(_agent_rows(sandbox, "weather-1").values()) == {1}


def test_account_removal_sweep_destroys_only_that_users_credentials(sandbox, monkeypatch):
    # FR-015: account removal / logout reuses the per-user revocation sweep.
    from cryptography.fernet import Fernet

    from orchestrator.credential_manager import CredentialManager

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    db = Database(sandbox.dsn)
    _seed(sandbox)
    manager = CredentialManager(db=db)

    for _ in range(2):  # idempotent like the retirement purge
        manager.remove_machine_credentials_for_user("user-a")
        rows = _fetch_all(sandbox, "SELECT owner_user_id FROM machine_credential")
        assert [r["owner_user_id"] for r in rows] == ["user-b"]
    # The machine row itself outlives a credential-only revocation (FR-015
    # destroys secrets; inventory deletion is the separate machine-delete path).
    assert len(_fetch_all(sandbox, "SELECT 1 FROM remote_machine")) == 2
