"""Exercise memory source locks through independent PostgreSQL transactions.
The personalization getter fences source writers until the caller commits or rolls back.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from test_preferences import memory_record

from astralplane.repositories.preferences import PersonalizationRepository
from tests.integration.test_lifecycle_acceptance import (
    concurrent_database,
    wait_for_blocked_statement,
)


def seed(database):
    record = memory_record(memory_id=str(uuid4()), owner_id="memory-lock-owner")
    with database.transaction() as tx:
        return PersonalizationRepository().create_memory(tx, record)


def mutate(repository, tx, source, action):
    identity = {"owner_id": source.owner_id, "memory_id": source.memory_id}
    if action == "update":
        return repository.update_memory_value(
            tx, **identity, value="Changed source", signature=None,
            updated_at=source.updated_at + 1, expected_updated_at=source.updated_at,
        )
    if action == "delete":
        return repository.delete_memory(tx, **identity, expected_updated_at=source.updated_at)
    if action == "supersede":
        return repository.supersede_memory(tx, **identity, superseded_at=source.updated_at + 1)
    return repository.set_validity(
        tx, **identity, valid_from=None, valid_to=source.created_at, ingested_at=None,
        updated_at=source.updated_at + 1, expected_updated_at=source.updated_at,
    )


@pytest.mark.parametrize("action", ("update", "delete", "supersede", "expire"))
@pytest.mark.parametrize("rollback", (False, True))
@pytest.mark.postgresql_lock_catalog
def test_memory_lock_blocks_writers_until_caller_finishes(migrated_clone, action, rollback):
    source = seed(migrated_clone.database)
    repository = PersonalizationRepository()
    entered, state = Event(), {}

    def writer(database):
        with database.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            return mutate(repository, tx, source, action)

    with (
        concurrent_database(migrated_clone) as database,
        concurrent_database(migrated_clone) as observer,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        try:
            with migrated_clone.database.transaction() as holder:
                blocker = holder.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
                assert repository.get_memory(
                    holder, owner_id=source.owner_id, memory_id=source.memory_id, for_update=True
                ) == source
                pending = workers.submit(writer, database)
                assert entered.wait(timeout=30)
                assert state["pid"] != blocker
                wait_for_blocked_statement(observer, state["pid"], blocker)
                assert not pending.done()
                if rollback:
                    raise RuntimeError("intentional caller rollback")
        except RuntimeError as error:
            assert rollback and str(error) == "intentional caller rollback"
        assert pending.result(timeout=30)
    with migrated_clone.database.transaction() as tx:
        current = repository.get_memory(tx, owner_id=source.owner_id, memory_id=source.memory_id)
        if action == "delete":
            assert current is None
        else:
            assert current.updated_at == source.updated_at + 1
            if action == "update":
                assert current.value == "Changed source"
            elif action == "supersede":
                assert current.superseded_at is not None
            else:
                assert current.valid_to == source.created_at


@pytest.mark.parametrize("action", ("update", "delete", "supersede", "expire"))
@pytest.mark.postgresql_lock_catalog
def test_locked_read_waits_for_writer_and_returns_committed_current_state(migrated_clone, action):
    source = seed(migrated_clone.database)
    repository = PersonalizationRepository()
    entered, state = Event(), {}

    def reader(database):
        with database.transaction() as tx:
            state["pid"] = tx.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            entered.set()
            return repository.get_memory(
                tx, owner_id=source.owner_id, memory_id=source.memory_id, for_update=True
            )

    with (
        concurrent_database(migrated_clone) as database,
        concurrent_database(migrated_clone) as observer,
        ThreadPoolExecutor(max_workers=1) as workers,
    ):
        with migrated_clone.database.transaction() as holder:
            blocker = holder.fetch_one("SELECT pg_backend_pid() AS pid")["pid"]
            mutate(repository, holder, source, action)
            pending = workers.submit(reader, database)
            assert entered.wait(timeout=30)
            wait_for_blocked_statement(observer, state["pid"], blocker)
            assert not pending.done()
        current = pending.result(timeout=30)
    if action == "delete":
        assert current is None
    else:
        assert current != source
        assert current.updated_at == source.updated_at + 1


@pytest.mark.parametrize("options", ({}, {"for_update": False}))
def test_ordinary_memory_read_preserves_nonlocking_snapshot(migrated_clone, options):
    source = seed(migrated_clone.database)
    repository = PersonalizationRepository()

    def reader(database):
        with database.transaction() as tx:
            return repository.get_memory(
                tx, owner_id=source.owner_id, memory_id=source.memory_id, **options
            )

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=1) as workers,
        migrated_clone.database.transaction() as holder,
    ):
        mutate(repository, holder, source, "update")
        assert workers.submit(reader, database).result(timeout=30) == source


def test_missing_and_foreign_memory_reads_never_lock_foreign_source(migrated_clone):
    source = seed(migrated_clone.database)
    repository = PersonalizationRepository()

    def writer(database):
        with database.transaction() as tx:
            return mutate(repository, tx, source, "update")

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=1) as workers,
        migrated_clone.database.transaction() as holder,
    ):
        assert repository.get_memory(
            holder, owner_id="foreign", memory_id=source.memory_id, for_update=True
        ) is None
        assert repository.get_memory(
            holder, owner_id=source.owner_id, memory_id=str(uuid4()), for_update=True
        ) is None
        assert workers.submit(writer, database).result(timeout=30).value == "Changed source"
