"""Unit and facade checks for push destination and delivery fences plus the pinned
089.011 migration edge. Real PostgreSQL behavior is exercised separately by the focused
push-delivery repository suite.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

import astralplane
from astralplane.api import create_repository_catalog
from astralplane.database import migrations as canonical
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.push_deliveries import PushDeliveryRepository
from tests.repositories._support import Result, ScriptedTransaction

OWNER = "push-owner"
TOKEN = b"synthetic-host-encrypted-push-token"
DIGEST = hashlib.sha256(TOKEN).hexdigest()
DESTINATION_ID = str(uuid.UUID(int=10))
DELIVERY_ID = str(uuid.UUID(int=11))
ENTRY_ID = str(uuid.UUID(int=12))
CLAIM_ID = str(uuid.UUID(int=13))


def destination_row(**changes):
    row = {
        "destination_id": DESTINATION_ID,
        "owner_id": OWNER,
        "destination_key": "device-1",
        "token_format": "astral.push-token/v1",
        "token_ciphertext": TOKEN,
        "token_sha256": DIGEST,
        "token_version": 1,
        "revision": 1,
        "created_at": 10,
        "updated_at": 10,
        "revoked_at": None,
        "deleted_at": None,
    }
    row.update(changes)
    return row


def delivery_row(**changes):
    row = {
        "delivery_id": DELIVERY_ID,
        "owner_id": OWNER,
        "entry_id": ENTRY_ID,
        "destination_id": DESTINATION_ID,
        "delivery_key": "delivery-1",
        "event_kind": "completion",
        "content_format": "astral.notification/v1",
        "content_ciphertext": b"synthetic-payload",
        "content_sha256": hashlib.sha256(b"synthetic-payload").hexdigest(),
        "token_version": 1,
        "destination_revision": 1,
        "status": "claimed",
        "attempt_count": 1,
        "max_attempts": 3,
        "available_at": 20,
        "lease_owner": "worker-1",
        "claim_id": CLAIM_ID,
        "lease_expires_at": 60,
        "claimed_at": 30,
        "acknowledged_at": None,
        "last_error_code": None,
        "created_at": 20,
        "updated_at": 30,
        "version": 1,
    }
    row.update(changes)
    return row


def claimed_row(**changes):
    row = delivery_row(**changes)
    row.update(
        {
            "destination_token_format": "astral.push-token/v1",
            "destination_token_ciphertext": TOKEN,
            "destination_token_sha256": DIGEST,
        }
    )
    return row


def test_facade_exposes_push_delivery_repository() -> None:
    catalog = create_repository_catalog()
    assert isinstance(catalog.push_deliveries, PushDeliveryRepository)
    assert isinstance(catalog.as_mapping()["push_deliveries"], PushDeliveryRepository)
    assert astralplane.PushDeliveryRepository is PushDeliveryRepository
    assert astralplane.create_push_delivery_repository is not None


def test_089_011_push_delivery_edge_is_pinned_to_the_exact_089_010_registry() -> None:
    from astralplane.database.push_delivery_schema import PUSH_DELIVERY_SCHEMA_STATEMENTS

    edge = canonical.PLANE_SCHEMA_089_011_MIGRATION
    assert edge in canonical.MIGRATION_REGISTRY.migrations
    assert canonical.MIGRATION_REGISTRY.migrations[-12] is edge
    assert edge.name == "astralplane-089-push-deliveries"
    assert edge.source_revisions == ("089.010",)
    assert edge.target_revision == "089.011"
    assert edge.checksum == canonical._statements_checksum(PUSH_DELIVERY_SCHEMA_STATEMENTS)

    ddl = "\n".join(PUSH_DELIVERY_SCHEMA_STATEMENTS)
    for table in ("notification_device_destination", "notification_push_delivery"):
        assert f"CREATE TABLE {table}" in ddl
        assert f"('{table}')" in canonical.CURRENT_SCHEMA_STRUCTURE_QUERY
    assert "token_version BIGINT NOT NULL" in ddl
    assert "destination_revision BIGINT NOT NULL" in ddl
    assert "FOR UPDATE" not in ddl
    assert "ALTER TABLE" not in ddl
    assert "IF NOT EXISTS" not in ddl

    assert canonical.PLANE_SCHEMA_089_010_REGISTRY_DIGEST == (
        "facf476fbde30deb1952b352e2f80d56742956c368b9f97e3e04478934cc2d9b"
    )
    assert canonical.CURRENT_DATA_PLANE_REVISION.schema_revision == "089.022"
    assert canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.010") == (
        canonical.PLANE_SCHEMA_089_010_REGISTRY_DIGEST
    )
    assert dict(canonical.PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS)["089.010"] == (
        "1e1fb4ca7f6ec63e790a65c3ebbaf028702f0ddefb084152fba6cb19cb161c68",
    )
    assert canonical.CURRENT_SCHEMA_STRUCTURE_DIGEST == (
        "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
    )
    assert canonical.CURRENT_SCHEMA_VERIFIER_CHECKSUM == (
        "2aba6eb28dcfcea86eb53f704719332ff5dc3a8dcee37f26dae267f32e761d9c"
    )
    assert canonical.PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "836964bd86b0329361aaa84d33712cdb07ee2277f62c143bca5ddd3fa31f627e"
    )
    assert canonical.MIGRATION_DIGEST == (
        "dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27"
    )


def test_destination_reads_and_identity_validation_are_owner_scoped() -> None:
    transaction = ScriptedTransaction(one=[destination_row()])
    result = PushDeliveryRepository().get_destination(
        transaction,
        owner_id=OWNER,
        destination_id=DESTINATION_ID,
    )
    assert result.destination_id == DESTINATION_ID
    assert "owner_id = %s" in transaction.calls[0][1]
    assert transaction.calls[0][2] == (OWNER, DESTINATION_ID)
    with pytest.raises(RepositoryValidationError):
        PushDeliveryRepository().get_destination(transaction, owner_id=OWNER)
    with pytest.raises(RepositoryValidationError):
        PushDeliveryRepository().get_destination(
            transaction,
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            destination_key="device-1",
        )


def test_destination_insert_replay_conflict_and_token_validation() -> None:
    repository = PushDeliveryRepository()
    with pytest.raises(RepositoryValidationError):
        repository.register_destination(
            ScriptedTransaction(),
            owner_id=OWNER,
            destination_key="device-1",
            token_format="format",
            token_ciphertext=TOKEN,
            token_sha256="0" * 64,
            created_at=10,
        )
    transaction = ScriptedTransaction(
        execute=[Result()],
        one=[destination_row(token_ciphertext=b"different")],
    )
    with pytest.raises(RepositoryConflictError, match="immutable token"):
        repository.register_destination(
            transaction,
            owner_id=OWNER,
            destination_key="device-1",
            token_format="astral.push-token/v1",
            token_ciphertext=TOKEN,
            token_sha256=DIGEST,
            created_at=10,
        )
    assert "destination_key" in transaction.calls[1][1]


def test_enqueue_replay_and_conflict_paths_are_typed() -> None:
    repository = PushDeliveryRepository()
    transaction = ScriptedTransaction(
        execute=[Result()],
        one=[
            destination_row(),
            {
                "event_kind": "completion",
                "content_format": "astral.notification/v1",
                "content_ciphertext": b"synthetic-payload",
                "content_sha256": hashlib.sha256(b"synthetic-payload").hexdigest(),
            },
            delivery_row(max_attempts=4),
        ],
    )
    with pytest.raises(RepositoryConflictError, match="immutable delivery semantics"):
        repository.enqueue_delivery(
            transaction,
            owner_id=OWNER,
            entry_id=ENTRY_ID,
            destination_id=DESTINATION_ID,
            delivery_key="delivery-1",
            expected_destination_revision=1,
            expected_token_version=1,
            max_attempts=3,
            available_at=20,
            created_at=20,
        )
    assert "FOR SHARE" in transaction.calls[0][1]


def test_claim_validation_and_record_shape() -> None:
    repository = PushDeliveryRepository()
    with pytest.raises(RepositoryValidationError):
        repository.claim_deliveries(
            ScriptedTransaction(),
            owner_id=OWNER,
            worker_id="worker",
            now=30,
            lease_expires_at=30,
            limit=1,
        )
    with pytest.raises(RepositoryValidationError):
        repository.claim_deliveries(
            ScriptedTransaction(),
            owner_id=OWNER,
            worker_id="worker",
            now=30,
            lease_expires_at=30 + 86401,
            limit=1,
        )
    transaction = ScriptedTransaction(all_rows=((claimed_row(),),))
    claimed = repository.claim_deliveries(
        transaction,
        owner_id=OWNER,
        worker_id="worker-1",
        now=30,
        lease_expires_at=60,
        limit=1,
    )
    assert claimed[0].token_ciphertext == TOKEN
    statement = transaction.calls[0][1]
    assert "FOR UPDATE OF delivery SKIP LOCKED" in statement
    assert "FOR SHARE OF destination" in statement
    assert "destination.token_version = delivery.token_version" in statement


def test_acknowledgment_and_failure_fences_are_typed() -> None:
    repository = PushDeliveryRepository()
    stale = ScriptedTransaction(
        execute=[Result()],
        one=[delivery_row(claim_id=str(uuid.UUID(int=99)))],
    )
    with pytest.raises(RepositoryConflictError, match="superseded"):
        repository.acknowledge_delivery(
            stale,
            owner_id=OWNER,
            delivery_id=DELIVERY_ID,
            claim_id=CLAIM_ID,
            acknowledged_at=40,
        )
    replay = ScriptedTransaction(
        execute=[Result()],
        one=[
            delivery_row(
                status="retry",
                lease_owner=None,
                lease_expires_at=None,
                last_error_code="transport_error",
                available_at=45,
                updated_at=44,
            )
        ],
    )
    result = repository.fail_delivery(
        replay,
        owner_id=OWNER,
        delivery_id=DELIVERY_ID,
        claim_id=CLAIM_ID,
        failed_at=44,
        error_code="transport_error",
        retry_at=45,
    )
    assert result.status == "retry"
    with pytest.raises(RepositoryValidationError):
        repository.fail_delivery(
            ScriptedTransaction(),
            owner_id=OWNER,
            delivery_id=DELIVERY_ID,
            claim_id=CLAIM_ID,
            failed_at=44,
            error_code="UPPER",
        )
    with pytest.raises(RepositoryValidationError):
        repository.fail_delivery(
            ScriptedTransaction(),
            owner_id=OWNER,
            delivery_id=DELIVERY_ID,
            claim_id=CLAIM_ID,
            failed_at=44,
            error_code="transport_error",
            retry_at=43,
        )


def test_expire_stale_is_owner_scoped_and_returns_records() -> None:
    transaction = ScriptedTransaction(
        all_rows=(
            (
                delivery_row(
                    status="superseded",
                    lease_owner=None,
                    lease_expires_at=None,
                ),
            ),
        )
    )
    records = PushDeliveryRepository().expire_stale_deliveries(
        transaction,
        owner_id=OWNER,
        expired_at=50,
        limit=10,
    )
    assert records[0].status == "superseded"
    assert transaction.calls[0][2] == (OWNER, 9007199254740991, 10, 50)
    assert "FOR UPDATE OF delivery SKIP LOCKED" in transaction.calls[0][1]


def test_corrupt_persisted_records_fail_closed() -> None:
    with pytest.raises(RepositoryDataError, match="retained"):
        PushDeliveryRepository().get_destination(
            ScriptedTransaction(
                one=[destination_row(deleted_at=20, updated_at=20)],
            ),
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            include_deleted=True,
        )
    with pytest.raises(RepositoryDataError, match="opaque bytes"):
        PushDeliveryRepository().get_delivery(
            ScriptedTransaction(one=[delivery_row(content_ciphertext="plaintext")]),
            owner_id=OWNER,
            delivery_id=DELIVERY_ID,
        )

def test_optional_row_and_destination_corruption_fail_closed() -> None:
    repository = PushDeliveryRepository()
    row = destination_row()
    with pytest.raises(RepositoryDataError, match="multiple"):
        repository.register_destination(
            ScriptedTransaction(execute=[Result(returned_records=(row, row))]),
            owner_id=OWNER,
            destination_key="device-1",
            token_format="astral.push-token/v1",
            token_ciphertext=TOKEN,
            token_sha256=DIGEST,
            created_at=10,
        )
    with pytest.raises(RepositoryDataError, match="non-mapping"):
        repository.register_destination(
            ScriptedTransaction(execute=[Result(returned_records=("invalid",))]),
            owner_id=OWNER,
            destination_key="device-1",
            token_format="astral.push-token/v1",
            token_ciphertext=TOKEN,
            token_sha256=DIGEST,
            created_at=10,
        )
    with pytest.raises(RepositoryConflictError, match="identity already exists"):
        repository.register_destination(
            ScriptedTransaction(execute=[Result()], one=[None]),
            owner_id=OWNER,
            destination_key="device-1",
            token_format="astral.push-token/v1",
            token_ciphertext=TOKEN,
            token_sha256=DIGEST,
            created_at=10,
        )
    for corrupt in (
        destination_row(token_format=None, token_ciphertext=None, token_sha256=None),
        destination_row(token_ciphertext="plaintext"),
        destination_row(revoked_at="invalid"),
    ):
        with pytest.raises(RepositoryDataError):
            repository.get_destination(
                ScriptedTransaction(one=[corrupt]),
                owner_id=OWNER,
                destination_id=DESTINATION_ID,
            )


def test_delivery_record_validation_and_claim_corruption_fail_closed() -> None:
    repository = PushDeliveryRepository()
    for corrupt in (
        delivery_row(status="unknown"),
        delivery_row(lease_owner=None),
        delivery_row(
            status="pending",
            attempt_count=0,
            lease_owner=None,
            lease_expires_at=None,
            claim_id=CLAIM_ID,
            claimed_at=30,
        ),
        delivery_row(
            status="retry",
            attempt_count=1,
            lease_owner=None,
            lease_expires_at=None,
            claim_id=None,
            claimed_at=None,
            last_error_code="transport_error",
        ),
    ):
        with pytest.raises(RepositoryDataError):
            repository.get_delivery(
                ScriptedTransaction(one=[corrupt]),
                owner_id=OWNER,
                delivery_id=DELIVERY_ID,
            )
    with pytest.raises(RepositoryDataError, match="missing its opaque destination token"):
        repository.claim_deliveries(
            ScriptedTransaction(
                all_rows=(({**claimed_row(), "destination_token_ciphertext": None},),)
            ),
            owner_id=OWNER,
            worker_id="worker-1",
            now=30,
            lease_expires_at=60,
            limit=1,
        )
    with pytest.raises(RepositoryDataError, match="different worker"):
        repository.claim_deliveries(
            ScriptedTransaction(
                all_rows=((claimed_row(lease_owner="different-worker"),),)
            ),
            owner_id=OWNER,
            worker_id="worker-1",
            now=30,
            lease_expires_at=60,
            limit=1,
        )


def test_destination_validation_listing_and_lifecycle_fences() -> None:
    repository = PushDeliveryRepository()
    for token_value, digest in (("not-bytes", DIGEST), (b"", DIGEST), (TOKEN, "Z" * 64)):
        with pytest.raises(RepositoryValidationError):
            repository.register_destination(
                ScriptedTransaction(),
                owner_id=OWNER,
                destination_key="device-1",
                token_format="format",
                token_ciphertext=token_value,
                token_sha256=digest,
                created_at=10,
            )
    with pytest.raises(RepositoryValidationError):
        repository.register_destination(
            ScriptedTransaction(),
            owner_id=OWNER,
            destination_key="device-1",
            token_format="",
            token_ciphertext=TOKEN,
            token_sha256=DIGEST,
            created_at=10,
        )
    with pytest.raises(RepositoryValidationError):
        repository.get_destination(
            ScriptedTransaction(),
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            include_deleted=1,
        )
    listed = ScriptedTransaction(all_rows=((destination_row(),),))
    assert (
        len(
            repository.list_destinations(
                listed, owner_id=OWNER, limit=10, include_deleted=True
            )
        )
        == 1
    )
    assert "deleted_at IS NULL" not in listed.calls[0][1]
    with pytest.raises(RepositoryValidationError):
        repository.list_destinations(
            ScriptedTransaction(), owner_id=OWNER, limit=10, include_deleted=1
        )

    def lifecycle(row, method, **changes):
        values = dict(
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            expected_revision=changes.pop("expected_revision", 1),
        )
        values.update(changes)
        return method(
            ScriptedTransaction(execute=[Result()], one=[row]),
            **values,
        )

    with pytest.raises(RepositoryConflictError, match="exhausted"):
        repository.revoke_destination(
            ScriptedTransaction(),
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            expected_revision=9007199254740991,
            revoked_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="exhausted"):
        repository.delete_destination(
            ScriptedTransaction(),
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            expected_revision=9007199254740991,
            deleted_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="stale"):
        lifecycle(destination_row(revision=2), repository.revoke_destination, revoked_at=20)
    with pytest.raises(RepositoryConflictError, match="precedes destination creation"):
        lifecycle(
            destination_row(created_at=40, updated_at=40),
            repository.revoke_destination,
            revoked_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="revocation fence rejected"):
        lifecycle(destination_row(), repository.revoke_destination, revoked_at=20)
    with pytest.raises(RepositoryConflictError, match="stale"):
        lifecycle(destination_row(revision=2), repository.delete_destination, deleted_at=20)
    with pytest.raises(RepositoryConflictError, match="precedes destination creation"):
        lifecycle(
            destination_row(created_at=40, updated_at=40),
            repository.delete_destination,
            deleted_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="precedes destination revocation"):
        lifecycle(
            destination_row(revoked_at=30, updated_at=30),
            repository.delete_destination,
            deleted_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="deletion fence rejected"):
        lifecycle(destination_row(), repository.delete_destination, deleted_at=20)
    with pytest.raises(RepositoryConflictError, match="deleted"):
        repository.rotate_destination_token(
            ScriptedTransaction(
                execute=[Result()],
                one=[
                    destination_row(
                        deleted_at=20,
                        updated_at=20,
                        token_format=None,
                        token_ciphertext=None,
                        token_sha256=None,
                    )
                ],
            ),
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            expected_revision=1,
            expected_token_version=1,
            token_format="format",
            token_ciphertext=TOKEN,
            token_sha256=DIGEST,
            rotated_at=21,
        )
    with pytest.raises(RepositoryConflictError, match="rotation fence rejected"):
        repository.rotate_destination_token(
            ScriptedTransaction(execute=[Result()], one=[destination_row()]),
            owner_id=OWNER,
            destination_id=DESTINATION_ID,
            expected_revision=1,
            expected_token_version=1,
            token_format="astral.push-token/v1",
            token_ciphertext=TOKEN,
            token_sha256=DIGEST,
            rotated_at=20,
        )


def test_enqueue_and_acknowledgment_failure_branches() -> None:
    repository = PushDeliveryRepository()
    destination = destination_row()
    entry = {
        "event_kind": "completion",
        "content_format": "astral.notification/v1",
        "content_ciphertext": "plaintext",
        "content_sha256": hashlib.sha256(b"synthetic-payload").hexdigest(),
    }
    with pytest.raises(RepositoryDataError, match="opaque bytes"):
        repository.enqueue_delivery(
            ScriptedTransaction(one=[destination, entry]),
            owner_id=OWNER,
            entry_id=ENTRY_ID,
            destination_id=DESTINATION_ID,
            delivery_key="delivery-1",
            expected_destination_revision=1,
            expected_token_version=1,
            max_attempts=3,
            available_at=20,
            created_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="identity already exists"):
        repository.enqueue_delivery(
            ScriptedTransaction(
                execute=[Result()],
                one=[
                    destination,
                    {**entry, "content_ciphertext": b"synthetic-payload"},
                    None,
                ],
            ),
            owner_id=OWNER,
            entry_id=ENTRY_ID,
            destination_id=DESTINATION_ID,
            delivery_key="delivery-1",
            expected_destination_revision=1,
            expected_token_version=1,
            max_attempts=3,
            available_at=20,
            created_at=20,
        )
    filtered = ScriptedTransaction(all_rows=((delivery_row(),),))
    assert len(
        repository.list_deliveries(
            filtered,
            owner_id=OWNER,
            limit=10,
            status="claimed",
            destination_id=DESTINATION_ID,
        )
    ) == 1
    assert "status = %s" in filtered.calls[0][1]
    assert "destination_id = %s" in filtered.calls[0][1]

    def acknowledge(row, acknowledged_at=40):
        return repository.acknowledge_delivery(
            ScriptedTransaction(execute=[Result()], one=[row]),
            owner_id=OWNER,
            delivery_id=DELIVERY_ID,
            claim_id=CLAIM_ID,
            acknowledged_at=acknowledged_at,
        )

    with pytest.raises(RepositoryNotFoundError):
        acknowledge(None)
    with pytest.raises(RepositoryConflictError, match="already acknowledged"):
        acknowledge(
            delivery_row(
                status="delivered",
                lease_owner=None,
                lease_expires_at=None,
                acknowledged_at=39,
                updated_at=39,
            )
        )
    with pytest.raises(RepositoryConflictError, match="already carries"):
        acknowledge(delivery_row(acknowledged_at=39, updated_at=39))
    with pytest.raises(RepositoryConflictError, match="precedes"):
        acknowledge(delivery_row(updated_at=40), acknowledged_at=39)
    with pytest.raises(RepositoryConflictError, match="version is exhausted"):
        acknowledge(delivery_row(version=9007199254740991))
    with pytest.raises(RepositoryConflictError, match="fence rejected"):
        acknowledge(delivery_row())

    def fail(row, **changes):
        values = dict(
            owner_id=OWNER,
            delivery_id=DELIVERY_ID,
            claim_id=CLAIM_ID,
            failed_at=40,
            error_code="transport_error",
            retry_at=41,
        )
        values.update(changes)
        return repository.fail_delivery(
            ScriptedTransaction(execute=[Result()], one=[row]),
            **values,
        )

    with pytest.raises(RepositoryNotFoundError):
        fail(None)
    with pytest.raises(RepositoryConflictError, match="acknowledged"):
        fail(
            delivery_row(
                status="delivered",
                lease_owner=None,
                lease_expires_at=None,
                acknowledged_at=39,
                updated_at=39,
            )
        )
    with pytest.raises(RepositoryConflictError, match="not claimable"):
        fail(
            delivery_row(
                status="superseded",
                lease_owner=None,
                lease_expires_at=None,
            )
        )
    with pytest.raises(RepositoryConflictError, match="time regressed"):
        fail(delivery_row(updated_at=40))
    with pytest.raises(RepositoryConflictError, match="version is exhausted"):
        fail(delivery_row(version=9007199254740991))
    with pytest.raises(RepositoryConflictError, match="failure fence rejected"):
        fail(delivery_row())
