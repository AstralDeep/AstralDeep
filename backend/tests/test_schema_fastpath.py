"""schema/policy marker fast paths and 052 backfill migration tests.

A matching schema_meta revision marker must skip the full _init_db
DDL/migration run while still executing feature 060's fixed-lock independent
policy-marker check within the 250ms budget. A missing marker triggers the
full run; an unsupported marker fails closed without mutation. Also covers the promoted
tool_overrides per-kind backfill
(_migrate_backfill_tool_kinds_052) semantics and idempotency. Requires a
reachable Postgres; skipped where unreachable.

Every test uses a unique temporary database and never mutates the configured
AstralDeep development database.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

try:
    import psycopg2
    from psycopg2 import sql
    from shared.database import (
        Database,
        SCHEMA_REVISION,
        SchemaRevisionError,
        _build_database_url,
    )
except Exception:  # pragma: no cover - import guard
    Database = None  # type: ignore
    SCHEMA_REVISION = None  # type: ignore
    SchemaRevisionError = RuntimeError  # type: ignore
    _build_database_url = None  # type: ignore


SCHEMA_LOCK = (1095980114, 60001)
POLICY_LOCK = (1095980114, 60002)


class _RedactedDsn(str):
    def __repr__(self):
        return "<isolated PostgreSQL DSN>"


@pytest.fixture(scope="module")
def isolated_database_url():
    if Database is None:
        pytest.skip("psycopg2/shared.database unavailable")
    base_dsn = _build_database_url()
    try:
        params = psycopg2.extensions.parse_dsn(base_dsn)
        name = f"astraldeep_fastpath_{uuid.uuid4().hex}"
        connection = psycopg2.connect(**params)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        connection.close()
    except Exception as exc:  # pragma: no cover - environment/privilege gate
        pytest.skip(f"cannot create isolated PostgreSQL database: {exc}")

    database_params = dict(params)
    database_params["dbname"] = name
    database_url = _RedactedDsn(psycopg2.extensions.make_dsn(**database_params))
    previous_pool_setting = os.environ.get("DB_POOL_DISABLE")
    os.environ["DB_POOL_DISABLE"] = "1"
    try:
        yield database_url
    finally:
        if previous_pool_setting is None:
            os.environ.pop("DB_POOL_DISABLE", None)
        else:
            os.environ["DB_POOL_DISABLE"] = previous_pool_setting
        try:
            connection = psycopg2.connect(**params)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
                )
            connection.close()
        except Exception:
            pass


def _db_or_skip(database_url):
    """Return a connected Database (marker freshly upserted) or skip."""
    if Database is None:
        pytest.skip("psycopg2/shared.database unavailable")
    try:
        return Database(database_url)
    except Exception as exc:  # pragma: no cover - no DB in this env
        pytest.skip(f"database unreachable: {exc}")


def test_marker_row_written(isolated_database_url):
    db = _db_or_skip(isolated_database_url)
    row = db.fetch_one(
        "SELECT value FROM schema_meta WHERE key = ?", ("revision",)
    )
    assert row is not None and row["value"] == SCHEMA_REVISION


def test_fast_path_skips_full_run(monkeypatch, isolated_database_url):
    _db_or_skip(isolated_database_url)

    def fail(self, conn, cursor):
        raise AssertionError("full schema run must be skipped on marker match")

    monkeypatch.setattr(Database, "_apply_full_schema", fail)
    Database(isolated_database_url)


def test_fast_path_runs_only_marker_statements(monkeypatch, isolated_database_url):
    _db_or_skip(isolated_database_url)
    statements = []

    class CountingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, query, params=None):
            statements.append((query, params))
            if params is None:
                return self._cursor.execute(query)
            return self._cursor.execute(query, params)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class CountingConn:
        def __init__(self, conn):
            self._conn = conn

        def cursor(self, *args, **kwargs):
            return CountingCursor(self._conn.cursor(*args, **kwargs))

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setenv("DB_POOL_DISABLE", "1")
    real_borrow = Database._borrow

    def counting_borrow(self):
        conn, pooled = real_borrow(self)
        return CountingConn(conn), pooled

    monkeypatch.setattr(Database, "_borrow", counting_borrow)
    Database(isolated_database_url)

    def holds_lock(statement, expected):
        query, params = statement
        compact = " ".join(str(query).split())
        return "pg_advisory_xact_lock" in compact and (
            params == expected
            or all(str(value) in compact for value in expected)
        )

    schema_lock_index = next(
        index for index, statement in enumerate(statements)
        if holds_lock(statement, SCHEMA_LOCK)
    )
    policy_lock_index = next(
        index for index, statement in enumerate(statements)
        if holds_lock(statement, POLICY_LOCK)
    )
    revision_probe_index = next(
        index for index, (query, _) in enumerate(statements)
        if "SELECT value FROM schema_meta" in str(query) and "revision" in str(query)
    )
    policy_probe_index = next(
        index for index, (query, _) in enumerate(statements)
        if "SELECT value FROM schema_meta" in str(query)
        and "user_agent_policy_revision" in str(query)
    )
    assert schema_lock_index < revision_probe_index
    assert policy_lock_index < policy_probe_index
    assert not any(
        "operation_record" in str(query) for query, _ in statements
    ), statements


def test_unsupported_revision_fails_closed(isolated_database_url):
    db = _db_or_skip(isolated_database_url)
    db.execute(
        "UPDATE schema_meta SET value = ? WHERE key = ?",
        ("000.000-test-stale", "revision"),
    )
    with pytest.raises(SchemaRevisionError, match="not an approved upgrade source"):
        Database(isolated_database_url)
    row = db.fetch_one(
        "SELECT value FROM schema_meta WHERE key = ?", ("revision",)
    )
    assert row["value"] == "000.000-test-stale"


def test_missing_marker_triggers_full_run(monkeypatch, isolated_database_url):
    db = _db_or_skip(isolated_database_url)
    db.execute("DELETE FROM schema_meta WHERE key = ?", ("revision",))
    calls = []
    real_apply = Database._apply_full_schema

    def spy(self, conn, cursor):
        calls.append(1)
        return real_apply(self, conn, cursor)

    monkeypatch.setattr(Database, "_apply_full_schema", spy)
    Database(isolated_database_url)
    assert calls == [1]
    row = db.fetch_one(
        "SELECT value FROM schema_meta WHERE key = ?", ("revision",)
    )
    assert row["value"] == SCHEMA_REVISION


def test_fast_path_within_budget(isolated_database_url):
    _db_or_skip(isolated_database_url)
    durations = []
    for _ in range(3):
        start = time.perf_counter()
        Database(isolated_database_url)
        durations.append(time.perf_counter() - start)
    assert min(durations) <= 0.25, durations


def _backfill_cleanup(db, user_id):
    db.execute("DELETE FROM tool_overrides WHERE user_id = ?", (user_id,))


def test_backfill_tool_kinds_052_semantics_and_idempotency(isolated_database_url):
    db = _db_or_skip(isolated_database_url)
    user, agent, tool = "u-test-052-backfill", "agent-test-052", "tool_x"
    _backfill_cleanup(db, user)
    try:
        db.execute(
            "INSERT INTO tool_overrides "
            "(user_id, agent_id, tool_name, permission_kind, enabled, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (user, agent, tool, False, 1),
        )
        db.execute(
            "INSERT INTO tool_overrides "
            "(user_id, agent_id, tool_name, permission_kind, enabled, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user, agent, tool, "tools:read", True, 1),
        )
        db.execute(
            "INSERT INTO tool_overrides "
            "(user_id, agent_id, tool_name, permission_kind, enabled, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (user, agent, "tool_enabled_legacy", True, 1),
        )

        conn = db._get_connection()
        try:
            cursor = conn.cursor()
            db._migrate_backfill_tool_kinds_052(cursor)
            db._migrate_backfill_tool_kinds_052(cursor)
            conn.commit()
        finally:
            conn.close()

        rows = db.fetch_all(
            "SELECT tool_name, permission_kind, enabled FROM tool_overrides "
            "WHERE user_id = ? AND agent_id = ? AND permission_kind IS NOT NULL",
            (user, agent),
        )
        state = {
            (r["tool_name"], r["permission_kind"]): bool(r["enabled"]) for r in rows
        }
        assert state[(tool, "tools:read")] is True
        assert state[(tool, "tools:write")] is False
        assert state[(tool, "tools:search")] is False
        assert state[(tool, "tools:system")] is False
        assert len(state) == 4
        assert not any(name == "tool_enabled_legacy" for name, _kind in state)

        legacy = db.fetch_all(
            "SELECT tool_name FROM tool_overrides "
            "WHERE user_id = ? AND agent_id = ? AND permission_kind IS NULL",
            (user, agent),
        )
        assert {r["tool_name"] for r in legacy} == {tool, "tool_enabled_legacy"}
    finally:
        _backfill_cleanup(db, user)
