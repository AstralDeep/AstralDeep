"""Completion-wake fence, ownership, and facade checks."""

from __future__ import annotations

import uuid

import pytest

from astralplane.database import migrations as canonical
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
)
from astralplane.repositories.completion_wake import CompletionWakeRepository
from tests.repositories._support import Result, ScriptedTransaction

REAL_089_001_REGISTRY_DIGEST = "35741bd0de148f836cd8b75b160531013836a61bd46b9e17e7790641412979d8"
STALE_089_001_REGISTRY_DIGEST = "ae3b77d9067dec56503f738216faa4c1bc2ed9523c57f7ce3957830846ec43b0"


def test_089_001_pins_match_upstream_main() -> None:
    assert canonical.PLANE_SCHEMA_089_001_REGISTRY_DIGEST == REAL_089_001_REGISTRY_DIGEST
    assert canonical.PLANE_SCHEMA_089_001_SCHEMA_VERIFIER_CHECKSUM == (
        "7123aabb64906d6bb6875921f597f88cbcba5df9393c57786afe830044280016"
    )
    assert canonical.PLANE_SCHEMA_089_001_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM == (
        "bff0d85f218b82544c953134e59524552745032890cf5de0ad3814951aeef4f8"
    )


def test_089_001_admission_recognizes_main_bytes() -> None:
    assert (
        canonical.CURRENT_DATA_PLANE_REVISION.predecessor_digest_for("089.001")
        == REAL_089_001_REGISTRY_DIGEST
    )


def _live_sub() -> dict[str, object]:
    return {
        "subscription_id": str(uuid.uuid4()),
        "owner_id": "owner-1",
        "terminal_condition": "any_terminal",
        "source_revision": 3,
        "current_revision_fence": 9,
        "created_at": 100,
        "revoked_at": None,
    }


def test_accept_loses_race_to_revoke() -> None:
    repo = CompletionWakeRepository()
    tx = ScriptedTransaction(
        one=[_live_sub(), None, None],
        execute=[Result(rowcount=0)],
    )
    with pytest.raises(RepositoryConflictError, match="revoked or fence moved"):
        repo.accept_wake_receipt(
            tx,
            owner_id="owner-1",
            subscription_id=str(uuid.uuid4()),
            idempotency_key="k-1",
            observed_terminal="completed",
            observed_revision=5,
            accepted_at=200,
        )
    kinds = [c[0] for c in tx.calls]
    assert kinds[0] == "one"
    writes = [c[1] for c in tx.calls if c[0] == "execute"]
    assert "SELECT 1" in writes[-1]
    assert "revoked_at IS NULL" in writes[-1]
    assert "ON CONFLICT" in writes[-1]


def test_accept_happy_path_inserts_fenced() -> None:
    repo = CompletionWakeRepository()
    tx = ScriptedTransaction(
        one=[_live_sub(), None],
        execute=[Result(rowcount=1)],
    )
    receipt = repo.accept_wake_receipt(
        tx,
        owner_id="owner-1",
        subscription_id=str(uuid.uuid4()),
        idempotency_key="k-1",
        observed_terminal="completed",
        observed_revision=5,
        accepted_at=200,
    )
    assert receipt.owner_id == "owner-1"


def test_accept_duplicate_race_returns_winner() -> None:
    repo = CompletionWakeRepository()
    winner = {
        "receipt_id": str(uuid.uuid4()),
        "subscription_id": str(uuid.uuid4()),
        "owner_id": "owner-1",
        "idempotency_key": "k-1",
        "observed_terminal": "completed",
        "observed_revision": 5,
        "accepted_at": 200,
        "replay_of": None,
    }
    tx = ScriptedTransaction(
        one=[_live_sub(), None, winner],
        execute=[Result(rowcount=0)],
    )
    receipt = repo.accept_wake_receipt(
        tx,
        owner_id="owner-1",
        subscription_id=winner["subscription_id"],
        idempotency_key="k-1",
        observed_terminal="completed",
        observed_revision=5,
        accepted_at=201,
    )
    assert receipt.receipt_id == winner["receipt_id"]
    assert "AND owner_id=" in tx.calls[-1][1]


def test_revoke_loses_race_to_concurrent_revoke() -> None:
    repo = CompletionWakeRepository()
    tx = ScriptedTransaction(
        one=[{**_live_sub(), "revoked_at": None}],
        execute=[Result(rowcount=0)],
    )
    with pytest.raises(RepositoryConflictError, match="already revoked"):
        repo.revoke_subscription(
            tx,
            owner_id="owner-1",
            subscription_id=str(uuid.uuid4()),
            revoked_at=300,
        )
    assert "AND owner_id=" in tx.calls[-1][1]
    assert "revoked_at IS NULL" in tx.calls[-1][1]


def test_delete_is_owner_fenced() -> None:
    repo = CompletionWakeRepository()
    tx = ScriptedTransaction(
        one=[{"owner_id": "owner-1"}],
        execute=[Result(rowcount=1)],
    )
    repo.delete_subscription(tx, owner_id="owner-1", subscription_id=str(uuid.uuid4()))
    assert "AND owner_id=" in tx.calls[-1][1]


def test_delete_missing_row_reports_not_found() -> None:
    repo = CompletionWakeRepository()
    tx = ScriptedTransaction(
        one=[{"owner_id": "owner-1"}],
        execute=[Result(rowcount=0)],
    )
    with pytest.raises(RepositoryNotFoundError):
        repo.delete_subscription(tx, owner_id="owner-1", subscription_id=str(uuid.uuid4()))


def test_facade_exposes_completion_wake() -> None:
    from astralplane.api import create_repository_catalog

    catalog = create_repository_catalog()
    assert isinstance(catalog.completion_wake, CompletionWakeRepository)
    assert isinstance(catalog.as_mapping()["completion_wake"], CompletionWakeRepository)
