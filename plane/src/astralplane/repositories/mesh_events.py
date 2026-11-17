"""Owner-isolated store for one owner's mesh event log: per-device feeds whose chain
position, previous hash and sequence are checked on append, fork evidence that freezes a
feed, and hash-addressed sealed bodies that may be absent. The host verifies signatures and
decryption; this module keeps the durable chain rules and every read carries the owner.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
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
    optional_hash_value,
    returned_row,
    stored_bytes,
    stored_integer,
    stored_json,
)

APPENDED: Final = "appended"
DUPLICATE: Final = "duplicate"
FORKED: Final = "forked"

_MAX_HEADER: Final = 65536
_MAX_SEALED_HEADER: Final = 131072
_MAX_SEALED_BODY: Final = 16777216
_MAX_PAGE: Final = 1000

_FEED_FIELDS: Final = (
    "owner_id, mesh_id, feed_id, state, head_seq, head_hash, recorded_at, frozen_at"
)
_EVENT_FIELDS: Final = (
    "owner_id, mesh_id, feed_id, seq, event_id, prev_hash, clock_ms, clock_n, scope, "
    "event_type, labels, key_id, body_hash, header_bytes, header_sealed, recorded_at"
)
_ENTRY_FIELDS: Final = ", ".join(f"entry.{name}" for name in _EVENT_FIELDS.split(", "))
_EVIDENCE_FIELDS: Final = (
    "owner_id, mesh_id, feed_id, seq, event_id, header_bytes, header_sealed, recorded_at"
)

_GET_MESH_SQL: Final = """
SELECT owner_id, mesh_id, recorded_at FROM event_mesh WHERE owner_id = %s AND mesh_id = %s
""".strip()

_LIST_MESH_SQL: Final = """
SELECT owner_id, mesh_id, recorded_at FROM event_mesh
WHERE owner_id = %s ORDER BY recorded_at ASC, mesh_id ASC LIMIT %s
""".strip()

_ENSURE_FEED_SQL: Final = """
INSERT INTO event_feed (owner_id, mesh_id, feed_id, state, head_seq, head_hash, recorded_at)
SELECT mesh.owner_id, mesh.mesh_id, %s, 'accepted', 0, NULL, %s
FROM event_mesh AS mesh
WHERE mesh.owner_id = %s AND mesh.mesh_id = %s
ON CONFLICT (owner_id, mesh_id, feed_id) DO NOTHING
""".strip()

_LOCK_FEED_SQL: Final = f"""
SELECT {_FEED_FIELDS} FROM event_feed
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s
FOR UPDATE
""".strip()

_GET_FEED_SQL: Final = f"""
SELECT {_FEED_FIELDS} FROM event_feed
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s
""".strip()

_LIST_FEEDS_SQL: Final = f"""
SELECT {_FEED_FIELDS} FROM event_feed
WHERE owner_id = %s AND mesh_id = %s ORDER BY feed_id ASC
""".strip()

_EVENT_AT_SQL: Final = f"""
SELECT {_EVENT_FIELDS} FROM event_entry
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s AND seq = %s
""".strip()

_EVENT_BY_ID_SQL: Final = f"""
SELECT {_EVENT_FIELDS} FROM event_entry
WHERE owner_id = %s AND mesh_id = %s AND event_id = %s
""".strip()

_INSERT_EVENT_SQL: Final = f"""
INSERT INTO event_entry (
    owner_id, mesh_id, feed_id, seq, event_id, prev_hash, clock_ms, clock_n, scope,
    event_type, labels, key_id, body_hash, header_bytes, header_sealed, recorded_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
RETURNING {_EVENT_FIELDS}
""".strip()

_ADVANCE_HEAD_SQL: Final = f"""
UPDATE event_feed SET head_seq = %s, head_hash = %s
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s AND head_seq = %s
RETURNING {_FEED_FIELDS}
""".strip()

_FREEZE_SQL: Final = f"""
UPDATE event_feed SET state = 'frozen', frozen_at = COALESCE(frozen_at, %s)
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s
RETURNING {_FEED_FIELDS}
""".strip()

_EVIDENCE_FROM_STORED_SQL: Final = """
INSERT INTO event_fork_evidence (
    owner_id, mesh_id, feed_id, seq, event_id, header_bytes, header_sealed, recorded_at
)
SELECT owner_id, mesh_id, feed_id, seq, event_id, header_bytes, header_sealed, %s
FROM event_entry
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s AND seq = %s
ON CONFLICT DO NOTHING
""".strip()

_EVIDENCE_INSERT_SQL: Final = """
INSERT INTO event_fork_evidence (
    owner_id, mesh_id, feed_id, seq, event_id, header_bytes, header_sealed, recorded_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
""".strip()

_EVIDENCE_LIST_SQL: Final = f"""
SELECT {_EVIDENCE_FIELDS} FROM event_fork_evidence
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s
ORDER BY seq ASC, event_id ASC
""".strip()

_AFTER_SQL: Final = f"""
SELECT {_EVENT_FIELDS} FROM event_entry
WHERE owner_id = %s AND mesh_id = %s AND feed_id = %s AND seq > %s
ORDER BY seq ASC LIMIT %s
""".strip()

_SCOPE_SQL: Final = f"""
SELECT {_EVENT_FIELDS} FROM event_entry
WHERE owner_id = %s AND mesh_id = %s AND scope = %s
ORDER BY clock_ms ASC, clock_n ASC, feed_id ASC, seq ASC LIMIT %s
""".strip()

_SCOPE_AFTER_SQL: Final = f"""
SELECT {_EVENT_FIELDS} FROM event_entry
WHERE owner_id = %s AND mesh_id = %s AND scope = %s
  AND (clock_ms, clock_n, feed_id, seq) > (%s, %s, %s, %s)
ORDER BY clock_ms ASC, clock_n ASC, feed_id ASC, seq ASC LIMIT %s
""".strip()

_PUT_BODY_SQL: Final = """
INSERT INTO event_body (owner_id, mesh_id, body_hash, sealed, recorded_at)
SELECT mesh.owner_id, mesh.mesh_id, %s, %s, %s
FROM event_mesh AS mesh
WHERE mesh.owner_id = %s AND mesh.mesh_id = %s
ON CONFLICT (owner_id, mesh_id, body_hash) DO NOTHING
""".strip()

_GET_BODY_SQL: Final = """
SELECT owner_id, mesh_id, body_hash, sealed, recorded_at FROM event_body
WHERE owner_id = %s AND mesh_id = %s AND body_hash = %s
""".strip()

_MISSING_BODIES_SQL: Final = f"""
SELECT {_ENTRY_FIELDS}
FROM event_entry AS entry
LEFT JOIN event_body AS body
  ON body.owner_id = entry.owner_id AND body.mesh_id = entry.mesh_id
 AND body.body_hash = entry.body_hash
WHERE entry.owner_id = %s AND entry.mesh_id = %s AND body.body_hash IS NULL
  AND (%s::text IS NULL OR entry.scope = %s)
ORDER BY entry.clock_ms ASC, entry.clock_n ASC, entry.feed_id ASC, entry.seq ASC
LIMIT %s
""".strip()


@dataclass(frozen=True, slots=True)
class EventMeshRecord:
    owner_id: str
    mesh_id: str
    recorded_at: int


@dataclass(frozen=True, slots=True)
class EventFeedRecord:
    owner_id: str
    mesh_id: str
    feed_id: str
    state: str
    head_seq: int
    head_hash: str | None
    recorded_at: int
    frozen_at: int | None


@dataclass(frozen=True, slots=True)
class EventRecord:
    owner_id: str
    mesh_id: str
    feed_id: str
    seq: int
    event_id: str
    prev_hash: str | None
    clock_ms: int
    clock_n: int
    scope: str
    event_type: str
    labels: Any
    key_id: str
    body_hash: str
    header_bytes: bytes
    header_sealed: bytes
    recorded_at: int


@dataclass(frozen=True, slots=True)
class EventAppendResult:
    outcome: str
    feed: EventFeedRecord
    event: EventRecord | None


@dataclass(frozen=True, slots=True)
class ForkEvidenceRecord:
    owner_id: str
    mesh_id: str
    feed_id: str
    seq: int
    event_id: str
    header_bytes: bytes
    header_sealed: bytes
    recorded_at: int


@dataclass(frozen=True, slots=True)
class EventBodyRecord:
    owner_id: str
    mesh_id: str
    body_hash: str
    sealed: bytes
    recorded_at: int


@dataclass(frozen=True, slots=True)
class EventCursor:
    clock_ms: int
    clock_n: int
    feed_id: str
    seq: int


def _mesh(row: Mapping[str, Any]) -> EventMeshRecord:
    return EventMeshRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _feed(row: Mapping[str, Any]) -> EventFeedRecord:
    frozen_at = _row_value(row, "frozen_at")
    head_hash = _row_value(row, "head_hash")
    return EventFeedRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        feed_id=str(_row_value(row, "feed_id")),
        state=str(_row_value(row, "state")),
        head_seq=stored_integer(row, "head_seq"),
        head_hash=None if head_hash is None else str(head_hash),
        recorded_at=stored_integer(row, "recorded_at"),
        frozen_at=None if frozen_at is None else stored_integer(row, "frozen_at"),
    )


def _event(row: Mapping[str, Any]) -> EventRecord:
    prev_hash = _row_value(row, "prev_hash")
    return EventRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        feed_id=str(_row_value(row, "feed_id")),
        seq=stored_integer(row, "seq"),
        event_id=str(_row_value(row, "event_id")),
        prev_hash=None if prev_hash is None else str(prev_hash),
        clock_ms=stored_integer(row, "clock_ms"),
        clock_n=stored_integer(row, "clock_n"),
        scope=str(_row_value(row, "scope")),
        event_type=str(_row_value(row, "event_type")),
        labels=stored_json(row, "labels"),
        key_id=str(_row_value(row, "key_id")),
        body_hash=str(_row_value(row, "body_hash")),
        header_bytes=stored_bytes(row, "header_bytes"),
        header_sealed=stored_bytes(row, "header_sealed"),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _evidence(row: Mapping[str, Any]) -> ForkEvidenceRecord:
    return ForkEvidenceRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        feed_id=str(_row_value(row, "feed_id")),
        seq=stored_integer(row, "seq"),
        event_id=str(_row_value(row, "event_id")),
        header_bytes=stored_bytes(row, "header_bytes"),
        header_sealed=stored_bytes(row, "header_sealed"),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _body(row: Mapping[str, Any]) -> EventBodyRecord:
    return EventBodyRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        body_hash=str(_row_value(row, "body_hash")),
        sealed=stored_bytes(row, "sealed"),
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _owned(rows: tuple[Mapping[str, Any], ...], owner_id: str) -> None:
    if any(row.get("owner_id") != owner_id for row in rows):
        raise RepositoryDataError("batched read crossed the owner boundary")


def _page_limit(limit: object) -> int:
    return _bounded_limit(limit, maximum=_MAX_PAGE)


class EventLogRepository:
    def get_mesh(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str
    ) -> EventMeshRecord | None:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        row = query.fetch_one(_GET_MESH_SQL, (owner, mesh))
        return None if row is None else _mesh(row)

    def list_meshes(
        self, query: QueryExecutor, *, owner_id: str, limit: int = 200
    ) -> tuple[EventMeshRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        rows = tuple(query.fetch_all(_LIST_MESH_SQL, (owner, _bounded_limit(limit))))
        _owned(rows, owner)
        return tuple(_mesh(row) for row in rows)

    def append_event(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        feed_id: str,
        seq: int,
        event_id: str,
        prev_hash: str | None,
        clock_ms: int,
        clock_n: int,
        scope: str,
        event_type: str,
        labels: object,
        key_id: str,
        body_hash: str,
        header_bytes: bytes,
        header_sealed: bytes,
        recorded_at: int,
    ) -> EventAppendResult:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        feed = _required_id(feed_id, "feed_id", maximum=128)
        position = bounded_integer(seq, "seq", minimum=1)
        identity = hash_value(event_id, "event_id")
        previous = optional_hash_value(prev_hash, "prev_hash")
        if (position == 1) != (previous is None):
            raise RepositoryConflictError(
                "previous hash must be absent exactly at the first position",
                metadata={"reason": "previous_hash_shape"},
            )
        clock_ms = bounded_integer(clock_ms, "clock_ms")
        clock_n = bounded_integer(clock_n, "clock_n")
        scope = _bounded_text(scope, "scope", maximum=512)
        event_type = _bounded_text(event_type, "event_type", maximum=128)
        labels_json = _canonical_json(labels, "labels")
        key = _bounded_text(key_id, "key_id", maximum=256)
        body = hash_value(body_hash, "body_hash")
        header = byte_string(header_bytes, "header_bytes", maximum=_MAX_HEADER)
        sealed = byte_string(header_sealed, "header_sealed", maximum=_MAX_SEALED_HEADER)
        recorded = bounded_integer(recorded_at, "recorded_at")
        if hash_reference(header) != identity:
            raise RepositoryConflictError(
                "event id is not the hash of the signed header",
                metadata={"reason": "event_id_mismatch"},
            )
        transaction.execute(_ENSURE_FEED_SQL, (feed, recorded, owner, mesh))
        feed_row = transaction.fetch_one(_LOCK_FEED_SQL, (owner, mesh, feed))
        if feed_row is None:
            raise RepositoryNotFoundError(
                "mesh was not found", metadata={"operation": "event.append"}
            )
        head = _feed(feed_row)
        stored = transaction.fetch_one(_EVENT_AT_SQL, (owner, mesh, feed, position))
        if stored is not None:
            existing = _event(stored)
            if existing.event_id == identity:
                self._require_same_event(
                    existing,
                    prev_hash=previous,
                    clock=(clock_ms, clock_n),
                    scope=scope,
                    event_type=event_type,
                    labels_json=labels_json,
                    key_id=key,
                    body_hash=body,
                    header=header,
                )
                return EventAppendResult(DUPLICATE, head, existing)
            transaction.execute(_EVIDENCE_FROM_STORED_SQL, (recorded, owner, mesh, feed, position))
            transaction.execute(
                _EVIDENCE_INSERT_SQL,
                (owner, mesh, feed, position, identity, header, sealed, recorded),
            )
            frozen = transaction.execute(_FREEZE_SQL, (recorded, owner, mesh, feed))
            row = returned_row(frozen, "event.freeze")
            return EventAppendResult(FORKED, _feed(row) if row is not None else head, None)
        if head.state == "frozen":
            raise RepositoryConflictError(
                "feed is frozen by fork evidence", metadata={"reason": "feed_frozen"}
            )
        if transaction.fetch_one(_EVENT_BY_ID_SQL, (owner, mesh, identity)) is not None:
            raise RepositoryConflictError(
                "event id is already recorded at another position",
                metadata={"reason": "event_id_reused"},
            )
        if position != head.head_seq + 1:
            raise RepositoryConflictError(
                "event sequence is not contiguous with the feed head",
                metadata={"reason": "sequence_gap", "head_seq": head.head_seq},
            )
        if previous != head.head_hash:
            raise RepositoryConflictError(
                "previous hash does not match the feed head",
                metadata={"reason": "previous_hash_mismatch"},
            )
        inserted = returned_row(
            transaction.execute(
                _INSERT_EVENT_SQL,
                (
                    owner,
                    mesh,
                    feed,
                    position,
                    identity,
                    previous,
                    clock_ms,
                    clock_n,
                    scope,
                    event_type,
                    labels_json,
                    key,
                    body,
                    header,
                    sealed,
                    recorded,
                ),
            ),
            "event.append",
        )
        advanced = returned_row(
            transaction.execute(
                _ADVANCE_HEAD_SQL, (position, identity, owner, mesh, feed, head.head_seq)
            ),
            "event.advance_head",
        )
        if inserted is None or advanced is None:
            raise RepositoryDataError(
                "event append did not return its records", metadata={"operation": "event.append"}
            )
        return EventAppendResult(APPENDED, _feed(advanced), _event(inserted))

    @staticmethod
    def _require_same_event(
        existing: EventRecord,
        *,
        prev_hash: str | None,
        clock: tuple[int, int],
        scope: str,
        event_type: str,
        labels_json: str,
        key_id: str,
        body_hash: str,
        header: bytes,
    ) -> None:
        observed = (
            existing.prev_hash,
            (existing.clock_ms, existing.clock_n),
            existing.scope,
            existing.event_type,
            _thaw(existing.labels),
            existing.key_id,
            existing.body_hash,
            existing.header_bytes,
        )
        expected = (
            prev_hash,
            clock,
            scope,
            event_type,
            json.loads(labels_json),
            key_id,
            body_hash,
            header,
        )
        if observed != expected:
            raise RepositoryConflictError(
                "event identity was reused with different semantics",
                metadata={"operation": "event.append"},
            )

    def get_feed(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, feed_id: str
    ) -> EventFeedRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            _GET_FEED_SQL,
            (owner, hash_value(mesh_id, "mesh_id"), _required_id(feed_id, "feed_id", maximum=128)),
        )
        return None if row is None else _feed(row)

    def list_feed_heads(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str
    ) -> tuple[EventFeedRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        rows = tuple(query.fetch_all(_LIST_FEEDS_SQL, (owner, hash_value(mesh_id, "mesh_id"))))
        _owned(rows, owner)
        return tuple(_feed(row) for row in rows)

    def get_event(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, event_id: str
    ) -> EventRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            _EVENT_BY_ID_SQL,
            (owner, hash_value(mesh_id, "mesh_id"), hash_value(event_id, "event_id")),
        )
        return None if row is None else _event(row)

    def list_events_after(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        mesh_id: str,
        feed_id: str,
        after_seq: int = 0,
        limit: int = 200,
    ) -> tuple[EventRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        rows = tuple(
            query.fetch_all(
                _AFTER_SQL,
                (
                    owner,
                    hash_value(mesh_id, "mesh_id"),
                    _required_id(feed_id, "feed_id", maximum=128),
                    bounded_integer(after_seq, "after_seq"),
                    _page_limit(limit),
                ),
            )
        )
        _owned(rows, owner)
        return tuple(_event(row) for row in rows)

    def list_events_by_scope(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        mesh_id: str,
        scope: str,
        after: EventCursor | None = None,
        limit: int = 200,
    ) -> tuple[EventRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        scope_value = _bounded_text(scope, "scope", maximum=512)
        bounded = _page_limit(limit)
        if after is None:
            rows = tuple(query.fetch_all(_SCOPE_SQL, (owner, mesh, scope_value, bounded)))
        else:
            rows = tuple(
                query.fetch_all(
                    _SCOPE_AFTER_SQL,
                    (
                        owner,
                        mesh,
                        scope_value,
                        bounded_integer(after.clock_ms, "after.clock_ms"),
                        bounded_integer(after.clock_n, "after.clock_n"),
                        _required_id(after.feed_id, "after.feed_id", maximum=128),
                        bounded_integer(after.seq, "after.seq", minimum=1),
                        bounded,
                    ),
                )
            )
        _owned(rows, owner)
        return tuple(_event(row) for row in rows)

    def list_fork_evidence(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, feed_id: str
    ) -> tuple[ForkEvidenceRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        rows = tuple(
            query.fetch_all(
                _EVIDENCE_LIST_SQL,
                (
                    owner,
                    hash_value(mesh_id, "mesh_id"),
                    _required_id(feed_id, "feed_id", maximum=128),
                ),
            )
        )
        _owned(rows, owner)
        return tuple(_evidence(row) for row in rows)

    def put_body(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        body_hash: str,
        sealed: bytes,
        recorded_at: int,
    ) -> EventBodyRecord:
        owner = _required_id(owner_id, "owner_id")
        mesh = hash_value(mesh_id, "mesh_id")
        digest = hash_value(body_hash, "body_hash")
        data = byte_string(sealed, "sealed", maximum=_MAX_SEALED_BODY)
        if hash_reference(data) != digest:
            raise RepositoryConflictError(
                "sealed body does not match its hash", metadata={"reason": "body_hash_mismatch"}
            )
        recorded = bounded_integer(recorded_at, "recorded_at")
        transaction.execute(_PUT_BODY_SQL, (digest, data, recorded, owner, mesh))
        row = transaction.fetch_one(_GET_BODY_SQL, (owner, mesh, digest))
        if row is None:
            raise RepositoryNotFoundError("mesh was not found", metadata={"operation": "body.put"})
        return _body(row)

    def get_body(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, body_hash: str
    ) -> EventBodyRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            _GET_BODY_SQL,
            (owner, hash_value(mesh_id, "mesh_id"), hash_value(body_hash, "body_hash")),
        )
        return None if row is None else _body(row)

    def list_events_missing_bodies(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        mesh_id: str,
        scope: str | None = None,
        limit: int = 200,
    ) -> tuple[EventRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        scope_value = None if scope is None else _bounded_text(scope, "scope", maximum=512)
        rows = tuple(
            query.fetch_all(
                _MISSING_BODIES_SQL,
                (
                    owner,
                    hash_value(mesh_id, "mesh_id"),
                    scope_value,
                    scope_value,
                    _page_limit(limit),
                ),
            )
        )
        _owned(rows, owner)
        return tuple(_event(row) for row in rows)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(_thaw(item) for item in value)
    return value


__all__ = (
    "APPENDED",
    "DUPLICATE",
    "FORKED",
    "EventAppendResult",
    "EventBodyRecord",
    "EventCursor",
    "EventFeedRecord",
    "EventLogRepository",
    "EventMeshRecord",
    "EventRecord",
    "ForkEvidenceRecord",
)
