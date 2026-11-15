"""System-wide interaction, quality-signal, quarantine, and knowledge-proposal state;
not owner-partitioned, so callers apply their own admin authorization. Used by
AstralDeep's feedback/repository.py for quality evaluation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

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
    _non_negative_int,
    _positive_int,
    _required_id,
    _row_value,
    _single_returned,
    _structured_json,
)

_QUALITY_STATUSES = frozenset({"healthy", "insufficient-data", "underperforming"})
_DETECTORS = frozenset({"inline", "loop_pre_pass"})
_QUARANTINE_STATUSES = frozenset({"held", "released", "dismissed"})
_PROPOSAL_STATUSES = frozenset(
    {"pending", "accepted", "applied", "rejected", "superseded"}
)


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class QuarantineStatus(StrEnum):
    HELD = "held"
    RELEASED = "released"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class InteractionRecord:
    interaction_id: int
    agent_id: str
    tool_name: str
    success: bool
    error_message: str | None = field(repr=False)
    response_time_ms: int | None
    conversation_id: str | None = field(repr=False)
    synthesized: bool
    created_at: int | None


@dataclass(frozen=True, slots=True)
class InteractionStatsRecord:
    agent_id: str
    tool_name: str
    total_calls: int
    success_count: int
    average_response_ms: float | None


@dataclass(frozen=True, slots=True)
class QualitySignalRecord:
    signal_id: str
    agent_id: str
    tool_name: str
    window_start: datetime
    window_end: datetime
    dispatch_count: int
    failure_count: int
    negative_feedback_count: int
    failure_rate: float
    negative_feedback_rate: float
    status: str
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class QualityAggregateRecord:
    agent_id: str
    tool_name: str
    dispatch_count: int
    failure_count: int
    negative_feedback_count: int


@dataclass(frozen=True, slots=True)
class QualityEvidenceRecord:
    audit_event_ids: tuple[str, ...]
    feedback_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CleanCommentSample:
    feedback_id: str
    category: str
    comment: str = field(repr=False)
    created_at: datetime = field(repr=False)


@dataclass(frozen=True, slots=True)
class QuarantineEntryRecord:
    feedback_id: str
    reason: str
    detector: str
    detected_at: datetime
    status: QuarantineStatus
    actor_user_id: str | None
    actioned_at: datetime | None


@dataclass(frozen=True, slots=True)
class QuarantineReviewRecord:
    entry: QuarantineEntryRecord
    owner_id: str = field(repr=False)
    source_agent: str | None = None
    source_tool: str | None = None
    comment: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class KnowledgeProposalRecord:
    proposal_id: str
    agent_id: str
    tool_name: str
    artifact_path: str = field(repr=False)
    diff_payload: str = field(repr=False)
    artifact_sha_at_generation: str
    evidence: Mapping[str, Any] = field(repr=False)
    status: ProposalStatus
    reviewer_user_id: str | None
    reviewed_at: datetime | None
    reviewer_rationale: str | None = field(repr=False)
    applied_at: datetime | None
    generated_at: datetime


class InteractionRepository:
    _FIELDS = (
        "id, agent_id, tool_name, success, error_message, response_time_ms, "
        "chat_id, synthesized, created_at"
    )

    def record_for_owner(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        agent_id: str,
        tool_name: str,
        success: bool,
        error_message: str | None,
        response_time_ms: int | None,
        created_at: int,
    ) -> InteractionRecord:
        owner = _required_id(owner_id, "owner_id")
        conversation = _required_id(conversation_id, "conversation_id")
        values = _interaction_values(
            agent_id=agent_id,
            tool_name=tool_name,
            success=success,
            error_message=error_message,
            response_time_ms=response_time_ms,
            created_at=created_at,
        )
        result = transaction.execute(
            f"""
            INSERT INTO interaction_log (
                agent_id, tool_name, success, error_message, response_time_ms,
                chat_id, synthesized, created_at
            )
            SELECT %s, %s, %s, %s, %s, chat.id, FALSE, %s
              FROM chats AS chat
             WHERE chat.id = %s AND chat.user_id = %s
            RETURNING {self._FIELDS}
            """,
            (*values, conversation, owner),
        )
        if not getattr(result, "returned_records", ()):
            raise RepositoryNotFoundError("owner-scoped conversation was not found")
        return _interaction(_single_returned(result, "knowledge.interaction.record_owner"))

    def record_for_administration(
        self,
        transaction: Transaction,
        *,
        agent_id: str,
        tool_name: str,
        success: bool,
        error_message: str | None,
        response_time_ms: int | None,
        created_at: int,
    ) -> InteractionRecord:
        values = _interaction_values(
            agent_id=agent_id,
            tool_name=tool_name,
            success=success,
            error_message=error_message,
            response_time_ms=response_time_ms,
            created_at=created_at,
        )
        result = transaction.execute(
            f"""
            INSERT INTO interaction_log (
                agent_id, tool_name, success, error_message, response_time_ms,
                chat_id, synthesized, created_at
            ) VALUES (%s, %s, %s, %s, %s, NULL, FALSE, %s)
            RETURNING {self._FIELDS}
            """,
            values,
        )
        return _interaction(_single_returned(result, "knowledge.interaction.record_admin"))

    def list_unsynthesized_for_administration(
        self,
        query: QueryExecutor,
        *,
        limit: int = 500,
    ) -> tuple[InteractionRecord, ...]:
        maximum = _bounded_limit(limit, maximum=2000)
        rows = query.fetch_all(
            f"SELECT {self._FIELDS} FROM interaction_log "
            "WHERE synthesized = FALSE "
            "ORDER BY created_at ASC NULLS FIRST, id ASC LIMIT %s",
            (maximum,),
        )
        return tuple(_interaction(row) for row in rows)

    def get_many_for_administration(
        self,
        query: QueryExecutor,
        *,
        interaction_ids: Sequence[int],
    ) -> tuple[InteractionRecord, ...]:
        identifiers = _positive_ids(interaction_ids, "interaction_ids", maximum=2000)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM interaction_log
            WHERE id = ANY(%s)
            ORDER BY array_position(%s::bigint[], id)
            """,
            (list(identifiers), list(identifiers)),
        )
        records = tuple(_interaction(row) for row in rows)
        observed = tuple(record.interaction_id for record in records)
        if observed != identifiers:
            raise RepositoryNotFoundError("interaction set was incomplete or out of order")
        return records

    def mark_synthesized_for_administration(
        self,
        transaction: Transaction,
        *,
        interaction_ids: Sequence[int],
    ) -> tuple[int, ...]:
        identifiers = _positive_ids(interaction_ids, "interaction_ids", maximum=2000)
        transaction.execute(
            "UPDATE interaction_log SET synthesized = TRUE "
            "WHERE id = ANY(%s) AND synthesized = FALSE",
            (list(identifiers),),
        )
        rows = transaction.fetch_all(
            "SELECT id, synthesized FROM interaction_log WHERE id = ANY(%s) ORDER BY id",
            (list(identifiers),),
        )
        observed = tuple(_positive_int(_row_value(row, "id"), "interaction_id") for row in rows)
        if observed != tuple(sorted(identifiers)) or any(
            _stored_bool(_row_value(row, "synthesized"), "synthesized") is False
            for row in rows
        ):
            raise RepositoryNotFoundError("interaction synthesis set was incomplete")
        return observed

    def stats_for_administration(
        self,
        query: QueryExecutor,
        *,
        agent_id: str | None = None,
        limit: int = 500,
    ) -> tuple[InteractionStatsRecord, ...]:
        maximum = _bounded_limit(limit, maximum=2000)
        parameters: tuple[object, ...]
        where = ""
        if agent_id is None:
            parameters = (maximum,)
        else:
            agent = _required_id(agent_id, "agent_id")
            where = " WHERE agent_id = %s"
            parameters = (agent, maximum)
        rows = query.fetch_all(
            "SELECT agent_id, tool_name, COUNT(*) AS total_calls, "
            "COUNT(*) FILTER (WHERE success) AS success_count, "
            "AVG(response_time_ms) AS avg_response_ms FROM interaction_log"
            + where
            + " GROUP BY agent_id, tool_name ORDER BY agent_id, tool_name LIMIT %s",
            parameters,
        )
        return tuple(_interaction_stats(row) for row in rows)


class QualitySignalRepository:
    _FIELDS = (
        "id, agent_id, tool_name, window_start, window_end, dispatch_count, "
        "failure_count, negative_feedback_count, failure_rate, "
        "negative_feedback_rate, status, computed_at"
    )

    def put_for_administration(
        self,
        transaction: Transaction,
        record: QualitySignalRecord,
        *,
        expected_computed_at: datetime | None = None,
    ) -> QualitySignalRecord:
        signal = _validated_quality(record)
        result = transaction.execute(
            f"""
            INSERT INTO tool_quality_signal (
                id, agent_id, tool_name, window_start, window_end,
                dispatch_count, failure_count, negative_feedback_count,
                failure_rate, negative_feedback_rate, status, computed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (agent_id, tool_name, window_end) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            _quality_parameters(signal),
        )
        if getattr(result, "returned_records", ()):
            return _quality(_single_returned(result, "knowledge.quality.create"))
        existing = self.latest_for_administration(
            transaction,
            agent_id=signal.agent_id,
            tool_name=signal.tool_name,
            window_end=signal.window_end,
        )
        if existing is None:
            raise RepositoryConflictError("quality signal identity was unavailable")
        if _quality_semantics(existing) == _quality_semantics(signal):
            return existing
        if expected_computed_at is None:
            raise RepositoryConflictError("quality signal replay changed computed semantics")
        expected = _aware_time(expected_computed_at, "expected_computed_at")
        update = transaction.execute(
            f"""
            UPDATE tool_quality_signal
               SET window_start = %s, dispatch_count = %s, failure_count = %s,
                   negative_feedback_count = %s, failure_rate = %s,
                   negative_feedback_rate = %s, status = %s, computed_at = %s
             WHERE agent_id = %s AND tool_name = %s AND window_end = %s
               AND computed_at = %s
            RETURNING {self._FIELDS}
            """,
            (
                signal.window_start,
                signal.dispatch_count,
                signal.failure_count,
                signal.negative_feedback_count,
                signal.failure_rate,
                signal.negative_feedback_rate,
                signal.status,
                signal.computed_at,
                signal.agent_id,
                signal.tool_name,
                signal.window_end,
                expected,
            ),
        )
        if not getattr(update, "returned_records", ()):
            raise RepositoryConflictError("quality signal timestamp fence is stale")
        return _quality(_single_returned(update, "knowledge.quality.replace"))

    def latest_for_administration(
        self,
        query: QueryExecutor,
        *,
        agent_id: str,
        tool_name: str,
        window_end: datetime | None = None,
    ) -> QualitySignalRecord | None:
        agent = _required_id(agent_id, "agent_id")
        tool = _required_id(tool_name, "tool_name")
        if window_end is None:
            sql = (
                f"SELECT {self._FIELDS} FROM tool_quality_signal "
                "WHERE agent_id = %s AND tool_name = %s "
                "ORDER BY computed_at DESC, id DESC LIMIT 1"
            )
            parameters = (agent, tool)
        else:
            sql = (
                f"SELECT {self._FIELDS} FROM tool_quality_signal "
                "WHERE agent_id = %s AND tool_name = %s AND window_end = %s"
            )
            parameters = (agent, tool, _aware_time(window_end, "window_end"))
        row = query.fetch_one(sql, parameters)
        return None if row is None else _quality(row)

    def list_underperforming_for_administration(
        self,
        query: QueryExecutor,
        *,
        limit: int = 50,
        before_computed_at: datetime | None = None,
        before_signal_id: str | None = None,
    ) -> tuple[QualitySignalRecord, ...]:
        maximum = _bounded_limit(limit)
        if (before_computed_at is None) != (before_signal_id is None):
            raise RepositoryValidationError("quality cursor fields must be supplied together")
        cursor_clause = ""
        parameters: list[object] = []
        if before_computed_at is not None and before_signal_id is not None:
            cursor_clause = " AND (computed_at, id::text) < (%s, %s)"
            parameters.extend(
                (
                    _aware_time(before_computed_at, "before_computed_at"),
                    _required_id(before_signal_id, "before_signal_id"),
                )
            )
        parameters.append(maximum)
        latest = (
            f"SELECT DISTINCT ON (agent_id, tool_name) {self._FIELDS} "
            "FROM tool_quality_signal ORDER BY agent_id, tool_name, computed_at DESC, id DESC"
        )
        if dialect_of(query).is_sqlite:
            latest = (
                f"SELECT {self._FIELDS} FROM (SELECT {self._FIELDS}, row_number() OVER "
                "(PARTITION BY agent_id, tool_name ORDER BY computed_at DESC, id DESC) AS rank "
                "FROM tool_quality_signal) AS ranked WHERE rank=1"
            )
        rows = query.fetch_all(
            f"""
            WITH latest AS ({latest})
            SELECT * FROM latest WHERE status = 'underperforming'{cursor_clause}
             ORDER BY computed_at DESC, id DESC LIMIT %s
            """,
            tuple(parameters),
        )
        return tuple(_quality(row) for row in rows)

    def aggregate_window_for_administration(
        self,
        query: QueryExecutor,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[QualityAggregateRecord, ...]:
        start, end = _window(window_start, window_end)
        rows = query.fetch_all(
            """
            WITH dispatches AS (
                SELECT agent_id,
                       REPLACE(REPLACE(action_type, 'tool.', ''), '.end', '') AS tool_name,
                       COUNT(*) AS dispatch_count,
                       COUNT(*) FILTER (WHERE outcome = 'failure') AS failure_count
                  FROM audit_events
                 WHERE event_class = 'agent_tool_call'
                   AND action_type LIKE 'tool.%%.end'
                   AND recorded_at >= %s AND recorded_at <= %s
                 GROUP BY agent_id, tool_name
            ), feedback_negs AS (
                SELECT source_agent AS agent_id, source_tool AS tool_name,
                       COUNT(*) AS negative_feedback_count
                  FROM component_feedback
                 WHERE lifecycle = 'active' AND sentiment = 'negative'
                   AND created_at >= %s AND created_at <= %s
                   AND source_agent IS NOT NULL AND source_tool IS NOT NULL
                 GROUP BY source_agent, source_tool
            )
            SELECT COALESCE(dispatches.agent_id, feedback_negs.agent_id) AS agent_id,
                   COALESCE(dispatches.tool_name, feedback_negs.tool_name) AS tool_name,
                   COALESCE(dispatches.dispatch_count, 0) AS dispatch_count,
                   COALESCE(dispatches.failure_count, 0) AS failure_count,
                   COALESCE(feedback_negs.negative_feedback_count, 0)
                       AS negative_feedback_count
              FROM dispatches FULL OUTER JOIN feedback_negs
                ON dispatches.agent_id = feedback_negs.agent_id
               AND dispatches.tool_name = feedback_negs.tool_name
             ORDER BY agent_id, tool_name
            """,
            (start, end, start, end),
        )
        return tuple(_quality_aggregate(row) for row in rows)

    def category_breakdown_for_administration(
        self,
        query: QueryExecutor,
        *,
        agent_id: str,
        tool_name: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Mapping[str, int]:
        agent = _required_id(agent_id, "agent_id")
        tool = _required_id(tool_name, "tool_name")
        start, end = _window(window_start, window_end)
        rows = query.fetch_all(
            """
            SELECT category, COUNT(*) AS count FROM component_feedback
             WHERE lifecycle = 'active' AND sentiment = 'negative'
               AND source_agent = %s AND source_tool = %s
               AND created_at >= %s AND created_at <= %s
             GROUP BY category ORDER BY category
            """,
            (agent, tool, start, end),
        )
        return MappingProxyType(
            {
                _stored_text(_row_value(row, "category"), "category"): _stored_int(
                    _row_value(row, "count"), "count"
                )
                for row in rows
            }
        )

    def evidence_ids_for_administration(
        self,
        query: QueryExecutor,
        *,
        agent_id: str,
        tool_name: str,
        window_start: datetime,
        window_end: datetime,
        cap: int = 500,
    ) -> QualityEvidenceRecord:
        agent = _required_id(agent_id, "agent_id")
        tool = _required_id(tool_name, "tool_name")
        start, end = _window(window_start, window_end)
        maximum = _bounded_limit(cap, maximum=1000)
        audits = query.fetch_all(
            """
            SELECT event_id FROM audit_events
             WHERE event_class = 'agent_tool_call' AND action_type = %s
               AND agent_id = %s AND outcome = 'failure'
               AND recorded_at >= %s AND recorded_at <= %s
             ORDER BY recorded_at DESC, event_id DESC LIMIT %s
            """,
            (f"tool.{tool}.end", agent, start, end, maximum),
        )
        feedback = query.fetch_all(
            """
            SELECT id FROM component_feedback
             WHERE lifecycle = 'active' AND sentiment = 'negative'
               AND source_agent = %s AND source_tool = %s
               AND created_at >= %s AND created_at <= %s
             ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (agent, tool, start, end, maximum),
        )
        return QualityEvidenceRecord(
            audit_event_ids=tuple(
                _stored_text(_row_value(row, "event_id"), "event_id") for row in audits
            ),
            feedback_ids=tuple(
                _stored_text(_row_value(row, "id"), "feedback_id") for row in feedback
            ),
        )

    def clean_comment_samples_for_administration(
        self,
        query: QueryExecutor,
        *,
        agent_id: str,
        tool_name: str,
        window_start: datetime,
        window_end: datetime,
        cap: int = 5,
    ) -> tuple[CleanCommentSample, ...]:
        agent = _required_id(agent_id, "agent_id")
        tool = _required_id(tool_name, "tool_name")
        start, end = _window(window_start, window_end)
        maximum = _bounded_limit(cap, maximum=100)
        rows = query.fetch_all(
            """
            SELECT id, category, comment_raw, created_at FROM component_feedback
             WHERE lifecycle = 'active' AND sentiment = 'negative'
               AND comment_safety = 'clean' AND comment_raw IS NOT NULL
               AND comment_raw <> '' AND source_agent = %s AND source_tool = %s
               AND created_at >= %s AND created_at <= %s
             ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            (agent, tool, start, end, maximum),
        )
        return tuple(_comment_sample(row) for row in rows)

    def underperforming_count_for_administration(self, query: QueryExecutor) -> int:
        latest = (
            "SELECT DISTINCT ON (agent_id, tool_name) status FROM tool_quality_signal "
            "ORDER BY agent_id, tool_name, computed_at DESC, id DESC"
        )
        if dialect_of(query).is_sqlite:
            latest = (
                "SELECT status FROM (SELECT status, row_number() OVER "
                "(PARTITION BY agent_id, tool_name ORDER BY computed_at DESC, id DESC) AS rank "
                "FROM tool_quality_signal) AS ranked WHERE rank=1"
            )
        row = query.fetch_one(
            f"""
            WITH latest AS ({latest})
            SELECT COUNT(*) AS count FROM latest WHERE status = 'underperforming'
            """
        )
        return 0 if row is None else _stored_int(_row_value(row, "count"), "count")


class QuarantineRepository:
    _FIELDS = (
        "feedback_id, reason, detector, detected_at, status, actor_user_id, actioned_at"
    )

    def hold_for_owner(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        feedback_id: str,
        reason: str,
        detector: str,
        detected_at: datetime,
    ) -> QuarantineEntryRecord:
        owner = _required_id(owner_id, "owner_id")
        feedback = _required_id(feedback_id, "feedback_id")
        why = _bounded_text(reason, "reason", maximum=1024)
        if detector not in _DETECTORS:
            raise RepositoryValidationError("quarantine detector is unsupported")
        observed = _aware_time(detected_at, "detected_at")
        flagged = transaction.execute(
            """
            UPDATE component_feedback
               SET comment_safety = 'quarantined', comment_safety_reason = %s,
                   updated_at = %s
             WHERE id = %s AND user_id = %s AND lifecycle = 'active'
            """,
            (why, observed, feedback, owner),
        )
        if flagged.rowcount != 1:
            raise RepositoryNotFoundError("owner-scoped active feedback was not found")
        result = transaction.execute(
            f"""
            INSERT INTO quarantine_entry (
                feedback_id, reason, detector, detected_at, status,
                actor_user_id, actioned_at
            ) VALUES (%s, %s, %s, %s, 'held', NULL, NULL)
            ON CONFLICT (feedback_id) DO UPDATE SET
                reason = EXCLUDED.reason, detector = EXCLUDED.detector,
                detected_at = EXCLUDED.detected_at, status = 'held',
                actor_user_id = NULL, actioned_at = NULL
            RETURNING {self._FIELDS}
            """,
            (feedback, why, detector, observed),
        )
        return _quarantine(_single_returned(result, "knowledge.quarantine.hold"))

    def get_for_administration(
        self, query: QueryExecutor, *, feedback_id: str
    ) -> QuarantineEntryRecord | None:
        feedback = _required_id(feedback_id, "feedback_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM quarantine_entry WHERE feedback_id = %s",
            (feedback,),
        )
        return None if row is None else _quarantine(row)

    def list_for_administration(
        self,
        query: QueryExecutor,
        *,
        status: QuarantineStatus | str = QuarantineStatus.HELD,
        limit: int = 50,
        before_detected_at: datetime | None = None,
        before_feedback_id: str | None = None,
    ) -> tuple[QuarantineReviewRecord, ...]:
        lifecycle = _quarantine_status(status)
        maximum = _bounded_limit(limit)
        if (before_detected_at is None) != (before_feedback_id is None):
            raise RepositoryValidationError("quarantine cursor fields must be supplied together")
        cursor_clause = ""
        parameters: list[object] = [lifecycle.value]
        if before_detected_at is not None and before_feedback_id is not None:
            cursor_clause = " AND (entry.detected_at, entry.feedback_id::text) < (%s, %s)"
            parameters.extend(
                (
                    _aware_time(before_detected_at, "before_detected_at"),
                    _required_id(before_feedback_id, "before_feedback_id"),
                )
            )
        parameters.append(maximum)
        rows = query.fetch_all(
            f"""
            SELECT entry.{self._FIELDS.replace(', ', ', entry.')},
                   feedback.user_id, feedback.source_agent, feedback.source_tool,
                   feedback.comment_raw
              FROM quarantine_entry AS entry
              JOIN component_feedback AS feedback ON feedback.id = entry.feedback_id
             WHERE entry.status = %s{cursor_clause}
             ORDER BY entry.detected_at DESC, entry.feedback_id DESC LIMIT %s
            """,
            tuple(parameters),
        )
        return tuple(_quarantine_review(row) for row in rows)

    def action_for_administration(
        self,
        transaction: Transaction,
        *,
        feedback_id: str,
        expected_detected_at: datetime,
        status: QuarantineStatus | str,
        actor_user_id: str,
        actioned_at: datetime,
    ) -> QuarantineEntryRecord:
        feedback = _required_id(feedback_id, "feedback_id")
        expected = _aware_time(expected_detected_at, "expected_detected_at")
        target = _quarantine_status(status)
        if target not in {QuarantineStatus.RELEASED, QuarantineStatus.DISMISSED}:
            raise RepositoryValidationError("quarantine action must release or dismiss")
        actor = _required_id(actor_user_id, "actor_user_id")
        observed = _aware_time(actioned_at, "actioned_at")
        result = transaction.execute(
            f"""
            UPDATE quarantine_entry
               SET status = %s, actor_user_id = %s, actioned_at = %s
             WHERE feedback_id = %s AND status = 'held' AND detected_at = %s
            RETURNING {self._FIELDS}
            """,
            (target.value, actor, observed, feedback, expected),
        )
        if not getattr(result, "returned_records", ()):
            existing = self.get_for_administration(transaction, feedback_id=feedback)
            if existing is None:
                raise RepositoryNotFoundError("quarantine entry was not found")
            raise RepositoryConflictError("quarantine lifecycle fence is stale")
        entry = _quarantine(_single_returned(result, "knowledge.quarantine.action"))
        if target is QuarantineStatus.RELEASED:
            released = transaction.execute(
                """
                UPDATE component_feedback
                   SET comment_safety = 'clean', comment_safety_reason = NULL,
                       updated_at = %s
                 WHERE id = %s AND comment_safety = 'quarantined'
                """,
                (observed, feedback),
            )
            if released.rowcount != 1:
                raise RepositoryDataError("released quarantine did not update feedback safety")
        return entry


class KnowledgeProposalRepository:
    _FIELDS = (
        "id, agent_id, tool_name, artifact_path, diff_payload, "
        "artifact_sha_at_gen, evidence, status, reviewer_user_id, reviewed_at, "
        "reviewer_rationale, applied_at, generated_at"
    )

    def create_for_administration(
        self,
        transaction: Transaction,
        record: KnowledgeProposalRecord,
    ) -> KnowledgeProposalRecord:
        proposal = _validated_proposal(record, new=True)
        lock_key = _canonical_json(
            ("knowledge-proposal", proposal.agent_id, proposal.tool_name), "proposal_lock"
        )
        transaction.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
        )
        transaction.execute(
            """
            UPDATE knowledge_update_proposal SET status = 'superseded'
             WHERE agent_id = %s AND tool_name = %s AND status = 'pending'
               AND id <> %s
            """,
            (proposal.agent_id, proposal.tool_name, proposal.proposal_id),
        )
        result = transaction.execute(
            f"""
            INSERT INTO knowledge_update_proposal (
                id, agent_id, tool_name, artifact_path, diff_payload,
                artifact_sha_at_gen, evidence, status, reviewer_user_id,
                reviewed_at, reviewer_rationale, applied_at, generated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'pending',
                      NULL, NULL, NULL, NULL, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                proposal.proposal_id,
                proposal.agent_id,
                proposal.tool_name,
                proposal.artifact_path,
                proposal.diff_payload,
                proposal.artifact_sha_at_generation,
                _canonical_json(proposal.evidence, "evidence"),
                proposal.generated_at,
            ),
        )
        if getattr(result, "returned_records", ()):
            return _proposal(_single_returned(result, "knowledge.proposal.create"))
        existing = self.get_for_administration(
            transaction, proposal_id=proposal.proposal_id
        )
        if existing is None:
            raise RepositoryConflictError("proposal identity was unavailable")
        if existing != proposal:
            raise RepositoryConflictError("proposal replay changed immutable semantics")
        return existing

    def get_for_administration(
        self, query: QueryExecutor, *, proposal_id: str
    ) -> KnowledgeProposalRecord | None:
        proposal = _required_id(proposal_id, "proposal_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM knowledge_update_proposal WHERE id = %s",
            (proposal,),
        )
        return None if row is None else _proposal(row)

    def list_for_administration(
        self,
        query: QueryExecutor,
        *,
        status: ProposalStatus | str | None = None,
        agent_id: str | None = None,
        tool_name: str | None = None,
        limit: int = 50,
        before_generated_at: datetime | None = None,
        before_proposal_id: str | None = None,
    ) -> tuple[KnowledgeProposalRecord, ...]:
        clauses: list[str] = []
        parameters: list[object] = []
        if status is not None:
            clauses.append("status = %s")
            parameters.append(_proposal_status(status).value)
        if agent_id is not None:
            clauses.append("agent_id = %s")
            parameters.append(_required_id(agent_id, "agent_id"))
        if tool_name is not None:
            clauses.append("tool_name = %s")
            parameters.append(_required_id(tool_name, "tool_name"))
        if (before_generated_at is None) != (before_proposal_id is None):
            raise RepositoryValidationError("proposal cursor fields must be supplied together")
        if before_generated_at is not None and before_proposal_id is not None:
            clauses.append("(generated_at, id::text) < (%s, %s)")
            parameters.extend(
                (
                    _aware_time(before_generated_at, "before_generated_at"),
                    _required_id(before_proposal_id, "before_proposal_id"),
                )
            )
        parameters.append(_bounded_limit(limit))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = query.fetch_all(
            f"SELECT {self._FIELDS} FROM knowledge_update_proposal{where} "
            "ORDER BY generated_at DESC, id DESC LIMIT %s",
            tuple(parameters),
        )
        return tuple(_proposal(row) for row in rows)

    def transition_for_administration(
        self,
        transaction: Transaction,
        *,
        proposal_id: str,
        expected_status: ProposalStatus | str,
        status: ProposalStatus | str,
        reviewer_user_id: str,
        reviewed_at: datetime,
        reviewer_rationale: str | None = None,
    ) -> KnowledgeProposalRecord:
        proposal = _required_id(proposal_id, "proposal_id")
        expected = _proposal_status(expected_status)
        target = _proposal_status(status)
        if (expected, target) not in {
            (ProposalStatus.PENDING, ProposalStatus.ACCEPTED),
            (ProposalStatus.PENDING, ProposalStatus.REJECTED),
            (ProposalStatus.ACCEPTED, ProposalStatus.APPLIED),
        }:
            raise RepositoryValidationError("proposal lifecycle edge is unsupported")
        reviewer = _required_id(reviewer_user_id, "reviewer_user_id")
        observed = _aware_time(reviewed_at, "reviewed_at")
        rationale = _optional_bounded_text(
            reviewer_rationale, "reviewer_rationale", maximum=4096
        )
        result = transaction.execute(
            f"""
            UPDATE knowledge_update_proposal
               SET status = %s, reviewer_user_id = %s, reviewed_at = %s,
                   reviewer_rationale = %s,
                   applied_at = CASE WHEN %s = 'applied' THEN %s ELSE applied_at END
             WHERE id = %s AND status = %s
            RETURNING {self._FIELDS}
            """,
            (
                target.value,
                reviewer,
                observed,
                rationale,
                target.value,
                observed,
                proposal,
                expected.value,
            ),
        )
        if getattr(result, "returned_records", ()):
            return _proposal(_single_returned(result, "knowledge.proposal.transition"))
        existing = self.get_for_administration(transaction, proposal_id=proposal)
        if existing is None:
            raise RepositoryNotFoundError("knowledge proposal was not found")
        raise RepositoryConflictError("knowledge proposal lifecycle fence is stale")

    def pending_count_for_administration(self, query: QueryExecutor) -> int:
        row = query.fetch_one(
            "SELECT COUNT(*) AS count FROM knowledge_update_proposal WHERE status = 'pending'"
        )
        return 0 if row is None else _stored_int(_row_value(row, "count"), "count")


class KnowledgeRepository:
    def __init__(self) -> None:
        self.interactions = InteractionRepository()
        self.quality_signals = QualitySignalRepository()
        self.quarantine = QuarantineRepository()
        self.proposals = KnowledgeProposalRepository()


def _interaction_values(
    *,
    agent_id: object,
    tool_name: object,
    success: object,
    error_message: object,
    response_time_ms: object,
    created_at: object,
) -> tuple[object, ...]:
    agent = _required_id(agent_id, "agent_id")
    tool = _required_id(tool_name, "tool_name")
    if not isinstance(success, bool):
        raise RepositoryValidationError("success must be a boolean")
    error = _optional_bounded_text(error_message, "error_message", maximum=16384)
    response = (
        None
        if response_time_ms is None
        else _non_negative_int(response_time_ms, "response_time_ms")
    )
    observed = _non_negative_int(created_at, "created_at")
    return agent, tool, success, error, response, observed


def _positive_ids(values: object, field: str, *, maximum: int) -> tuple[int, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise RepositoryValidationError(f"{field} must be a sequence")
    identifiers = tuple(_positive_int(item, field) for item in values)
    if not identifiers or len(identifiers) > maximum or len(set(identifiers)) != len(identifiers):
        raise RepositoryValidationError(f"{field} must contain unique bounded identifiers")
    return identifiers


def _optional_bounded_text(
    value: object, field: str, *, maximum: int
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _aware_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RepositoryValidationError(f"{field} must be timezone-aware")
    return value


def _stored_time(value: object, field: str) -> datetime:
    try:
        return _aware_time(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted knowledge timestamp is invalid", metadata={"field": field}
        ) from exc


def _window(start: object, end: object) -> tuple[datetime, datetime]:
    window_start = _aware_time(start, "window_start")
    window_end = _aware_time(end, "window_end")
    if window_end <= window_start:
        raise RepositoryValidationError("window_end must follow window_start")
    return window_start, window_end


def _stored_text(value: object, field: str) -> str:
    try:
        return _required_id(value, field, maximum=4096)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted knowledge text is invalid", metadata={"field": field}
        ) from exc


def _stored_optional_text(value: object, field: str) -> str | None:
    return None if value is None else _stored_text(value, field)


def _stored_int(value: object, field: str) -> int:
    try:
        return _non_negative_int(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted knowledge integer is invalid", metadata={"field": field}
        ) from exc


def _stored_optional_int(value: object, field: str) -> int | None:
    return None if value is None else _stored_int(value, field)


def _stored_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise RepositoryDataError(
            "persisted knowledge boolean is invalid", metadata={"field": field}
        )
    return value


def _interaction(row: Mapping[str, Any]) -> InteractionRecord:
    return InteractionRecord(
        interaction_id=_positive_int(_row_value(row, "id"), "interaction_id"),
        agent_id=_stored_text(_row_value(row, "agent_id"), "agent_id"),
        tool_name=_stored_text(_row_value(row, "tool_name"), "tool_name"),
        success=_stored_bool(_row_value(row, "success"), "success"),
        error_message=_stored_optional_text(row.get("error_message"), "error_message"),
        response_time_ms=_stored_optional_int(row.get("response_time_ms"), "response_time_ms"),
        conversation_id=_stored_optional_text(row.get("chat_id"), "conversation_id"),
        synthesized=_stored_bool(_row_value(row, "synthesized"), "synthesized"),
        created_at=_stored_optional_int(row.get("created_at"), "created_at"),
    )


def _interaction_stats(row: Mapping[str, Any]) -> InteractionStatsRecord:
    total = _stored_int(_row_value(row, "total_calls"), "total_calls")
    successes = _stored_int(_row_value(row, "success_count"), "success_count")
    if successes > total:
        raise RepositoryDataError("persisted interaction aggregate is inconsistent")
    average = row.get("avg_response_ms")
    if average is not None and (
        not isinstance(average, (int, float))
        or not math.isfinite(float(average))
        or float(average) < 0
    ):
        raise RepositoryDataError("persisted interaction average is invalid")
    return InteractionStatsRecord(
        agent_id=_stored_text(_row_value(row, "agent_id"), "agent_id"),
        tool_name=_stored_text(_row_value(row, "tool_name"), "tool_name"),
        total_calls=total,
        success_count=successes,
        average_response_ms=None if average is None else float(average),
    )


def _rate(value: object, field: str, *, stored: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        error = RepositoryDataError if stored else RepositoryValidationError
        raise error(f"{field} must be a finite rate")
    rate = float(value)
    if not math.isfinite(rate) or rate < 0 or rate > 1:
        error = RepositoryDataError if stored else RepositoryValidationError
        raise error(f"{field} must be between zero and one")
    return rate


def _validated_quality(record: QualitySignalRecord) -> QualitySignalRecord:
    if not isinstance(record, QualitySignalRecord):
        raise RepositoryValidationError("record must be a QualitySignalRecord")
    start, end = _window(record.window_start, record.window_end)
    dispatch = _non_negative_int(record.dispatch_count, "dispatch_count")
    failures = _non_negative_int(record.failure_count, "failure_count")
    negatives = _non_negative_int(record.negative_feedback_count, "negative_feedback_count")
    if failures > dispatch:
        raise RepositoryValidationError("failure_count cannot exceed dispatch_count")
    if record.status not in _QUALITY_STATUSES:
        raise RepositoryValidationError("quality status is unsupported")
    return QualitySignalRecord(
        signal_id=_required_id(record.signal_id, "signal_id"),
        agent_id=_required_id(record.agent_id, "agent_id"),
        tool_name=_required_id(record.tool_name, "tool_name"),
        window_start=start,
        window_end=end,
        dispatch_count=dispatch,
        failure_count=failures,
        negative_feedback_count=negatives,
        failure_rate=_rate(record.failure_rate, "failure_rate"),
        negative_feedback_rate=_rate(record.negative_feedback_rate, "negative_feedback_rate"),
        status=record.status,
        computed_at=_aware_time(record.computed_at, "computed_at"),
    )


def _quality_parameters(record: QualitySignalRecord) -> tuple[object, ...]:
    return (
        record.signal_id,
        record.agent_id,
        record.tool_name,
        record.window_start,
        record.window_end,
        record.dispatch_count,
        record.failure_count,
        record.negative_feedback_count,
        record.failure_rate,
        record.negative_feedback_rate,
        record.status,
        record.computed_at,
    )


def _quality(row: Mapping[str, Any]) -> QualitySignalRecord:
    status = str(_row_value(row, "status"))
    if status not in _QUALITY_STATUSES:
        raise RepositoryDataError("persisted quality status is unsupported")
    start = _stored_time(_row_value(row, "window_start"), "window_start")
    end = _stored_time(_row_value(row, "window_end"), "window_end")
    if end <= start:
        raise RepositoryDataError("persisted quality window is invalid")
    dispatch = _stored_int(_row_value(row, "dispatch_count"), "dispatch_count")
    failure = _stored_int(_row_value(row, "failure_count"), "failure_count")
    if failure > dispatch:
        raise RepositoryDataError("persisted quality counts are inconsistent")
    return QualitySignalRecord(
        signal_id=_stored_text(_row_value(row, "id"), "signal_id"),
        agent_id=_stored_text(_row_value(row, "agent_id"), "agent_id"),
        tool_name=_stored_text(_row_value(row, "tool_name"), "tool_name"),
        window_start=start,
        window_end=end,
        dispatch_count=dispatch,
        failure_count=failure,
        negative_feedback_count=_stored_int(
            _row_value(row, "negative_feedback_count"), "negative_feedback_count"
        ),
        failure_rate=_rate(_row_value(row, "failure_rate"), "failure_rate", stored=True),
        negative_feedback_rate=_rate(
            _row_value(row, "negative_feedback_rate"),
            "negative_feedback_rate",
            stored=True,
        ),
        status=status,
        computed_at=_stored_time(_row_value(row, "computed_at"), "computed_at"),
    )


def _quality_semantics(record: QualitySignalRecord) -> tuple[object, ...]:
    return (
        record.agent_id,
        record.tool_name,
        record.window_start,
        record.window_end,
        record.dispatch_count,
        record.failure_count,
        record.negative_feedback_count,
        record.failure_rate,
        record.negative_feedback_rate,
        record.status,
        record.computed_at,
    )


def _quality_aggregate(row: Mapping[str, Any]) -> QualityAggregateRecord:
    dispatch = _stored_int(_row_value(row, "dispatch_count"), "dispatch_count")
    failures = _stored_int(_row_value(row, "failure_count"), "failure_count")
    if failures > dispatch:
        raise RepositoryDataError("persisted quality aggregate is inconsistent")
    return QualityAggregateRecord(
        agent_id=_stored_text(_row_value(row, "agent_id"), "agent_id"),
        tool_name=_stored_text(_row_value(row, "tool_name"), "tool_name"),
        dispatch_count=dispatch,
        failure_count=failures,
        negative_feedback_count=_stored_int(
            _row_value(row, "negative_feedback_count"), "negative_feedback_count"
        ),
    )


def _comment_sample(row: Mapping[str, Any]) -> CleanCommentSample:
    comment = row.get("comment_raw")
    if not isinstance(comment, str) or not comment:
        raise RepositoryDataError("persisted clean comment sample is invalid")
    return CleanCommentSample(
        feedback_id=_stored_text(_row_value(row, "id"), "feedback_id"),
        category=_stored_text(_row_value(row, "category"), "category"),
        comment=comment,
        created_at=_stored_time(_row_value(row, "created_at"), "created_at"),
    )


def _quarantine_status(value: object) -> QuarantineStatus:
    try:
        return QuarantineStatus(str(value))
    except ValueError as exc:
        raise RepositoryValidationError("quarantine status is unsupported") from exc


def _quarantine(row: Mapping[str, Any]) -> QuarantineEntryRecord:
    detector = str(_row_value(row, "detector"))
    status_value = str(_row_value(row, "status"))
    if detector not in _DETECTORS or status_value not in _QUARANTINE_STATUSES:
        raise RepositoryDataError("persisted quarantine enum is unsupported")
    status = QuarantineStatus(status_value)
    actioned = (
        None
        if row.get("actioned_at") is None
        else _stored_time(row["actioned_at"], "actioned_at")
    )
    actor = _stored_optional_text(row.get("actor_user_id"), "actor_user_id")
    if (status is QuarantineStatus.HELD) != (actor is None and actioned is None):
        raise RepositoryDataError("persisted quarantine lifecycle is inconsistent")
    return QuarantineEntryRecord(
        feedback_id=_stored_text(_row_value(row, "feedback_id"), "feedback_id"),
        reason=_stored_text(_row_value(row, "reason"), "reason"),
        detector=detector,
        detected_at=_stored_time(_row_value(row, "detected_at"), "detected_at"),
        status=status,
        actor_user_id=actor,
        actioned_at=actioned,
    )


def _quarantine_review(row: Mapping[str, Any]) -> QuarantineReviewRecord:
    comment = row.get("comment_raw")
    if comment is not None and not isinstance(comment, str):
        raise RepositoryDataError("persisted quarantine comment is invalid")
    return QuarantineReviewRecord(
        entry=_quarantine(row),
        owner_id=_stored_text(_row_value(row, "user_id"), "owner_id"),
        source_agent=_stored_optional_text(row.get("source_agent"), "source_agent"),
        source_tool=_stored_optional_text(row.get("source_tool"), "source_tool"),
        comment=comment,
    )


def _proposal_status(value: object) -> ProposalStatus:
    try:
        return ProposalStatus(str(value))
    except ValueError as exc:
        raise RepositoryValidationError("proposal status is unsupported") from exc


def _validated_proposal(
    record: KnowledgeProposalRecord, *, new: bool
) -> KnowledgeProposalRecord:
    if not isinstance(record, KnowledgeProposalRecord):
        raise RepositoryValidationError("record must be a KnowledgeProposalRecord")
    status = _proposal_status(record.status)
    if new and (
        status is not ProposalStatus.PENDING
        or record.reviewer_user_id is not None
        or record.reviewed_at is not None
        or record.reviewer_rationale is not None
        or record.applied_at is not None
    ):
        raise RepositoryValidationError("new proposals must begin pending and unreviewed")
    evidence = _structured_json(record.evidence, "evidence")
    if not isinstance(evidence, Mapping):
        raise RepositoryValidationError("proposal evidence must be a JSON object")
    return KnowledgeProposalRecord(
        proposal_id=_required_id(record.proposal_id, "proposal_id"),
        agent_id=_required_id(record.agent_id, "agent_id"),
        tool_name=_required_id(record.tool_name, "tool_name"),
        artifact_path=_bounded_text(record.artifact_path, "artifact_path", maximum=4096),
        diff_payload=_bounded_text(
            record.diff_payload, "diff_payload", maximum=1_000_000, allow_empty=True
        ),
        artifact_sha_at_generation=_required_id(
            record.artifact_sha_at_generation,
            "artifact_sha_at_generation",
            maximum=256,
        ),
        evidence=evidence,
        status=status,
        reviewer_user_id=record.reviewer_user_id,
        reviewed_at=record.reviewed_at,
        reviewer_rationale=record.reviewer_rationale,
        applied_at=record.applied_at,
        generated_at=_aware_time(record.generated_at, "generated_at"),
    )


def _proposal(row: Mapping[str, Any]) -> KnowledgeProposalRecord:
    try:
        status = _proposal_status(_row_value(row, "status"))
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted proposal status is unsupported") from exc
    evidence = _structured_json(_row_value(row, "evidence"), "evidence")
    if not isinstance(evidence, Mapping):
        raise RepositoryDataError("persisted proposal evidence must be an object")
    reviewed = (
        None
        if row.get("reviewed_at") is None
        else _stored_time(row["reviewed_at"], "reviewed_at")
    )
    applied = (
        None
        if row.get("applied_at") is None
        else _stored_time(row["applied_at"], "applied_at")
    )
    reviewer = _stored_optional_text(row.get("reviewer_user_id"), "reviewer_user_id")
    if status is ProposalStatus.PENDING and any(
        item is not None for item in (reviewed, applied, reviewer, row.get("reviewer_rationale"))
    ):
        raise RepositoryDataError("persisted pending proposal has review state")
    if status is ProposalStatus.APPLIED and applied is None:
        raise RepositoryDataError("persisted applied proposal lacks applied_at")
    return KnowledgeProposalRecord(
        proposal_id=_stored_text(_row_value(row, "id"), "proposal_id"),
        agent_id=_stored_text(_row_value(row, "agent_id"), "agent_id"),
        tool_name=_stored_text(_row_value(row, "tool_name"), "tool_name"),
        artifact_path=_stored_text(_row_value(row, "artifact_path"), "artifact_path"),
        diff_payload=_stored_text_allow_empty(_row_value(row, "diff_payload"), "diff_payload"),
        artifact_sha_at_generation=_stored_text(
            _row_value(row, "artifact_sha_at_gen"), "artifact_sha_at_generation"
        ),
        evidence=evidence,
        status=status,
        reviewer_user_id=reviewer,
        reviewed_at=reviewed,
        reviewer_rationale=_stored_optional_text(
            row.get("reviewer_rationale"), "reviewer_rationale"
        ),
        applied_at=applied,
        generated_at=_stored_time(_row_value(row, "generated_at"), "generated_at"),
    )


def _stored_text_allow_empty(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RepositoryDataError(
            "persisted knowledge text is invalid", metadata={"field": field}
        )
    return value


__all__ = (
    "CleanCommentSample",
    "InteractionRecord",
    "InteractionRepository",
    "InteractionStatsRecord",
    "KnowledgeProposalRecord",
    "KnowledgeProposalRepository",
    "KnowledgeRepository",
    "ProposalStatus",
    "QualityAggregateRecord",
    "QualityEvidenceRecord",
    "QualitySignalRecord",
    "QualitySignalRepository",
    "QuarantineEntryRecord",
    "QuarantineRepository",
    "QuarantineReviewRecord",
    "QuarantineStatus",
)
