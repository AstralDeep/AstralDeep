"""Owner-scoped durable stop epochs serialize admission, transitions and peer
receipts with caller-owned row locks. The host retains authority checks and
appends audit events in the same transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from astralplane.contracts import Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryValidationError,
    _bounded_text,
    _required_id,
)

_MAX_INT = 2**63 - 1
_FIELDS = "owner_id, epoch, revision, engaged, engaged_at, engaged_by, reason, updated_at"
_ACK_FIELDS = "owner_id, epoch, mesh_id, peer_id, receipt_digest, acknowledged_at"


class StopEpochConflictError(RepositoryConflictError):
    default_code = "stop_epoch_conflict"


class OwnerStoppedError(RepositoryConflictError):
    default_code = "owner_stopped"


@dataclass(frozen=True, slots=True)
class OwnerStopRecord:
    owner_id: str
    epoch: int
    revision: int
    engaged: bool
    engaged_at: datetime | None
    engaged_by: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class PeerStopAcknowledgment:
    owner_id: str
    epoch: int
    mesh_id: str
    peer_id: str
    receipt_digest: str
    acknowledged_at: datetime


def _integer(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_INT:
        raise RepositoryValidationError(f"{field} must be a bounded non-negative integer")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise RepositoryValidationError("at must be a timezone-aware UTC datetime")
    return value.astimezone(UTC)


def _operation_id(value: object) -> str:
    if not isinstance(value, UUID):
        raise RepositoryValidationError("operation_id must be a UUID")
    return str(value)


def _record(row) -> OwnerStopRecord:
    return OwnerStopRecord(
        row["owner_id"],
        row["epoch"],
        row["revision"],
        row["engaged"],
        None if row["engaged_at"] is None else row["engaged_at"].astimezone(UTC),
        row["engaged_by"],
        row["reason"],
    )


def _ack(row) -> PeerStopAcknowledgment:
    return PeerStopAcknowledgment(
        row["owner_id"],
        row["epoch"],
        row["mesh_id"],
        row["peer_id"],
        row["receipt_digest"],
        row["acknowledged_at"].astimezone(UTC),
    )


def _fence(row, revision: int) -> None:
    if row["revision"] != revision:
        raise StopEpochConflictError("owner stop revision changed")


def _transition_time(row, at: datetime) -> None:
    if row["updated_at"] is not None and at < row["updated_at"]:
        raise StopEpochConflictError("owner stop transition time regressed")


class StopEpochRepository:
    def _locked(self, transaction: Transaction, owner: str):
        transaction.execute(
            "INSERT INTO owner_stop_epoch (owner_id) VALUES (%s) ON CONFLICT (owner_id) DO NOTHING",
            (owner,),
        )
        return transaction.fetch_one(
            f"SELECT {_FIELDS} FROM owner_stop_epoch WHERE owner_id = %s FOR UPDATE",
            (owner,),
        )

    def get(
        self, transaction: Transaction, *, owner_id: str, for_update: bool = False
    ) -> OwnerStopRecord | None:
        owner = _required_id(owner_id, "owner_id")
        if type(for_update) is not bool:
            raise RepositoryValidationError("for_update must be a boolean")
        row = (
            self._locked(transaction, owner)
            if for_update
            else transaction.fetch_one(
                f"SELECT {_FIELDS} FROM owner_stop_epoch WHERE owner_id = %s", (owner,)
            )
        )
        return None if row is None or row["revision"] == 0 else _record(row)

    def engage(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        expected_revision: int,
        reason: str,
        actor_id: str,
        at: datetime,
    ) -> OwnerStopRecord:
        owner = _required_id(owner_id, "owner_id")
        revision = _integer(expected_revision, "expected_revision")
        actor = _required_id(actor_id, "actor_id")
        reason = _bounded_text(reason, "reason", maximum=280, allow_empty=True)
        at = _time(at)
        row = self._locked(transaction, owner)
        _fence(row, revision)
        if row["engaged"]:
            if (row["reason"], row["engaged_by"], row["engaged_at"]) != (reason, actor, at):
                raise StopEpochConflictError("engaged stop replay changed immutable semantics")
            return _record(row)
        _transition_time(row, at)
        if row["epoch"] == _MAX_INT or revision == _MAX_INT:
            raise StopEpochConflictError("owner stop counters exhausted")
        changed = transaction.fetch_one(
            f"UPDATE owner_stop_epoch SET epoch = epoch + 1, revision = revision + 1, "
            "engaged = TRUE, engaged_at = %s, engaged_by = %s, reason = %s, updated_at = %s "
            f"WHERE owner_id = %s AND revision = %s AND NOT engaged RETURNING {_FIELDS}",
            (at, actor, reason, at, owner, revision),
        )
        if changed is None:
            raise StopEpochConflictError("owner stop engage fence rejected")
        return _record(changed)

    def resume(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        expected_revision: int,
        expected_epoch: int,
        at: datetime,
    ) -> OwnerStopRecord:
        owner = _required_id(owner_id, "owner_id")
        revision = _integer(expected_revision, "expected_revision")
        epoch = _integer(expected_epoch, "expected_epoch")
        at = _time(at)
        row = self._locked(transaction, owner)
        _fence(row, revision)
        if not row["engaged"] or row["epoch"] != epoch:
            raise StopEpochConflictError("owner stop epoch or state changed")
        _transition_time(row, at)
        if revision == _MAX_INT:
            raise StopEpochConflictError("owner stop counters exhausted")
        changed = transaction.fetch_one(
            "UPDATE owner_stop_epoch SET revision = revision + 1, engaged = FALSE, "
            f"updated_at = %s WHERE owner_id = %s AND revision = %s AND epoch = %s "
            f"AND engaged RETURNING {_FIELDS}",
            (at, owner, revision, epoch),
        )
        if changed is None:
            raise StopEpochConflictError("owner stop resume fence rejected")
        return _record(changed)

    def acknowledge(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        peer_id: str,
        epoch: int,
        expected_revision: int,
        receipt_digest: str,
        at: datetime,
    ) -> PeerStopAcknowledgment:
        owner = _required_id(owner_id, "owner_id")
        mesh = _required_id(mesh_id, "mesh_id", maximum=64)
        peer = _required_id(peer_id, "peer_id", maximum=64)
        epoch = _integer(epoch, "epoch")
        revision = _integer(expected_revision, "expected_revision")
        digest = _required_id(receipt_digest, "receipt_digest", maximum=64)
        if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            raise RepositoryValidationError("receipt_digest must be a lowercase SHA-256 digest")
        at = _time(at)
        row = self._locked(transaction, owner)
        _fence(row, revision)
        if not row["engaged"] or row["epoch"] != epoch:
            raise StopEpochConflictError("acknowledgment belongs to an inactive stop epoch")
        _transition_time(row, at)
        with transaction.savepoint("stop_acknowledgment"):
            existing = transaction.fetch_one(
                f"SELECT {_ACK_FIELDS} FROM peer_stop_acknowledgment WHERE owner_id = %s "
                "AND epoch = %s AND mesh_id = %s AND peer_id = %s",
                (owner, epoch, mesh, peer),
            )
            if existing is not None:
                if existing["receipt_digest"] != digest:
                    raise StopEpochConflictError("acknowledgment replay changed immutable receipt")
                return _ack(existing)
            if revision == _MAX_INT:
                raise StopEpochConflictError("owner stop counters exhausted")
            if len(self.list_acknowledgments(transaction, owner_id=owner, epoch=epoch)) >= 64:
                raise StopEpochConflictError("stop acknowledgment inventory is full")
            inserted = transaction.fetch_one(
                "INSERT INTO peer_stop_acknowledgment "
                "(owner_id, epoch, mesh_id, peer_id, receipt_digest, acknowledged_at) "
                "SELECT %s, %s, %s, %s, %s, %s WHERE EXISTS "
                "(SELECT 1 FROM owner_stop_epoch WHERE owner_id = %s AND revision = %s "
                f"AND epoch = %s AND engaged) RETURNING {_ACK_FIELDS}",
                (owner, epoch, mesh, peer, digest, at, owner, revision, epoch),
            )
            changed = transaction.fetch_one(
                "UPDATE owner_stop_epoch SET revision = revision + 1, updated_at = %s "
                "WHERE owner_id = %s AND revision = %s AND epoch = %s AND engaged "
                "RETURNING revision",
                (at, owner, revision, epoch),
            )
            if inserted is None or changed is None:
                raise StopEpochConflictError("stop acknowledgment fence rejected")
            return _ack(inserted)

    def list_acknowledgments(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        epoch: int,
    ) -> tuple[PeerStopAcknowledgment, ...]:
        owner = _required_id(owner_id, "owner_id")
        epoch = _integer(epoch, "epoch")
        rows = transaction.fetch_all(
            f"SELECT {_ACK_FIELDS} FROM peer_stop_acknowledgment WHERE owner_id = %s "
            "AND epoch = %s ORDER BY mesh_id, peer_id LIMIT 65",
            (owner, epoch),
        )
        if len(rows) > 64:
            raise StopEpochConflictError("stop acknowledgment inventory exceeds its bound")
        return tuple(_ack(row) for row in rows)

    def assert_running(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        expected_epoch: int | None = None,
    ) -> OwnerStopRecord | None:
        owner = _required_id(owner_id, "owner_id")
        epoch = None if expected_epoch is None else _integer(expected_epoch, "expected_epoch")
        row = self._locked(transaction, owner)
        if row["engaged"]:
            raise OwnerStoppedError("owner stop is engaged")
        if epoch is not None and row["epoch"] != epoch:
            raise StopEpochConflictError("owner stop epoch changed before admission")
        return None if row["revision"] == 0 else _record(row)

    def bind_operation(
        self, transaction: Transaction, *, owner_id: str, operation_id: UUID
    ) -> int:
        owner = _required_id(owner_id, "owner_id")
        operation = _operation_id(operation_id)
        row = self._locked(transaction, owner)
        if row["engaged"]:
            raise OwnerStoppedError("owner stop is engaged")
        existing = transaction.fetch_one(
            "SELECT epoch FROM owner_stop_operation_epoch "
            "WHERE owner_id = %s AND operation_id = %s",
            (owner, operation),
        )
        if existing is not None:
            if existing["epoch"] != row["epoch"]:
                raise StopEpochConflictError("operation stop binding belongs to an earlier epoch")
            return existing["epoch"]
        inserted = transaction.fetch_one(
            "INSERT INTO owner_stop_operation_epoch (owner_id, operation_id, epoch) "
            "SELECT %s, %s, %s WHERE EXISTS "
            "(SELECT 1 FROM owner_stop_epoch WHERE owner_id = %s AND epoch = %s "
            "AND NOT engaged) RETURNING epoch",
            (owner, operation, row["epoch"], owner, row["epoch"]),
        )
        if inserted is None:
            raise StopEpochConflictError("operation stop binding fence rejected")
        return inserted["epoch"]

    def assert_operation(
        self, transaction: Transaction, *, owner_id: str, operation_id: UUID
    ) -> None:
        owner = _required_id(owner_id, "owner_id")
        operation = _operation_id(operation_id)
        row = self._locked(transaction, owner)
        if row["engaged"]:
            raise OwnerStoppedError("owner stop is engaged")
        binding = transaction.fetch_one(
            "SELECT epoch FROM owner_stop_operation_epoch "
            "WHERE owner_id = %s AND operation_id = %s",
            (owner, operation),
        )
        if (binding is None and row["epoch"] != 0) or (
            binding is not None and binding["epoch"] != row["epoch"]
        ):
            raise StopEpochConflictError("operation stop epoch changed before execution")
