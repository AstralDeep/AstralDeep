"""Append-only, hash-chained audit-event persistence with a per-owner descending page
cursor and chain verification, plus owner-scoped conversation range reads and
verifiable chain-segment proofs. Used by AstralDeep's audit/repository.py and
audit_retention.py for tamper-evident event history and chat exports.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from astralplane.contracts import QueryExecutor, Record, Transaction
from astralplane.errors import PlaneError

GENESIS_DIGEST = bytes(32)
AuditAuthenticator = Callable[[str, bytes], bytes]
AUDIT_TURN_METADATA_KEY: Final = "turn_id"
AUDIT_CONVERSATION_ID_MAX_LENGTH: Final = 256
AUDIT_CONVERSATION_READ_MAX_EVENTS: Final = 2000


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    chain_id: str
    auth_principal: str
    agent_id: str | None
    event_class: str
    action_type: str
    description: str
    conversation_id: str | None
    correlation_id: str
    outcome: str
    outcome_detail: str | None
    inputs_json: str
    outputs_json: str
    artifact_pointers_json: str
    started_at: datetime
    completed_at: datetime | None
    key_id: str
    schema_version: int = 2

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("event_id", self.event_id, 128),
            ("chain_id", self.chain_id, 512),
            ("auth_principal", self.auth_principal, 512),
            ("event_class", self.event_class, 128),
            ("action_type", self.action_type, 128),
            ("description", self.description, 1024),
            ("correlation_id", self.correlation_id, 128),
            ("key_id", self.key_id, 128),
        ):
            _required(name, value, maximum)
        if self.outcome not in {"in_progress", "success", "failure", "interrupted"}:
            raise ValueError("audit outcome is not supported")
        if self.schema_version not in {1, 2}:
            raise ValueError("schema_version must be 1 or 2")
        _aware("started_at", self.started_at)
        if self.completed_at is not None:
            _aware("completed_at", self.completed_at)
            if self.completed_at < self.started_at:
                raise ValueError("completed_at cannot precede started_at")
        object.__setattr__(
            self,
            "inputs_json",
            canonical_json(self.inputs_json, expected_type=dict),
        )
        object.__setattr__(
            self,
            "outputs_json",
            canonical_json(self.outputs_json, expected_type=dict),
        )
        object.__setattr__(
            self,
            "artifact_pointers_json",
            canonical_json(self.artifact_pointers_json, expected_type=list),
        )


@dataclass(frozen=True, slots=True)
class AuditRecord:
    event: AuditEvent
    sequence: int
    recorded_at: datetime
    previous_digest: bytes
    entry_digest: bytes

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("audit sequence must be positive")
        _aware("recorded_at", self.recorded_at)
        _digest_bytes("previous_digest", self.previous_digest)
        _digest_bytes("entry_digest", self.entry_digest)


@dataclass(frozen=True, slots=True)
class AuditCursor:
    recorded_at: datetime
    event_id: str

    def __post_init__(self) -> None:
        _aware("recorded_at", self.recorded_at)
        _required("event_id", self.event_id, 128)


@dataclass(frozen=True, slots=True)
class AuditPage:
    records: tuple[AuditRecord, ...]
    next_cursor: AuditCursor | None


@dataclass(frozen=True, slots=True)
class AuditSegmentRun:
    anchor_digest: bytes
    records: tuple[AuditRecord, ...]

    def __post_init__(self) -> None:
        _digest_bytes("anchor_digest", self.anchor_digest)
        if not isinstance(self.records, tuple) or not self.records:
            raise ValueError("audit segment run must contain at least one record")
        for record in self.records:
            if not isinstance(record, AuditRecord):
                raise ValueError("audit segment run records must be AuditRecord values")
        if self.records[0].previous_digest != self.anchor_digest:
            raise ValueError("audit segment run anchor must match its first record")
        expected_sequence = self.records[0].sequence
        for record in self.records:
            if record.sequence != expected_sequence:
                raise ValueError("audit segment run records must be chain-contiguous")
            expected_sequence += 1


@dataclass(frozen=True, slots=True)
class AuditSegmentProof:
    chain_id: str
    runs: tuple[AuditSegmentRun, ...]
    key_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required("chain_id", self.chain_id, 512)
        if not isinstance(self.runs, tuple):
            raise ValueError("audit segment proof runs must be a tuple")
        previous_last_sequence = 0
        key_ids: list[str] = []
        for run in self.runs:
            if not isinstance(run, AuditSegmentRun):
                raise ValueError("audit segment proof runs must be AuditSegmentRun values")
            if run.records[0].sequence <= previous_last_sequence:
                raise ValueError("audit segment proof runs must ascend without overlap")
            previous_last_sequence = run.records[-1].sequence
            for record in run.records:
                if record.event.chain_id != self.chain_id:
                    raise ValueError("audit segment proof records must share the chain id")
                key_ids.append(record.event.key_id)
        object.__setattr__(self, "key_ids", tuple(dict.fromkeys(key_ids)))


@dataclass(frozen=True, slots=True)
class ToolTrajectoryEvent:
    agent_id: str
    correlation_id: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class ChainVerification:
    valid: bool
    first_invalid_event_id: str | None
    reason: str | None
    last_sequence: int
    last_digest: bytes


class AuditRepository:
    def append(
        self,
        transaction: Transaction,
        event: AuditEvent,
        authenticate: AuditAuthenticator,
    ) -> AuditRecord:
        transaction.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtext(%s)) AS locked",
            (f"audit_events:{event.chain_id}",),
        )
        existing = transaction.fetch_one(
            """
            SELECT * FROM audit_events
            WHERE actor_user_id = %s AND event_id = %s
            """,
            (event.chain_id, event.event_id),
        )
        if existing is not None:
            record = _record(existing)
            expected = _authenticate(
                authenticate,
                record.event.key_id,
                record.previous_digest + canonical_event_bytes(event, record.sequence),
            )
            if record.event != event or record.entry_digest != expected:
                raise PlaneError(
                    "audit event identity has conflicting semantics",
                    code="audit_idempotency_conflict",
                    metadata={"chain_id": event.chain_id},
                )
            return record

        previous = transaction.fetch_one(
            """
            SELECT chain_sequence, entry_hash FROM audit_events
            WHERE actor_user_id = %s
            ORDER BY chain_sequence DESC LIMIT 1
            """,
            (event.chain_id,),
        )
        sequence = 1 if previous is None else int(previous["chain_sequence"]) + 1
        previous_digest = GENESIS_DIGEST if previous is None else _bytes(previous["entry_hash"])
        entry_digest = _authenticate(
            authenticate,
            event.key_id,
            previous_digest + canonical_event_bytes(event, sequence),
        )
        row = transaction.fetch_one(
            """
            INSERT INTO audit_events (
                event_id, actor_user_id, auth_principal, agent_id, event_class,
                action_type, description, conversation_id, correlation_id,
                outcome, outcome_detail, inputs_meta, outputs_meta,
                artifact_pointers, started_at, completed_at, chain_sequence,
                prev_hash, entry_hash, key_id, schema_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING *
            """,
            (
                event.event_id,
                event.chain_id,
                event.auth_principal,
                event.agent_id,
                event.event_class,
                event.action_type,
                event.description,
                event.conversation_id,
                event.correlation_id,
                event.outcome,
                event.outcome_detail,
                canonical_json(event.inputs_json, expected_type=dict),
                canonical_json(event.outputs_json, expected_type=dict),
                canonical_json(event.artifact_pointers_json, expected_type=list),
                event.started_at,
                event.completed_at,
                sequence,
                previous_digest,
                entry_digest,
                event.key_id,
                event.schema_version,
            ),
        )
        if row is None:
            raise PlaneError(
                "audit append did not return its durable row",
                code="audit_append_failed",
                metadata={"chain_id": event.chain_id},
            )
        record = _record(row)
        if (
            record.sequence != sequence
            or record.previous_digest != previous_digest
            or record.entry_digest != entry_digest
        ):
            raise PlaneError(
                "audit append returned inconsistent chain evidence",
                code="audit_append_inconsistent",
                metadata={"chain_id": event.chain_id},
            )
        return record

    def get(self, transaction: Transaction, *, chain_id: str, event_id: str) -> AuditRecord | None:
        row = transaction.fetch_one(
            "SELECT * FROM audit_events WHERE actor_user_id = %s AND event_id = %s",
            (chain_id, event_id),
        )
        return None if row is None else _record(row)

    def list_for_chain(
        self,
        transaction: Transaction,
        *,
        chain_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> tuple[AuditRecord, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        rows = transaction.fetch_all(
            """
            SELECT * FROM audit_events
            WHERE actor_user_id = %s AND chain_sequence > %s
            ORDER BY chain_sequence, event_id LIMIT %s
            """,
            (chain_id, after_sequence, limit),
        )
        return tuple(_record(row) for row in rows)

    def list_page(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        event_classes: Sequence[str] | None = None,
        outcomes: Sequence[str] | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        keyword: str | None = None,
        conversation_id: str | None = None,
        cursor: AuditCursor | None = None,
        limit: int = 50,
    ) -> AuditPage:
        _required("owner_id", owner_id, 512)
        if conversation_id is not None:
            _bounded_identifier("conversation_id", conversation_id)
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        classes = _bounded_filters("event_classes", event_classes)
        selected_outcomes = _bounded_filters("outcomes", outcomes)
        unsupported = set(selected_outcomes).difference(
            {"in_progress", "success", "failure", "interrupted"}
        )
        if unsupported:
            raise ValueError("outcomes contains an unsupported value")
        if from_ts is not None:
            _aware("from_ts", from_ts)
        if to_ts is not None:
            _aware("to_ts", to_ts)
        if from_ts is not None and to_ts is not None and from_ts >= to_ts:
            raise ValueError("from_ts must precede to_ts")
        if cursor is not None and not isinstance(cursor, AuditCursor):
            raise ValueError("cursor must be an AuditCursor")

        clauses = ["actor_user_id = %s"]
        parameters: list[object] = [owner_id]
        if conversation_id is not None:
            clauses.append("conversation_id = %s")
            parameters.append(conversation_id)
        if cursor is not None:
            clauses.append("(recorded_at, event_id) < (%s, %s)")
            parameters.extend((cursor.recorded_at, cursor.event_id))
        if classes:
            clauses.append("event_class = ANY(%s)")
            parameters.append(list(classes))
        if selected_outcomes:
            clauses.append("outcome = ANY(%s)")
            parameters.append(list(selected_outcomes))
        if from_ts is not None:
            clauses.append("recorded_at >= %s")
            parameters.append(from_ts)
        if to_ts is not None:
            clauses.append("recorded_at < %s")
            parameters.append(to_ts)
        if keyword is not None:
            normalized = _required_keyword(keyword)
            escaped = normalized.casefold().replace("\\", "\\\\").replace("%", "\\%")
            escaped = escaped.replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(LOWER(description) LIKE %s ESCAPE E'\\\\' "
                "OR LOWER(action_type) LIKE %s ESCAPE E'\\\\')"
            )
            parameters.extend((pattern, pattern))
        parameters.append(limit + 1)
        rows = query.fetch_all(
            f"""
            SELECT * FROM audit_events
            WHERE {' AND '.join(clauses)}
            ORDER BY recorded_at DESC, event_id DESC
            LIMIT %s
            """,
            tuple(parameters),
        )
        records = tuple(_record(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            last = records[-1]
            next_cursor = AuditCursor(
                recorded_at=last.recorded_at,
                event_id=last.event.event_id,
            )
        return AuditPage(records=records, next_cursor=next_cursor)

    def list_conversation_range(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        conversation_id: str,
        turn_id: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        cursor: AuditCursor | None = None,
        limit: int = 200,
        max_events: int = AUDIT_CONVERSATION_READ_MAX_EVENTS,
    ) -> AuditPage:
        _required("owner_id", owner_id, 512)
        _bounded_identifier("conversation_id", conversation_id)
        if turn_id is not None:
            _bounded_identifier("turn_id", turn_id)
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if not 1 <= max_events <= AUDIT_CONVERSATION_READ_MAX_EVENTS:
            raise ValueError(
                f"max_events must be between 1 and {AUDIT_CONVERSATION_READ_MAX_EVENTS}"
            )
        if from_ts is not None:
            _aware("from_ts", from_ts)
        if to_ts is not None:
            _aware("to_ts", to_ts)
        if from_ts is not None and to_ts is not None and from_ts >= to_ts:
            raise ValueError("from_ts must precede to_ts")
        if cursor is not None and not isinstance(cursor, AuditCursor):
            raise ValueError("cursor must be an AuditCursor")

        clauses, parameters = _conversation_clauses(
            owner_id=owner_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            from_ts=from_ts,
            to_ts=to_ts,
            cursor=cursor,
        )
        matched = query.fetch_one(
            f"SELECT count(*) AS matched FROM audit_events WHERE {' AND '.join(clauses)}",
            tuple(parameters),
        )
        count = 0 if matched is None else int(matched["matched"])
        if count > max_events:
            raise PlaneError(
                "audit conversation range exceeds its bounded read size",
                code="audit_range_exceeds_bound",
                metadata={"bound": max_events, "matched": count},
            )
        rows = query.fetch_all(
            f"""
            SELECT * FROM audit_events
            WHERE {' AND '.join(clauses)}
            ORDER BY recorded_at DESC, event_id DESC
            LIMIT %s
            """,
            (*parameters, limit + 1),
        )
        records = tuple(_record(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            last = records[-1]
            next_cursor = AuditCursor(
                recorded_at=last.recorded_at,
                event_id=last.event.event_id,
            )
        return AuditPage(records=records, next_cursor=next_cursor)

    def load_chain(
        self,
        transaction: Transaction,
        *,
        chain_id: str,
        start_sequence: int = 1,
    ) -> tuple[AuditRecord, ...]:
        if start_sequence <= 0:
            raise ValueError("start_sequence must be positive")
        rows = transaction.fetch_all(
            """
            SELECT * FROM audit_events
            WHERE actor_user_id = %s AND chain_sequence >= %s
            ORDER BY chain_sequence, event_id
            """,
            (chain_id, start_sequence),
        )
        return tuple(_record(row) for row in rows)

    def load_conversation_segment(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        turn_id: str | None = None,
        max_events: int = AUDIT_CONVERSATION_READ_MAX_EVENTS,
    ) -> AuditSegmentProof:
        _required("owner_id", owner_id, 512)
        _bounded_identifier("conversation_id", conversation_id)
        if turn_id is not None:
            _bounded_identifier("turn_id", turn_id)
        if not 1 <= max_events <= AUDIT_CONVERSATION_READ_MAX_EVENTS:
            raise ValueError(
                f"max_events must be between 1 and {AUDIT_CONVERSATION_READ_MAX_EVENTS}"
            )

        clauses, parameters = _conversation_clauses(
            owner_id=owner_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        rows = transaction.fetch_all(
            f"""
            SELECT * FROM audit_events
            WHERE {' AND '.join(clauses)}
            ORDER BY chain_sequence, event_id
            LIMIT %s
            """,
            (*parameters, max_events + 1),
        )
        if len(rows) > max_events:
            raise PlaneError(
                "audit conversation segment exceeds its bounded read size",
                code="audit_segment_exceeds_bound",
                metadata={"bound": max_events},
            )
        records = tuple(_record(row) for row in rows)
        runs: list[AuditSegmentRun] = []
        current: list[AuditRecord] = []
        for record in records:
            if current and record.sequence != current[-1].sequence + 1:
                runs.append(
                    AuditSegmentRun(
                        anchor_digest=current[0].previous_digest,
                        records=tuple(current),
                    )
                )
                current = []
            current.append(record)
        if current:
            runs.append(
                AuditSegmentRun(anchor_digest=current[0].previous_digest, records=tuple(current))
            )
        return AuditSegmentProof(chain_id=owner_id, runs=tuple(runs))

    def list_tool_trajectory_events_for_administration(
        self,
        query: QueryExecutor,
        *,
        from_ts: datetime,
        to_ts: datetime,
        limit: int = 2000,
    ) -> tuple[ToolTrajectoryEvent, ...]:
        _aware("from_ts", from_ts)
        _aware("to_ts", to_ts)
        if from_ts > to_ts:
            raise ValueError("from_ts cannot follow to_ts")
        if not 1 <= limit <= 2000:
            raise ValueError("limit must be between 1 and 2000")
        rows = query.fetch_all(
            """
            SELECT agent_id, correlation_id,
                   REPLACE(REPLACE(action_type, 'tool.', ''), '.end', '') AS tool_name
            FROM audit_events
            WHERE event_class = 'agent_tool_call'
              AND action_type LIKE 'tool.%.end'
              AND agent_id IS NOT NULL
              AND recorded_at >= %s AND recorded_at <= %s
            ORDER BY agent_id, correlation_id, recorded_at, event_id
            LIMIT %s
            """,
            (from_ts, to_ts, limit),
        )
        events: list[ToolTrajectoryEvent] = []
        for row in rows:
            agent_id = str(row.get("agent_id") or "")
            correlation_id = str(row.get("correlation_id") or "")
            tool_name = str(row.get("tool_name") or "")
            _required("persisted agent_id", agent_id, 512)
            _required("persisted correlation_id", correlation_id, 128)
            _required("persisted tool_name", tool_name, 256)
            events.append(
                ToolTrajectoryEvent(
                    agent_id=agent_id,
                    correlation_id=correlation_id,
                    tool_name=tool_name,
                )
            )
        return tuple(events)

    def verify_chain(
        self,
        transaction: Transaction,
        *,
        chain_id: str,
        authenticate: AuditAuthenticator,
        start_sequence: int = 1,
        expected_previous_digest: bytes = GENESIS_DIGEST,
    ) -> ChainVerification:
        _digest_bytes("expected_previous_digest", expected_previous_digest)
        records = self.load_chain(
            transaction,
            chain_id=chain_id,
            start_sequence=start_sequence,
        )
        return verify_records(
            records,
            chain_id=chain_id,
            authenticate=authenticate,
            start_sequence=start_sequence,
            expected_previous_digest=expected_previous_digest,
        )


def verify_records(
    records: Sequence[AuditRecord],
    *,
    chain_id: str,
    authenticate: AuditAuthenticator,
    start_sequence: int = 1,
    expected_previous_digest: bytes = GENESIS_DIGEST,
) -> ChainVerification:
    _digest_bytes("expected_previous_digest", expected_previous_digest)
    expected_sequence = start_sequence
    previous_digest = expected_previous_digest
    for record in records:
        reason: str | None = None
        if record.event.chain_id != chain_id:
            reason = "wrong_chain"
        elif record.sequence != expected_sequence:
            reason = "non_contiguous_sequence"
        elif record.previous_digest != previous_digest:
            reason = "previous_digest_mismatch"
        else:
            expected_entry = _authenticate(
                authenticate,
                record.event.key_id,
                previous_digest + canonical_event_bytes(record.event, record.sequence),
            )
            if record.entry_digest != expected_entry:
                reason = "entry_digest_mismatch"
        if reason is not None:
            return ChainVerification(
                valid=False,
                first_invalid_event_id=record.event.event_id,
                reason=reason,
                last_sequence=expected_sequence - 1,
                last_digest=previous_digest,
            )
        expected_sequence += 1
        previous_digest = record.entry_digest
    return ChainVerification(
        valid=True,
        first_invalid_event_id=None,
        reason=None,
        last_sequence=expected_sequence - 1,
        last_digest=previous_digest,
    )


def verify_segment_proof(
    proof: AuditSegmentProof,
    *,
    authenticate: AuditAuthenticator,
) -> ChainVerification:
    if not isinstance(proof, AuditSegmentProof):
        raise ValueError("proof must be an AuditSegmentProof")
    if not proof.runs:
        return ChainVerification(
            valid=True,
            first_invalid_event_id=None,
            reason=None,
            last_sequence=0,
            last_digest=GENESIS_DIGEST,
        )
    for run in proof.runs:
        result = verify_records(
            run.records,
            chain_id=proof.chain_id,
            authenticate=authenticate,
            start_sequence=run.records[0].sequence,
            expected_previous_digest=run.anchor_digest,
        )
        if not result.valid:
            return result
    last_run = proof.runs[-1]
    last_record = last_run.records[-1]
    return ChainVerification(
        valid=True,
        first_invalid_event_id=None,
        reason=None,
        last_sequence=last_record.sequence,
        last_digest=last_record.entry_digest,
    )


def canonical_event_bytes(event: AuditEvent, sequence: int) -> bytes:
    if sequence <= 0:
        raise ValueError("audit sequence must be positive")
    if event.schema_version == 1:
        return _canonical_event_v1(event)
    if event.schema_version != 2:
        raise ValueError("schema_version must be 1 or 2")
    canonical = {
        "schema_version": event.schema_version,
        "chain_sequence": sequence,
        "event_id": event.event_id,
        "actor_user_id": event.chain_id,
        "auth_principal": event.auth_principal,
        "agent_id": event.agent_id,
        "event_class": event.event_class,
        "action_type": event.action_type,
        "description": event.description,
        "conversation_id": event.conversation_id,
        "correlation_id": event.correlation_id,
        "outcome": event.outcome,
        "outcome_detail": event.outcome_detail,
        "inputs_meta": json.loads(event.inputs_json),
        "outputs_meta": json.loads(event.outputs_json),
        "artifact_pointers": json.loads(event.artifact_pointers_json),
        "started_at": _utc_iso(event.started_at),
        "completed_at": None if event.completed_at is None else _utc_iso(event.completed_at),
    }
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _canonical_event_v1(event: AuditEvent) -> bytes:
    canonical = {
        "schema_version": 1,
        "event_id": event.event_id,
        "actor_user_id": event.chain_id,
        "auth_principal": event.auth_principal,
        "agent_id": event.agent_id,
        "event_class": event.event_class,
        "action_type": event.action_type,
        "description": event.description,
        "conversation_id": event.conversation_id,
        "correlation_id": event.correlation_id,
        "outcome": event.outcome,
        "outcome_detail": event.outcome_detail,
        "inputs_meta": json.loads(event.inputs_json),
        "outputs_meta": json.loads(event.outputs_json),
        "artifact_pointers": json.loads(event.artifact_pointers_json),
        "started_at": event.started_at.astimezone(UTC).isoformat(),
        "completed_at": (
            None if event.completed_at is None else event.completed_at.astimezone(UTC).isoformat()
        ),
    }
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def canonical_json(value: str | Mapping[str, Any] | Sequence[Any], *, expected_type: type) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        if expected_type is dict:
            if not isinstance(parsed, Mapping):
                raise TypeError
        elif expected_type is list:
            if not isinstance(parsed, (list, tuple)):
                raise TypeError
        else:
            raise TypeError
        normalized = _normalize_json_value(parsed)
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"audit JSON must contain a {expected_type.__name__}") from exc


def _normalize_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized[key] = _normalize_json_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    raise TypeError("value is not JSON-compatible")


def _conversation_clauses(
    *,
    owner_id: str,
    conversation_id: str,
    turn_id: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    cursor: AuditCursor | None = None,
) -> tuple[list[str], list[object]]:
    clauses = ["actor_user_id = %s", "conversation_id = %s"]
    parameters: list[object] = [owner_id, conversation_id]
    if turn_id is not None:
        clauses.append("(inputs_meta->>%s = %s OR outputs_meta->>%s = %s)")
        parameters.extend(
            (AUDIT_TURN_METADATA_KEY, turn_id, AUDIT_TURN_METADATA_KEY, turn_id)
        )
    if from_ts is not None:
        clauses.append("recorded_at >= %s")
        parameters.append(from_ts)
    if to_ts is not None:
        clauses.append("recorded_at < %s")
        parameters.append(to_ts)
    if cursor is not None:
        clauses.append("(recorded_at, event_id) < (%s, %s)")
        parameters.extend((cursor.recorded_at, cursor.event_id))
    return clauses, parameters


def _record(row: Record) -> AuditRecord:
    event = AuditEvent(
        event_id=str(row["event_id"]),
        chain_id=str(row["actor_user_id"]),
        auth_principal=str(row["auth_principal"]),
        agent_id=None if row.get("agent_id") is None else str(row["agent_id"]),
        event_class=str(row["event_class"]),
        action_type=str(row["action_type"]),
        description=str(row["description"]),
        conversation_id=(
            None if row.get("conversation_id") is None else str(row["conversation_id"])
        ),
        correlation_id=str(row["correlation_id"]),
        outcome=str(row["outcome"]),
        outcome_detail=(None if row.get("outcome_detail") is None else str(row["outcome_detail"])),
        inputs_json=canonical_json(row.get("inputs_meta") or {}, expected_type=dict),
        outputs_json=canonical_json(row.get("outputs_meta") or {}, expected_type=dict),
        artifact_pointers_json=canonical_json(
            row.get("artifact_pointers") or [], expected_type=list
        ),
        started_at=row["started_at"],
        completed_at=row.get("completed_at"),
        key_id=str(row["key_id"]),
        schema_version=int(row["schema_version"]),
    )
    return AuditRecord(
        event=event,
        sequence=int(row["chain_sequence"]),
        recorded_at=row["recorded_at"],
        previous_digest=_bytes(row["prev_hash"]),
        entry_digest=_bytes(row["entry_hash"]),
    )


def _authenticate(authenticate: AuditAuthenticator, key_id: str, payload: bytes) -> bytes:
    digest = bytes(authenticate(key_id, payload))
    _digest_bytes("audit authenticator result", digest)
    return digest


def _bytes(value: object) -> bytes:
    try:
        return bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("audit digest is not byte-compatible") from exc


def _digest_bytes(name: str, value: bytes) -> None:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must contain 32 bytes")


def _required(name: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")


def _bounded_filters(name: str, values: Sequence[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or len(values) > 32:
        raise ValueError(f"{name} must contain at most 32 values")
    result = tuple(values)
    for value in result:
        _required(name, value, 128)
    return result


def _required_keyword(value: str) -> str:
    _required("keyword", value, 256)
    return value.strip()


def _bounded_identifier(name: str, value: str) -> str:
    _required(name, value, AUDIT_CONVERSATION_ID_MAX_LENGTH)
    if "\x00" in value:
        raise ValueError(f"{name} must not contain null characters")
    return value


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = (
    "AUDIT_CONVERSATION_ID_MAX_LENGTH",
    "AUDIT_CONVERSATION_READ_MAX_EVENTS",
    "AUDIT_TURN_METADATA_KEY",
    "GENESIS_DIGEST",
    "AuditAuthenticator",
    "AuditCursor",
    "AuditEvent",
    "AuditPage",
    "AuditRecord",
    "AuditRepository",
    "AuditSegmentProof",
    "AuditSegmentRun",
    "ChainVerification",
    "ToolTrajectoryEvent",
    "canonical_event_bytes",
    "canonical_json",
    "verify_records",
    "verify_segment_proof",
)
