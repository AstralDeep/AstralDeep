"""Registry of generated attachment parsers: global coverage once promoted, with
owner-scoped claim provenance kept separate. AstralPlane owns the unique-gap claim
and lifecycle CAS; AstralDeep decides whether a gap gets claimed or approved.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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


class AttachmentParserStatus(StrEnum):
    PENDING = "pending"
    LIVE = "live"
    FAILED = "failed"
    DISCARDED = "discarded"


class AttachmentParserClaimDisposition(StrEnum):
    CLAIMED = "claimed"
    OWNER_REPLAY = "owner_replay"
    GAP_ALREADY_CLAIMED = "gap_already_claimed"


@dataclass(frozen=True, slots=True)
class AttachmentParserCoverageRecord:
    parser_id: str
    extension: str | None
    category: str
    gap_fingerprint: str
    status: AttachmentParserStatus
    live_agent_id: str | None
    tool_name: str | None
    updated_at: int

    @property
    def covered(self) -> bool:
        return self.status is AttachmentParserStatus.LIVE and self.tool_name is not None


@dataclass(frozen=True, slots=True)
class AttachmentParserRecord:
    parser_id: str
    extension: str | None
    category: str
    gap_fingerprint: str
    status: AttachmentParserStatus
    draft_agent_id: str | None = field(repr=False)
    live_agent_id: str | None
    tool_name: str | None
    source_attachment_id: str | None = field(repr=False)
    source_conversation_id: str | None = field(repr=False)
    requested_by: str | None = field(repr=False)
    approved_by: str | None = field(repr=False)
    created_at: int
    updated_at: int

    @property
    def coverage(self) -> AttachmentParserCoverageRecord:
        return AttachmentParserCoverageRecord(
            parser_id=self.parser_id,
            extension=self.extension,
            category=self.category,
            gap_fingerprint=self.gap_fingerprint,
            status=self.status,
            live_agent_id=self.live_agent_id,
            tool_name=self.tool_name,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True, slots=True)
class AttachmentParserClaimResult:
    disposition: AttachmentParserClaimDisposition
    coverage: AttachmentParserCoverageRecord
    owner_record: AttachmentParserRecord | None = field(default=None, repr=False)


class AttachmentParserRepository:
    _FIELDS = (
        "id, extension, category, gap_fingerprint, status, draft_agent_id, "
        "live_agent_id, tool_name, source_attachment_id, source_chat_id, "
        "requested_by, approved_by, created_at, updated_at"
    )
    _COVERAGE_FIELDS = (
        "id, extension, category, gap_fingerprint, status, live_agent_id, "
        "tool_name, updated_at"
    )

    def claim_pending(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        gap_fingerprint: str,
        category: str,
        extension: str | None,
        draft_agent_id: str | None,
        source_attachment_id: str | None,
        source_conversation_id: str | None,
        claimed_at: int,
    ) -> AttachmentParserClaimResult:
        owner = _required_id(owner_id, "owner_id")
        gap = _required_id(gap_fingerprint, "gap_fingerprint", maximum=512)
        parser_category = _bounded_text(category, "category", maximum=128)
        parser_extension = _optional_text(extension, "extension", maximum=64)
        draft = _optional_id(draft_agent_id, "draft_agent_id")
        attachment = _optional_id(source_attachment_id, "source_attachment_id")
        conversation = _optional_id(source_conversation_id, "source_conversation_id")
        timestamp = _non_negative_int(claimed_at, "claimed_at")
        parser_id = str(uuid.uuid4())
        result = transaction.execute(
            f"""
            INSERT INTO attachment_parser (
                id, extension, category, gap_fingerprint, status,
                draft_agent_id, live_agent_id, tool_name,
                source_attachment_id, source_chat_id, requested_by, approved_by,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'pending', %s, NULL, NULL, %s, %s, %s, NULL, %s, %s)
            ON CONFLICT (gap_fingerprint) DO UPDATE SET
                extension = EXCLUDED.extension,
                category = EXCLUDED.category,
                status = 'pending',
                draft_agent_id = EXCLUDED.draft_agent_id,
                live_agent_id = NULL,
                tool_name = NULL,
                source_attachment_id = EXCLUDED.source_attachment_id,
                source_chat_id = EXCLUDED.source_chat_id,
                requested_by = EXCLUDED.requested_by,
                approved_by = NULL,
                updated_at = EXCLUDED.updated_at
            WHERE attachment_parser.status IN ('failed', 'discarded')
            RETURNING {self._FIELDS}
            """,
            (
                parser_id,
                parser_extension,
                parser_category,
                gap,
                draft,
                attachment,
                conversation,
                owner,
                timestamp,
                timestamp,
            ),
        )
        returned = getattr(result, "returned_records", ())
        if returned:
            record = _parser(_single_returned(result, "attachment_parser.claim"))
            if record.requested_by != owner:
                raise RepositoryDataError(
                    "parser claim returned another owner's provenance",
                    metadata={"operation": "attachment_parser.claim"},
                )
            return AttachmentParserClaimResult(
                disposition=AttachmentParserClaimDisposition.CLAIMED,
                coverage=record.coverage,
                owner_record=record,
            )
        if result.rowcount not in (0,):
            raise RepositoryDataError(
                "parser claim returned an invalid row count",
                metadata={"operation": "attachment_parser.claim"},
            )

        existing = self._get_for_administration(transaction, gap_fingerprint=gap)
        if existing is None:
            raise RepositoryConflictError(
                "parser gap claim lost its unique-row race",
                metadata={"operation": "attachment_parser.claim"},
            )
        if existing.requested_by == owner:
            return AttachmentParserClaimResult(
                disposition=AttachmentParserClaimDisposition.OWNER_REPLAY,
                coverage=existing.coverage,
                owner_record=existing,
            )
        return AttachmentParserClaimResult(
            disposition=AttachmentParserClaimDisposition.GAP_ALREADY_CLAIMED,
            coverage=existing.coverage,
        )

    def get_coverage(
        self,
        query: QueryExecutor,
        *,
        gap_fingerprint: str,
    ) -> AttachmentParserCoverageRecord | None:
        gap = _required_id(gap_fingerprint, "gap_fingerprint", maximum=512)
        row = query.fetch_one(
            f"SELECT {self._COVERAGE_FIELDS} FROM attachment_parser "
            "WHERE gap_fingerprint = %s",
            (gap,),
        )
        return None if row is None else _coverage(row)

    def get_owner_claim_by_gap(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        gap_fingerprint: str,
    ) -> AttachmentParserRecord | None:
        owner = _required_id(owner_id, "owner_id")
        gap = _required_id(gap_fingerprint, "gap_fingerprint", maximum=512)
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM attachment_parser "
            "WHERE gap_fingerprint = %s AND requested_by = %s",
            (gap, owner),
        )
        return None if row is None else _owned_parser(row, owner)

    def get_owner_claim_by_draft(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        draft_agent_id: str,
    ) -> AttachmentParserRecord | None:
        owner = _required_id(owner_id, "owner_id")
        draft = _required_id(draft_agent_id, "draft_agent_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM attachment_parser "
            "WHERE draft_agent_id = %s AND requested_by = %s",
            (draft, owner),
        )
        return None if row is None else _owned_parser(row, owner)

    def get_by_draft_for_administration(
        self,
        query: QueryExecutor,
        *,
        draft_agent_id: str,
    ) -> AttachmentParserRecord | None:
        draft = _required_id(draft_agent_id, "draft_agent_id")
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM attachment_parser WHERE draft_agent_id = %s",
            (draft,),
        )
        return None if row is None else _parser(row)

    def list_owner_claims(
        self,
        query: QueryExecutor,
        *,
        owner_id: str,
        status: AttachmentParserStatus | str,
        limit: int = 200,
    ) -> tuple[AttachmentParserRecord, ...]:
        owner = _required_id(owner_id, "owner_id")
        lifecycle = _status(status, "status")
        maximum = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"SELECT {self._FIELDS} FROM attachment_parser "
            "WHERE requested_by = %s AND status = %s "
            "ORDER BY created_at DESC, id ASC LIMIT %s",
            (owner, lifecycle.value, maximum),
        )
        return tuple(_owned_parser(row, owner) for row in rows)

    def list_by_status_for_administration(
        self,
        query: QueryExecutor,
        *,
        status: AttachmentParserStatus | str,
        limit: int = 200,
    ) -> tuple[AttachmentParserRecord, ...]:
        lifecycle = _status(status, "status")
        maximum = _bounded_limit(limit, maximum=1000)
        rows = query.fetch_all(
            f"SELECT {self._FIELDS} FROM attachment_parser WHERE status = %s "
            "ORDER BY created_at DESC, id ASC LIMIT %s",
            (lifecycle.value, maximum),
        )
        return tuple(_parser(row) for row in rows)

    def mark_status_for_owner(
        self,
        transaction: Transaction,
        *,
        owner_id: str,
        gap_fingerprint: str,
        expected_status: AttachmentParserStatus | str,
        expected_updated_at: int,
        status: AttachmentParserStatus | str,
        updated_at: int,
    ) -> AttachmentParserRecord:
        owner = _required_id(owner_id, "owner_id")
        gap = _required_id(gap_fingerprint, "gap_fingerprint", maximum=512)
        expected = _status(expected_status, "expected_status")
        target = _status(status, "status")
        if expected is not AttachmentParserStatus.PENDING or target not in {
            AttachmentParserStatus.FAILED,
            AttachmentParserStatus.DISCARDED,
        }:
            raise RepositoryValidationError(
                "owner parser transition must move pending to failed or discarded"
            )
        previous, current = _timestamps(expected_updated_at, updated_at)
        result = transaction.execute(
            f"""
            UPDATE attachment_parser
               SET status = %s, updated_at = %s
             WHERE gap_fingerprint = %s AND requested_by = %s
               AND status = %s AND updated_at = %s
            RETURNING {self._FIELDS}
            """,
            (target.value, current, gap, owner, expected.value, previous),
        )
        returned = getattr(result, "returned_records", ())
        if returned:
            return _owned_parser(
                _single_returned(result, "attachment_parser.owner_transition"), owner
            )
        existing = self.get_owner_claim_by_gap(
            transaction, owner_id=owner, gap_fingerprint=gap
        )
        _raise_transition_miss(existing, operation="attachment_parser.owner_transition")

    def mark_live_for_administration(
        self,
        transaction: Transaction,
        *,
        gap_fingerprint: str,
        expected_status: AttachmentParserStatus | str,
        expected_updated_at: int,
        live_agent_id: str,
        tool_name: str,
        approved_by: str,
        updated_at: int,
    ) -> AttachmentParserRecord:
        gap = _required_id(gap_fingerprint, "gap_fingerprint", maximum=512)
        expected = _status(expected_status, "expected_status")
        if expected is not AttachmentParserStatus.PENDING:
            raise RepositoryValidationError("only a pending parser may be promoted live")
        agent = _required_id(live_agent_id, "live_agent_id")
        tool = _bounded_text(tool_name, "tool_name", maximum=512)
        approver = _required_id(approved_by, "approved_by")
        previous, current = _timestamps(expected_updated_at, updated_at)
        result = transaction.execute(
            f"""
            UPDATE attachment_parser
               SET status = 'live', live_agent_id = %s, tool_name = %s,
                   approved_by = %s, updated_at = %s
             WHERE gap_fingerprint = %s AND status = %s AND updated_at = %s
            RETURNING {self._FIELDS}
            """,
            (agent, tool, approver, current, gap, expected.value, previous),
        )
        returned = getattr(result, "returned_records", ())
        if returned:
            return _parser(_single_returned(result, "attachment_parser.promote"))
        existing = self._get_for_administration(transaction, gap_fingerprint=gap)
        _raise_transition_miss(existing, operation="attachment_parser.promote")

    def _get_for_administration(
        self,
        query: QueryExecutor,
        *,
        gap_fingerprint: str,
    ) -> AttachmentParserRecord | None:
        row = query.fetch_one(
            f"SELECT {self._FIELDS} FROM attachment_parser WHERE gap_fingerprint = %s",
            (gap_fingerprint,),
        )
        return None if row is None else _parser(row)


def _raise_transition_miss(existing: object, *, operation: str) -> None:
    if existing is None:
        raise RepositoryNotFoundError(
            "owner-scoped parser claim was not found", metadata={"operation": operation}
        )
    raise RepositoryConflictError(
        "parser lifecycle fence is stale", metadata={"operation": operation}
    )


def _timestamps(expected_updated_at: object, updated_at: object) -> tuple[int, int]:
    previous = _non_negative_int(expected_updated_at, "expected_updated_at")
    current = _non_negative_int(updated_at, "updated_at")
    if current <= previous:
        raise RepositoryValidationError("updated_at must advance the lifecycle fence")
    return previous, current


def _status(value: object, field: str) -> AttachmentParserStatus:
    try:
        return AttachmentParserStatus(str(value))
    except ValueError as exc:
        raise RepositoryValidationError(f"{field} is unsupported") from exc


def _optional_text(value: object, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field, maximum=maximum, allow_empty=True)


def _optional_id(value: object, field: str) -> str | None:
    return None if value is None else _required_id(value, field)


def _stored_text(value: object, field: str, *, maximum: int = 512) -> str:
    try:
        return _required_id(value, field, maximum=maximum)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted parser text is invalid", metadata={"field": field}
        ) from exc


def _stored_optional_text(
    value: object,
    field: str,
    *,
    maximum: int = 512,
    allow_empty: bool = False,
) -> str | None:
    if value is None:
        return None
    try:
        return _bounded_text(value, field, maximum=maximum, allow_empty=allow_empty)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted parser text is invalid", metadata={"field": field}
        ) from exc


def _stored_status(value: object) -> AttachmentParserStatus:
    try:
        return _status(value, "status")
    except RepositoryValidationError as exc:
        raise RepositoryDataError("persisted parser status is unsupported") from exc


def _stored_time(value: object, field: str) -> int:
    try:
        return _non_negative_int(value, field)
    except RepositoryValidationError as exc:
        raise RepositoryDataError(
            "persisted parser timestamp is invalid", metadata={"field": field}
        ) from exc


def _coverage(row: Mapping[str, Any]) -> AttachmentParserCoverageRecord:
    status = _stored_status(_row_value(row, "status"))
    live_agent_id = _stored_optional_text(row.get("live_agent_id"), "live_agent_id")
    tool_name = _stored_optional_text(row.get("tool_name"), "tool_name")
    if status is AttachmentParserStatus.LIVE and (
        live_agent_id is None or tool_name is None
    ):
        raise RepositoryDataError("persisted live parser has incomplete coverage")
    return AttachmentParserCoverageRecord(
        parser_id=_stored_text(_row_value(row, "id"), "parser_id"),
        extension=_stored_optional_text(
            row.get("extension"), "extension", maximum=64, allow_empty=True
        ),
        category=_stored_text(_row_value(row, "category"), "category", maximum=128),
        gap_fingerprint=_stored_text(
            _row_value(row, "gap_fingerprint"), "gap_fingerprint"
        ),
        status=status,
        live_agent_id=live_agent_id,
        tool_name=tool_name,
        updated_at=_stored_time(_row_value(row, "updated_at"), "updated_at"),
    )


def _parser(row: Mapping[str, Any]) -> AttachmentParserRecord:
    coverage = _coverage(row)
    created_at = _stored_time(_row_value(row, "created_at"), "created_at")
    if coverage.updated_at < created_at:
        raise RepositoryDataError("persisted parser lifecycle timestamps are inconsistent")
    return AttachmentParserRecord(
        parser_id=coverage.parser_id,
        extension=coverage.extension,
        category=coverage.category,
        gap_fingerprint=coverage.gap_fingerprint,
        status=coverage.status,
        draft_agent_id=_stored_optional_text(row.get("draft_agent_id"), "draft_agent_id"),
        live_agent_id=coverage.live_agent_id,
        tool_name=coverage.tool_name,
        source_attachment_id=_stored_optional_text(
            row.get("source_attachment_id"), "source_attachment_id"
        ),
        source_conversation_id=_stored_optional_text(
            row.get("source_chat_id"), "source_conversation_id"
        ),
        requested_by=_stored_optional_text(row.get("requested_by"), "requested_by"),
        approved_by=_stored_optional_text(row.get("approved_by"), "approved_by"),
        created_at=created_at,
        updated_at=coverage.updated_at,
    )


def _owned_parser(row: Mapping[str, Any], owner_id: str) -> AttachmentParserRecord:
    record = _parser(row)
    if record.requested_by != owner_id:
        raise RepositoryDataError("parser owner query returned foreign provenance")
    return record


__all__ = (
    "AttachmentParserClaimDisposition",
    "AttachmentParserClaimResult",
    "AttachmentParserCoverageRecord",
    "AttachmentParserRecord",
    "AttachmentParserRepository",
    "AttachmentParserStatus",
)
