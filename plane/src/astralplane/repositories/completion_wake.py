"""Owner-scoped completion subscriptions and deduplicated wake receipts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _required_id,
)

TERMINALS: Final = ("completed", "failed", "cancelled")
CONDITIONS: Final = (*TERMINALS, "any_terminal")


def _required_owner(value: object, field: str = "owner_id") -> str:
    return _required_id(value, field, maximum=512)


def _required_uuid(value: object, field: str) -> str:
    text = _required_id(value, field, maximum=36)
    try:
        return str(uuid.UUID(text))
    except ValueError as exc:
        raise RepositoryValidationError(f"{field} must be a UUID string") from exc


def _required_revision(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepositoryValidationError(f"{field} must be an integer revision")
    if not 1 <= value <= 9007199254740991:
        raise RepositoryValidationError(f"{field} out of range")
    return value


def _required_millis(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepositoryValidationError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class CompletionSubscription:
    subscription_id: str
    owner_id: str
    waiter_operation_id: str
    waiter_owner_id: str
    source_operation_id: str
    source_owner_id: str
    terminal_condition: str
    source_revision: int
    current_revision_fence: int
    created_at: int
    revoked_at: int | None


@dataclass(frozen=True)
class WakeReceipt:
    receipt_id: str
    subscription_id: str
    owner_id: str
    idempotency_key: str
    observed_terminal: str
    observed_revision: int
    accepted_at: int
    replay_of: str | None


class CompletionWakeRepository:
    def register_subscription(self, tx: Any, **kwargs: Any) -> CompletionSubscription:
        return register_subscription(tx, **kwargs)

    def revoke_subscription(self, tx: Any, **kwargs: Any) -> CompletionSubscription:
        return revoke_subscription(tx, **kwargs)

    def delete_subscription(self, tx: Any, **kwargs: Any) -> None:
        delete_subscription(tx, **kwargs)

    def accept_wake_receipt(self, tx: Any, **kwargs: Any) -> WakeReceipt:
        return accept_wake_receipt(tx, **kwargs)

    def replay_wake_receipt(self, tx: Any, **kwargs: Any) -> WakeReceipt:
        return replay_wake_receipt(tx, **kwargs)


def register_subscription(
    tx: Any,
    *,
    owner_id: str,
    waiter_operation_id: str,
    waiter_owner_id: str,
    source_operation_id: str,
    source_owner_id: str,
    terminal_condition: str,
    source_revision: int,
    current_revision_fence: int,
    created_at: int,
    subscription_id: str | None = None,
) -> CompletionSubscription:
    owner = _required_owner(owner_id)
    waiter_op = _required_uuid(waiter_operation_id, "waiter_operation_id")
    waiter_owner = _required_owner(waiter_owner_id, "waiter_owner_id")
    source_op = _required_uuid(source_operation_id, "source_operation_id")
    source_owner = _required_owner(source_owner_id, "source_owner_id")
    if terminal_condition not in CONDITIONS:
        raise RepositoryValidationError("terminal_condition must be a known condition")
    source_rev = _required_revision(source_revision, "source_revision")
    fence = _required_revision(current_revision_fence, "current_revision_fence")
    if source_rev > fence:
        raise RepositoryValidationError("source_revision must not exceed its fence")
    if waiter_op == source_op and waiter_owner == source_owner:
        raise RepositoryValidationError("a waiter cannot subscribe to itself")
    created = _required_millis(created_at, "created_at")
    sub_id = (
        str(uuid.uuid4())
        if subscription_id is None
        else _required_uuid(subscription_id, "subscription_id")
    )
    try:
        tx.execute(
            "INSERT INTO completion_subscription (subscription_id, owner_id,"
            " waiter_operation_id, waiter_owner_id, source_operation_id,"
            " source_owner_id, terminal_condition, source_revision,"
            " current_revision_fence, created_at) VALUES"
            " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                sub_id,
                owner,
                waiter_op,
                waiter_owner,
                source_op,
                source_owner,
                terminal_condition,
                source_rev,
                fence,
                created,
            ),
        )
    except Exception as exc:
        raise RepositoryConflictError("completion subscription already exists") from exc
    return CompletionSubscription(
        sub_id,
        owner,
        waiter_op,
        waiter_owner,
        source_op,
        source_owner,
        terminal_condition,
        source_rev,
        fence,
        created,
        None,
    )


def revoke_subscription(
    tx: Any, *, owner_id: str, subscription_id: str, revoked_at: int
) -> CompletionSubscription:
    owner = _required_owner(owner_id)
    sub_id = _required_uuid(subscription_id, "subscription_id")
    at = _required_millis(revoked_at, "revoked_at")
    row = tx.fetch_one(
        "SELECT * FROM completion_subscription WHERE subscription_id=%s AND owner_id=%s",
        (sub_id, owner),
    )
    if row is None:
        raise RepositoryNotFoundError("completion subscription not found")
    if row["revoked_at"] is not None:
        raise RepositoryConflictError("completion subscription already revoked")
    if at < row["created_at"]:
        raise RepositoryDataError("revoked_at precedes created_at")
    result = tx.execute(
        "UPDATE completion_subscription SET revoked_at=%s"
        " WHERE subscription_id=%s AND owner_id=%s AND revoked_at IS NULL",
        (at, sub_id, owner),
    )
    if getattr(result, "rowcount", 1) != 1:
        raise RepositoryConflictError("completion subscription already revoked")
    return CompletionSubscription(**{**dict(row), "revoked_at": at})


def delete_subscription(tx: Any, *, owner_id: str, subscription_id: str) -> None:
    owner = _required_owner(owner_id)
    sub_id = _required_uuid(subscription_id, "subscription_id")
    row = tx.fetch_one(
        "SELECT owner_id FROM completion_subscription WHERE subscription_id=%s AND owner_id=%s",
        (sub_id, owner),
    )
    if row is None:
        raise RepositoryNotFoundError("completion subscription not found")
    result = tx.execute(
        "DELETE FROM completion_subscription WHERE subscription_id=%s AND owner_id=%s",
        (sub_id, owner),
    )
    if getattr(result, "rowcount", 1) != 1:
        raise RepositoryNotFoundError("completion subscription not found")


def _covers(condition: str, observed: str) -> bool:
    return condition == "any_terminal" or condition == observed


def accept_wake_receipt(
    tx: Any,
    *,
    owner_id: str,
    subscription_id: str,
    idempotency_key: str,
    observed_terminal: str,
    observed_revision: int,
    accepted_at: int,
    receipt_id: str | None = None,
) -> WakeReceipt:
    owner = _required_owner(owner_id)
    sub_id = _required_uuid(subscription_id, "subscription_id")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 512:
        raise RepositoryValidationError("idempotency_key must be 1..512 chars")
    if observed_terminal not in TERMINALS:
        raise RepositoryValidationError("observed_terminal must be a terminal value")
    revision = _required_revision(observed_revision, "observed_revision")
    accepted = _required_millis(accepted_at, "accepted_at")
    sub = tx.fetch_one(
        "SELECT * FROM completion_subscription WHERE subscription_id=%s AND owner_id=%s"
        " FOR UPDATE",
        (sub_id, owner),
    )
    if sub is None:
        raise RepositoryNotFoundError("completion subscription not found")
    if sub["revoked_at"] is not None:
        raise RepositoryConflictError("subscription is revoked")
    if not _covers(sub["terminal_condition"], observed_terminal):
        raise RepositoryConflictError("observed terminal not covered by subscription")
    if not sub["source_revision"] <= revision <= sub["current_revision_fence"]:
        raise RepositoryConflictError("observed revision outside subscription fence")
    existing = tx.fetch_one(
        "SELECT * FROM wake_receipt WHERE subscription_id=%s AND idempotency_key=%s"
        " AND owner_id=%s",
        (sub_id, idempotency_key, owner),
    )
    if existing is not None:
        return WakeReceipt(**dict(existing))
    rid = str(uuid.uuid4()) if receipt_id is None else _required_uuid(receipt_id, "receipt_id")
    result = tx.execute(
        "INSERT INTO wake_receipt (receipt_id, subscription_id, owner_id,"
        " idempotency_key, observed_terminal, observed_revision, accepted_at)"
        " SELECT %s,%s,%s,%s,%s,%s,%s WHERE EXISTS (SELECT 1"
        " FROM completion_subscription WHERE subscription_id=%s"
        " AND owner_id=%s AND revoked_at IS NULL"
        " AND (terminal_condition='any_terminal' OR terminal_condition=%s)"
        " AND source_revision<=%s AND %s<=current_revision_fence)"
        " ON CONFLICT (subscription_id, idempotency_key) DO NOTHING",
        (
            rid,
            sub_id,
            owner,
            idempotency_key,
            observed_terminal,
            revision,
            accepted,
            sub_id,
            owner,
            observed_terminal,
            revision,
            revision,
        ),
    )
    if getattr(result, "rowcount", 1) != 1:
        raced = tx.fetch_one(
            "SELECT * FROM wake_receipt WHERE subscription_id=%s AND idempotency_key=%s"
            " AND owner_id=%s",
            (sub_id, idempotency_key, owner),
        )
        if raced is not None:
            return WakeReceipt(**dict(raced))
        raise RepositoryConflictError("subscription revoked or fence moved during acceptance")
    return WakeReceipt(
        rid, sub_id, owner, idempotency_key, observed_terminal, revision, accepted, None
    )


def replay_wake_receipt(
    tx: Any, *, owner_id: str, receipt_id: str, idempotency_key: str, accepted_at: int
) -> WakeReceipt:
    owner = _required_owner(owner_id)
    original_id = _required_uuid(receipt_id, "receipt_id")
    original = tx.fetch_one(
        "SELECT * FROM wake_receipt WHERE receipt_id=%s AND owner_id=%s",
        (original_id, owner),
    )
    if original is None:
        raise RepositoryNotFoundError("wake receipt not found")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 512:
        raise RepositoryValidationError("idempotency_key must be 1..512 chars")
    accepted = _required_millis(accepted_at, "accepted_at")
    existing = tx.fetch_one(
        "SELECT * FROM wake_receipt WHERE subscription_id=%s AND idempotency_key=%s"
        " AND owner_id=%s",
        (original["subscription_id"], idempotency_key, owner),
    )
    if existing is not None:
        return WakeReceipt(**dict(existing))
    rid = str(uuid.uuid4())
    result = tx.execute(
        "INSERT INTO wake_receipt (receipt_id, subscription_id, owner_id,"
        " idempotency_key, observed_terminal, observed_revision, accepted_at,"
        " replay_of) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (subscription_id, idempotency_key) DO NOTHING",
        (
            rid,
            original["subscription_id"],
            owner,
            idempotency_key,
            original["observed_terminal"],
            original["observed_revision"],
            accepted,
            original_id,
        ),
    )
    if getattr(result, "rowcount", 1) != 1:
        raced = tx.fetch_one(
            "SELECT * FROM wake_receipt WHERE subscription_id=%s AND idempotency_key=%s"
            " AND owner_id=%s",
            (original["subscription_id"], idempotency_key, owner),
        )
        if raced is not None:
            return WakeReceipt(**dict(raced))
        raise RepositoryConflictError("wake receipt replay already recorded")
    return WakeReceipt(
        rid,
        original["subscription_id"],
        owner,
        idempotency_key,
        original["observed_terminal"],
        original["observed_revision"],
        accepted,
        original_id,
    )
