"""Real-PostgreSQL tests for astralplane.repositories.history: request-scoped
statement-timeout caps reset at transaction end and release pool borrows and locks
even when a blocked query is still pending.
"""

from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, uid

from astralplane.repositories.history import SessionRepository
from tests.fixtures.database_errors import LOCK_ERRORS


def schema(database):
    with database.transaction() as transaction:
        return transaction.fetch_one("SELECT current_schema() AS name")["name"]


def settings(transaction):
    if transaction.dialect.is_sqlite:
        from astralplane.database.sqlite_functions import timeout_seconds

        return {
            name: round(
                timeout_seconds(
                    transaction.fetch_one("SELECT current_setting(%s) AS value", (name,))["value"]
                )
                * 1000
            )
            for name in ("lock_timeout", "statement_timeout")
        }
    return {
        row["name"]: int(row["setting"])
        for row in transaction.fetch_all(
            "SELECT name, setting FROM pg_settings "
            "WHERE name IN ('lock_timeout', 'statement_timeout')"
        )
    }


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("lock,statement", [(0, 0), (25, 250), (5000, 5000)])
def test_request_caps_preserve_stricter_settings_and_reset_at_transaction_end(
    database, rollback, lock, statement
):
    with independent_database(schema(database)) as selected:
        with selected.transaction() as transaction:
            transaction.execute("SELECT set_config('lock_timeout', %s, false)", (str(lock),))
            transaction.execute(
                "SELECT set_config('statement_timeout', %s, false)", (str(statement),)
            )
        try:
            with selected.transaction() as transaction:
                SessionRepository.bound_request_execution_waits(transaction)
                assert settings(transaction) == {
                    "lock_timeout": min(lock or 100, 100),
                    "statement_timeout": min(statement or 1000, 1000),
                }
                if rollback:
                    raise RuntimeError("synthetic rollback")
        except RuntimeError:
            assert rollback
        with selected.transaction() as transaction:
            assert settings(transaction) == {"lock_timeout": lock, "statement_timeout": statement}


@pytest.mark.parametrize("lock_kind", ["owner", "table"])
def test_blocked_query_finishes_and_returns_pool_borrow_while_blocker_remains(database, lock_kind):
    owner = uid()
    with (
        independent_database(schema(database)) as worker_database,
        database.transaction() as blocker,
    ):
        if lock_kind == "owner":
            blocker.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner,))
        else:
            blocker.execute(
                "UPDATE web_session SET sid=sid WHERE 0"
                if blocker.dialect.is_sqlite
                else "LOCK TABLE web_session IN ACCESS EXCLUSIVE MODE"
            )

        def blocked():
            with worker_database.transaction() as transaction:
                SessionRepository.bound_request_execution_waits(transaction)
                if lock_kind == "owner":
                    transaction.fetch_one(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner,)
                    )
                else:
                    if transaction.dialect.is_sqlite:
                        transaction.execute("UPDATE web_session SET sid=sid WHERE 0")
                    else:
                        transaction.fetch_one("SELECT sid FROM web_session LIMIT 1")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(blocked)
            with pytest.raises(LOCK_ERRORS):
                future.result(timeout=3)
        assert worker_database._pool.snapshot.borrowed == 0
        with worker_database.transaction() as transaction:
            assert transaction.fetch_one("SELECT 1 AS alive")["alive"] == 1


@pytest.mark.postgresql_lock_catalog
def test_slow_statement_releases_previously_acquired_owner_lock(database):
    owner = uid()
    with independent_database(schema(database)) as worker_database:

        def slow():
            with worker_database.transaction() as transaction:
                SessionRepository.bound_request_execution_waits(transaction)
                transaction.fetch_one(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,79))", (owner,)
                )
                transaction.fetch_one("SELECT pg_sleep(10)")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            pytest.raises(psycopg2.errors.QueryCanceled),
        ):
            executor.submit(slow).result(timeout=3)
        assert worker_database._pool.snapshot.borrowed == 0
        with database.transaction() as transaction:
            assert transaction.fetch_one(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,79)) AS acquired", (owner,)
            )["acquired"]
