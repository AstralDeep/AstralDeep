"""Real-PostgreSQL tests for astralplane.repositories.secrets: a selected config row
can't change until the selecting transaction ends, and an unrelated owner may still
update while the selected row stays locked.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, uid

from astralplane.repositories.secrets import EncryptedLLMConfigRepository


def put(transaction, owner, *, model="original"):
    return EncryptedLLMConfigRepository().upsert_user(
        transaction,
        owner_id=owner,
        provider="custom",
        base_url="https://models.invalid/v1",
        model=model,
        api_key_ciphertext="opaque-test-ciphertext",
    )


def seed(database):
    owner = uid()
    with database.transaction() as transaction:
        original = put(transaction, owner)
        schema = transaction.fetch_one("SELECT current_schema() AS name")["name"]
    return owner, schema, original


def wait_for_lock(transaction, waiting_pid, blocking_pid):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if (
            blocking_pid
            in transaction.fetch_one("SELECT pg_blocking_pids(%s) AS pids", (waiting_pid,))["pids"]
        ):
            return
        time.sleep(0.01)
    pytest.fail("configuration writer did not wait on the exact selecting transaction")


@pytest.mark.parametrize("operation", ["update", "delete", "delete_recreate"])
@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.postgresql_lock_catalog
def test_selected_row_cannot_change_until_selection_commit_or_rollback(
    database, operation, rollback
):
    owner, schema, original = seed(database)
    repository = EncryptedLLMConfigRepository()
    entered, identities = Event(), {}

    def writer():
        with independent_database(schema) as db, db.transaction() as transaction:
            transaction.execute("SET LOCAL statement_timeout='5s'")
            identities["writer"] = transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            if operation in {"delete", "delete_recreate"}:
                repository.delete_user(transaction, owner_id=owner)
            if operation in {"update", "delete_recreate"}:
                return put(transaction, owner, model="replacement")
            return None

    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            with database.transaction() as transaction:
                selected = repository.get_user_for_update(transaction, owner_id=owner)
                assert selected == original
                blocking = transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
                future = worker.submit(writer)
                assert entered.wait(3)
                wait_for_lock(transaction, identities["writer"], blocking)
                assert not future.done()
                assert repository.get_user(transaction, owner_id=owner) == selected
                if rollback:
                    raise RuntimeError("intentional selection rollback")
        except RuntimeError as error:
            assert rollback and str(error) == "intentional selection rollback"
        changed = future.result(timeout=5)
    with database.transaction() as transaction:
        actual = repository.get_user(transaction, owner_id=owner)
        assert actual == changed
        if operation != "delete":
            assert actual.model == "replacement" and actual != original


def test_wrong_or_missing_owner_never_adopts_other_config_and_has_no_gap_lock(database):
    owner, schema, original = seed(database)
    repository = EncryptedLLMConfigRepository()
    missing = uid()

    def insert_missing():
        with independent_database(schema) as db, db.transaction() as transaction:
            transaction.execute("SET LOCAL lock_timeout='500ms'")
            return put(transaction, missing)

    with ThreadPoolExecutor(max_workers=1) as worker, database.transaction() as transaction:
        assert repository.get_user_for_update(transaction, owner_id=missing) is None
        inserted = worker.submit(insert_missing).result(timeout=3)
        assert inserted.owner_id == missing
        assert repository.get_user_for_update(transaction, owner_id=owner) == original
        assert inserted != original


def test_other_owner_can_update_while_selected_owner_row_is_locked(database):
    owner, schema, original = seed(database)
    other = uid()
    repository = EncryptedLLMConfigRepository()
    with database.transaction() as transaction:
        put(transaction, other)

    def update_other():
        with independent_database(schema) as db, db.transaction() as transaction:
            transaction.execute("SET LOCAL lock_timeout='500ms'")
            return put(transaction, other, model="other-new")

    with ThreadPoolExecutor(max_workers=1) as worker, database.transaction() as transaction:
        assert repository.get_user_for_update(transaction, owner_id=owner) == original
        if transaction.dialect.is_sqlite:
            from astralplane.database.sqlite import SQLiteLockTimeoutError

            with pytest.raises(SQLiteLockTimeoutError):
                worker.submit(update_other).result(timeout=3)
        else:
            assert worker.submit(update_other).result(timeout=3).model == "other-new"
        assert repository.get_user(transaction, owner_id=owner) == original
    assert update_other().model == "other-new"
    with database.transaction() as transaction:
        assert repository.get_user(transaction, owner_id=owner) == original


@pytest.mark.postgresql_lock_catalog
def test_selector_waits_for_preexisting_update_and_returns_actual_changed_row(database):
    owner, schema, original = seed(database)
    repository = EncryptedLLMConfigRepository()
    entered, identities = Event(), {}

    def select():
        with independent_database(schema) as db, db.transaction() as transaction:
            transaction.execute("SET LOCAL statement_timeout='5s'")
            identities["selector"] = transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            return repository.get_user_for_update(transaction, owner_id=owner)

    with ThreadPoolExecutor(max_workers=1) as worker:
        with database.transaction() as transaction:
            changed = put(transaction, owner, model="changed-before-lock")
            blocking = transaction.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            future = worker.submit(select)
            assert entered.wait(3)
            wait_for_lock(transaction, identities["selector"], blocking)
        selected = future.result(timeout=5)
    assert selected == changed and selected != original
    assert "opaque-test-ciphertext" not in repr(selected)
