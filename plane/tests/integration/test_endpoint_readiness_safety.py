"""Exercise readiness against hostile metadata and concurrent PostgreSQL changes.
The public observer must refuse unsafe catalogs without leaving effects or borrowed sessions.
"""

from __future__ import annotations

import pytest

from astralplane import EndpointReadinessState, inspect_endpoint_readiness
from tests.integration.test_endpoint_failover_readiness import _request


def _hostile_metadata(transaction):
    transaction.execute("ALTER TABLE schema_meta RENAME TO saved_schema_meta")
    transaction.execute("CREATE TABLE unexpected_effect (value integer)")
    transaction.execute(
        "CREATE FUNCTION hostile_meta(value text) RETURNS text LANGUAGE plpgsql AS $$ "
        "BEGIN INSERT INTO unexpected_effect VALUES (1); RETURN value; END $$"
    )
    transaction.execute(
        "CREATE VIEW schema_meta AS "
        "SELECT key,hostile_meta(value) AS value FROM saved_schema_meta"
    )


def _inspect(database, tmp_path):
    root = tmp_path / "blobs"
    root.mkdir(exist_ok=True)
    return inspect_endpoint_readiness(**_request(database, {"blobs": root}))


def _assert_no_effects_or_sessions(database):
    with database.database.transaction() as transaction:
        assert transaction.fetch_one("SELECT count(*) AS n FROM unexpected_effect")["n"] == 0
        assert transaction.fetch_one(
            "SELECT count(*) AS n FROM pg_catalog.pg_stat_activity "
            "WHERE datname = pg_catalog.current_database() "
            "AND application_name = 'astralplane:endpoint-readiness'"
        )["n"] == 0


def test_substituted_metadata_view_is_refused_without_committed_effects(
    migrated_clone, tmp_path
):
    with migrated_clone.database.transaction() as transaction:
        _hostile_metadata(transaction)

    report = _inspect(migrated_clone, tmp_path)

    assert report.state is EndpointReadinessState.INCOMPATIBLE
    assert report.fence is None
    _assert_no_effects_or_sessions(migrated_clone)


def test_migration_contention_is_refused_without_waiting_for_the_owner(
    migrated_clone, tmp_path
):
    from psycopg2.extensions import make_dsn

    from astralplane.database.migrations import CURRENT_DATA_PLANE_REVISION

    root = tmp_path / "blobs"
    root.mkdir()
    request = _request(migrated_clone, {"blobs": root})
    request["database_url"] = make_dsn(
        request["database_url"],
        options=f"-csearch_path={migrated_clone.schema},pg_catalog -cstatement_timeout=100",
    )
    with migrated_clone.database.transaction() as transaction:
        transaction.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock(%s, %s)",
            CURRENT_DATA_PLANE_REVISION.migration_lock,
        )
        report = inspect_endpoint_readiness(**request)
        assert report.state is EndpointReadinessState.UNAVAILABLE
        assert report.reasons == ("migration_in_progress",)
        assert transaction.fetch_one("SELECT 1 AS usable")["usable"] == 1


def test_attested_metadata_cannot_be_replaced_before_its_read(
    migrated_clone, tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from psycopg2.errors import LockNotAvailable

    from astralplane import endpoint_readiness as module

    attested, resume = Event(), Event()
    verify = module.MIGRATION_REGISTRY.verify_current

    def pause(transaction):
        verify(transaction)
        attested.set()
        assert resume.wait(10)

    monkeypatch.setattr(module.MIGRATION_REGISTRY, "verify_current", pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_inspect, migrated_clone, tmp_path)
        try:
            assert attested.wait(10)
            with (
                pytest.raises(LockNotAvailable),
                migrated_clone.database.transaction() as transaction,
            ):
                transaction.execute("SET LOCAL lock_timeout = '50ms'")
                _hostile_metadata(transaction)
        finally:
            resume.set()
        assert future.result(timeout=10).state is EndpointReadinessState.WRITABLE
    with migrated_clone.database.transaction() as transaction:
        assert transaction.fetch_one(
            "SELECT pg_catalog.to_regclass('unexpected_effect') AS effect"
        )["effect"] is None


def test_observation_is_read_only_and_rolls_back_notifications(
    migrated_clone, tmp_path, monkeypatch
):
    import psycopg2

    from astralplane import endpoint_readiness as module

    verify = module.MIGRATION_REGISTRY.verify_current
    posture = []

    def notify(transaction):
        verify(transaction)
        posture.append(transaction.fetch_one("SHOW transaction_read_only")["transaction_read_only"])
        transaction.execute("SELECT pg_catalog.pg_notify('readiness_effect', 'synthetic')")

    monkeypatch.setattr(module.MIGRATION_REGISTRY, "verify_current", notify)
    listener = psycopg2.connect(migrated_clone.dsn)
    try:
        listener.autocommit = True
        with listener.cursor() as cursor:
            cursor.execute("LISTEN readiness_effect")
        report = _inspect(migrated_clone, tmp_path)
        with listener.cursor() as cursor:
            cursor.execute("SELECT 1")
        listener.poll()
        assert report.state is EndpointReadinessState.WRITABLE
        assert posture == ["on"]
        assert listener.notifies == []
    finally:
        listener.close()


def test_namespace_replacement_cannot_borrow_the_attested_identity(
    migrated_clone, tmp_path, monkeypatch
):
    from astralplane import endpoint_readiness as module

    verify = module.MIGRATION_REGISTRY.verify_current

    def replace_namespace(transaction):
        verify(transaction)
        with migrated_clone.database.transaction() as other:
            other.execute(f'ALTER SCHEMA "{migrated_clone.schema}" RENAME TO old_readiness_schema')
            other.execute(f'CREATE SCHEMA "{migrated_clone.schema}"')
            other.execute(
                f'CREATE TABLE "{migrated_clone.schema}".schema_meta AS '
                'SELECT * FROM old_readiness_schema.schema_meta'
            )

    monkeypatch.setattr(module.MIGRATION_REGISTRY, "verify_current", replace_namespace)
    report = _inspect(migrated_clone, tmp_path)
    assert not report.writable
    assert report.fence is None


def test_metadata_lock_contention_is_unavailable_and_releases_the_observer(
    migrated_clone, tmp_path
):
    with migrated_clone.database.transaction() as transaction:
        transaction.execute("LOCK TABLE schema_meta IN ACCESS EXCLUSIVE MODE")
        report = _inspect(migrated_clone, tmp_path)
        assert report.state is EndpointReadinessState.UNAVAILABLE
        assert report.fence is None
        assert transaction.fetch_one(
            "SELECT count(*) AS n FROM pg_catalog.pg_stat_activity "
            "WHERE datname = pg_catalog.current_database() "
            "AND application_name = 'astralplane:endpoint-readiness'"
        )["n"] == 0


def test_query_wait_is_cancelled_by_the_observers_server_limit(
    migrated_clone, tmp_path, monkeypatch
):
    from psycopg2.errors import QueryCanceled

    from astralplane import endpoint_readiness as module

    failures = []

    def wait(transaction):
        try:
            transaction.fetch_one("SELECT pg_catalog.pg_sleep(60)")
        except QueryCanceled as error:
            failures.append(error.pgcode)
            raise

    monkeypatch.setattr(module.MIGRATION_REGISTRY, "verify_current", wait)
    report = _inspect(migrated_clone, tmp_path)
    assert report.state is EndpointReadinessState.UNAVAILABLE
    assert failures == ["57014"]
    with migrated_clone.database.transaction() as transaction:
        assert transaction.fetch_one(
            "SELECT count(*) AS n FROM pg_catalog.pg_stat_activity "
            "WHERE datname = pg_catalog.current_database() "
            "AND application_name = 'astralplane:endpoint-readiness'"
        )["n"] == 0


def _trace_connections(monkeypatch, *, failure=None):
    from astralplane import endpoint_readiness as module

    create = module.create_postgres_driver_pool
    connections = []

    class Connection:
        def __init__(self, connection):
            self.connection = connection
            self.rollbacks = 0
            self.commits = 0

        def __getattr__(self, name):
            return getattr(self.connection, name)

        @property
        def autocommit(self):
            return self.connection.autocommit

        @autocommit.setter
        def autocommit(self, value):
            self.connection.autocommit = value

        def rollback(self):
            self.rollbacks += 1
            self.connection.rollback()
            if failure == f"rollback-{self.rollbacks}":
                raise OSError("synthetic rollback acknowledgement lost")

        def commit(self):
            self.commits += 1
            self.connection.commit()

    class Driver:
        def __init__(self, driver):
            self.driver = driver

        def getconn(self):
            connection = Connection(self.driver.getconn())
            connections.append(connection)
            return connection

        def putconn(self, connection, *, close=False):
            if failure == "release":
                raise OSError("synthetic release failure")
            self.driver.putconn(connection.connection, close=close)

        def closeall(self):
            self.driver.closeall()

    def traced(*args, **kwargs):
        return Driver(create(*args, **kwargs))

    monkeypatch.setattr(module, "create_postgres_driver_pool", traced)
    return connections


@pytest.mark.parametrize("failure", ["rollback-1", "rollback-2", "release"])
def test_transaction_cleanup_fault_never_publishes_a_fence(
    migrated_clone, tmp_path, monkeypatch, failure
):
    connections = _trace_connections(monkeypatch, failure=failure)
    report = _inspect(migrated_clone, tmp_path)
    assert report.state is EndpointReadinessState.UNAVAILABLE
    assert report.fence is None
    assert len(connections) == 1
    assert connections[0].connection.closed
    assert connections[0].commits == 0


@pytest.mark.parametrize("replacement", ["view", "rls", "domain"])
def test_namespace_rebound_metadata_cannot_execute_view_or_policy_functions(
    migrated_clone, tmp_path, monkeypatch, replacement
):
    from uuid import uuid4

    from astralplane import endpoint_readiness as module

    role = "readiness_" + uuid4().hex
    schema = f'"{migrated_clone.schema}"'
    verify = module.MIGRATION_REGISTRY.verify_current
    connections = _trace_connections(monkeypatch)
    role_created = False

    def replace_namespace(transaction):
        nonlocal role_created
        verify(transaction)
        with migrated_clone.database.transaction() as other:
            other.execute(f"ALTER SCHEMA {schema} RENAME TO old_readiness_schema")
            other.execute(f"CREATE SCHEMA {schema}")
            other.execute(f"CREATE TABLE {schema}.unexpected_effect (value integer)")
            if replacement == "view":
                other.execute(
                    f"CREATE FUNCTION {schema}.hostile_meta(value text) RETURNS text "
                    "LANGUAGE plpgsql AS $$ BEGIN RAISE NOTICE 'hostile invocation'; "
                    f"INSERT INTO {schema}.unexpected_effect VALUES (1); RETURN value; END $$"
                )
                other.execute(
                    f"CREATE VIEW {schema}.schema_meta AS "
                    f"SELECT tableoid,key,{schema}.hostile_meta(value) AS value "
                    "FROM old_readiness_schema.schema_meta"
                )
            elif replacement == "rls":
                other.execute(f"CREATE ROLE {role}")
                role_created = True
                other.execute(
                    f"CREATE TABLE {schema}.schema_meta AS "
                    "SELECT * FROM old_readiness_schema.schema_meta"
                )
                other.execute(
                    f"CREATE FUNCTION {schema}.hostile_policy() RETURNS boolean "
                    "LANGUAGE plpgsql AS $$ BEGIN RAISE NOTICE 'hostile invocation'; "
                    f"INSERT INTO {schema}.unexpected_effect VALUES (1); RETURN TRUE; END $$"
                )
                other.execute(f"ALTER TABLE {schema}.schema_meta ENABLE ROW LEVEL SECURITY")
                other.execute(f"ALTER TABLE {schema}.schema_meta FORCE ROW LEVEL SECURITY")
                other.execute(
                    f"CREATE POLICY hostile_policy ON {schema}.schema_meta "
                    f"USING ({schema}.hostile_policy())"
                )
                other.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
                other.execute(f"GRANT SELECT ON {schema}.schema_meta TO {role}")
                other.execute(f"GRANT INSERT ON {schema}.unexpected_effect TO {role}")
            else:
                other.execute(f"CREATE DOMAIN {schema}.metadata_text AS text")
                other.execute(
                    f"CREATE TABLE {schema}.schema_meta "
                    f"(key {schema}.metadata_text, value {schema}.metadata_text)"
                )
                other.execute(
                    f"INSERT INTO {schema}.schema_meta "
                    "SELECT * FROM old_readiness_schema.schema_meta"
                )
        if replacement == "rls":
            transaction.execute(f"SET LOCAL ROLE {role}")

    monkeypatch.setattr(module.MIGRATION_REGISTRY, "verify_current", replace_namespace)
    try:
        report = _inspect(migrated_clone, tmp_path)
        assert not report.writable and report.fence is None
        assert len(connections) == 1
        assert not any("hostile invocation" in notice for notice in connections[0].notices)
        assert connections[0].commits == 0
        _assert_no_effects_or_sessions(migrated_clone)
    finally:
        if role_created:
            with migrated_clone.database.transaction() as transaction:
                transaction.execute(f"DROP OWNED BY {role}")
                transaction.execute(f"DROP ROLE {role}")


@pytest.mark.parametrize("boundary", ["before-lock", "after-read"])
def test_namespace_identity_is_rechecked_at_both_sides_of_the_metadata_read(
    migrated_clone, tmp_path, monkeypatch, boundary
):
    from astralplane.database.transaction import Transaction

    execute, fetch_all = Transaction.execute, Transaction.fetch_all
    replaced = []

    def replace_namespace():
        with migrated_clone.database.transaction() as other:
            other.execute(f'ALTER SCHEMA "{migrated_clone.schema}" RENAME TO old_readiness_schema')
            other.execute(f'CREATE SCHEMA "{migrated_clone.schema}"')
            other.execute(
                f'CREATE TABLE "{migrated_clone.schema}".schema_meta AS '
                'SELECT * FROM old_readiness_schema.schema_meta'
            )
        replaced.append(True)

    def before_lock(transaction, statement, parameters=()):
        if statement.startswith("LOCK TABLE ONLY"):
            replace_namespace()
        return execute(transaction, statement, parameters)

    def after_read(transaction, statement, parameters=()):
        result = fetch_all(transaction, statement, parameters)
        if statement.startswith("WITH metadata AS MATERIALIZED"):
            replace_namespace()
        return result

    if boundary == "before-lock":
        monkeypatch.setattr(Transaction, "execute", before_lock)
    else:
        monkeypatch.setattr(Transaction, "fetch_all", after_read)
    report = _inspect(migrated_clone, tmp_path)
    assert replaced == [True]
    assert report.state is EndpointReadinessState.INCOMPATIBLE
    assert report.reasons == ("schema_identity_changed",)
    assert report.fence is None


@pytest.mark.parametrize("isolation", ["repeatable read", "serializable"])
@pytest.mark.parametrize("posture", ["writable", "replaced", "read-only"])
def test_inherited_isolation_cannot_hide_a_committed_namespace_replacement(
    migrated_clone, tmp_path, monkeypatch, isolation, posture
):
    from psycopg2.extensions import make_dsn

    from astralplane import endpoint_readiness as module
    from astralplane.database.transaction import Transaction

    observe, fetch_all = module._observe_database, Transaction.fetch_all
    observations, replacements = [], []
    connections = _trace_connections(monkeypatch)

    def observed(transaction, **kwargs):
        observations.append(
            (
                transaction.fetch_one("SHOW default_transaction_isolation")[
                    "default_transaction_isolation"
                ],
                transaction.fetch_one("SHOW transaction_isolation")["transaction_isolation"],
            )
        )
        return observe(transaction, **kwargs)

    def after_read(transaction, statement, parameters=()):
        result = fetch_all(transaction, statement, parameters)
        if posture == "replaced" and statement.startswith("WITH metadata AS MATERIALIZED"):
            with migrated_clone.database.transaction() as other:
                other.execute(
                    f'ALTER SCHEMA "{migrated_clone.schema}" RENAME TO old_readiness_schema'
                )
                other.execute(f'CREATE SCHEMA "{migrated_clone.schema}"')
                other.execute(
                    f'CREATE TABLE "{migrated_clone.schema}".schema_meta AS '
                    'SELECT * FROM old_readiness_schema.schema_meta'
                )
            replacements.append(True)
        return result

    monkeypatch.setattr(module, "_observe_database", observed)
    monkeypatch.setattr(Transaction, "fetch_all", after_read)
    root = tmp_path / "blobs"
    root.mkdir()
    request = _request(migrated_clone, {"blobs": root})
    escaped_isolation = isolation.replace(" ", "\\ ")
    options = (
        f"-csearch_path={migrated_clone.schema},pg_catalog "
        f"-cdefault_transaction_isolation={escaped_isolation}"
    )
    if posture == "read-only":
        options += " -cdefault_transaction_read_only=on"
    request["database_url"] = make_dsn(request["database_url"], options=options)
    report = inspect_endpoint_readiness(**request)

    assert replacements == ([True] if posture == "replaced" else [])
    expected_state = {
        "writable": EndpointReadinessState.WRITABLE,
        "replaced": EndpointReadinessState.INCOMPATIBLE,
        "read-only": EndpointReadinessState.NOT_PRIMARY,
    }[posture]
    assert report.state is expected_state
    assert (report.fence is not None) is (posture == "writable")
    if posture == "replaced":
        assert report.reasons == ("schema_identity_changed",)
    assert observations == [(isolation, "read committed")]
    assert len(connections) == 1
    assert connections[0].commits == 0
    assert connections[0].connection.closed
