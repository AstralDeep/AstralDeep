"""Tests for src/astralplane/contracts: the neutral durable-outbox Protocol exposes a
complete, driver-independent lease and failure lifecycle.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from typing import get_type_hints

import pytest

from astralplane.contracts import (
    ClaimedOutboxEntry,
    CommandResultContract,
    OutboxEntry,
    OutboxStore,
    ReclaimedOutboxEntry,
)


def test_outbox_neutral_records_are_frozen_and_driver_independent() -> None:
    available_at = datetime(2026, 8, 13, tzinfo=UTC)
    entry = OutboxEntry(
        entry_id="entry-1",
        topic="audit.publish",
        canonical_payload=b'{"event":"created"}',
        payload_sha256="a" * 64,
        idempotency_key="operation-1",
        available_at=available_at,
    )
    claimed = ClaimedOutboxEntry(
        entry=entry,
        worker_id="worker-1",
        lease_expires_at=available_at + timedelta(seconds=30),
        expected_version=2,
        attempt=1,
    )
    reclaimed = ReclaimedOutboxEntry(
        entry_id=entry.entry_id,
        previous_worker_id=claimed.worker_id,
        expected_version=3,
        available_at=available_at + timedelta(seconds=31),
    )

    assert entry.canonical_payload == b'{"event":"created"}'
    assert claimed.entry is entry
    assert reclaimed.previous_worker_id == "worker-1"
    with pytest.raises(FrozenInstanceError):
        entry.topic = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        claimed.attempt = 2  # type: ignore[misc]
    assert tuple(field.name for field in fields(OutboxEntry)) == (
        "entry_id",
        "topic",
        "canonical_payload",
        "payload_sha256",
        "idempotency_key",
        "available_at",
    )


def test_outbox_store_exposes_the_complete_lease_and_failure_lifecycle() -> None:
    expected_parameters = {
        "enqueue": ("self", "transaction", "entry"),
        "claim": (
            "self",
            "transaction",
            "worker_id",
            "topics",
            "now",
            "lease_duration",
            "limit",
        ),
        "ack": (
            "self",
            "transaction",
            "entry_id",
            "worker_id",
            "expected_version",
            "now",
        ),
        "retry": (
            "self",
            "transaction",
            "entry_id",
            "worker_id",
            "expected_version",
            "available_at",
            "error_code",
            "now",
        ),
        "dead_letter": (
            "self",
            "transaction",
            "entry_id",
            "worker_id",
            "expected_version",
            "error_code",
            "now",
        ),
        "reclaim_expired": ("self", "transaction", "now", "limit"),
    }

    for method_name, parameter_names in expected_parameters.items():
        method = getattr(OutboxStore, method_name)
        assert tuple(inspect.signature(method).parameters) == parameter_names

    assert not hasattr(OutboxStore, "acknowledge")


def test_outbox_contract_return_types_are_neutral_detached_values() -> None:
    enqueue_hints = get_type_hints(OutboxStore.enqueue)
    claim_hints = get_type_hints(OutboxStore.claim)
    ack_hints = get_type_hints(OutboxStore.ack)
    retry_hints = get_type_hints(OutboxStore.retry)
    dead_hints = get_type_hints(OutboxStore.dead_letter)
    reclaim_hints = get_type_hints(OutboxStore.reclaim_expired)

    assert enqueue_hints["entry"] is OutboxEntry
    assert enqueue_hints["return"] is CommandResultContract
    assert claim_hints["return"] == tuple[ClaimedOutboxEntry, ...]
    assert ack_hints["return"] is CommandResultContract
    assert retry_hints["return"] is CommandResultContract
    assert dead_hints["return"] is CommandResultContract
    assert reclaim_hints["return"] == tuple[ReclaimedOutboxEntry, ...]


class CompleteOutbox:
    def enqueue(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    def claim(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    def ack(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    def retry(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    def dead_letter(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError

    def reclaim_expired(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError


class IncompleteOutbox(CompleteOutbox):
    retry = None


def test_runtime_protocol_requires_every_outbox_lifecycle_method() -> None:
    assert isinstance(CompleteOutbox(), OutboxStore)
    assert not isinstance(IncompleteOutbox(), OutboxStore)
