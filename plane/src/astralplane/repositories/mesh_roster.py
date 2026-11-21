"""Owner-isolated store for a mesh's roster entries and countersigned checkpoints. The
first roster entry's hash is the mesh id, later entries must follow entries already held,
and checkpoints can be compared with the feed heads the event log holds. Signatures are
verified by the host before anything reaches this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.database.dialect import dialect_of
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _required_id,
    _row_value,
)
from astralplane.repositories._mesh_values import (
    bounded_integer,
    byte_string,
    hash_reference,
    hash_value,
    returned_row,
    stored_bytes,
    stored_integer,
    stored_json,
)

_MAX_SIGNED: Final = 65536
_MAX_SIGNATURE: Final = 1024
_MAX_FOLLOWS: Final = 64
_MAX_PAGE: Final = 1000

_ENTRY_FIELDS: Final = "owner_id, mesh_id, entry_id, kind, follows, signed_bytes, recorded_at"
_CHECKPOINT_FIELDS: Final = (
    "owner_id, mesh_id, checkpoint_id, feed_id, seq, head_hash, signed_bytes, recorded_at"
)

_INSERT_MESH_SQL: Final = """
INSERT INTO event_mesh (owner_id, mesh_id, recorded_at) VALUES (%s, %s, %s)
ON CONFLICT (mesh_id) DO NOTHING
RETURNING owner_id
""".strip()

_MESH_OWNER_SQL: Final = "SELECT owner_id FROM event_mesh WHERE mesh_id = %s"

_INSERT_ENTRY_SQL: Final = f"""
INSERT INTO event_roster_entry (
    owner_id, mesh_id, entry_id, kind, follows, signed_bytes, recorded_at
)
SELECT mesh.owner_id, mesh.mesh_id, %s, %s, %s::jsonb, %s, %s
FROM event_mesh AS mesh
WHERE mesh.owner_id = %s AND mesh.mesh_id = %s
ON CONFLICT (owner_id, mesh_id, entry_id) DO NOTHING
RETURNING {_ENTRY_FIELDS}
""".strip()

_GET_ENTRY_SQL: Final = f"""
SELECT {_ENTRY_FIELDS} FROM event_roster_entry
WHERE owner_id = %s AND mesh_id = %s AND entry_id = %s
""".strip()

_HELD_ENTRIES_SQL: Final = """
SELECT entry_id FROM event_roster_entry
WHERE owner_id = %s AND mesh_id = %s AND entry_id = ANY(%s)
""".strip()

_LIST_ENTRIES_SQL: Final = f"""
SELECT {_ENTRY_FIELDS} FROM event_roster_entry
WHERE owner_id = %s AND mesh_id = %s
ORDER BY recorded_at ASC, entry_id ASC LIMIT %s
""".strip()

_TIPS_SQL: Final = f"""
SELECT {_ENTRY_FIELDS} FROM event_roster_entry AS entry
WHERE entry.owner_id = %s AND entry.mesh_id = %s
  AND NOT EXISTS (
      SELECT 1 FROM event_roster_entry AS follower
      WHERE follower.owner_id = entry.owner_id AND follower.mesh_id = entry.mesh_id
        AND follower.follows @> jsonb_build_array(entry.entry_id)
  )
ORDER BY entry.recorded_at ASC, entry.entry_id ASC
""".strip()

_INSERT_CHECKPOINT_SQL: Final = f"""
INSERT INTO event_checkpoint (
    owner_id, mesh_id, checkpoint_id, feed_id, seq, head_hash, signed_bytes, recorded_at
)
SELECT mesh.owner_id, mesh.mesh_id, %s, %s, %s, %s, %s, %s
FROM event_mesh AS mesh
WHERE mesh.owner_id = %s AND mesh.mesh_id = %s
ON CONFLICT (owner_id, mesh_id, checkpoint_id) DO NOTHING
RETURNING {_CHECKPOINT_FIELDS}
""".strip()

_GET_CHECKPOINT_SQL: Final = f"""
SELECT {_CHECKPOINT_FIELDS} FROM event_checkpoint
WHERE owner_id = %s AND mesh_id = %s AND checkpoint_id = %s
""".strip()

_LIST_CHECKPOINTS_SQL: Final = f"""
SELECT {_CHECKPOINT_FIELDS} FROM event_checkpoint
WHERE owner_id = %s AND mesh_id = %s AND (%s::text IS NULL OR feed_id = %s)
ORDER BY feed_id ASC, seq ASC, checkpoint_id ASC LIMIT %s
""".strip()

_INSERT_COUNTERSIGNATURE_SQL: Final = """
INSERT INTO event_checkpoint_countersignature (
    owner_id, mesh_id, checkpoint_id, signer_id, signature, recorded_at
)
SELECT checkpoint.owner_id, checkpoint.mesh_id, checkpoint.checkpoint_id, %s, %s, %s
FROM event_checkpoint AS checkpoint
WHERE checkpoint.owner_id = %s AND checkpoint.mesh_id = %s AND checkpoint.checkpoint_id = %s
ON CONFLICT (owner_id, mesh_id, checkpoint_id, signer_id) DO NOTHING
""".strip()

_COUNTERSIGNATURES_SQL: Final = """
SELECT owner_id, mesh_id, checkpoint_id, signer_id, signature, recorded_at
FROM event_checkpoint_countersignature
WHERE owner_id = %s AND mesh_id = %s AND checkpoint_id = ANY(%s)
ORDER BY checkpoint_id ASC, signer_id ASC
""".strip()

_BEHIND_SQL: Final = """
SELECT checkpoint.feed_id, checkpoint.checkpoint_id, checkpoint.seq AS checkpoint_seq,
       COALESCE(feed.head_seq, 0) AS held_seq
FROM event_checkpoint AS checkpoint
LEFT JOIN event_feed AS feed
  ON feed.owner_id = checkpoint.owner_id AND feed.mesh_id = checkpoint.mesh_id
 AND feed.feed_id = checkpoint.feed_id
WHERE checkpoint.owner_id = %s AND checkpoint.mesh_id = %s
  AND checkpoint.seq > COALESCE(feed.head_seq, 0)
  AND EXISTS (
      SELECT 1 FROM event_checkpoint_countersignature AS signature
      WHERE signature.owner_id = checkpoint.owner_id AND signature.mesh_id = checkpoint.mesh_id
        AND signature.checkpoint_id = checkpoint.checkpoint_id
  )
ORDER BY checkpoint.feed_id ASC, checkpoint.seq DESC, checkpoint.checkpoint_id ASC
""".strip()

_CONTRADICTIONS_SQL: Final = """
SELECT checkpoint.feed_id, checkpoint.checkpoint_id, checkpoint.seq AS checkpoint_seq,
       checkpoint.head_hash AS checkpoint_hash, entry.event_id AS held_event_id
FROM event_checkpoint AS checkpoint
JOIN event_entry AS entry
  ON entry.owner_id = checkpoint.owner_id AND entry.mesh_id = checkpoint.mesh_id
 AND entry.feed_id = checkpoint.feed_id AND entry.seq = checkpoint.seq
WHERE checkpoint.owner_id = %s AND checkpoint.mesh_id = %s
  AND entry.event_id <> checkpoint.head_hash
ORDER BY checkpoint.feed_id ASC, checkpoint.seq ASC, checkpoint.checkpoint_id ASC
""".strip()


@dataclass(frozen=True, slots=True)
class RosterEntryRecord:
    owner_id: str
    mesh_id: str
    entry_id: str
    kind: str
    follows: tuple[str, ...]
    signed_bytes: bytes
    recorded_at: int


@dataclass(frozen=True, slots=True)
class CountersignatureRecord:
    owner_id: str
    mesh_id: str
    checkpoint_id: str
    signer_id: str
    signature: bytes
    recorded_at: int


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    owner_id: str
    mesh_id: str
    checkpoint_id: str
    feed_id: str
    seq: int
    head_hash: str
    signed_bytes: bytes
    recorded_at: int
    countersignatures: tuple[CountersignatureRecord, ...]


@dataclass(frozen=True, slots=True)
class FeedBehindCheckpoint:
    feed_id: str
    checkpoint_id: str
    checkpoint_seq: int
    held_seq: int


@dataclass(frozen=True, slots=True)
class CheckpointContradiction:
    feed_id: str
    checkpoint_id: str
    checkpoint_seq: int
    checkpoint_hash: str
    held_event_id: str


def _entry(row: Mapping[str, Any]) -> RosterEntryRecord:
    follows = stored_json(row, "follows")
    if not isinstance(follows, tuple) or any(type(item) is not str for item in follows):
        raise RepositoryDataError(
            "persisted roster follows are malformed", metadata={"field": "follows"}
        )
    return RosterEntryRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        entry_id=str(_row_value(row, "entry_id")),
        kind=str(_row_value(row, "kind")),
        follows=follows,
        signed_bytes=stored_bytes(row, "signed_bytes"),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _countersignature(row: Mapping[str, Any]) -> CountersignatureRecord:
    return CountersignatureRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        checkpoint_id=str(_row_value(row, "checkpoint_id")),
        signer_id=str(_row_value(row, "signer_id")),
        signature=stored_bytes(row, "signature"),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _checkpoint(
    row: Mapping[str, Any], signatures: Sequence[CountersignatureRecord]
) -> CheckpointRecord:
    return CheckpointRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        checkpoint_id=str(_row_value(row, "checkpoint_id")),
        feed_id=str(_row_value(row, "feed_id")),
        seq=stored_integer(row, "seq"),
        head_hash=str(_row_value(row, "head_hash")),
        signed_bytes=stored_bytes(row, "signed_bytes"),
        recorded_at=stored_integer(row, "recorded_at"),
        countersignatures=tuple(signatures),
    )


def _owned(rows: tuple[Mapping[str, Any], ...], owner_id: str) -> None:
    if any(row.get("owner_id", owner_id) != owner_id for row in rows):
        raise RepositoryDataError("batched read crossed the owner boundary")


def _follows(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RepositoryValidationError("follows must be a list of entry ids")
    entries = tuple(hash_value(item, "follows") for item in value)
    if len(entries) > _MAX_FOLLOWS or len(set(entries)) != len(entries):
        raise RepositoryValidationError(
            "follows must hold distinct entry ids within the bound",
            metadata={"maximum": _MAX_FOLLOWS},
        )
    return tuple(sorted(entries))


class EventRosterRepository:
    def create_mesh(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        kind: str,
        signed_bytes: bytes,
        recorded_at: int,
    ) -> RosterEntryRecord:
        owner = _required_id(owner_id, "owner_id")
        kind_value = _bounded_text(kind, "kind", maximum=64)
        data = byte_string(signed_bytes, "signed_bytes", maximum=_MAX_SIGNED)
        recorded = bounded_integer(recorded_at, "recorded_at")
        mesh = hash_reference(data)
        created = transaction.execute(_INSERT_MESH_SQL, (owner, mesh, recorded))
        if returned_row(created, "mesh.create") is None:
            holder = transaction.fetch_one(_MESH_OWNER_SQL, (mesh,))
            if holder is None or holder.get("owner_id") != owner:
                raise RepositoryConflictError(
                    "mesh identity is already owned by another namespace",
                    metadata={"operation": "mesh.create"},
                )
        return self._store_entry(
            transaction,
            owner=owner,
            mesh=mesh,
            entry_id=mesh,
            kind=kind_value,
            follows=(),
            data=data,
            recorded=recorded,
        )

    def append_entry(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        kind: str,
        follows: Sequence[str],
        signed_bytes: bytes,
        recorded_at: int,
    ) -> RosterEntryRecord:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        kind_value = _bounded_text(kind, "kind", maximum=64)
        followed = _follows(follows)
        if not followed:
            raise RepositoryValidationError("a later roster entry must follow an earlier entry")
        data = byte_string(signed_bytes, "signed_bytes", maximum=_MAX_SIGNED)
        recorded = bounded_integer(recorded_at, "recorded_at")
        held = {
            str(row["entry_id"])
            for row in transaction.fetch_all(_HELD_ENTRIES_SQL, (owner, mesh, list(followed)))
        }
        if held != set(followed):
            raise RepositoryNotFoundError(
                "a followed roster entry is not recorded",
                metadata={"operation": "roster.append"},
            )
        return self._store_entry(
            transaction,
            owner=owner,
            mesh=mesh,
            entry_id=hash_reference(data),
            kind=kind_value,
            follows=followed,
            data=data,
            recorded=recorded,
        )

    def _store_entry(
        self,
        transaction: Transaction,
        *,
        owner: str,
        mesh: str,
        entry_id: str,
        kind: str,
        follows: tuple[str, ...],
        data: bytes,
        recorded: int,
    ) -> RosterEntryRecord:
        row = returned_row(
            transaction.execute(
                _INSERT_ENTRY_SQL,
                (
                    entry_id,
                    kind,
                    _canonical_json(list(follows), "follows"),
                    data,
                    recorded,
                    owner,
                    mesh,
                ),
            ),
            "roster.append",
        )
        if row is not None:
            return _entry(row)
        existing = self.get_entry(transaction, owner_id=owner, mesh_id=mesh, entry_id=entry_id)
        if existing is None:
            raise RepositoryNotFoundError(
                "mesh was not found", metadata={"operation": "roster.append"}
            )
        if (existing.kind, existing.follows, existing.signed_bytes) != (kind, follows, data):
            raise RepositoryConflictError(
                "roster entry identity was reused with different semantics",
                metadata={"operation": "roster.append"},
            )
        return existing

    def get_entry(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, entry_id: str
    ) -> RosterEntryRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            _GET_ENTRY_SQL,
            (owner, hash_value(mesh_id, "mesh_id"), hash_value(entry_id, "entry_id")),
        )
        return None if row is None else _entry(row)

    def list_entries(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, limit: int = 200
    ) -> tuple[RosterEntryRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        rows = tuple(
            query.fetch_all(
                _LIST_ENTRIES_SQL,
                (owner, hash_value(mesh_id, "mesh_id"), _bounded_limit(limit, maximum=_MAX_PAGE)),
            )
        )
        _owned(rows, owner)
        return tuple(_entry(row) for row in rows)

    def list_tips(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str
    ) -> tuple[RosterEntryRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        statement = dialect_of(query).choose(
            postgres=_TIPS_SQL,
            sqlite=_TIPS_SQL.replace(
                "follower.follows @> jsonb_build_array(entry.entry_id)",
                "EXISTS (SELECT 1 FROM json_each(follower.follows) AS followed "
                "WHERE followed.value = entry.entry_id)",
            ),
        )
        rows = tuple(query.fetch_all(statement, (owner, hash_value(mesh_id, "mesh_id"))))
        _owned(rows, owner)
        return tuple(_entry(row) for row in rows)

    def put_checkpoint(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        feed_id: str,
        seq: int,
        head_hash: str,
        signed_bytes: bytes,
        recorded_at: int,
    ) -> CheckpointRecord:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        feed = _required_id(feed_id, "feed_id", maximum=128)
        position = bounded_integer(seq, "seq", minimum=1)
        head = hash_value(head_hash, "head_hash")
        data = byte_string(signed_bytes, "signed_bytes", maximum=_MAX_SIGNED)
        recorded = bounded_integer(recorded_at, "recorded_at")
        checkpoint_id = hash_reference(data)
        row = returned_row(
            transaction.execute(
                _INSERT_CHECKPOINT_SQL,
                (checkpoint_id, feed, position, head, data, recorded, owner, mesh),
            ),
            "checkpoint.put",
        )
        if row is not None:
            return _checkpoint(row, ())
        existing = self.get_checkpoint(
            transaction, owner_id=owner, mesh_id=mesh, checkpoint_id=checkpoint_id
        )
        if existing is None:
            raise RepositoryNotFoundError(
                "mesh was not found", metadata={"operation": "checkpoint.put"}
            )
        if (existing.feed_id, existing.seq, existing.head_hash) != (feed, position, head):
            raise RepositoryConflictError(
                "checkpoint identity was reused with different semantics",
                metadata={"operation": "checkpoint.put"},
            )
        return existing

    def add_countersignature(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        checkpoint_id: str,
        signer_id: str,
        signature: bytes,
        recorded_at: int,
    ) -> CheckpointRecord:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        checkpoint = hash_value(checkpoint_id, "checkpoint_id")
        signer = _required_id(signer_id, "signer_id", maximum=128)
        proof = byte_string(signature, "signature", maximum=_MAX_SIGNATURE)
        recorded = bounded_integer(recorded_at, "recorded_at")
        transaction.execute(
            _INSERT_COUNTERSIGNATURE_SQL, (signer, proof, recorded, owner, mesh, checkpoint)
        )
        stored = self.get_checkpoint(
            transaction, owner_id=owner, mesh_id=mesh, checkpoint_id=checkpoint
        )
        if stored is None:
            raise RepositoryNotFoundError(
                "checkpoint was not found", metadata={"operation": "checkpoint.countersign"}
            )
        return stored

    def get_checkpoint(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, checkpoint_id: str
    ) -> CheckpointRecord | None:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        checkpoint = hash_value(checkpoint_id, "checkpoint_id")
        row = query.fetch_one(_GET_CHECKPOINT_SQL, (owner, mesh, checkpoint))
        if row is None:
            return None
        return _checkpoint(row, self._signatures(query, owner, mesh, [checkpoint])[checkpoint])

    def list_checkpoints(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        mesh_id: str,
        feed_id: str | None = None,
        limit: int = 200,
    ) -> tuple[CheckpointRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        feed = None if feed_id is None else _required_id(feed_id, "feed_id", maximum=128)
        rows = tuple(
            query.fetch_all(
                _LIST_CHECKPOINTS_SQL,
                (owner, mesh, feed, feed, _bounded_limit(limit, maximum=_MAX_PAGE)),
            )
        )
        _owned(rows, owner)
        signatures = self._signatures(
            query, owner, mesh, [str(row["checkpoint_id"]) for row in rows]
        )
        return tuple(_checkpoint(row, signatures[str(row["checkpoint_id"])]) for row in rows)

    @staticmethod
    def _signatures(
        query: QueryExecutor, owner: str, mesh: str, checkpoint_ids: list[str]
    ) -> dict[str, list[CountersignatureRecord]]:
        grouped: dict[str, list[CountersignatureRecord]] = {item: [] for item in checkpoint_ids}
        if not checkpoint_ids:
            return grouped
        rows = tuple(query.fetch_all(_COUNTERSIGNATURES_SQL, (owner, mesh, checkpoint_ids)))
        _owned(rows, owner)
        for row in rows:
            record = _countersignature(row)
            grouped[record.checkpoint_id].append(record)
        return grouped

    def list_feeds_behind_checkpoints(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str
    ) -> tuple[FeedBehindCheckpoint, ...]:
        owner = _required_id(owner_id, "owner_id")
        rows = tuple(query.fetch_all(_BEHIND_SQL, (owner, hash_value(mesh_id, "mesh_id"))))
        return tuple(
            FeedBehindCheckpoint(
                feed_id=str(_row_value(row, "feed_id")),
                checkpoint_id=str(_row_value(row, "checkpoint_id")),
                checkpoint_seq=stored_integer(row, "checkpoint_seq"),
                held_seq=stored_integer(row, "held_seq"),
            )
            for row in rows
        )

    def list_checkpoint_contradictions(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str
    ) -> tuple[CheckpointContradiction, ...]:
        owner = _required_id(owner_id, "owner_id")
        rows = tuple(query.fetch_all(_CONTRADICTIONS_SQL, (owner, hash_value(mesh_id, "mesh_id"))))
        return tuple(
            CheckpointContradiction(
                feed_id=str(_row_value(row, "feed_id")),
                checkpoint_id=str(_row_value(row, "checkpoint_id")),
                checkpoint_seq=stored_integer(row, "checkpoint_seq"),
                checkpoint_hash=str(_row_value(row, "checkpoint_hash")),
                held_event_id=str(_row_value(row, "held_event_id")),
            )
            for row in rows
        )


__all__ = (
    "CheckpointContradiction",
    "CheckpointRecord",
    "CountersignatureRecord",
    "EventRosterRepository",
    "FeedBehindCheckpoint",
    "RosterEntryRecord",
)
