"""Real-PostgreSQL tests for owner-scoped notification preferences, opaque inbox
records, deduplication, acknowledgment, retention, tombstones, and delivery identities.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from test_assignments_postgres import database as database

from astralplane.api import create_repository_catalog
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.notifications import NotificationRepository


@pytest.fixture()
def tx(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM notification_delivery_identity")
        transaction.execute("DELETE FROM notification_inbox_entry")
        transaction.execute("DELETE FROM notification_preference")
        yield transaction


@pytest.fixture()
def repo():
    return NotificationRepository()


def uid():
    return str(uuid.uuid4())


def content(value=b"synthetic-opaque-notification"):
    return value, hashlib.sha256(value).hexdigest()


def preference_args(**changes):
    values = dict(
        owner_id="owner-1",
        expected_revision=0,
        in_app_completion=True,
        in_app_failure=True,
        in_app_required_action=True,
        inbox_completion=True,
        inbox_failure=True,
        inbox_required_action=True,
        push_completion=False,
        push_failure=True,
        push_required_action=True,
        updated_at=100,
    )
    values.update(changes)
    return values


def entry_args(**changes):
    body, digest = content()
    values = dict(
        owner_id="owner-1",
        event_kind="completion",
        source_id="assignment-1",
        deduplication_key="completion:assignment-1:1",
        content_format="astral.notification/v1",
        content_ciphertext=body,
        content_sha256=digest,
        created_at=10,
        retention_expires_at=100,
    )
    values.update(changes)
    return values


def test_preferences_roundtrip_update_and_stale_fence(repo, tx):
    created = repo.put_preferences(tx, **preference_args())
    assert created.revision == 1
    assert repo.get_preferences(tx, owner_id="owner-1") == created

    updated = repo.put_preferences(
        tx,
        **preference_args(
            expected_revision=1,
            push_completion=True,
            updated_at=101,
        ),
    )
    assert updated.revision == 2
    assert updated.push_completion is True
    assert repo.put_preferences(
        tx,
        **preference_args(
            expected_revision=2,
            push_completion=True,
            updated_at=101,
        ),
    ) == updated

    with pytest.raises(RepositoryConflictError, match="stale"):
        repo.put_preferences(
            tx,
            **preference_args(expected_revision=1, updated_at=102),
        )
    with pytest.raises(RepositoryConflictError, match="regressed"):
        repo.put_preferences(
            tx,
            **preference_args(expected_revision=2, updated_at=99),
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.put_preferences(
            tx,
            **preference_args(owner_id="missing", expected_revision=1),
        )


def test_preferences_reject_malformed_and_conflicting_creation(repo, tx):
    with pytest.raises(RepositoryValidationError):
        repo.put_preferences(tx, **preference_args(in_app_completion=1))
    with pytest.raises(RepositoryValidationError):
        repo.put_preferences(tx, **preference_args(expected_revision=-1))
    with pytest.raises(RepositoryValidationError):
        repo.put_preferences(tx, **preference_args(updated_at=-1))
    with pytest.raises(RepositoryValidationError):
        repo.put_preferences(tx, **preference_args(owner_id=""))

    repo.put_preferences(tx, **preference_args())
    assert repo.put_preferences(tx, **preference_args()) == repo.get_preferences(
        tx, owner_id="owner-1"
    )
    with pytest.raises(RepositoryConflictError, match="changed immutable values"):
        repo.put_preferences(tx, **preference_args(updated_at=101))
    with pytest.raises(RepositoryConflictError, match="changed immutable values"):
        repo.put_preferences(tx, **preference_args(push_completion=True))


def test_entry_create_deduplicate_and_owner_reads(repo, tx):
    first = repo.create_entry(
        tx,
        **entry_args(entry_id=str(uuid.UUID(int=1))),
    )
    assert first.owner_id == "owner-1"
    assert repo.get_entry(tx, owner_id="owner-1", entry_id=first.entry_id) == first
    assert repo.get_entry(tx, owner_id="intruder", entry_id=first.entry_id) is None
    assert (
        repo.get_entry_by_deduplication_key(
            tx,
            owner_id="owner-1",
            deduplication_key=first.deduplication_key,
        )
        == first
    )

    duplicate = repo.create_entry(tx, **entry_args())
    assert duplicate.entry_id == first.entry_id
    assert duplicate.content_ciphertext == b"synthetic-opaque-notification"

    different_body, different_digest = content(b"different")
    with pytest.raises(RepositoryConflictError, match="immutable content"):
        repo.create_entry(
            tx,
            **entry_args(
                content_ciphertext=different_body,
                content_sha256=different_digest,
            ),
        )
    with pytest.raises(RepositoryConflictError, match="immutable content"):
        repo.create_entry(tx, **entry_args(source_id="assignment-2"))

    second = repo.create_entry(
        tx,
        **entry_args(
            entry_id=str(uuid.UUID(int=2)),
            deduplication_key="failure:assignment-1:1",
            event_kind="failure",
            created_at=20,
            retention_expires_at=110,
        ),
    )
    third = repo.create_entry(
        tx,
        **entry_args(
            entry_id=str(uuid.UUID(int=3)),
            deduplication_key="required:assignment-1:1",
            event_kind="required_action",
            created_at=20,
            retention_expires_at=110,
        ),
    )
    assert [item.entry_id for item in repo.list_entries(tx, owner_id="owner-1", limit=10)] == [
        third.entry_id,
        second.entry_id,
        first.entry_id,
    ]
    assert repo.list_entries(tx, owner_id="intruder", limit=10) == ()
    assert len(repo.list_entries(tx, owner_id="owner-1", limit=2)) == 2
    with pytest.raises(RepositoryValidationError):
        repo.list_entries(tx, owner_id="owner-1", limit=0)


def test_entry_validation_rejects_malformed_content_and_identity(repo, tx):
    with pytest.raises(RepositoryValidationError):
        repo.create_entry(tx, **entry_args(event_kind="unknown"))
    with pytest.raises(RepositoryValidationError):
        repo.create_entry(tx, **entry_args(content_ciphertext=b""))
    with pytest.raises(RepositoryValidationError):
        repo.create_entry(tx, **entry_args(content_sha256="0" * 64))
    with pytest.raises(RepositoryValidationError):
        repo.create_entry(tx, **entry_args(deduplication_key=""))
    with pytest.raises(RepositoryValidationError):
        repo.create_entry(tx, **entry_args(retention_expires_at=10))
    with pytest.raises(RepositoryValidationError):
        repo.create_entry(tx, **entry_args(entry_id="not-a-uuid"))


def test_acknowledgment_is_owner_fenced_idempotent_and_stale_safe(repo, tx):
    entry = repo.create_entry(tx, **entry_args())
    acknowledged = repo.acknowledge_entry(
        tx,
        owner_id="owner-1",
        entry_id=entry.entry_id,
        expected_revision=0,
        acknowledged_at=30,
    )
    assert acknowledged.acknowledged_at == 30
    assert acknowledged.revision == 1
    assert (
        repo.acknowledge_entry(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            expected_revision=0,
            acknowledged_at=30,
        )
        == acknowledged
    )
    with pytest.raises(RepositoryConflictError, match="already acknowledged"):
        repo.acknowledge_entry(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            expected_revision=0,
            acknowledged_at=31,
        )

    fresh = repo.create_entry(
        tx,
        **entry_args(deduplication_key="second"),
    )
    with pytest.raises(RepositoryConflictError, match="stale"):
        repo.acknowledge_entry(
            tx,
            owner_id="owner-1",
            entry_id=fresh.entry_id,
            expected_revision=4,
            acknowledged_at=30,
        )
    with pytest.raises(RepositoryConflictError, match="precedes"):
        repo.acknowledge_entry(
            tx,
            owner_id="owner-1",
            entry_id=fresh.entry_id,
            expected_revision=0,
            acknowledged_at=9,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.acknowledge_entry(
            tx,
            owner_id="intruder",
            entry_id=fresh.entry_id,
            expected_revision=0,
            acknowledged_at=30,
        )


def test_delete_and_retention_are_owner_fenced_tombstones(repo, tx):
    entry = repo.create_entry(tx, **entry_args())
    deleted = repo.delete_entry(
        tx,
        owner_id="owner-1",
        entry_id=entry.entry_id,
        expected_revision=0,
        deleted_at=40,
    )
    assert deleted.deleted_at == 40
    assert deleted.revision == 1
    assert repo.get_entry(tx, owner_id="owner-1", entry_id=entry.entry_id) is None
    assert (
        repo.get_entry(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            include_deleted=True,
        )
        == deleted
    )
    assert (
        repo.delete_entry(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            expected_revision=0,
            deleted_at=40,
        )
        == deleted
    )
    with pytest.raises(RepositoryConflictError, match="already deleted"):
        repo.delete_entry(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            expected_revision=0,
            deleted_at=41,
        )
    with pytest.raises(RepositoryConflictError, match="deleted"):
        repo.acknowledge_entry(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            expected_revision=1,
            acknowledged_at=42,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.delete_entry(
            tx,
            owner_id="intruder",
            entry_id=entry.entry_id,
            expected_revision=0,
            deleted_at=40,
        )

    fresh = repo.create_entry(tx, **entry_args(deduplication_key="stale"))
    with pytest.raises(RepositoryConflictError, match="stale"):
        repo.delete_entry(
            tx,
            owner_id="owner-1",
            entry_id=fresh.entry_id,
            expected_revision=2,
            deleted_at=40,
        )
    with pytest.raises(RepositoryConflictError, match="precedes"):
        repo.delete_entry(
            tx,
            owner_id="owner-1",
            entry_id=fresh.entry_id,
            expected_revision=0,
            deleted_at=9,
        )


def test_retention_purge_is_bounded_and_owner_scoped(repo, tx):
    first = repo.create_entry(
        tx,
        **entry_args(
            deduplication_key="expires-20",
            retention_expires_at=20,
        ),
    )
    second = repo.create_entry(
        tx,
        **entry_args(
            deduplication_key="expires-30",
            created_at=10,
            retention_expires_at=30,
        ),
    )
    foreign = repo.create_entry(
        tx,
        **entry_args(
            owner_id="owner-2",
            deduplication_key="expires-20",
            retention_expires_at=20,
        ),
    )
    purged = repo.purge_expired_entries(
        tx,
        owner_id="owner-1",
        expired_at=25,
        limit=1,
    )
    assert [record.entry_id for record in purged] == [first.entry_id]
    assert purged[0].deleted_at == 25
    assert repo.purge_expired_entries(
        tx,
        owner_id="owner-1",
        expired_at=25,
        limit=1,
    ) == ()
    assert (
        repo.get_entry(tx, owner_id="owner-2", entry_id=foreign.entry_id)
        is not None
    )
    remaining = repo.purge_expired_entries(
        tx,
        owner_id="owner-1",
        expired_at=35,
        limit=10,
    )
    assert [record.entry_id for record in remaining] == [second.entry_id]
    assert repo.purge_expired_entries(
        tx,
        owner_id="owner-1",
        expired_at=35,
        limit=10,
    ) == ()
    with pytest.raises(RepositoryValidationError):
        repo.purge_expired_entries(
            tx,
            owner_id="owner-1",
            expired_at=35,
            limit=0,
        )


def test_delivery_identities_are_deduplicated_and_cross_owner_denied(repo, tx):
    entry = repo.create_entry(tx, **entry_args())
    delivery_id = str(uuid.UUID(int=77))
    first = repo.register_delivery_identity(
        tx,
        owner_id="owner-1",
        entry_id=entry.entry_id,
        delivery_key="delivery-key-1",
        created_at=50,
        delivery_id=delivery_id,
    )
    assert first.delivery_id == delivery_id
    assert (
        repo.register_delivery_identity(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            delivery_key="delivery-key-1",
            created_at=50,
        )
        == first
    )
    assert (
        repo.get_delivery_identity(
            tx,
            owner_id="owner-1",
            delivery_id=delivery_id,
        )
        == first
    )
    assert (
        repo.get_delivery_identity(
            tx,
            owner_id="intruder",
            delivery_id=delivery_id,
        )
        is None
    )
    assert repo.list_delivery_identities(
        tx,
        owner_id="owner-1",
        limit=10,
    ) == (first,)
    assert repo.list_delivery_identities(
        tx,
        owner_id="owner-1",
        entry_id=entry.entry_id,
        limit=10,
    ) == (first,)

    second_entry = repo.create_entry(
        tx,
        **entry_args(deduplication_key="second"),
    )
    with pytest.raises(RepositoryConflictError, match="immutable semantics"):
        repo.register_delivery_identity(
            tx,
            owner_id="owner-1",
            entry_id=second_entry.entry_id,
            delivery_key="delivery-key-1",
            created_at=50,
        )
    with pytest.raises(RepositoryConflictError, match="immutable semantics"):
        repo.register_delivery_identity(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            delivery_key="delivery-key-1",
            created_at=51,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.register_delivery_identity(
            tx,
            owner_id="intruder",
            entry_id=entry.entry_id,
            delivery_key="intruder-key",
            created_at=50,
        )
    repo.delete_entry(
        tx,
        owner_id="owner-1",
        entry_id=entry.entry_id,
        expected_revision=0,
        deleted_at=60,
    )
    with pytest.raises(RepositoryConflictError, match="deleted"):
        repo.register_delivery_identity(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            delivery_key="delivery-key-2",
            created_at=61,
        )
    with pytest.raises(RepositoryValidationError):
        repo.register_delivery_identity(
            tx,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            delivery_key="",
            created_at=61,
        )


def test_restart_preserves_entries_and_rejects_a_stale_ack(database):
    repo = NotificationRepository()
    with database.transaction() as first:
        first.execute("DELETE FROM notification_delivery_identity")
        first.execute("DELETE FROM notification_inbox_entry")
        first.execute("DELETE FROM notification_preference")
        entry = repo.create_entry(first, **entry_args())
        repo.put_preferences(first, **preference_args())
        repo.acknowledge_entry(
            first,
            owner_id="owner-1",
            entry_id=entry.entry_id,
            expected_revision=0,
            acknowledged_at=30,
        )
        saved_entry = entry.entry_id
    with database.transaction() as restarted:
        stored = repo.get_entry(
            restarted,
            owner_id="owner-1",
            entry_id=saved_entry,
        )
        assert stored is not None and stored.acknowledged_at == 30
        assert repo.get_preferences(restarted, owner_id="owner-1").revision == 1
        with pytest.raises(RepositoryConflictError, match="already acknowledged"):
            repo.acknowledge_entry(
                restarted,
                owner_id="owner-1",
                entry_id=saved_entry,
                expected_revision=1,
                acknowledged_at=31,
            )


@pytest.mark.parametrize("kind", ["entry", "delivery"])
@pytest.mark.parametrize(
    ("owner", "key"),
    [("owner-1", "new-key"), ("owner-2", "original-key"), ("owner-2", "new-key")],
)
def test_uuid_collision_is_typed_private_and_keeps_caller_transaction(
    migrated_clone,
    kind,
    owner,
    key,
):
    repo = create_repository_catalog().notifications
    db = migrated_clone.database
    identity = "40000000-0000-4000-8000-000000000001"
    with db.transaction() as tx:
        original = repo.create_entry(
            tx, **entry_args(entry_id=identity, deduplication_key="original-key")
        )
        if kind == "delivery":
            original = repo.register_delivery_identity(
                tx,
                owner_id="owner-1",
                entry_id=original.entry_id,
                delivery_key="original-key",
                created_at=20,
                delivery_id=identity,
            )
            target = repo.create_entry(tx, **entry_args(owner_id=owner))
        repo.put_preferences(tx, **preference_args(owner_id=owner))
        with pytest.raises(RepositoryConflictError) as refusal:
            if kind == "entry":
                repo.create_entry(
                    tx, **entry_args(owner_id=owner, entry_id=identity, deduplication_key=key)
                )
            else:
                repo.register_delivery_identity(
                    tx,
                    owner_id=owner,
                    entry_id=target.entry_id,
                    delivery_key=key,
                    created_at=20,
                    delivery_id=identity,
                )
        assert not refusal.value.metadata
        assert refusal.value.__cause__ is None
        assert refusal.value.__context__ is None
        assert "owner-1" not in str(refusal.value)
        assert identity not in str(refusal.value)
        assert repo.get_preferences(tx, owner_id=owner).revision == 1
    with db.transaction() as tx:
        assert repo.get_preferences(tx, owner_id=owner).revision == 1
        if kind == "entry":
            assert repo.get_entry(tx, owner_id="owner-1", entry_id=identity) == original
            assert (
                repo.get_entry_by_deduplication_key(
                    tx,
                    owner_id=owner,
                    deduplication_key=key,
                )
                is None
            )
        else:
            assert (
                repo.get_delivery_identity(
                    tx,
                    owner_id="owner-1",
                    delivery_id=identity,
                )
                == original
            )
            assert (
                repo.list_delivery_identities(
                    tx, owner_id=owner, entry_id=target.entry_id, limit=10
                )
                == ()
            )


@pytest.mark.parametrize("kind", ["entry", "delivery"])
def test_unhandled_uuid_conflict_rolls_back_only_the_callers_work(migrated_clone, kind):
    repo = create_repository_catalog().notifications
    db = migrated_clone.database
    identity = "40000000-0000-4000-8000-000000000001"
    with db.transaction() as tx:
        original = repo.create_entry(tx, **entry_args(entry_id=identity))
        if kind == "delivery":
            repo.register_delivery_identity(
                tx,
                owner_id="owner-1",
                entry_id=original.entry_id,
                delivery_key="original-key",
                created_at=20,
                delivery_id=identity,
            )
    with pytest.raises(RepositoryConflictError), db.transaction() as tx:
        repo.put_preferences(tx, **preference_args())
        if kind == "entry":
            repo.create_entry(tx, **entry_args(entry_id=identity, deduplication_key="new-key"))
        else:
            repo.register_delivery_identity(
                tx,
                owner_id="owner-1",
                entry_id=original.entry_id,
                delivery_key="new-key",
                created_at=20,
                delivery_id=identity,
            )
    with db.transaction() as tx:
        assert repo.get_preferences(tx, owner_id="owner-1") is None
        assert repo.get_entry(tx, owner_id="owner-1", entry_id=identity) == original


@pytest.mark.parametrize(
    ("operation", "field"),
    [("acknowledge_entry", "acknowledged_at"), ("delete_entry", "deleted_at")],
)
@pytest.mark.parametrize("revision", [9007199254740990, 9007199254740991])
def test_revision_boundary_is_typed_and_preserves_replays(
    migrated_clone,
    operation,
    field,
    revision,
):
    repo = create_repository_catalog().notifications
    db = migrated_clone.database
    with db.transaction() as tx:
        original = repo.create_entry(tx, **entry_args())
        tx.execute(
            "UPDATE notification_inbox_entry SET revision=%s WHERE entry_id=%s",
            (revision, original.entry_id),
        )
        values = dict(
            owner_id="owner-1",
            entry_id=original.entry_id,
            expected_revision=revision,
            **{field: 30},
        )
        if revision == 9007199254740991:
            with pytest.raises(RepositoryConflictError, match="exhausted"):
                getattr(repo, operation)(tx, **values)
            unchanged = repo.get_entry(tx, owner_id="owner-1", entry_id=original.entry_id)
            assert unchanged.revision == revision
            assert getattr(unchanged, field) is None
        else:
            changed = getattr(repo, operation)(tx, **values)
            assert changed.revision == 9007199254740991
            assert getattr(changed, field) == 30
            values["expected_revision"] = 9007199254740991
            assert getattr(repo, operation)(tx, **values) == changed
            values[field] = 31
            with pytest.raises(RepositoryConflictError):
                getattr(repo, operation)(tx, **values)
        assert repo.put_preferences(tx, **preference_args()).revision == 1
