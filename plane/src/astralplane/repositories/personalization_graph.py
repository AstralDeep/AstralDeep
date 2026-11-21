"""Owner-scoped link graph between personalization memories, plus short-term promotion
signals and consolidation-sweep history. Used by AstralDeep's
personalization/repository.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _non_negative_int,
    _required_id,
    _row_value,
    _single_returned,
)

_CATEGORIES = frozenset({"context", "goal", "preference", "profession", "workflow_tag"})
_TRIGGERS = frozenset({"manual", "scheduled"})


@dataclass(frozen=True, slots=True)
class MemoryLinkRecord:
    owner_id: str
    memory_id: str
    linked_id: str
    created_at: int | None


@dataclass(frozen=True, slots=True)
class ShortTermSignalRecord:
    signal_id: str
    owner_id: str
    category: str
    value: str = field(repr=False)
    recall_count: int = 0
    last_seen_at: int | None = None
    created_at: int | None = None


@dataclass(frozen=True, slots=True)
class ConsolidationSweepRecord:
    sweep_id: str
    owner_id: str
    ran_at: int
    candidates_considered: int
    promoted_count: int
    summary: str = field(repr=False)
    trigger: str = "scheduled"


class PersonalizationGraphRepository:
    _LINK_FIELDS = "user_id, memory_id, linked_id, created_at"
    _SIGNAL_FIELDS = (
        "id, user_id, category, value, recall_count, last_seen_at, created_at"
    )
    _SWEEP_FIELDS = (
        "id, user_id, ran_at, candidates_considered, promoted_count, summary, trigger"
    )

    def add_link(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        memory_id: str,
        linked_id: str,
        created_at: int,
    ) -> tuple[MemoryLinkRecord, MemoryLinkRecord]:
        owner = _required_id(owner_id, "owner_id")
        left = _required_id(memory_id, "memory_id")
        right = _required_id(linked_id, "linked_id")
        if left == right:
            raise RepositoryValidationError("memory links cannot target themselves")
        observed_at = _non_negative_int(created_at, "created_at")
        transaction.execute(
            f"""
            WITH endpoints AS (
                SELECT source.id AS memory_id, target.id AS linked_id
                  FROM memory_item AS source
                  JOIN memory_item AS target
                    ON target.id = %s AND target.user_id = %s
                   AND target.superseded_at IS NULL
                 WHERE source.id = %s AND source.user_id = %s
                   AND source.superseded_at IS NULL
            ), directed AS (
                SELECT memory_id, linked_id FROM endpoints
                UNION ALL
                SELECT linked_id, memory_id FROM endpoints
            )
            INSERT INTO memory_link (user_id, memory_id, linked_id, created_at)
            SELECT %s, memory_id, linked_id, %s FROM directed
            ON CONFLICT (user_id, memory_id, linked_id) DO NOTHING
            RETURNING {self._LINK_FIELDS}
            """,
            (right, owner, left, owner, owner, observed_at),
        )
        pair = self._load_pair(
            transaction,
            owner_id=owner,
            memory_id=left,
            linked_id=right,
        )
        if len(pair) != 2:
            raise RepositoryNotFoundError(
                "owner-scoped live memory endpoints were unavailable",
                metadata={"operation": "personalization_graph.add_link"},
            )
        return pair[0], pair[1]

    def remove_link(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        memory_id: str,
        linked_id: str,
    ) -> bool:
        owner = _required_id(owner_id, "owner_id")
        left = _required_id(memory_id, "memory_id")
        right = _required_id(linked_id, "linked_id")
        if left == right:
            raise RepositoryValidationError("memory links cannot target themselves")
        result = transaction.execute(
            """
            DELETE FROM memory_link
             WHERE user_id = %s
               AND ((memory_id = %s AND linked_id = %s)
                 OR (memory_id = %s AND linked_id = %s))
            """,
            (owner, left, right, right, left),
        )
        if result.rowcount not in (0, 2):
            raise RepositoryDataError("persisted memory graph contained a partial link pair")
        return result.rowcount == 2

    def linked_ids(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        memory_id: str,
        limit: int = 200,
    ) -> tuple[str, ...]:
        owner = _required_id(owner_id, "owner_id")
        memory = _required_id(memory_id, "memory_id")
        maximum = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            """
            SELECT link.linked_id
              FROM memory_link AS link
              JOIN memory_item AS source
                ON source.id = link.memory_id AND source.user_id = link.user_id
               AND source.superseded_at IS NULL
              JOIN memory_item AS target
                ON target.id = link.linked_id AND target.user_id = link.user_id
               AND target.superseded_at IS NULL
             WHERE link.user_id = %s AND link.memory_id = %s
             ORDER BY link.linked_id
             LIMIT %s
            """,
            (owner, memory, maximum),
        )
        return tuple(_stored_id(_row_value(row, "linked_id"), "linked_id") for row in rows)

    def list_links(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        limit: int = 1000,
    ) -> tuple[MemoryLinkRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        maximum = _bounded_limit(limit, maximum=5000)
        rows = query.fetch_all(
            f"""
            SELECT link.{self._LINK_FIELDS.replace(', ', ', link.')}
              FROM memory_link AS link
              JOIN memory_item AS source
                ON source.id = link.memory_id AND source.user_id = link.user_id
               AND source.superseded_at IS NULL
              JOIN memory_item AS target
                ON target.id = link.linked_id AND target.user_id = link.user_id
               AND target.superseded_at IS NULL
             WHERE link.user_id = %s
             ORDER BY link.memory_id, link.linked_id
             LIMIT %s
            """,
            (owner, maximum),
        )
        return tuple(_owned_link(row, owner) for row in rows)

    def create_signal(
        self,
        transaction: Transaction,
        record: ShortTermSignalRecord,
    ) -> ShortTermSignalRecord:
        signal = _validated_signal(record)
        result = transaction.execute(
            f"""
            INSERT INTO short_term_signal (
                id, user_id, category, value, recall_count, last_seen_at, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._SIGNAL_FIELDS}
            """,
            (
                signal.signal_id,
                signal.owner_id,
                signal.category,
                signal.value,
                signal.recall_count,
                signal.last_seen_at,
                signal.created_at,
            ),
        )
        returned = getattr(result, "returned_records", ())
        if returned:
            return _owned_signal(
                _single_returned(result, "personalization_graph.create_signal"),
                signal.owner_id,
            )
        existing = self.get_signal(
            transaction,
            owner_id=signal.owner_id,
            signal_id=signal.signal_id,
        )
        if existing is None:
            raise RepositoryConflictError("signal identity is owned by another namespace")
        if existing != signal:
            raise RepositoryConflictError("signal replay changed immutable semantics")
        return existing

    def get_signal(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        signal_id: str,
    ) -> ShortTermSignalRecord | None:
        owner = _required_id(owner_id, "owner_id")
        signal = _required_id(signal_id, "signal_id")
        row = query.fetch_one(
            f"SELECT {self._SIGNAL_FIELDS} FROM short_term_signal "
            "WHERE id = %s AND user_id = %s",
            (signal, owner),
        )
        return None if row is None else _owned_signal(row, owner)

    def list_signals(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        limit: int = 200,
    ) -> tuple[ShortTermSignalRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        maximum = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"SELECT {self._SIGNAL_FIELDS} FROM short_term_signal "
            "WHERE user_id = %s ORDER BY last_seen_at DESC NULLS LAST, id ASC LIMIT %s",
            (owner, maximum),
        )
        return tuple(_owned_signal(row, owner) for row in rows)

    def delete_signal(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        signal_id: str,
    ) -> bool:
        owner = _required_id(owner_id, "owner_id")
        signal = _required_id(signal_id, "signal_id")
        result = transaction.execute(
            "DELETE FROM short_term_signal WHERE id = %s AND user_id = %s",
            (signal, owner),
        )
        if result.rowcount not in (0, 1):
            raise RepositoryDataError("signal delete returned an invalid row count")
        return result.rowcount == 1

    def record_sweep(
        self,
        transaction: Transaction,
        record: ConsolidationSweepRecord,
    ) -> ConsolidationSweepRecord:
        sweep = _validated_sweep(record)
        result = transaction.execute(
            f"""
            INSERT INTO consolidation_sweep (
                id, user_id, ran_at, candidates_considered, promoted_count,
                summary, trigger
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._SWEEP_FIELDS}
            """,
            (
                sweep.sweep_id,
                sweep.owner_id,
                sweep.ran_at,
                sweep.candidates_considered,
                sweep.promoted_count,
                sweep.summary,
                sweep.trigger,
            ),
        )
        returned = getattr(result, "returned_records", ())
        if returned:
            return _owned_sweep(
                _single_returned(result, "personalization_graph.record_sweep"),
                sweep.owner_id,
            )
        existing = self.get_sweep(
            transaction,
            owner_id=sweep.owner_id,
            sweep_id=sweep.sweep_id,
        )
        if existing is None:
            raise RepositoryConflictError("sweep identity is owned by another namespace")
        if existing != sweep:
            raise RepositoryConflictError("sweep replay changed immutable semantics")
        return existing

    def get_sweep(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        sweep_id: str,
    ) -> ConsolidationSweepRecord | None:
        owner = _required_id(owner_id, "owner_id")
        sweep = _required_id(sweep_id, "sweep_id")
        row = query.fetch_one(
            f"SELECT {self._SWEEP_FIELDS} FROM consolidation_sweep "
            "WHERE id = %s AND user_id = %s",
            (sweep, owner),
        )
        return None if row is None else _owned_sweep(row, owner)

    def list_sweeps(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        limit: int = 20,
    ) -> tuple[ConsolidationSweepRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        maximum = _bounded_limit(limit, maximum=200)
        rows = query.fetch_all(
            f"SELECT {self._SWEEP_FIELDS} FROM consolidation_sweep "
            "WHERE user_id = %s ORDER BY ran_at DESC, id ASC LIMIT %s",
            (owner, maximum),
        )
        return tuple(_owned_sweep(row, owner) for row in rows)

    def _load_pair(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        memory_id: str,
        linked_id: str,
    ) -> tuple[MemoryLinkRecord, ...]:
        rows = query.fetch_all(
            f"""
            SELECT {self._LINK_FIELDS} FROM memory_link
             WHERE user_id = %s
               AND ((memory_id = %s AND linked_id = %s)
                 OR (memory_id = %s AND linked_id = %s))
             ORDER BY memory_id, linked_id
            """,
            (owner_id, memory_id, linked_id, linked_id, memory_id),
        )
        return tuple(_owned_link(row, owner_id) for row in rows)


def _optional_time(value: object, field: str) -> int | None:
    return None if value is None else _non_negative_int(value, field)


def _stored_id(value: object, field: str) -> str:
    try:
        return _required_id(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted personalization graph identity is invalid",
            metadata={"field": field},
        ) from exc


def _stored_int(value: object, field: str) -> int:
    try:
        return _non_negative_int(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted personalization graph integer is invalid",
            metadata={"field": field},
        ) from exc


def _stored_optional_int(value: object, field: str) -> int | None:
    return None if value is None else _stored_int(value, field)


def _link(row: Mapping[str, Any]) -> MemoryLinkRecord:
    return MemoryLinkRecord(
        owner_id=_stored_id(_row_value(row, "user_id"), "owner_id"),
        memory_id=_stored_id(_row_value(row, "memory_id"), "memory_id"),
        linked_id=_stored_id(_row_value(row, "linked_id"), "linked_id"),
        created_at=_stored_optional_int(row.get("created_at"), "created_at"),
    )


def _owned_link(row: Mapping[str, Any], owner_id: str) -> MemoryLinkRecord:
    record = _link(row)
    if record.owner_id != owner_id:
        raise RepositoryDataError("memory link query returned another owner's row")
    return record


def _validated_signal(record: ShortTermSignalRecord) -> ShortTermSignalRecord:
    if not isinstance(record, ShortTermSignalRecord):
        raise RepositoryValidationError("record must be a ShortTermSignalRecord")
    signal = _required_id(record.signal_id, "signal_id")
    owner = _required_id(record.owner_id, "owner_id")
    if record.category not in _CATEGORIES:
        raise RepositoryValidationError("signal category is unsupported")
    value = _bounded_text(record.value, "value", maximum=16384)
    recalls = _non_negative_int(record.recall_count, "recall_count")
    last_seen = _optional_time(record.last_seen_at, "last_seen_at")
    created = _optional_time(record.created_at, "created_at")
    return ShortTermSignalRecord(
        signal_id=signal,
        owner_id=owner,
        category=record.category,
        value=value,
        recall_count=recalls,
        last_seen_at=last_seen,
        created_at=created,
    )


def _signal(row: Mapping[str, Any]) -> ShortTermSignalRecord:
    category = str(_row_value(row, "category"))
    if category not in _CATEGORIES:
        raise RepositoryDataError("persisted signal category is unsupported")
    value = row.get("value")
    if not isinstance(value, str):
        raise RepositoryDataError("persisted signal value is invalid")
    return ShortTermSignalRecord(
        signal_id=_stored_id(_row_value(row, "id"), "signal_id"),
        owner_id=_stored_id(_row_value(row, "user_id"), "owner_id"),
        category=category,
        value=value,
        recall_count=_stored_int(_row_value(row, "recall_count"), "recall_count"),
        last_seen_at=_stored_optional_int(row.get("last_seen_at"), "last_seen_at"),
        created_at=_stored_optional_int(row.get("created_at"), "created_at"),
    )


def _owned_signal(row: Mapping[str, Any], owner_id: str) -> ShortTermSignalRecord:
    record = _signal(row)
    if record.owner_id != owner_id:
        raise RepositoryDataError("signal query returned another owner's row")
    return record


def _validated_sweep(record: ConsolidationSweepRecord) -> ConsolidationSweepRecord:
    if not isinstance(record, ConsolidationSweepRecord):
        raise RepositoryValidationError("record must be a ConsolidationSweepRecord")
    sweep = _required_id(record.sweep_id, "sweep_id")
    owner = _required_id(record.owner_id, "owner_id")
    ran_at = _non_negative_int(record.ran_at, "ran_at")
    candidates = _non_negative_int(record.candidates_considered, "candidates_considered")
    promoted = _non_negative_int(record.promoted_count, "promoted_count")
    if promoted > candidates:
        raise RepositoryValidationError("promoted_count cannot exceed candidates_considered")
    summary = _bounded_text(record.summary, "summary", maximum=16384, allow_empty=True)
    if record.trigger not in _TRIGGERS:
        raise RepositoryValidationError("consolidation trigger is unsupported")
    return ConsolidationSweepRecord(
        sweep_id=sweep,
        owner_id=owner,
        ran_at=ran_at,
        candidates_considered=candidates,
        promoted_count=promoted,
        summary=summary,
        trigger=record.trigger,
    )


def _sweep(row: Mapping[str, Any]) -> ConsolidationSweepRecord:
    trigger = str(_row_value(row, "trigger"))
    if trigger not in _TRIGGERS:
        raise RepositoryDataError("persisted consolidation trigger is unsupported")
    record = ConsolidationSweepRecord(
        sweep_id=_stored_id(_row_value(row, "id"), "sweep_id"),
        owner_id=_stored_id(_row_value(row, "user_id"), "owner_id"),
        ran_at=_stored_int(_row_value(row, "ran_at"), "ran_at"),
        candidates_considered=_stored_int(
            _row_value(row, "candidates_considered"), "candidates_considered"
        ),
        promoted_count=_stored_int(_row_value(row, "promoted_count"), "promoted_count"),
        summary=str(_row_value(row, "summary")),
        trigger=trigger,
    )
    if record.promoted_count > record.candidates_considered:
        raise RepositoryDataError("persisted consolidation counts are inconsistent")
    return record


def _owned_sweep(row: Mapping[str, Any], owner_id: str) -> ConsolidationSweepRecord:
    record = _sweep(row)
    if record.owner_id != owner_id:
        raise RepositoryDataError("sweep query returned another owner's row")
    return record


__all__ = (
    "ConsolidationSweepRecord",
    "MemoryLinkRecord",
    "PersonalizationGraphRepository",
    "ShortTermSignalRecord",
)
