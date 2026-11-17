"""Real-PostgreSQL tests for tool-policy fencing in astralplane.repositories:
reverse-order writers never deadlock against the policy fence, and stale snapshots or
malformed preferences are refused.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError

import psycopg2
import pytest
from psycopg2.extensions import make_dsn

from astralplane.contracts import IsolationLevel
from astralplane.database.pool import ConnectionPool
from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import LockContentionError, is_lock_contention
from astralplane.repositories import RepositoryConflictError, RepositoryValidationError
from tests.integration.test_catalog_caller_rollback import (
    _DedicatedDriverPool,
)
from tests.integration.test_catalog_caller_rollback import (
    catalog_database as catalog_database,
)

OWNER = "fixed-policy-owner"
AGENT = "web-research-1"
TABLES = (
    "agent_ownership",
    "agent_scopes",
    "agent_trust",
    "draft_agents",
    "tool_overrides",
    "user_agent",
    "user_preferences",
)


@pytest.fixture(autouse=True)
def clean_facts(catalog_database):
    with catalog_database.database.transaction() as tx:
        for table in TABLES:
            tx.execute(f"DELETE FROM {table}")
        tx.execute("DELETE FROM user_llm_config")
        catalog_database.catalog.tool_policy_state.set_scopes(
            tx,
            owner_id=OWNER,
            agent_id=AGENT,
            scopes={"tools:read": True},
            updated_at=1,
        )
        catalog_database.catalog.encrypted_llm_config.upsert_user(
            tx,
            owner_id=OWNER,
            provider="openai",
            base_url="https://example.test",
            model="synthetic",
            api_key_ciphertext=None,
        )


@contextmanager
def independent(fixture):
    if os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"].startswith("sqlite:"):
        from astralplane.database.sqlite import create_sqlite_driver_pool

        driver = create_sqlite_driver_pool(os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"])
        pool = ConnectionPool(driver)
        try:
            yield PlaneDatabase(pool)
        finally:
            pool.close()
        return
    connection = psycopg2.connect(
        make_dsn(
            os.environ["ASTRALPLANE_TEST_POSTGRES_DSN"],
            options=f"-csearch_path={fixture.schema},pg_catalog",
        )
    )
    pool = ConnectionPool(_DedicatedDriverPool(connection))
    try:
        yield PlaneDatabase(pool)
    finally:
        connection.close()


def snapshot(fixture, tx, owner=OWNER):
    return fixture.catalog.tool_policy_state.lock_fixed_reader_policy_snapshot(tx, owner_id=owner)


def wait_for_lock(tx, pid):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        row = tx.fetch_one(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
            (pid,),
        )
        if row and row["wait_event_type"] == "Lock":
            return
        time.sleep(0.005)
    raise AssertionError("owned worker did not reach its lock")


def test_exact_facts_are_detached_and_owner_scoped(catalog_database):
    repo = catalog_database.catalog.tool_policy_state
    with catalog_database.database.transaction() as tx:
        repo.set_scopes(
            tx, owner_id="other", agent_id=AGENT, scopes={"tools:read": False}, updated_at=2
        )
        repo.set_tool_override(
            tx,
            owner_id=OWNER,
            agent_id=AGENT,
            tool_name="fetch_page",
            permission_kind="tools:read",
            enabled=False,
            updated_at=3,
        )
        repo.set_tool_override(
            tx,
            owner_id=OWNER,
            agent_id=AGENT,
            tool_name="ignored",
            permission_kind="tools:read",
            enabled=True,
            updated_at=3,
        )
        repo.set_agent_disabled(tx, owner_id=OWNER, agent_id=AGENT, disabled=True, updated_at=4)
        tx.execute("INSERT INTO agent_trust(agent_id,is_safe) VALUES (%s,TRUE)", (AGENT,))
        tx.execute(
            "INSERT INTO agent_ownership(agent_id,owner_email,is_public) "
            "VALUES (%s,'synthetic@example.test',FALSE)",
            (AGENT,),
        )
        tx.execute(
            "INSERT INTO user_agent(agent_id,owner_user_id,display_name,deleted_at) "
            "VALUES (%s,%s,'Synthetic',1)",
            (AGENT, OWNER),
        )
        for identifier, created, status in [
            ("a", 1, "live"),
            ("b", 2, "pending"),
            ("c", 2, "live"),
        ]:
            tx.execute(
                "INSERT INTO draft_agents(id,user_id,agent_name,agent_slug,description,"
                "created_at,status) VALUES (%s,%s,'Synthetic','web_research','',%s,%s)",
                (identifier, OWNER, created, status),
            )
    with catalog_database.database.transaction() as tx:
        value = snapshot(catalog_database, tx)
        other = snapshot(catalog_database, tx, "other")
    assert value.scopes[0].enabled and not other.scopes[0].enabled
    assert len(value.overrides) == 1 and not value.overrides[0].enabled
    assert value.disabled and value.is_safe and value.is_public is False
    assert value.user_agent_owner == OWNER and value.user_agent_deleted
    assert value.draft_status == "pending"
    assert not other.disabled and other.overrides == ()
    with pytest.raises(FrozenInstanceError):
        value.disabled = False


def test_absent_facts_do_not_create_rows(catalog_database):
    with catalog_database.database.transaction() as tx:
        value = snapshot(catalog_database, tx, "absent")
        assert value.scopes == value.overrides == ()
        assert not value.disabled and not value.is_safe and not value.user_agent_deleted
        assert value.is_public is value.user_agent_owner is value.draft_status is None
        assert tx.fetch_one("SELECT count(*) AS n FROM user_preferences")["n"] == 0


@pytest.mark.parametrize("table", TABLES)
def test_every_ordinary_writer_conflicts_immediately(catalog_database, table):
    with independent(catalog_database) as writer, writer.transaction() as block:
        block.execute(f"DELETE FROM {table} WHERE FALSE")
        started = time.monotonic()
        with (
            pytest.raises(
                LockContentionError, match=r"^fixed reader policy unavailable$"
            ) as caught,
            catalog_database.database.transaction() as tx,
        ):
            snapshot(catalog_database, tx)
        assert is_lock_contention(caught.value)
        assert caught.value.__suppress_context__
        assert time.monotonic() - started < 1
        assert block.fetch_one("SELECT 1 AS n")["n"] == 1
    with catalog_database.database.transaction() as tx:
        assert snapshot(catalog_database, tx).scopes[0].enabled


@pytest.mark.parametrize("kind", ["new_override", "same_owner", "other_owner", "global", "prefs"])
@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.postgresql_lock_catalog
def test_fence_blocks_phantom_and_existing_writers_until_transaction_ends(
    catalog_database,
    kind,
    rollback,
):
    entered = threading.Event()
    pid = []
    repo = catalog_database.catalog.tool_policy_state

    def write():
        with independent(catalog_database) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout = '4s'")
            pid.append(tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
            entered.set()
            if kind == "new_override":
                repo.set_tool_override(
                    tx,
                    owner_id=OWNER,
                    agent_id=AGENT,
                    tool_name="fetch_page",
                    permission_kind=None,
                    enabled=False,
                    updated_at=2,
                )
            elif kind in {"same_owner", "other_owner"}:
                repo.set_scopes(
                    tx,
                    owner_id=OWNER if kind == "same_owner" else "other",
                    agent_id=AGENT,
                    scopes={"tools:read": False},
                    updated_at=2,
                )
            elif kind == "global":
                repo.prune_agent_overrides(tx, agent_id=AGENT, live_tool_names=[])
            else:
                tx.execute(
                    "INSERT INTO user_preferences(user_id,preferences) VALUES (%s,%s)",
                    (OWNER, '{"disabled_agents":["web-research-1"]}'),
                )
        return True

    with ThreadPoolExecutor(max_workers=1) as workers:
        try:
            with catalog_database.database.transaction() as tx:
                assert snapshot(catalog_database, tx).scopes[0].enabled
                future = workers.submit(write)
                assert entered.wait(2)
                wait_for_lock(tx, pid[0])
                assert not future.done()
                if rollback:
                    raise RuntimeError("owned rollback")
        except RuntimeError as error:
            assert rollback and str(error) == "owned rollback"
        assert future.result(timeout=3) is True


@pytest.mark.postgresql_lock_catalog
def test_reverse_policy_then_config_writer_cannot_deadlock(catalog_database):
    ready, proceed = threading.Event(), threading.Event()
    pid = []
    repo = catalog_database.catalog.tool_policy_state

    def write():
        with independent(catalog_database) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout = '4s'")
            repo.set_scopes(
                tx, owner_id=OWNER, agent_id=AGENT, scopes={"tools:read": False}, updated_at=2
            )
            pid.append(tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
            ready.set()
            assert proceed.wait(3)
            catalog_database.catalog.encrypted_llm_config.get_user_for_update(tx, owner_id=OWNER)
        return True

    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(write)
        assert ready.wait(2)
        with (
            pytest.raises(RepositoryConflictError, match="fixed reader policy unavailable"),
            catalog_database.database.transaction() as tx,
        ):
            catalog_database.catalog.encrypted_llm_config.get_user_for_update(tx, owner_id=OWNER)
            proceed.set()
            wait_for_lock(tx, pid[0])
            snapshot(catalog_database, tx)
        assert future.result(timeout=3) is True
    with catalog_database.database.transaction() as tx:
        assert not snapshot(catalog_database, tx).scopes[0].enabled


@pytest.mark.postgresql_lock_catalog
def test_config_then_revoke_is_observed_after_config_wait(catalog_database):
    entered = threading.Event()
    pid = []
    repo = catalog_database.catalog.tool_policy_state

    def read():
        with independent(catalog_database) as db, db.transaction() as tx:
            tx.execute("SET LOCAL statement_timeout = '4s'")
            assert repo.list_scopes(tx, owner_id=OWNER, agent_id=AGENT)[0].enabled
            pid.append(tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"])
            entered.set()
            catalog_database.catalog.encrypted_llm_config.get_user_for_update(tx, owner_id=OWNER)
            return snapshot(catalog_database, tx).scopes[0].enabled

    with ThreadPoolExecutor(max_workers=1) as workers:
        with catalog_database.database.transaction() as writer:
            catalog_database.catalog.encrypted_llm_config.get_user_for_update(
                writer, owner_id=OWNER
            )
            future = workers.submit(read)
            assert entered.wait(2)
            wait_for_lock(writer, pid[0])
            repo.set_scopes(
                writer, owner_id=OWNER, agent_id=AGENT, scopes={"tools:read": False}, updated_at=2
            )
        assert future.result(timeout=3) is False


@pytest.mark.parametrize("isolation", [IsolationLevel.REPEATABLE_READ, IsolationLevel.SERIALIZABLE])
def test_stale_snapshot_isolation_refuses(catalog_database, isolation):
    with (
        pytest.raises(RepositoryConflictError, match="fixed reader policy unavailable"),
        catalog_database.database.transaction(isolation=isolation) as tx,
    ):
        tx.fetch_one("SELECT 1")
        snapshot(catalog_database, tx)


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"disabled_agents":1}'])
def test_malformed_preferences_have_no_data_in_refusal(catalog_database, raw):
    with catalog_database.database.transaction() as tx:
        tx.execute("INSERT INTO user_preferences(user_id,preferences) VALUES (%s,%s)", (OWNER, raw))
    with (
        pytest.raises(RepositoryConflictError) as caught,
        catalog_database.database.transaction() as tx,
    ):
        snapshot(catalog_database, tx)
    assert str(caught.value) == "fixed reader policy unavailable"
    assert caught.value.__suppress_context__
    assert not is_lock_contention(caught.value)


def test_invalid_owner_does_not_touch_transaction(catalog_database):
    with pytest.raises(RepositoryValidationError):
        snapshot(catalog_database, object(), "")
