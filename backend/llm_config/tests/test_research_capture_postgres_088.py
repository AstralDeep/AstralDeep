"""Tests for llm_config/user_store.py against real Postgres: cross-process config
updates bypass the stale in-memory cache, owner/system rows never alias, and a
corrupt record is retained rather than silently discarded.
"""

import asyncio
from dataclasses import FrozenInstanceError
from contextlib import ExitStack
import threading
from datetime import timedelta

import pytest

from audit.pii import private_binding_key
from llm_config.research_profile import BASE_URL, MODEL, select_config
from llm_config.user_store import UserConfigCaptureUnavailable, UserLLMConfigStore
from persistent_agents.tests.test_engine_postgres import plane as plane


@pytest.fixture
def stores(plane, fernet_key, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "pg_research")
    monkeypatch.setenv(
        "AUDIT_HMAC_SECRET", "synthetic-pg-binding-" + "0123456789abcdef" * 2
    )
    monkeypatch.delenv("AUDIT_HMAC_SECRET_PG_RESEARCH", raising=False)
    return tuple(UserLLMConfigStore(plane_runtime=plane) for _ in range(2))


def save(store, owner="alice", key="synthetic-no-network-key"):
    return store.set_sync(
        owner, provider="openai", base_url=BASE_URL, model=MODEL, api_key=key
    )


def selected(store, owner="alice"):
    capture = store.capture_user_sync(owner)
    return select_config(capture, store=store, binding_key=private_binding_key())


def test_real_cross_process_update_bypasses_stale_cache(stores):
    first, other = stores
    cached = save(first)
    before = selected(first)
    save(other, key="synthetic-rotated-no-network-key")
    after = selected(first)
    assert first.get_sync("alice") is cached
    assert after.revision != before.revision
    assert not before.matches(after._capture._record)
    assert before._api_key == "synthetic-no-network-key"
    assert after._api_key == "synthetic-rotated-no-network-key"
    assert before._capture._record.created_at == after._capture._record.created_at
    assert before._capture._record.updated_at < after._capture._record.updated_at
    assert before._capture._record.updated_at.utcoffset() == timedelta(0)
    with pytest.raises(FrozenInstanceError):
        before._capture._record.model = "replacement"


def test_real_clear_recreate_same_plaintext_has_new_cipher_and_revision(stores):
    first, other = stores
    save(first)
    before = selected(first)
    other.clear_sync("alice")
    assert first.capture_user_sync("alice") is None
    assert first.get_sync("alice") is not None
    save(other)
    after = selected(first)
    assert before._api_key == after._api_key
    assert before.revision != after.revision
    assert (
        before._capture._record.api_key_ciphertext
        != after._capture._record.api_key_ciphertext
    )
    assert before._capture._record.created_at < after._capture._record.created_at


def test_real_owner_scope_and_system_never_alias(stores):
    first, _ = stores
    save(first, "alice")
    save(first, "bob")
    before = selected(first, "alice")
    other = selected(first, "bob")
    assert before.owner_id == "alice" and other.owner_id == "bob"
    assert not before.matches(other._capture._record)
    assert before.revision != other.revision
    assert first.capture_user_sync("ALICE") is None
    assert first.capture_user_sync("alice ") is None
    assert first.capture_user_sync("__system__") is None


def test_real_corrupt_record_retained_no_discard_or_cache_mutation(stores, plane):
    first, _ = stores
    cached = save(first)
    repository = plane.repositories.encrypted_llm_config
    with plane.transaction() as transaction:
        corrupted = repository.upsert_user(
            transaction,
            owner_id="alice",
            provider="openai",
            base_url=BASE_URL,
            model=MODEL,
            api_key_ciphertext="synthetic-invalid-ciphertext",
        )
    capture = first.capture_user_sync("alice")
    assert capture.matches(corrupted)
    with pytest.raises(UserConfigCaptureUnavailable):
        selected(first)
    with plane.transaction() as transaction:
        assert capture.matches(repository.get_user(transaction, owner_id="alice"))
    assert first.get_sync("alice") is cached


@pytest.mark.asyncio
async def test_real_async_capture_detaches_transaction(stores, plane):
    first, other = stores
    await asyncio.to_thread(save, first)
    capture = await first.capture_user("alice")
    await asyncio.wait_for(
        asyncio.to_thread(save, other, "alice", "synthetic-new-key"), 2
    )
    after = await first.capture_user("alice")
    assert not capture.matches(after._record)
    with plane.transaction() as transaction:
        assert after.matches(
            plane.repositories.encrypted_llm_config.get_user(
                transaction, owner_id="alice"
            )
        )


@pytest.mark.asyncio
async def test_table_blocker_remains_held_when_capture_worker_releases_transaction(
    stores, plane
):
    first, _ = stores
    await asyncio.to_thread(save, first)
    with ExitStack() as holders, plane.transaction() as blocker:
        for _ in range(6):
            held = holders.enter_context(plane.transaction())
            held.fetch_one("SELECT 1 AS held")
        blocker.execute("LOCK TABLE user_llm_config IN ACCESS EXCLUSIVE MODE")
        blocked = asyncio.create_task(first.capture_user("alice"))
        with pytest.raises(
            UserConfigCaptureUnavailable, match="^user_config_capture_unavailable$"
        ):
            await asyncio.wait_for(blocked, 2)
        assert blocked.done()
        assert blocker.fetch_one("SELECT 1 AS held")["held"] == 1
        with plane.transaction() as probe:
            assert probe.fetch_one("SELECT 1 AS released")["released"] == 1
    assert first.capture_user_sync("alice") is not None


@pytest.mark.asyncio
async def test_statement_timeout_finishes_worker_and_returns_pool_slot(
    stores, plane, monkeypatch
):
    first, _ = stores
    await asyncio.to_thread(save, first)
    repository = first._repository.repository
    original = repository.get_user

    def delayed(transaction, *, owner_id):
        transaction.fetch_one("SELECT pg_sleep(3)")
        return original(transaction, owner_id=owner_id)

    monkeypatch.setattr(repository, "get_user", delayed)
    task = asyncio.create_task(first.capture_user("alice"))
    with pytest.raises(UserConfigCaptureUnavailable):
        await asyncio.wait_for(task, 2)
    assert task.done()
    monkeypatch.setattr(repository, "get_user", original)
    assert await first.capture_user("alice") is not None


@pytest.mark.asyncio
async def test_short_outer_cancellation_does_not_leave_worker_waiting_for_slow_query(
    stores, monkeypatch
):
    first, _ = stores
    await asyncio.to_thread(save, first)
    repository = first._repository.repository
    original = repository.get_user
    started, finished = threading.Event(), threading.Event()

    def delayed(transaction, *, owner_id):
        started.set()
        transaction.fetch_one("SELECT pg_sleep(3)")
        return original(transaction, owner_id=owner_id)

    capture_sync = first.capture_user_sync

    def observed_capture(owner_id):
        try:
            return capture_sync(owner_id)
        finally:
            finished.set()

    monkeypatch.setattr(repository, "get_user", delayed)
    monkeypatch.setattr(first, "capture_user_sync", observed_capture)
    task = asyncio.create_task(first.capture_user("alice"))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 2)
    monkeypatch.setattr(repository, "get_user", original)
    assert await first.capture_user("alice") is not None
