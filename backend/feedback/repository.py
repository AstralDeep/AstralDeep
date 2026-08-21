"""Per-user-isolated persistence facade for the feedback subsystem.

Every query that touches ``component_feedback``, ``tool_quality_signal``,
``knowledge_update_proposal``, or ``quarantine_entry`` lives here. Routes
and the recorder NEVER write SQL inline — they go through this module.

Design notes:

* Every method that selects, updates, or deletes ``component_feedback``
  rows takes ``actor_user_id`` as a mandatory first argument and applies
  it to the WHERE clause. There are no "list all" or "look up by id alone"
  helpers. Cross-user reads return None / empty list, indistinguishable
  from "not found" (mirrors audit-log pattern from feature 003, FR-009).
* Admin-only methods (``list_underperforming``, ``insert_quality_signal``,
  ``list_proposals``, etc.) are NOT per-user — they are gated by the
  ``admin`` role check at the API layer. The repository methods themselves
  accept any actor; authorization is the caller's responsibility.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from astralplane.repositories.knowledge import (
    KnowledgeProposalRecord,
    ProposalStatus,
    QualitySignalRecord,
)
from astralplane.repositories.preferences import FeedbackCursor, FeedbackRecord
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

from .schemas import (
    DEFAULT_DEDUP_WINDOW_SECONDS,
    ComponentFeedbackDTO,
    KnowledgeUpdateProposalDTO,
    QuarantineEntryDTO,
    ToolQualitySignalDTO,
)

logger = logging.getLogger("Feedback.Repository")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _after(previous: datetime) -> datetime:
    """Return an aware timestamp that strictly advances a repository CAS fence."""

    return max(_utcnow(), previous + timedelta(microseconds=1))


class FeedbackRepository:
    """Thin façade over the four feature-004 tables."""

    def __init__(
        self,
        db: Any,
        *,
        plane_runtime=None,
        plane_repositories=None,
        knowledge_repository=None,
        preferences_repository=None,
        audit_repository=None,
    ):
        repository, runtime = repository_from(
            "knowledge",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        knowledge = knowledge_repository or repository
        self._quality = PlaneRepositoryContext(
            repository=knowledge.quality_signals,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._quarantine = PlaneRepositoryContext(
            repository=knowledge.quarantine,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._proposals = PlaneRepositoryContext(
            repository=knowledge.proposals,
            plane_runtime=runtime,
            legacy_database=db,
        )
        preferences, preferences_runtime = repository_from(
            "preferences",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._feedback = PlaneRepositoryContext(
            repository=(preferences_repository or preferences).feedback,
            plane_runtime=preferences_runtime,
            legacy_database=db,
        )
        audit, audit_runtime = repository_from(
            "audit",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._audit = PlaneRepositoryContext(
            repository=audit_repository or audit,
            plane_runtime=audit_runtime,
            legacy_database=db,
        )

    # ------------------------------------------------------------------
    # ComponentFeedback — submit / dedup / list / retract / amend
    # ------------------------------------------------------------------

    def find_in_dedup_window(
        self,
        actor_user_id: str,
        correlation_id: Optional[str],
        component_id: Optional[str],
        *,
        window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        now: Optional[datetime] = None,
    ) -> Optional[ComponentFeedbackDTO]:
        """Return the active feedback row this user has on this dispatch+component
        within the dedup window, if any. Used to collapse rapid double-submits.
        """
        cutoff = (now or _utcnow()) - timedelta(seconds=window_seconds)
        record = self._feedback.call(
            self._feedback.repository.find_in_dedup_window,
            owner_id=actor_user_id,
            correlation_id=correlation_id,
            component_id=component_id,
            cutoff=cutoff,
        )
        return None if record is None else _feedback_record_to_dto(record)

    def insert(
        self,
        actor_user_id: str,
        *,
        conversation_id: Optional[str],
        correlation_id: Optional[str],
        source_agent: Optional[str],
        source_tool: Optional[str],
        component_id: Optional[str],
        sentiment: str,
        category: str,
        comment_raw: Optional[str],
        comment_safety: str,
        comment_safety_reason: Optional[str],
        supersedes_id: Optional[str] = None,
    ) -> ComponentFeedbackDTO:
        """Insert a new active feedback row.

        If ``supersedes_id`` is given, that row is marked ``superseded`` and
        its ``superseded_by`` set to the new row's id, atomically with the
        insert. Caller is responsible for verifying ``supersedes_id`` belongs
        to the same user — this method assumes the check already happened.
        """
        now = _utcnow()
        replacement = FeedbackRecord(
            feedback_id=str(uuid.uuid4()),
            owner_id=actor_user_id,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            source_agent=source_agent,
            source_tool=source_tool,
            component_id=component_id,
            sentiment=sentiment,
            category=category,
            comment=comment_raw,
            comment_safety=comment_safety,
            comment_safety_reason=comment_safety_reason,
            lifecycle="active",
            superseded_by=None,
            created_at=now,
            updated_at=now,
        )
        with self._feedback.transaction() as transaction:
            if supersedes_id is None:
                record = self._feedback.repository.submit(transaction, replacement)
            else:
                record = self._feedback.repository.supersede(
                    transaction,
                    owner_id=actor_user_id,
                    old_feedback_id=supersedes_id,
                    replacement=replacement,
                    updated_at=now,
                )
        return _feedback_record_to_dto(record)

    def update_in_window(
        self,
        actor_user_id: str,
        feedback_id: str,
        *,
        sentiment: str,
        category: str,
        comment_raw: Optional[str],
        comment_safety: str,
        comment_safety_reason: Optional[str],
    ) -> Optional[ComponentFeedbackDTO]:
        """Update an existing in-window row in place. No new row created.

        Returns the updated DTO, or None if the row no longer matches the
        user (cross-user attempt — indistinguishable from not found).
        """
        with self._feedback.transaction() as transaction:
            existing = self._feedback.repository.get(
                transaction,
                owner_id=actor_user_id,
                feedback_id=feedback_id,
            )
            if existing is None or existing.lifecycle != "active":
                return None
            record = self._feedback.repository.amend_active(
                transaction,
                owner_id=actor_user_id,
                feedback_id=feedback_id,
                expected_updated_at=existing.updated_at,
                sentiment=sentiment,
                category=category,
                comment=comment_raw,
                comment_safety=comment_safety,
                comment_safety_reason=comment_safety_reason,
                updated_at=_after(existing.updated_at),
            )
        return None if record is None else _feedback_record_to_dto(record)

    def get_for_user(
        self, actor_user_id: str, feedback_id: str
    ) -> Optional[ComponentFeedbackDTO]:
        record = self._feedback.call(
            self._feedback.repository.get,
            owner_id=actor_user_id,
            feedback_id=feedback_id,
        )
        return None if record is None else _feedback_record_to_dto(record)

    def list_for_user(
        self,
        actor_user_id: str,
        *,
        lifecycle: str = "active",
        source_tool: Optional[str] = None,
        source_agent: Optional[str] = None,
        from_ts: Optional[datetime] = None,
        to_ts: Optional[datetime] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[ComponentFeedbackDTO], Optional[str]]:
        """Strictly per-user list. Cursor is the last row's created_at + id, JSON-encoded."""
        typed_cursor = None
        if cursor:
            try:
                c_data = json.loads(cursor)
                typed_cursor = FeedbackCursor(
                    created_at=datetime.fromisoformat(c_data["t"]),
                    feedback_id=str(c_data["i"]),
                )
            except Exception:
                pass  # ignore malformed cursor
        page = self._feedback.call(
            self._feedback.repository.list_page,
            owner_id=actor_user_id,
            lifecycle=lifecycle,
            source_tool=source_tool,
            source_agent=source_agent,
            from_time=from_ts,
            to_time=to_ts,
            cursor=typed_cursor,
            limit=limit,
        )
        next_cursor = None
        if page.next_cursor is not None:
            next_cursor = json.dumps(
                {
                    "t": page.next_cursor.created_at.isoformat(),
                    "i": page.next_cursor.feedback_id,
                }
            )
        return [_feedback_record_to_dto(record) for record in page.records], next_cursor

    def retract(
        self, actor_user_id: str, feedback_id: str
    ) -> Optional[ComponentFeedbackDTO]:
        """Mark the user's own row as retracted. Returns the updated DTO,
        or None if not found / cross-user."""
        record = self._feedback.call(
            self._feedback.repository.retract,
            owner_id=actor_user_id,
            feedback_id=feedback_id,
            updated_at=_utcnow(),
        )
        return None if record is None else _feedback_record_to_dto(record)

    # ------------------------------------------------------------------
    # Quarantine entries
    # ------------------------------------------------------------------

    def upsert_quarantine(
        self,
        feedback_id: str,
        *,
        owner_user_id: str,
        reason: str,
        detector: str,
    ) -> QuarantineEntryDTO:
        """Create or replace the quarantine_entry for a feedback record.

        Used by both the inline submit path (``detector='inline'``) and the
        loop pre-pass (``detector='loop_pre_pass'``). When the loop pre-pass
        flags a record the inline pass had cleared, the existing inline row
        is overwritten — the PRIMARY KEY on ``feedback_id`` enforces single-row.
        """
        record = self._quarantine.call(
            self._quarantine.repository.hold_for_owner,
            owner_id=owner_user_id,
            feedback_id=feedback_id,
            reason=reason,
            detector=detector,
            detected_at=_utcnow(),
        )
        return _quarantine_record_to_dto(record)

    def list_quarantine(
        self, *, status: str = "held", limit: int = 50, cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Admin-only list of quarantine entries joined with their feedback rows."""
        before_detected_at = None
        before_feedback_id = None
        if cursor:
            try:
                c_data = json.loads(cursor)
                before_detected_at = datetime.fromisoformat(c_data["t"])
                before_feedback_id = str(c_data["i"])
            except Exception:
                pass
        records = self._quarantine.call(
            self._quarantine.repository.list_for_administration,
            status=status,
            limit=limit + 1,
            before_detected_at=before_detected_at,
            before_feedback_id=before_feedback_id,
        )
        items = [
            {
                "feedback_id": record.entry.feedback_id,
                "user_id": record.owner_id,
                "source_agent": record.source_agent,
                "source_tool": record.source_tool,
                "comment_raw": record.comment,
                "reason": record.entry.reason,
                "detector": record.entry.detector,
                "detected_at": _iso(record.entry.detected_at),
                "status": record.entry.status.value,
            }
            for record in records[:limit]
        ]
        next_cursor = None
        if len(records) > limit:
            last = records[limit - 1].entry
            next_cursor = json.dumps(
                {"t": _iso(last.detected_at), "i": last.feedback_id}
            )
        return items, next_cursor

    def quarantine_action(
        self, feedback_id: str, *, status: str, actor_user_id: str,
    ) -> Optional[QuarantineEntryDTO]:
        """Apply a 'released' or 'dismissed' action.

        Released: also flips the underlying feedback's ``comment_safety`` back
        to ``'clean'`` so subsequent synthesizer cycles pick up the comment.
        Dismissed: feedback's ``comment_safety`` stays ``'quarantined'``.
        """
        if status not in ("released", "dismissed"):
            raise ValueError(f"unsupported quarantine status transition: {status!r}")
        with self._quarantine.transaction() as transaction:
            existing = self._quarantine.repository.get_for_administration(
                transaction,
                feedback_id=feedback_id,
            )
            if existing is None or existing.status.value != "held":
                return None
            record = self._quarantine.repository.action_for_administration(
                transaction,
                feedback_id=feedback_id,
                expected_detected_at=existing.detected_at,
                status=status,
                actor_user_id=actor_user_id,
                actioned_at=_utcnow(),
            )
        return _quarantine_record_to_dto(record)

    # ------------------------------------------------------------------
    # Tool quality signals
    # ------------------------------------------------------------------

    def insert_quality_signal(self, dto: ToolQualitySignalDTO) -> ToolQualitySignalDTO:
        signal = QualitySignalRecord(
            signal_id=dto.id or str(uuid.uuid4()),
            agent_id=dto.agent_id,
            tool_name=dto.tool_name,
            window_start=dto.window_start,
            window_end=dto.window_end,
            dispatch_count=dto.dispatch_count,
            failure_count=dto.failure_count,
            negative_feedback_count=dto.negative_feedback_count,
            failure_rate=dto.failure_rate,
            negative_feedback_rate=dto.negative_feedback_rate,
            status=dto.status,
            computed_at=dto.computed_at,
        )
        with self._quality.transaction() as transaction:
            existing = self._quality.repository.latest_for_administration(
                transaction,
                agent_id=dto.agent_id,
                tool_name=dto.tool_name,
                window_end=dto.window_end,
            )
            record = self._quality.repository.put_for_administration(
                transaction,
                signal,
                expected_computed_at=(
                    None if existing is None else existing.computed_at
                ),
            )
        return _quality_record_to_dto(record)

    def latest_quality_signal(
        self, agent_id: str, tool_name: str
    ) -> Optional[ToolQualitySignalDTO]:
        record = self._quality.call(
            self._quality.repository.latest_for_administration,
            agent_id=agent_id,
            tool_name=tool_name,
        )
        return None if record is None else _quality_record_to_dto(record)

    def list_underperforming(
        self, *, limit: int = 50, cursor: Optional[str] = None,
    ) -> Tuple[List[ToolQualitySignalDTO], Optional[str]]:
        """List the latest snapshot per (agent, tool) where status='underperforming'."""
        before_computed_at = None
        before_signal_id = None
        if cursor:
            try:
                c_data = json.loads(cursor)
                before_computed_at = datetime.fromisoformat(c_data["t"])
                before_signal_id = str(c_data["i"])
            except Exception:
                pass
        records = self._quality.call(
            self._quality.repository.list_underperforming_for_administration,
            limit=limit + 1,
            before_computed_at=before_computed_at,
            before_signal_id=before_signal_id,
        )
        dtos = [_quality_record_to_dto(record) for record in records[:limit]]
        next_cursor = None
        if len(records) > limit:
            last = records[limit - 1]
            next_cursor = json.dumps(
                {"t": last.computed_at.isoformat(), "i": last.signal_id}
            )
        return dtos, next_cursor

    # ------------------------------------------------------------------
    # Aggregations used by the daily quality job
    # ------------------------------------------------------------------

    def aggregate_window(
        self, window_start: datetime, window_end: datetime
    ) -> List[Dict[str, Any]]:
        """Aggregate dispatch + failure + negative-feedback counts per (agent, tool)
        over the given window. Pulls ``dispatch_count`` and ``failure_count`` from
        the audit-log via the ``agent_tool_call`` event class.
        """
        records = self._quality.call(
            self._quality.repository.aggregate_window_for_administration,
            window_start=window_start,
            window_end=window_end,
        )
        return [
            {
                "agent_id": record.agent_id,
                "tool_name": record.tool_name,
                "dispatch_count": record.dispatch_count,
                "failure_count": record.failure_count,
                "negative_feedback_count": record.negative_feedback_count,
            }
            for record in records
        ]

    def category_breakdown(
        self, agent_id: str, tool_name: str,
        window_start: datetime, window_end: datetime,
    ) -> Dict[str, int]:
        counts = self._quality.call(
            self._quality.repository.category_breakdown_for_administration,
            agent_id=agent_id,
            tool_name=tool_name,
            window_start=window_start,
            window_end=window_end,
        )
        return dict(counts)

    def evidence_ids(
        self, agent_id: str, tool_name: str,
        window_start: datetime, window_end: datetime,
        *, cap: int = 500,
    ) -> Tuple[List[str], List[str]]:
        """Return (audit_event_ids, component_feedback_ids) for a flagged tool's evidence."""
        record = self._quality.call(
            self._quality.repository.evidence_ids_for_administration,
            agent_id=agent_id,
            tool_name=tool_name,
            window_start=window_start,
            window_end=window_end,
            cap=cap,
        )
        return list(record.audit_event_ids), list(record.feedback_ids)

    def list_clean_comment_candidates(
        self,
        *,
        since: datetime,
        limit: int = 500,
    ) -> List[Tuple[str, str, str]]:
        """Return the bounded administrative workload for the safety pre-pass."""

        records = self._feedback.call(
            self._feedback.repository.list_clean_comment_candidates_for_administration,
            since=since,
            limit=limit,
        )
        return [
            (record.feedback_id, record.owner_id, record.comment)
            for record in records
        ]

    def collect_clean_comment_samples(
        self, agent_id: str, tool_name: str, window_start: datetime, window_end: datetime,
        *, cap: int = 5,
    ) -> List[Dict[str, Any]]:
        """A bounded sample of clean negative-feedback comments for synthesizer input."""
        records = self._quality.call(
            self._quality.repository.clean_comment_samples_for_administration,
            agent_id=agent_id,
            tool_name=tool_name,
            window_start=window_start,
            window_end=window_end,
            cap=cap,
        )
        return [
            {
                "id": record.feedback_id,
                "category": record.category,
                "comment": record.comment,
                "created_at": _iso(record.created_at),
            }
            for record in records
        ]

    # ------------------------------------------------------------------
    # Knowledge update proposals
    # ------------------------------------------------------------------

    def insert_proposal(
        self,
        *,
        agent_id: str,
        tool_name: str,
        artifact_path: str,
        diff_payload: str,
        artifact_sha_at_gen: str,
        evidence: Dict[str, Any],
    ) -> KnowledgeUpdateProposalDTO:
        record = self._proposals.call(
            self._proposals.repository.create_for_administration,
            record=KnowledgeProposalRecord(
                proposal_id=str(uuid.uuid4()),
                agent_id=agent_id,
                tool_name=tool_name,
                artifact_path=artifact_path,
                diff_payload=diff_payload,
                artifact_sha_at_generation=artifact_sha_at_gen,
                evidence=evidence,
                status=ProposalStatus.PENDING,
                reviewer_user_id=None,
                reviewed_at=None,
                reviewer_rationale=None,
                applied_at=None,
                generated_at=_utcnow(),
            ),
        )
        return _proposal_record_to_dto(record)

    def get_proposal(self, proposal_id: str) -> Optional[KnowledgeUpdateProposalDTO]:
        record = self._proposals.call(
            self._proposals.repository.get_for_administration,
            proposal_id=proposal_id,
        )
        return None if record is None else _proposal_record_to_dto(record)

    def list_proposals(
        self,
        *,
        status: Optional[str] = None,
        agent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[KnowledgeUpdateProposalDTO], Optional[str]]:
        before_generated_at = None
        before_proposal_id = None
        if cursor:
            try:
                c_data = json.loads(cursor)
                before_generated_at = datetime.fromisoformat(c_data["t"])
                before_proposal_id = str(c_data["i"])
            except Exception:
                pass
        records = self._proposals.call(
            self._proposals.repository.list_for_administration,
            status=status,
            agent_id=agent_id,
            tool_name=tool_name,
            limit=limit + 1,
            before_generated_at=before_generated_at,
            before_proposal_id=before_proposal_id,
        )
        dtos = [_proposal_record_to_dto(record) for record in records[:limit]]
        next_cursor = None
        if len(records) > limit:
            last = records[limit - 1]
            next_cursor = json.dumps(
                {"t": last.generated_at.isoformat(), "i": last.proposal_id}
            )
        return dtos, next_cursor

    def transition_proposal(
        self,
        proposal_id: str,
        *,
        new_status: str,
        reviewer_user_id: str,
        reviewer_rationale: Optional[str] = None,
        applied: bool = False,
    ) -> Optional[KnowledgeUpdateProposalDTO]:
        """Atomic state transition for accept / reject / apply."""
        existing = self._proposals.call(
            self._proposals.repository.get_for_administration,
            proposal_id=proposal_id,
        )
        if existing is None:
            return None
        expected = (
            ProposalStatus.ACCEPTED if applied else existing.status
        )
        record = self._proposals.call(
            self._proposals.repository.transition_for_administration,
            proposal_id=proposal_id,
            expected_status=expected,
            status=new_status,
            reviewer_user_id=reviewer_user_id,
            reviewed_at=_utcnow(),
            reviewer_rationale=reviewer_rationale,
        )
        return _proposal_record_to_dto(record)

    def pending_count(self) -> int:
        return self._proposals.call(
            self._proposals.repository.pending_count_for_administration,
        )

    def underperforming_count(self) -> int:
        """Count of distinct (agent, tool) currently in 'underperforming' state."""
        return self._quality.call(
            self._quality.repository.underperforming_count_for_administration,
        )


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------

def _feedback_record_to_dto(record: FeedbackRecord) -> ComponentFeedbackDTO:
    return ComponentFeedbackDTO(
        id=record.feedback_id,
        user_id=record.owner_id,
        conversation_id=record.conversation_id,
        correlation_id=record.correlation_id,
        source_agent=record.source_agent,
        source_tool=record.source_tool,
        component_id=record.component_id,
        sentiment=record.sentiment,
        category=record.category,
        comment_raw=record.comment,
        comment_safety=record.comment_safety,
        comment_safety_reason=record.comment_safety_reason,
        lifecycle=record.lifecycle,
        superseded_by=record.superseded_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _quality_record_to_dto(record: Any) -> ToolQualitySignalDTO:
    return ToolQualitySignalDTO(
        id=record.signal_id,
        agent_id=record.agent_id,
        tool_name=record.tool_name,
        window_start=record.window_start,
        window_end=record.window_end,
        dispatch_count=record.dispatch_count,
        failure_count=record.failure_count,
        negative_feedback_count=record.negative_feedback_count,
        failure_rate=record.failure_rate,
        negative_feedback_rate=record.negative_feedback_rate,
        status=str(record.status),
        computed_at=record.computed_at,
    )


def _proposal_record_to_dto(record: Any) -> KnowledgeUpdateProposalDTO:
    return KnowledgeUpdateProposalDTO(
        id=record.proposal_id,
        agent_id=record.agent_id,
        tool_name=record.tool_name,
        artifact_path=record.artifact_path,
        diff_payload=record.diff_payload,
        artifact_sha_at_gen=record.artifact_sha_at_generation,
        evidence=dict(record.evidence),
        status=record.status.value,
        reviewer_user_id=record.reviewer_user_id,
        reviewed_at=record.reviewed_at,
        reviewer_rationale=record.reviewer_rationale,
        applied_at=record.applied_at,
        generated_at=record.generated_at,
    )


def _quarantine_record_to_dto(record: Any) -> QuarantineEntryDTO:
    return QuarantineEntryDTO(
        feedback_id=record.feedback_id,
        reason=record.reason,
        detector=record.detector,
        detected_at=record.detected_at,
        status=record.status.value,
        actor_user_id=record.actor_user_id,
        actioned_at=record.actioned_at,
    )


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None
