"""Owner-isolated, append-only ledger of what a mesh's owner learned: proposals, their
adoption and its reversal, each with a content hash, a scope and the event ids that justify
it. Entries are idempotent by their own event id and follow the proposal, adoption, reversal
order; deciding whether an adoption is allowed stays with the host.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from astralplane.contracts import QueryExecutor, Transaction
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
    hash_value,
    returned_row,
    stored_integer,
    stored_json,
)

_LOCK_SEED: Final = 105_051
_MAX_PAGE: Final = 1000
_MAX_EVIDENCE: Final = 64
_ITEM_KINDS: Final = frozenset({"memory", "skill", "instruction"})
_REVIEWS: Final = frozenset({"automatic", "owner"})

_FIELDS: Final = (
    "owner_id, mesh_id, event_id, kind, proposal_id, item_kind, content_hash, scope, review, "
    "evidence, recorded_at"
)

_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))"

_BY_EVENT_SQL: Final = f"""
SELECT {_FIELDS} FROM learning_ledger_entry
WHERE owner_id = %s AND mesh_id = %s AND event_id = %s
""".strip()

_BY_PROPOSAL_SQL: Final = f"""
SELECT {_FIELDS} FROM learning_ledger_entry
WHERE owner_id = %s AND mesh_id = %s AND proposal_id = %s
ORDER BY CASE kind WHEN 'proposal' THEN 0 WHEN 'adoption' THEN 1 ELSE 2 END ASC
""".strip()

_INSERT_SQL: Final = f"""
INSERT INTO learning_ledger_entry (
    owner_id, mesh_id, event_id, kind, proposal_id, item_kind, content_hash, scope, review,
    evidence, recorded_at
)
SELECT mesh.owner_id, mesh.mesh_id, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
FROM event_mesh AS mesh
WHERE mesh.owner_id = %s AND mesh.mesh_id = %s
RETURNING {_FIELDS}
""".strip()

_ADOPTED_SQL: Final = f"""
SELECT {_FIELDS} FROM learning_ledger_entry AS adoption
WHERE adoption.owner_id = %s AND adoption.mesh_id = %s AND adoption.kind = 'adoption'
  AND (%s::text IS NULL OR adoption.scope = %s)
  AND NOT EXISTS (
      SELECT 1 FROM learning_ledger_entry AS reversal
      WHERE reversal.owner_id = adoption.owner_id AND reversal.mesh_id = adoption.mesh_id
        AND reversal.proposal_id = adoption.proposal_id AND reversal.kind = 'reversal'
  )
ORDER BY adoption.recorded_at ASC, adoption.event_id ASC LIMIT %s
""".strip()

_AWAITING_SQL: Final = f"""
SELECT {_FIELDS} FROM learning_ledger_entry AS proposal
WHERE proposal.owner_id = %s AND proposal.mesh_id = %s AND proposal.kind = 'proposal'
  AND NOT EXISTS (
      SELECT 1 FROM learning_ledger_entry AS decision
      WHERE decision.owner_id = proposal.owner_id AND decision.mesh_id = proposal.mesh_id
        AND decision.proposal_id = proposal.proposal_id AND decision.kind = 'adoption'
  )
ORDER BY proposal.recorded_at ASC, proposal.event_id ASC LIMIT %s
""".strip()


@dataclass(frozen=True, slots=True)
class LedgerEntryRecord:
    owner_id: str
    mesh_id: str
    event_id: str
    kind: str
    proposal_id: str
    item_kind: str
    content_hash: str
    scope: str
    review: str | None
    evidence: tuple[str, ...]
    recorded_at: int


@dataclass(frozen=True, slots=True)
class LearningItemRecord:
    proposal: LedgerEntryRecord
    adoption: LedgerEntryRecord | None
    reversal: LedgerEntryRecord | None

    @property
    def status(self) -> str:
        if self.reversal is not None:
            return "reversed"
        return "proposed" if self.adoption is None else "adopted"


def _entry(row: Mapping[str, Any]) -> LedgerEntryRecord:
    evidence = stored_json(row, "evidence")
    if not isinstance(evidence, tuple) or any(type(item) is not str for item in evidence):
        raise RepositoryDataError(
            "persisted ledger evidence is malformed", metadata={"field": "evidence"}
        )
    review = _row_value(row, "review")
    return LedgerEntryRecord(
        owner_id=str(_row_value(row, "owner_id")),
        mesh_id=str(_row_value(row, "mesh_id")),
        event_id=str(_row_value(row, "event_id")),
        kind=str(_row_value(row, "kind")),
        proposal_id=str(_row_value(row, "proposal_id")),
        item_kind=str(_row_value(row, "item_kind")),
        content_hash=str(_row_value(row, "content_hash")),
        scope=str(_row_value(row, "scope")),
        review=None if review is None else str(review),
        evidence=evidence,
        recorded_at=stored_integer(row, "recorded_at"),
    )


def _evidence(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RepositoryValidationError("evidence must be a list of event ids")
    events = tuple(hash_value(item, "evidence") for item in value)
    if not 1 <= len(events) <= _MAX_EVIDENCE or len(set(events)) != len(events):
        raise RepositoryValidationError(
            "evidence must hold distinct event ids within the bound",
            metadata={"maximum": _MAX_EVIDENCE},
        )
    return tuple(sorted(events))


def _choice(value: object, field: str, allowed: frozenset[str]) -> str:
    text = _bounded_text(value, field, maximum=32)
    if text not in allowed:
        raise RepositoryValidationError(f"{field} is not a supported value")
    return text


def _lock(transaction: Transaction, owner: str, mesh: str, proposal: str) -> None:
    transaction.fetch_one(_LOCK_SQL, (f"{owner}\x1f{mesh}\x1f{proposal}", _LOCK_SEED))


class LearningLedgerRepository:
    def record_proposal(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        event_id: str,
        item_kind: str,
        content_hash: str,
        scope: str,
        evidence: Sequence[str],
        recorded_at: int,
    ) -> LedgerEntryRecord:
        event = hash_value(event_id, "event_id")
        owner, mesh = _required_id(owner_id, "owner_id"), hash_value(mesh_id, "mesh_id")
        _lock(transaction, owner, mesh, event)
        return self._store(
            transaction,
            owner=owner,
            mesh=mesh,
            event=event,
            kind="proposal",
            proposal=event,
            item_kind=_choice(item_kind, "item_kind", _ITEM_KINDS),
            content_hash=hash_value(content_hash, "content_hash"),
            scope=_bounded_text(scope, "scope", maximum=512),
            review=None,
            evidence=_evidence(evidence),
            recorded=bounded_integer(recorded_at, "recorded_at"),
        )

    def record_adoption(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        event_id: str,
        proposal_id: str,
        scope: str,
        review: str,
        evidence: Sequence[str],
        recorded_at: int,
    ) -> LedgerEntryRecord:
        owner, mesh = _required_id(owner_id, "owner_id"), hash_value(mesh_id, "mesh_id")
        proposal = hash_value(proposal_id, "proposal_id")
        _lock(transaction, owner, mesh, proposal)
        item = self._item(transaction, owner, mesh, proposal)
        return self._store(
            transaction,
            owner=owner,
            mesh=mesh,
            event=hash_value(event_id, "event_id"),
            kind="adoption",
            proposal=proposal,
            item_kind=item.proposal.item_kind,
            content_hash=item.proposal.content_hash,
            scope=_bounded_text(scope, "scope", maximum=512),
            review=_choice(review, "review", _REVIEWS),
            evidence=_evidence(evidence),
            recorded=bounded_integer(recorded_at, "recorded_at"),
            prior=item,
        )

    def record_reversal(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        mesh_id: str,
        event_id: str,
        proposal_id: str,
        evidence: Sequence[str],
        recorded_at: int,
    ) -> LedgerEntryRecord:
        owner, mesh = _required_id(owner_id, "owner_id"), hash_value(mesh_id, "mesh_id")
        proposal = hash_value(proposal_id, "proposal_id")
        _lock(transaction, owner, mesh, proposal)
        item = self._item(transaction, owner, mesh, proposal)
        scope = item.adoption.scope if item.adoption is not None else item.proposal.scope
        return self._store(
            transaction,
            owner=owner,
            mesh=mesh,
            event=hash_value(event_id, "event_id"),
            kind="reversal",
            proposal=proposal,
            item_kind=item.proposal.item_kind,
            content_hash=item.proposal.content_hash,
            scope=scope,
            review=None,
            evidence=_evidence(evidence),
            recorded=bounded_integer(recorded_at, "recorded_at"),
            prior=item,
        )

    def _item(
        self, query: QueryExecutor, owner: str, mesh: str, proposal: str
    ) -> LearningItemRecord:
        found = self.get_item(query, owner_id=owner, mesh_id=mesh, proposal_id=proposal)
        if found is None:
            raise RepositoryNotFoundError(
                "proposal was not found", metadata={"operation": "ledger.record"}
            )
        return found

    def _store(
        self,
        transaction: Transaction,
        *,
        owner: str,
        mesh: str,
        event: str,
        kind: str,
        proposal: str,
        item_kind: str,
        content_hash: str,
        scope: str,
        review: str | None,
        evidence: tuple[str, ...],
        recorded: int,
        prior: LearningItemRecord | None = None,
    ) -> LedgerEntryRecord:
        existing = transaction.fetch_one(_BY_EVENT_SQL, (owner, mesh, event))
        if existing is not None:
            record = _entry(existing)
            expected = (kind, proposal, item_kind, content_hash, scope, review, evidence)
            observed = (
                record.kind,
                record.proposal_id,
                record.item_kind,
                record.content_hash,
                record.scope,
                record.review,
                record.evidence,
            )
            if observed != expected:
                raise RepositoryConflictError(
                    "ledger event identity was reused with different semantics",
                    metadata={"operation": "ledger.record"},
                )
            return record
        if (
            kind == "adoption"
            and prior is not None
            and (prior.reversal is not None or prior.adoption is not None)
        ):
            raise RepositoryConflictError(
                "proposal already has a decision", metadata={"reason": "already_decided"}
            )
        if kind == "reversal" and prior is not None:
            if prior.adoption is None:
                raise RepositoryConflictError(
                    "only an adopted item can be reversed", metadata={"reason": "not_adopted"}
                )
            if prior.reversal is not None:
                raise RepositoryConflictError(
                    "adoption is already reversed", metadata={"reason": "already_reversed"}
                )
        row = returned_row(
            transaction.execute(
                _INSERT_SQL,
                (
                    event,
                    kind,
                    proposal,
                    item_kind,
                    content_hash,
                    scope,
                    review,
                    _canonical_json(list(evidence), "evidence"),
                    recorded,
                    owner,
                    mesh,
                ),
            ),
            "ledger.record",
        )
        if row is None:
            raise RepositoryNotFoundError(
                "mesh was not found", metadata={"operation": "ledger.record"}
            )
        return _entry(row)

    def get_entry(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, event_id: str
    ) -> LedgerEntryRecord | None:
        row = query.fetch_one(
            _BY_EVENT_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                hash_value(event_id, "event_id"),
            ),
        )
        return None if row is None else _entry(row)

    def get_item(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, proposal_id: str
    ) -> LearningItemRecord | None:
        rows = query.fetch_all(
            _BY_PROPOSAL_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                hash_value(proposal_id, "proposal_id"),
            ),
        )
        entries = {record.kind: record for record in map(_entry, rows)}
        proposal = entries.get("proposal")
        if proposal is None:
            return None
        return LearningItemRecord(proposal, entries.get("adoption"), entries.get("reversal"))

    def list_adopted(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        mesh_id: str,
        scope: str | None = None,
        limit: int = 200,
    ) -> tuple[LedgerEntryRecord, ...]:
        scope_value = None if scope is None else _bounded_text(scope, "scope", maximum=512)
        rows = query.fetch_all(
            _ADOPTED_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                scope_value,
                scope_value,
                _bounded_limit(limit, maximum=_MAX_PAGE),
            ),
        )
        return tuple(_entry(row) for row in rows)

    def list_awaiting_decision(
        self, query: QueryExecutor, *, owner_id: str, mesh_id: str, limit: int = 200
    ) -> tuple[LedgerEntryRecord, ...]:
        rows = query.fetch_all(
            _AWAITING_SQL,
            (
                _required_id(owner_id, "owner_id"),
                hash_value(mesh_id, "mesh_id"),
                _bounded_limit(limit, maximum=_MAX_PAGE),
            ),
        )
        return tuple(_entry(row) for row in rows)


__all__ = ("LearningItemRecord", "LearningLedgerRepository", "LedgerEntryRecord")
