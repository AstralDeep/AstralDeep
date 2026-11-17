"""Exercises SQLite boot, strict SQL adaptation, locking, durable reconciliation and rollback.
Every database is disposable and the shared repository suite supplies owner-isolation checks.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from astralplane import SCHEMA_REVISION, create_runtime, is_lock_contention
from astralplane.database.migrations import MigrationRunner
from astralplane.database.pool import ConnectionPool
from astralplane.database.sqlite import (
    SQLiteLockTimeoutError,
    create_sqlite_driver_pool,
    sqlite_path_from_url,
)
from astralplane.database.sqlite_functions import (
    _attachment_id_is_canonical,
    _blob_owner_is_canonical,
    _blob_storage_key_is_canonical,
    _capabilities_are_canonical,
    _identifier_is_canonical,
    _positive_unique_int_array,
    _valid_selected_input,
)
from astralplane.database.sqlite_migrations import (
    SQLITE_DATA_PLANE_REVISION,
    SQLITE_MIGRATION_REGISTRY,
    SQLiteBootRunner,
)
from astralplane.database.sqlite_reconciliation import SQLiteReconciliationCoordinator
from astralplane.database.sqlite_sql import UnsupportedSQLiteConstructError, translate
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import InitializationError, SchemaRevisionError, TransactionStateError
from astralplane.reconciliation import RECONCILIATION_ADVISORY_LOCK


class Hook:
    name = "sqlite-test-hook"
    version = "1"

    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def reconcile(self, context):
        self.calls += 1
        if self.fail:
            raise RuntimeError("hook failed")
        return {"changed": 1}


@pytest.fixture
def database(tmp_path):
    pool = ConnectionPool(create_sqlite_driver_pool(f"sqlite://{tmp_path / 'plane.db'}"))
    try:
        yield PlaneDatabase(pool)
    finally:
        pool.close()


def runner(database):
    return SQLiteBootRunner(
        database,
        MigrationRunner(
            database, revision=SQLITE_DATA_PLANE_REVISION, registry=SQLITE_MIGRATION_REGISTRY
        ),
    )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:relative",
        "sqlite://host/file.db",
        "sqlite:///",
        "sqlite:///:memory:",
        "sqlite:///tmp/file.db?mode=ro",
        "sqlite:///tmp/file.db#part",
        "postgresql:///tmp/file.db",
        "sqlite:///tmp/%00file.db",
    ],
)
def test_url_rejects_ambiguous_or_nonpersistent_targets(url):
    with pytest.raises(InitializationError):
        sqlite_path_from_url(url)


@pytest.mark.parametrize(
    "name,value",
    [
        ("maximum_connections", 1.5),
        ("maximum_connections", True),
        ("maximum_connections", 0),
        ("acquire_timeout_seconds", float("inf")),
        ("acquire_timeout_seconds", 0),
        ("busy_timeout_seconds", float("nan")),
        ("busy_timeout_seconds", -1),
    ],
)
def test_pool_settings_reject_unbounded_or_invalid_values(tmp_path, name, value):
    with pytest.raises(InitializationError):
        create_sqlite_driver_pool(f"sqlite://{tmp_path / 'plane.db'}", **{name: value})


def test_file_permissions_pool_bounds_and_closed_connections(tmp_path):
    path = tmp_path / "plane.db"
    pool = create_sqlite_driver_pool(
        f"sqlite://{path}", maximum_connections=1, acquire_timeout_seconds=0.001
    )
    connection = pool.getconn()
    assert path.stat().st_mode & 0o777 == 0o600
    assert connection.raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert connection.raw.execute("PRAGMA foreign_keys").fetchone() == (1,)
    with pytest.raises(InitializationError, match="checkout"):
        pool.getconn()
    pool.putconn(connection)
    with pytest.raises(InitializationError, match="not checked out"):
        pool.putconn(connection)
    assert pool.getconn() is connection
    pool.putconn(connection, close=True)
    with pytest.raises(sqlite3.ProgrammingError):
        connection.cursor()
    pool.closeall()
    pool.closeall()
    with pytest.raises(InitializationError, match="closed"):
        pool.getconn()


def test_cursor_lifecycle_and_savepoint_rollback(database):
    with database.transaction() as tx:
        tx.execute("CREATE TABLE values_table(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        assert tx.execute("INSERT INTO values_table VALUES(%s,%s)", (1, "first")).rowcount == 1
        with pytest.raises(sqlite3.IntegrityError), tx.savepoint("rejected"):
            tx.execute("INSERT INTO values_table VALUES(%s,%s)", (1, "duplicate"))
        assert (
            tx.fetch_one("SELECT value FROM values_table WHERE id=%(id)s", {"id": 1})["value"]
            == "first"
        )
    with pytest.raises(RuntimeError), database.transaction() as tx:
        tx.execute("INSERT INTO values_table VALUES(2,'rolled back')")
        raise RuntimeError("rollback")
    with database.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS count FROM values_table")["count"] == 1
    with database._pool.connection() as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1 AS value")
        assert cursor.fetchone() == (1,)
        cursor.execute("SET LOCAL lock_timeout='1ms'")
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchall()
        cursor.close()
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.execute("SELECT 1")


def test_read_only_snapshot_and_lock_deadline(tmp_path):
    path = tmp_path / "plane.db"
    pool = ConnectionPool(create_sqlite_driver_pool(f"sqlite://{path}"))
    competing_pool = ConnectionPool(create_sqlite_driver_pool(f"sqlite://{path}"))
    database, competing = PlaneDatabase(pool), PlaneDatabase(competing_pool)
    try:
        runner(database).run(expected_revision=SCHEMA_REVISION)
        with database.transaction() as tx:
            tx.execute("CREATE TABLE value_table(value INTEGER)")
            tx.execute("INSERT INTO value_table VALUES(1)")
        with database.transaction() as writer:
            writer.execute("UPDATE value_table SET value=2")
            with competing.transaction(read_only=True) as reader:
                assert reader.fetch_one("SELECT value FROM value_table")["value"] == 1
            with pytest.raises(SQLiteLockTimeoutError), competing.transaction() as rejected:
                rejected.execute("SET LOCAL lock_timeout='1ms'")
                rejected.fetch_one("SELECT value FROM value_table FOR UPDATE")
        with (
            pytest.raises(sqlite3.OperationalError),
            competing.transaction(read_only=True) as reader,
        ):
            reader.execute("UPDATE value_table SET value=3")
        with competing.transaction() as writer:
            writer.execute("UPDATE value_table SET value=4")
    finally:
        pool.close()
        competing_pool.close()


def test_read_first_write_transaction_holds_the_writer_from_its_first_statement(tmp_path):
    path = tmp_path / "plane.db"
    pool = ConnectionPool(create_sqlite_driver_pool(f"sqlite://{path}"))
    competing_pool = ConnectionPool(create_sqlite_driver_pool(f"sqlite://{path}"))
    database, competing = PlaneDatabase(pool), PlaneDatabase(competing_pool)
    try:
        runner(database).run(expected_revision=SCHEMA_REVISION)
        with database.transaction() as tx:
            tx.execute("CREATE TABLE value_table(value INTEGER)")
            tx.execute("INSERT INTO value_table VALUES(1)")
        with database.transaction() as writer:
            assert writer.fetch_one("SELECT value FROM value_table")["value"] == 1
            with pytest.raises(SQLiteLockTimeoutError), competing.transaction() as rejected:
                rejected.execute("SET LOCAL lock_timeout='1ms'")
                rejected.execute("UPDATE value_table SET value=3")
            writer.fetch_one("SELECT value FROM value_table FOR UPDATE")
            writer.execute("UPDATE value_table SET value=2")
        with competing.transaction(read_only=True) as reader:
            assert reader.fetch_one("SELECT value FROM value_table")["value"] == 2
    finally:
        pool.close()
        competing_pool.close()


@pytest.mark.parametrize(
    "sql,parameters,expected",
    [
        ("SELECT %s::bigint AS value", ("123",), 123),
        ("SELECT (%s::jsonb->>'count')::integer AS value", ('{"count":12}',), 12),
        ("SELECT %s::boolean AS value", ("false",), 0),
        ("SELECT %s::jsonb AS value", ('{"b":2,"a":1}',), {"a": 1, "b": 2}),
        (
            "SELECT %s::uuid AS value",
            ("{00000000000040008000000000000001}",),
            "00000000-0000-4000-8000-000000000001",
        ),
        ("SELECT %s=ANY(%s::text[]) AS value", ("x", ["x", "y"]), 1),
        ("SELECT %s<>ALL(%s::text[]) AS value", ("x", ["z"]), 1),
        ("SELECT %s=ANY(%s::text[]) AS value", ("x", []), 0),
        ("SELECT %s=ANY(%s::text[]) AS value", ("x", None), None),
        ("SELECT %s<>ALL(%s::text[]) AS value", ("x", None), None),
        ("SELECT %s::jsonb ? %s AS value", ('["scope"]', "scope"), 1),
        ("SELECT clock_timestamp()+(%s*interval '1 second') AS value", (2,), "timestamp"),
        ("SELECT clock_timestamp()-interval '1 day' AS value", (), "timestamp"),
        ("SELECT left(%s,2) AS value", ("text",), "te"),
        ("SELECT right(%s,2) AS value", ("text",), "xt"),
        ("SELECT right(%s,-1) AS value", ("text",), "ext"),
        ("SELECT right(%s,9) AS value", ("text",), "text"),
        ("SELECT right(%s,0) AS value", ("text",), ""),
        ('SELECT %s COLLATE "C" AS value', ("é",), "é"),
        (
            "SELECT jsonb_set(%s,'{nested,x}',to_jsonb(%s)) AS value",
            ('{"nested":{"x":1}}', 2),
            {"nested": {"x": 2}},
        ),
    ],
)
def test_sql_adaptation_preserves_values(database, sql, parameters, expected):
    with database.transaction() as tx:
        value = tx.fetch_one(sql, parameters)["value"]
    if expected == "timestamp":
        assert isinstance(value, datetime) and value.tzinfo == UTC
    else:
        assert value == expected


@pytest.mark.parametrize(
    "statement,parameters",
    [
        ("SELECT %s::integer", ("not an integer",)),
        ("SELECT %s::smallint", (32768,)),
        ("SELECT %s::boolean", ("maybe",)),
        ("SELECT %s::jsonb", ("{bad",)),
        ("SELECT %s::uuid", ("not a UUID",)),
        ("SELECT %s::jsonb", ('{"nested":["\\u0000"]}',)),
        ("SELECT %s::timestamptz", ("not a date",)),
    ],
)
def test_invalid_casts_fail_and_abort_instead_of_returning_coerced_success(
    database, statement, parameters
):
    with (
        pytest.raises(TransactionStateError),
        database.transaction() as tx,
        pytest.raises(sqlite3.OperationalError),
    ):
        tx.fetch_one(statement, parameters)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT DISTINCT ON(id) id FROM sample",
        "SELECT * FROM unnest(%s)",
        "SELECT * FROM sample JOIN LATERAL(SELECT 1) ON true",
        "SELECT $$body$$",
        "SET CONSTRAINTS ALL DEFERRED",
        "SET LOCAL row_security=off",
    ],
)
def test_unsupported_sql_fails_explicitly(database, statement):
    with pytest.raises(UnsupportedSQLiteConstructError), database.transaction() as tx:
        tx.execute(statement)


def test_translation_preserves_literals_and_parameter_boundaries(database):
    statement = "SELECT 'FOR UPDATE ::uuid %s' AS value, %s::text AS bound /* ::integer */"
    assert "FOR UPDATE ::uuid %s" in translate(statement)
    with database.transaction() as tx:
        row = tx.fetch_one(statement, ("'; DROP TABLE schema_meta; --",))
        assert row["value"] == "FOR UPDATE ::uuid %s"
        assert row["bound"] == "'; DROP TABLE schema_meta; --"


def test_guarded_schema_is_repeat_safe_and_rejects_drift(database):
    first = runner(database).run(expected_revision=SCHEMA_REVISION)
    assert first.applied_steps == ("astralplane-sqlite-baseline",)
    assert runner(database).run(expected_revision=SCHEMA_REVISION).already_current
    with database.transaction() as tx:
        tx.execute("DROP INDEX ix_workspace_snapshot_chat")
    with pytest.raises(SchemaRevisionError, match="structure"):
        runner(database).run(expected_revision=SCHEMA_REVISION)


@pytest.mark.parametrize(
    "key,value", [("revision", "089.021"), ("astralplane_migration_digest", "a" * 64)]
)
def test_guarded_schema_rejects_unrecognized_metadata(database, key, value):
    runner(database).run(expected_revision=SCHEMA_REVISION)
    with database.transaction() as tx:
        tx.execute("UPDATE schema_meta SET value=%s WHERE key=%s", (value, key))
    with pytest.raises(SchemaRevisionError):
        runner(database).run(expected_revision=SCHEMA_REVISION)


def test_guarded_schema_refuses_foreign_database_without_creating_metadata(database):
    with database.transaction() as tx:
        tx.execute("CREATE TABLE foreign_table(value TEXT)")
    with pytest.raises(SchemaRevisionError, match="neither empty"):
        runner(database).run(expected_revision=SCHEMA_REVISION)
    with database.transaction() as tx:
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM sqlite_master WHERE name='schema_meta'")["n"]
            == 0
        )


def test_runtime_reports_the_sqlite_digest_and_reuses_durable_hook_marker(tmp_path):
    url = f"sqlite://{tmp_path / 'plane.db'}"
    first, second = Hook(), Hook()
    for hook in (first, second):
        runtime = create_runtime(url, identity=f"sqlite-{uuid4()}", reconcilers=(hook,))
        try:
            runtime.initialize()
            assert runtime.health().ready
            assert (
                runtime.inspect_compatibility().migration_digest == SQLITE_MIGRATION_REGISTRY.digest
            )
        finally:
            runtime.close()
    assert first.calls == 1 and second.calls == 0


def test_reconciliation_lock_is_cross_process_and_released_after_failure(tmp_path):
    path = tmp_path / "plane.db"
    pool = ConnectionPool(create_sqlite_driver_pool(f"sqlite://{path}"))
    coordinator = SQLiteReconciliationCoordinator(pool, path, timeout_seconds=0.01)
    values = dict(
        advisory_lock=RECONCILIATION_ADVISORY_LOCK,
        schema_revision=SCHEMA_REVISION,
        plan_digest="a" * 64,
    )
    try:
        with coordinator.coordinate(**values):
            script = """
from pathlib import Path
import sys
from astralplane.database.sqlite_reconciliation import SQLiteReconciliationCoordinator
from astralplane.errors import ReconciliationError
from astralplane.reconciliation import RECONCILIATION_ADVISORY_LOCK
coordinator=SQLiteReconciliationCoordinator(None, Path(sys.argv[1]), timeout_seconds=0.001)
try:
    with coordinator.coordinate(advisory_lock=RECONCILIATION_ADVISORY_LOCK,
          schema_revision='089.022',plan_digest='a'*64):
        raise AssertionError('lock was not exclusive')
except ReconciliationError:
    pass
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0, result.stderr
        with pytest.raises(RuntimeError), coordinator.coordinate(**values):
            raise RuntimeError("hook interrupted")
        with coordinator.coordinate(**values):
            pass
    finally:
        pool.close()


@pytest.mark.parametrize(
    "function,valid,invalid",
    [
        (
            _identifier_is_canonical,
            ["owner", "a/b:c@x", "x" * 128],
            ["", " space", "a!", "a/", "x" * 129],
        ),
        (
            _blob_owner_is_canonical,
            ["owner", "a@example.com", "__verif__test"],
            ["", "CON", "lpt1.txt", "owner.", "owner ", "a/b", "x" * 256],
        ),
        (
            _blob_storage_key_is_canonical,
            ["file", "project/file.txt", "x" * 255],
            [
                "",
                "/absolute",
                "a\\b",
                "a/../b",
                "a/./b",
                "CON.txt",
                "x.",
                "bad?",
                "a\x00b",
                ".astralplane-internal/x",
                "x" * 256,
                "/".join(["x"] * 33),
                "x" * 4097,
            ],
        ),
        (
            _attachment_id_is_canonical,
            ["attachment-1", "a@example"],
            ["", "a/b", "NUL", "bad ", "a:b"],
        ),
    ],
)
def test_identifier_constraints_fail_closed_on_nonportable_names(function, valid, invalid):
    assert function(None) is None
    for value in valid:
        assert function(value) == 1
    for value in invalid:
        assert function(value) == 0


@pytest.mark.parametrize(
    "function,valid,invalid",
    [
        (
            _capabilities_are_canonical,
            ['["a"]', '["a","b"]'],
            ["invalid", "{}", "[]", "[1]", '["b","a"]', '["a","a"]', '["bad "]'],
        ),
        (
            _positive_unique_int_array,
            ["[1]", "[1,3,2]"],
            ["invalid", "{}", "[]", "[0]", "[true]", "[1,1]", '["1"]'],
        ),
    ],
)
def test_array_constraints_preserve_canonical_types_and_order(function, valid, invalid):
    assert function(None) is None
    for value in valid:
        assert function(value) == 1
    for value in invalid:
        assert function(value) == 0


def test_selected_input_constraint_accepts_only_the_closed_envelope():
    import copy
    import json

    payload = {
        "version": 1,
        "expansion_version": 1,
        "references": [],
        "binding_key_id": "local_1",
        "combined_binding": "a" * 64,
        "agent": {
            "agent_id": "agent",
            "revision_id": "00000000-0000-4000-8000-000000000001",
            "definition_digest": "b" * 64,
            "kind": "declarative",
        },
    }
    assert _valid_selected_input(None) is None
    assert _valid_selected_input("invalid") == 0
    assert _valid_selected_input("[]") == 0
    assert _valid_selected_input(json.dumps(payload)) == 1
    invalid = [
        ("version", True),
        ("version", 2),
        ("expansion_version", "1"),
        ("binding_key_id", None),
        ("binding_key_id", "Invalid"),
        ("combined_binding", None),
        ("combined_binding", "z" * 64),
        ("references", {}),
        ("references", [{}] * 29),
        ("references", [{}]),
        ("references", [{"kind": "unknown", "resource_id": "x", "revision": 1}]),
        ("agent", {}),
        ("agent", None),
    ]
    for key, value in invalid:
        candidate = copy.deepcopy(payload)
        candidate[key] = value
        assert _valid_selected_input(json.dumps(candidate)) == 0
    for key, value in [
        ("kind", "executable"),
        ("agent_id", "bad\n"),
        ("agent_id", "x" * 17000),
        ("revision_id", "invalid"),
        ("definition_digest", "invalid"),
    ]:
        candidate = copy.deepcopy(payload)
        candidate["agent"][key] = value
        assert _valid_selected_input(json.dumps(candidate)) == 0
    reference = {
        "kind": "skill",
        "resource_id": "00000000-0000-4000-8000-000000000001",
        "revision": 1,
    }
    for key, value in [
        ("resource_id", "invalid"),
        ("revision", True),
        ("revision", 0),
        ("revision", 2**53),
    ]:
        candidate = copy.deepcopy(payload)
        candidate["references"] = [dict(reference, **{key: value})]
        assert _valid_selected_input(json.dumps(candidate)) == 0
    candidate = copy.deepcopy(payload)
    candidate["references"] = [reference, reference]
    assert _valid_selected_input(json.dumps(candidate)) == 0
    candidate["references"] = [reference]
    candidate["agent"] = None
    assert _valid_selected_input(json.dumps(candidate)) == 1


def test_statement_and_transaction_clocks_have_distinct_stability(database):
    with database.transaction() as tx:
        first = tx.fetch_one(
            "SELECT now() AS tx_time,statement_timestamp() AS first,statement_timestamp() AS second"
        )
        assert first["first"] == first["second"]
        second = tx.fetch_one("SELECT now() AS tx_time,statement_timestamp() AS observed")
        assert first["tx_time"] == second["tx_time"]


def test_settings_roll_back_with_savepoints_and_expire_after_transaction(database):
    with database.transaction() as tx:
        tx.execute("CREATE TABLE value_table(value INTEGER)")
        tx.execute("SET LOCAL lock_timeout='50ms'")
        with pytest.raises(RuntimeError), tx.savepoint("settings"):
            tx.execute("SET LOCAL lock_timeout='1ms'")
            tx.execute("SET LOCAL audit.allow_purge='true'")
            raise RuntimeError("cancelled")
        assert tx.fetch_one("SELECT current_setting('lock_timeout') AS value")["value"] == "50ms"
        assert (
            tx.fetch_one("SELECT current_setting('audit.allow_purge',true) AS value")["value"]
            is None
        )
    with database.transaction() as tx:
        assert tx.fetch_one("SELECT current_setting('lock_timeout') AS value")["value"] == "0"


def test_structure_digest_distinguishes_quoted_literals():
    from astralplane.database.sqlite_migrations import sqlite_structure_digest

    def digest(sql):
        return sqlite_structure_digest((("table", "example", "example", sql),))

    assert digest("CREATE TABLE example (value TEXT DEFAULT 'a b')") != digest(
        "CREATE TABLE example (value TEXT DEFAULT 'a  b')"
    )
    assert digest("CREATE TABLE example (value TEXT)") == digest(
        "CREATE  TABLE\nexample (value TEXT)"
    )


def test_lock_contention_uses_the_shared_repository_conflict_type():
    from astralplane.errors import RepositoryConflictError as SharedConflict
    from astralplane.repositories import RepositoryConflictError

    error = SQLiteLockTimeoutError()
    assert SharedConflict is RepositoryConflictError
    assert isinstance(error, RepositoryConflictError)
    assert error.code == "repository_busy" and error.pgcode == "55P03"


def test_lock_words_in_a_native_sql_error_do_not_create_contention(database):
    with pytest.raises(sqlite3.OperationalError) as caught, database.transaction() as tx:
        tx.fetch_one("SELECT [database is locked]")
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert not is_lock_contention(caught.value)


@pytest.mark.parametrize("method", ["fetchall", "fetchone"])
def test_statement_deadline_covers_deferred_cursor_consumption(database, monkeypatch, method):
    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    with database._pool.connection() as connection:
        connection.set_local_setting("statement_timeout", "1ms")
        cursor = connection.cursor()
        cursor.execute(
            "WITH RECURSIVE sequence(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM sequence "
            "WHERE n<100000) SELECT n FROM sequence"
        )
        observed[0] = 100.0
        with pytest.raises(TimeoutError, match="statement deadline exceeded") as caught:
            getattr(cursor, method)()
        assert not hasattr(caught.value, "sqlite_errorcode")
        cursor.close()
        connection.rollback()
        with connection.cursor() as retry:
            retry.execute("SELECT 1 AS usable")
            assert retry.fetchall() == [(1,)]


def test_vm_work_crossing_deadline_produces_native_interrupt(database, monkeypatch):
    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    with database._pool.connection() as connection:

        def advance(value):
            if value >= 10:
                observed[0] = 100.0
            return value

        connection.raw.create_function("advance_clock", 1, advance)
        connection.set_local_setting("statement_timeout", "1ms")
        cursor = connection.cursor()
        cursor.execute(
            "WITH RECURSIVE sequence(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM sequence "
            "WHERE n<100000) SELECT advance_clock(n) FROM sequence"
        )
        with pytest.raises(sqlite3.OperationalError, match="interrupted") as caught:
            cursor.fetchall()
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        cursor.close()
        connection.rollback()
        assert connection.raw.execute("SELECT 1").fetchall() == [(1,)]


def test_short_final_fetch_checks_expired_deadline_without_vm_progress(database, monkeypatch):
    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    with database._pool.connection() as connection:
        connection.set_local_setting("statement_timeout", "1ms")
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        observed[0] = 100.0
        with pytest.raises(TimeoutError, match="statement deadline exceeded"):
            cursor.fetchone()
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchall()
        cursor.close()


@pytest.mark.parametrize("phase", ["execute", "fetch"])
def test_short_step_crossing_deadline_cannot_return_late_results(database, monkeypatch, phase):
    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    with database._pool.connection() as connection:

        def advance(value):
            if phase == "execute" or value == 1:
                observed[0] = 100.0
            return value

        connection.raw.create_function("advance_clock", 1, advance)
        connection.set_local_setting("statement_timeout", "1ms")
        cursor = connection.cursor()
        query = "WITH seq(n) AS (VALUES(0),(1)) SELECT advance_clock(n) FROM seq"
        with pytest.raises(TimeoutError, match="statement deadline exceeded"):
            cursor.execute(query)
            cursor.fetchone()
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchall()
        cursor.close()


@pytest.mark.parametrize("prior_read", [False, True])
def test_locked_select_internal_probe_consumption_keeps_original_deadline(
    database, monkeypatch, prior_read
):
    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    with database._pool.connection() as connection:

        def advance(value):
            if value >= 10:
                observed[0] = 100.0
            return value

        connection.raw.create_function("advance_clock", 1, advance)
        connection.raw.execute("CREATE TABLE probe_values(value INTEGER)")
        connection.raw.execute("INSERT INTO probe_values VALUES(1)")
        if prior_read:
            connection.begin_if_needed(write=False)
            connection.raw.execute("SELECT * FROM probe_values").fetchall()
        connection.set_local_setting("statement_timeout", "1ms")
        cursor = connection.cursor()
        with pytest.raises(sqlite3.OperationalError, match="interrupted") as caught:
            cursor.execute(
                "WITH RECURSIVE sequence(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM sequence "
                "WHERE n<100000) SELECT value,n FROM probe_values CROSS JOIN sequence "
                "WHERE advance_clock(n) IN (0,100000) FOR UPDATE OF probe_values"
            )
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        cursor.close()
        connection.rollback()
        assert connection.raw.execute("SELECT 1").fetchall() == [(1,)]


def test_writer_wait_does_not_reset_statement_deadline_for_short_select(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from tests.fixtures.sqlite_template import ObservedWriterGate

    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    driver = create_sqlite_driver_pool(f"sqlite://{tmp_path / 'deadline.db'}")
    gate = ObservedWriterGate()
    driver._writer_gate = gate
    first, second = driver.getconn(), driver.getconn()
    try:
        first.raw.execute("CREATE TABLE value_table(value INTEGER)")
        first.raw.execute("INSERT INTO value_table VALUES(0)")
        with first.cursor() as held:
            held.execute("UPDATE value_table SET value=1")
        gate.observe = True

        def waiting():
            second.set_local_setting("statement_timeout", "1ms")
            try:
                with second.cursor() as contender:
                    contender.execute("SELECT value FROM value_table FOR UPDATE")
            finally:
                second.rollback()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(waiting)
            assert gate.waiting.wait(5)
            observed[0] = 100.0
            first.commit()
            with pytest.raises(TimeoutError, match="statement deadline exceeded"):
                future.result(5)
        assert second.raw.execute("SELECT value FROM value_table").fetchone() == (1,)
    finally:
        driver.putconn(first)
        driver.putconn(second)
        driver.closeall()


def test_cursor_close_clears_deadline_before_connection_reuse(database, monkeypatch):
    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    with database._pool.connection() as connection:
        connection.set_local_setting("statement_timeout", "1ms")
        cursor = connection.cursor()
        cursor.execute(
            "WITH RECURSIVE sequence(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM sequence "
            "WHERE n<100000) SELECT n FROM sequence"
        )
        cursor.close()
        observed[0] = 100.0
        assert connection.raw.execute("SELECT 1").fetchall() == [(1,)]


def test_idle_cursor_does_not_leave_a_global_connection_progress_handler(database, monkeypatch):
    observed = [0.0]
    monkeypatch.setattr("astralplane.database.sqlite.time.monotonic", lambda: observed[0])
    with database._pool.connection() as connection:
        connection.set_local_setting("statement_timeout", "1ms")
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        observed[0] = 100.0
        assert (
            len(
                connection.raw.execute(
                    "WITH RECURSIVE seq(n) AS (VALUES(0) UNION ALL "
                    "SELECT n+1 FROM seq WHERE n<1000) "
                    "SELECT n FROM seq"
                ).fetchall()
            )
            == 1001
        )
        with pytest.raises(TimeoutError):
            cursor.fetchone()
        cursor.close()


def test_baseline_rejects_invalid_uuid_nul_and_integer_width(database):
    runner(database).run(expected_revision=SCHEMA_REVISION)
    with database.transaction() as tx:
        tx.execute("INSERT INTO user_personalization(user_id) VALUES('owner')")
        for statement, parameters in (
            ("INSERT INTO agent_trust(agent_id) VALUES(%s)", ("bad\x00identity",)),
            ("UPDATE user_personalization SET goals=%s WHERE user_id='owner'", ('["\\u0000"]',)),
            (
                "UPDATE operation_admission_class SET active_limit=%s WHERE class_name='global'",
                (2**31,),
            ),
            (
                "INSERT INTO chat_project(owner_id,project_id,name,position,created_at,updated_at,"
                "create_digest) VALUES('owner',%s,'name',0,1,1,%s)",
                ("invalid", "a" * 64),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError), tx.savepoint("invalid_scalar"):
                tx.execute(statement, parameters)
        tx.execute(
            "UPDATE operation_admission_class SET active_limit=%s WHERE class_name='global'",
            (2**31 - 1,),
        )
        assert (
            tx.fetch_one(
                "SELECT active_limit FROM operation_admission_class WHERE class_name='global'"
            )["active_limit"]
            == 2**31 - 1
        )
        assert tx.fetch_one("SELECT count(*) AS n FROM chat_project")["n"] == 0


def test_transaction_started_by_a_refused_try_lock_reports_a_stale_snapshot_on_write(tmp_path):
    from contextlib import ExitStack

    path = tmp_path / "plane.db"
    pools = [
        ConnectionPool(create_sqlite_driver_pool(f"sqlite://{path}", busy_timeout_seconds=0.05))
        for _ in range(2)
    ]
    first, second = [PlaneDatabase(pool) for pool in pools]
    try:
        runner(first).run(expected_revision=SCHEMA_REVISION)
        with first.transaction() as tx:
            tx.execute("CREATE TABLE value_table(value INTEGER)")
            tx.execute("INSERT INTO value_table VALUES(1)")
        with ExitStack() as stack:
            held = stack.enter_context(first.transaction())
            held.fetch_one("SELECT pg_advisory_xact_lock(%s,%s)", (1, 2))
            with (
                pytest.raises(SQLiteLockTimeoutError, match="retry the transaction"),
                second.transaction() as contender,
            ):
                query = "SELECT pg_try_advisory_xact_lock(%s,%s) AS acquired"
                assert contender.fetch_one(query, (1, 2))["acquired"] is False
                assert contender.fetch_one("SELECT value FROM value_table")["value"] == 1
                held.execute("UPDATE value_table SET value=2")
                stack.close()
                assert contender.fetch_one(query, (1, 2))["acquired"] is False
                contender.execute("UPDATE value_table SET value=3")
        with second.transaction() as fresh:
            fresh.execute("UPDATE value_table SET value=3")
            assert fresh.fetch_one("SELECT value FROM value_table")["value"] == 3
    finally:
        for pool in pools:
            pool.close()


def test_try_lock_reports_a_real_writer_reservation_and_retries_fresh(tmp_path):
    path = tmp_path / "plane.db"
    pools = [
        ConnectionPool(create_sqlite_driver_pool(f"sqlite://{path}", busy_timeout_seconds=0.05))
        for _ in range(2)
    ]
    first, second = [PlaneDatabase(pool) for pool in pools]
    try:
        runner(first).run(expected_revision=SCHEMA_REVISION)
        with first.transaction() as held:
            held.fetch_one("SELECT pg_advisory_xact_lock(%s,%s)", (1, 2))
            with second.transaction() as contender:
                assert (
                    contender.fetch_one(
                        "SELECT pg_try_advisory_xact_lock(%s,%s) AS acquired", (1, 2)
                    )["acquired"]
                    is False
                )
                assert contender.fetch_one("SELECT 1 AS usable")["usable"] == 1
        with second.transaction() as retry:
            assert (
                retry.fetch_one("SELECT pg_try_advisory_xact_lock(%s,%s) AS acquired", (1, 2))[
                    "acquired"
                ]
                is True
            )
    finally:
        for pool in pools:
            pool.close()
