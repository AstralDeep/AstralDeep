"""Proves mesh-id owners (mesh. plus 64 hex) store, read and isolate like any owner id through
the mesh, notification, chat queue, atlas and harness cleanup repositories with no schema change.
Runs on SQLite or PostgreSQL through the shared migrated_clone fixture and its DSN variable.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from astralplane import SCHEMA_REVISION, create_chat_queue_repository, create_event_log_repository
from astralplane.api import create_repository_catalog
from astralplane.errors import SQLContractError
from astralplane.purge import PostgresPurgeStore, PurgeTargetScope
from astralplane.repositories.atlas_pages import AtlasPageCreateCommand
from astralplane.repositories.harness_cleanup import (
    HarnessCleanupProfile,
    HarnessCleanupRepository,
)
from astralplane.repositories.history import ConversationRepository
from astralplane.repositories.notifications import NotificationRepository
from tests.repositories._mesh_support import FEED, append_chain, genesis

MESH_OWNER = "mesh." + hashlib.sha256(b"synthetic-mesh-owner").hexdigest()
OTHER_MESH_OWNER = "mesh." + hashlib.sha256(b"synthetic-other-owner").hexdigest()
RAW_KERNEL_ID = "sha256:" + hashlib.sha256(b"synthetic-mesh-owner").hexdigest()
NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
LEGACY_OWNER = "oidc.0123456789abcdef.synthetic-subject"
CHAT = "owner-id-chat"
LEGACY_CHAT = "owner-id-legacy-chat"
ITEM = "10000000-0000-4000-8000-000000000001"
LEGACY_ITEM = "10000000-0000-4000-8000-000000000002"
PAGE = "10000000-0000-4000-8000-000000000003"


def conversation(tx, owner, chat=CHAT):
    ConversationRepository().create(
        tx,
        owner_id=owner,
        conversation_id=chat,
        title="Synthetic owner id chat",
        agent_id=None,
        created_at=10,
    )


def inbox_entry(tx, owner):
    body = b"synthetic-opaque-notification"
    return NotificationRepository().create_entry(
        tx,
        owner_id=owner,
        event_kind="completion",
        source_id="assignment-1",
        deduplication_key="completion:assignment-1:1",
        content_format="astral.notification/v1",
        content_ciphertext=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        created_at=10,
        retention_expires_at=100,
    )


def page_command(owner):
    return AtlasPageCreateCommand(
        owner_id=owner,
        command_id=str(uuid.uuid4()),
        page_id=PAGE,
        project_id=None,
        slug="owner-id-page",
        sources=("https://example.invalid/source",),
        content=b"synthetic-page-ciphertext",
        created_at=10,
    )


def test_schema_revision_is_unchanged():
    assert SCHEMA_REVISION == "089.022"


def test_mesh_owner_mesh_log_round_trips_and_isolates(migrated_clone):
    log = create_event_log_repository()
    with migrated_clone.database.transaction() as tx:
        mesh = genesis(tx, owner=MESH_OWNER)
        legacy_mesh = genesis(tx, owner=LEGACY_OWNER)
        events = append_chain(tx, mesh, 2, owner=MESH_OWNER)
        legacy_events = append_chain(tx, legacy_mesh, 1, owner=LEGACY_OWNER)
        head = log.get_feed(tx, owner_id=MESH_OWNER, mesh_id=mesh, feed_id=FEED)
        assert head.owner_id == MESH_OWNER and head.head_seq == 2
        assert head.head_hash == events[1].event.event_id
        assert log.get_feed(tx, owner_id=OTHER_MESH_OWNER, mesh_id=mesh, feed_id=FEED) is None
        legacy_head = log.get_feed(tx, owner_id=LEGACY_OWNER, mesh_id=legacy_mesh, feed_id=FEED)
        assert legacy_head.head_seq == 1
        assert legacy_head.head_hash == legacy_events[0].event.event_id


def test_mesh_owner_notification_round_trips_beside_legacy_owner(migrated_clone):
    repo = NotificationRepository()
    with migrated_clone.database.transaction() as tx:
        legacy = inbox_entry(tx, LEGACY_OWNER)
        created = inbox_entry(tx, MESH_OWNER)
        assert created.owner_id == MESH_OWNER
        assert repo.get_entry(tx, owner_id=MESH_OWNER, entry_id=created.entry_id) == created
        assert repo.get_entry(tx, owner_id=OTHER_MESH_OWNER, entry_id=created.entry_id) is None
        assert repo.get_entry(tx, owner_id=LEGACY_OWNER, entry_id=created.entry_id) is None
        assert repo.get_entry(tx, owner_id=LEGACY_OWNER, entry_id=legacy.entry_id) == legacy


def test_mesh_owner_chat_queue_round_trips_beside_legacy_owner(migrated_clone):
    repo = create_chat_queue_repository()
    with migrated_clone.database.transaction() as tx:
        conversation(tx, LEGACY_OWNER, LEGACY_CHAT)
        legacy = repo.enqueue(
            tx,
            owner_id=LEGACY_OWNER,
            chat_id=LEGACY_CHAT,
            text="legacy",
            created_at=20,
            item_id=LEGACY_ITEM,
        )
        conversation(tx, MESH_OWNER)
        queued = repo.enqueue(
            tx, owner_id=MESH_OWNER, chat_id=CHAT, text="mesh", created_at=20, item_id=ITEM
        )
        assert queued.owner_id == MESH_OWNER
        assert repo.list(tx, owner_id=MESH_OWNER, chat_id=CHAT) == (queued,)
        assert repo.get(tx, owner_id=OTHER_MESH_OWNER, chat_id=CHAT, item_id=ITEM) is None
        assert repo.list(tx, owner_id=LEGACY_OWNER, chat_id=LEGACY_CHAT) == (legacy,)


def test_mesh_owner_atlas_page_round_trips_beside_legacy_owner(migrated_clone):
    repo = create_repository_catalog().atlas_pages
    with migrated_clone.database.transaction() as tx:
        legacy = repo.create_page(tx, command=page_command(LEGACY_OWNER)).page
        mesh_command = page_command(MESH_OWNER)
        created = repo.create_page(tx, command=mesh_command).page
        assert created.owner_id == MESH_OWNER and created.content == mesh_command.content
        assert repo.get_page(tx, owner_id=MESH_OWNER, page_id=PAGE) == created
        assert repo.get_page(tx, owner_id=OTHER_MESH_OWNER, page_id=PAGE) is None
        assert repo.get_page(tx, owner_id=LEGACY_OWNER, page_id=PAGE) == legacy


def test_mesh_owner_purge_tombstone_round_trips_and_spares_legacy_owner(migrated_clone):
    store = PostgresPurgeStore()
    with migrated_clone.database.transaction() as tx:
        scheduled = store.schedule_owner_namespace(
            tx, owner_id=MESH_OWNER, requested_at=NOW, deleted_at=100
        )
        tombstone = scheduled.tombstone
        assert tombstone.owner_id == MESH_OWNER
        assert tombstone.target_scope is PurgeTargetScope.OWNER_NAMESPACE
        assert store.load(tx, owner_id=MESH_OWNER, tombstone_id=tombstone.tombstone_id) == tombstone
        assert (
            store.load(tx, owner_id=OTHER_MESH_OWNER, tombstone_id=tombstone.tombstone_id) is None
        )
        assert store.load(tx, owner_id=LEGACY_OWNER, tombstone_id=tombstone.tombstone_id) is None
        legacy = store.schedule_owner_namespace(
            tx, owner_id=LEGACY_OWNER, requested_at=NOW, deleted_at=100
        ).tombstone
        assert legacy.owner_id == LEGACY_OWNER
        assert legacy.tombstone_id != tombstone.tombstone_id
        assert store.load(tx, owner_id=MESH_OWNER, tombstone_id=tombstone.tombstone_id) == tombstone


def test_raw_kernel_id_owner_is_refused_and_writes_nothing(migrated_clone):
    with migrated_clone.database.transaction() as tx:
        with pytest.raises(SQLContractError):
            PostgresPurgeStore().schedule_owner_namespace(
                tx, owner_id=RAW_KERNEL_ID, requested_at=NOW, deleted_at=100
            )
        rows = tx.fetch_all("SELECT owner_id FROM astralplane_purge_tombstone", ())
        assert [row["owner_id"] for row in rows] == []


def test_harness_cleanup_without_issuer_hash_spares_mesh_and_legacy_owners(migrated_clone):
    harness_owner = "__verif__run_1_synthetic"
    with migrated_clone.database.transaction() as tx:
        conversation(tx, MESH_OWNER)
        conversation(tx, harness_owner, "owner-id-harness-chat")
        legacy_chat = "legacy-chat"
        ConversationRepository().create(
            tx,
            owner_id=LEGACY_OWNER,
            conversation_id=legacy_chat,
            title="Synthetic legacy chat",
            agent_id=None,
            created_at=10,
        )
        report = HarnessCleanupRepository().purge_run(
            tx, profile=HarnessCleanupProfile.VERIFICATION, run_id="run_1"
        )
        assert report.namespace_prefix == "__verif__run_1_"
        remaining = {row["user_id"] for row in tx.fetch_all("SELECT user_id FROM chats", ())}
        assert remaining == {MESH_OWNER, LEGACY_OWNER}
