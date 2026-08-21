"""Product adapter for Plane-owned attachment persistence.

Application callers bind this adapter to the one initialized AstralPlane
runtime/catalog.  The temporary positional ``db`` argument remains only for
older focused tests while the shared Deep database facade is retired.  All
methods enforce user ownership; non-owner reads return ``None`` rather than
the row, so callers get a uniform "not found" surface.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from astralplane.repositories.artifacts import AttachmentRecord
from orchestrator.attachments.models import Attachment
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)


def _to_attachment(record: AttachmentRecord) -> Attachment:
    """Convert a detached Plane record to an :class:`Attachment`."""
    created_at = record.created_at
    if isinstance(created_at, (int, float)):
        created_at_dt = datetime.fromtimestamp(created_at / 1000.0, tz=timezone.utc)
    else:
        created_at_dt = created_at
    deleted_at = record.deleted_at
    deleted_at_dt = None
    if deleted_at is not None:
        if isinstance(deleted_at, (int, float)):
            deleted_at_dt = datetime.fromtimestamp(deleted_at / 1000.0, tz=timezone.utc)
        else:
            deleted_at_dt = deleted_at
    return Attachment(
        attachment_id=record.attachment_id,
        user_id=record.owner_id,
        filename=record.filename,
        content_type=record.content_type,
        category=record.category,
        extension=record.extension,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        storage_path=record.storage_locator,
        created_at=created_at_dt,
        deleted_at=deleted_at_dt,
    )


def _encode_cursor(created_at_ms: int, attachment_id: str) -> str:
    payload = json.dumps(
        {"created_at": created_at_ms, "attachment_id": attachment_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> Optional[Tuple[int, str]]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode()).decode()
        data = json.loads(raw)
        return int(data["created_at"]), str(data["attachment_id"])
    except Exception:
        return None


class AttachmentRepository:
    """Product model adapter over AstralPlane's artifact repositories."""

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
            "artifacts",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._artifacts = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )

    @classmethod
    def from_plane_source(cls, source) -> "AttachmentRepository":
        """Bind to an application-scoped Plane runtime/catalog source."""

        runtime = getattr(source, "plane_runtime", None) or getattr(source, "runtime", None)
        repositories = getattr(source, "plane_repositories", None) or getattr(
            source, "repositories", None
        )
        if runtime is None or repositories is None:
            raise RuntimeError("attachment persistence requires the application Plane runtime")
        return cls(plane_runtime=runtime, plane_repositories=repositories)

    def get_by_id(self, attachment_id: str, user_id: str) -> Optional[Attachment]:
        """Return the live attachment for *user_id*, or ``None`` if missing/foreign/deleted."""
        record = self._artifacts.call(
            self._artifacts.repository.attachments.get,
            owner_id=user_id,
            attachment_id=attachment_id,
        )
        return None if record is None else _to_attachment(record)

    def list_for_user(
        self,
        user_id: str,
        *,
        category: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Attachment], Optional[str]]:
        """Cursor-paginated listing of a user's live attachments, newest first."""
        limit = max(1, min(int(limit), 200))
        decoded = _decode_cursor(cursor) if cursor else None
        cursor_created_at, cursor_id = decoded if decoded else (None, None)
        page_limit = limit + 1 if limit < 200 else limit
        records = self._artifacts.call(
            self._artifacts.repository.attachments.list_live,
            owner_id=user_id,
            category=category,
            limit=page_limit,
            before_created_at=cursor_created_at,
            before_attachment_id=cursor_id,
        )
        items = [_to_attachment(record) for record in records[:limit]]
        next_cursor = None
        if len(records) > limit or (limit == 200 and len(records) == limit):
            last = records[limit - 1]
            next_cursor = _encode_cursor(last.created_at, last.attachment_id)
        return items, next_cursor

    # ── async facade (event-loop-safe twins of the sync reads above) ──────
    async def aget_by_id(self, attachment_id: str, user_id: str) -> Optional[Attachment]:
        """Async twin of :meth:`get_by_id`, run off the event loop."""
        return await asyncio.to_thread(self.get_by_id, attachment_id, user_id)

    async def alist_for_user(
        self,
        user_id: str,
        *,
        category: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Attachment], Optional[str]]:
        """Async twin of :meth:`list_for_user`, run off the event loop."""
        return await asyncio.to_thread(
            self.list_for_user, user_id, category=category, limit=limit, cursor=cursor,
        )

__all__ = ["AttachmentRepository"]
