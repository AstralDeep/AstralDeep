"""In-memory fake Database for Feature 031 repository/wiring tests.

Recognizes exactly the queries issued by AttachmentRepository,
MessageAttachmentRepository, and AttachmentParserRepository (``?`` placeholder
dialect) and serves them from per-table lists of dicts. Mirrors the approach in
``conftest.StubDatabase`` but extends coverage to the two new tables.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import List, Optional, Tuple

from astralplane.repositories.artifacts import MessageAttachmentRecord
from astralplane.repositories.attachment_parsers import (
    AttachmentParserClaimDisposition,
    AttachmentParserClaimResult,
    AttachmentParserRecord,
    AttachmentParserStatus,
)
from tests.attachments.conftest import _AttachmentPlaneRepository


class _MessageAttachmentPlaneRepository:
    def __init__(self) -> None:
        self.records: list[MessageAttachmentRecord] = []

    def link(self, _transaction, **values) -> MessageAttachmentRecord:
        record = MessageAttachmentRecord(
            link_id=values["link_id"],
            conversation_id=values["conversation_id"],
            message_id=(
                None
                if values["message_id"] is None
                else str(values["message_id"])
            ),
            attachment_id=values["attachment_id"],
            owner_id=values["owner_id"],
            created_at=values["created_at"],
        )
        self.records.append(record)
        return record

    def list_for_conversation(
        self,
        _transaction,
        *,
        owner_id: str,
        conversation_id: str,
    ) -> tuple[MessageAttachmentRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self.records
                    if record.owner_id == owner_id
                    and record.conversation_id == conversation_id
                ),
                key=lambda record: (record.created_at, record.link_id),
            )
        )

    def list_for_message(
        self,
        _transaction,
        *,
        owner_id: str,
        message_id: str,
    ) -> tuple[MessageAttachmentRecord, ...]:
        identity = str(message_id)
        return tuple(
            sorted(
                (
                    record
                    for record in self.records
                    if record.owner_id == owner_id
                    and record.message_id == identity
                ),
                key=lambda record: (record.created_at, record.link_id),
            )
        )


class _ArtifactPlaneRepository:
    def __init__(self) -> None:
        attachments = _AttachmentPlaneRepository()
        self.attachments = attachments
        self.materializations = attachments
        self.message_attachments = _MessageAttachmentPlaneRepository()


class _ParserPlaneRepository:
    def __init__(self) -> None:
        self.records: list[AttachmentParserRecord] = []

    def claim_pending(self, _transaction, **values) -> AttachmentParserClaimResult:
        existing = next(
            (
                record
                for record in self.records
                if record.gap_fingerprint == values["gap_fingerprint"]
            ),
            None,
        )
        if existing is not None:
            return AttachmentParserClaimResult(
                disposition=(
                    AttachmentParserClaimDisposition.OWNER_REPLAY
                    if existing.requested_by == values["owner_id"]
                    else AttachmentParserClaimDisposition.GAP_ALREADY_CLAIMED
                ),
                coverage=existing.coverage,
                owner_record=(
                    existing if existing.requested_by == values["owner_id"] else None
                ),
            )
        record = AttachmentParserRecord(
            parser_id=str(uuid.uuid4()),
            extension=values["extension"],
            category=values["category"],
            gap_fingerprint=values["gap_fingerprint"],
            status=AttachmentParserStatus.PENDING,
            draft_agent_id=values["draft_agent_id"],
            live_agent_id=None,
            tool_name=None,
            source_attachment_id=values["source_attachment_id"],
            source_conversation_id=values["source_conversation_id"],
            requested_by=values["owner_id"],
            approved_by=None,
            created_at=values["claimed_at"],
            updated_at=values["claimed_at"],
        )
        self.records.append(record)
        return AttachmentParserClaimResult(
            disposition=AttachmentParserClaimDisposition.CLAIMED,
            coverage=record.coverage,
            owner_record=record,
        )

    def get_coverage(self, _transaction, *, gap_fingerprint: str):
        record = self._by_gap(gap_fingerprint)
        return None if record is None else record.coverage

    def get_owner_claim_by_gap(
        self,
        _transaction,
        *,
        owner_id: str,
        gap_fingerprint: str,
    ):
        record = self._by_gap(gap_fingerprint)
        return record if record is not None and record.requested_by == owner_id else None

    def get_owner_claim_by_draft(
        self,
        _transaction,
        *,
        owner_id: str,
        draft_agent_id: str,
    ):
        return next(
            (
                record
                for record in self.records
                if record.requested_by == owner_id
                and record.draft_agent_id == draft_agent_id
            ),
            None,
        )

    def get_by_draft_for_administration(
        self,
        _transaction,
        *,
        draft_agent_id: str,
    ):
        return next(
            (
                record
                for record in self.records
                if record.draft_agent_id == draft_agent_id
            ),
            None,
        )

    def mark_live_for_administration(self, _transaction, **values):
        record = self._by_gap(values["gap_fingerprint"])
        assert record is not None
        updated = replace(
            record,
            status=AttachmentParserStatus.LIVE,
            live_agent_id=values["live_agent_id"],
            tool_name=values["tool_name"],
            approved_by=values["approved_by"],
            updated_at=values["updated_at"],
        )
        self.records[self.records.index(record)] = updated
        return updated

    def mark_status_for_owner(self, _transaction, **values):
        record = self.get_owner_claim_by_gap(
            _transaction,
            owner_id=values["owner_id"],
            gap_fingerprint=values["gap_fingerprint"],
        )
        assert record is not None
        updated = replace(
            record,
            status=AttachmentParserStatus(values["status"]),
            updated_at=values["updated_at"],
        )
        self.records[self.records.index(record)] = updated
        return updated

    def list_owner_claims(self, _transaction, *, owner_id: str, status, limit: int):
        lifecycle = AttachmentParserStatus(status)
        return tuple(
            record
            for record in reversed(self.records)
            if record.requested_by == owner_id and record.status is lifecycle
        )[:limit]

    def list_by_status_for_administration(
        self,
        _transaction,
        *,
        status,
        limit: int,
    ):
        lifecycle = AttachmentParserStatus(status)
        return tuple(
            record for record in reversed(self.records) if record.status is lifecycle
        )[:limit]

    def _by_gap(self, gap_fingerprint: str):
        return next(
            (
                record
                for record in self.records
                if record.gap_fingerprint == gap_fingerprint
            ),
            None,
        )


class _Cursor:
    def __init__(self, rowcount: int = 0):
        self.rowcount = rowcount


class FakeDB:
    def __init__(self) -> None:
        self.user_attachments: List[dict] = []
        self.message_attachment: List[dict] = []
        self.attachment_parser: List[dict] = []
        artifacts = _ArtifactPlaneRepository()
        parsers = _ParserPlaneRepository()
        self.plane_repositories = SimpleNamespace(
            artifacts=artifacts,
            attachment_parsers=parsers,
        )
        self.message_attachment = artifacts.message_attachments.records
        self.attachment_parser = parsers.records

    # -- writes -------------------------------------------------------------
    def execute(self, query: str, params: Tuple = ()) -> _Cursor:
        q = " ".join(query.split()).lower()
        if q.startswith("insert into user_attachments"):
            (aid, uid, fn, ctype, cat, ext, size, sha, path, created) = params
            self.user_attachments.append({
                "attachment_id": aid, "user_id": uid, "filename": fn,
                "content_type": ctype, "category": cat, "extension": ext,
                "size_bytes": size, "sha256": sha, "storage_path": path,
                "created_at": created, "deleted_at": None,
            })
            return _Cursor(1)
        if q.startswith("insert into message_attachment"):
            (rid, chat_id, message_id, aid, uid, created) = params
            self.message_attachment.append({
                "id": rid, "chat_id": chat_id, "message_id": message_id,
                "attachment_id": aid, "user_id": uid, "created_at": created,
            })
            return _Cursor(1)
        if q.startswith("insert into attachment_parser"):
            (rid, ext, cat, gap, status, draft_id, src_att, src_chat,
             requested_by, created, updated) = params
            self.attachment_parser.append({
                "id": rid, "extension": ext, "category": cat,
                "gap_fingerprint": gap, "status": status,
                "draft_agent_id": draft_id, "live_agent_id": None,
                "tool_name": None, "source_attachment_id": src_att,
                "source_chat_id": src_chat, "requested_by": requested_by,
                "approved_by": None, "created_at": created, "updated_at": updated,
            })
            return _Cursor(1)
        if q.startswith("update attachment_parser") and "set status = ?, live_agent_id" in q:
            status, live_id, tool, approved, updated, gap = params
            for r in self.attachment_parser:
                if r["gap_fingerprint"] == gap:
                    r.update(status=status, live_agent_id=live_id, tool_name=tool,
                             approved_by=approved, updated_at=updated)
            return _Cursor(1)
        if q.startswith("update attachment_parser") and "set status = ?, updated_at" in q:
            status, updated, gap = params
            for r in self.attachment_parser:
                if r["gap_fingerprint"] == gap:
                    r.update(status=status, updated_at=updated)
            return _Cursor(1)
        raise NotImplementedError(query)

    # -- reads --------------------------------------------------------------
    def fetch_one(self, query: str, params: Tuple = ()) -> Optional[dict]:
        q = " ".join(query.split()).lower()
        if "from user_attachments where attachment_id = ? and user_id = ? and deleted_at is null" in q:
            aid, uid = params
            for r in self.user_attachments:
                if r["attachment_id"] == aid and r["user_id"] == uid and r["deleted_at"] is None:
                    return dict(r)
            return None
        if "from user_attachments where attachment_id = ?" in q:
            (aid,) = params
            for r in self.user_attachments:
                if r["attachment_id"] == aid:
                    return dict(r)
            return None
        if "from attachment_parser where gap_fingerprint = ?" in q:
            (gap,) = params
            for r in self.attachment_parser:
                if r["gap_fingerprint"] == gap:
                    return dict(r)
            return None
        if "from attachment_parser where draft_agent_id = ?" in q:
            (did,) = params
            for r in self.attachment_parser:
                if r["draft_agent_id"] == did:
                    return dict(r)
            return None
        raise NotImplementedError(query)

    def fetch_all(self, query: str, params: Tuple = ()) -> List[dict]:
        q = " ".join(query.split()).lower()
        if "from message_attachment where message_id = ? and user_id = ?" in q:
            mid, uid = params
            rows = [r for r in self.message_attachment if r["message_id"] == mid and r["user_id"] == uid]
            return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"])]
        if "from message_attachment where chat_id = ? and user_id = ?" in q:
            cid, uid = params
            rows = [r for r in self.message_attachment if r["chat_id"] == cid and r["user_id"] == uid]
            return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"])]
        if "from attachment_parser where status = ?" in q:
            (status,) = params
            rows = [r for r in self.attachment_parser if r["status"] == status]
            return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)]
        raise NotImplementedError(query)
