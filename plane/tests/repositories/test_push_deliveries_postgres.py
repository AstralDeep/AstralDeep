"""Real-PostgreSQL tests for opaque push destinations and claimed delivery records.
They prove token-version fences, rotation, revocation, deletion, replay, bounded
claims, late acknowledgments, restart recovery, concurrency, and caller rollback.
"""

from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_assignments_postgres import database as database

from astralplane.api import create_repository_catalog
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.notifications import NotificationRepository
from astralplane.repositories.push_deliveries import PushDeliveryRepository
from tests.integration.test_lifecycle_acceptance import concurrent_database

OWNER = "push-owner"
FOREIGN = "push-foreign-owner"


@pytest.fixture()
def tx(database):
    with database.transaction() as transaction:
        transaction.execute("DELETE FROM notification_push_delivery")
        transaction.execute("DELETE FROM notification_device_destination")
        transaction.execute("DELETE FROM notification_delivery_identity")
        transaction.execute("DELETE FROM notification_inbox_entry")
        yield transaction


@pytest.fixture()
def repo():
    return PushDeliveryRepository()


def token(value=b"synthetic-host-encrypted-push-token"):
    return value, hashlib.sha256(value).hexdigest()


def make_entry(transaction, *, owner=OWNER, key="push-entry", created_at=10):
    body = b"synthetic-opaque-push-payload"
    return NotificationRepository().create_entry(
        transaction,
        owner_id=owner,
        event_kind="completion",
        source_id="synthetic-push-source",
        deduplication_key=key,
        content_format="astral.notification/v1",
        content_ciphertext=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        created_at=created_at,
        retention_expires_at=1000,
    )


def make_destination(transaction, repo, *, owner=OWNER, key="device-1", created_at=10):
    body, digest = token()
    return repo.register_destination(
        transaction,
        owner_id=owner,
        destination_key=key,
        token_format="astral.push-token/v1",
        token_ciphertext=body,
        token_sha256=digest,
        created_at=created_at,
    )


def enqueue(transaction, repo, entry, destination, **changes):
    values = dict(
        owner_id=destination.owner_id,
        entry_id=entry.entry_id,
        destination_id=destination.destination_id,
        delivery_key="push-delivery-1",
        expected_destination_revision=destination.revision,
        expected_token_version=destination.token_version,
        max_attempts=3,
        available_at=20,
        created_at=20,
    )
    values.update(changes)
    return repo.enqueue_delivery(transaction, **values)


def test_destination_registration_replay_rotation_revocation_and_deletion(repo, tx):
    created = make_destination(tx, repo)
    assert created.revision == 1
    assert created.token_version == 1
    assert created.token_ciphertext == b"synthetic-host-encrypted-push-token"
    assert make_destination(tx, repo) == created
    assert (
        repo.get_destination(tx, owner_id=OWNER, destination_id=created.destination_id)
        == created
    )
    assert (
        repo.get_destination(tx, owner_id=FOREIGN, destination_id=created.destination_id)
        is None
    )
    assert repo.get_destination(
        tx, owner_id=OWNER, destination_key="device-1"
    ) == created
    assert repo.list_destinations(tx, owner_id=OWNER, limit=10) == (created,)
    assert repo.list_destinations(tx, owner_id=FOREIGN, limit=10) == ()

    with pytest.raises(RepositoryConflictError, match="immutable token"):
        repo.register_destination(
            tx,
            owner_id=OWNER,
            destination_key="device-1",
            token_format="astral.push-token/v1",
            token_ciphertext=b"different",
            token_sha256=hashlib.sha256(b"different").hexdigest(),
            created_at=10,
        )

    rotated_body, rotated_digest = token(b"synthetic-rotated-token")
    rotated = repo.rotate_destination_token(
        tx,
        owner_id=OWNER,
        destination_id=created.destination_id,
        expected_revision=1,
        expected_token_version=1,
        token_format="astral.push-token/v2",
        token_ciphertext=rotated_body,
        token_sha256=rotated_digest,
        rotated_at=20,
    )
    assert rotated.revision == 2
    assert rotated.token_version == 2
    assert rotated.token_ciphertext == rotated_body
    assert (
        repo.rotate_destination_token(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=1,
            expected_token_version=1,
            token_format="astral.push-token/v2",
            token_ciphertext=rotated_body,
            token_sha256=rotated_digest,
            rotated_at=20,
        )
        == rotated
    )
    with pytest.raises(RepositoryConflictError, match="revision is stale"):
        repo.rotate_destination_token(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=1,
            expected_token_version=1,
            token_format="astral.push-token/v2",
            token_ciphertext=b"different",
            token_sha256=hashlib.sha256(b"different").hexdigest(),
            rotated_at=21,
        )
    with pytest.raises(RepositoryConflictError, match="token version is stale"):
        repo.rotate_destination_token(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=2,
            expected_token_version=1,
            token_format="astral.push-token/v3",
            token_ciphertext=b"another",
            token_sha256=hashlib.sha256(b"another").hexdigest(),
            rotated_at=21,
        )

    revoked = repo.revoke_destination(
        tx,
        owner_id=OWNER,
        destination_id=created.destination_id,
        expected_revision=2,
        revoked_at=30,
    )
    assert revoked.revoked_at == 30
    assert revoked.revision == 3
    assert (
        repo.revoke_destination(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=2,
            revoked_at=30,
        )
        == revoked
    )
    with pytest.raises(RepositoryConflictError, match="already revoked"):
        repo.revoke_destination(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=2,
            revoked_at=31,
        )
    with pytest.raises(RepositoryConflictError, match="revoked"):
        repo.rotate_destination_token(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=3,
            expected_token_version=2,
            token_format="astral.push-token/v3",
            token_ciphertext=b"another",
            token_sha256=hashlib.sha256(b"another").hexdigest(),
            rotated_at=32,
        )

    deleted = repo.delete_destination(
        tx,
        owner_id=OWNER,
        destination_id=created.destination_id,
        expected_revision=3,
        deleted_at=40,
    )
    assert deleted.deleted_at == 40
    assert deleted.revision == 4
    assert deleted.token_ciphertext is None
    assert deleted.token_format is None
    assert deleted.token_sha256 is None
    assert repo.get_destination(
        tx, owner_id=OWNER, destination_id=created.destination_id
    ) is None
    assert (
        repo.get_destination(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            include_deleted=True,
        )
        == deleted
    )
    assert repo.list_destinations(tx, owner_id=OWNER, limit=10) == ()
    assert repo.list_destinations(tx, owner_id=OWNER, limit=10, include_deleted=True) == (
        deleted,
    )
    assert (
        repo.delete_destination(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=3,
            deleted_at=40,
        )
        == deleted
    )
    with pytest.raises(RepositoryConflictError, match="already deleted"):
        repo.delete_destination(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=3,
            deleted_at=41,
        )


def test_destination_validation_and_cross_owner_denials(repo, tx):
    body, digest = token()
    with pytest.raises(RepositoryValidationError):
        repo.register_destination(
            tx,
            owner_id=OWNER,
            destination_key="device",
            token_format="format",
            token_ciphertext=body,
            token_sha256="0" * 64,
            created_at=1,
        )
    with pytest.raises(RepositoryValidationError):
        repo.register_destination(
            tx,
            owner_id=OWNER,
            destination_key="",
            token_format="format",
            token_ciphertext=body,
            token_sha256=digest,
            created_at=1,
        )
    with pytest.raises(RepositoryValidationError):
        repo.get_destination(tx, owner_id=OWNER)
    with pytest.raises(RepositoryValidationError):
        repo.get_destination(
            tx,
            owner_id=OWNER,
            destination_id="not-a-uuid",
        )
    created = make_destination(tx, repo)
    with pytest.raises(RepositoryNotFoundError):
        repo.rotate_destination_token(
            tx,
            owner_id=FOREIGN,
            destination_id=created.destination_id,
            expected_revision=1,
            expected_token_version=1,
            token_format="format",
            token_ciphertext=body,
            token_sha256=digest,
            rotated_at=20,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.revoke_destination(
            tx,
            owner_id=FOREIGN,
            destination_id=created.destination_id,
            expected_revision=1,
            revoked_at=20,
        )
    with pytest.raises(RepositoryNotFoundError):
        repo.delete_destination(
            tx,
            owner_id=FOREIGN,
            destination_id=created.destination_id,
            expected_revision=1,
            deleted_at=20,
        )
    with pytest.raises(RepositoryConflictError, match="exhausted"):
        repo.rotate_destination_token(
            tx,
            owner_id=OWNER,
            destination_id=created.destination_id,
            expected_revision=9007199254740991,
            expected_token_version=1,
            token_format="format",
            token_ciphertext=body,
            token_sha256=digest,
            rotated_at=20,
        )


def test_enqueue_replay_fences_and_claim_acknowledge_lifecycle(repo, tx):
    entry = make_entry(tx)
    destination = make_destination(tx, repo)
    delivery = enqueue(tx, repo, entry, destination)
    assert delivery.status == "pending"
    assert delivery.token_version == 1
    assert delivery.destination_revision == 1
    assert delivery.content_ciphertext == entry.content_ciphertext
    assert enqueue(tx, repo, entry, destination) == delivery
    assert repo.get_delivery(
        tx, owner_id=OWNER, delivery_id=delivery.delivery_id
    ) == delivery
    assert repo.get_delivery_by_key(
        tx, owner_id=OWNER, delivery_key=delivery.delivery_key
    ) == delivery
    assert repo.get_delivery(tx, owner_id=FOREIGN, delivery_id=delivery.delivery_id) is None
    assert repo.list_deliveries(tx, owner_id=OWNER, limit=10) == (delivery,)
    assert repo.list_deliveries(tx, owner_id=FOREIGN, limit=10) == ()
    with pytest.raises(RepositoryConflictError, match="immutable delivery"):
        enqueue(tx, repo, entry, destination, max_attempts=4)
    with pytest.raises(RepositoryConflictError, match="revision is stale"):
        enqueue(tx, repo, entry, destination, expected_destination_revision=2)
    with pytest.raises(RepositoryConflictError, match="token version is stale"):
        enqueue(tx, repo, entry, destination, expected_token_version=2)
    with pytest.raises(RepositoryValidationError):
        enqueue(tx, repo, entry, destination, max_attempts=0)
    with pytest.raises(RepositoryValidationError):
        enqueue(tx, repo, entry, destination, available_at=19)
    with pytest.raises(RepositoryValidationError):
        repo.list_deliveries(tx, owner_id=OWNER, limit=10, status="unknown")

    claimed = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-1",
        now=30,
        lease_expires_at=60,
        limit=10,
    )
    assert len(claimed) == 1
    assert claimed[0].delivery.delivery_id == delivery.delivery_id
    assert claimed[0].delivery.status == "claimed"
    assert claimed[0].delivery.attempt_count == 1
    assert claimed[0].token_ciphertext == destination.token_ciphertext
    assert claimed[0].token_sha256 == destination.token_sha256
    assert (
        repo.claim_deliveries(
            tx,
            owner_id=OWNER,
            worker_id="worker-2",
            now=30,
            lease_expires_at=60,
            limit=10,
        )
        == ()
    )
    acknowledged = repo.acknowledge_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=delivery.delivery_id,
        claim_id=claimed[0].delivery.claim_id,
        acknowledged_at=31,
    )
    assert acknowledged.status == "delivered"
    assert acknowledged.acknowledged_at == 31
    assert (
        repo.acknowledge_delivery(
            tx,
            owner_id=OWNER,
            delivery_id=delivery.delivery_id,
            claim_id=claimed[0].delivery.claim_id,
            acknowledged_at=31,
        )
        == acknowledged
    )
    with pytest.raises(RepositoryConflictError, match="already acknowledged"):
        repo.acknowledge_delivery(
            tx,
            owner_id=OWNER,
            delivery_id=delivery.delivery_id,
            claim_id=claimed[0].delivery.claim_id,
            acknowledged_at=32,
        )


def test_late_acknowledgment_cannot_overwrite_a_newer_claim(repo, tx):
    entry = make_entry(tx)
    destination = make_destination(tx, repo)
    delivery = enqueue(tx, repo, entry, destination)
    first = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-1",
        now=30,
        lease_expires_at=40,
        limit=1,
    )[0]
    retried = repo.fail_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=delivery.delivery_id,
        claim_id=first.delivery.claim_id,
        failed_at=35,
        error_code="provider_unavailable",
        retry_at=36,
    )
    assert retried.status == "retry"
    late = repo.acknowledge_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=delivery.delivery_id,
        claim_id=first.delivery.claim_id,
        acknowledged_at=37,
    )
    assert late.status == "delivered"

    second_entry = make_entry(tx, key="push-entry-2")
    second = enqueue(
        tx,
        repo,
        second_entry,
        destination,
        delivery_key="push-delivery-2",
    )
    first_claim = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-1",
        now=50,
        lease_expires_at=60,
        limit=1,
    )[0]
    repo.fail_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=second.delivery_id,
        claim_id=first_claim.delivery.claim_id,
        failed_at=51,
        error_code="rate_limited",
        retry_at=52,
    )
    second_claim = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-2",
        now=53,
        lease_expires_at=63,
        limit=1,
    )[0]
    assert second_claim.delivery.claim_id != first_claim.delivery.claim_id
    with pytest.raises(RepositoryConflictError, match="superseded"):
        repo.acknowledge_delivery(
            tx,
            owner_id=OWNER,
            delivery_id=second.delivery_id,
            claim_id=first_claim.delivery.claim_id,
            acknowledged_at=54,
        )
    assert (
        repo.acknowledge_delivery(
            tx,
            owner_id=OWNER,
            delivery_id=second.delivery_id,
            claim_id=second_claim.delivery.claim_id,
            acknowledged_at=54,
        ).status
        == "delivered"
    )


def test_rotation_revocation_and_deletion_block_stale_claims(repo, tx):
    entry = make_entry(tx)
    destination = make_destination(tx, repo)
    first = enqueue(tx, repo, entry, destination, delivery_key="delivery-stale")
    body, digest = token(b"synthetic-next-token")
    rotated = repo.rotate_destination_token(
        tx,
        owner_id=OWNER,
        destination_id=destination.destination_id,
        expected_revision=1,
        expected_token_version=1,
        token_format="astral.push-token/v2",
        token_ciphertext=body,
        token_sha256=digest,
        rotated_at=20,
    )
    assert (
        repo.claim_deliveries(
            tx,
            owner_id=OWNER,
            worker_id="worker",
            now=30,
            lease_expires_at=60,
            limit=10,
        )
        == ()
    )
    superseded = repo.expire_stale_deliveries(
        tx, owner_id=OWNER, expired_at=31, limit=10
    )
    assert [item.delivery_id for item in superseded] == [first.delivery_id]
    assert superseded[0].status == "superseded"
    assert repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker",
        now=32,
        lease_expires_at=62,
        limit=10,
    ) == ()

    in_flight = enqueue(
        tx,
        repo,
        entry,
        rotated,
        delivery_key="delivery-in-flight",
    )
    pending = enqueue(
        tx,
        repo,
        entry,
        rotated,
        delivery_key="delivery-revoked",
        available_at=45,
        created_at=45,
    )
    claimed = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker",
        now=40,
        lease_expires_at=70,
        limit=1,
    )[0]
    assert claimed.delivery.delivery_id == in_flight.delivery_id
    revoked = repo.revoke_destination(
        tx,
        owner_id=OWNER,
        destination_id=destination.destination_id,
        expected_revision=2,
        revoked_at=41,
    )
    late = repo.acknowledge_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=in_flight.delivery_id,
        claim_id=claimed.delivery.claim_id,
        acknowledged_at=42,
    )
    assert late.status == "delivered"
    assert (
        repo.get_destination(
            tx, owner_id=OWNER, destination_id=destination.destination_id
        )
        == revoked
    )

    assert repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker",
        now=50,
        lease_expires_at=80,
        limit=10,
    ) == ()
    deleted = repo.delete_destination(
        tx,
        owner_id=OWNER,
        destination_id=destination.destination_id,
        expected_revision=3,
        deleted_at=51,
    )
    assert deleted.token_ciphertext is None
    assert [item.delivery_id for item in repo.expire_stale_deliveries(
        tx, owner_id=OWNER, expired_at=52, limit=10
    )] == [pending.delivery_id]
    with pytest.raises(RepositoryConflictError, match="deleted"):
        enqueue(tx, repo, entry, deleted, delivery_key="delivery-after-delete")


def test_expired_lease_restart_and_bounded_retry_recovery(repo, tx):
    entry = make_entry(tx)
    destination = make_destination(tx, repo)
    delivery = enqueue(tx, repo, entry, destination, max_attempts=1)
    first = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-1",
        now=30,
        lease_expires_at=40,
        limit=1,
    )[0]
    assert first.delivery.attempt_count == 1
    assert repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-2",
        now=41,
        lease_expires_at=50,
        limit=1,
    ) == ()
    dead = repo.get_delivery(tx, owner_id=OWNER, delivery_id=delivery.delivery_id)
    assert dead.status == "dead_letter"
    assert dead.last_error_code == "lease_expired"
    late = repo.acknowledge_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=delivery.delivery_id,
        claim_id=first.delivery.claim_id,
        acknowledged_at=42,
    )
    assert late.status == "delivered"

    second_entry = make_entry(tx, key="push-entry-2")
    retrying = enqueue(
        tx,
        repo,
        second_entry,
        destination,
        delivery_key="delivery-retry",
        max_attempts=2,
    )
    first_retry_claim = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-1",
        now=60,
        lease_expires_at=70,
        limit=1,
    )[0]
    repo.fail_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=retrying.delivery_id,
        claim_id=first_retry_claim.delivery.claim_id,
        failed_at=61,
        error_code="transport_error",
        retry_at=62,
    )
    second_retry_claim = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker-2",
        now=63,
        lease_expires_at=73,
        limit=1,
    )[0]
    terminal = repo.fail_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=retrying.delivery_id,
        claim_id=second_retry_claim.delivery.claim_id,
        failed_at=64,
        error_code="transport_error",
        retry_at=65,
    )
    assert terminal.status == "dead_letter"
    assert terminal.attempt_count == 2


def test_enqueue_requires_live_owned_entry_and_destination(repo, tx):
    entry = make_entry(tx)
    destination = make_destination(tx, repo)
    with pytest.raises(RepositoryNotFoundError, match="destination"):
        enqueue(tx, repo, entry, destination, destination_id=str(uuid.uuid4()))
    with pytest.raises(RepositoryNotFoundError, match="inbox entry"):
        enqueue(tx, repo, entry, destination, entry_id=str(uuid.uuid4()))
    deleted_entry = NotificationRepository().delete_entry(
        tx,
        owner_id=OWNER,
        entry_id=entry.entry_id,
        expected_revision=0,
        deleted_at=21,
    )
    assert deleted_entry.deleted_at == 21
    with pytest.raises(RepositoryNotFoundError, match="inbox entry"):
        enqueue(tx, repo, entry, destination, delivery_key="deleted-entry")
    revoked = repo.revoke_destination(
        tx,
        owner_id=OWNER,
        destination_id=destination.destination_id,
        expected_revision=1,
        revoked_at=22,
    )
    with pytest.raises(RepositoryConflictError, match="revoked"):
        enqueue(
            tx,
            repo,
            entry,
            revoked,
            delivery_key="revoked",
            expected_destination_revision=2,
            expected_token_version=1,
        )


def test_concurrent_claim_admits_one_worker(migrated_clone):
    repo = create_repository_catalog().push_deliveries
    notifications = create_repository_catalog().notifications
    with migrated_clone.database.transaction() as tx:
        body = b"synthetic-opaque-push-payload"
        entry = notifications.create_entry(
            tx,
            owner_id=OWNER,
            event_kind="completion",
            source_id="concurrent-source",
            deduplication_key="concurrent-entry",
            content_format="astral.notification/v1",
            content_ciphertext=body,
            content_sha256=hashlib.sha256(body).hexdigest(),
            created_at=10,
            retention_expires_at=1000,
        )
        token_body, digest = token()
        destination = repo.register_destination(
            tx,
            owner_id=OWNER,
            destination_key="concurrent-device",
            token_format="astral.push-token/v1",
            token_ciphertext=token_body,
            token_sha256=digest,
            created_at=10,
        )
        repo.enqueue_delivery(
            tx,
            owner_id=OWNER,
            entry_id=entry.entry_id,
            destination_id=destination.destination_id,
            delivery_key="concurrent-delivery",
            expected_destination_revision=1,
            expected_token_version=1,
            max_attempts=2,
            available_at=20,
            created_at=20,
        )
    barrier = Barrier(2)

    def claim(database, worker):
        with database.transaction() as tx:
            barrier.wait(timeout=30)
            return repo.claim_deliveries(
                tx,
                owner_id=OWNER,
                worker_id=worker,
                now=30,
                lease_expires_at=60,
                limit=1,
            )

    with (
        concurrent_database(migrated_clone) as database,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        results = tuple(workers.map(lambda worker: claim(database, worker), ("a", "b")))
    assert sum(len(result) for result in results) == 1


def test_public_push_delivery_contract_behaviors():
    from test_assignments_postgres import standalone_database

    fixture = standalone_database()
    database = next(fixture)
    repo = create_repository_catalog().push_deliveries
    notifications = create_repository_catalog().notifications
    try:
        with database.transaction() as tx:
            entry = notifications.create_entry(
                tx,
                owner_id=OWNER,
                event_kind="completion",
                source_id="contract-source",
                deduplication_key="contract-entry",
                content_format="astral.notification/v1",
                content_ciphertext=b"synthetic-contract-payload",
                content_sha256=hashlib.sha256(b"synthetic-contract-payload").hexdigest(),
                created_at=10,
                retention_expires_at=1000,
            )
            token_body, digest = token()
            destination = repo.register_destination(
                tx,
                owner_id=OWNER,
                destination_key="contract-device",
                token_format="astral.push-token/v1",
                token_ciphertext=token_body,
                token_sha256=digest,
                created_at=10,
            )
            assert (
                repo.get_destination(
                    tx, owner_id=FOREIGN, destination_id=destination.destination_id
                )
                is None
            )
            delivery = enqueue(tx, repo, entry, destination)
            assert enqueue(tx, repo, entry, destination) == delivery
            with pytest.raises(RepositoryConflictError):
                enqueue(tx, repo, entry, destination, max_attempts=5)
            claimed = repo.claim_deliveries(
                tx,
                owner_id=OWNER,
                worker_id="contract-worker",
                now=30,
                lease_expires_at=60,
                limit=1,
            )[0]
            acknowledged = repo.acknowledge_delivery(
                tx,
                owner_id=OWNER,
                delivery_id=delivery.delivery_id,
                claim_id=claimed.delivery.claim_id,
                acknowledged_at=31,
            )
            assert acknowledged.status == "delivered"
            with pytest.raises(RepositoryConflictError):
                repo.acknowledge_delivery(
                    tx,
                    owner_id=OWNER,
                    delivery_id=delivery.delivery_id,
                    claim_id=claimed.delivery.claim_id,
                    acknowledged_at=32,
                )
        with pytest.raises(RuntimeError, match="caller abort"), database.transaction() as tx:
            body, digest = token(b"rollback-token")
            rolled_back = repo.register_destination(
                tx,
                owner_id=OWNER,
                destination_key="rollback-device",
                token_format="astral.push-token/v1",
                token_ciphertext=body,
                token_sha256=digest,
                created_at=40,
            )
            assert repo.get_destination(
                tx, owner_id=OWNER, destination_id=rolled_back.destination_id
            ) is not None
            raise RuntimeError("caller abort")
        with database.transaction() as tx:
            assert repo.get_destination(
                tx, owner_id=OWNER, destination_id=rolled_back.destination_id
            ) is None
    finally:
        with pytest.raises(StopIteration):
            next(fixture)

def test_claim_and_late_acknowledgment_survive_restart(migrated_clone):
    repo = create_repository_catalog().push_deliveries
    notifications = create_repository_catalog().notifications
    with migrated_clone.database.transaction() as tx:
        body = b"synthetic-restart-payload"
        entry = notifications.create_entry(
            tx,
            owner_id=OWNER,
            event_kind="completion",
            source_id="restart-source",
            deduplication_key="restart-entry",
            content_format="astral.notification/v1",
            content_ciphertext=body,
            content_sha256=hashlib.sha256(body).hexdigest(),
            created_at=10,
            retention_expires_at=1000,
        )
        token_body, digest = token(b"synthetic-restart-token")
        destination = repo.register_destination(
            tx,
            owner_id=OWNER,
            destination_key="restart-device",
            token_format="astral.push-token/v1",
            token_ciphertext=token_body,
            token_sha256=digest,
            created_at=10,
        )
        acknowledged_delivery = repo.enqueue_delivery(
            tx,
            owner_id=OWNER,
            entry_id=entry.entry_id,
            destination_id=destination.destination_id,
            delivery_key="restart-delivery-ack",
            expected_destination_revision=1,
            expected_token_version=1,
            max_attempts=2,
            available_at=20,
            created_at=20,
        )
        expired_delivery = repo.enqueue_delivery(
            tx,
            owner_id=OWNER,
            entry_id=entry.entry_id,
            destination_id=destination.destination_id,
            delivery_key="restart-delivery-expired",
            expected_destination_revision=1,
            expected_token_version=1,
            max_attempts=1,
            available_at=25,
            created_at=25,
        )
    with migrated_clone.database.transaction() as tx:
        first = repo.claim_deliveries(
            tx,
            owner_id=OWNER,
            worker_id="worker-before-restart",
            now=30,
            lease_expires_at=40,
            limit=1,
        )[0]
        assert first.delivery.delivery_id == acknowledged_delivery.delivery_id
        first_claim_id = first.delivery.claim_id
    with migrated_clone.database.transaction() as tx:
        persisted = repo.get_delivery(
            tx, owner_id=OWNER, delivery_id=acknowledged_delivery.delivery_id
        )
        assert persisted.status == "claimed"
        assert persisted.claim_id == first_claim_id
        late = repo.acknowledge_delivery(
            tx,
            owner_id=OWNER,
            delivery_id=acknowledged_delivery.delivery_id,
            claim_id=first_claim_id,
            acknowledged_at=41,
        )
        assert late.status == "delivered"
    with migrated_clone.database.transaction() as tx:
        expired_claim = repo.claim_deliveries(
            tx,
            owner_id=OWNER,
            worker_id="worker-after-restart",
            now=50,
            lease_expires_at=60,
            limit=1,
        )[0]
        assert expired_claim.delivery.delivery_id == expired_delivery.delivery_id
    with migrated_clone.database.transaction() as tx:
        assert (
            repo.claim_deliveries(
                tx,
                owner_id=OWNER,
                worker_id="worker-after-expiry",
                now=61,
                lease_expires_at=71,
                limit=1,
            )
            == ()
        )
        dead = repo.get_delivery(tx, owner_id=OWNER, delivery_id=expired_delivery.delivery_id)
        assert dead.status == "dead_letter"
        assert dead.last_error_code == "lease_expired"


def test_late_acknowledgment_survives_destination_deletion(repo, tx):
    entry = make_entry(tx)
    destination = make_destination(tx, repo)
    delivery = enqueue(tx, repo, entry, destination)
    claimed = repo.claim_deliveries(
        tx,
        owner_id=OWNER,
        worker_id="worker",
        now=30,
        lease_expires_at=60,
        limit=1,
    )[0]
    deleted = repo.delete_destination(
        tx,
        owner_id=OWNER,
        destination_id=destination.destination_id,
        expected_revision=1,
        deleted_at=31,
    )
    late = repo.acknowledge_delivery(
        tx,
        owner_id=OWNER,
        delivery_id=delivery.delivery_id,
        claim_id=claimed.delivery.claim_id,
        acknowledged_at=32,
    )
    assert late.status == "delivered"
    assert (
        repo.get_destination(
            tx,
            owner_id=OWNER,
            destination_id=destination.destination_id,
            include_deleted=True,
        )
        == deleted
    )
    assert deleted.token_ciphertext is None
