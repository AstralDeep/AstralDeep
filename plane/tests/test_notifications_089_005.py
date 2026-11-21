"""Unit and contract checks for notification preference, inbox, and delivery-identity
fences, plus the pinned 089.005 migration edge and public facade.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from astralplane.database import migrations as canonical
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
)
from astralplane.repositories.notifications import (
    NotificationRepository,
)
from tests.repositories._support import Result, ScriptedTransaction

OLD_089_004_REGISTRY_DIGEST = "08b7d824a62284351d1ce178708bb185d7c81f77c96ab14e890f7e5675a008fc"
CURRENT_089_005_REGISTRY_DIGEST = "98a9082bb3317192cd6e48df3b63c76dbb9404814cb4a0180eb997f8737e37b0"
CURRENT_089_005_STRUCTURE_DIGEST = (
    "2aae4fa6281bda3ca690ce9eb174a5e43b454f0862b0544129bd537eb67bb798"
)


def _preference_row(**changes):
    row = {
        "owner_id": "owner-1",
        "in_app_completion": True,
        "in_app_failure": True,
        "in_app_required_action": True,
        "inbox_completion": True,
        "inbox_failure": True,
        "inbox_required_action": True,
        "push_completion": False,
        "push_failure": False,
        "push_required_action": False,
        "revision": 1,
        "updated_at": 100,
    }
    row.update(changes)
    return row


def _entry_row(**changes):
    body = b"synthetic-opaque-notification"
    row = {
        "entry_id": str(uuid.UUID(int=1)),
        "owner_id": "owner-1",
        "event_kind": "completion",
        "source_id": "assignment-1",
        "deduplication_key": "completion:assignment-1:1",
        "content_format": "astral.notification/v1",
        "content_ciphertext": body,
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "created_at": 10,
        "retention_expires_at": 100,
        "acknowledged_at": None,
        "deleted_at": None,
        "revision": 0,
    }
    row.update(changes)
    return row


def _delivery_row(**changes):
    row = {
        "delivery_id": str(uuid.UUID(int=9)),
        "entry_id": str(uuid.UUID(int=1)),
        "owner_id": "owner-1",
        "delivery_key": "delivery-key-1",
        "created_at": 50,
    }
    row.update(changes)
    return row


def test_preferences_read_is_owner_scoped() -> None:
    transaction = ScriptedTransaction(one=[_preference_row()])
    result = NotificationRepository().get_preferences(transaction, owner_id="owner-1")
    assert result.owner_id == "owner-1"
    assert "WHERE owner_id = %s" in transaction.calls[0][1]
    assert transaction.calls[0][2] == ("owner-1",)


def test_entry_exact_replay_returns_original() -> None:
    transaction = ScriptedTransaction(
        execute=[Result()],
        one=[_entry_row()],
    )
    result = NotificationRepository().create_entry(
        transaction,
        owner_id="owner-1",
        event_kind="completion",
        source_id="assignment-1",
        deduplication_key="completion:assignment-1:1",
        content_format="astral.notification/v1",
        content_ciphertext=b"synthetic-opaque-notification",
        content_sha256=hashlib.sha256(b"synthetic-opaque-notification").hexdigest(),
        created_at=10,
        retention_expires_at=100,
    )
    assert result.entry_id == str(uuid.UUID(int=1))
    assert transaction.calls[1][2] == ("owner-1", "completion:assignment-1:1")


def test_acknowledgment_stale_revision_conflicts() -> None:
    transaction = ScriptedTransaction(
        execute=[Result()],
        one=[_entry_row(revision=1)],
    )
    with pytest.raises(RepositoryConflictError, match="stale"):
        NotificationRepository().acknowledge_entry(
            transaction,
            owner_id="owner-1",
            entry_id=str(uuid.UUID(int=1)),
            expected_revision=0,
            acknowledged_at=30,
        )
    assert "revision = %s" in transaction.calls[0][1]


def test_deleted_entry_cannot_be_acknowledged() -> None:
    transaction = ScriptedTransaction(
        execute=[Result()],
        one=[_entry_row(deleted_at=40, revision=1)],
    )
    with pytest.raises(RepositoryConflictError, match="deleted"):
        NotificationRepository().acknowledge_entry(
            transaction,
            owner_id="owner-1",
            entry_id=str(uuid.UUID(int=1)),
            expected_revision=0,
            acknowledged_at=30,
        )


def test_delivery_identity_replay_conflict_is_typed() -> None:
    transaction = ScriptedTransaction(
        execute=[Result()],
        one=[
            {"entry_id": str(uuid.UUID(int=1)), "deleted_at": None},
            _delivery_row(created_at=51),
        ],
    )
    with pytest.raises(RepositoryConflictError, match="immutable semantics"):
        NotificationRepository().register_delivery_identity(
            transaction,
            owner_id="owner-1",
            entry_id=str(uuid.UUID(int=1)),
            delivery_key="delivery-key-1",
            created_at=50,
        )
    assert "FOR UPDATE" in transaction.calls[0][1]


def test_preference_revision_exhaustion_is_visible() -> None:
    transaction = ScriptedTransaction(
        one=[_preference_row(revision=9007199254740991, push_completion=True)],
    )
    with pytest.raises(RepositoryConflictError, match="exhausted"):
        NotificationRepository().put_preferences(
            transaction,
            owner_id="owner-1",
            expected_revision=9007199254740991,
            in_app_completion=True,
            in_app_failure=True,
            in_app_required_action=True,
            inbox_completion=True,
            inbox_failure=True,
            inbox_required_action=True,
            push_completion=False,
            push_failure=False,
            push_required_action=False,
            updated_at=101,
        )


def test_entry_revision_exhaustion_is_visible_for_ack_and_delete() -> None:
    repository = NotificationRepository()
    exhausted = _entry_row(revision=9007199254740991)
    acknowledgment = ScriptedTransaction(one=[exhausted])
    with pytest.raises(RepositoryConflictError, match="exhausted"):
        repository.acknowledge_entry(
            acknowledgment,
            owner_id="owner-1",
            entry_id=str(uuid.UUID(int=1)),
            expected_revision=9007199254740991,
            acknowledged_at=30,
        )
    deletion = ScriptedTransaction(one=[exhausted])
    with pytest.raises(RepositoryConflictError, match="exhausted"):
        repository.delete_entry(
            deletion,
            owner_id="owner-1",
            entry_id=str(uuid.UUID(int=1)),
            expected_revision=9007199254740991,
            deleted_at=30,
        )

    assert all(call[0] == "one" for call in acknowledgment.calls + deletion.calls)


def test_missing_insert_owner_rows_are_reported() -> None:
    repository = NotificationRepository()
    with pytest.raises(RepositoryConflictError, match="lost its owner row"):
        repository.put_preferences(
            ScriptedTransaction(execute=[Result()], one=[None]),
            owner_id="owner-1",
            expected_revision=0,
            in_app_completion=True,
            in_app_failure=True,
            in_app_required_action=True,
            inbox_completion=True,
            inbox_failure=True,
            inbox_required_action=True,
            push_completion=False,
            push_failure=False,
            push_required_action=False,
            updated_at=100,
        )
    with pytest.raises(RepositoryConflictError, match="identity already exists"):
        repository.create_entry(
            ScriptedTransaction(execute=[Result()], one=[None]),
            owner_id="owner-1",
            event_kind="completion",
            source_id="assignment-1",
            deduplication_key="completion:assignment-1:1",
            content_format="astral.notification/v1",
            content_ciphertext=b"synthetic-opaque-notification",
            content_sha256=hashlib.sha256(b"synthetic-opaque-notification").hexdigest(),
            created_at=10,
            retention_expires_at=100,
        )


def test_persisted_non_opaque_content_fails_closed() -> None:
    transaction = ScriptedTransaction(
        one=[_entry_row(content_ciphertext="plaintext")],
    )
    with pytest.raises(RepositoryDataError, match="opaque bytes"):
        NotificationRepository().get_entry(
            transaction,
            owner_id="owner-1",
            entry_id=str(uuid.UUID(int=1)),
        )


def test_unknown_preference_update_is_not_found() -> None:
    transaction = ScriptedTransaction(one=[None])
    with pytest.raises(RepositoryNotFoundError):
        NotificationRepository().put_preferences(
            transaction,
            owner_id="owner-1",
            expected_revision=1,
            in_app_completion=True,
            in_app_failure=True,
            in_app_required_action=True,
            inbox_completion=True,
            inbox_failure=True,
            inbox_required_action=True,
            push_completion=False,
            push_failure=False,
            push_required_action=False,
            updated_at=100,
        )


def test_facade_exposes_notifications() -> None:
    from astralplane.api import create_repository_catalog

    catalog = create_repository_catalog()
    assert isinstance(catalog.notifications, NotificationRepository)
    assert isinstance(catalog.as_mapping()["notifications"], NotificationRepository)


def test_089_005_notification_edge_is_pinned_to_the_exact_089_004_registry() -> None:
    from astralplane.database.notification_schema import NOTIFICATION_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_005_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-18] is edge
    assert edge.name == "astralplane-089-notification-records"
    assert edge.source_revisions == ("089.004",)
    assert edge.target_revision == "089.005"
    assert edge.checksum == canonical._statements_checksum(NOTIFICATION_SCHEMA_STATEMENTS)

    ddl = "\n".join(NOTIFICATION_SCHEMA_STATEMENTS)
    for table in (
        "notification_preference",
        "notification_inbox_entry",
        "notification_delivery_identity",
    ):
        assert f"CREATE TABLE {table}" in ddl
        assert f"('{table}')" in canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "UNIQUE(owner_id, deduplication_key)" in ddl
    assert "FOREIGN KEY (entry_id, owner_id)" in ddl
    assert "content_ciphertext BYTEA NOT NULL" in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_004_REGISTRY_DIGEST == OLD_089_004_REGISTRY_DIGEST
    assert canonical.PLANE_SCHEMA_089_004_SCHEMA_VERIFIER_CHECKSUM == (
        "48d93ab8d6372404c557a816e6d1b57bde5e8c2a3ae64c91d0c1e67ae27337a5"
    )
    assert canonical.PLANE_SCHEMA_089_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "e3f2988fea4b6e8cf7edd9064cec67b3d0665f0863de40bbd8332ff9e7f0a9f3"
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.004") == (
        OLD_089_004_REGISTRY_DIGEST
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.004"] == (
        "4be9c1e2ab9fe95472bd1a1a986bcfc578984071cd4c7de41397b39af6851c22",
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.005"] == (
        CURRENT_089_005_STRUCTURE_DIGEST,
    )
    assert canonical.PLANE_SCHEMA_089_005_REGISTRY_DIGEST == CURRENT_089_005_REGISTRY_DIGEST
    assert canonical.PLANE_SCHEMA_089_005_SCHEMA_VERIFIER_CHECKSUM == (
        "e6fe3fdb7a7c87498589b5d9eb11ce7a9c98e6a42619127f585e6c23ca76e1b3"
    )
    assert canonical.PLANE_SCHEMA_089_005_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "0cf15dc0f990fc103169bf0d1c2137b434cfa8ecf63922aed6741092778918f4"
    )
