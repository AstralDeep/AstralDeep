"""Feedback, onboarding-state, personalization profile/memory, theme, chat organization and
data-sharing-consent persistence. Grouped behind PreferencesRepository; used by
AstralDeep's feedback, onboarding, personalization and history modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from astralplane.contracts import QueryExecutor, Transaction
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
    _bounded_limit,
    _bounded_text,
    _canonical_json,
    _non_negative_int,
    _required_id,
    _row_value,
    _single_returned,
    _structured_json,
)
from astralplane.repositories.guidance import ExplicitNotesRepository, SkillsRepository
from astralplane.repositories.harness_preferences import HarnessPreferenceRepository

_SENTIMENTS = frozenset({"negative", "positive"})
_FEEDBACK_CATEGORIES = frozenset(
    {"irrelevant", "layout-broken", "other", "too-slow", "unspecified", "wrong-data"}
)
_COMMENT_SAFETY = frozenset({"clean", "quarantined"})
_FEEDBACK_LIFECYCLES = frozenset({"active", "retracted", "superseded"})
_ONBOARDING_STATES = frozenset({"completed", "in_progress", "not_started", "skipped"})
_MEMORY_CATEGORIES = frozenset({"context", "goal", "preference", "profession", "workflow_tag"})
_MEMORY_SOURCES = frozenset({"explicit", "promoted"})
CHAT_ORGANIZATION_KEY = "chat_organization"
CHAT_ORGANIZATION_FLAGS = ("pinned", "archived")
CHAT_ORGANIZATION_LIMIT = 500


@dataclass(frozen=True, slots=True)
class ChatOrganizationRecord:
    owner_id: str
    pinned: tuple[str, ...]
    archived: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    feedback_id: str
    owner_id: str
    conversation_id: str | None
    correlation_id: str | None
    source_agent: str | None
    source_tool: str | None
    component_id: str | None
    sentiment: str
    category: str
    comment: str | None
    comment_safety: str
    comment_safety_reason: str | None
    lifecycle: str
    superseded_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FeedbackCursor:
    created_at: datetime
    feedback_id: str


@dataclass(frozen=True, slots=True)
class FeedbackPage:
    records: tuple[FeedbackRecord, ...]
    next_cursor: FeedbackCursor | None


@dataclass(frozen=True, slots=True)
class FeedbackCommentCandidate:
    feedback_id: str
    owner_id: str
    comment: str


@dataclass(frozen=True, slots=True)
class OnboardingStateRecord:
    owner_id: str
    status: str
    last_step_id: int | None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    skipped_at: datetime | None
    dismissed_at: datetime | None
    dismiss_count: int


@dataclass(frozen=True, slots=True)
class PersonalizationProfileRecord:
    owner_id: str
    profession: str | None
    goals: tuple[Any, ...]
    personality: Mapping[str, Any]
    dreaming_enabled: bool
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    owner_id: str
    category: str
    value: str
    source: str
    salience: float
    created_at: int
    updated_at: int
    superseded_by: str | None
    superseded_at: int | None
    keywords: str | None
    signature: str | None
    valid_from: int | None
    valid_to: int | None
    ingested_at: int | None
    recall_count: int
    last_recalled_at: int | None
    project_id: str | None


@dataclass(frozen=True, slots=True)
class MemoryEnvelopeCursor:
    owner_id: str
    project_id: str | None
    envelope_format: str
    envelope_kind: str
    created_at: int
    memory_id: str


@dataclass(frozen=True, slots=True)
class MemoryEnvelopePage:
    records: tuple[MemoryRecord, ...]
    next_cursor: MemoryEnvelopeCursor | None


@dataclass(frozen=True, slots=True)
class PersonaRecord:
    owner_id: str
    persona: str
    score: float
    updated_at: int


@dataclass(frozen=True, slots=True)
class ThemePreferenceRecord:
    owner_id: str
    theme: Mapping[str, Any]
    updated_at: int | None


def _optional_returned(result: object, operation: str) -> Any:
    if not getattr(result, "returned_records", ()):
        return None
    return _single_returned(result, operation)


def _aware_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryValidationError(f"{field} must be a timezone-aware datetime")
    return value


def _stored_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryDataError(
            "persisted timestamp is not timezone-aware", metadata={"field": field}
        )
    return value


def _optional_id(value: object, field: str) -> str | None:
    return None if value is None else _required_id(value, field)


def _feedback(row: Mapping[str, Any]) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=str(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        conversation_id=(
            None if row.get("conversation_id") is None else str(row["conversation_id"])
        ),
        correlation_id=(None if row.get("correlation_id") is None else str(row["correlation_id"])),
        source_agent=(None if row.get("source_agent") is None else str(row["source_agent"])),
        source_tool=(None if row.get("source_tool") is None else str(row["source_tool"])),
        component_id=(None if row.get("component_id") is None else str(row["component_id"])),
        sentiment=str(_row_value(row, "sentiment")),
        category=str(_row_value(row, "category")),
        comment=None if row.get("comment_raw") is None else str(row["comment_raw"]),
        comment_safety=str(_row_value(row, "comment_safety")),
        comment_safety_reason=(
            None if row.get("comment_safety_reason") is None else str(row["comment_safety_reason"])
        ),
        lifecycle=str(_row_value(row, "lifecycle")),
        superseded_by=(None if row.get("superseded_by") is None else str(row["superseded_by"])),
        created_at=_stored_time(_row_value(row, "created_at"), "created_at"),
        updated_at=_stored_time(_row_value(row, "updated_at"), "updated_at"),
    )


def _onboarding(row: Mapping[str, Any]) -> OnboardingStateRecord:
    return OnboardingStateRecord(
        owner_id=str(_row_value(row, "user_id")),
        status=str(_row_value(row, "status")),
        last_step_id=(None if row.get("last_step_id") is None else int(row["last_step_id"])),
        started_at=_stored_time(_row_value(row, "started_at"), "started_at"),
        updated_at=_stored_time(_row_value(row, "updated_at"), "updated_at"),
        completed_at=(
            None
            if row.get("completed_at") is None
            else _stored_time(row["completed_at"], "completed_at")
        ),
        skipped_at=(
            None if row.get("skipped_at") is None else _stored_time(row["skipped_at"], "skipped_at")
        ),
        dismissed_at=(
            None
            if row.get("dismissed_at") is None
            else _stored_time(row["dismissed_at"], "dismissed_at")
        ),
        dismiss_count=int(row.get("dismiss_count") or 0),
    )


def _theme_preference(row: Mapping[str, Any]) -> ThemePreferenceRecord:
    preferences = _structured_json(_row_value(row, "preferences"), "preferences")
    if not isinstance(preferences, Mapping):
        raise RepositoryDataError("persisted preferences document must be an object")
    theme = preferences.get("theme", {})
    if not isinstance(theme, Mapping):
        raise RepositoryDataError("persisted theme preference must be an object")
    updated_at_raw = row.get("updated_at")
    if updated_at_raw is None:
        updated_at = None
    else:
        try:
            updated_at = int(updated_at_raw)
        except (TypeError, ValueError) as exc:
            raise RepositoryDataError("persisted theme timestamp is invalid") from exc
        if updated_at < 0:
            raise RepositoryDataError("persisted theme timestamp is negative")
    return ThemePreferenceRecord(
        owner_id=str(_row_value(row, "user_id")),
        theme=_structured_json(theme, "theme"),
        updated_at=updated_at,
    )


def _profile(row: Mapping[str, Any]) -> PersonalizationProfileRecord:
    goals = _structured_json(_row_value(row, "goals"), "goals")
    personality = _structured_json(_row_value(row, "personality"), "personality")
    if not isinstance(goals, tuple) or not isinstance(personality, Mapping):
        raise RepositoryDataError("personalization goals/personality have invalid shapes")
    return PersonalizationProfileRecord(
        owner_id=str(_row_value(row, "user_id")),
        profession=(None if row.get("profession") is None else str(row["profession"])),
        goals=goals,
        personality=personality,
        dreaming_enabled=bool(_row_value(row, "dreaming_enabled")),
        created_at=int(_row_value(row, "created_at")),
        updated_at=int(_row_value(row, "updated_at")),
    )


def _memory(row: Mapping[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        memory_id=str(_row_value(row, "id")),
        owner_id=str(_row_value(row, "user_id")),
        category=str(_row_value(row, "category")),
        value=str(_row_value(row, "value")),
        source=str(_row_value(row, "source")),
        salience=float(_row_value(row, "salience")),
        created_at=int(_row_value(row, "created_at")),
        updated_at=int(_row_value(row, "updated_at")),
        superseded_by=(None if row.get("superseded_by") is None else str(row["superseded_by"])),
        superseded_at=(None if row.get("superseded_at") is None else int(row["superseded_at"])),
        keywords=None if row.get("keywords") is None else str(row["keywords"]),
        signature=None if row.get("signature") is None else str(row["signature"]),
        valid_from=None if row.get("valid_from") is None else int(row["valid_from"]),
        valid_to=None if row.get("valid_to") is None else int(row["valid_to"]),
        ingested_at=None if row.get("ingested_at") is None else int(row["ingested_at"]),
        recall_count=int(row.get("recall_count") or 0),
        last_recalled_at=(
            None if row.get("last_recalled_at") is None else int(row["last_recalled_at"])
        ),
        project_id=None if row.get("project_id") is None else str(row["project_id"]),
    )


def _persona(row: Mapping[str, Any]) -> PersonaRecord:
    return PersonaRecord(
        owner_id=str(_row_value(row, "user_id")),
        persona=str(_row_value(row, "persona")),
        score=float(_row_value(row, "score")),
        updated_at=int(_row_value(row, "updated_at")),
    )



@dataclass(frozen=True, slots=True)
class DataSharingAcknowledgmentRecord:
    owner_id: str
    notice_version: str
    acknowledged_at: datetime
    first_acknowledged_at: datetime



def _acknowledged_time(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RepositoryValidationError(
            "acknowledgment time must be a timezone-aware datetime"
        )
    return value


def _stored_acknowledgment_time(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryDataError(
            "persisted timestamp is not timezone-aware",
            metadata={"field": field_name},
        )
    return value


def _acknowledgment_record(
    row: Mapping[str, Any],
) -> DataSharingAcknowledgmentRecord:
    return DataSharingAcknowledgmentRecord(
        owner_id=str(_row_value(row, "user_id")),
        notice_version=_bounded_text(
            _row_value(row, "notice_version"), "notice_version", maximum=64
        ),
        acknowledged_at=_stored_acknowledgment_time(
            _row_value(row, "acknowledged_at"), "acknowledged_at"
        ),
        first_acknowledged_at=_stored_acknowledgment_time(
            _row_value(row, "first_acknowledged_at"), "first_acknowledged_at"
        ),
    )


class FeedbackRepository:
    _FIELDS = """
        id, user_id, conversation_id, correlation_id, source_agent,
        source_tool, component_id, sentiment, category, comment_raw,
        comment_safety, comment_safety_reason, lifecycle, superseded_by,
        created_at, updated_at
    """

    def submit(
        self,
        transaction: Transaction,
        record: FeedbackRecord,
    ) -> FeedbackRecord:
        feedback_id = _required_id(record.feedback_id, "feedback_id")
        owner_id = _required_id(record.owner_id, "owner_id")
        conversation_id = _optional_id(record.conversation_id, "conversation_id")
        correlation_id = _optional_id(record.correlation_id, "correlation_id")
        source_agent = _optional_id(record.source_agent, "source_agent")
        source_tool = _optional_id(record.source_tool, "source_tool")
        component_id = _optional_id(record.component_id, "component_id")
        if record.sentiment not in _SENTIMENTS:
            raise RepositoryValidationError("feedback sentiment is unsupported")
        if record.category not in _FEEDBACK_CATEGORIES:
            raise RepositoryValidationError("feedback category is unsupported")
        if record.comment_safety not in _COMMENT_SAFETY:
            raise RepositoryValidationError("feedback comment safety is unsupported")
        if record.lifecycle != "active" or record.superseded_by is not None:
            raise RepositoryValidationError("new feedback must begin active and unsuperseded")
        if record.comment is not None:
            _bounded_text(record.comment, "comment", maximum=8192, allow_empty=True)
        if record.comment_safety_reason is not None:
            _bounded_text(
                record.comment_safety_reason,
                "comment_safety_reason",
                maximum=1024,
                allow_empty=True,
            )
        created_at = _aware_time(record.created_at, "created_at")
        updated_at = _aware_time(record.updated_at, "updated_at")
        if updated_at < created_at:
            raise RepositoryValidationError("feedback updated_at cannot precede created_at")
        result = transaction.execute(
            f"""
            INSERT INTO component_feedback (
                id, user_id, conversation_id, correlation_id, source_agent,
                source_tool, component_id, sentiment, category, comment_raw,
                comment_safety, comment_safety_reason, lifecycle, superseded_by,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      'active', NULL, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._FIELDS}
            """,
            (
                feedback_id,
                owner_id,
                conversation_id,
                correlation_id,
                source_agent,
                source_tool,
                component_id,
                record.sentiment,
                record.category,
                record.comment,
                record.comment_safety,
                record.comment_safety_reason,
                created_at,
                updated_at,
            ),
        )
        row = _optional_returned(result, "feedback.submit")
        if row is not None:
            return _feedback(row)
        existing = self.get(transaction, owner_id=owner_id, feedback_id=feedback_id)
        if existing is None:
            raise RepositoryConflictError(
                "feedback identity is owned by another namespace",
                metadata={"operation": "feedback.submit"},
            )
        expected = (
            conversation_id,
            correlation_id,
            source_agent,
            source_tool,
            component_id,
            record.sentiment,
            record.category,
            record.comment,
            record.comment_safety,
            record.comment_safety_reason,
            created_at,
            updated_at,
        )
        observed = (
            existing.conversation_id,
            existing.correlation_id,
            existing.source_agent,
            existing.source_tool,
            existing.component_id,
            existing.sentiment,
            existing.category,
            existing.comment,
            existing.comment_safety,
            existing.comment_safety_reason,
            existing.created_at,
            existing.updated_at,
        )
        if expected != observed:
            raise RepositoryConflictError(
                "feedback idempotency identity was reused with different semantics",
                metadata={"operation": "feedback.submit"},
            )
        return existing

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        feedback_id: str,
    ) -> FeedbackRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        feedback_id = _required_id(feedback_id, "feedback_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM component_feedback WHERE id = %s AND user_id = %s",
            (feedback_id, owner_id),
        )
        return None if row is None else _feedback(row)

    def find_in_dedup_window(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        correlation_id: str | None,
        component_id: str | None,
        cutoff: datetime,
    ) -> FeedbackRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        correlation_id = _optional_id(correlation_id, "correlation_id")
        component_id = _optional_id(component_id, "component_id")
        cutoff = _aware_time(cutoff, "cutoff")
        row = query.fetch_one(
            f"""
            SELECT {self._FIELDS}
            FROM component_feedback
            WHERE user_id = %s
              AND correlation_id IS NOT DISTINCT FROM %s
              AND component_id IS NOT DISTINCT FROM %s
              AND lifecycle = 'active'
              AND created_at >= %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (owner_id, correlation_id, component_id, cutoff),
        )
        return None if row is None else _feedback(row)

    def amend_active(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        feedback_id: str,
        expected_updated_at: datetime,
        sentiment: str,
        category: str,
        comment: str | None,
        comment_safety: str,
        comment_safety_reason: str | None,
        updated_at: datetime,
    ) -> FeedbackRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        feedback_id = _required_id(feedback_id, "feedback_id")
        expected_updated_at = _aware_time(expected_updated_at, "expected_updated_at")
        updated_at = _aware_time(updated_at, "updated_at")
        if updated_at <= expected_updated_at:
            raise RepositoryValidationError("feedback updated_at must advance the CAS fence")
        if sentiment not in _SENTIMENTS:
            raise RepositoryValidationError("feedback sentiment is unsupported")
        if category not in _FEEDBACK_CATEGORIES:
            raise RepositoryValidationError("feedback category is unsupported")
        if comment_safety not in _COMMENT_SAFETY:
            raise RepositoryValidationError("feedback comment safety is unsupported")
        if comment is not None:
            _bounded_text(comment, "comment", maximum=8192, allow_empty=True)
        if comment_safety_reason is not None:
            _bounded_text(
                comment_safety_reason,
                "comment_safety_reason",
                maximum=1024,
                allow_empty=True,
            )
        result = transaction.execute(
            f"""
            UPDATE component_feedback
            SET sentiment = %s,
                category = %s,
                comment_raw = %s,
                comment_safety = %s,
                comment_safety_reason = %s,
                updated_at = %s
            WHERE id = %s
              AND user_id = %s
              AND lifecycle = 'active'
              AND updated_at = %s
            RETURNING {self._FIELDS}
            """,
            (
                sentiment,
                category,
                comment,
                comment_safety,
                comment_safety_reason,
                updated_at,
                feedback_id,
                owner_id,
                expected_updated_at,
            ),
        )
        row = _optional_returned(result, "feedback.amend_active")
        if row is not None:
            return _feedback(row)
        existing = self.get(transaction, owner_id=owner_id, feedback_id=feedback_id)
        if existing is None:
            return None
        raise RepositoryConflictError(
            "feedback is no longer active at the expected version",
            metadata={"operation": "feedback.amend_active"},
        )

    def list_page(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        lifecycle: str = "active",
        source_tool: str | None = None,
        source_agent: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        cursor: FeedbackCursor | None = None,
        limit: int = 50,
    ) -> FeedbackPage:
        owner_id = _required_id(owner_id, "owner_id")
        if lifecycle not in _FEEDBACK_LIFECYCLES:
            raise RepositoryValidationError("feedback lifecycle is unsupported")
        source_tool = _optional_id(source_tool, "source_tool")
        source_agent = _optional_id(source_agent, "source_agent")
        if from_time is not None:
            from_time = _aware_time(from_time, "from_time")
        if to_time is not None:
            to_time = _aware_time(to_time, "to_time")
        if from_time is not None and to_time is not None and from_time > to_time:
            raise RepositoryValidationError("feedback from_time cannot follow to_time")
        if cursor is not None:
            if not isinstance(cursor, FeedbackCursor):
                raise RepositoryValidationError("feedback cursor has an unsupported type")
            cursor_time = _aware_time(cursor.created_at, "cursor.created_at")
            cursor_id = _required_id(cursor.feedback_id, "cursor.feedback_id")
        else:
            cursor_time = None
            cursor_id = None
        limit = _bounded_limit(limit)

        clauses = ["user_id = %s", "lifecycle = %s"]
        parameters: list[object] = [owner_id, lifecycle]
        if source_tool is not None:
            clauses.append("source_tool = %s")
            parameters.append(source_tool)
        if source_agent is not None:
            clauses.append("source_agent = %s")
            parameters.append(source_agent)
        if from_time is not None:
            clauses.append("created_at >= %s")
            parameters.append(from_time)
        if to_time is not None:
            clauses.append("created_at <= %s")
            parameters.append(to_time)
        if cursor_time is not None:
            clauses.append("(created_at, id::text) < (%s, %s)")
            parameters.extend((cursor_time, cursor_id))
        parameters.append(limit + 1)
        rows = query.fetch_all(
            f"""
            SELECT {self._FIELDS}
            FROM component_feedback
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id::text DESC
            LIMIT %s
            """,
            tuple(parameters),
        )
        records = tuple(_feedback(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1]
            next_cursor = FeedbackCursor(
                created_at=last.created_at,
                feedback_id=last.feedback_id,
            )
        return FeedbackPage(records=records, next_cursor=next_cursor)

    def list_for_owner(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        lifecycle: str = "active",
        limit: int = 50,
    ) -> tuple[FeedbackRecord, ...]:
        return self.list_page(
            query,
            owner_id=owner_id,
            lifecycle=lifecycle,
            limit=limit,
        ).records

    def list_clean_comment_candidates_for_administration(
        self,
        query: QueryExecutor,
        *,
        since: datetime,
        limit: int = 500,
    ) -> tuple[FeedbackCommentCandidate, ...]:
        since = _aware_time(since, "since")
        limit = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            """
            SELECT id, user_id, comment_raw
            FROM component_feedback
            WHERE lifecycle = 'active'
              AND comment_safety = 'clean'
              AND comment_raw IS NOT NULL
              AND comment_raw <> ''
              AND created_at >= %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (since, limit),
        )
        candidates: list[FeedbackCommentCandidate] = []
        for row in rows:
            comment = _row_value(row, "comment_raw")
            if not isinstance(comment, str) or not comment or len(comment) > 8192:
                raise RepositoryDataError("persisted feedback comment is outside safe bounds")
            candidates.append(
                FeedbackCommentCandidate(
                    feedback_id=str(_row_value(row, "id")),
                    owner_id=str(_row_value(row, "user_id")),
                    comment=comment,
                )
            )
        return tuple(candidates)

    def retract(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        feedback_id: str,
        updated_at: datetime,
    ) -> FeedbackRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        feedback_id = _required_id(feedback_id, "feedback_id")
        updated_at = _aware_time(updated_at, "updated_at")
        result = transaction.execute(
            f"""
            UPDATE component_feedback
            SET lifecycle = 'retracted', updated_at = %s
            WHERE id = %s AND user_id = %s AND lifecycle = 'active'
            RETURNING {self._FIELDS}
            """,
            (updated_at, feedback_id, owner_id),
        )
        row = _optional_returned(result, "feedback.retract")
        return None if row is None else _feedback(row)

    def supersede(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        old_feedback_id: str,
        replacement: FeedbackRecord,
        updated_at: datetime,
    ) -> FeedbackRecord:
        owner_id = _required_id(owner_id, "owner_id")
        old_feedback_id = _required_id(old_feedback_id, "old_feedback_id")
        if replacement.owner_id != owner_id:
            raise RepositoryValidationError("replacement feedback owner changed")
        updated_at = _aware_time(updated_at, "updated_at")
        new_record = self.submit(transaction, replacement)
        result = transaction.execute(
            """
            UPDATE component_feedback
            SET lifecycle = 'superseded', superseded_by = %s, updated_at = %s
            WHERE id = %s AND user_id = %s AND lifecycle = 'active'
            """,
            (new_record.feedback_id, updated_at, old_feedback_id, owner_id),
        )
        if result.rowcount == 1:
            return new_record
        old = self.get(transaction, owner_id=owner_id, feedback_id=old_feedback_id)
        if old is None:
            raise RepositoryNotFoundError(
                "feedback to supersede was not found",
                metadata={"operation": "feedback.supersede"},
            )
        if old.lifecycle == "superseded" and old.superseded_by == new_record.feedback_id:
            return new_record
        raise RepositoryConflictError(
            "feedback is no longer active",
            metadata={"operation": "feedback.supersede"},
        )


class OnboardingRepository:
    _FIELDS = """
        user_id, status, last_step_id, started_at, updated_at,
        completed_at, skipped_at, dismissed_at, dismiss_count
    """

    def get_state(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
    ) -> OnboardingStateRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM onboarding_state WHERE user_id = %s",
            (owner_id,),
        )
        return None if row is None else _onboarding(row)

    def put_state(
        self,
        transaction: Transaction,
        record: OnboardingStateRecord,
        *,
        expected_updated_at: datetime | None = None,
    ) -> OnboardingStateRecord:
        owner_id = _required_id(record.owner_id, "owner_id")
        if record.status not in _ONBOARDING_STATES:
            raise RepositoryValidationError("onboarding status is unsupported")
        if record.last_step_id is not None:
            _non_negative_int(record.last_step_id, "last_step_id")
        started_at = _aware_time(record.started_at, "started_at")
        updated_at = _aware_time(record.updated_at, "updated_at")
        completed_at = (
            None
            if record.completed_at is None
            else _aware_time(record.completed_at, "completed_at")
        )
        skipped_at = (
            None if record.skipped_at is None else _aware_time(record.skipped_at, "skipped_at")
        )
        dismissed_at = (
            None
            if record.dismissed_at is None
            else _aware_time(record.dismissed_at, "dismissed_at")
        )
        dismiss_count = _non_negative_int(record.dismiss_count, "dismiss_count")
        if expected_updated_at is None:
            result = transaction.execute(
                f"""
                INSERT INTO onboarding_state (
                    user_id, status, last_step_id, started_at, updated_at,
                    completed_at, skipped_at, dismissed_at, dismiss_count
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING {self._FIELDS}
                """,
                (
                    owner_id,
                    record.status,
                    record.last_step_id,
                    started_at,
                    updated_at,
                    completed_at,
                    skipped_at,
                    dismissed_at,
                    dismiss_count,
                ),
            )
        else:
            expected_updated_at = _aware_time(expected_updated_at, "expected_updated_at")
            if updated_at <= expected_updated_at:
                raise RepositoryValidationError(
                    "onboarding updated_at must advance the compare-and-set fence"
                )
            result = transaction.execute(
                f"""
                UPDATE onboarding_state
                SET status = %s, last_step_id = %s, updated_at = %s,
                    completed_at = %s, skipped_at = %s,
                    dismissed_at = %s, dismiss_count = %s
                WHERE user_id = %s AND updated_at = %s
                RETURNING {self._FIELDS}
                """,
                (
                    record.status,
                    record.last_step_id,
                    updated_at,
                    completed_at,
                    skipped_at,
                    dismissed_at,
                    dismiss_count,
                    owner_id,
                    expected_updated_at,
                ),
            )
        row = _optional_returned(result, "onboarding.put_state")
        if row is not None:
            return _onboarding(row)
        existing = self.get_state(transaction, owner_id=owner_id)
        if existing is None:
            raise RepositoryNotFoundError(
                "onboarding state was not found",
                metadata={"operation": "onboarding.put_state"},
            )
        if expected_updated_at is None and existing == record:
            return existing
        raise RepositoryConflictError(
            "onboarding state changed since it was read",
            metadata={"operation": "onboarding.put_state"},
        )

    def record_dismissal(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        dismissed_at: datetime,
    ) -> OnboardingStateRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        dismissed_at = _aware_time(dismissed_at, "dismissed_at")
        result = transaction.execute(
            f"""
            UPDATE onboarding_state
            SET dismissed_at = %s, dismiss_count = dismiss_count + 1,
                updated_at = %s
            WHERE user_id = %s
            RETURNING {self._FIELDS}
            """,
            (dismissed_at, dismissed_at, owner_id),
        )
        row = _optional_returned(result, "onboarding.record_dismissal")
        return None if row is None else _onboarding(row)


class PersonalizationRepository(ExplicitNotesRepository):
    _PROFILE_FIELDS = """
        user_id, profession, goals, personality, dreaming_enabled,
        created_at, updated_at
    """
    _MEMORY_FIELDS = """
        id, user_id, category, value, source, salience, created_at, updated_at,
        superseded_by, superseded_at, keywords, signature, valid_from, valid_to,
        ingested_at, recall_count, last_recalled_at, project_id
    """

    def get_profile(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
    ) -> PersonalizationProfileRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            f"SELECT {self._PROFILE_FIELDS} FROM user_personalization WHERE user_id = %s",
            (owner_id,),
        )
        return None if row is None else _profile(row)

    def put_profile(
        self,
        transaction: Transaction,
        record: PersonalizationProfileRecord,
        *,
        expected_updated_at: int | None = None,
    ) -> PersonalizationProfileRecord:
        owner_id = _required_id(record.owner_id, "owner_id")
        if record.profession is not None:
            _bounded_text(record.profession, "profession", maximum=1024, allow_empty=True)
        created_at = _non_negative_int(record.created_at, "created_at")
        updated_at = _non_negative_int(record.updated_at, "updated_at")
        goals = _canonical_json(tuple(record.goals), "goals")
        personality = _canonical_json(dict(record.personality), "personality")
        if expected_updated_at is None:
            result = transaction.execute(
                f"""
                INSERT INTO user_personalization (
                    user_id, profession, goals, personality, dreaming_enabled,
                    created_at, updated_at
                ) VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING {self._PROFILE_FIELDS}
                """,
                (
                    owner_id,
                    record.profession,
                    goals,
                    personality,
                    bool(record.dreaming_enabled),
                    created_at,
                    updated_at,
                ),
            )
        else:
            expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
            if updated_at <= expected_updated_at:
                raise RepositoryValidationError(
                    "profile updated_at must advance the compare-and-set fence"
                )
            result = transaction.execute(
                f"""
                UPDATE user_personalization
                SET profession = %s, goals = %s::jsonb, personality = %s::jsonb,
                    dreaming_enabled = %s, updated_at = %s
                WHERE user_id = %s AND updated_at = %s
                RETURNING {self._PROFILE_FIELDS}
                """,
                (
                    record.profession,
                    goals,
                    personality,
                    bool(record.dreaming_enabled),
                    updated_at,
                    owner_id,
                    expected_updated_at,
                ),
            )
        row = _optional_returned(result, "personalization.put_profile")
        if row is not None:
            return _profile(row)
        existing = self.get_profile(transaction, owner_id=owner_id)
        if existing is None:
            raise RepositoryNotFoundError(
                "personalization profile was not found",
                metadata={"operation": "personalization.put_profile"},
            )
        if expected_updated_at is None and existing == record:
            return existing
        raise RepositoryConflictError(
            "personalization profile changed since it was read",
            metadata={"operation": "personalization.put_profile"},
        )

    def reset_profile(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        updated_at: int,
        expected_updated_at: int,
    ) -> PersonalizationProfileRecord:
        owner_id = _required_id(owner_id, "owner_id")
        expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
        updated_at = _non_negative_int(updated_at, "updated_at")
        if updated_at <= expected_updated_at:
            raise RepositoryValidationError("profile updated_at must advance the CAS fence")
        result = transaction.execute(
            f"""
            UPDATE user_personalization
            SET profession = NULL,
                goals = '[]'::jsonb,
                personality = '{{}}'::jsonb,
                updated_at = %s
            WHERE user_id = %s AND updated_at = %s
            RETURNING {self._PROFILE_FIELDS}
            """,
            (updated_at, owner_id, expected_updated_at),
        )
        row = _optional_returned(result, "personalization.reset_profile")
        if row is not None:
            return _profile(row)
        existing = self.get_profile(transaction, owner_id=owner_id)
        if existing is None:
            raise RepositoryNotFoundError(
                "personalization profile was not found",
                metadata={"operation": "personalization.reset_profile"},
            )
        raise RepositoryConflictError(
            "personalization profile changed since it was read",
            metadata={"operation": "personalization.reset_profile"},
        )

    def create_memory(
        self,
        transaction: Transaction,
        record: MemoryRecord,
    ) -> MemoryRecord:
        memory_id = _required_id(record.memory_id, "memory_id")
        owner_id = _required_id(record.owner_id, "owner_id")
        if record.category not in _MEMORY_CATEGORIES:
            raise RepositoryValidationError("memory category is unsupported")
        if record.source not in _MEMORY_SOURCES:
            raise RepositoryValidationError("memory source is unsupported")
        value = _bounded_text(record.value, "value", maximum=16384)
        created_at = _non_negative_int(record.created_at, "created_at")
        updated_at = _non_negative_int(record.updated_at, "updated_at")
        if record.superseded_by is not None or record.superseded_at is not None:
            raise RepositoryValidationError("new memory must begin live")
        result = transaction.execute(
            f"""
            INSERT INTO memory_item (
                id, user_id, category, value, source, salience, created_at,
                updated_at, superseded_by, superseded_at, keywords, signature,
                valid_from, valid_to, ingested_at, recall_count,
                last_recalled_at, project_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s,
                      %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING {self._MEMORY_FIELDS}
            """,
            (
                memory_id,
                owner_id,
                record.category,
                value,
                record.source,
                float(record.salience),
                created_at,
                updated_at,
                record.keywords,
                record.signature,
                record.valid_from,
                record.valid_to,
                record.ingested_at,
                _non_negative_int(record.recall_count, "recall_count"),
                record.last_recalled_at,
                record.project_id,
            ),
        )
        row = _optional_returned(result, "personalization.create_memory")
        if row is not None:
            return _memory(row)
        existing = self.get_memory(transaction, owner_id=owner_id, memory_id=memory_id)
        if existing is None:
            raise RepositoryConflictError(
                "memory identity is owned by another namespace",
                metadata={"operation": "personalization.create_memory"},
            )
        if existing != record:
            raise RepositoryConflictError(
                "memory idempotency identity was reused with different semantics",
                metadata={"operation": "personalization.create_memory"},
            )
        return existing

    def get_memory(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        memory_id: str,
        for_update: bool = False,
    ) -> MemoryRecord | None:
        if type(for_update) is not bool:
            raise RepositoryValidationError("for_update must be a boolean")
        owner_id = _required_id(owner_id, "owner_id")
        memory_id = _required_id(memory_id, "memory_id")
        row = query.fetch_one(
            f"""
            SELECT {self._MEMORY_FIELDS}
            FROM memory_item
            WHERE id = %s AND user_id = %s
            """ + (" FOR UPDATE" if for_update else ""),
            (memory_id, owner_id),
        )
        return None if row is None else _memory(row)

    def list_memory(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        project_id: str | None = None,
        include_global: bool = True,
        global_only: bool = False,
        limit: int = 200,
    ) -> tuple[MemoryRecord, ...]:
        owner_id = _required_id(owner_id, "owner_id")
        if not isinstance(global_only, bool):
            raise RepositoryValidationError("global_only must be a boolean")
        if global_only and project_id is not None:
            raise RepositoryValidationError(
                "global_only cannot be combined with a concrete project_id"
            )
        limit = _bounded_limit(limit, maximum=1000)
        parameters: list[object] = [owner_id]
        project_clause = ""
        if global_only:
            project_clause = " AND project_id IS NULL"
        elif project_id is not None:
            project_id = _required_id(project_id, "project_id")
            if include_global:
                project_clause = " AND (project_id = %s OR project_id IS NULL)"
            else:
                project_clause = " AND project_id = %s"
            parameters.append(project_id)
        parameters.append(limit)
        rows = query.fetch_all(
            f"""
            SELECT {self._MEMORY_FIELDS}
            FROM memory_item
            WHERE user_id = %s AND superseded_at IS NULL{project_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            tuple(parameters),
        )
        return tuple(_memory(row) for row in rows)

    def list_memory_envelope_page(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        project_id: str | None,
        envelope_format: str,
        envelope_kind: str,
        cursor: MemoryEnvelopeCursor | None = None,
        limit: int = 50,
    ) -> MemoryEnvelopePage:
        owner_id = _required_id(owner_id, "owner_id")
        project_id = _optional_id(project_id, "project_id")
        envelope_format = _required_id(envelope_format, "envelope_format", maximum=128)
        envelope_kind = _required_id(envelope_kind, "envelope_kind", maximum=128)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RepositoryValidationError("envelope page limit must be between 1 and 100")
        clauses = ["user_id=%s", "superseded_at IS NULL"]
        parameters: list[object] = [owner_id]
        if project_id is None:
            clauses.append("project_id IS NULL")
        else:
            clauses.append("project_id=%s")
            parameters.append(project_id)
        clauses.append(
            "CASE WHEN pg_input_is_valid(value,'jsonb') THEN "
            "jsonb_typeof(value::jsonb)='object' "
            "AND value::jsonb->'format'=to_jsonb(%s::text) "
            "AND value::jsonb->'kind'=to_jsonb(%s::text) "
            "AND value::jsonb->'owner_id'=to_jsonb(user_id) "
            "AND value::jsonb->'project_id'=COALESCE(to_jsonb(project_id),'null'::jsonb) "
            "ELSE FALSE END"
        )
        parameters.extend((envelope_format, envelope_kind))
        if cursor is not None:
            if (type(cursor) is not MemoryEnvelopeCursor
                    or (cursor.owner_id, cursor.project_id,
                        cursor.envelope_format, cursor.envelope_kind)
                    != (owner_id, project_id, envelope_format, envelope_kind)
                    or type(cursor.created_at) is not int
                    or not 0 <= cursor.created_at < 2 ** 63):
                raise RepositoryValidationError("envelope cursor is invalid for this scope")
            try:
                if (not isinstance(cursor.memory_id, str)
                        or str(UUID(cursor.memory_id)) != cursor.memory_id):
                    raise ValueError
            except (ValueError, AttributeError):
                raise RepositoryValidationError(
                    "envelope cursor memory identity is invalid"
                ) from None
            clauses.append("(created_at,id)<(%s,%s::uuid)")
            parameters.extend((cursor.created_at, cursor.memory_id))
        parameters.append(limit + 1)
        rows = query.fetch_all(
            f"SELECT {self._MEMORY_FIELDS} FROM memory_item WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC,id DESC LIMIT %s", tuple(parameters),
        )
        records = tuple(_memory(row) for row in rows[:limit])
        continuation = None
        if len(rows) > limit and records:
            last = records[-1]
            continuation = MemoryEnvelopeCursor(
                owner_id, project_id, envelope_format, envelope_kind,
                last.created_at, last.memory_id,
            )
        return MemoryEnvelopePage(records, continuation)

    def supersede_memory(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        memory_id: str,
        superseded_at: int,
        replacement_id: str | None = None,
    ) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        memory_id = _required_id(memory_id, "memory_id")
        replacement_id = _optional_id(replacement_id, "replacement_id")
        superseded_at = _non_negative_int(superseded_at, "superseded_at")
        result = transaction.execute(
            """
            UPDATE memory_item
            SET superseded_by = %s, superseded_at = %s, updated_at = %s
            WHERE id = %s AND user_id = %s AND superseded_at IS NULL
            """,
            (replacement_id, superseded_at, superseded_at, memory_id, owner_id),
        )
        if result.rowcount == 1:
            return True
        existing = self.get_memory(transaction, owner_id=owner_id, memory_id=memory_id)
        return bool(
            existing is not None
            and existing.superseded_at == superseded_at
            and existing.superseded_by == replacement_id
        )

    def set_validity(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        memory_id: str,
        valid_from: int | None,
        valid_to: int | None,
        ingested_at: int | None,
        updated_at: int,
        expected_updated_at: int,
    ) -> MemoryRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        memory_id = _required_id(memory_id, "memory_id")
        valid_from = (
            None if valid_from is None else _non_negative_int(valid_from, "valid_from")
        )
        valid_to = None if valid_to is None else _non_negative_int(valid_to, "valid_to")
        ingested_at = (
            None if ingested_at is None else _non_negative_int(ingested_at, "ingested_at")
        )
        if valid_from is not None and valid_to is not None and valid_from > valid_to:
            raise RepositoryValidationError("memory valid_from cannot follow valid_to")
        expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
        updated_at = _non_negative_int(updated_at, "updated_at")
        if updated_at <= expected_updated_at:
            raise RepositoryValidationError("memory updated_at must advance the CAS fence")
        result = transaction.execute(
            f"""
            UPDATE memory_item
            SET valid_from = %s,
                valid_to = %s,
                ingested_at = COALESCE(%s, ingested_at),
                updated_at = %s
            WHERE id = %s AND user_id = %s AND updated_at = %s
            RETURNING {self._MEMORY_FIELDS}
            """,
            (
                valid_from,
                valid_to,
                ingested_at,
                updated_at,
                memory_id,
                owner_id,
                expected_updated_at,
            ),
        )
        row = _optional_returned(result, "personalization.set_validity")
        if row is not None:
            return _memory(row)
        existing = self.get_memory(transaction, owner_id=owner_id, memory_id=memory_id)
        if existing is None:
            return None
        raise RepositoryConflictError(
            "memory changed since it was read",
            metadata={"operation": "personalization.set_validity"},
        )

    def update_memory_value(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        memory_id: str,
        value: str,
        signature: str | None,
        updated_at: int,
        expected_updated_at: int,
    ) -> MemoryRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        memory_id = _required_id(memory_id, "memory_id")
        value = _bounded_text(value, "value", maximum=16384)
        if signature is not None:
            signature = _bounded_text(signature, "signature", maximum=1024)
        expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
        updated_at = _non_negative_int(updated_at, "updated_at")
        if updated_at <= expected_updated_at:
            raise RepositoryValidationError("memory updated_at must advance the CAS fence")
        result = transaction.execute(
            f"""
            UPDATE memory_item
            SET value = %s, signature = %s, updated_at = %s
            WHERE id = %s AND user_id = %s AND updated_at = %s
            RETURNING {self._MEMORY_FIELDS}
            """,
            (value, signature, updated_at, memory_id, owner_id, expected_updated_at),
        )
        row = _optional_returned(result, "personalization.update_memory_value")
        if row is not None:
            return _memory(row)
        existing = self.get_memory(transaction, owner_id=owner_id, memory_id=memory_id)
        if existing is None:
            return None
        raise RepositoryConflictError(
            "memory changed since it was read",
            metadata={"operation": "personalization.update_memory_value"},
        )

    def delete_memory(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        memory_id: str,
        expected_updated_at: int,
    ) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        memory_id = _required_id(memory_id, "memory_id")
        expected_updated_at = _non_negative_int(expected_updated_at, "expected_updated_at")
        result = transaction.execute(
            """
            DELETE FROM memory_item
            WHERE id = %s AND user_id = %s AND updated_at = %s
            """,
            (memory_id, owner_id, expected_updated_at),
        )
        if result.rowcount == 1:
            return True
        existing = self.get_memory(transaction, owner_id=owner_id, memory_id=memory_id)
        if existing is None:
            return False
        raise RepositoryConflictError(
            "memory changed since it was read",
            metadata={"operation": "personalization.delete_memory"},
        )

    def record_recall(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        memory_id: str,
        recalled_at: int,
    ) -> MemoryRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        memory_id = _required_id(memory_id, "memory_id")
        recalled_at = _non_negative_int(recalled_at, "recalled_at")
        result = transaction.execute(
            f"""
            UPDATE memory_item
            SET recall_count = COALESCE(recall_count, 0) + 1,
                last_recalled_at = %s
            WHERE id = %s AND user_id = %s AND superseded_at IS NULL
            RETURNING {self._MEMORY_FIELDS}
            """,
            (recalled_at, memory_id, owner_id),
        )
        row = _optional_returned(result, "personalization.record_recall")
        return None if row is None else _memory(row)

    def put_persona(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        persona: str,
        score: float,
        updated_at: int,
    ) -> PersonaRecord:
        owner_id = _required_id(owner_id, "owner_id")
        persona = _bounded_text(persona, "persona", maximum=16384, allow_empty=True)
        updated_at = _non_negative_int(updated_at, "updated_at")
        result = transaction.execute(
            """
            INSERT INTO user_persona (user_id, persona, score, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                persona = EXCLUDED.persona,
                score = EXCLUDED.score,
                updated_at = EXCLUDED.updated_at
            WHERE user_persona.updated_at IS NULL
               OR user_persona.updated_at <= EXCLUDED.updated_at
            RETURNING user_id, persona, score, updated_at
            """,
            (owner_id, persona, float(score), updated_at),
        )
        row = _optional_returned(result, "personalization.put_persona")
        if row is None:
            raise RepositoryConflictError(
                "persona update is older than the durable value",
                metadata={"operation": "personalization.put_persona"},
            )
        return _persona(row)

    def get_persona(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
    ) -> PersonaRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            """
            SELECT user_id, persona, score, updated_at
            FROM user_persona
            WHERE user_id = %s
            """,
            (owner_id,),
        )
        return None if row is None else _persona(row)


class ThemePreferenceRepository:
    _SELECT = (
        "SELECT user_id, preferences, updated_at FROM user_preferences "
        "WHERE user_id = %s"
    )

    def get(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
    ) -> ThemePreferenceRecord | None:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(self._SELECT, (owner_id,))
        return None if row is None else _theme_preference(row)

    def put(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        theme: Mapping[str, object],
    ) -> ThemePreferenceRecord:
        owner_id = _required_id(owner_id, "owner_id")
        if not isinstance(theme, Mapping):
            raise RepositoryValidationError("theme must be a JSON object")
        theme_payload = _canonical_json(theme, "theme")
        if len(theme_payload.encode("utf-8")) > 65_536:
            raise RepositoryValidationError(
                "theme exceeds its maximum encoded size",
                metadata={"maximum_bytes": 65_536},
            )
        existing = transaction.fetch_one(self._SELECT + " FOR UPDATE", (owner_id,))
        if existing is None:
            inserted = transaction.execute(
                """
                INSERT INTO user_preferences (user_id, preferences, updated_at)
                VALUES (
                    %s, %s,
                    FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT
                )
                ON CONFLICT (user_id) DO NOTHING
                RETURNING user_id, preferences, updated_at
                """,
                (owner_id, _canonical_json({"theme": theme}, "preferences")),
            )
            row = _optional_returned(inserted, "theme_preference.put.insert")
            if row is not None:
                return _theme_preference(row)
            existing = transaction.fetch_one(
                self._SELECT + " FOR UPDATE", (owner_id,)
            )
            if existing is None:
                raise RepositoryConflictError(
                    "theme preference row disappeared during concurrent creation",
                    metadata={"operation": "theme_preference.put"},
                )
        preferences = _structured_json(
            _row_value(existing, "preferences"), "preferences"
        )
        if not isinstance(preferences, Mapping):
            raise RepositoryDataError("persisted preferences document must be an object")
        merged = dict(preferences)
        merged["theme"] = theme
        result = transaction.execute(
            """
            UPDATE user_preferences
            SET preferences = %s,
                updated_at = GREATEST(
                    COALESCE(updated_at, 0) + 1,
                    FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT
                )
            WHERE user_id = %s
            RETURNING user_id, preferences, updated_at
            """,
            (_canonical_json(merged, "preferences"), owner_id),
        )
        row = _optional_returned(result, "theme_preference.put.update")
        if row is None:
            raise RepositoryConflictError(
                "theme preference row changed after it was locked",
                metadata={"operation": "theme_preference.put"},
            )
        return _theme_preference(row)



class DataSharingAcknowledgmentRepository:
    _FIELDS = "user_id, notice_version, acknowledged_at, first_acknowledged_at"

    def get_user(
        self,
        executor: QueryExecutor,
        *,
        owner_id: str,
    ) -> DataSharingAcknowledgmentRecord | None:
        owner = _required_id(owner_id, "owner_id")
        row = executor.fetch_one(
            f"SELECT {self._FIELDS} FROM user_data_sharing_acknowledgment "
            "WHERE user_id = %s",
            (owner,),
        )
        return None if row is None else _acknowledgment_record(row)

    def acknowledge(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        notice_version: str,
        at: datetime,
    ) -> DataSharingAcknowledgmentRecord:
        owner = _required_id(owner_id, "owner_id")
        version = _bounded_text(notice_version, "notice_version", maximum=64)
        moment = _acknowledged_time(at)
        result = transaction.execute(
            f"""
            INSERT INTO user_data_sharing_acknowledgment (
                user_id, notice_version, acknowledged_at, first_acknowledged_at
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                notice_version = EXCLUDED.notice_version,
                acknowledged_at = GREATEST(
                    EXCLUDED.acknowledged_at,
                    user_data_sharing_acknowledgment.first_acknowledged_at
                ),
                first_acknowledged_at =
                    user_data_sharing_acknowledgment.first_acknowledged_at
            RETURNING {self._FIELDS}
            """,
            (owner, version, moment, moment),
        )
        return _acknowledgment_record(
            _single_returned(result, "acknowledge data sharing notice")
        )

    def has_acknowledged(
        self,
        executor: QueryExecutor,
        *,
        owner_id: str,
        notice_version: str,
    ) -> bool:
        version = _bounded_text(notice_version, "notice_version", maximum=64)
        record = self.get_user(executor, owner_id=owner_id)
        return record is not None and record.notice_version == version


def _chat_organization(value: object) -> dict[str, list[str]]:
    if value is None:
        return {flag: [] for flag in CHAT_ORGANIZATION_FLAGS}
    if not isinstance(value, Mapping):
        raise RepositoryDataError("persisted chat organization must be an object")
    out: dict[str, list[str]] = {}
    for flag in CHAT_ORGANIZATION_FLAGS:
        items = value.get(flag, [])
        if (
            not isinstance(items, (list, tuple))
            or any(not isinstance(item, str) or not item for item in items)
            or len(items) != len(set(items))
        ):
            raise RepositoryDataError(
                "persisted chat organization lists must hold distinct identities"
            )
        out[flag] = list(items)
    return out


def _preferences_document(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    preferences = _structured_json(_row_value(row, "preferences"), "preferences")
    if not isinstance(preferences, Mapping):
        raise RepositoryDataError("persisted preferences document must be an object")
    return dict(preferences)


class PreferencesRepository:
    def __init__(self) -> None:
        self.harness = HarnessPreferenceRepository()
        self.skills = SkillsRepository()
        self.feedback = FeedbackRepository()
        self.onboarding = OnboardingRepository()
        self.personalization = PersonalizationRepository()
        self.theme = ThemePreferenceRepository()
        self.data_sharing = DataSharingAcknowledgmentRepository()

    def get_chat_phi_notice_enabled(self, query: QueryExecutor, *, owner_id: str) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s", (owner_id,)
        )
        if row is None:
            return True
        preferences = _structured_json(_row_value(row, "preferences"), "preferences")
        if not isinstance(preferences, Mapping):
            raise RepositoryDataError("persisted preferences document must be an object")
        enabled = preferences.get("chat_phi_notice_enabled", True)
        if type(enabled) is not bool:
            raise RepositoryDataError("persisted chat notice preference must be boolean")
        return enabled

    def set_chat_phi_notice_enabled(
        self, transaction: Transaction, *, owner_id: str, enabled: bool
    ) -> None:
        owner_id = _required_id(owner_id, "owner_id")
        if type(enabled) is not bool:
            raise RepositoryValidationError("chat notice preference must be boolean")
        transaction.execute(
            "INSERT INTO user_preferences (user_id, preferences, updated_at) "
            "VALUES (%s, '{}', 0) ON CONFLICT (user_id) DO NOTHING", (owner_id,)
        )
        row = transaction.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s FOR UPDATE",
            (owner_id,),
        )
        if row is None:
            raise RepositoryConflictError("chat notice preference row disappeared")
        preferences = _structured_json(_row_value(row, "preferences"), "preferences")
        if not isinstance(preferences, Mapping):
            raise RepositoryDataError("persisted preferences document must be an object")
        merged = dict(preferences)
        merged["chat_phi_notice_enabled"] = enabled
        result = transaction.execute(
            "UPDATE user_preferences SET preferences = %s, "
            "updated_at = GREATEST(COALESCE(updated_at, 0) + 1, "
            "FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT) "
            "WHERE user_id = %s", (_canonical_json(merged, "preferences"), owner_id),
        )
        if result.rowcount != 1:
            raise RepositoryConflictError("chat notice preference update was not applied")

    def get_phi_protection_enabled(self, query: QueryExecutor, *, owner_id: str) -> bool:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s", (owner_id,)
        )
        if row is None:
            return False
        preferences = _structured_json(_row_value(row, "preferences"), "preferences")
        if not isinstance(preferences, Mapping):
            raise RepositoryDataError("persisted preferences document must be an object")
        enabled = preferences.get("phi_protection_enabled", False)
        if type(enabled) is not bool:
            raise RepositoryDataError("persisted PHI protection preference must be boolean")
        return enabled

    def set_phi_protection_enabled(
        self, transaction: Transaction, *, owner_id: str, enabled: bool
    ) -> None:
        owner_id = _required_id(owner_id, "owner_id")
        if type(enabled) is not bool:
            raise RepositoryValidationError("PHI protection preference must be boolean")
        transaction.execute(
            "INSERT INTO user_preferences (user_id, preferences, updated_at) "
            "VALUES (%s, '{}', 0) ON CONFLICT (user_id) DO NOTHING", (owner_id,)
        )
        row = transaction.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s FOR UPDATE",
            (owner_id,),
        )
        if row is None:
            raise RepositoryConflictError("PHI protection preference row disappeared")
        preferences = _structured_json(_row_value(row, "preferences"), "preferences")
        if not isinstance(preferences, Mapping):
            raise RepositoryDataError("persisted preferences document must be an object")
        merged = dict(preferences)
        merged["phi_protection_enabled"] = enabled
        result = transaction.execute(
            "UPDATE user_preferences SET preferences = %s, "
            "updated_at = GREATEST(COALESCE(updated_at, 0) + 1, "
            "FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT) "
            "WHERE user_id = %s", (_canonical_json(merged, "preferences"), owner_id),
        )
        if result.rowcount != 1:
            raise RepositoryConflictError("PHI protection preference update was not applied")

    def get_chat_organization(
        self, query: QueryExecutor, *, owner_id: str
    ) -> ChatOrganizationRecord:
        owner_id = _required_id(owner_id, "owner_id")
        row = query.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s", (owner_id,)
        )
        organization = _chat_organization(_preferences_document(row).get(CHAT_ORGANIZATION_KEY))
        return ChatOrganizationRecord(
            owner_id, tuple(organization["pinned"]), tuple(organization["archived"])
        )

    def set_chat_flag(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        conversation_id: str,
        flag: str,
        value: bool,
    ) -> ChatOrganizationRecord:
        owner_id = _required_id(owner_id, "owner_id")
        conversation_id = _required_id(conversation_id, "conversation_id")
        if flag not in CHAT_ORGANIZATION_FLAGS:
            raise RepositoryValidationError("chat organization flag is unsupported")
        if type(value) is not bool:
            raise RepositoryValidationError("chat organization value must be boolean")
        owned = transaction.fetch_one(
            "SELECT 1 AS present FROM chats WHERE id = %s AND user_id = %s",
            (conversation_id, owner_id),
        )
        if owned is None:
            raise RepositoryNotFoundError(
                "conversation was not found", metadata={"operation": "chat_organization.set"}
            )
        transaction.execute(
            "INSERT INTO user_preferences (user_id, preferences, updated_at) "
            "VALUES (%s, '{}', 0) ON CONFLICT (user_id) DO NOTHING",
            (owner_id,),
        )
        row = transaction.fetch_one(
            "SELECT preferences FROM user_preferences WHERE user_id = %s FOR UPDATE",
            (owner_id,),
        )
        if row is None:
            raise RepositoryConflictError("chat organization preference row disappeared")
        preferences = _preferences_document(row)
        organization = _chat_organization(preferences.get(CHAT_ORGANIZATION_KEY))
        known = sorted({item for items in organization.values() for item in items})
        present = {
            str(_row_value(found, "id"))
            for found in transaction.fetch_all(
                "SELECT id FROM chats WHERE user_id = %s AND id = ANY(%s)", (owner_id, known)
            )
        }
        organization = {
            name: [item for item in items if item in present]
            for name, items in organization.items()
        }
        current = [item for item in organization[flag] if item != conversation_id]
        if value:
            if len(current) >= CHAT_ORGANIZATION_LIMIT:
                raise RepositoryValidationError(
                    "too many conversations carry this flag",
                    metadata={"maximum": CHAT_ORGANIZATION_LIMIT},
                )
            current.insert(0, conversation_id)
        organization[flag] = current
        preferences[CHAT_ORGANIZATION_KEY] = organization
        result = transaction.execute(
            "UPDATE user_preferences SET preferences = %s, "
            "updated_at = GREATEST(COALESCE(updated_at, 0) + 1, "
            "FLOOR(EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT) "
            "WHERE user_id = %s",
            (_canonical_json(preferences, "preferences"), owner_id),
        )
        if result.rowcount != 1:
            raise RepositoryConflictError("chat organization update was not applied")
        return ChatOrganizationRecord(
            owner_id, tuple(organization["pinned"]), tuple(organization["archived"])
        )


__all__ = (
    "CHAT_ORGANIZATION_LIMIT",
    "ChatOrganizationRecord",
    "DataSharingAcknowledgmentRecord",
    "DataSharingAcknowledgmentRepository",
    "FeedbackCommentCandidate",
    "FeedbackCursor",
    "FeedbackPage",
    "FeedbackRecord",
    "FeedbackRepository",
    "MemoryEnvelopeCursor",
    "MemoryEnvelopePage",
    "MemoryRecord",
    "OnboardingRepository",
    "OnboardingStateRecord",
    "PersonaRecord",
    "PersonalizationProfileRecord",
    "PersonalizationRepository",
    "PreferencesRepository",
    "ThemePreferenceRecord",
    "ThemePreferenceRepository",
)
