"""Feature 063 — guarded schema delta over a REPRESENTATIVE dataset (T077).

Constitution IX: the 063 delta (remote_machine, machine_credential,
remote_operation_proposal, tracked_job) must apply idempotently over real rows
— not just an empty database — and the rollback documented in
specs/063-remote-compute-agents/data-model.md must restore prior state. Tests
run against uniquely named throwaway PostgreSQL databases (the
test_migrations_060.py isolation pattern); the configured AstralDeep database
is never touched.
"""
from __future__ import annotations

import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2 import sql  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from shared import database as database_module  # noqa: E402
from shared.database import Database, _build_database_url  # noqa: E402

FEATURE_TABLES = ("remote_machine", "machine_credential",
                  "remote_operation_proposal", "tracked_job")
FEATURE_INDEXES = ("idx_remote_machine_owner", "idx_machine_credential_owner",
                   "idx_rop_owner_status", "uq_tracked_job_machine_job",
                   "idx_tracked_job_open")
# The documented rollback DROP order (machine_credential's FK on remote_machine
# means the child must go first).
ROLLBACK_DROP_ORDER = ("machine_credential", "remote_operation_proposal",
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
        pytest.skip(f"PostgreSQL unavailable for isolated migration tests: {exc}")
    name = f"astraldeep_063_mig_{uuid.uuid4().hex}"
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


def _seed_representative(sandbox: _Sandbox) -> None:
    """One populated row per 063 table, exercising the optional columns (pinned
    host key, key passphrase, approved + pending proposals, open + terminal
    jobs) — plus split-id permission rows the 063 merge delta must purge."""
    now = int(time.time())
    with sandbox.connect() as conn, conn.cursor() as c:
        c.execute(
            "INSERT INTO remote_machine (machine_id, owner_user_id, label, address,"
            " port, username, os_family, role, host_key_type, host_key_fingerprint,"
            " host_key_blob, last_verdict, last_checked_at, created_at, updated_at)"
            " VALUES ('m1', 'user-a', 'dgx', '10.0.0.5', 22, 'me', 'linux', 'cluster',"
            " 'ssh-ed25519', 'SHA256:pinned', 'blob', 'ok', %s, %s, %s)",
            (now, now, now))
        c.execute(
            "INSERT INTO machine_credential (machine_id, owner_user_id, cred_type,"
            " encrypted_secret, encrypted_passphrase, created_at, updated_at)"
            " VALUES ('m1', 'user-a', 'ssh_key', 'enc-pem', 'enc-pass', %s, %s)",
            (now, now))
        c.execute(
            "INSERT INTO remote_operation_proposal (proposal_id, owner_user_id,"
            " chat_id, machine_id, agent_id, verb, args_json, args_fingerprint,"
            " summary, status, created_at, expires_at, decided_at)"
            " VALUES ('p-approved', 'user-a', 'chat-1', 'm1', 'remote-compute-1',"
            " 'remove_path', '{\"path\": \"/x\"}', 'fp1', 'Delete /x on dgx',"
            " 'approved', %s, %s, %s)",
            (now, now + 900, now))
        c.execute(
            "INSERT INTO remote_operation_proposal (proposal_id, owner_user_id,"
            " machine_id, agent_id, verb, args_json, args_fingerprint, summary,"
            " status, created_at, expires_at)"
            " VALUES ('p-pending', 'user-a', 'm1', 'remote-compute-1', 'cancel_job',"
            " '{\"job_id\": \"5\"}', 'fp2', 'Cancel job 5', 'pending', %s, %s)",
            (now, now + 900))
        c.execute(
            "INSERT INTO tracked_job (tracked_job_id, owner_user_id, machine_id,"
            " chat_id, scheduler_job_id, submit_marker, output_path, component_id,"
            " job_name, state, terminal, notify_on_finish, created_at, last_polled_at)"
            " VALUES ('t-open', 'user-a', 'm1', 'chat-1', '101', 'nonce-1',"
            " '/home/me/.astral_jobs/x.out', 'au_rjob_101', 'train', 'running',"
            " FALSE, TRUE, %s, %s)",
            (now, now))
        c.execute(
            "INSERT INTO tracked_job (tracked_job_id, owner_user_id, machine_id,"
            " scheduler_job_id, state, exit_code, terminal, created_at, finished_at)"
            " VALUES ('t-done', 'user-a', 'm1', '100', 'completed', '0:0', TRUE,"
            " %s, %s)",
            (now, now))
        c.execute(
            "INSERT INTO agent_scopes (user_id, agent_id, scope, enabled, updated_at)"
            " VALUES ('user-a', 'remote-observe-1', 'tools:read', TRUE, %s)", (now,))
        conn.commit()


def _snapshot(sandbox: _Sandbox) -> dict:
    out = {}
    with sandbox.connect() as conn, conn.cursor() as c:
        for table, pk in (("remote_machine", "machine_id"),
                          ("machine_credential", "machine_id"),
                          ("remote_operation_proposal", "proposal_id"),
                          ("tracked_job", "tracked_job_id")):
            c.execute(f"SELECT * FROM {table} ORDER BY {pk}")
            out[table] = [dict(r) for r in c.fetchall()]
    return out


def _fetch_one(sandbox: _Sandbox, query: str, params: tuple = ()) -> dict:
    with sandbox.connect() as conn, conn.cursor() as c:
        c.execute(query, params)
        return c.fetchone()


def _table_exists(sandbox: _Sandbox, name: str) -> bool:
    return _fetch_one(sandbox, "SELECT to_regclass(%s) AS reg",
                      (f"public.{name}",))["reg"] is not None


def _index_exists(sandbox: _Sandbox, name: str) -> bool:
    return _fetch_one(sandbox, "SELECT 1 AS x FROM pg_indexes "
                      "WHERE schemaname = 'public' AND indexname = %s",
                      (name,)) is not None


def _clear_revision(sandbox: _Sandbox) -> None:
    with sandbox.connect() as conn, conn.cursor() as c:
        c.execute("DELETE FROM schema_meta WHERE key = 'revision'")
        conn.commit()


def test_delta_reapplies_idempotently_over_representative_rows(sandbox):
    Database(sandbox.dsn)
    _seed_representative(sandbox)
    expected = _snapshot(sandbox)

    # Fast-path re-run (marker current) must not disturb the rows.
    Database(sandbox.dsn)
    assert _snapshot(sandbox) == expected

    # Forced full re-apply from an absent marker: the guarded delta runs again
    # over REAL rows — CREATE IF NOT EXISTS + the merge cleanup, no data loss.
    _clear_revision(sandbox)
    Database(sandbox.dsn)
    assert _fetch_one(
        sandbox, "SELECT value FROM schema_meta WHERE key = 'revision'"
    )["value"] == database_module.SCHEMA_REVISION
    assert _snapshot(sandbox) == expected
    for index in FEATURE_INDEXES:
        assert _index_exists(sandbox, index), index
    # The 063 merge delta purged the merged-away split id's permission row.
    assert _fetch_one(
        sandbox,
        "SELECT COUNT(*) AS n FROM agent_scopes WHERE agent_id = 'remote-observe-1'"
    )["n"] == 0


def test_documented_rollback_then_reapply_restores_schema(sandbox):
    Database(sandbox.dsn)
    _seed_representative(sandbox)

    # data-model.md rollback: drop the four tables (FK-safe order) and clear
    # the revision marker to force a clean re-derive on the next boot.
    with sandbox.connect() as conn, conn.cursor() as c:
        for table in ROLLBACK_DROP_ORDER:
            c.execute(f"DROP TABLE IF EXISTS {table}")
        c.execute("DELETE FROM schema_meta WHERE key = 'revision'")
        conn.commit()
    for table in FEATURE_TABLES:
        assert not _table_exists(sandbox, table), table

    # The next boot re-derives the full schema: 063 tables return empty with
    # their indexes and CHECK constraints — prior (pre-data) state restored.
    Database(sandbox.dsn)
    for table in FEATURE_TABLES:
        assert _table_exists(sandbox, table), table
        assert _fetch_one(sandbox, f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    for index in FEATURE_INDEXES:
        assert _index_exists(sandbox, index), index
    with sandbox.connect() as conn, conn.cursor() as c:
        with pytest.raises(psycopg2.IntegrityError):
            c.execute(
                "INSERT INTO remote_machine (machine_id, owner_user_id, label,"
                " address, port, username, os_family, role, created_at, updated_at)"
                " VALUES ('bad', 'u', 'l', 'a', 22, 'u', 'beos', 'cluster', 1, 1)")
