"""Product adapter for Plane's global auto-parser registry.

Feature 031-attachment-upload-parsing. One row per file-type gap, keyed by a
unique ``gap_fingerprint`` so the same unreadable type never spawns a second
draft while one is pending/live (FR-018). Carries the dedup key, lifecycle
status, the backing draft/live agent + tool, and provenance (who requested it,
which attachment/chat triggered it, which admin approved it).
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

# Lifecycle states for a registry row.
STATUS_PENDING = "pending"
STATUS_LIVE = "live"
STATUS_FAILED = "failed"
STATUS_DISCARDED = "discarded"


class AttachmentParserRepository:
    """Product-shape adapter over Plane's owner-safe parser registry."""

    def __init__(
        self,
        db=None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
    ) -> None:
        self.db = db
        repository, runtime = repository_from(
            "attachment_parsers",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._parsers = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )

    @classmethod
    def from_plane_source(cls, source) -> "AttachmentParserRepository":
        """Bind to an application-scoped Plane runtime/catalog source."""

        runtime = getattr(source, "plane_runtime", None) or getattr(source, "runtime", None)
        repositories = getattr(source, "plane_repositories", None) or getattr(
            source, "repositories", None
        )
        if runtime is None or repositories is None:
            raise RuntimeError("parser persistence requires the application Plane runtime")
        return cls(plane_runtime=runtime, plane_repositories=repositories)

    @staticmethod
    def _coverage_dict(record) -> dict:
        return {
            "id": record.parser_id,
            "extension": record.extension,
            "category": record.category,
            "gap_fingerprint": record.gap_fingerprint,
            "status": record.status.value,
            "live_agent_id": record.live_agent_id,
            "tool_name": record.tool_name,
            "updated_at": record.updated_at,
        }

    @classmethod
    def _record_dict(cls, record) -> dict:
        value = cls._coverage_dict(record.coverage)
        value.update(
            {
                "draft_agent_id": record.draft_agent_id,
                "source_attachment_id": record.source_attachment_id,
                "source_chat_id": record.source_conversation_id,
                "requested_by": record.requested_by,
                "approved_by": record.approved_by,
                "created_at": record.created_at,
            }
        )
        return value

    def get_by_gap(self, gap_fingerprint: str) -> Optional[dict]:
        """Return only non-sensitive global coverage for one format gap."""

        record = self._parsers.call(
            self._parsers.repository.get_coverage,
            gap_fingerprint=gap_fingerprint,
        )
        return None if record is None else self._coverage_dict(record)

    def get_by_draft(
        self,
        draft_agent_id: str,
        *,
        owner_user_id: Optional[str] = None,
        for_administration: bool = False,
    ) -> Optional[dict]:
        """Return provenance only under an owner or authorized-admin context."""

        if for_administration:
            operation = self._parsers.repository.get_by_draft_for_administration
            kwargs = {"draft_agent_id": draft_agent_id}
        elif owner_user_id:
            operation = self._parsers.repository.get_owner_claim_by_draft
            kwargs = {
                "owner_id": owner_user_id,
                "draft_agent_id": draft_agent_id,
            }
        else:
            raise ValueError("parser draft provenance requires an owner or administrator")
        record = self._parsers.call(operation, **kwargs)
        return None if record is None else self._record_dict(record)

    def create_pending(
        self,
        *,
        gap_fingerprint: str,
        category: str,
        extension: Optional[str],
        draft_agent_id: Optional[str],
        source_attachment_id: Optional[str],
        source_chat_id: Optional[str],
        requested_by: Optional[str],
    ) -> dict:
        """Insert a new ``pending`` registry row and return it.

        Idempotent against the unique ``gap_fingerprint``: if a row already
        exists for this gap it is returned unchanged (the caller treats that as
        a dedup hit and does NOT create a second draft).
        """
        if not requested_by:
            raise ValueError("parser claims require an owner")
        now_ms = int(time.time() * 1000)
        claim = self._parsers.call(
            self._parsers.repository.claim_pending,
            owner_id=requested_by,
            gap_fingerprint=gap_fingerprint,
            category=category,
            extension=extension,
            draft_agent_id=draft_agent_id,
            source_attachment_id=source_attachment_id,
            source_conversation_id=source_chat_id,
            claimed_at=now_ms,
        )
        if claim.owner_record is not None:
            return self._record_dict(claim.owner_record)
        return self._coverage_dict(claim.coverage)

    def mark_live(
        self,
        gap_fingerprint: str,
        *,
        live_agent_id: str,
        tool_name: str,
        approved_by: Optional[str],
    ) -> None:
        """Promote a registry row to ``live`` (global coverage)."""
        if not approved_by:
            raise ValueError("parser promotion requires an approving administrator")
        coverage = self._parsers.call(
            self._parsers.repository.get_coverage,
            gap_fingerprint=gap_fingerprint,
        )
        if coverage is None:
            return
        if coverage.status.value == STATUS_LIVE:
            if (
                coverage.live_agent_id == live_agent_id
                and coverage.tool_name == tool_name
            ):
                return
            raise RuntimeError("live parser coverage has conflicting semantics")
        now_ms = max(int(time.time() * 1000), coverage.updated_at + 1)
        self._parsers.call(
            self._parsers.repository.mark_live_for_administration,
            gap_fingerprint=gap_fingerprint,
            expected_status=coverage.status,
            expected_updated_at=coverage.updated_at,
            live_agent_id=live_agent_id,
            tool_name=tool_name,
            approved_by=approved_by,
            updated_at=now_ms,
        )

    def mark_status(
        self,
        gap_fingerprint: str,
        status: str,
        *,
        owner_user_id: str,
    ) -> None:
        """Set the lifecycle *status* (``failed``/``discarded``/``pending``)."""
        record = self._parsers.call(
            self._parsers.repository.get_owner_claim_by_gap,
            owner_id=owner_user_id,
            gap_fingerprint=gap_fingerprint,
        )
        if record is None:
            return
        if record.status.value == status:
            return
        self._parsers.call(
            self._parsers.repository.mark_status_for_owner,
            owner_id=owner_user_id,
            gap_fingerprint=gap_fingerprint,
            expected_status=record.status,
            expected_updated_at=record.updated_at,
            status=status,
            updated_at=max(int(time.time() * 1000), record.updated_at + 1),
        )

    def list_by_status(
        self,
        status: str,
        *,
        owner_user_id: Optional[str] = None,
        for_administration: bool = False,
    ) -> List[dict]:
        """List owner claims, or global provenance after admin authorization."""

        if for_administration:
            operation = self._parsers.repository.list_by_status_for_administration
            kwargs = {"status": status, "limit": 1000}
        elif owner_user_id:
            operation = self._parsers.repository.list_owner_claims
            kwargs = {
                "owner_id": owner_user_id,
                "status": status,
                "limit": 1000,
            }
        else:
            raise ValueError("parser provenance listing requires an owner or administrator")
        records = self._parsers.call(operation, **kwargs)
        return [self._record_dict(record) for record in records]

    # ── async facade (event-loop-safe twins of the sync methods above) ────
    async def aget_by_gap(self, gap_fingerprint: str) -> Optional[dict]:
        """Async twin of :meth:`get_by_gap`, run off the event loop."""
        return await asyncio.to_thread(self.get_by_gap, gap_fingerprint)

    async def aget_by_draft(self, draft_agent_id: str, **kwargs) -> Optional[dict]:
        """Async twin of :meth:`get_by_draft`, run off the event loop."""
        return await asyncio.to_thread(self.get_by_draft, draft_agent_id, **kwargs)

    async def acreate_pending(self, **kwargs) -> dict:
        """Async twin of :meth:`create_pending`, run off the event loop."""
        return await asyncio.to_thread(self.create_pending, **kwargs)

    async def amark_live(self, gap_fingerprint: str, *, live_agent_id: str,
                         tool_name: str, approved_by: Optional[str]) -> None:
        """Async twin of :meth:`mark_live`, run off the event loop."""
        return await asyncio.to_thread(
            self.mark_live, gap_fingerprint, live_agent_id=live_agent_id,
            tool_name=tool_name, approved_by=approved_by,
        )

    async def amark_status(self, gap_fingerprint: str, status: str, **kwargs) -> None:
        """Async twin of :meth:`mark_status`, run off the event loop."""
        return await asyncio.to_thread(
            self.mark_status,
            gap_fingerprint,
            status,
            **kwargs,
        )

    async def alist_by_status(self, status: str, **kwargs) -> List[dict]:
        """Async twin of :meth:`list_by_status`, run off the event loop."""
        return await asyncio.to_thread(self.list_by_status, status, **kwargs)


__all__ = [
    "AttachmentParserRepository",
    "STATUS_PENDING",
    "STATUS_LIVE",
    "STATUS_FAILED",
    "STATUS_DISCARDED",
]
